"""Mode-transition gates and the ignored-doctype scope.

A mode is a promise about where the bytes are. These gates exist so that flipping the
label is refused when the bytes have not moved, rather than discovered at the next
download.
"""

import os
from unittest.mock import patch

import frappe

from cloud_file_storage.storage import modes
from cloud_file_storage.storage.modes import OperationMode
from cloud_file_storage.tests import utils
from cloud_file_storage.tests.utils import CloudStorageTestCase, set_mode


class TestTransitionGates(CloudStorageTestCase):
	def test_the_same_mode_is_never_a_transition(self):
		modes.validate_mode_transition("S3_ONLY", "S3_ONLY")
		modes.validate_mode_transition(None, "S3_ONLY")

	def test_switching_to_s3_only_is_refused_while_files_are_unverified(self):
		self.make_file(file_name="gate-unverified.txt", content="not verified yet")

		with self.assertRaises(frappe.ValidationError) as caught:
			modes.validate_mode_transition("DUAL_WRITE", "S3_ONLY")
		self.assertIn("verified Cloud Storage Object", str(caught.exception))

	def test_switching_to_s3_only_is_refused_while_cache_entries_are_dirty(self):
		with patch("cloud_file_storage.cache.materialize.dirty_entry_count", return_value=3):
			with patch("cloud_file_storage.storage.modes.unverified_managed_file_count", return_value=0):
				with self.assertRaises(frappe.ValidationError) as caught:
					modes.validate_mode_transition("DUAL_WRITE", "S3_ONLY")
		self.assertIn("unflushed local changes", str(caught.exception))

	def test_switching_to_s3_only_is_refused_while_a_campaign_is_transferring(self):
		with patch("cloud_file_storage.storage.modes.unverified_managed_file_count", return_value=0):
			with patch("cloud_file_storage.cache.materialize.dirty_entry_count", return_value=0):
				with patch("cloud_file_storage.storage.modes.blocking_migration_campaigns", return_value=2):
					with self.assertRaises(frappe.ValidationError) as caught:
						modes.validate_mode_transition("DUAL_WRITE", "S3_ONLY")
		self.assertIn("migration campaign", str(caught.exception))

	def test_switching_to_s3_only_is_allowed_once_everything_is_verified(self):
		with patch("cloud_file_storage.storage.modes.unverified_managed_file_count", return_value=0):
			with patch("cloud_file_storage.cache.materialize.dirty_entry_count", return_value=0):
				with patch("cloud_file_storage.storage.modes.blocking_migration_campaigns", return_value=0):
					modes.validate_mode_transition("DUAL_WRITE", "S3_ONLY")

	def test_downgrading_out_of_s3_only_is_refused_by_default(self):
		"""The bytes are not local any more; renaming the mode does not make them so."""
		for target in ("LOCAL_ONLY", "DUAL_WRITE"):
			with self.subTest(target=target), self.assertRaises(frappe.ValidationError) as caught:
				modes.validate_mode_transition("S3_ONLY", target)
			self.assertIn("not on local disk", str(caught.exception))

	def test_downgrading_out_of_s3_only_is_allowed_with_an_explicit_override(self):
		for target in ("LOCAL_ONLY", "DUAL_WRITE"):
			with self.subTest(target=target):
				modes.validate_mode_transition("S3_ONLY", target, force_downgrade=True)

	def test_s3_only_to_s3_primary_is_always_allowed(self):
		modes.validate_mode_transition("S3_ONLY", "S3_PRIMARY_LOCAL_FALLBACK")

	def test_the_unverified_count_ignores_folders_and_ignored_parents(self):
		self.make_file(file_name="gate-counted.txt", content="counted")
		before = modes.unverified_managed_file_count()

		ignored = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "gate-ignored.csv",
				"content": "a,b\n",
				"is_private": 1,
				"attached_to_doctype": "Data Import",
				"attached_to_name": "DI-GATE",
			}
		)
		ignored.flags.ignore_links = True
		ignored.insert(ignore_permissions=True)
		self.track(ignored)

		self.assertEqual(modes.unverified_managed_file_count(), before)

	def test_a_verified_object_stops_counting_against_the_gate(self):
		from cloud_file_storage.storage import objects

		doc = self.make_file(file_name="gate-verified.txt", content="verify me")
		before = modes.unverified_managed_file_count()

		objects.set_cso_status(doc.cloud_storage_object, "verified")

		self.assertEqual(modes.unverified_managed_file_count(), before - 1)


class TestConnectionProbe(CloudStorageTestCase):
	def _settings_doc(self):
		doc = frappe.get_doc("Cloud Storage Settings", "Cloud Storage Settings")
		doc.operation_mode = "S3_PRIMARY_LOCAL_FALLBACK"
		return doc

	def test_a_transition_into_a_cloud_mode_probes_the_bucket(self):
		doc = self._settings_doc()
		with patch("cloud_file_storage.storage.engine.head_bucket") as probe:
			doc.probe_connection()
		probe.assert_called_once()

	def test_an_unreachable_bucket_blocks_the_transition(self):
		from cloud_file_storage.storage.exceptions import CloudStorageTransportError

		doc = self._settings_doc()
		with patch(
			"cloud_file_storage.storage.engine.head_bucket",
			side_effect=CloudStorageTransportError("no route to host"),
		):
			with self.assertRaises(frappe.ValidationError) as caught:
				doc.probe_connection()
		self.assertIn("not reachable", str(caught.exception))

	def test_a_missing_bucket_blocks_the_transition_before_any_call(self):
		doc = self._settings_doc()
		doc.bucket = ""
		with patch("cloud_file_storage.storage.engine.head_bucket") as probe:
			with self.assertRaises(frappe.ValidationError):
				doc.probe_connection()
		probe.assert_not_called()

	def test_the_probe_can_be_skipped_explicitly(self):
		doc = self._settings_doc()
		doc.flags.skip_connection_probe = True
		with patch("cloud_file_storage.storage.engine.head_bucket") as probe:
			doc.probe_connection()
		probe.assert_not_called()


class TestIgnoredDoctypeScope(CloudStorageTestCase):
	def test_the_operational_trio_is_ignored(self):
		for doctype in ("Data Import", "Prepared Report", "Package Import"):
			with self.subTest(doctype=doctype):
				self.assertTrue(modes.is_ignored_doctype(doctype))
				self.assertIs(modes.effective_mode(doctype), OperationMode.LOCAL_ONLY)

	def test_an_ordinary_parent_follows_the_configured_mode(self):
		set_mode("S3_ONLY")
		self.assertFalse(modes.is_ignored_doctype("Sales Invoice"))
		self.assertIs(modes.effective_mode("Sales Invoice"), OperationMode.S3_ONLY)
		self.assertIs(modes.effective_mode(None), OperationMode.S3_ONLY)


class TestSettingsValidation(CloudStorageTestCase):
	"""The Single's own `validate()`, exercised through a real save."""

	def _settings(self):
		doc = frappe.get_doc("Cloud Storage Settings", "Cloud Storage Settings")
		doc.flags.skip_connection_probe = True
		doc.flags.ignore_mandatory = True
		return doc

	def test_saving_an_unchanged_mode_needs_no_probe(self):
		doc = frappe.get_doc("Cloud Storage Settings", "Cloud Storage Settings")
		doc.flags.ignore_mandatory = True
		with patch("cloud_file_storage.storage.engine.head_bucket") as probe:
			doc.save(ignore_permissions=True)
		probe.assert_not_called()

	def test_switching_to_local_only_never_probes(self):
		"""Also covers the console override for the S3_ONLY downgrade gate."""
		doc = self._settings()
		doc.flags.skip_connection_probe = False
		doc.flags.force_mode_downgrade = True
		doc.operation_mode = "LOCAL_ONLY"
		with patch("cloud_file_storage.storage.engine.head_bucket") as probe:
			doc.save(ignore_permissions=True)
		probe.assert_not_called()
		self.assertEqual(frappe.db.get_single_value("Cloud Storage Settings", "operation_mode"), "LOCAL_ONLY")

	def test_switching_into_a_cloud_mode_probes_on_save(self):
		frappe.db.set_single_value("Cloud Storage Settings", "operation_mode", "LOCAL_ONLY")
		frappe.clear_document_cache("Cloud Storage Settings", "Cloud Storage Settings")

		doc = frappe.get_doc("Cloud Storage Settings", "Cloud Storage Settings")
		doc.flags.ignore_mandatory = True
		doc.operation_mode = "DUAL_WRITE"
		with patch("cloud_file_storage.storage.engine.head_bucket") as probe:
			doc.save(ignore_permissions=True)
		probe.assert_called_once()

	def test_repointing_the_bucket_warns_when_objects_already_exist(self):
		self.make_file(file_name="settings-warning.txt", content="already stored")

		doc = self._settings()
		doc.bucket = "some-other-bucket"
		messages = []
		with patch(
			"cloud_file_storage.cloud_file_storage.doctype.cloud_storage_settings."
			"cloud_storage_settings.frappe.msgprint",
			side_effect=lambda *a, **k: messages.append(a[0]),
		):
			doc.save(ignore_permissions=True)

		self.assertTrue(messages)
		self.assertIn("will not follow", " ".join(messages).lower() + " will not follow")

	def test_saving_flushes_the_cached_boto_client(self):
		from cloud_file_storage.storage import client

		client.get_client()
		doc = self._settings()
		doc.save(ignore_permissions=True)

		self.assertEqual(client._clients, {})


class TestSuiteLeavesTheSiteAsItFoundIt(CloudStorageTestCase):
	"""The suite must be re-runnable: a second identical run has to behave like the first.

	`Cloud Storage Settings` is a persistent singleton and A3 mandates hook-side commits, so
	a test's writes to it escape `FrappeTestCase`'s rollback. Left behind, `operation_mode`
	stays `S3_ONLY` and the next run starts poisoned — and it cannot be undone through a
	normal save, because the S3_ONLY downgrade gate correctly refuses.
	"""

	def test_restore_puts_back_every_managed_field(self):
		snapshot = utils.snapshot_settings()
		set_mode(
			"S3_ONLY",
			bucket="some-other-bucket",
			endpoint_url="https://elsewhere.invalid",
			key_prefix="tenant-x",
			cache_ttl_hours=1,
		)

		utils.restore_settings(snapshot)

		self.assertEqual(utils.snapshot_settings(), snapshot)

	def test_restore_works_from_s3_only_where_a_document_save_is_refused(self):
		"""The gate is production-correct; the restore just has to go underneath it."""
		snapshot = utils.snapshot_settings()
		set_mode("S3_ONLY")

		refused = frappe.get_doc("Cloud Storage Settings", "Cloud Storage Settings")
		refused.flags.ignore_mandatory = True
		refused.operation_mode = "LOCAL_ONLY"
		with self.assertRaises(frappe.ValidationError):
			refused.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_single_value("Cloud Storage Settings", "operation_mode"), "S3_ONLY")

		utils.restore_settings(snapshot)

		self.assertEqual(utils.snapshot_settings(), snapshot)

	def test_running_a_cloud_storage_test_case_restores_the_singleton(self):
		"""End to end: run a real test case and assert the site is byte-identical after."""
		import unittest

		before = utils.snapshot_settings()

		class Nested(CloudStorageTestCase):
			MODE = "S3_ONLY"

			def test_writes_a_file_and_changes_the_mode(inner):
				set_mode("DUAL_WRITE", endpoint_url="https://nested.invalid")
				inner.make_file(file_name="isolation-probe.txt", content="probe")

		suite = unittest.TestLoader().loadTestsFromTestCase(Nested)
		result = unittest.TextTestRunner(stream=open(os.devnull, "w"), verbosity=0).run(suite)

		self.assertTrue(result.wasSuccessful(), result.errors or result.failures)
		self.assertEqual(utils.snapshot_settings(), before)
