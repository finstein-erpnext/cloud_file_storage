"""CLEANUP — the only module in this engine that touches a local file.

Everything here is built around one sentence from PLAN §C: *no local file or thumbnail is
deleted or quarantined until its remote object is independently verified — and re-verified
at deletion time (fresh remote HEAD + local re-stat/re-hash)*. So the gates are not checked
once at button time and trusted afterwards; every one of them is re-evaluated server-side,
per object, inside the job, immediately before the rename:

1. the campaign carries an `approve_cleanup` signature (A9 — a separate, audited action);
2. the operation mode is S3_PRIMARY_LOCAL_FALLBACK or S3_ONLY (ADR-M16) — deleting local
   copies under DUAL_WRITE would break the mode's own promise;
3. the object is Verified, its Cloud Storage Object is `verified`, and **every** ref carries
   the link (a superseded ref means the runtime won and the local file stays);
4. A17 — no ref hangs off a LOCAL_OPERATIONAL parent (Data Import / Prepared Report /
   Package Import). This is a hard refusal, not a warning;
5. a **fresh** remote HEAD, using the same 64MB strategy split VERIFY uses, so a byte lost
   in the bucket between VERIFY and now blocks the deletion;
6. A4 — the local file is re-`stat`ed and, on any drift, re-hashed against the object's
   recorded hash. Drift means the site rewrote the file after we uploaded it, and the bytes
   in the bucket are no longer this file's bytes;
7. for a thumbnail, `serving.thumbnails.can_release_local_thumbnail` — source verified AND
   derived object verified.

Any gate that does not hold produces a `cleanup_blocked` conflict and leaves the file
exactly where it is. There is no code path here that deletes on a failed check.
"""

import os

import frappe
from frappe.utils import add_to_date, cint, now_datetime

from cloud_file_storage.migration import analyzer, audit, engine, verify
from cloud_file_storage.storage import objects
from cloud_file_storage.storage.modes import OperationMode, get_mode, get_settings

CAMPAIGN_DOCTYPE = "Cloud Migration Campaign"
OBJECT_DOCTYPE = "Cloud Migration Object"
REF_DOCTYPE = "Cloud Migration File Ref"

#: A32 — quarantine lives OUTSIDE `private/files`, so nothing quarantined can be reached by
#: a URL: `download_private_file` resolves through a File row, and nginx only ever serves
#: `public/`. Same filesystem as both file trees, so the rename is atomic.
QUARANTINE_DIRNAME = "cloud_migration_trash"

#: Modes in which a local copy is no longer part of the storage contract (ADR-M16).
CLEANUP_MODES = (OperationMode.S3_PRIMARY_LOCAL_FALLBACK, OperationMode.S3_ONLY)

#: Object statuses CLEANUP will consider at all.
CLEANABLE_STATUSES = ("Verified", "CleanupEligible")


def quarantine_root() -> str:
	return frappe.get_site_path("private", QUARANTINE_DIRNAME)


def quarantine_path(campaign: str, migration_object: str) -> str:
	"""A32 — `<trash>/<campaign>/<migration_object_name>`.

	Collision-free by construction: the migration object name is `sha1(campaign‖url_hash)`,
	so two files with the same basename in different directories cannot land on one another
	— which is precisely what a `<trash>/<basename>` layout would do to `a/report.pdf` and
	`b/report.pdf`, silently destroying one of them.
	"""
	return os.path.join(quarantine_root(), campaign, migration_object)


# ------------------------------------------------------------------------------- gates


def campaign_cleanup_gate(camp) -> tuple[bool, str]:
	"""The two campaign-level gates, re-evaluated inside the job rather than at button time."""
	if not (camp.cleanup_approved_by and camp.cleanup_approved_at):
		return False, "cleanup has not been approved"

	mode = get_mode()
	if mode not in CLEANUP_MODES:
		return False, f"operation mode is {mode.value}; local copies are still part of the contract"

	return True, "approved"


def object_cleanup_gate(obj, *, settings=None) -> tuple[bool, str]:
	"""Everything that must hold for ONE object, checked in cost order (cheap first)."""
	if obj.status not in CLEANABLE_STATUSES:
		return False, f"object is {obj.status}, not verified"

	if cint(obj.ignored_ref_count):
		# A17 — LOCAL_OPERATIONAL bytes are consumed through local paths (Package Import
		# feeds a tar subprocess). Removing the local copy breaks the consumer, and no
		# amount of remote verification makes that acceptable.
		return False, "a reference hangs off an ignored (LOCAL_OPERATIONAL) doctype"

	if obj.skip_reason == "superseded_by_runtime":
		return False, "a reference was superseded by the live runtime"

	if not obj.cloud_storage_object:
		return False, "object has no cloud object"

	cso = objects.get_cso(obj.cloud_storage_object)
	if cso is None:
		return False, "cloud object row is gone"
	if cso.status != "verified":
		return False, f"cloud object is {cso.status}, not verified"

	if not cint(obj.is_thumbnail) and not verify.object_is_fully_linked(obj.name):
		# Thumbnails have no File Refs of their own — core keeps no File row for a thumbnail,
		# only a `thumbnail_url` on the source. Their equivalent gate is `thumbnail_gate`,
		# which is strictly stronger: it requires the source object AND the derived object to
		# be verified before a local rendering may move.
		return False, "not every File reference carries the link"

	if not obj.disk_path or not os.path.exists(obj.disk_path):
		return False, "local file is not where the scan found it"

	return True, "eligible"


def fresh_remote_check(obj, camp, *, settings=None) -> tuple[bool, str]:
	"""Gate 5 — a NEW verification against the bucket, at deletion time.

	Cheap (a HEAD) below the cutover and a streamed re-GET above it, exactly as VERIFY does:
	the residual-risk register requires the pre-delete check to honour the same 64MB split,
	because above it the stored checksum is composite and proves nothing.
	"""
	cso = objects.get_cso(obj.cloud_storage_object)
	if cso is None:
		return False, "cloud object row is gone"
	try:
		verify.verify_object(cso, cutover_mb=camp.verify_strategy_cutover_mb, settings=settings)
	except Exception as exc:  # noqa: BLE001 - any failure blocks the deletion, by design
		return False, f"pre-delete verification failed: {exc}"
	return True, "remote object re-verified"


def path_confinement_gate(obj) -> tuple[bool, str]:
	"""A22 — the path CLEANUP is about to unlink must sit inside this site's own trees.

	**Found by independent security review.** Every other gate in this module checks the
	*object's* state — verified remotely, refs linked, mode, freshness. None checked the
	**path**. `disk_path` and `quarantine_path` are ordinary columns on `Cloud Migration Object`,
	which grants `write` to System Manager and does not mark `disk_path` read-only, so the
	confinement lived entirely in "the analyzer wrote this column" — which is to say nowhere,
	the same shape `cache.materialize.entry_path` had before it was fixed.

	On a multi-site bench an unconfined unlink crosses the **site boundary**: another site's
	attachments, or the bench's own app code. `analyzer.local_disk_path` already refuses
	non-canonical URLs for exactly this reason ("this path is later handed to `os.rename` by
	CLEANUP"); this is the same refusal applied at the moment of deletion, where it binds
	regardless of who wrote the column.
	"""
	roots = (
		frappe.get_site_path("public", "files"),
		frappe.get_site_path("private", "files"),
		quarantine_root(),
	)
	if not analyzer.confined_under(obj.disk_path, roots):
		return False, f"disk_path is outside this site's file trees: {obj.disk_path!r}"
	quarantine = getattr(obj, "quarantine_path", None)
	if quarantine and not analyzer.confined_under(quarantine, (quarantine_root(),)):
		return False, f"quarantine_path is outside the quarantine root: {quarantine!r}"
	return True, "paths confined to this site"


def local_recheck(obj) -> tuple[bool, str]:
	"""A4 — re-`stat`, and on drift re-hash the LOCAL file against the object's hash.

	The cheap check first: if size and mtime still match what the scan recorded, the file has
	not been touched. Any difference and the bytes are re-hashed, because a same-size
	overwrite is exactly the case a stat comparison misses — and it is also the case where
	deleting the local copy would destroy the only copy of the *new* bytes, since what is in
	the bucket is the old ones.
	"""
	path = obj.disk_path
	try:
		stat = os.stat(path)
	except OSError as exc:
		return False, f"local file could not be stat'ed: {exc}"

	recorded_size = cint(obj.size_bytes) or cint(obj.disk_size)
	if recorded_size and stat.st_size != recorded_size:
		return False, f"local size drifted: {stat.st_size} != {recorded_size}"

	drifted = _mtime_drifted(obj, stat)
	if not drifted:
		return True, "local file unchanged since upload"

	from cloud_file_storage.storage.hashing import digest_path

	digest = digest_path(path)
	if obj.sha256 and digest.sha256 != obj.sha256:
		return False, "local content drifted: the bytes on disk are not the bytes we uploaded"
	return True, "local file re-hashed and matches"


def _mtime_drifted(obj, stat) -> bool:
	from frappe.utils import get_datetime

	if not obj.disk_mtime:
		return True
	recorded = get_datetime(obj.disk_mtime)
	from datetime import datetime

	return abs((datetime.fromtimestamp(stat.st_mtime) - recorded).total_seconds()) > 1


def thumbnail_gate(obj) -> tuple[bool, str]:
	"""PLAN §A — a local thumbnail waits for source verification AND guaranteed availability."""
	if not cint(obj.is_thumbnail):
		return True, "not a thumbnail"

	parent = obj.parent_object
	if not parent:
		return False, "thumbnail object has no source object"

	files = frappe.db.get_all(
		REF_DOCTYPE, filters={"migration_object": parent}, pluck="file", limit_page_length=0
	)
	if not files:
		return False, "source object has no File references"

	from cloud_file_storage.serving import thumbnails

	for name in files:
		allowed, reason = thumbnails.can_release_local_thumbnail(frappe.get_doc("File", name))
		if not allowed:
			return False, f"thumbnail availability not guaranteed for {name}: {reason}"
	return True, "source and derived thumbnail are both verified"


# ------------------------------------------------------------------------------- the job


def run_cleanup_batch(campaign: str, batch: str) -> dict:
	"""One batch of cleanups. Every gate re-evaluated per object, inside the job."""
	with engine.migration_job():
		if not engine.cas_batch(batch, expected="CleanupDispatched", to="Cleaning", heartbeat=True):
			return {"claimed": False}

		camp = frappe.get_doc(CAMPAIGN_DOCTYPE, campaign)
		settings = get_settings()

		allowed, reason = campaign_cleanup_gate(camp)
		if not allowed:
			engine.cas_batch(batch, expected="Cleaning", to="Verified")
			audit.record("cleanup_refused", campaign=campaign, batch=batch, reason=reason)
			frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)
			return {"claimed": True, "refused": reason}

		beat = engine.Heartbeat(batch)
		cleaned = 0
		blocked = 0
		stopped = False

		try:
			for obj in engine.iter_batch_objects(batch, CLEANABLE_STATUSES):
				if engine.control_flag(campaign) in ("PAUSE", "STOP"):
					stopped = True
					break
				if cleanup_object(obj, camp, settings=settings):
					cleaned += 1
				else:
					blocked += 1
				beat.tick(size=cint(obj.size_bytes), failed=False)
		finally:
			beat.flush()

		engine.cas_batch(batch, expected="Cleaning", to="Cleaned" if not stopped else "Verified")
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)

		analyzer.refresh_counters(campaign)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)

		if not stopped:
			engine.chain_next(campaign)
		return {"claimed": True, "cleaned": cleaned, "blocked": blocked}


def cleanup_object(obj, camp, *, settings=None) -> bool:
	"""Run every gate, then quarantine (or delete). Returns whether the local copy moved.

	The campaign-level gate is re-checked here as well as in `run_cleanup_batch`, and that
	is deliberate rather than redundant: this function is the one that moves a file, so
	every caller — the batch job, a future Desk action, a console session — has to meet
	approval and the mode gate. A guard that only exists in the loop above it protects the
	loop, not the file.
	"""
	settings = settings or get_settings()

	for gate in (
		lambda: path_confinement_gate(obj),
		lambda: campaign_cleanup_gate(camp),
		lambda: object_cleanup_gate(obj, settings=settings),
		lambda: thumbnail_gate(obj),
		lambda: fresh_remote_check(obj, camp, settings=settings),
		lambda: local_recheck(obj),
	):
		allowed, reason = gate()
		if not allowed:
			_block(obj, camp, reason)
			return False

	if camp.cleanup_mode == "Direct Delete":
		return _delete_local(obj, camp)
	return _quarantine_local(obj, camp)


def _block(obj, camp, reason: str):
	"""Record why nothing was deleted, once, and leave the file alone."""
	engine._open_conflict(
		obj,
		camp,
		conflict_type="cleanup_blocked",
		severity="Warning",
		details={"reason": reason, "disk_path": obj.disk_path},
		error_class="CleanupBlocked",
	)
	audit.record(
		"cleanup_refused",
		campaign=camp.name,
		migration_object=obj.name,
		cloud_storage_object=obj.cloud_storage_object,
		reason=reason,
		disk_path=obj.disk_path,
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)


def _quarantine_local(obj, camp) -> bool:
	"""A32 — atomic rename into `<trash>/<campaign>/<object>`; refuse if the target exists."""
	target = quarantine_path(camp.name, obj.name)
	os.makedirs(os.path.dirname(target), exist_ok=True)

	if os.path.exists(target):
		# Never overwrite a quarantined file: the one already there is somebody's only
		# remaining local copy until the TTL purge, and the rename would destroy it.
		_block(obj, camp, f"quarantine target already exists: {target}")
		return False

	try:
		os.rename(obj.disk_path, target)
	except OSError as exc:
		# Includes EXDEV (different filesystems). Deliberately no copy-then-delete fallback:
		# that turns an atomic move into a window where both copies can be lost.
		_block(obj, camp, f"quarantine rename failed: {exc}")
		return False

	engine.cas_object(
		obj.name,
		expected=obj.status,
		to="Quarantined",
		quarantine_path=target,
		cleaned_at=now_datetime(),
	)
	audit.record(
		"local_quarantine",
		campaign=camp.name,
		migration_object=obj.name,
		cloud_storage_object=obj.cloud_storage_object,
		disk_path=obj.disk_path,
		quarantine_path=target,
		size_bytes=cint(obj.size_bytes),
		sha256=obj.sha256,
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)
	return True


def _delete_local(obj, camp) -> bool:
	"""Direct Delete mode — opt-in, same gates, no going back."""
	try:
		os.remove(obj.disk_path)
	except OSError as exc:
		_block(obj, camp, f"delete failed: {exc}")
		return False

	engine.cas_object(obj.name, expected=obj.status, to="CleanedUp", cleaned_at=now_datetime())
	audit.record(
		"local_delete",
		campaign=camp.name,
		migration_object=obj.name,
		cloud_storage_object=obj.cloud_storage_object,
		disk_path=obj.disk_path,
		size_bytes=cint(obj.size_bytes),
		sha256=obj.sha256,
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)
	return True


# ------------------------------------------------------------------------------- purge


def purge_expired_quarantine(limit: int = 1000) -> dict:
	"""A32 — DB-keyed TTL purge. Never filesystem mtime.

	`os.stat().st_mtime` on a quarantined file is the *original* file's mtime, because
	`rename` does not touch it — so an mtime-keyed purge would delete a file quarantined
	five minutes ago the moment it happened to be an old attachment, which is the exact
	opposite of a retention window. `cleaned_at` is when the quarantine happened, and it is
	the only clock this function reads.
	"""
	purged = 0
	failed = 0

	campaigns = frappe.db.get_all(
		CAMPAIGN_DOCTYPE,
		filters={"quarantine_ttl_days": (">", 0)},
		fields=["name", "quarantine_ttl_days"],
		limit_page_length=0,
	)
	for camp in campaigns:
		cutoff = add_to_date(now_datetime(), days=-cint(camp.quarantine_ttl_days))
		rows = frappe.db.get_all(
			OBJECT_DOCTYPE,
			filters={
				"campaign": camp.name,
				"status": "Quarantined",
				"cleaned_at": ("<", cutoff),
			},
			fields=["name", "quarantine_path", "cloud_storage_object", "size_bytes"],
			limit_page_length=limit,
		)
		for row in rows:
			if _purge_one(camp.name, row):
				purged += 1
			else:
				failed += 1

	return {"purged": purged, "failed": failed}


def _purge_one(campaign: str, row, *, camp=None) -> bool:
	"""Delete one expired quarantined file — after re-proving the remote object, now.

	The purge is the **last** moment a local copy exists, and it runs unattended from the
	scheduler up to `quarantine_ttl_days` (default 14) after the quarantine. The gates that
	justified that quarantine were evaluated then, not now, and docs/INVARIANTS.md invariant 1 asks
	for verification *at deletion time*: in fourteen days a bucket can lose an object, a
	lifecycle rule can move it, an operator can delete it by hand. So the remote object is
	re-verified here before the only remaining copy goes, and a failure blocks and audits
	rather than deleting — the same fail-safe direction as every other gate in this module.
	"""
	camp = camp or frappe.get_doc(CAMPAIGN_DOCTYPE, campaign)
	obj = frappe.db.get_value(OBJECT_DOCTYPE, row.name, "*", as_dict=True)
	if not obj:
		return False

	allowed, reason = _purge_gate(obj, camp)
	if not allowed:
		frappe.db.set_value(
			OBJECT_DOCTYPE,
			row.name,
			{"attempt_count": cint(obj.attempt_count) + 1, "last_error": reason[:500]},
			update_modified=False,
		)
		audit.record(
			"cleanup_refused",
			campaign=campaign,
			migration_object=row.name,
			cloud_storage_object=row.cloud_storage_object,
			phase="quarantine_purge",
			reason=reason,
			quarantine_path=row.quarantine_path,
		)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)
		return False

	path = row.quarantine_path
	# The same confinement the cleanup gate applies, repeated here because this is a SEPARATE
	# entry point: `_purge_one` is reached from the nightly scheduler (hooks.py), not through
	# `cleanup_object`, so the gate chain never runs for it. `_purge_gate` above checks only
	# that the REMOTE object is verified -- nothing in that path looked at the local path.
	if not analyzer.confined_under(path, (quarantine_root(),)):
		frappe.log_error(
			title="Quarantine purge refused: path outside the quarantine root",
			message=f"{row.name}: {path!r}",
		)
		return False
	try:
		if path and os.path.exists(path):
			os.remove(path)
	except OSError as exc:
		frappe.log_error(title="Quarantine purge failed", message=f"{row.name}: {exc}")
		return False

	engine.cas_object(row.name, expected="Quarantined", to="CleanedUp")
	audit.record(
		"quarantine_purge",
		campaign=campaign,
		migration_object=row.name,
		cloud_storage_object=row.cloud_storage_object,
		quarantine_path=path,
		size_bytes=cint(row.size_bytes),
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)
	return True


def _purge_gate(obj, camp) -> tuple[bool, str]:
	"""What must still hold before the last local copy of an object is destroyed.

	Deliberately narrower than `object_cleanup_gate`: by now the File rows may legitimately
	have moved on — the runtime can have relinked or deleted them — and the question is no
	longer "should this have been quarantined", which was answered when it was, but "are the
	bytes still in the bucket". So: a cloud object, verified or adopted, and a **fresh**
	check against the bucket using the same strategy split VERIFY uses.
	"""
	if not obj.cloud_storage_object:
		return False, "object has no cloud object"

	cso = objects.get_cso(obj.cloud_storage_object)
	if cso is None:
		return False, "cloud object row is gone"
	if cso.status not in ("verified", "legacy_unverified"):
		return False, f"cloud object is {cso.status}, not verified"

	try:
		verify.verify_object(cso, cutover_mb=camp.verify_strategy_cutover_mb)
	except Exception as exc:  # noqa: BLE001 - any failure keeps the file, by design
		return False, f"pre-purge verification failed: {exc}"

	return True, "remote object re-verified at purge time"


def restore_from_quarantine(migration_object: str) -> bool:
	"""Put a quarantined file back where it came from. The reason quarantine is the default.

	Refuses if something already occupies the original path — that would be a *newer* file
	the site created after the quarantine, and overwriting it to undo an old decision is a
	second data loss, not a repair.
	"""
	obj = frappe.db.get_value(
		OBJECT_DOCTYPE,
		migration_object,
		["name", "campaign", "status", "disk_path", "quarantine_path", "cloud_storage_object"],
		as_dict=True,
	)
	if not obj or obj.status != "Quarantined":
		return False
	if not obj.quarantine_path or not os.path.exists(obj.quarantine_path):
		return False
	# Third entry point, third confinement check. A restore moves BOTH ways -- out of the
	# quarantine root and back onto `disk_path` -- and both columns are Desk-writable, so an
	# unconfined restore is an arbitrary file MOVE rather than merely an arbitrary delete.
	allowed, reason = path_confinement_gate(frappe._dict(obj))
	if not allowed:
		frappe.log_error(title="Quarantine restore refused", message=f"{obj.name}: {reason}")
		return False
	if not obj.disk_path or os.path.exists(obj.disk_path):
		return False

	os.makedirs(os.path.dirname(obj.disk_path), exist_ok=True)
	os.rename(obj.quarantine_path, obj.disk_path)

	engine.cas_object(obj.name, expected="Quarantined", to="Verified", quarantine_path=None, cleaned_at=None)
	audit.record(
		"conflict_action",
		campaign=obj.campaign,
		migration_object=obj.name,
		cloud_storage_object=obj.cloud_storage_object,
		action="restore_from_quarantine",
		disk_path=obj.disk_path,
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)
	return True
