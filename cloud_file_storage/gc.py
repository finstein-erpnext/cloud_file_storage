"""Deferred object garbage collection — the only place an S3 object is ever deleted.

The safety property (A6, ADR-4) is not "we counted the references correctly". It is that
counting wrongly cannot destroy shared bytes:

1. dereferencing only **schedules** — `pending_delete` plus a grace window;
2. the pre-delete recount is a **locking** read (`for_update`), because MariaDB's
   REPEATABLE READ will otherwise serve a stale non-locking count taken before a
   concurrent re-reference committed;
3. each object is committed on its own, so a crash halfway through a sweep leaves every
   other object exactly where it was;
4. a re-referenced object is revived to its previous status, never deleted;
5. objects owned by a non-terminal migration campaign count as referenced (A14).

Worst case is therefore "an unreferenced object survives a few days longer", never "bytes
that someone still points at are gone".
"""

import os

import frappe
from frappe.utils import add_to_date, cint, now_datetime

from cloud_file_storage.background import enqueue_maintenance
from cloud_file_storage.storage import engine, objects
from cloud_file_storage.storage.exceptions import CloudObjectNotFound, CloudStorageError
from cloud_file_storage.storage.hashing import digest_path
from cloud_file_storage.storage.modes import get_mode, get_settings

CSO_DOCTYPE = "Cloud Storage Object"

#: Objects handled per sweep. Bounded so a maintenance tick stays O(seconds).
GC_BATCH_SIZE = 500
ORPHAN_BATCH_SIZE = 200
REPAIR_BATCH_SIZE = 100
#: Give up re-uploading after this many attempts; the object stays `failed` and queryable.
MAX_REPAIR_ATTEMPTS = 10


def run_deferred_object_gc(limit: int = GC_BATCH_SIZE) -> dict:
	"""Physically delete objects whose grace has elapsed and which are still unreferenced."""
	settings = get_settings()
	if not get_mode(settings).uses_cloud:
		return {"scanned": 0, "deleted": 0, "revived": 0, "failed": 0}

	candidates = frappe.get_all(
		CSO_DOCTYPE,
		filters={"status": "pending_delete", "deletion_scheduled_at": ("<=", now_datetime())},
		pluck="name",
		limit=limit,
		order_by="deletion_scheduled_at asc",
	)

	stats = {"scanned": 0, "deleted": 0, "revived": 0, "failed": 0}
	for name in candidates:
		stats["scanned"] += 1
		try:
			outcome = _collect_one(name, settings)
		except Exception as exc:  # noqa: BLE001 - one bad object must not stop the sweep
			frappe.db.rollback()
			frappe.log_error(title="cloud_file_storage GC", message=f"cso={name} error={exc}")
			stats["failed"] += 1
			continue
		stats[outcome] = stats.get(outcome, 0) + 1

	return stats


def _collect_one(name: str, settings) -> str:
	"""Handle exactly one object, then commit. Returns 'deleted', 'revived' or 'failed'."""
	status = objects.lock_cso(name)
	if status != "pending_delete":
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)
		return "revived" if status else "failed"

	scheduled_at = frappe.db.get_value(CSO_DOCTYPE, name, "deletion_scheduled_at")
	if scheduled_at and scheduled_at > now_datetime():
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)
		return "revived"

	# The decision. `live_reference_count` reads `FOR UPDATE` (A6) — everything above this
	# line, including the candidate query that opened this transaction's read view, is only
	# a hint, and a plain count here would still be answering from that stale snapshot.
	if objects.live_reference_count(name):
		prior = frappe.db.get_value(CSO_DOCTYPE, name, "status_before_delete") or "pending_upload"
		objects.set_cso_status(name, prior, deletion_scheduled_at=None, status_before_delete=None)
		objects.refresh_reference_count(name)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)
		return "revived"

	cso = frappe.get_doc(CSO_DOCTYPE, name)
	try:
		engine.delete(cso, settings=settings)
	except CloudObjectNotFound:
		# Already gone from the bucket. The tombstone still has to be written, otherwise
		# this row is rescanned forever.
		pass
	except CloudStorageError as exc:
		objects.record_failure(name, str(exc), status="pending_delete")
		frappe.db.commit()
		return "failed"

	# Tombstone: the row survives with its key and timestamps so a later re-upload of the
	# same bytes revives it (A25) and reconciliation can tell "deleted" from "never existed".
	objects.set_cso_status(
		name,
		"deleted",
		deletion_scheduled_at=None,
		status_before_delete=None,
		reference_count=0,
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)
	return "deleted"


def run_orphan_sweep(limit: int = ORPHAN_BATCH_SIZE) -> dict:
	"""Schedule objects that lost their last reference without the delete hook noticing.

	Core's `_delete_file_on_disk` gate ignores `is_private` when counting hash-sharers
	(`file.py:518-519`), so the last deleter of a private object can be routed to
	`only_thumbnail=True` because a public twin shares the MD5. That leak ends here.
	"""
	settings = get_settings()
	if not get_mode(settings).uses_cloud:
		return {"scanned": 0, "scheduled": 0}

	grace_days = cint(settings.object_delete_grace_days) or 7
	cutoff = add_to_date(now_datetime(), days=-grace_days)

	candidates = frappe.get_all(
		CSO_DOCTYPE,
		filters={
			"status": ("in", ("uploaded", "verified", "legacy_unverified")),
			"modified": ("<", cutoff),
			"reference_count": 0,
		},
		pluck="name",
		limit=limit,
	)

	stats = {"scanned": 0, "scheduled": 0}
	for name in candidates:
		stats["scanned"] += 1
		try:
			if objects.schedule_deletion(name, settings=settings):
				stats["scheduled"] += 1
			frappe.db.commit()
		except Exception as exc:  # noqa: BLE001
			frappe.db.rollback()
			frappe.log_error(title="cloud_file_storage orphan sweep", message=f"cso={name} error={exc}")

	return stats


def run_deferred_object_gc_dispatch():
	"""Daily dispatcher: the GC itself is inline S3 work and must not run on a shared queue.

	`run_deferred_object_gc` issues up to `GC_BATCH_SIZE` DeleteObject calls inline. Under the
	`daily_maintenance` frequency frappe placed it on `long` (1500s timeout,
	`scheduled_job_type.get_queue_name`); moving the hook to `daily` for v15.16.0 compatibility
	silently moved it to `default` (300s) -- five times less, on the queue this module's own
	docstring says this work must never compete for. The frequency change was still right; this
	restores the queue and the timeout it cost.
	"""
	settings = get_settings()
	if not get_mode(settings).uses_cloud:
		return
	enqueue_maintenance("cloud_file_storage.gc.run_deferred_object_gc", timeout=1500)


def run_orphan_sweep_dispatch():
	"""Daily dispatcher: the sweep commits per row and is not O(ms). See the note above."""
	settings = get_settings()
	if not get_mode(settings).uses_cloud:
		return
	enqueue_maintenance("cloud_file_storage.gc.run_orphan_sweep", timeout=1500)


def repair_pending_uploads_dispatch():
	"""Hourly dispatcher: hand the actual re-uploads to the dedicated queue."""
	settings = get_settings()
	if not get_mode(settings).uses_cloud:
		return

	pending = frappe.db.count(
		CSO_DOCTYPE,
		{"status": ("in", ("pending_upload", "failed")), "retry_count": ("<", MAX_REPAIR_ATTEMPTS)},
	)
	if not pending:
		return

	enqueue_maintenance("cloud_file_storage.gc.repair_pending_uploads", timeout=1500)


def repair_pending_uploads(limit: int = REPAIR_BATCH_SIZE) -> dict:
	"""Re-upload objects whose PUT failed, from the local copy that was kept.

	An object with no local copy cannot be repaired here — it is recorded `failed` with the
	reason rather than being silently retried forever.
	"""
	settings = get_settings()
	if not get_mode(settings).uses_cloud:
		return {"scanned": 0, "uploaded": 0, "unrepairable": 0, "failed": 0}

	candidates = frappe.get_all(
		CSO_DOCTYPE,
		filters={"status": ("in", ("pending_upload", "failed")), "retry_count": ("<", MAX_REPAIR_ATTEMPTS)},
		pluck="name",
		limit=limit,
	)

	stats = {"scanned": 0, "uploaded": 0, "unrepairable": 0, "failed": 0}
	for name in candidates:
		stats["scanned"] += 1
		try:
			outcome = _repair_one(name, settings)
			frappe.db.commit()
		except Exception as exc:  # noqa: BLE001
			frappe.db.rollback()
			frappe.log_error(title="cloud_file_storage repair", message=f"cso={name} error={exc}")
			outcome = "failed"
		stats[outcome] = stats.get(outcome, 0) + 1

	return stats


def _repair_one(name: str, settings) -> str:
	status = objects.lock_cso(name)
	if status not in ("pending_upload", "failed"):
		return "unrepairable"

	cso = frappe.get_doc(CSO_DOCTYPE, name)
	source = _local_source_for(cso)
	if not source:
		objects.record_failure(name, "no local copy available to re-upload from")
		return "unrepairable"

	# The key IS the hash (PLAN §A). If the local file drifted since the object was recorded
	# — a DUAL_WRITE file written during an outage and then edited through `get_full_path()`
	# — uploading it under the old key would publish bytes whose SHA256 is not their address,
	# silently breaking content-addressing and pre-poisoning P5's `engine.verify()`.
	digest = digest_path(source)
	if cso.content_sha256 and digest.sha256 != cso.content_sha256:
		objects.record_failure(
			name,
			f"local copy {source} hashes to {digest.sha256[:12]}… but the object key expects "
			f"{cso.content_sha256[:12]}…; refusing to publish bytes under the wrong key",
		)
		return "unrepairable"

	try:
		engine.put_path(cso, source, settings=settings)
	except CloudStorageError as exc:
		objects.record_failure(name, str(exc))
		return "failed"

	objects.set_cso_status(name, "uploaded", last_error=None)
	return "uploaded"


def _local_source_for(cso) -> str | None:
	"""A local file whose bytes are this object's, if one of its File rows still has one."""
	for file_name in frappe.get_all("File", filters={"cloud_storage_object": cso.name}, pluck="name"):
		file_doc = frappe.get_doc("File", file_name)
		resolver = getattr(file_doc, "_canonical_local_path", None)
		if not resolver:
			continue
		path = resolver()
		if path and os.path.exists(path):
			return path
	return None
