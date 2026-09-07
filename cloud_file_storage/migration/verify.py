"""Independent verification, and the one place a File row learns about its cloud object.

**This module contains no deletion path** (PLAN §C, gate F6) — it is asserted by
`tests/test_migration_safety_lint.py`, which walks this file's AST.

Two invariants shape everything here:

* **Nothing is linked before it is verified** (ADR-M10). Writing `File.cloud_storage_object`
  flips serving to S3 for that row, so it happens only after a *fresh, independent* check of
  the remote bytes — never on the strength of "the upload call returned".
* **The link never clobbers a newer runtime relink** (A26). Between the scan and this
  transaction the site is live: `save_file(overwrite=True)` can have replaced the bytes,
  a privacy flip can have rewritten the URL, DUAL_WRITE can have linked the row to a
  different object. The UPDATE is guarded on all three, and refs that no longer match are
  `Skipped(superseded_by_runtime)` with an audit row — never `CleanupEligible`, because the
  local file of a superseded ref must stay exactly where it is.

This module holds the third sanctioned raw-SQL site against `tabFile` (docs/INVARIANTS.md invariant
5): :func:`link_refs`, which is the guarded UPDATE plus the `content_hash` backfill that
design §6.4 step 3 requires in the same transaction.
"""

import frappe
from frappe.utils import cint, now_datetime

from cloud_file_storage.migration import audit, engine
from cloud_file_storage.storage import engine as storage_engine
from cloud_file_storage.storage import objects
from cloud_file_storage.storage.exceptions import CloudObjectNotFound, CloudStorageIntegrityError
from cloud_file_storage.storage.modes import get_settings

OBJECT_DOCTYPE = "Cloud Migration Object"
REF_DOCTYPE = "Cloud Migration File Ref"
BATCH_DOCTYPE = "Cloud Migration Batch"

#: Object statuses VERIFY picks up. `DedupReused` and `Adopted` are here deliberately:
#: neither uploaded anything, but both still need a fresh remote check before their File
#: rows may be linked, and the guarded link exists in exactly one place.
VERIFIABLE_STATUSES = ("Uploaded", "DedupReused", "Adopted")

MEGABYTE = 1024 * 1024


def run_verify_batch(campaign: str, batch: str) -> dict:
	"""Verify one batch, then link the refs of everything that verified."""
	with engine.migration_job():
		# A31 — a batch with objects still Pending or Uploading has not finished uploading,
		# whatever its own status says. Verifying it would declare a batch verified while
		# some of its bytes were never sent.
		residue = engine.batch_has_upload_residue(batch)
		if residue:
			engine.cas_batch(batch, expected="VerifyDispatched", to="Pending")
			frappe.db.set_value(
				BATCH_DOCTYPE,
				batch,
				"last_error",
				f"verify refused: {residue} object(s) still Pending/Uploading",
				update_modified=False,
			)
			frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)
			return {"claimed": False, "residue": residue}

		if not engine.cas_batch(batch, expected="VerifyDispatched", to="Verifying", heartbeat=True):
			return {"claimed": False}

		camp = engine._campaign(campaign)
		settings = get_settings()
		beat = engine.Heartbeat(batch)
		stopped = False

		try:
			for obj in engine.iter_batch_objects(batch, VERIFIABLE_STATUSES):
				if engine.control_flag(campaign) in ("PAUSE", "STOP"):
					stopped = True
					break
				ok = verify_and_link(obj, camp, settings)
				beat.tick(size=cint(obj.size_bytes), failed=not ok)
		finally:
			beat.flush()

		_finalize_verify_batch(batch, camp, stopped=stopped)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)

		from cloud_file_storage.migration import analyzer

		analyzer.refresh_counters(campaign)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)

		if not stopped:
			engine.chain_next(campaign)
		return {"claimed": True, "objects": beat.count, "failed": beat.failed}


def _finalize_verify_batch(batch: str, camp, *, stopped: bool = False) -> str:
	"""Mirror of A31 for the verify arm: no `Verified` while verifiable objects remain."""
	residue = frappe.db.count(
		OBJECT_DOCTYPE, {"batch": batch, "status": ("in", VERIFIABLE_STATUSES + ("Verifying",))}
	)
	if residue == 0:
		if engine.cas_batch(batch, expected="Verifying", to="Verified"):
			return "Verified"
		return frappe.db.get_value(BATCH_DOCTYPE, batch, "status")

	if stopped:
		engine.cas_batch(batch, expected="Verifying", to="Uploaded")
		return "Uploaded"

	attempts = cint(frappe.db.get_value(BATCH_DOCTYPE, batch, "attempts")) + 1
	target = "Failed" if attempts > cint(camp.max_attempts or 5) else "Uploaded"
	frappe.db.sql(
		f"UPDATE `tab{BATCH_DOCTYPE}` SET status=%(target)s, attempts=%(attempts)s, "
		"last_error=%(last_error)s WHERE name=%(name)s AND status='Verifying'",
		{
			"target": target,
			"attempts": attempts,
			"last_error": f"{residue} object(s) still unverified after the batch job",
			"name": batch,
		},
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)
	return target


# ------------------------------------------------------------------- per-object verify


def verify_object(cso, *, cutover_mb: int, settings=None) -> dict:
	"""The strategy split (ADR-M7/M8). Raises on any mismatch; never mutates anything.

	Below the cutover the object is guaranteed single-part, so S3's `ChecksumSHA256` **is**
	the full-object SHA256 and one HEAD settles it. At or above it the stored checksum is a
	checksum of part checksums, which cannot be compared against a full-object hash — so the
	bytes are re-fetched and re-hashed. That is more expensive and it is also the strongest
	independent check there is; at an 83KB average the tail above 64MB is a few hundred
	files.
	"""
	size = cint(cso.file_size)
	if size >= cint(cutover_mb) * MEGABYTE:
		return storage_engine.verify_by_streaming(cso, settings=settings)
	return storage_engine.verify(cso, settings=settings)


def verify_and_link(obj, camp, settings) -> bool:
	"""Verify one object and, on success, link its refs in a single transaction."""
	previous_status = obj.status
	if not engine.cas_object(obj.name, expected=previous_status, to="Verifying"):
		return False

	cso_name = obj.cloud_storage_object or frappe.db.get_value(
		OBJECT_DOCTYPE, obj.name, "cloud_storage_object"
	)
	if not cso_name:
		engine._open_conflict(
			obj,
			camp,
			conflict_type="verify_failed",
			severity="Blocker",
			details={"reason": "object reached VERIFY with no cloud object"},
			error_class="MissingCloudStorageObject",
		)
		return False

	cso = objects.get_cso(cso_name)
	if cso is None:
		engine._open_conflict(
			obj,
			camp,
			conflict_type="verify_failed",
			severity="Blocker",
			details={"reason": "cloud object row vanished", "cloud_storage_object": cso_name},
			error_class="MissingCloudStorageObject",
		)
		return False

	try:
		verify_object(cso, cutover_mb=camp.verify_strategy_cutover_mb, settings=settings)
	except (CloudStorageIntegrityError, CloudObjectNotFound) as exc:
		_record_mismatch(obj, camp, cso, exc)
		return False
	except Exception as exc:  # noqa: BLE001 - a transient failure retries, it does not condemn
		engine.cas_object(
			obj.name,
			expected="Verifying",
			to="Uploaded",
			attempt_count=cint(obj.attempt_count) + 1,
			error_class=type(exc).__name__,
			last_error=str(exc)[:500],
		)
		return False

	# The bytes are proven. Everything below is one transaction: CSO status, the guarded
	# File link, the ref bookkeeping and the object's own status commit or roll back
	# together, so there is no window in which a File points at an unverified object.
	#
	# A15 — an ADOPTED object is the one case where passing `verify_object` does not earn
	# the `verified` status. For a `legacy_unverified` row the check that just ran was
	# existence, size and (when the ETag is usable at all) an MD5 comparison — never a
	# SHA256 hash of the bytes, because nobody has ever hashed them. Promoting here would
	# make the row claim a verification that did not happen, on exactly the population A15
	# was written about. The link is still written: those bytes were already what the site
	# was serving, and `legacy_unverified` is a servable status. `adoption.promote_adopted_
	# object` is the only path to `verified`, and it re-reads every byte.
	if cso.status not in ("verified", "legacy_unverified"):
		objects.set_cso_status(cso.name, "verified")

	result = link_refs(obj, cso, campaign=camp.name)

	engine.cas_object(
		obj.name,
		expected="Verifying",
		to="Verified",
		verified_at=now_datetime(),
		error_class=None,
		last_error=None,
	)
	objects.refresh_reference_count(cso.name)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)

	audit.record(
		"file_link",
		campaign=camp.name,
		migration_object=obj.name,
		cloud_storage_object=cso.name,
		linked=result["linked"],
		superseded=result["superseded"],
		refs=result["refs"],
		row_count=result["row_count"],
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)
	return True


def _record_mismatch(obj, camp, cso, exc):
	"""A26/§6.4 step 4 — the object fails, the CSO is not promoted, the local file is untouched.

	`checksum_unstable` is the second-mismatch case: a file whose *local* hash changed
	between attempts is being actively rewritten by the live site (an overwrite flow), which
	is a different problem from a bucket that returned the wrong bytes and needs a different
	answer from the operator.
	"""
	recorded = frappe.db.get_value(OBJECT_DOCTYPE, obj.name, ["sha256", "attempt_count"], as_dict=True)
	unstable = cint(recorded.attempt_count) > 0 and recorded.sha256 and recorded.sha256 != cso.content_sha256

	engine._open_conflict(
		obj,
		camp,
		conflict_type="checksum_unstable" if unstable else "checksum_mismatch",
		severity="Blocker",
		details={
			"error": str(exc)[:500],
			"expected_sha256": cso.content_sha256,
			"recorded_sha256": recorded.sha256,
			"s3_key": cso.s3_key,
			"size_bytes": cint(cso.file_size),
		},
		error_class=type(exc).__name__,
		attempt_count=cint(recorded.attempt_count) + 1,
	)


# --------------------------------------------------- A26: the guarded link (sanctioned SQL)


#: The A26 guard, verbatim in structure:
#:
#: * `f.cloud_storage_object IS NULL OR = %(cso)s` — never steal a row the runtime relinked;
#: * `f.file_url = r.file_url` — never link a row whose URL moved under us (a privacy flip);
#: * `f.content_hash IS NULL OR = %(md5)s` — never link a row whose bytes were replaced.
#:
#: The one extension is the `COALESCE`: an adopted legacy object has no MD5 of its own (A15),
#: and `f.content_hash = NULL` is never true, so a literal `%(md5)s` would refuse to link
#: every adopted row that *does* carry a hash. Falling back to the hash recorded at scan time
#: keeps the guard's meaning exactly — "this row still holds the bytes we analysed" — in the
#: case A26 was not written for.
LINK_SQL = """
	UPDATE `tabFile` f
	JOIN `tabCloud Migration File Ref` r ON r.file = f.name
	SET f.cloud_storage_object = %(cso)s
	WHERE r.migration_object = %(obj)s
	  AND (f.cloud_storage_object IS NULL OR f.cloud_storage_object = %(cso)s)
	  AND f.file_url = r.file_url
	  AND (f.content_hash IS NULL OR f.content_hash = COALESCE(%(md5)s, r.content_hash))
"""

#: Restores core dedup/refcount on rows the 0.2.x fork nulled (contract #15). Scoped to the
#: rows the statement above just linked, and only where the column is empty — it never
#: overwrites a hash the site already believes.
BACKFILL_SQL = """
	UPDATE `tabFile` f
	JOIN `tabCloud Migration File Ref` r ON r.file = f.name
	SET f.content_hash = %(md5)s
	WHERE r.migration_object = %(obj)s
	  AND f.cloud_storage_object = %(cso)s
	  AND (f.content_hash IS NULL OR f.content_hash = '')
	  AND %(md5)s IS NOT NULL
"""

#: Which refs actually ended up on this object. It re-asserts `f.file_url = r.file_url`
#: alongside the link, because a row can carry this object legitimately (DUAL_WRITE linked
#: it) while having moved to a different URL — counting that as "linked by us" would let
#: CLEANUP quarantine a local file at a path this campaign never verified.
#: Read back rather than inferred from ROW_COUNT: MariaDB reports *changed* rows, so a ref that was already correctly linked
#: (a retried batch, a DUAL_WRITE row that got there first) reports zero and would be
#: mis-declared superseded — and a superseded ref is one whose local file must never be
#: cleaned up. ROW_COUNT is still captured, and recorded in the audit row.
LINKED_REFS_SQL = """
	SELECT r.name AS ref, r.file AS file
	FROM `tabCloud Migration File Ref` r
	JOIN `tabFile` f ON f.name = r.file
	WHERE r.migration_object = %(obj)s
	  AND f.cloud_storage_object = %(cso)s
	  AND f.file_url = r.file_url
"""


def link_refs(obj, cso, *, campaign: str) -> dict:
	"""The guarded VERIFY link transaction (A26). The only write this engine makes to `tabFile`.

	Returns `{refs, linked, superseded, row_count}`. Every ref that did not end up on this
	object is marked `Skipped(superseded_by_runtime)` and audited — the runtime won, which is
	the correct outcome, and the object is barred from CLEANUP.
	"""
	md5 = cso.content_hash_md5 or None
	params = {"cso": cso.name, "obj": obj.name, "md5": md5}

	frappe.db.sql(LINK_SQL, params)
	row_count = cint(frappe.db._cursor.rowcount)

	frappe.db.sql(BACKFILL_SQL, params)

	linked = frappe.db.sql(LINKED_REFS_SQL, params, as_dict=True)
	linked_refs = {row.ref for row in linked}

	all_refs = frappe.db.get_all(
		REF_DOCTYPE, filters={"migration_object": obj.name}, fields=["name", "file"], limit_page_length=0
	)
	superseded = [ref for ref in all_refs if ref.name not in linked_refs]

	if linked_refs:
		frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- interpolates only closed-set identifiers (module constants / _file_columns()); every value is parameter-bound
			f"UPDATE `tab{REF_DOCTYPE}` SET linked = 1 WHERE name IN %(names)s",
			{"names": tuple(linked_refs)},
		)

	for ref in superseded:
		frappe.db.set_value(
			REF_DOCTYPE,
			ref.name,
			{"linked": 0, "skip_reason": "superseded_by_runtime"},
			update_modified=False,
		)
		audit.record(
			"link_skipped",
			campaign=campaign,
			migration_object=obj.name,
			file=ref.file,
			cloud_storage_object=cso.name,
			reason="superseded_by_runtime",
		)

	if superseded:
		# A26 — an object whose refs did not all land is never CleanupEligible. Recorded on
		# the object so CLEANUP can refuse it without re-deriving the reason.
		frappe.db.set_value(
			OBJECT_DOCTYPE,
			obj.name,
			{"skip_reason": "superseded_by_runtime"},
			update_modified=False,
		)

	return {
		"refs": len(all_refs),
		"linked": len(linked_refs),
		"superseded": len(superseded),
		"row_count": row_count,
	}


def object_is_fully_linked(migration_object: str) -> bool:
	"""Every ref of this object carries the link. CLEANUP's precondition (design §6.5 gate 3)."""
	total = frappe.db.count(REF_DOCTYPE, {"migration_object": migration_object})
	linked = frappe.db.count(REF_DOCTYPE, {"migration_object": migration_object, "linked": 1})
	return bool(total) and total == linked
