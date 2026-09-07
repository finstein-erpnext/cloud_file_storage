"""Cloud Storage Object: identity, the frozen state machine, dedup and revival (A7/A15/A25)."""

from unittest.mock import patch

import frappe

from cloud_file_storage.cloud_file_storage.doctype.cloud_storage_object.cloud_storage_object import (
	PRESENT_STATUSES,
	SERVABLE_STATUSES,
	STATUSES,
)
from cloud_file_storage.storage import objects
from cloud_file_storage.storage.hashing import digest_bytes
from cloud_file_storage.tests.utils import CSO_DOCTYPE, TEST_BUCKET, CloudStorageTestCase


class CSOTestCase(CloudStorageTestCase):
	def ensure(self, content=b"payload", visibility="private", **kwargs):
		digest = digest_bytes(content)
		return objects.ensure_cso(
			content_sha256=digest.sha256,
			content_hash_md5=digest.md5,
			file_size=digest.size,
			visibility=visibility,
			mime_type="application/octet-stream",
			**kwargs,
		)


class TestFrozenInterface(CSOTestCase):
	"""A7 — statuses, columns and the public API are frozen; P5 imports exactly these."""

	def test_status_set_is_exactly_the_frozen_lowercase_set(self):
		self.assertEqual(
			set(STATUSES),
			{
				"pending_upload",
				"uploaded",
				"verified",
				"failed",
				"orphaned",
				"pending_delete",
				"deleted",
				"legacy_unverified",
			},
		)

	def test_doctype_select_offers_exactly_those_statuses(self):
		options = frappe.get_meta(CSO_DOCTYPE).get_field("status").options.split("\n")
		self.assertEqual([option for option in options if option], list(STATUSES))

	def test_frozen_columns_exist_with_the_frozen_names(self):
		meta = frappe.get_meta(CSO_DOCTYPE)
		for fieldname in ("content_sha256", "content_hash_md5", "file_size", "s3_key"):
			with self.subTest(field=fieldname):
				self.assertIsNotNone(meta.get_field(fieldname))

	def test_byte_counters_are_long_int(self):
		meta = frappe.get_meta(CSO_DOCTYPE)
		self.assertEqual(meta.get_field("file_size").fieldtype, "Long Int")
		self.assertEqual(meta.get_field("reference_count").fieldtype, "Long Int")

	def test_public_api_surface_is_callable(self):
		from cloud_file_storage.storage import engine

		for name in ("ensure_cso", "adopt_references", "set_cso_status"):
			with self.subTest(api=name):
				self.assertTrue(callable(getattr(objects, name)))
		self.assertTrue(callable(engine.verify))

	def test_pending_delete_is_still_servable_but_not_present(self):
		"""A16: an object awaiting GC still serves; A25: it is not 'already uploaded'."""
		self.assertIn("pending_delete", SERVABLE_STATUSES)
		self.assertNotIn("pending_delete", PRESENT_STATUSES)


class TestIdentityAndDedup(CSOTestCase):
	def test_same_bytes_and_visibility_converge_on_one_object(self):
		first = self.ensure(b"same bytes")
		second = self.ensure(b"same bytes")
		self.assertEqual(first.name, second.name)
		self.assertEqual(
			frappe.db.count(CSO_DOCTYPE, {"content_sha256": first.content_sha256, "bucket": TEST_BUCKET}), 1
		)

	def test_visibility_is_part_of_the_identity(self):
		private = self.ensure(b"same bytes", visibility="private")
		public = self.ensure(b"same bytes", visibility="public")
		self.assertNotEqual(private.name, public.name)
		self.assertIn("/prv/", private.s3_key)
		self.assertIn("/pub/", public.s3_key)

	def test_key_is_derived_from_the_content_not_the_caller(self):
		cso = self.ensure(b"identity", source_path="/private/files/whatever.pdf")
		self.assertTrue(cso.s3_key.endswith(cso.content_sha256))
		self.assertNotIn("whatever", cso.s3_key)

	def test_the_unique_identity_index_rejects_a_duplicate_row(self):
		"""The real backstop behind the lock: the DB refuses a second row for one identity."""
		cso = self.ensure(b"unique index")
		duplicate = frappe.new_doc(CSO_DOCTYPE)
		duplicate.update(
			{
				"content_sha256": cso.content_sha256,
				"content_hash_md5": cso.content_hash_md5,
				"file_size": cso.file_size,
				"visibility": cso.visibility,
				"bucket": cso.bucket,
				"s3_key": cso.s3_key + "-different",
				"status": "pending_upload",
			}
		)
		with self.assertRaises(frappe.UniqueValidationError):
			duplicate.insert(ignore_permissions=True)

	def test_the_unique_s3_key_index_rejects_a_second_claim_on_one_object(self):
		cso = self.ensure(b"unique key")
		other = frappe.new_doc(CSO_DOCTYPE)
		other.update(
			{
				"content_sha256": "b" * 64,
				"content_hash_md5": "c" * 32,
				"file_size": 1,
				"visibility": cso.visibility,
				"bucket": cso.bucket,
				"s3_key": cso.s3_key,
				"status": "pending_upload",
			}
		)
		with self.assertRaises(frappe.UniqueValidationError):
			other.insert(ignore_permissions=True)

	def test_a_lost_insert_race_re_reads_the_winner_instead_of_failing(self):
		"""Simulates the residual race the gap lock does not cover.

		`find_by_identity` is made to report "nothing there" for the locking read even
		though the row exists, which is exactly what a concurrent committed insert looks
		like: the insert then hits the unique index and the recovery path must return the
		winner's row rather than propagating DuplicateEntryError to the upload path.
		"""
		winner = self.ensure(b"race payload")
		real_find = objects.find_by_identity
		calls = {"n": 0}

		def flaky_find(*args, **kwargs):
			calls["n"] += 1
			if calls["n"] == 1:
				return None
			return real_find(*args, **kwargs)

		with patch("cloud_file_storage.storage.objects.find_by_identity", side_effect=flaky_find):
			recovered = self.ensure(b"race payload")

		self.assertEqual(recovered.name, winner.name)
		self.assertGreaterEqual(calls["n"], 2)


class TestStateMachine(CSOTestCase):
	def test_a_new_object_starts_pending_upload_and_needs_upload(self):
		cso = self.ensure(b"fresh")
		self.assertEqual(cso.status, "pending_upload")
		self.assertTrue(objects.needs_upload(cso))

	def test_the_upload_gate_is_positive(self):
		"""A25 — upload unless the object is already known present."""
		cso = self.ensure(b"gate")
		for status in ("uploaded", "verified", "legacy_unverified"):
			with self.subTest(status=status):
				frappe.db.set_value(CSO_DOCTYPE, cso.name, "status", status)
				self.assertFalse(objects.needs_upload(frappe.get_doc(CSO_DOCTYPE, cso.name)))

		for status in ("pending_upload", "failed", "orphaned", "pending_delete", "deleted"):
			with self.subTest(status=status):
				frappe.db.set_value(CSO_DOCTYPE, cso.name, "status", status)
				self.assertTrue(objects.needs_upload(frappe.get_doc(CSO_DOCTYPE, cso.name)))

	def test_illegal_transitions_raise_instead_of_fabricating_a_status(self):
		cso = self.ensure(b"transitions")
		with self.assertRaises(objects.InvalidStatusTransition):
			objects.set_cso_status(cso.name, "deleted")
		with self.assertRaises(objects.InvalidStatusTransition):
			objects.set_cso_status(cso.name, "not_a_status")
		self.assertEqual(frappe.db.get_value(CSO_DOCTYPE, cso.name, "status"), "pending_upload")

	def test_timestamps_are_stamped_by_the_transition_not_the_caller(self):
		cso = self.ensure(b"timestamps")
		objects.set_cso_status(cso.name, "uploaded")
		self.assertIsNotNone(frappe.db.get_value(CSO_DOCTYPE, cso.name, "uploaded_at"))
		objects.set_cso_status(cso.name, "verified")
		self.assertIsNotNone(frappe.db.get_value(CSO_DOCTYPE, cso.name, "verified_at"))

	def test_record_failure_keeps_the_retry_history(self):
		cso = self.ensure(b"failures")
		objects.record_failure(cso.name, "boom")
		objects.record_failure(cso.name, "boom again")
		row = frappe.db.get_value(
			CSO_DOCTYPE, cso.name, ["status", "retry_count", "last_error"], as_dict=True
		)
		self.assertEqual(row.status, "failed")
		self.assertEqual(row.retry_count, 2)
		self.assertEqual(row.last_error, "boom again")

	def test_legacy_unverified_may_exist_without_hashes_but_nothing_else_may(self):
		"""A15 — adoption is honest: a row without a hash can never claim to be verified."""
		adopted = frappe.new_doc(CSO_DOCTYPE)
		adopted.update(
			{
				"visibility": "private",
				"bucket": TEST_BUCKET,
				"s3_key": "legacy/adopted/key",
				"status": "legacy_unverified",
				"file_size": 10,
			}
		)
		adopted.insert(ignore_permissions=True)
		self.assertTrue(adopted.name)

		adopted.status = "verified"
		with self.assertRaises(frappe.ValidationError):
			adopted.save(ignore_permissions=True)


class TestRevival(CSOTestCase):
	def test_a_tombstone_revives_to_pending_upload(self):
		"""A25 — delete, GC, then re-upload identical bytes must work."""
		cso = self.ensure(b"tombstone")
		objects.set_cso_status(cso.name, "uploaded")
		objects.set_cso_status(cso.name, "pending_delete", status_before_delete="uploaded")
		objects.set_cso_status(cso.name, "deleted")

		revived = self.ensure(b"tombstone")

		self.assertEqual(revived.name, cso.name)
		self.assertEqual(revived.status, "pending_upload")
		self.assertTrue(objects.needs_upload(revived))
		self.assertIsNone(revived.deletion_scheduled_at)

	def test_a_pending_delete_object_restores_its_prior_status_never_an_upgrade(self):
		"""A15 — resurrection restores, it does not promote."""
		for prior in ("uploaded", "legacy_unverified"):
			with self.subTest(prior=prior):
				content = f"resurrect-{prior}".encode()
				cso = self.ensure(content)
				objects.set_cso_status(cso.name, prior)
				objects.set_cso_status(cso.name, "pending_delete", status_before_delete=prior)

				revived = self.ensure(content)

				self.assertEqual(revived.name, cso.name)
				self.assertEqual(revived.status, prior)
				self.assertIsNone(revived.deletion_scheduled_at)

	def test_an_orphaned_object_is_re_uploaded_rather_than_trusted(self):
		cso = self.ensure(b"orphan")
		objects.set_cso_status(cso.name, "orphaned")
		revived = self.ensure(b"orphan")
		self.assertEqual(revived.status, "pending_upload")

	def test_revival_keeps_the_audit_trail(self):
		cso = self.ensure(b"audit")
		objects.record_failure(cso.name, "an earlier failure")
		objects.set_cso_status(cso.name, "pending_delete", status_before_delete="failed")
		objects.set_cso_status(cso.name, "deleted")

		revived = self.ensure(b"audit")
		self.assertEqual(revived.retry_count, 1)
		self.assertEqual(revived.last_error, "an earlier failure")


class TestReferences(CSOTestCase):
	def _file(self, name, content, cso=None):
		doc = self.make_file(file_name=name, content=content, is_private=1)
		if cso:
			frappe.db.set_value("File", doc.name, "cloud_storage_object", cso.name, update_modified=False)
		return doc

	def test_adopt_references_relinks_and_refreshes_the_counter(self):
		cso = self.ensure(b"adopt")
		first = self._file("adopt-1.txt", "adopt one")
		second = self._file("adopt-2.txt", "adopt two")

		relinked = objects.adopt_references(cso.name, [first.name, second.name])

		self.assertEqual(relinked, 2)
		self.assertEqual(frappe.db.get_value("File", first.name, "cloud_storage_object"), cso.name)
		self.assertEqual(frappe.db.get_value(CSO_DOCTYPE, cso.name, "reference_count"), 2)

	def test_adopt_references_is_idempotent(self):
		cso = self.ensure(b"idempotent adopt")
		doc = self._file("adopt-3.txt", "adopt three")
		objects.adopt_references(cso.name, [doc.name])
		self.assertEqual(objects.adopt_references(cso.name, [doc.name]), 0)

	def test_releasing_a_reference_that_is_not_the_last_does_not_schedule_deletion(self):
		cso = self.ensure(b"shared")
		objects.set_cso_status(cso.name, "uploaded")
		first = self._file("shared-1.txt", "shared one", cso)
		self._file("shared-2.txt", "shared two", cso)

		scheduled = objects.release_reference(cso.name, excluding_file=first.name)

		self.assertFalse(scheduled)
		self.assertEqual(frappe.db.get_value(CSO_DOCTYPE, cso.name, "status"), "uploaded")

	def test_releasing_the_last_reference_schedules_and_remembers_the_prior_status(self):
		cso = self.ensure(b"last ref")
		objects.set_cso_status(cso.name, "uploaded")
		objects.set_cso_status(cso.name, "verified")
		only = self._file("last-1.txt", "last one", cso)

		scheduled = objects.release_reference(cso.name, excluding_file=only.name)

		self.assertTrue(scheduled)
		row = frappe.db.get_value(
			CSO_DOCTYPE, cso.name, ["status", "status_before_delete", "deletion_scheduled_at"], as_dict=True
		)
		self.assertEqual(row.status, "pending_delete")
		self.assertEqual(row.status_before_delete, "verified")
		self.assertIsNotNone(row.deletion_scheduled_at)

	def test_migration_owned_objects_count_as_referenced(self):
		"""A14 — a campaign that is still running owns its objects."""
		cso = self.ensure(b"campaign owned")
		objects.set_cso_status(cso.name, "uploaded")

		with patch("cloud_file_storage.storage.objects.migration_reference_count", return_value=1):
			self.assertEqual(objects.live_reference_count(cso.name), 1)
			self.assertFalse(objects.release_reference(cso.name))

		self.assertEqual(frappe.db.get_value(CSO_DOCTYPE, cso.name, "status"), "uploaded")

	def test_migration_reference_count_is_zero_before_p5_ships_the_doctype(self):
		cso = self.ensure(b"no migration engine yet")
		self.assertEqual(objects.migration_reference_count(cso.name), 0)
