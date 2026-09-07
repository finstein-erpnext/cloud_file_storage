"""Progress snapshots, realtime publishing and the campaign report export (design §9).

The realtime cadence matters more than it looks: `publish_realtime` rides the **queue**
Redis instance, so a batch job publishing per object would degrade every Desk connection on
the site while the migration runs. Events are therefore throttled to one per two seconds
per process, with a module-level monotonic guard.

The export is streamed with a server-side cursor and written straight to a file handle.
Materialising 1.2M rows to build a CSV would defeat the point of having batched everything
else.
"""

import csv
import json
import os
import time

import frappe
from frappe.utils import cint

CAMPAIGN_DOCTYPE = "Cloud Migration Campaign"
OBJECT_DOCTYPE = "Cloud Migration Object"
CONFLICT_DOCTYPE = "Cloud Migration Conflict"

REALTIME_EVENT = "cfs_migration_progress"
REALTIME_MIN_INTERVAL = 2.0

_last_published = 0.0

#: Columns of the per-object export, in a fixed order so a diff between two runs is readable.
EXPORT_COLUMNS = (
	"name",
	"identity_kind",
	"file_url",
	"classification",
	"status",
	"skip_reason",
	"ref_count",
	"ignored_ref_count",
	"is_thumbnail",
	"on_disk",
	"disk_path",
	"size_bytes",
	"sha256",
	"md5",
	"cloud_storage_object",
	"attempt_count",
	"error_class",
	"last_error",
	"uploaded_at",
	"verified_at",
	"cleaned_at",
	"quarantine_path",
	"conflict",
)


def campaign_snapshot(campaign: str) -> dict:
	"""The counter family plus derived rate/ETA. One row read, no COUNT(*) per render."""
	row = frappe.db.get_value(
		CAMPAIGN_DOCTYPE,
		campaign,
		[
			"name",
			"title",
			"status",
			"active_phase",
			"control_flag",
			"total_objects",
			"total_files",
			"objects_pending",
			"objects_uploading",
			"objects_uploaded",
			"objects_verified",
			"objects_cleaned",
			"objects_failed",
			"objects_conflict",
			"objects_skipped",
			"objects_adopted",
			"objects_dedup_reused",
			"bytes_total",
			"bytes_uploaded",
			"bytes_verified",
			"started_at",
			"completed_at",
		],
		as_dict=True,
	)
	if not row:
		return {}

	total = cint(row.total_objects)
	terminal = (
		cint(row.objects_verified)
		+ cint(row.objects_cleaned)
		+ cint(row.objects_skipped)
		+ cint(row.objects_adopted)
		+ cint(row.objects_dedup_reused)
	)
	row["progress_pct"] = round(100.0 * terminal / total, 2) if total else 0.0
	row["remaining"] = max(0, total - terminal - cint(row.objects_failed) - cint(row.objects_conflict))
	row["open_blockers"] = frappe.db.count(
		CONFLICT_DOCTYPE, {"campaign": campaign, "status": "Open", "severity": "Blocker"}
	)
	return row


#: Exactly the roles `cloud_migration_dashboard.json` admits to the Page. The snapshot goes to
#: the audience that can already open the dashboard and no wider.
DASHBOARD_ROLES = ("System Manager", "Cloud Storage Manager")


def dashboard_audience() -> list[str]:
	"""Enabled users who may open the migration dashboard.

	**Why this is not a bare `publish_realtime(event, snapshot)`.** With neither `user` nor
	`room`, frappe resolves the room to `get_site_room()` — the literal string `"all"` — and
	the campaign snapshot goes to *every* Desk user (`frappe/realtime.py:58-71`). That is the
	natural-looking fix for "nobody is receiving this" and it turns an inert feature into an
	oversharing one. frappe has no role room, so the audience is resolved to users and each
	gets their own `user:<name>` room.

	The previous value was `user="Administrator"`, which is the narrowest possible audience and
	therefore safe — but the Page admits two roles, so for every non-Administrator operator the
	binding never fired and the dashboard silently ran on its 30s poll alone.

	Administrator is included explicitly: it holds every role implicitly and may carry no
	`Has Role` row at all, so a role query alone would drop the one user that used to work.
	"""
	holders = frappe.get_all(
		"Has Role",
		filters={"parenttype": "User", "role": ("in", DASHBOARD_ROLES)},
		pluck="parent",
		distinct=True,
	)
	# `user_type` matters (L-5): a Website User holding Cloud Storage Manager would enter the
	# audience and receive campaign snapshots while being unable to open the Desk Page at all.
	# The docstring says "may open the dashboard", so the query has to mean that rather than
	# "holds one of these roles".
	enabled = (
		frappe.get_all(
			"User",
			filters={"name": ("in", holders), "enabled": 1, "user_type": "System User"},
			pluck="name",
		)
		if holders
		else []
	)
	return sorted((set(enabled) | {"Administrator"}) - {"Guest"})


def publish_campaign_snapshot(campaign: str, *, force: bool = False) -> dict | None:
	"""Publish at most once every two seconds per process (design §9)."""
	global _last_published

	now = time.monotonic()
	if not force and (now - _last_published) < REALTIME_MIN_INTERVAL:
		return None

	snapshot = campaign_snapshot(campaign)
	if not snapshot:
		return None

	_last_published = now
	# Per recipient, not around the loop (N-5): one wrapping `try` meant a failure on the
	# third recipient silently dropped the fourth onward, so an operator's dashboard could go
	# quiet because of somebody else's connection. A realtime hiccup must never fail a
	# migration batch, but it should cost only the recipient it happened to.
	for recipient in dashboard_audience():
		try:
			frappe.publish_realtime(REALTIME_EVENT, snapshot, user=recipient)
		except Exception:  # noqa: BLE001 - reported, never raised into the batch
			frappe.logger("cloud_file_storage").warning(
				f"could not publish migration progress to {recipient}"
			)
	return snapshot


def reset_publish_throttle():
	"""Test hook — the throttle is process-global by design, so tests must be able to clear it."""
	global _last_published
	_last_published = 0.0


# ------------------------------------------------------------------------------- export


def iter_objects(campaign: str, page_size: int = 2000):
	"""Keyset cursor over the campaign's objects. Never materialises the whole set."""
	cursor = ""
	while True:
		page = frappe.db.get_all(
			OBJECT_DOCTYPE,
			filters={"campaign": campaign, "name": (">", cursor)},
			fields=list(EXPORT_COLUMNS),
			order_by="name asc",
			limit_page_length=page_size,
		)
		if not page:
			return
		yield from page
		cursor = page[-1]["name"]


def export_campaign_report(campaign: str, fmt: str = "csv") -> str:
	"""Write one row per object to a private file. Returns the site-relative path.

	**Site-relative, not absolute** (N-3): `frappe.get_site_path` returns a path relative to
	the bench's `sites/` directory, and this string is shown to the operator, so the docstring
	has to match what they will see.

	`campaign` is checked for existence and then reduced to its basename before it reaches the
	join (M-3). Campaign names are `format:CFS-CAMP-{####}`, so a traversing value can never
	name a real campaign and the file would be header-only — but the join was still reachable
	over HTTP by the operator role, and "the primitive is only good for creating an empty file
	in a writable directory" is an argument that stops being true the moment the filename or
	the columns change. Both guards are cheap; neither depends on the other holding.
	"""
	if fmt not in ("csv", "jsonl"):
		frappe.throw(f"unsupported report format {fmt!r}")

	if not frappe.db.exists(CAMPAIGN_DOCTYPE, campaign):
		frappe.throw(frappe._("Campaign {0} does not exist.").format(campaign))

	directory = frappe.get_site_path("private", "files")
	os.makedirs(directory, exist_ok=True)
	safe_name = os.path.basename(str(campaign))
	path = os.path.join(directory, f"{safe_name}-migration-report.{fmt}")

	rows = 0
	with open(path, "w", newline="") as handle:  # nosemgrep: frappe-security-file-traversal
		if fmt == "csv":
			writer = csv.DictWriter(handle, fieldnames=list(EXPORT_COLUMNS), extrasaction="ignore")
			writer.writeheader()
			for row in iter_objects(campaign):
				writer.writerow(row)
				rows += 1
		else:
			for row in iter_objects(campaign):
				handle.write(json.dumps(row, default=str, sort_keys=True) + "\n")
				rows += 1

	frappe.logger("cloud_file_storage").info(
		{"event": "migration_report_exported", "campaign": campaign, "rows": rows, "path": path}
	)
	return path


def summary(campaign: str) -> dict:
	"""Counts by classification and by status — the header of any report or CLI status."""
	by_status = frappe.db.sql(
		f"SELECT status, COUNT(*) AS objects, COALESCE(SUM(size_bytes), 0) AS bytes "
		f"FROM `tab{OBJECT_DOCTYPE}` WHERE campaign=%(campaign)s GROUP BY status",
		{"campaign": campaign},
		as_dict=True,
	)
	by_classification = frappe.db.sql(
		f"SELECT classification, COUNT(*) AS objects FROM `tab{OBJECT_DOCTYPE}` "
		f"WHERE campaign=%(campaign)s GROUP BY classification",
		{"campaign": campaign},
		as_dict=True,
	)
	by_conflict = frappe.db.sql(
		f"SELECT conflict_type, status, COUNT(*) AS conflicts FROM `tab{CONFLICT_DOCTYPE}` "
		f"WHERE campaign=%(campaign)s GROUP BY conflict_type, status",
		{"campaign": campaign},
		as_dict=True,
	)
	return {
		"campaign": campaign,
		"by_status": by_status,
		"by_classification": by_classification,
		"by_conflict": by_conflict,
	}


def convergence(campaign: str) -> dict:
	"""F3's convergence figure: the share of in-scope objects that reached a verified state.

	`Skipped` objects are excluded from the denominator rather than counted as failures —
	an ignored-doctype attachment or an orphan on disk was never in scope, and counting it
	against convergence would let a site with many Data Import files fail a gate about
	migration correctness.
	"""
	rows = frappe.db.sql(
		f"SELECT status, COUNT(*) AS objects FROM `tab{OBJECT_DOCTYPE}` "
		f"WHERE campaign=%(campaign)s GROUP BY status",
		{"campaign": campaign},
		as_dict=True,
	)
	counts = {row.status: cint(row.objects) for row in rows}
	in_scope = sum(count for status, count in counts.items() if status != "Skipped")
	verified = sum(
		counts.get(status, 0) for status in ("Verified", "CleanupEligible", "Quarantined", "CleanedUp")
	)
	return {
		"in_scope": in_scope,
		"verified": verified,
		"skipped": counts.get("Skipped", 0),
		"ratio": (verified / in_scope) if in_scope else 0.0,
		"counts": counts,
	}
