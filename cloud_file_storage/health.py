"""Operator-facing detectors for conditions this app can see but must not silently fix.

Everything here reports; nothing here mutates. That division is the point: the conditions
worth detecting are mostly files this app does not own, and an app that quietly moves an
operator's data is a worse surprise than the condition it was papering over. The precedent
is `compat.detect_deprecated_hooks` — find it, name it, say how to fix it, change nothing.
"""

import os

import frappe
from frappe import _

#: The role `backup/health.get_backup_health` gates on. Named here so the panel withholds
#: the section for anyone P6 would have refused, rather than re-deciding it.
MANAGER_ROLE = "System Manager"

#: How many paths a finding carries. A finding is a finding at 1 or at 10,000; the list is
#: there for the operator's remediation, and an unbounded one would be a memory hazard on a
#: site that has been running this way for years.
MAX_REPORTED_PATHS = 50


def public_private_tree() -> str:
	"""`sites/<site>/public/private` — a directory that should not exist."""
	return frappe.get_site_path("public", "private")


def detect_public_private_residue(log: bool = True) -> dict:
	"""Files under `sites/<site>/public/private/` — private data nginx serves to anyone.

	**The vector, which predates this app and is not ours to fix in place.** Core's
	`File.make_thumbnail` writes every thumbnail to
	`get_site_path("public", thumbnail_url.lstrip("/"))` (`frappe/core/doctype/file/file.py:463`),
	so a private file's thumbnail — whose URL is `/private/files/<name>_<suffix>.<ext>` —
	lands at `public/private/files/<name>_<suffix>.<ext>`. bench's nginx template answers
	`location /` with `try_files /<site>/public/$uri @webserver`
	(`bench/config/templates/nginx.conf:91`), so a GET of that URL is served **off disk,
	before any frappe process runs**, with no permission check possible. The
	`^/protected/(.*)` block in the same template is `internal`: it exists for
	X-Accel-Redirect and does not guard `/private/...` requests. Core's own `delete_file`
	then looks for that thumbnail in `private/files/` (`frappe/utils/file_manager.py:317-322`),
	so it is orphaned on delete as well — the same inconsistency, seen from the other end.

	**What this app already does.** Rows it manages are fixed: `thumbnails.local_thumbnail_path`
	writes a private thumbnail to `private/files/`, and the privacy flip moves an existing
	one out of the public tree (never deletes it). Rows it does not manage — a pre-install
	row, a LOCAL_OPERATIONAL doctype, any row on a LOCAL_ONLY site — keep core's behaviour,
	because PLAN §A requires LOCAL_ONLY to be byte-identical to core and because relocating
	files this app does not own is not its call to make. Hence this detector.

	Returns ``{"tree", "count", "paths", "truncated", "message"}``; `count` is exact,
	`paths` is capped at :data:`MAX_REPORTED_PATHS` and is relative to the site directory.
	"""
	tree = public_private_tree()

	count = 0
	paths: list[str] = []
	site_path = frappe.get_site_path()

	for directory, _subdirectories, filenames in os.walk(tree):
		for filename in filenames:
			count += 1
			if len(paths) < MAX_REPORTED_PATHS:
				full_path = os.path.join(directory, filename)
				paths.append(os.path.relpath(full_path, site_path))

	paths.sort()
	finding = {
		"tree": tree,
		"count": count,
		"paths": paths,
		"truncated": count > len(paths),
		"message": _(
			"{0} file(s) under {1}. These are private-namespaced files inside the PUBLIC "
			"site tree, which nginx serves to unauthenticated callers off disk "
			"(try_files /<site>/public/$uri). This is upstream frappe behaviour: "
			"File.make_thumbnail writes every thumbnail under public/ even when the "
			"thumbnail URL is /private/files/... . Cloud File Storage does not move files "
			"it does not manage, so remediate by hand: move each file to the matching "
			"private/files/ path (a thumbnail of a cloud-backed file can simply be deleted "
			"— it is regenerated on demand). See docs/runbooks/deployment.md."
		).format(count, tree)
		if count
		else _("No private-namespaced files in the public site tree."),
	}

	if count and log:
		frappe.log_error(
			title="cloud_file_storage: private files in the public site tree",
			message=f"{finding['message']}\n\n" + "\n".join(finding["paths"]),
		)

	return finding


# --------------------------------------------------------------------------- health panel

#: Cloud Storage Object statuses whose bytes are actually sitting in the bucket right now.
#: `pending_delete` is included because a scheduled deletion has not happened yet — the
#: bytes are still there and still cost money, and a panel that hid them would understate
#: the bucket by exactly the amount an operator is about to reclaim.
PRESENT_IN_BUCKET_STATUSES = ("uploaded", "verified", "legacy_unverified", "pending_delete")

#: Statuses that mean "this app believes bytes are missing or unusable". Counted, never
#: added to the byte totals.
UNHEALTHY_STATUSES = ("failed", "orphaned")


def storage_health() -> dict:
	"""The Settings health panel (PLAN §A, design §3.4).

	**The one thing this function exists to get right**: cloud-managed permanent attachment
	bytes and bounded temporary/local operational bytes are reported as two separate
	quantities and are never added together. PLAN §A requires the panel and the release
	report to distinguish them, because "94% migrated" that silently counts Data Import
	spreadsheets as cloud attachments is a number that cannot be acted on — and because the
	LOCAL_OPERATIONAL doctypes are a *documented mode exemption* under S3_ONLY, not a
	migration failure.

	A third bucket, `local_unmigrated`, holds attachment bytes that are neither: local files
	whose parent is not an ignored doctype and which no Cloud Storage Object backs yet. They
	get their own key rather than being folded into either side, since folding them into the
	operational number would excuse them and folding them into the cloud number would claim
	them.
	"""
	from frappe.utils import cint, now_datetime

	from cloud_file_storage.storage.modes import get_mode, get_settings, ignored_doctypes

	settings = get_settings()
	ignored = sorted(ignored_doctypes(settings))

	return {
		"generated_at": str(now_datetime()),
		"mode": get_mode(settings).value,
		"cloud_managed": _cloud_managed_section(),
		"local_operational": _local_operational_section(ignored, settings),
		"local_unmigrated": _local_unmigrated_section(ignored),
		"migration": _migration_section(),
		"backup": _backup_section(),
		"warnings": _health_warnings(settings, ignored),
		"grace_days": cint(settings.object_delete_grace_days),
		"restore_window_days": cint(settings.restore_window_days),
	}


def _cso_totals(*, derived: bool):
	"""Per-status count and byte sum over Cloud Storage Object rows.

	Query builder rather than `frappe.db.sql`: raw SQL against the CSO table is sanctioned at
	exactly three audited sites (docs/INVARIANTS.md invariant 5) and a health panel is not one of them.
	"""
	from frappe.query_builder.functions import Count, Sum

	cso = frappe.qb.DocType("Cloud Storage Object")
	query = (
		frappe.qb.from_(cso)
		.select(cso.status, Count("*").as_("row_count"), Sum(cso.file_size).as_("byte_total"))
		.groupby(cso.status)
	)
	query = query.where(cso.derived_of.isnotnull() if derived else cso.derived_of.isnull())

	counts: dict[str, int] = {}
	byte_totals: dict[str, int] = {}
	for row in query.run(as_dict=True):
		counts[row["status"]] = int(row["row_count"] or 0)
		byte_totals[row["status"]] = int(row["byte_total"] or 0)
	return counts, byte_totals


def _present_bytes(byte_totals: dict[str, int]) -> int:
	return sum(byte_totals.get(status, 0) for status in PRESENT_IN_BUCKET_STATUSES)


def _cloud_managed_section() -> dict:
	from frappe.query_builder.functions import Count

	source_counts, source_bytes = _cso_totals(derived=False)
	derived_counts, derived_bytes = _cso_totals(derived=True)

	file_doctype = frappe.qb.DocType("File")
	linked = (
		frappe.qb.from_(file_doctype)
		.select(Count("*"))
		.where(file_doctype.cloud_storage_object.isnotnull())
		.run()
	)

	return {
		"label": _("Cloud-managed permanent attachment bytes"),
		"description": _(
			"Bytes of permanent attachments held in the object store and managed by this "
			"app. Excludes operational and temporary local bytes entirely."
		),
		"attachment_objects": sum(source_counts.get(status, 0) for status in PRESENT_IN_BUCKET_STATUSES),
		"attachment_bytes": _present_bytes(source_bytes),
		"verified_bytes": source_bytes.get("verified", 0),
		"derived_objects": sum(derived_counts.get(status, 0) for status in PRESENT_IN_BUCKET_STATUSES),
		"derived_bytes": _present_bytes(derived_bytes),
		"unhealthy_objects": sum(source_counts.get(status, 0) for status in UNHEALTHY_STATUSES),
		"pending_upload_objects": source_counts.get("pending_upload", 0),
		"status_counts": source_counts,
		"derived_status_counts": derived_counts,
		"linked_files": int(linked[0][0]) if linked else 0,
	}


def _attachment_bytes(*, ignored: list[str], cloud_backed: bool | None) -> tuple[int, int]:
	"""(file count, byte sum) over non-folder File rows, split by ignored parent + link.

	`cloud_backed=None` means "ignore the link"; True/False filter on it. Folders carry no
	bytes and would inflate the count.
	"""
	from frappe.query_builder.functions import Count, Sum

	file_doctype = frappe.qb.DocType("File")
	query = (
		frappe.qb.from_(file_doctype)
		.select(Count("*").as_("row_count"), Sum(file_doctype.file_size).as_("byte_total"))
		.where(file_doctype.is_folder == 0)
	)

	if ignored:
		query = query.where(file_doctype.attached_to_doctype.isin(ignored))
	else:
		# No ignored doctypes configured: the operational set is empty by definition, and a
		# bare `isin([])` is a filter some query builders drop rather than falsify.
		return 0, 0

	if cloud_backed is True:
		query = query.where(file_doctype.cloud_storage_object.isnotnull())
	elif cloud_backed is False:
		query = query.where(file_doctype.cloud_storage_object.isnull())

	row = query.run(as_dict=True)
	if not row:
		return 0, 0
	return int(row[0]["row_count"] or 0), int(row[0]["byte_total"] or 0)


def _local_operational_section(ignored: list[str], settings) -> dict:
	from frappe.utils import cint

	from cloud_file_storage.cache import eviction, materialize

	operational_files, operational_bytes = _attachment_bytes(ignored=ignored, cloud_backed=None)

	# Query builder rather than SQL functions written as strings in `fields=`: v16's rewritten
	# SELECT parser rejects those outright (frappe/database/query.py:1881-1897,
	# "SQL functions are not allowed as strings in SELECT"). This is the same shape the panel
	# already uses for the CSO rollup above, and it is valid on v15 and v16 alike.
	from frappe.query_builder.functions import Count, Sum

	cmo = frappe.qb.DocType("Cloud Migration Object")
	quarantined = (
		frappe.qb.from_(cmo)
		.select(Count("*").as_("row_count"), Sum(cmo.size_bytes).as_("byte_total"))
		.where(cmo.status == "Quarantined")
	).run(as_dict=True)
	quarantine_rows = int((quarantined[0].get("row_count") if quarantined else 0) or 0)
	quarantine_bytes = int((quarantined[0].get("byte_total") if quarantined else 0) or 0)

	return {
		"label": _("Bounded temporary / local operational bytes"),
		"description": _(
			"Short-lived operational bytes that stay on local disk by design: attachments of "
			"the LOCAL_OPERATIONAL doctypes, the bounded materialization cache, and files "
			"quarantined by a migration cleanup pass. These are never counted as migrated "
			"cloud attachments, and under S3_ONLY they remain a documented mode exemption."
		),
		"ignored_doctypes": ignored,
		"operational_files": operational_files,
		"operational_bytes": operational_bytes,
		"cache_bytes": materialize.cache_size_bytes(),
		"cache_budget_bytes": eviction.budget_bytes(settings),
		"cache_dirty_entries": materialize.dirty_entry_count(),
		"quarantined_objects": quarantine_rows,
		"quarantine_bytes": quarantine_bytes,
		"quarantine_ttl_days": cint(getattr(settings, "quarantine_ttl_days", 0)) or None,
		"cache_directory": materialize.CACHE_DIRNAME,
	}


def _local_unmigrated_section(ignored: list[str]) -> dict:
	"""Permanent attachments still only on local disk — neither cloud-managed nor operational."""
	from frappe.query_builder.functions import Count, Sum

	file_doctype = frappe.qb.DocType("File")
	query = (
		frappe.qb.from_(file_doctype)
		.select(Count("*").as_("row_count"), Sum(file_doctype.file_size).as_("byte_total"))
		.where(file_doctype.is_folder == 0)
		.where(file_doctype.cloud_storage_object.isnull())
	)
	if ignored:
		query = query.where(
			file_doctype.attached_to_doctype.notin(ignored) | file_doctype.attached_to_doctype.isnull()
		)

	row = query.run(as_dict=True)
	rows = int(row[0]["row_count"] or 0) if row else 0
	byte_total = int(row[0]["byte_total"] or 0) if row else 0

	return {
		"label": _("Local attachment bytes not yet cloud-managed"),
		"description": _(
			"Permanent attachments that no Cloud Storage Object backs yet. Reported "
			"separately so they are neither claimed as migrated nor excused as operational."
		),
		"files": rows,
		"bytes": byte_total,
	}


def _migration_section() -> dict:
	"""The active campaign, if any, plus the last finished one. No counters are recomputed."""
	from cloud_file_storage.cloud_file_storage.doctype.cloud_migration_campaign.cloud_migration_campaign import (
		ACTIVE_STATUSES,
	)

	active = frappe.db.get_value(
		"Cloud Migration Campaign",
		{"status": ("in", ACTIVE_STATUSES)},
		["name", "title", "status", "active_phase"],
		as_dict=True,
	)
	last = frappe.db.get_all(
		"Cloud Migration Campaign",
		fields=["name", "title", "status", "modified"],
		order_by="modified desc",
		limit_page_length=1,
	)
	reconcile = frappe.db.get_all(
		"Cloud Reconcile Run",
		fields=["name", "status", "started_at", "completed_at", "orphan_remote", "unreferenced_cso"],
		order_by="creation desc",
		limit_page_length=1,
	)

	return {
		"active_campaign": active,
		"latest_campaign": last[0] if last else None,
		"open_blockers": frappe.db.count(
			"Cloud Migration Conflict", {"status": "Open", "severity": "Blocker"}
		),
		"open_conflicts": frappe.db.count("Cloud Migration Conflict", {"status": "Open"}),
		"last_reconcile": reconcile[0] if reconcile else None,
	}


def _backup_health_module():
	"""`cloud_file_storage.backup.health`, or None when the backup domain is not installed.

	A named seam rather than an inline `try: import` inside the caller, so a test can
	substitute a stand-in by patching this function. The alternative — injecting the module
	into `sys.modules` — breaks frappe's document cache in the same process: the Settings
	Single is pickled on cache write, and a mutated module table makes pickle resolve the
	controller class to a different object than the instance's own.
	"""
	try:
		from cloud_file_storage.backup import health as backup_health
	except ImportError:
		return None
	return backup_health


def _backup_section() -> dict:
	"""P6's backup findings, rendered here and **detected there**.

	`backup/health.backup_health()` computes schedule staleness, file tarballs still being
	taken after the cloud cutover, and lifecycle drift. This function does not recompute any
	of that, and a test asserts the findings come back byte-for-byte as that module produced
	them: a panel with its own copy of the staleness arithmetic is two answers to one
	question, and the one an operator acts on is whichever they happened to open.

	**The import is guarded** because P6 and P7 were built in separate worktrees and this is
	the consumer side. On a tree where the backup domain has not landed, the panel says so.
	Growing a local detector to fill the gap would mean deleting it at convergence, and the
	deletion is the step that gets skipped.

	**Visibility follows P6's own gate, not this panel's.** `get_backup_health` is
	`System Manager` only; `get_storage_health` admits Cloud Storage Manager as well. Serving
	backup findings to the wider role from here would widen another phase's decision from
	the outside, so the section reports itself withheld instead. Whether the operator role
	should see backup health is a convergence question, and it is named in the P7 report.
	"""
	backup_health = _backup_health_module()
	if backup_health is None:
		return {
			"available": False,
			"reason": _("The backup module is not installed on this site."),
		}

	if MANAGER_ROLE not in frappe.get_roles():
		return {
			"available": True,
			"visible": False,
			"reason": _("Backup health is visible to System Managers only."),
		}

	return {
		"available": True,
		"visible": True,
		"label": _("Backup"),
		**backup_health.backup_health(),
		"recent": backup_health.recent_backups(limit=5),
	}


def _health_warnings(settings, ignored: list[str]) -> list[dict]:
	from frappe.utils import cint

	from cloud_file_storage.migration import engine as migration_engine

	warnings = []

	if not migration_engine.migration_queue_available():
		warnings.append(
			{
				"code": "migration_queue_missing",
				"message": _(
					"The dedicated {0} queue is not configured on this bench. Migration jobs "
					"would share the queues the ERP itself uses — see docs/runbooks/deployment.md."
				).format(migration_engine.MIGRATION_QUEUE),
			}
		)

	if cint(settings.object_delete_grace_days) < cint(settings.restore_window_days):
		warnings.append(
			{
				"code": "grace_below_restore_window",
				"message": _(
					"Object delete grace ({0}d) is shorter than the restore window ({1}d): a "
					"restore inside the promised window could find the object already deleted."
				).format(settings.object_delete_grace_days, settings.restore_window_days),
			}
		)

	if not ignored:
		warnings.append(
			{
				"code": "no_ignored_doctypes",
				"message": _(
					"No LOCAL_OPERATIONAL doctypes are configured. Data Import, Prepared Report "
					"and Package Import attachments are consumed through local paths and are "
					"expected to stay local."
				),
			}
		)

	residue = detect_public_private_residue(log=False)
	if residue["count"]:
		warnings.append({"code": "public_private_residue", "message": residue["message"]})

	from cloud_file_storage.api.compat import detect_deprecated_hooks

	for finding in detect_deprecated_hooks(log=False):
		warnings.append({"code": "deprecated_hook", "message": finding["message"]})

	return warnings
