# Copyright (c) 2026, Finstein and contributors
# For license information, please see license.txt

from urllib.parse import urlparse

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from cloud_file_storage.backup import lifecycle
from cloud_file_storage.backup import settings as backup_settings


class CloudBackupSettings(Document):
	def validate(self):
		self.validate_bucket_isolation()
		self.validate_target()
		self.validate_credentials()
		self.validate_endpoint_url()
		self.validate_archive_class()
		self.validate_retention_math()
		self.probe_connection()

	def on_update(self):
		frappe.clear_document_cache(self.doctype)

	def validate_bucket_isolation(self):
		"""The one rule that never bends (B5): backup bucket != attachment bucket."""
		backup_settings.assert_backup_bucket_isolated(self, context="backup")

	def validate_target(self):
		if not cint(self.enabled):
			return
		if not (self.backup_bucket or "").strip():
			frappe.throw(
				_("Set a {0} before enabling cloud backups.").format(frappe.bold(_("Backup Bucket"))),
				title=_("Backup Not Configured"),
			)
		if not backup_settings.expand_prefix(self.backup_prefix):
			frappe.throw(
				_(
					"Set a {0}. An empty prefix would put backup artifacts at the bucket root, "
					"where the generated lifecycle rules cannot be scoped to them."
				).format(frappe.bold(_("Backup Prefix"))),
				title=_("Backup Prefix Required"),
			)

	def validate_credentials(self):
		"""Static keys come in pairs, exactly as on Cloud Storage Settings."""
		if cint(self.use_storage_credentials):
			return

		has_key = bool((self.access_key or "").strip())
		has_secret = bool((self.get_password("secret_key", raise_exception=False) or "").strip())
		if has_key != has_secret:
			frappe.throw(
				_("Set both {0} and {1}, or leave both empty and use the default credential chain.").format(
					frappe.bold(_("Access Key ID")), frappe.bold(_("Secret Access Key"))
				)
			)

	def validate_endpoint_url(self):
		if cint(self.use_storage_credentials):
			return
		endpoint_url = (self.endpoint_url or "").strip()
		if not endpoint_url:
			return

		parsed = urlparse(endpoint_url)
		allowed_schemes = ("https", "http") if frappe.conf.allow_tests else ("https",)
		if parsed.scheme not in allowed_schemes or not parsed.netloc:
			frappe.throw(
				_("Enter a valid HTTPS URL in {0}.").format(frappe.bold(_("Endpoint URL"))),
				title=_("Invalid Endpoint"),
			)
		if parsed.params or parsed.query or parsed.fragment:
			frappe.throw(
				_("{0} must not include query strings or fragments.").format(frappe.bold(_("Endpoint URL")))
			)

	def validate_archive_class(self):
		"""An archive class needs a hot window to transition out of."""
		archive_class = (self.archive_storage_class or "").strip()
		if not archive_class:
			return
		if archive_class not in lifecycle.GLACIER_MIN_DAYS:
			frappe.throw(
				_("{0} is not an archive storage class.").format(frappe.bold(archive_class)),
				title=_("Unknown Storage Class"),
			)
		if cint(self.hot_retention_days) <= 0:
			frappe.throw(
				_(
					"Set {0} to at least 1 day, or clear {1}. A transition on day 0 would archive "
					"an artifact before it can be restored from warm storage."
				).format(frappe.bold(_("Hot Retention Days")), frappe.bold(_("Archive Storage Class"))),
				title=_("Archive Transition Not Configurable"),
			)

	def validate_retention_math(self):
		"""Refuse a policy that pays an archive minimum for storage it then deletes.

		The check is `lifecycle.validate_economics` itself, not a second copy of its
		arithmetic: a validator that disagreed with the preview dialog would let an operator
		save a configuration the apply then refuses.
		"""
		errors = lifecycle.economics_errors(lifecycle.validate_economics(self, object_stats={}))
		if not errors:
			return
		frappe.throw(
			"<br>".join(f"{finding['message']} ({finding['math']})" for finding in errors),
			title=_("Retention Policy Refused"),
			exc=frappe.ValidationError,
		)

	def probe_connection(self):
		"""Prove the backup bucket is reachable before anything depends on it.

		`flags.skip_connection_probe` mirrors Cloud Storage Settings: it exists for tests and
		for an operator configuring a bucket unreachable from this host. The app never sets it.
		"""
		if not cint(self.enabled) or self.flags.skip_connection_probe:
			return
		if not (self.backup_bucket or "").strip():
			return

		try:
			backup_settings.get_backup_client(self).head_bucket(Bucket=self.backup_bucket)
		except Exception as exc:  # noqa: BLE001 - reported to the operator, never swallowed
			frappe.throw(
				_("Cannot reach the backup bucket {0}: {1}").format(
					frappe.bold(self.backup_bucket), str(exc)
				),
				title=_("Backup Bucket Unreachable"),
			)
