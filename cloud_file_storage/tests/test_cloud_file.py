"""`CloudFile` — the A1 resolution chain and the read-side overrides.

A1 exists because core creates File rows from a `file_url` alone and then calls
`get_content()` on them before any `cloud_storage_object` link is written. Every link in
the chain is exercised here in isolation, and then through the real core flows that need
it (amend/copy, `attach_files_to_document`, `file_manager.save_file`).
"""

import os

import frappe

from cloud_file_storage.cache import materialize
from cloud_file_storage.overrides.file import stash_legacy_cso
from cloud_file_storage.storage import objects
from cloud_file_storage.storage.exceptions import CloudObjectNotFound
from cloud_file_storage.tests.utils import CSO_DOCTYPE, CloudStorageTestCase, set_mode


class TestResolutionChain(CloudStorageTestCase):
	def test_link_1_own_cloud_storage_object(self):
		doc = self.make_file(file_name="chain-own.txt", content="own link")
		reloaded = frappe.get_doc("File", doc.name)

		self.assertEqual(reloaded._resolve_cso().name, doc.cloud_storage_object)

	def test_link_2_a_sibling_sharing_the_file_url(self):
		"""Amend/copy inserts a row with the same `file_url` and no link of its own."""
		source = self.make_file(file_name="chain-url.txt", content="url sibling")

		unlinked = frappe.new_doc("File")
		unlinked.update({"file_name": "chain-url.txt", "file_url": source.file_url, "is_private": 1})

		self.assertIsNone(unlinked.cloud_storage_object)
		self.assertEqual(unlinked._resolve_cso().name, source.cloud_storage_object)

	def test_link_3_a_sibling_sharing_content_hash_and_visibility(self):
		source = self.make_file(file_name="chain-hash.txt", content="hash sibling")

		unlinked = frappe.new_doc("File")
		unlinked.update(
			{
				"file_name": "chain-hash-other.txt",
				"file_url": "/private/files/chain-hash-other.txt",
				"content_hash": source.content_hash,
				"is_private": 1,
			}
		)

		self.assertEqual(unlinked._resolve_cso().name, source.cloud_storage_object)

	def test_link_3_does_not_cross_the_privacy_boundary(self):
		"""Visibility is part of the object identity; a public twin is a different object."""
		source = self.make_file(file_name="chain-private.txt", content="privacy matters", is_private=1)

		public_candidate = frappe.new_doc("File")
		public_candidate.update(
			{
				"file_name": "chain-public.txt",
				"file_url": "/files/chain-public.txt",
				"content_hash": source.content_hash,
				"is_private": 0,
			}
		)

		self.assertIsNone(public_candidate._resolve_cso())

	def test_link_4_the_legacy_write_stash(self):
		"""`file_manager.save_file` creates the object before the row exists."""
		cso = objects.ensure_cso(
			content_sha256="a" * 64,
			content_hash_md5="b" * 32,
			file_size=3,
			visibility="private",
		)
		stash_legacy_cso("b" * 32, 1, cso.name)

		candidate = frappe.new_doc("File")
		candidate.update(
			{
				"file_name": "chain-stash.txt",
				"file_url": "/private/files/chain-stash.txt",
				"content_hash": "b" * 32,
				"is_private": 1,
			}
		)

		self.assertEqual(candidate._resolve_cso().name, cso.name)

	def test_the_chain_resolves_to_nothing_rather_than_guessing(self):
		candidate = frappe.new_doc("File")
		candidate.update(
			{
				"file_name": "chain-none.txt",
				"file_url": "/private/files/chain-none.txt",
				"content_hash": "f" * 32,
				"is_private": 1,
			}
		)
		self.assertIsNone(candidate._resolve_cso())
		self.assertFalse(candidate._is_cloud_backed())

	def test_a_folder_never_resolves(self):
		folder = frappe.new_doc("File")
		folder.update({"file_name": "a folder", "is_folder": 1})
		self.assertIsNone(folder._resolve_cso())

	def test_every_read_side_method_uses_the_chain(self):
		"""A1 names these five explicitly."""
		source = self.make_file(file_name="chain-methods.txt", content="chain users")

		unlinked = frappe.new_doc("File")
		unlinked.update({"file_name": "chain-methods.txt", "file_url": source.file_url, "is_private": 1})

		self.assertTrue(unlinked._is_cloud_backed())
		self.assertEqual(unlinked._read_bytes(), b"chain users")
		self.assertTrue(unlinked.exists_on_disk())
		self.assertTrue(unlinked.validate_file_on_disk())
		self.assertIn("cloud_storage_cache", unlinked.get_full_path())


class TestUrlOnlyFileCreation(CloudStorageTestCase):
	"""A1 — the three core sites that create a File from a `file_url` alone, in S3_ONLY."""

	def test_amend_style_copy_reads_the_bytes_back(self):
		"""`document.py:446-464` re-reads content on amend and re-saves it."""
		source = self.make_file(file_name="urlonly-amend.txt", content="amend payload")

		copy = frappe.new_doc("File")
		copy.update(
			{
				"file_name": "urlonly-amend.txt",
				"file_url": source.file_url,
				"is_private": 1,
			}
		)
		copy.save_file(content=copy.get_content())
		copy.flags.ignore_duplicate_entry_error = True
		copy.insert(ignore_permissions=True)
		self.track(copy)

		self.assertEqual(frappe.get_doc("File", copy.name).get_content(), "amend payload")
		self.assertTrue(frappe.db.get_value("File", copy.name, "cloud_storage_object"))

	def test_attach_files_to_document_style_row_adopts_the_object(self):
		"""`file/utils.py:363-374` creates a row from a URL, then `after_insert` links it."""
		source = self.make_file(file_name="urlonly-attach.txt", content="attach payload")

		attached = frappe.get_doc(
			{
				"doctype": "File",
				"file_url": source.file_url,
				"file_name": "urlonly-attach.txt",
				"is_private": 1,
				"content_hash": source.content_hash,
			}
		)
		attached.flags.ignore_duplicate_entry_error = True
		attached.insert(ignore_permissions=True)
		self.track(attached)

		self.assertEqual(
			frappe.db.get_value("File", attached.name, "cloud_storage_object"),
			source.cloud_storage_object,
		)

	def test_file_manager_save_file_row_is_linked_after_insert(self):
		from frappe.utils.file_manager import save_file

		doc = save_file("urlonly-manager.txt", b"manager payload", None, None, is_private=1)
		self.track(doc)

		self.assertTrue(frappe.db.get_value("File", doc.name, "cloud_storage_object"))
		self.assertEqual(frappe.get_doc("File", doc.name).get_content(), "manager payload")


class TestReadPaths(CloudStorageTestCase):
	def test_a_local_copy_is_preferred_over_the_network(self):
		set_mode("DUAL_WRITE")
		doc = self.make_file(file_name="read-local.txt", content="local first")
		reloaded = frappe.get_doc("File", doc.name)

		from cloud_file_storage.storage.exceptions import CloudStorageTransportError

		self.store.fail_with = CloudStorageTransportError("network must not be used")
		try:
			self.assertEqual(reloaded.get_content(), "local first")
			self.assertEqual(reloaded.get_full_path(), self.local_path(reloaded))
		finally:
			self.store.fail_with = None

	def test_a_pending_upload_object_is_not_treated_as_readable(self):
		doc = self.make_file(file_name="read-pending.txt", content="not yet uploaded")
		frappe.db.set_value(CSO_DOCTYPE, doc.cloud_storage_object, "status", "pending_upload")

		with self.assertRaises(CloudObjectNotFound):
			frappe.get_doc("File", doc.name).get_content()

	def test_an_object_awaiting_gc_is_still_readable(self):
		"""A16 — `pending_delete` stays on the serving allow-list."""
		doc = self.make_file(file_name="read-pending-delete.txt", content="still servable")
		objects.set_cso_status(doc.cloud_storage_object, "pending_delete", status_before_delete="uploaded")

		self.assertEqual(frappe.get_doc("File", doc.name).get_content(), "still servable")

	def test_materialization_verifies_the_bytes_before_serving_them(self):
		doc = self.make_file(file_name="read-corrupt.txt", content="honest bytes")
		cso = self.cso_of(doc)
		self.store.objects[cso.s3_key] = b"tampered bytes of a different length"

		from cloud_file_storage.storage.exceptions import CloudStorageIntegrityError

		with self.assertRaises(CloudStorageIntegrityError):
			frappe.get_doc("File", doc.name).get_full_path()

	def test_a_second_materialize_reuses_the_cache_entry(self):
		doc = self.make_file(file_name="read-cached.txt", content="cache me")
		reloaded = frappe.get_doc("File", doc.name)

		first = reloaded.get_full_path()
		downloads_before = len(self.store.objects)
		self.store.fail_with = CloudObjectNotFound("must not download again")
		try:
			second = frappe.get_doc("File", doc.name).get_full_path()
		finally:
			self.store.fail_with = None

		self.assertEqual(first, second)
		self.assertEqual(len(self.store.objects), downloads_before)


class TestWriteBackTracking(CloudStorageTestCase):
	"""A4 — EVERY path handed out is tracked, with mtime and size recorded at hand-out."""

	def test_a_cache_path_is_tracked(self):
		doc = self.make_file(file_name="track-cache.txt", content="tracked")
		path = frappe.get_doc("File", doc.name).get_full_path()

		tracked = materialize.tracked_paths()
		self.assertIn(path, tracked)
		self.assertTrue(tracked[path]["is_cache_entry"])
		self.assertIsNotNone(tracked[path]["mtime"])
		self.assertEqual(tracked[path]["size"], len("tracked"))

	def test_a_canonical_local_path_is_tracked_too(self):
		"""The A4 clause that is easy to miss: a caller can write to the canonical path."""
		set_mode("DUAL_WRITE")
		doc = self.make_file(file_name="track-canonical.txt", content="canonical tracked")
		materialize.clear_tracking()

		path = frappe.get_doc("File", doc.name).get_full_path()

		self.assertEqual(path, self.local_path(doc))
		tracked = materialize.tracked_paths()
		self.assertIn(path, tracked)
		self.assertFalse(tracked[path]["is_cache_entry"])
		self.assertEqual(tracked[path]["size"], len("canonical tracked"))

	def test_nothing_is_tracked_for_a_file_with_no_object(self):
		set_mode("LOCAL_ONLY")
		doc = self.make_file(file_name="track-none.txt", content="plain local")
		materialize.clear_tracking()

		frappe.get_doc("File", doc.name).get_full_path()

		self.assertEqual(materialize.tracked_paths(), {})


class TestPrivacyFlipDetails(CloudStorageTestCase):
	def test_the_local_copy_moves_when_there_is_one(self):
		set_mode("DUAL_WRITE")
		doc = self.make_file(file_name="flip-local.txt", content="flip with local", is_private=1)
		private_path = self.local_path(doc)
		self.assertTrue(os.path.exists(private_path))

		reloaded = frappe.get_doc("File", doc.name)
		reloaded.is_private = 0
		reloaded.save(ignore_permissions=True)

		self.assertFalse(os.path.exists(private_path))
		self.assertTrue(os.path.exists(frappe.get_site_path("public", "files", "flip-local.txt")))

	def test_flipping_back_and_forth_converges_on_the_original_object(self):
		doc = self.make_file(file_name="flip-round-trip.txt", content="round trip", is_private=1)
		original_cso = doc.cloud_storage_object

		first = frappe.get_doc("File", doc.name)
		first.is_private = 0
		first.save(ignore_permissions=True)

		second = frappe.get_doc("File", doc.name)
		second.is_private = 1
		second.save(ignore_permissions=True)

		self.assertEqual(frappe.db.get_value("File", doc.name, "cloud_storage_object"), original_cso)
		self.assertEqual(frappe.db.get_value(CSO_DOCTYPE, original_cso, "status"), "uploaded")

	def test_a_flip_with_no_object_falls_through_to_core(self):
		set_mode("LOCAL_ONLY")
		doc = self.make_file(file_name="flip-core.txt", content="core behaviour", is_private=1)

		reloaded = frappe.get_doc("File", doc.name)
		reloaded.is_private = 0
		reloaded.save(ignore_permissions=True)

		self.assertTrue(reloaded.file_url.startswith("/files/"))
		self.assertTrue(os.path.exists(frappe.get_site_path("public", "files", "flip-core.txt")))


class TestRollbackBehaviour(CloudStorageTestCase):
	def test_a_cloud_only_row_does_not_try_to_restore_bytes_on_disk(self):
		"""Objects are immutable and content-addressed; there is nothing to put back."""
		doc = self.make_file(file_name="rollback-cloud.txt", content="v1")
		reloaded = frappe.get_doc("File", doc.name)
		reloaded.flags.original_content = b"v0"

		reloaded.on_rollback()

		self.assertFalse(os.path.exists(self.local_path(reloaded)))
		self.assertNotIn("original_content", reloaded.flags)

	def test_a_dual_write_row_still_gets_core_disk_restore(self):
		set_mode("DUAL_WRITE")
		doc = self.make_file(file_name="rollback-local.txt", content="v1")
		reloaded = frappe.get_doc("File", doc.name)
		reloaded.flags.original_content = b"v0"

		reloaded.on_rollback()

		with open(self.local_path(reloaded), "rb") as handle:
			self.assertEqual(handle.read(), b"v0")


class TestPrivacyFlipDoesNotCaptureHashlessRows(CloudStorageTestCase):
	"""A NULL `content_hash` must never be treated as "matches every hashless row".

	`frappe.get_all(filters={"content_hash": None})` compiles to `content_hash IS NULL`
	(`frappe/database/query.py:216-217`) and returns every hashless File on the site. Core's
	own `update_existing_file_docs` renders `content_hash = NULL`, which matches nothing —
	so those are exactly the rows core leaves alone. Reachable today: the sanctioned
	`patches/v0_2_0/backfill_s3_object_key` nulls the column on fork-era rows.
	"""

	def _hashless_cloud_file(self, name, content):
		doc = self.make_file(file_name=name, content=content)
		frappe.db.set_value("File", doc.name, "content_hash", None, update_modified=False)
		return frappe.get_doc("File", doc.name)

	def test_flipping_a_hashless_row_does_not_relink_other_hashless_rows(self):
		flipped = self._hashless_cloud_file("hashless-flip.txt", "flip me")
		bystander = self._hashless_cloud_file("hashless-bystander.txt", "leave me alone")
		bystander_cso = bystander.cloud_storage_object
		self.assertIsNone(frappe.db.get_value("File", bystander.name, "content_hash"))

		flipped.is_private = 0
		flipped.save(ignore_permissions=True)

		self.assertEqual(
			frappe.db.get_value("File", bystander.name, "cloud_storage_object"),
			bystander_cso,
			"an unrelated hashless row was repointed at the flipped file's object",
		)

	def test_the_bystander_still_serves_its_own_bytes(self):
		"""The consequence that matters: silent content substitution."""
		flipped = self._hashless_cloud_file("hashless-sub-a.txt", "bytes of A")
		bystander = self._hashless_cloud_file("hashless-sub-b.txt", "bytes of B")

		flipped.is_private = 0
		flipped.save(ignore_permissions=True)

		self.assertEqual(frappe.get_doc("File", bystander.name).get_content(), "bytes of B")

	def test_a_hashed_row_still_relinks_its_real_siblings(self):
		"""The guard must not disable the A16 relink it is protecting."""
		doc = self.make_file(file_name="hashed-flip.txt", content="shared bytes for the flip")
		sibling = self.make_file(
			file_name="hashed-flip-twin.txt",
			content="shared bytes for the flip",
			ignore_duplicate_entry_error=1,
		)
		old_cso = frappe.db.get_value("File", doc.name, "cloud_storage_object")
		self.assertEqual(frappe.db.get_value("File", sibling.name, "cloud_storage_object"), old_cso)

		reloaded = frappe.get_doc("File", doc.name)
		reloaded.is_private = 0
		reloaded.save(ignore_permissions=True)

		new_cso = frappe.db.get_value("File", doc.name, "cloud_storage_object")
		self.assertNotEqual(new_cso, old_cso)
		self.assertEqual(frappe.db.get_value("File", sibling.name, "cloud_storage_object"), new_cso)
