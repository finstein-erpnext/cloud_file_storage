# Copyright (c) 2026, Finstein and contributors
# For license information, please see license.txt

from urllib.parse import urlparse

import frappe
from frappe import _
from frappe.model.document import Document

from cloud_file_storage.storage import client as storage_client

# Live objects must be retrievable synchronously; archive classes (GLACIER, DEEP_ARCHIVE,
# GLACIER_IR ...) belong on the backup bucket only. See docs/PLAN.md §A and FD-5.
# Bound to `storage.client`'s tuple rather than restated: three copies of this is three
# chances for the attachment bucket and the lifecycle generator to disagree about which
# classes can serve a download. `test_backup_lifecycle.py` asserts all three agree.
SYNC_RETRIEVAL_STORAGE_CLASSES = storage_client.SYNC_RETRIEVAL_STORAGE_CLASSES

#: Changing any of these invalidates every cached client and, if objects already exist,
#: means the bucket the CSO rows point at is no longer the bucket we would read from.
CONNECTION_FIELDS = ("bucket", "region", "endpoint_url", "addressing_style", "key_prefix")


class CloudStorageSettings(Document):
	def validate(self):
		self.validate_credentials()
		self.validate_endpoint_url()
		self.validate_storage_class()
		self.validate_ignored_doctypes()
		self.validate_bucket_isolation()
		self.validate_restore_window()
		self.warn_on_connection_change_with_existing_objects()
		self.validate_operation_mode()

	def on_update(self):
		from cloud_file_storage.storage.client import clear_client_cache

		clear_client_cache()

	def validate_credentials(self):
		"""Static keys come in pairs; the IAM default chain needs neither."""
		if self.use_default_credential_chain:
			return

		has_access_key_id = bool((self.access_key_id or "").strip())
		has_secret_access_key = bool(
			(self.get_password("secret_access_key", raise_exception=False) or "").strip()
		)
		if has_access_key_id != has_secret_access_key:
			frappe.throw(
				_("Set both {0} and {1}, or leave both empty and use the default credential chain.").format(
					frappe.bold(_("Access Key ID")), frappe.bold(_("Secret Access Key"))
				)
			)

	def validate_endpoint_url(self):
		"""HTTPS-only, except on sites running with `allow_tests` (local MinIO)."""
		endpoint_url = (self.endpoint_url or "").strip()
		if not endpoint_url:
			return

		parsed_endpoint = urlparse(endpoint_url)
		allowed_schemes = ("https", "http") if frappe.conf.allow_tests else ("https",)
		if parsed_endpoint.scheme not in allowed_schemes or not parsed_endpoint.netloc:
			frappe.throw(
				_("Enter a valid HTTPS URL in {0}, for example: {1}.").format(
					frappe.bold(_("Endpoint URL")), frappe.bold("https://nbg1.your-objectstorage.com")
				)
			)

		if parsed_endpoint.params or parsed_endpoint.query or parsed_endpoint.fragment:
			frappe.throw(
				_("{0} must not include query strings or fragments.").format(frappe.bold(_("Endpoint URL")))
			)

	def validate_storage_class(self):
		"""Delegated to the backup domain's guard (design §2.3) so there is one refusal.

		The same function refuses an archive class here and refuses to point a lifecycle
		transition at this bucket; a second implementation would eventually let one of the
		two through.
		"""
		from cloud_file_storage.backup.lifecycle import assert_synchronous_retrieval

		assert_synchronous_retrieval(self.storage_class, _("The live attachment bucket"))

	def validate_ignored_doctypes(self):
		seen = set()
		for row in self.ignored_doctypes or []:
			if not row.doctype_name:
				continue
			if row.doctype_name in seen:
				frappe.throw(
					_("{0} is listed more than once in {1}.").format(
						frappe.bold(row.doctype_name), frappe.bold(_("Ignored DocTypes"))
					)
				)
			seen.add(row.doctype_name)

	def warn_on_connection_change_with_existing_objects(self):
		"""Repointing the bucket orphans every object already recorded — say so."""
		if self.is_new():
			return

		changed = [field for field in CONNECTION_FIELDS if self.has_value_changed(field)]
		if not changed:
			return

		if not frappe.db.table_exists("Cloud Storage Object"):
			return

		existing = frappe.db.count("Cloud Storage Object", {"status": ("!=", "deleted")})
		if not existing:
			return

		frappe.msgprint(
			_(
				"{0} object(s) are already recorded against the previous connection settings. "
				"Changing {1} does not move them: they will stop resolving until the bucket "
				"is repointed back or a migration campaign moves them."
			).format(existing, frappe.bold(", ".join(changed))),
			title=_("Existing Objects Will Not Follow"),
			indicator="orange",
		)

	def validate_bucket_isolation(self):
		"""The backup bucket is never the attachment bucket — checked from THIS side too (M-2).

		`assert_backup_bucket_isolated` was called from every path that edits the backup
		side, and from none that edits this one. So the invariant held while the backup
		bucket moved and silently broke when the attachment bucket moved to match: set
		`bucket` equal to `backup_bucket` and `download_url` will presign arbitrary
		attachment objects with no File permission check and no access log — the serving
		gate bypassed through the backup door.
		"""
		from cloud_file_storage.backup.settings import assert_backup_bucket_isolated

		if not frappe.db.exists("DocType", "Cloud Backup Settings"):
			return
		assert_backup_bucket_isolated(context="the attachment bucket", attachment_bucket=self.bucket)

	def validate_restore_window(self):
		"""A27: the object grace period must cover the restore window it promises."""
		from cloud_file_storage.backup.retention import assert_grace_covers_restore_window

		assert_grace_covers_restore_window(self)

	def validate_operation_mode(self):
		"""Server-validated mode transitions (PLAN §A: never a silent fallback).

		A mode is a promise about where the bytes are. Flipping the label without moving
		them is refused here rather than discovered at the next download.
		"""
		from cloud_file_storage.storage.modes import OperationMode, validate_mode_transition

		previous = frappe.db.get_single_value(self.doctype, "operation_mode") if not self.is_new() else None
		validate_mode_transition(
			previous,
			self.operation_mode,
			force_downgrade=bool(self.flags.force_mode_downgrade),
		)

		if previous == self.operation_mode:
			return
		if self.operation_mode == OperationMode.LOCAL_ONLY.value:
			return
		self.probe_connection()

	def probe_connection(self):
		"""Prove the bucket is reachable before a mode starts depending on it.

		`flags.skip_connection_probe` exists for tests and for an operator configuring a
		bucket that is deliberately unreachable from this host; it is never set by the app.
		"""
		if self.flags.skip_connection_probe:
			return

		from cloud_file_storage.storage import engine

		if not self.bucket:
			frappe.throw(
				_("Configure a bucket before selecting {0}.").format(frappe.bold(self.operation_mode)),
				title=_("Cloud Storage Not Configured"),
			)

		try:
			engine.head_bucket(settings=self)
		except Exception as exc:  # noqa: BLE001 - reported to the operator, not swallowed
			frappe.throw(
				_("Cannot switch to {0}: the bucket is not reachable ({1}).").format(
					frappe.bold(self.operation_mode), str(exc)
				),
				title=_("Mode Transition Blocked"),
			)
