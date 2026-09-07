"""Operation modes and the settings accessor every other module reads through.

All four modes are implemented at the storage layer (PLAN.md §B, P2 row). A mode never
degrades silently: when S3 is unavailable the outcome is either a durable local write with
a queryable `pending_upload` Cloud Storage Object, or a typed error — never "we quietly
wrote it somewhere else and told you it worked".
"""

from enum import Enum

import frappe
from frappe.query_builder.functions import Count
from frappe.utils import cint

SETTINGS_DOCTYPE = "Cloud Storage Settings"


class OperationMode(str, Enum):
	LOCAL_ONLY = "LOCAL_ONLY"
	DUAL_WRITE = "DUAL_WRITE"
	S3_PRIMARY_LOCAL_FALLBACK = "S3_PRIMARY_LOCAL_FALLBACK"
	S3_ONLY = "S3_ONLY"

	@property
	def uses_cloud(self) -> bool:
		return self is not OperationMode.LOCAL_ONLY

	@property
	def keeps_local_copy(self) -> bool:
		"""Whether a successful write also leaves the canonical local file in place."""
		return self in (OperationMode.LOCAL_ONLY, OperationMode.DUAL_WRITE)

	@property
	def allows_local_fallback(self) -> bool:
		"""Whether an S3 failure may be absorbed by writing locally instead."""
		return self in (
			OperationMode.LOCAL_ONLY,
			OperationMode.DUAL_WRITE,
			OperationMode.S3_PRIMARY_LOCAL_FALLBACK,
		)


def get_settings():
	"""The Cloud Storage Settings single.

	Every module reads settings through this one function so a test can patch a single
	symbol and so the cached-doc invalidation story stays in one place.
	"""
	return frappe.get_cached_doc(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE)


def get_mode(settings=None) -> OperationMode:
	settings = settings or get_settings()
	try:
		return OperationMode(settings.operation_mode or OperationMode.LOCAL_ONLY.value)
	except ValueError:
		# An unknown value must not silently enable cloud writes.
		frappe.logger("cloud_file_storage").error(
			f"unknown operation_mode {settings.operation_mode!r}; falling back to LOCAL_ONLY"
		)
		return OperationMode.LOCAL_ONLY


def ignored_doctypes(settings=None) -> set[str]:
	settings = settings or get_settings()
	return {row.doctype_name for row in (settings.ignored_doctypes or []) if row.doctype_name}


def is_ignored_doctype(parent_doctype: str | None, settings=None) -> bool:
	"""LOCAL_OPERATIONAL parents (Data Import / Prepared Report / Package Import, ADR 0022).

	Their attachments are short-lived operational bytes consumed through local paths; they
	are never claimed as migrated cloud attachments.
	"""
	if not parent_doctype:
		return False
	return parent_doctype in ignored_doctypes(settings)


def effective_mode(parent_doctype: str | None = None, settings=None) -> OperationMode:
	"""The mode that applies to one File: ignored parents behave as LOCAL_ONLY."""
	settings = settings or get_settings()
	if is_ignored_doctype(parent_doctype, settings):
		return OperationMode.LOCAL_ONLY
	return get_mode(settings)


def fail_insert_on_s3_error(settings=None) -> bool:
	settings = settings or get_settings()
	return bool(cint(settings.fail_insert_on_s3_error))


# --- mode transition gates (server-validated, called from CloudStorageSettings.validate) ---

#: Downgrades out of S3_ONLY that need bytes back on local disk first.
_LOCAL_BYTE_MODES = (OperationMode.LOCAL_ONLY, OperationMode.DUAL_WRITE)


def unverified_managed_file_count() -> int:
	"""Managed Files whose bytes are not provably in the bucket yet.

	"Managed" = a real, non-folder File with a canonical URL whose parent doctype is not on
	the ignored list. Counted with the query builder (never raw SQL — docs/INVARIANTS.md invariant 5).
	"""
	file = frappe.qb.DocType("File")
	cso = frappe.qb.DocType("Cloud Storage Object")

	ignored = ignored_doctypes()
	query = (
		frappe.qb.from_(file)
		.left_join(cso)
		.on(file.cloud_storage_object == cso.name)
		.select(Count(file.name))
		.where(file.is_folder == 0)
		.where(file.file_url.like("/files/%") | file.file_url.like("/private/files/%"))
		.where(cso.name.isnull() | (cso.status != "verified"))
	)
	if ignored:
		query = query.where(file.attached_to_doctype.notin(list(ignored)) | file.attached_to_doctype.isnull())

	result = query.run()
	return cint(result[0][0]) if result else 0


def blocking_migration_campaigns() -> int:
	"""Campaigns still moving bytes. Zero until P5 ships the doctype."""
	if not frappe.db.table_exists("Cloud Migration Campaign"):
		return 0
	return frappe.db.count("Cloud Migration Campaign", {"status": ("in", ("Running", "Cleanup Running"))})


def validate_mode_transition(old_mode: str | None, new_mode: str, *, force_downgrade: bool = False):
	"""Raise a user-facing error when a mode change would strand bytes.

	Failures here are explicit and queryable — the whole point of A30/PLAN §A is that a
	mode is a promise about where bytes are, so flipping the label without moving them is
	refused.
	"""
	from frappe import _

	from cloud_file_storage.cache import materialize

	if not old_mode or old_mode == new_mode:
		return

	try:
		previous = OperationMode(old_mode)
		target = OperationMode(new_mode)
	except ValueError:
		return

	if target is OperationMode.S3_ONLY:
		unverified = unverified_managed_file_count()
		if unverified:
			frappe.throw(
				_(
					"Cannot switch to S3_ONLY: {0} managed File(s) do not have a verified "
					"Cloud Storage Object yet. Run a migration campaign to completion first."
				).format(unverified),
				title=_("Mode Transition Blocked"),
			)

		dirty = materialize.dirty_entry_count()
		if dirty:
			frappe.throw(
				_(
					"Cannot switch to S3_ONLY: {0} materialization cache entries still hold "
					"unflushed local changes."
				).format(dirty),
				title=_("Mode Transition Blocked"),
			)

		running = blocking_migration_campaigns()
		if running:
			frappe.throw(
				_("Cannot switch to S3_ONLY while {0} migration campaign(s) are still transferring.").format(
					running
				),
				title=_("Mode Transition Blocked"),
			)

	if previous is OperationMode.S3_ONLY and target in _LOCAL_BYTE_MODES and not force_downgrade:
		frappe.throw(
			_(
				"Cannot switch from S3_ONLY to {0}: the bytes are not on local disk any more. "
				"Run a reverse-materialization campaign, or set "
				"<code>flags.force_mode_downgrade</code> from the console if you know the "
				"local copies are present."
			).format(target.value),
			title=_("Mode Transition Blocked"),
		)
