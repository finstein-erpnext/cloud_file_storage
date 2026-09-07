"""Cloud Storage Object state — the single choke point (A7).

Every status change, every reference count and every resurrection goes through this
module. The migration engine imports `ensure_cso` / `adopt_references` / `set_cso_status`
(A7) plus `adopt_legacy_cso` — added by P5's entry gate, which asserts that the frozen four
cannot express an adoption — and never inlines SQL against the CSO table (docs/INVARIANTS.md
invariant 4).

Two properties this module exists to guarantee:

* **Reference counts are derived, never stored.** `frappe.db.delete("File", ...)` bypasses
  `on_trash` entirely, so a stored counter drifts permanently. The live count is read from
  `tabFile` under a row lock at the moment it matters (ADR-4/A6). `reference_count` on the
  doc is a list-view convenience and is documented as non-authoritative.
* **Nothing is ever destroyed by a state change.** `pending_delete` is a schedule, not a
  deletion; the physical `DeleteObject` lives only in `gc.py`.
"""

import frappe
from frappe.query_builder.functions import Count
from frappe.utils import add_to_date, cint, now_datetime

from cloud_file_storage.cloud_file_storage.doctype.cloud_storage_object.cloud_storage_object import (
	PRESENT_STATUSES,
	SERVABLE_STATUSES,
	STATUSES,
)
from cloud_file_storage.storage import keys
from cloud_file_storage.storage.modes import get_settings

CSO_DOCTYPE = "Cloud Storage Object"

#: What frappe raises when an insert loses a race on a unique index. `UniqueValidationError`
#: is the index case (`base_document.show_unique_validation_message`); `DuplicateEntryError`
#: is the primary-key case.
UNIQUE_VIOLATIONS = (frappe.UniqueValidationError, frappe.DuplicateEntryError)

#: Campaign statuses after which a campaign no longer owns its objects (A14). Everything
#: else counts as a live reference so a sweep cannot delete bytes a campaign still needs.
TERMINAL_CAMPAIGN_STATUSES = ("Completed", "Failed", "Stopped")

#: Which status may follow which. Same-status is always a no-op. Deliberately explicit:
#: an unlisted jump is a bug and raises rather than silently fabricating a state.
_ALLOWED_TRANSITIONS = {
	"pending_upload": {"uploaded", "failed", "orphaned", "pending_delete", "legacy_unverified"},
	"uploaded": {"verified", "failed", "orphaned", "pending_delete"},
	"verified": {"failed", "orphaned", "pending_delete"},
	"failed": {"pending_upload", "uploaded", "orphaned", "pending_delete"},
	"orphaned": {"pending_upload", "uploaded", "verified", "pending_delete", "legacy_unverified"},
	# Revival out of pending_delete restores the prior status (A15) — every non-terminal
	# status is reachable, plus the one real deletion edge.
	"pending_delete": {
		"pending_upload",
		"uploaded",
		"verified",
		"failed",
		"orphaned",
		"legacy_unverified",
		"deleted",
	},
	# A25 tombstone revival: a deleted object re-uploads as a fresh pending_upload.
	"deleted": {"pending_upload"},
	"legacy_unverified": {"verified", "failed", "orphaned", "pending_delete"},
}


class InvalidStatusTransition(frappe.ValidationError):
	pass


def lock_cso(name: str) -> str | None:
	"""Take the row lock and return the current status (None when the row is gone).

	MariaDB's REPEATABLE READ will happily serve a stale non-locking count, which is
	exactly the failure A6 corrects: every decision that depends on "how many references
	are there right now" must start here.
	"""
	return frappe.db.get_value(CSO_DOCTYPE, name, "status", for_update=True)


def live_reference_count(name: str, *, exclude_file: str | None = None, for_update: bool = True) -> int:
	"""How many live references this object has (A6).

	Three kinds of reference are counted, and the count is deliberately conservative —
	over-counting delays a deletion, under-counting destroys bytes:

	1. File rows with an explicit `cloud_storage_object` link;
	2. File rows with **no** link that the A1 resolution chain would still resolve to this
	   object through a content-hash sibling. `frappe.db.delete` and legacy/unadopted rows
	   both produce these, and to a link-only count such a reader is worth zero;
	3. migration objects of non-terminal campaigns (A14).

	`for_update` defaults to True and that is the whole point: `frappe.db.count` issues a
	plain `SELECT COUNT(*)` (it has no `for_update` parameter), and under InnoDB REPEATABLE
	READ a plain read uses the transaction's original read view — established, for a GC
	sweep, by the candidate query *before* any lock was taken. Locking the CSO row does not
	lock or serialise the File rows being counted, so a File committed onto this object after
	the sweep started would be invisible and its bytes deleted.

	It is passed False by exactly one caller, `refresh_reference_count`, which maintains a
	denormalized list-view number documented as never authoritative. Taking a range lock on
	`tabFile` on every write to keep a cosmetic counter fresh buys nothing and costs
	contention; every caller whose answer decides whether bytes die keeps the locking read.

	**These locking reads depend on the indexes `install.ensure_file_indexes` creates on
	`tabFile`** — `cloud_storage_object` and the `content_hash(32)` prefix. They are what keep
	`FOR UPDATE` locking the matching rows instead of escalating to a scan of a 1.2M-row
	table, which at production scale would make the A6 fix correct and unusable at the same
	time. Dropping either index is not a performance decision; it changes what this lock
	locks.
	"""
	file = frappe.qb.DocType("File")

	linked = frappe.qb.from_(file).select(Count("*")).where(file.cloud_storage_object == name)
	if exclude_file:
		linked = linked.where(file.name != exclude_file)
	if for_update:
		linked = linked.for_update()
	result = linked.run()
	count = cint(result[0][0]) if result else 0

	return (
		count
		+ unlinked_sibling_count(name, exclude_file=exclude_file, for_update=for_update)
		+ migration_reference_count(name, for_update=for_update)
	)


def unlinked_sibling_count(name: str, *, exclude_file: str | None = None, for_update: bool = True) -> int:
	"""File rows the A1 chain resolves to this object without carrying a link of their own.

	This must mirror **both** arms of `CloudFile._resolve_cso_uncached`, because a reader it
	does not see is a reader GC is entitled to delete out from under:

	* the `file_url` arm — a link-less row sharing its URL with a row that IS linked here
	  resolves through that sibling. Its own `content_hash` may be NULL or divergent, so the
	  hash arm alone never sees it, and `patches/v0_2_0/backfill_s3_object_key` nulls exactly
	  that column on fork-era rows;
	* the `content_hash` + `is_private` arm — the object's own identity.

	Rows whose `content_hash` is NULL are never matched by the hash arm: a NULL filter
	compiles to `IS NULL` in frappe's query builder and would match every hashless row on the
	site (the R6 trap).
	"""
	identity = frappe.db.get_value(CSO_DOCTYPE, name, ["content_hash_md5", "visibility"], as_dict=True)
	# Reading the identity without a lock is safe ONLY because `content_hash_md5` and
	# `visibility` are immutable once `ensure_cso` has inserted the row: they are part of the
	# unique identity index, and a change would mean a different object. If a later phase
	# ever makes either mutable, this read has to move under the lock.
	#
	# One amendment, P3: `restamp_derived_visibility` can change `visibility` on a DERIVED
	# object when its source's privacy flips. The read stays unlocked — but NOT on the grounds
	# that a stale label is merely over-inclusive. `is_private` is half of the hash arm's
	# predicate, so a stale label does not only add rows: it changes WHICH rows match, and it
	# can drop one as easily as add one.
	#
	# The reason it is safe is stronger than that. The A1 hash arm resolves an unlinked File
	# row only THROUGH a sibling File that already carries a `cloud_storage_object` link, and
	# no File row is ever linked to a derived object — a thumbnail has no File row of its own,
	# and the byte-identical-to-source case resolves to the SOURCE object rather than creating
	# a derived one. So for a derived object the hash arm's true contribution is exactly zero,
	# every row it matches here is spurious, and whichever way a stale label shifts the match
	# set the result stays at or above the truth. For primary objects nothing changed: their
	# visibility is still immutable, and `restamp_derived_visibility` refuses them outright.
	if not identity:
		return 0

	file = frappe.qb.DocType("File")

	linked_urls = _linked_file_urls(name, for_update=for_update)

	resolves_here = None
	if identity.content_hash_md5:
		resolves_here = (file.content_hash == identity.content_hash_md5) & (
			file.is_private == (1 if identity.visibility == "private" else 0)
		)
	if linked_urls:
		by_url = file.file_url.isin(linked_urls)
		resolves_here = by_url if resolves_here is None else (resolves_here | by_url)

	if resolves_here is None:
		return 0

	query = (
		frappe.qb.from_(file)
		.select(Count("*"))
		.where(resolves_here)
		.where(file.cloud_storage_object.isnull() | (file.cloud_storage_object == ""))
		.where(file.is_folder == 0)
	)
	if exclude_file:
		query = query.where(file.name != exclude_file)
	if for_update:
		query = query.for_update()

	result = query.run()
	return cint(result[0][0]) if result else 0


def _linked_file_urls(name: str, *, for_update: bool = True) -> list[str]:
	"""The canonical URLs of rows linked to this object, for the A1 `file_url` arm."""
	file = frappe.qb.DocType("File")
	query = (
		frappe.qb.from_(file)
		.select(file.file_url)
		.distinct()
		.where(file.cloud_storage_object == name)
		.where(file.file_url.isnotnull())
		.where(file.file_url != "")
	)
	if for_update:
		query = query.for_update()

	return [row[0] for row in (query.run() or []) if row[0]]


def migration_reference_count(name: str, *, for_update: bool = True) -> int:
	"""References held by migration objects of non-terminal campaigns (A14).

	Locking for the same reason as :func:`live_reference_count` — P5's engine inherits this
	count and must not be handed a stale one.

	Zero until P5 ships the doctype; guarded on the table and column so this module never
	depends on the migration engine being installed.
	"""
	if not frappe.db.table_exists("Cloud Migration Object"):
		return 0
	if not frappe.db.has_column("Cloud Migration Object", "cloud_storage_object"):
		return 0

	migration_object = frappe.qb.DocType("Cloud Migration Object")
	campaign = frappe.qb.DocType("Cloud Migration Campaign")
	query = (
		frappe.qb.from_(migration_object)
		.join(campaign)
		.on(migration_object.campaign == campaign.name)
		.select(Count(migration_object.name))
		.where(migration_object.cloud_storage_object == name)
		.where(campaign.status.notin(TERMINAL_CAMPAIGN_STATUSES))
	)
	if for_update:
		query = query.for_update()

	result = query.run()
	return cint(result[0][0]) if result else 0


def needs_upload(cso) -> bool:
	"""A25 — positive upload gate: upload unless the bytes are known to be present."""
	return (cso.status if hasattr(cso, "status") else cso.get("status")) not in PRESENT_STATUSES


def is_servable(cso) -> bool:
	status = cso.status if hasattr(cso, "status") else cso.get("status")
	return status in SERVABLE_STATUSES


def get_cso(name: str):
	if not name:
		return None
	try:
		return frappe.get_doc(CSO_DOCTYPE, name)
	except frappe.DoesNotExistError:
		return None


def find_by_identity(content_sha256: str, visibility: str, bucket: str, *, for_update: bool = False):
	"""Locking-or-not lookup on the unique identity index."""
	return frappe.db.get_value(
		CSO_DOCTYPE,
		{"content_sha256": content_sha256, "visibility": visibility, "bucket": bucket},
		"name",
		for_update=for_update,
	)


def ensure_cso(
	*,
	content_sha256: str,
	content_hash_md5: str,
	file_size: int,
	visibility: str,
	mime_type: str | None = None,
	source_path: str | None = None,
	migration_batch: str | None = None,
	settings=None,
):
	"""Get-or-create the object row for this identity, reviving tombstones (A25).

	Returns the `Cloud Storage Object` document. Whether the caller must then upload is
	answered by :func:`needs_upload` — never inferred from "did we just create the row".
	"""
	settings = settings or get_settings()
	bucket = settings.bucket
	if not bucket:
		from cloud_file_storage.storage.exceptions import CloudStorageConfigurationError

		raise CloudStorageConfigurationError("Cloud Storage Settings has no bucket configured")

	s3_key = keys.build_object_key(content_sha256, visibility, key_prefix=settings.key_prefix)

	# The locking read also gap-locks the identity index, which is what serializes two
	# concurrent creators; the DuplicateEntry retry below covers the residual race.
	name = find_by_identity(content_sha256, visibility, bucket, for_update=True)
	if name:
		_revive_if_needed(name)
		return frappe.get_doc(CSO_DOCTYPE, name)

	doc = frappe.new_doc(CSO_DOCTYPE)
	doc.update(
		{
			"content_sha256": content_sha256,
			"content_hash_md5": content_hash_md5,
			"file_size": cint(file_size),
			"visibility": visibility,
			"mime_type": mime_type,
			"bucket": bucket,
			"s3_key": s3_key,
			"status": "pending_upload",
			"sse_mode": settings.sse_mode,
			"kms_key_id": settings.kms_key_id if settings.sse_mode == "SSE-KMS" else None,
			"source_path": source_path,
			"migration_batch": migration_batch,
		}
	)
	frappe.db.savepoint("cfs_ensure_cso")
	try:
		doc.insert(ignore_permissions=True)
	except UNIQUE_VIOLATIONS:
		# A concurrent creator won the unique index (frappe raises UniqueValidationError for
		# an index violation and DuplicateEntryError for a name clash). Re-read theirs;
		# identical identity means identical key, so both callers can upload the same bytes
		# harmlessly.
		frappe.db.rollback(save_point="cfs_ensure_cso")
		name = find_by_identity(content_sha256, visibility, bucket, for_update=True)
		if not name:
			raise
		_revive_if_needed(name)
		return frappe.get_doc(CSO_DOCTYPE, name)

	return doc


def ensure_derived_cso(
	*,
	s3_key: str,
	derived_of: str,
	derived_suffix: str,
	content_sha256: str,
	content_hash_md5: str,
	file_size: int,
	visibility: str,
	mime_type: str | None = None,
	settings=None,
):
	"""Get-or-create the object row for a derived (thumbnail) object.

	Separate from :func:`ensure_cso` because the key of a derived object is not a function
	of its own bytes: it is the SOURCE hash plus the rendered dimensions (PLAN.md §A), so
	re-rendering the same thumbnail converges on one object even if the encoder produced
	different bytes. Identity here is therefore `s3_key`, which carries its own unique index.

	The case that makes this more than a copy of `ensure_cso`: a thumbnail box larger than
	the image leaves the pixels untouched, and PIL's encoder is deterministic, so the
	"thumbnail" of a small PNG can be **byte-identical to its source**. That collides with
	the frozen `(content_sha256, visibility, bucket)` unique index. Those bytes are already
	in the bucket under the existing key, and content addressing says one set of bytes is
	one object — so the existing object is returned rather than duplicated. The File row's
	`cloud_thumbnail_object` link is what records which object serves the thumbnail URL,
	precisely so this case has somewhere to point.
	"""
	settings = settings or get_settings()
	bucket = settings.bucket
	if not bucket:
		from cloud_file_storage.storage.exceptions import CloudStorageConfigurationError

		raise CloudStorageConfigurationError("Cloud Storage Settings has no bucket configured")

	name = frappe.db.get_value(CSO_DOCTYPE, {"s3_key": s3_key, "bucket": bucket}, "name", for_update=True)
	if name:
		_revive_if_needed(name)
		doc = frappe.get_doc(CSO_DOCTYPE, name)
		# The bytes may legitimately differ (a different encoder, a re-render) while the key
		# stays the same, so the recorded identity follows the newest upload.
		if doc.content_sha256 != content_sha256:
			frappe.db.set_value(
				CSO_DOCTYPE,
				name,
				{
					"content_sha256": content_sha256,
					"content_hash_md5": content_hash_md5,
					"file_size": cint(file_size),
				},
				update_modified=False,
			)
			doc.reload()
		return doc

	doc = frappe.new_doc(CSO_DOCTYPE)
	doc.update(
		{
			"content_sha256": content_sha256,
			"content_hash_md5": content_hash_md5,
			"file_size": cint(file_size),
			"visibility": visibility,
			"mime_type": mime_type,
			"bucket": bucket,
			"s3_key": s3_key,
			"status": "pending_upload",
			"sse_mode": settings.sse_mode,
			"kms_key_id": settings.kms_key_id if settings.sse_mode == "SSE-KMS" else None,
			"derived_of": derived_of,
			"derived_suffix": derived_suffix,
		}
	)
	frappe.db.savepoint("cfs_ensure_derived_cso")
	try:
		doc.insert(ignore_permissions=True)
	except UNIQUE_VIOLATIONS:
		frappe.db.rollback(save_point="cfs_ensure_derived_cso")
		# Either a concurrent creator won the s3_key index, or these exact bytes already
		# exist under a different key (see the docstring). Both resolve to "use that row".
		name = frappe.db.get_value(CSO_DOCTYPE, {"s3_key": s3_key, "bucket": bucket}, "name")
		if not name:
			name = find_by_identity(content_sha256, visibility, bucket, for_update=True)
		if not name:
			raise
		_revive_if_needed(name)
		return frappe.get_doc(CSO_DOCTYPE, name)

	return doc


def restamp_derived_visibility(name: str, visibility: str) -> bool:
	"""Re-label a DERIVED object after its source's visibility flipped (A7 choke point).

	`store_thumbnail` stamps a derived object with its source's visibility, because a public
	stamp on the thumbnail of a private file is a false statement about what the object
	holds. A privacy flip would make that statement false again a moment later, so the flip
	comes back here.

	Three things make this safe to do to a derived object and NOT to a primary one, and the
	guards below are those three things:

	1. **A derived key does not encode visibility.** `build_derived_object_key` is
	   `<prefix>/<site>/thm/<src>_<w>x<h>` whatever the label says, so there is nothing to
	   copy and nothing to move — the field is metadata. A primary object's key *is*
	   `pub/`-or-`prv/`, so re-labelling one without moving its bytes would make the row lie
	   about where its bytes are; `derived_of` is therefore required, and a primary object is
	   refused rather than silently skipped.
	2. **`(content_sha256, visibility, bucket)` is a unique index.** Another object may
	   already hold the identity we would move into. The index would reject the write, so it
	   is checked under the lock first and the label is left alone when it is taken — a
	   stale label is a documentation defect, a failed flip is a data defect.
	3. **`unlinked_sibling_count` reads `visibility` without a lock**, on the documented
	   grounds that it is immutable after insert. That documentation is amended where it is
	   written: it holds for primary objects, and for derived objects the read is safe for a
	   different reason than "a stale label only inflates the count" — `is_private` is half of
	   the hash arm's predicate, so a stale label shifts which rows match and could drop one.
	   It is safe because the A1 hash arm resolves an unlinked File row only *through* a
	   sibling that already carries a link, and no File row is ever linked to a derived
	   object; the arm's true contribution to a derived object is zero, so any shift leaves
	   the count at or above the truth — the safe side for something that decides whether
	   bytes die.

	Returns whether the label was changed.
	"""
	if not name:
		return False

	status = lock_cso(name)
	if status is None:
		return False

	row = frappe.db.get_value(
		CSO_DOCTYPE, name, ["derived_of", "visibility", "content_sha256", "bucket"], as_dict=True
	)
	if not row or not row.derived_of:
		# Guard 1: a primary object's visibility lives in its key. Moving bytes between
		# prefixes is `handle_is_private_changed`'s job and goes through `ensure_cso`.
		return False
	if row.visibility == visibility:
		return False

	clash = find_by_identity(row.content_sha256, visibility, row.bucket, for_update=True)
	if clash and clash != name:
		# Guard 2: the frozen identity index is already taken. The bytes are unaffected and
		# still served through the File-side link; only the label stays stale.
		frappe.logger("cloud_file_storage").warning(
			f"derived object {name} keeps visibility {row.visibility}: {clash} already holds "
			f"({row.content_sha256[:12]}…, {visibility})"
		)
		return False

	frappe.db.set_value(CSO_DOCTYPE, name, "visibility", visibility, update_modified=False)
	return True


def _revive_if_needed(name: str):
	"""Bring a scheduled-for-deletion or tombstoned object back (A15/A25).

	`pending_delete` restores the status the object had before it was scheduled — never an
	upgrade. `deleted`/`orphaned` reset to `pending_upload`, so the caller re-uploads the
	bytes and the audit trail (uploaded_at, retry_count, last_error) survives.
	"""
	status = lock_cso(name)
	if status is None or status in PRESENT_STATUSES or status == "pending_upload":
		return

	if status == "pending_delete":
		prior = frappe.db.get_value(CSO_DOCTYPE, name, "status_before_delete") or "pending_upload"
		set_cso_status(name, prior, deletion_scheduled_at=None, status_before_delete=None)
		return

	if status in ("deleted", "orphaned"):
		set_cso_status(name, "pending_upload", deletion_scheduled_at=None, status_before_delete=None)


def set_cso_status(name: str, status: str, **fields):
	"""The only way a Cloud Storage Object changes status.

	Takes the row lock first, refuses transitions that are not on the state machine, and
	stamps the matching timestamp. Extra keyword fields are written in the same statement
	(pass ``None`` to clear a column).
	"""
	if status not in STATUSES:
		raise InvalidStatusTransition(f"{status!r} is not a Cloud Storage Object status")

	current = lock_cso(name)
	if current is None:
		raise frappe.DoesNotExistError(f"Cloud Storage Object {name} does not exist")

	if current != status and status not in _ALLOWED_TRANSITIONS.get(current, set()):
		raise InvalidStatusTransition(f"{current} -> {status} is not an allowed transition")

	values = {"status": status}
	if status == "uploaded" and "uploaded_at" not in fields:
		values["uploaded_at"] = now_datetime()
	if status == "verified" and "verified_at" not in fields:
		values["verified_at"] = now_datetime()
	values.update(fields)

	frappe.db.set_value(CSO_DOCTYPE, name, values, update_modified=True)
	return status


def schedule_deletion(name: str, *, settings=None) -> bool:
	"""Move a dereferenced object to `pending_delete` under the row lock.

	Returns whether the object was scheduled. Refuses when a live reference still exists —
	which is the entire safety property: the caller's "I was the last one" belief is
	re-checked here against a locked read.
	"""
	settings = settings or get_settings()
	status = lock_cso(name)
	if status is None or status in ("deleted", "pending_delete"):
		return False

	if live_reference_count(name):
		refresh_reference_count(name)
		return False

	grace_days = cint(settings.object_delete_grace_days) or 7
	set_cso_status(
		name,
		"pending_delete",
		status_before_delete=status,
		deletion_scheduled_at=add_to_date(now_datetime(), days=grace_days),
		reference_count=0,
	)
	return True


def refresh_reference_count(name: str) -> int:
	"""Best-effort refresh of the denormalized counter (list views only).

	Reads without `FOR UPDATE`: this number is documented as never authoritative, and range-
	locking `tabFile` on every write to keep it fresh would add contention for nothing.
	"""
	count = live_reference_count(name, for_update=False)
	frappe.db.set_value(CSO_DOCTYPE, name, "reference_count", count, update_modified=False)
	return count


def adopt_references(cso_name: str, file_names: list[str] | tuple[str, ...]) -> int:
	"""Point the given File rows at ``cso_name`` and refresh the counter (A7 public API).

	Uses the ORM, not SQL: the only sanctioned raw-SQL write against `tabFile` is the
	guarded VERIFY link UPDATE, which is P5's own audited site (docs/INVARIANTS.md invariant 5).
	Returns the number of rows relinked.
	"""
	if not file_names:
		return 0

	status = lock_cso(cso_name)
	if status is None:
		raise frappe.DoesNotExistError(f"Cloud Storage Object {cso_name} does not exist")

	content_hash = frappe.db.get_value(CSO_DOCTYPE, cso_name, "content_hash_md5")

	relinked = 0
	for file_name in file_names:
		current = frappe.db.get_value(
			"File", file_name, ["cloud_storage_object", "content_hash"], as_dict=True
		)
		if not current:
			continue

		values = {}
		if current.cloud_storage_object != cso_name:
			values["cloud_storage_object"] = cso_name
		# The row now references THESE bytes, so its content_hash has to be theirs. Core's
		# `_delete_file_on_disk` decides between a full delete and thumbnail-only by counting
		# rows that share `content_hash` (file.py:511-526); a sibling left holding the
		# pre-relink hash makes that gate route the deleting row to a full delete while this
		# object is still referenced.
		if content_hash and current.content_hash != content_hash:
			values["content_hash"] = content_hash

		if not values:
			continue
		frappe.db.set_value("File", file_name, values, update_modified=False)
		relinked += 1

	refresh_reference_count(cso_name)
	return relinked


def release_reference(cso_name: str, *, excluding_file: str | None = None, settings=None) -> bool:
	"""Drop one reference: schedule deletion when it was the last, else refresh the counter.

	The caller passes the File row it is about to remove (or has removed) as
	``excluding_file`` so the count is honest either side of the delete.
	"""
	settings = settings or get_settings()
	status = lock_cso(cso_name)
	if status is None:
		return False

	remaining = live_reference_count(cso_name, exclude_file=excluding_file)
	if remaining:
		frappe.db.set_value(CSO_DOCTYPE, cso_name, "reference_count", remaining, update_modified=False)
		return False

	if status in ("deleted", "pending_delete"):
		return False

	grace_days = cint(settings.object_delete_grace_days) or 7
	set_cso_status(
		cso_name,
		"pending_delete",
		status_before_delete=status,
		deletion_scheduled_at=add_to_date(now_datetime(), days=grace_days),
		reference_count=0,
	)
	return True


def record_failure(name: str, error: str, *, status: str = "failed"):
	"""Record a transport/upload failure without losing the retry history."""
	current = lock_cso(name)
	if current is None:
		return
	retry_count = cint(frappe.db.get_value(CSO_DOCTYPE, name, "retry_count")) + 1
	set_cso_status(name, status, last_error=(error or "")[:500], retry_count=retry_count)


def adopt_legacy_cso(
	*,
	s3_key: str,
	file_size: int,
	visibility: str,
	content_hash_md5: str | None = None,
	content_sha256: str | None = None,
	mime_type: str | None = None,
	etag: str | None = None,
	bucket: str | None = None,
	migration_batch: str | None = None,
	settings=None,
):
	"""Get-or-create the object row for bytes that are ALREADY in the bucket (A15/A18).

	Added in P5 because the A7 constructor cannot express an adoption, and the P5 entry gate
	(`tests/test_migration_entry_gate.py`) asserts exactly that rather than describing it:

	* `ensure_cso` derives `s3_key` from the content hash. An adopted object sits at the key
	  the 0.2.x fork chose (`File.s3_object_key`, e.g.
	  `attachments/2021/07/14/Sales Invoice/abcd_report.pdf`), and adoption moves no bytes
	  (migration-engine.md §8) — so a content-addressed key would point at nothing.
	* `ensure_cso` requires a SHA256, and an adopted row does not have one: the only cheap
	  evidence is the ETag, which is the MD5 for single-part uploads and meaningless for
	  multipart ones (A15). Re-reading 100GB to obtain one is what the promotion job is for.

	It lives here, and not in `migration/adoption.py`, so docs/INVARIANTS.md invariant 4 keeps
	holding: every CSO mutation the migration engine makes still goes through this module.
	The status is `legacy_unverified` and there is deliberately no argument to make it
	anything else — A15's honesty rule is structural, not a convention the caller may
	override. Promotion happens through `set_cso_status(name, "verified", …)` once a
	streamed re-GET has produced real hashes.

	Idempotent by `s3_key`, which carries its own unique index, so an interrupted adoption
	batch re-runs safely.
	"""
	settings = settings or get_settings()
	bucket = bucket or settings.bucket
	if not bucket:
		from cloud_file_storage.storage.exceptions import CloudStorageConfigurationError

		raise CloudStorageConfigurationError("Cloud Storage Settings has no bucket configured")

	if not s3_key:
		raise ValueError("adopt_legacy_cso needs the existing object key")

	name = frappe.db.get_value(CSO_DOCTYPE, {"s3_key": s3_key}, "name", for_update=True)
	if name:
		_revive_if_needed(name)
		return frappe.get_doc(CSO_DOCTYPE, name)

	doc = frappe.new_doc(CSO_DOCTYPE)
	doc.update(
		{
			"content_sha256": content_sha256,
			"content_hash_md5": content_hash_md5,
			"file_size": cint(file_size),
			"visibility": visibility,
			"mime_type": mime_type,
			"bucket": bucket,
			"s3_key": s3_key,
			"status": "legacy_unverified",
			"etag": etag,
			"migration_batch": migration_batch,
		}
	)
	frappe.db.savepoint("cfs_adopt_legacy_cso")
	try:
		doc.insert(ignore_permissions=True)
	except UNIQUE_VIOLATIONS:
		frappe.db.rollback(save_point="cfs_adopt_legacy_cso")
		# Either a concurrent adopter won the `s3_key` index, or — when the caller managed
		# to supply a SHA256 — the frozen identity index already holds these bytes under a
		# content-addressed key. Both mean "that row is the object", never "insert anyway".
		name = frappe.db.get_value(CSO_DOCTYPE, {"s3_key": s3_key}, "name")
		if not name and content_sha256:
			name = find_by_identity(content_sha256, visibility, bucket, for_update=True)
		if not name:
			raise
		_revive_if_needed(name)
		return frappe.get_doc(CSO_DOCTYPE, name)

	return doc
