"""A29 / gate F4 — Migration Capacity Preflight.

What this exists to prevent: a campaign that starts happily on a site with 40GB free,
writes 2.4M snapshot rows plus their indexes, and fills the volume the *database* lives on
— taking the live ERP down in the middle of a migration whose whole premise is that the
site stays up.

So the numbers are **measured, never assumed**. Every quantity below is read out of
`information_schema`, `/proc`, the Redis INFO block or the filesystem, and the projection
to production scale is a straight multiplication of a measured per-row cost. The
"1.5–2.5GB InnoDB" figure in the design is planning information and is not used here.

A production-scale campaign start is refused when a threshold is not met (A29). A small
campaign still records its numbers — that is where the per-row costs come from in the first
place, and it is what the 100k rehearsal is for.
"""

import os
import shutil

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

CAMPAIGN_DOCTYPE = "Cloud Migration Campaign"
OBJECT_DOCTYPE = "Cloud Migration Object"
REF_DOCTYPE = "Cloud Migration File Ref"
CONFLICT_DOCTYPE = "Cloud Migration Conflict"
AUDIT_DOCTYPE = "Cloud Storage Audit Log"

CSO_DOCTYPE = "Cloud Storage Object"

#: Every table a campaign grows. `Cloud Storage Object` is here because migration creates one
#: per unique content — 2.2KB per migrated object on the P5 rehearsal corpus, a fifth of the
#: total — and leaving it out understated the projection by that much on the one gate whose
#: job is to refuse a start that will not fit. Anything added to a campaign's write path
#: belongs here; the projection is only as honest as this tuple.
MEASURED_TABLES = (OBJECT_DOCTYPE, REF_DOCTYPE, CONFLICT_DOCTYPE, AUDIT_DOCTYPE, CSO_DOCTYPE)

#: Campaigns at or above this many objects are "production scale" and must pass.
PRODUCTION_SCALE_OBJECTS = 100_000

#: The corpus the projection targets (PLAN §A: ~1.2M File rows / ~100GB).
PROJECTION_ROWS = 1_200_000

#: Per-object costs measured on the P5 100,000-file rehearsal **after CLEANUP finished**,
#: recorded in `docs/evidence/p5-rehearsal/measure_postcleanup.json`.
#:
#: These exist because the gate runs at the wrong moment to measure itself. `assert_startable`
#: fires at campaign **start**, when `Cloud Storage Object` has ~0 rows and the audit log has
#: no `file_link` or `local_quarantine` rows at all — so a projection derived purely from the
#: live database gives those two tables a ratio of ~0 and silently drops ~3.8KB of the
#: measured 8.8KB per object. The gate would then demand about half the free space a campaign
#: actually needs, which is the direction in which a capacity gate passes a start it should
#: refuse. `project()` therefore takes the **larger** of what it measures and what the
#: rehearsal measured, per table and per quantity.
#:
#: Upper-bound caveat, preserved deliberately: InnoDB does not return extents to the
#: filesystem, so post-CLEANUP sizes include free space inside the tablespace. For a capacity
#: gate that is the right direction to err in.
CALIBRATION = {
	OBJECT_DOCTYPE: {"rows_per_object": 1.0, "bytes_per_row": 3388.19, "index_bytes_per_row": 1670.34},
	REF_DOCTYPE: {"rows_per_object": 1.00122, "bytes_per_row": 1693.99, "index_bytes_per_row": 788.59},
	CONFLICT_DOCTYPE: {"rows_per_object": 0.00155, "bytes_per_row": 1479.85, "index_bytes_per_row": 739.92},
	AUDIT_DOCTYPE: {"rows_per_object": 1.99848, "bytes_per_row": 770.56, "index_bytes_per_row": 200.24},
	CSO_DOCTYPE: {"rows_per_object": 0.997441, "bytes_per_row": 2223.48, "index_bytes_per_row": 1313.83},
}

#: Free space must be this multiple of the projected database growth. Two, because InnoDB
#: needs room to rebuild an index and because the site is live and still writing.
DISK_SAFETY_FACTOR = 2.0

#: How old the most recent database backup may be. A29 requires a verified recent backup:
#: the snapshot tables are droppable, but the `tabFile` link writes are not.
MAX_BACKUP_AGE_HOURS = 24


def table_metrics(doctype: str) -> dict:
	"""Measured bytes and rows for one table, straight out of `information_schema`.

	`TABLE_ROWS` is InnoDB's estimate, so the row count is taken from a real `COUNT(*)`
	instead — at rehearsal scale that is affordable and the per-row cost it feeds is the
	whole point of the exercise.
	"""
	table = f"tab{doctype}"
	row = frappe.db.sql(
		"""
		SELECT DATA_LENGTH AS data_bytes, INDEX_LENGTH AS index_bytes, DATA_FREE AS free_bytes
		FROM information_schema.TABLES
		WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %(table)s
		""",
		{"table": table},
		as_dict=True,
	)
	if not row:
		return {"doctype": doctype, "rows": 0, "data_bytes": 0, "index_bytes": 0, "total_bytes": 0}

	# `frappe.db.count`, not a raw COUNT: since `Cloud Storage Object` joined
	# `MEASURED_TABLES` this line would otherwise be raw SQL against a guarded table at an
	# unsanctioned site (docs/INVARIANTS.md invariants 4 and 5). The invariant is not conditioned on
	# the statement being read-only, and the effect here being benign is not the point.
	rows = cint(frappe.db.count(doctype))
	data_bytes = cint(row[0].data_bytes)
	index_bytes = cint(row[0].index_bytes)
	return {
		"doctype": doctype,
		"rows": rows,
		"data_bytes": data_bytes,
		"index_bytes": index_bytes,
		"total_bytes": data_bytes + index_bytes,
		"bytes_per_row": round((data_bytes + index_bytes) / rows, 2) if rows else 0.0,
		"index_bytes_per_row": round(index_bytes / rows, 2) if rows else 0.0,
	}


def process_metrics() -> dict:
	"""Peak RSS and cumulative block I/O of this process, from the kernel."""
	import resource

	usage = resource.getrusage(resource.RUSAGE_SELF)
	metrics = {"peak_rss_bytes": cint(usage.ru_maxrss) * 1024}

	try:
		with open("/proc/self/io") as handle:  # nosemgrep: frappe-security-file-traversal
			for line in handle:
				key, _, value = line.partition(":")
				if key in ("read_bytes", "write_bytes", "rchar", "wchar"):
					metrics[key] = cint(value.strip())
	except OSError:
		# Not Linux, or /proc unavailable. Recorded as absent rather than as zero: a zero
		# would read as "no I/O", which is a different claim from "not measured here".
		metrics["io_unavailable"] = True

	return metrics


def redis_metrics() -> dict:
	"""Queue-Redis memory and the depth of the migration queue."""
	metrics = {}
	try:
		from frappe.utils.background_jobs import get_queue

		queue = get_queue("cloud_migration")
		metrics["queue_depth"] = len(queue)
		info = queue.connection.info(section="memory")
		metrics["redis_used_memory_bytes"] = cint(info.get("used_memory"))
		metrics["redis_peak_memory_bytes"] = cint(info.get("used_memory_peak"))
	except Exception as exc:  # noqa: BLE001 - an unconfigured queue is a finding, not a crash
		metrics["redis_unavailable"] = str(exc)
	return metrics


def disk_metrics() -> dict:
	"""Free space where the site (and, on this bench, the database) lives."""
	site_path = frappe.get_site_path()
	usage = shutil.disk_usage(site_path)
	metrics = {
		"site_path": os.path.abspath(site_path),
		"disk_total_bytes": usage.total,
		"disk_used_bytes": usage.used,
		"disk_free_bytes": usage.free,
	}

	datadir = frappe.db.sql("SELECT @@datadir")[0][0]
	metrics["db_datadir"] = datadir
	try:
		db_usage = shutil.disk_usage(datadir)
		metrics["db_disk_free_bytes"] = db_usage.free
		metrics["db_disk_total_bytes"] = db_usage.total
	except OSError:
		metrics["db_disk_free_bytes"] = usage.free
		metrics["db_disk_total_bytes"] = usage.total

	metrics["innodb_file_per_table"] = bool(cint(frappe.db.sql("SELECT @@innodb_file_per_table")[0][0]))
	return metrics


def latest_backup() -> dict:
	"""The most recent database dump on disk, and its age."""
	directory = frappe.get_site_path("private", "backups")
	newest = None
	if os.path.isdir(directory):
		for entry in os.scandir(directory):
			if entry.is_file() and entry.name.endswith(("database.sql.gz", "database.sql")):
				if newest is None or entry.stat().st_mtime > newest.stat().st_mtime:
					newest = entry

	if newest is None:
		return {"present": False}

	from datetime import datetime

	taken_at = datetime.fromtimestamp(newest.stat().st_mtime)
	age_hours = (datetime.now() - taken_at).total_seconds() / 3600
	return {
		"present": True,
		"path": newest.path,
		"size_bytes": newest.stat().st_size,
		"taken_at": taken_at.isoformat(),
		"age_hours": round(age_hours, 2),
	}


def measure(campaign: str | None = None) -> dict:
	"""Every F4 quantity, measured now."""
	tables = {doctype: table_metrics(doctype) for doctype in MEASURED_TABLES}
	measured = {
		"measured_at": now_datetime().isoformat(),
		"tables": tables,
		"process": process_metrics(),
		"redis": redis_metrics(),
		"disk": disk_metrics(),
		"backup": latest_backup(),
	}
	if campaign:
		measured["campaign"] = {
			"name": campaign,
			"objects": frappe.db.count(OBJECT_DOCTYPE, {"campaign": campaign}),
			"refs": frappe.db.count(REF_DOCTYPE, {"campaign": campaign}),
			"conflicts": frappe.db.count(CONFLICT_DOCTYPE, {"campaign": campaign}),
			"audit_rows": frappe.db.count(AUDIT_DOCTYPE, {"campaign": campaign}),
		}
	return measured


def project(measured: dict, *, target_rows: int = PROJECTION_ROWS) -> dict:
	"""Scale the measured per-row costs up to a production corpus.

	Every table is projected on its **measured ratio to the object count**, not on the object
	count itself: shared URLs mean there are more File Refs than objects, dedup means there
	are slightly fewer Cloud Storage Objects, and the audit log carries roughly two rows per
	object (one `file_link`, one `local_quarantine`). Assuming 1:1 anywhere understates
	whichever table is the biggest, which is the direction that matters for a gate that
	refuses a start.

	**Measure after CLEANUP, not before.** Two of these tables only reach their full size
	during it — the audit log roughly doubles, and the object table grows as
	`quarantine_path` and `cleaned_at` fill in. A projection taken as CLEANUP starts is not a
	projection of the campaign.
	"""
	tables = measured.get("tables", {})
	objects_table = tables.get(OBJECT_DOCTYPE, {})
	measured_objects = cint(objects_table.get("rows"))

	projected: dict = {"target_rows": target_rows}
	total = 0
	total_index = 0

	for doctype in MEASURED_TABLES:
		metrics = tables.get(doctype, {})
		rows = cint(metrics.get("rows"))
		calibrated = CALIBRATION.get(doctype, {})

		measured_ratio = (rows / measured_objects) if measured_objects else 0.0
		# `max`, not "prefer measured": a campaign measured before UPLOAD has ~0 rows in the
		# tables CLEANUP fills, and taking that at face value is how the gate came to project
		# roughly six gigabytes where the rehearsal measured ten and a half. A site whose real
		# costs exceed the rehearsal's still wins, because its own measurement is larger.
		ratio = max(measured_ratio, float(calibrated.get("rows_per_object") or 0.0))
		per_row = max(
			float(metrics.get("bytes_per_row") or 0.0), float(calibrated.get("bytes_per_row") or 0.0)
		)
		index_per_row = max(
			float(metrics.get("index_bytes_per_row") or 0.0),
			float(calibrated.get("index_bytes_per_row") or 0.0),
		)
		scaled_rows = target_rows * ratio

		entry = {
			"rows": int(scaled_rows),
			"rows_per_object": round(ratio, 6),
			"measured_rows_per_object": round(measured_ratio, 6),
			"calibrated": ratio > measured_ratio or per_row > float(metrics.get("bytes_per_row") or 0.0),
			"total_bytes": int(per_row * scaled_rows),
			"index_bytes": int(index_per_row * scaled_rows),
		}
		projected[doctype] = entry
		total += entry["total_bytes"]
		total_index += entry["index_bytes"]

	# Kept under their historical names so an existing report stays readable.
	projected["refs_per_object"] = projected.get(REF_DOCTYPE, {}).get("rows_per_object", 0.0)
	projected["conflicts_per_object"] = projected.get(CONFLICT_DOCTYPE, {}).get("rows_per_object", 0.0)
	projected["audit_rows_per_object"] = projected.get(AUDIT_DOCTYPE, {}).get("rows_per_object", 0.0)
	projected["cloud_objects_per_object"] = projected.get(CSO_DOCTYPE, {}).get("rows_per_object", 0.0)
	projected["total_bytes"] = total
	projected["total_index_bytes"] = total_index
	return projected


def evaluate(
	measured: dict,
	projected: dict,
	*,
	disk_safety_factor: float = DISK_SAFETY_FACTOR,
	max_backup_age_hours: int = MAX_BACKUP_AGE_HOURS,
) -> list[dict]:
	"""The threshold checks. Each returns its own verdict and the numbers behind it."""
	checks = []

	required = projected["total_bytes"] * disk_safety_factor
	free = cint(measured["disk"].get("db_disk_free_bytes"))
	checks.append(
		{
			"check": "database_free_space",
			"passed": free >= required,
			"required_bytes": int(required),
			"free_bytes": free,
			"detail": (
				f"projected snapshot growth {projected['total_bytes']} bytes × safety factor "
				f"{disk_safety_factor}"
			),
		}
	)

	checks.append(
		{
			"check": "innodb_file_per_table",
			"passed": bool(measured["disk"].get("innodb_file_per_table")),
			"detail": "a shared tablespace never returns the space a snapshot purge frees",
		}
	)

	backup = measured.get("backup") or {}
	# `or` would be wrong here and wrong in the direction that matters: a backup taken
	# seconds ago has `age_hours == 0.0`, which is falsy, so `age or 1e9` would treat the
	# freshest possible backup as the oldest possible one and refuse the campaign.
	age_hours = backup.get("age_hours")
	fresh = bool(backup.get("present")) and age_hours is not None and float(age_hours) <= max_backup_age_hours
	checks.append(
		{
			"check": "recent_database_backup",
			"passed": fresh,
			"age_hours": backup.get("age_hours"),
			"detail": ("the snapshot tables are droppable, but VERIFY's link writes to tabFile are not"),
		}
	)

	redis = measured.get("redis") or {}
	checks.append(
		{
			"check": "migration_queue_reachable",
			"passed": "redis_unavailable" not in redis,
			"queue_depth": redis.get("queue_depth"),
			"detail": redis.get("redis_unavailable") or "queue reachable",
		}
	)

	return checks


def run_preflight(
	campaign: str,
	*,
	target_rows: int = PROJECTION_ROWS,
	disk_safety_factor: float = DISK_SAFETY_FACTOR,
	max_backup_age_hours: int = MAX_BACKUP_AGE_HOURS,
) -> dict:
	"""Measure, project, evaluate, and record the whole thing on the campaign (A29)."""
	measured = measure(campaign)
	projected = project(measured, target_rows=target_rows)
	checks = evaluate(
		measured,
		projected,
		disk_safety_factor=disk_safety_factor,
		max_backup_age_hours=max_backup_age_hours,
	)
	passed = all(check["passed"] for check in checks)

	report = {"measured": measured, "projected": projected, "checks": checks, "passed": passed}
	frappe.db.set_value(
		CAMPAIGN_DOCTYPE,
		campaign,
		{
			"preflight_status": "Passed" if passed else "Failed",
			"preflight_at": now_datetime(),
			"preflight_report": frappe.as_json(report),
		},
		update_modified=False,
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)
	return report


def is_production_scale(campaign: str) -> bool:
	return cint(frappe.db.count(OBJECT_DOCTYPE, {"campaign": campaign})) >= PRODUCTION_SCALE_OBJECTS


def assert_startable(campaign: str, *, skip_preflight: bool = False):
	"""A29 — refuse a production-scale start below the thresholds.

	`skip_preflight` exists for the rehearsal harness and for a small campaign an operator
	has already reasoned about; it does not exist for a production-scale one, which is why
	the size check comes first and the flag cannot reach it.
	"""
	if not is_production_scale(campaign):
		if not skip_preflight:
			run_preflight(campaign)
		return

	# Measured now, every time, and never read back from the campaign. An earlier "Passed"
	# is a statement about the machine as it was then: the free space it measured has since
	# been consumed by the very campaign that recorded it, and the backup it accepted ages
	# out of the freshness window on exactly the same 24-hour clock. Trusting yesterday's
	# verdict is how a start proceeds onto a full volume with no recoverable backup, which
	# is the outcome A29 exists to prevent. The cost is one `measure()` per start — four
	# COUNT(*)s on the snapshot tables, seconds at 1.2M rows, once.
	report = run_preflight(campaign)
	if not report["passed"]:
		failures = [check for check in report["checks"] if not check["passed"]]
		frappe.throw(
			_("Capacity preflight failed for a production-scale campaign:")
			+ " "
			+ "; ".join(f"{check['check']} ({check.get('detail')})" for check in failures),
			title=_("Migration Capacity Preflight Failed"),
		)
