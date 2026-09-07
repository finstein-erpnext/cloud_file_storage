"""The write path: dual-convention `write_file`, the four modes, and A2 persistence."""

import os
from unittest.mock import patch

import frappe
from frappe.utils.file_manager import save_file as file_manager_save_file

from cloud_file_storage import core_hooks
from cloud_file_storage.storage import breaker, objects
from cloud_file_storage.storage.exceptions import (
	CloudStorageTransportError,
	CloudStorageUnavailableError,
)
from cloud_file_storage.storage.hashing import digest_bytes
from cloud_file_storage.tests.utils import CSO_DOCTYPE, CloudStorageTestCase, set_mode


class TestWriteFileDispatch(CloudStorageTestCase):
	"""Contract #12 — both live call conventions."""

	def test_a_file_document_takes_the_document_convention(self):
		with patch("cloud_file_storage.core_hooks._write_file_doc", return_value={}) as doc_path:
			with patch("cloud_file_storage.core_hooks._write_file_legacy") as legacy_path:
				core_hooks.write_file(frappe._dict(doctype="File"))
		doc_path.assert_called_once()
		legacy_path.assert_not_called()

	def test_the_file_manager_signature_takes_the_legacy_convention(self):
		with patch("cloud_file_storage.core_hooks._write_file_doc") as doc_path:
			with patch("cloud_file_storage.core_hooks._write_file_legacy", return_value={}) as legacy_path:
				core_hooks.write_file("x.txt", b"bytes", content_type="text/plain", is_private=1)
		doc_path.assert_not_called()
		legacy_path.assert_called_once_with("x.txt", b"bytes", content_type="text/plain", is_private=1)

	def test_the_legacy_convention_returns_the_keys_core_filters_for(self):
		result = core_hooks.write_file("legacy.txt", b"legacy bytes", content_type="text/plain", is_private=1)
		for key in frappe.get_hooks()["write_file_keys"]:
			with self.subTest(key=key):
				self.assertIn(key, result)

	def test_the_legacy_convention_links_the_object_after_the_row_is_inserted(self):
		"""The e-Waybill / supplier-invoice shape: `file_manager.save_file`, then insert."""
		doc = file_manager_save_file("ewaybill.json", b'{"irn": "x"}', None, None, is_private=1)
		self.track(doc)

		self.assertTrue(doc.file_url.startswith("/private/files/"))
		linked = frappe.db.get_value("File", doc.name, "cloud_storage_object")
		self.assertTrue(linked, "the stashed object was not adopted by file_after_insert")
		self.assertTrue(self.store.contains(frappe.get_doc(CSO_DOCTYPE, linked)))


class TestCanonicalUrlAndHash(CloudStorageTestCase):
	def test_file_url_is_canonical_in_every_cloud_mode(self):
		for mode in ("DUAL_WRITE", "S3_PRIMARY_LOCAL_FALLBACK", "S3_ONLY"):
			with self.subTest(mode=mode):
				set_mode(mode)
				doc = self.make_file(file_name=f"canonical-{mode}.txt", content=f"bytes {mode}")
				self.assertTrue(doc.file_url.startswith("/private/files/"), doc.file_url)
				self.assertNotIn("http", doc.file_url)
				self.assertNotIn("/api/method/", doc.file_url)
				self.assertEqual(frappe.db.get_value("File", doc.name, "file_url"), doc.file_url)

	def test_public_files_get_the_public_canonical_prefix(self):
		doc = self.make_file(file_name="canonical-public.txt", content="public bytes", is_private=0)
		self.assertTrue(doc.file_url.startswith("/files/"), doc.file_url)

	def test_content_hash_stays_populated_and_is_the_md5_of_the_bytes(self):
		"""docs/INVARIANTS.md invariant 3 — the fork nulled this and destroyed core dedup."""
		content = "hash preservation"
		doc = self.make_file(file_name="hashed.txt", content=content)

		stored = frappe.db.get_value("File", doc.name, "content_hash")
		self.assertEqual(stored, digest_bytes(content).md5)

	def test_the_object_records_both_hashes_and_the_size(self):
		content = b"both hashes"
		doc = self.make_file(file_name="both-hashes.bin", content=content)
		cso = self.cso_of(doc)
		digest = digest_bytes(content)

		self.assertEqual(cso.content_sha256, digest.sha256)
		self.assertEqual(cso.content_hash_md5, digest.md5)
		self.assertEqual(cso.file_size, digest.size)

	def test_a_name_collision_with_different_bytes_gets_a_distinct_url(self):
		"""Core's collision check is `os.path.exists`, which is blind without local files."""
		first = self.make_file(file_name="collide.txt", content="first payload")
		second = self.make_file(file_name="collide.txt", content="second payload")

		self.assertNotEqual(first.file_url, second.file_url)
		self.assertTrue(second.file_url.startswith("/private/files/collide"))

	def test_identical_bytes_keep_one_object_with_two_references(self):
		first = self.make_file(file_name="dedup-a.txt", content="shared payload")
		second = self.make_file(
			file_name="dedup-b.txt", content="shared payload", ignore_duplicate_entry_error=1
		)

		first_cso = frappe.db.get_value("File", first.name, "cloud_storage_object")
		second_cso = frappe.db.get_value("File", second.name, "cloud_storage_object")
		self.assertEqual(first_cso, second_cso)
		self.assertEqual(objects.live_reference_count(first_cso), 2)


class TestModeMatrix(CloudStorageTestCase):
	def test_local_only_creates_no_object_at_all(self):
		set_mode("LOCAL_ONLY")
		doc = self.make_file(file_name="local-only.txt", content="stays here")

		self.assertIsNone(frappe.db.get_value("File", doc.name, "cloud_storage_object"))
		self.assertEqual(self.store.objects, {})
		self.assertTrue(os.path.exists(self.local_path(doc)))

	def test_dual_write_keeps_the_local_copy_and_uploads(self):
		set_mode("DUAL_WRITE")
		doc = self.make_file(file_name="dual.txt", content="both places")
		cso = self.cso_of(doc)

		self.assertTrue(os.path.exists(self.local_path(doc)))
		self.assertTrue(self.store.contains(cso))
		self.assertEqual(cso.status, "uploaded")

	def test_s3_primary_writes_no_canonical_local_copy(self):
		set_mode("S3_PRIMARY_LOCAL_FALLBACK")
		doc = self.make_file(file_name="primary.txt", content="cloud first")
		cso = self.cso_of(doc)

		self.assertFalse(os.path.exists(self.local_path(doc)))
		self.assertTrue(self.store.contains(cso))

	def test_s3_only_writes_no_canonical_local_copy(self):
		doc = self.make_file(file_name="only.txt", content="cloud only")
		cso = self.cso_of(doc)

		self.assertFalse(os.path.exists(self.local_path(doc)))
		self.assertTrue(self.store.contains(cso))

	def test_an_ignored_parent_stays_local_even_in_s3_only(self):
		"""LOCAL_OPERATIONAL scope (ADR 0022): Data Import bytes are consumed by path."""
		doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "import-template.csv",
				"content": "a,b\n1,2\n",
				"is_private": 1,
				"attached_to_doctype": "Data Import",
				"attached_to_name": "DI-TEST",
			}
		)
		doc.flags.ignore_links = True
		doc.insert(ignore_permissions=True)
		self.track(doc)

		self.assertIsNone(frappe.db.get_value("File", doc.name, "cloud_storage_object"))
		self.assertTrue(os.path.exists(self.local_path(doc)))


class TestFailureIsExplicit(CloudStorageTestCase):
	"""PLAN §A — failures are explicit queryable states, never silent fallbacks."""

	def test_s3_only_aborts_the_insert_with_a_typed_error(self):
		self.store.fail_with = CloudStorageTransportError("simulated outage")

		with self.assertRaises(CloudStorageTransportError):
			self.make_file(file_name="s3-only-outage.txt", content="never lands")

	def test_s3_primary_falls_back_to_local_and_records_the_failure(self):
		set_mode("S3_PRIMARY_LOCAL_FALLBACK")
		self.store.fail_with = CloudStorageTransportError("simulated outage")

		doc = self.make_file(file_name="primary-outage.txt", content="degraded but durable")
		cso = self.cso_of(doc)

		self.assertTrue(os.path.exists(self.local_path(doc)), "the bytes must be durable somewhere")
		self.assertEqual(cso.status, "failed")
		self.assertIn("simulated outage", cso.last_error)
		self.assertGreaterEqual(cso.retry_count, 1)

	def test_dual_write_keeps_the_local_copy_and_queues_a_repair(self):
		set_mode("DUAL_WRITE")
		self.store.fail_with = CloudStorageTransportError("simulated outage")

		doc = self.make_file(file_name="dual-outage.txt", content="local is authoritative")
		cso = self.cso_of(doc)

		self.assertTrue(os.path.exists(self.local_path(doc)))
		self.assertEqual(cso.status, "failed")

	def test_dual_write_aborts_when_the_operator_asked_it_to(self):
		set_mode("DUAL_WRITE", fail_insert_on_s3_error=1)
		self.store.fail_with = CloudStorageTransportError("simulated outage")

		with self.assertRaises(CloudStorageTransportError):
			self.make_file(file_name="dual-strict.txt", content="operator wants loud failure")

	def test_the_breaker_short_circuits_s3_only_before_core_does_any_work(self):
		"""A30 — an open breaker fails fast instead of waiting on a dead endpoint."""
		for _ in range(breaker.FAILURE_THRESHOLD):
			breaker.record_failure()
		self.addCleanup(breaker.reset)

		with self.assertRaises(CloudStorageUnavailableError):
			core_hooks.before_write_file(file_size=10)

	def test_before_write_file_is_a_no_op_in_modes_that_can_degrade(self):
		for _ in range(breaker.FAILURE_THRESHOLD):
			breaker.record_failure()
		self.addCleanup(breaker.reset)

		for mode in ("LOCAL_ONLY", "DUAL_WRITE", "S3_PRIMARY_LOCAL_FALLBACK"):
			with self.subTest(mode=mode):
				set_mode(mode)
				self.assertIsNone(core_hooks.before_write_file(file_size=10))


class TestOverwritePersistence(CloudStorageTestCase):
	"""A2 — `gst_return_log.py:103` overwrites and never calls `.save()`."""

	def _overwrite(self, doc, content):
		reloaded = frappe.get_doc("File", doc.name)
		reloaded.save_file(content=content, overwrite=True)
		return reloaded

	def test_an_overwrite_without_a_save_repoints_the_database_row(self):
		doc = self.make_file(file_name="overwrite.json", content='{"v": 1}')
		original_cso = frappe.db.get_value("File", doc.name, "cloud_storage_object")

		self._overwrite(doc, '{"v": 2}')

		row = frappe.db.get_value(
			"File", doc.name, ["cloud_storage_object", "content_hash", "file_size"], as_dict=True
		)
		self.assertNotEqual(row.cloud_storage_object, original_cso)
		self.assertEqual(row.content_hash, digest_bytes('{"v": 2}').md5)
		self.assertEqual(row.file_size, len('{"v": 2}'))

	def test_the_url_stays_stable_so_business_field_pointers_keep_resolving(self):
		"""Contract #11/#18 — the GST log stores `file.file_url` in its own field."""
		doc = self.make_file(file_name="stable-url.json", content='{"v": 1}')
		original_url = doc.file_url

		reloaded = self._overwrite(doc, '{"v": 2}')

		self.assertEqual(reloaded.file_url, original_url)
		self.assertEqual(frappe.db.get_value("File", doc.name, "file_url"), original_url)

	def test_the_previous_object_is_released_only_after_the_new_link_is_durable(self):
		doc = self.make_file(file_name="release-order.json", content='{"v": 1}')
		original_cso = frappe.db.get_value("File", doc.name, "cloud_storage_object")

		self._overwrite(doc, '{"v": 2}')

		new_cso = frappe.db.get_value("File", doc.name, "cloud_storage_object")
		self.assertNotEqual(new_cso, original_cso)
		self.assertEqual(frappe.db.get_value(CSO_DOCTYPE, original_cso, "status"), "pending_delete")
		self.assertEqual(frappe.db.get_value(CSO_DOCTYPE, new_cso, "status"), "uploaded")
		# The replacement is durable before the old one is scheduled, never the other way round.
		self.assertTrue(self.store.contains(frappe.get_doc(CSO_DOCTYPE, new_cso)))
		self.assertTrue(self.store.contains(frappe.get_doc(CSO_DOCTYPE, original_cso)))

	def test_url_siblings_follow_the_mutation(self):
		"""A2 extension — a row sharing the URL must not serve the pre-overwrite bytes."""
		doc = self.make_file(file_name="sibling-url.json", content='{"v": 1}')
		original_cso = frappe.db.get_value("File", doc.name, "cloud_storage_object")

		sibling = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "sibling-url.json",
				"file_url": doc.file_url,
				"is_private": 1,
				"content_hash": doc.content_hash,
			}
		)
		sibling.flags.ignore_duplicate_entry_error = True
		sibling.insert(ignore_permissions=True)
		self.track(sibling)
		frappe.db.set_value("File", sibling.name, "cloud_storage_object", original_cso, update_modified=False)

		self._overwrite(doc, '{"v": 2}')

		new_cso = frappe.db.get_value("File", doc.name, "cloud_storage_object")
		self.assertEqual(frappe.db.get_value("File", sibling.name, "cloud_storage_object"), new_cso)


class TestWritePathDiscipline(CloudStorageTestCase):
	def test_the_write_hook_never_commits_mid_transaction(self):
		"""The fork committed inside the hook, so a failed insert left a half-written row."""
		with patch.object(frappe.db, "commit", side_effect=AssertionError("write_file committed")):
			doc = self.make_file(file_name="no-commit.txt", content="no commit here")
		self.assertTrue(doc.name)

	def test_the_write_path_never_removes_a_local_file(self):
		"""docs/INVARIANTS.md invariant 1 — nothing is deleted before its remote copy is verified."""
		set_mode("DUAL_WRITE")
		with patch("cloud_file_storage.core_hooks.os.remove", side_effect=AssertionError("removed")):
			doc = self.make_file(file_name="no-remove.txt", content="still here")
		self.assertTrue(os.path.exists(self.local_path(doc)))

	def test_write_file_is_the_single_owner_hook_and_resolves_to_this_app(self):
		from frappe.utils import get_hook_method

		self.assertIs(get_hook_method("write_file"), core_hooks.write_file)
		self.assertIs(get_hook_method("delete_file_data_content"), core_hooks.delete_file_data_content)

	def test_a_competing_storage_app_is_refused_loudly(self):
		from cloud_file_storage.storage.exceptions import CloudStorageConfigurationError

		def fake_hooks(name=None, *args, **kwargs):
			if name == "write_file":
				return ["cloud_file_storage.core_hooks.write_file", "other_app.hooks.write_file"]
			if name == "override_doctype_class":
				return {"File": ["cloud_file_storage.overrides.file.CloudFile"]}
			return []

		with patch("cloud_file_storage.core_hooks.frappe.get_hooks", side_effect=fake_hooks):
			with self.assertRaises(CloudStorageConfigurationError):
				core_hooks.validate_single_file_owner()


class TestTheLocalWriteToleratesOlderFrappe(CloudStorageTestCase):
	"""`_write_local` must not require a `File` API that the declared floor does not have.

	`File.check_content` — the PDF-JavaScript rejection — first exists in frappe **v15.80.0**,
	along with its only helper `frappe.utils.pdf.pdf_contains_js`. `docs/supported-versions.md`
	declares a **v15.16.0** floor. Calling it unconditionally raised
	`AttributeError: 'CloudFile' object has no attribute 'check_content'` on every write, which
	the F1 reference matrix surfaced as 22 errors on v15.16.0 against a green v15.93.0.

	This bench runs a frappe that HAS the method, so the floor is simulated by removing it —
	the same technique used for the `doc_events_hooks` reader, and for the same reason: the
	installed platform is the forgiving one, so testing only against it is what let three
	successive floor defects ship.
	"""

	def test_a_file_without_check_content_still_writes(self):
		"""The floor: no `check_content` anywhere, so the write must simply proceed."""

		# Driven with an object that genuinely lacks the attribute — which is exactly what
		# v15.16.0 hands us — rather than by deleting the method from the real class, which
		# would leak across every later test in the process.
		class FloorFile:
			"""Only what `_write_local` legitimately uses. No `check_content`, as at the floor."""

			def __init__(self, path):
				self._path = path
				self.flags = frappe._dict()
				self._content = None

			def get_full_path(self):
				return self._path

			def on_rollback(self):
				pass

		path = os.path.join(frappe.get_site_path("private", "files"), "floor-no-check-direct.txt")
		self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
		floor_file = FloorFile(path)

		self.assertFalse(
			hasattr(floor_file, "check_content"),
			"precondition: the stand-in must lack the method, or this proves nothing",
		)

		written = core_hooks._write_local(floor_file, b"floor bytes")

		self.assertEqual(written, path)
		with open(path, "rb") as handle:
			self.assertEqual(handle.read(), b"floor bytes")

	def test_check_content_is_still_called_when_frappe_provides_it(self):
		"""Parity, not omission: where core has the scan, it must still run.

		The guard must not become an excuse to skip a security control on the frappe that
		actually has one. Reverting the fix to an unconditional call keeps this green; deleting
		the call entirely turns it red.
		"""
		calls = []

		class ModernFile:
			def __init__(self, path):
				self._path = path
				self.flags = frappe._dict()
				self._content = None

			def get_full_path(self):
				return self._path

			def check_content(self):
				calls.append(self._content)

			def on_rollback(self):
				pass

		path = os.path.join(frappe.get_site_path("private", "files"), "modern-check.txt")
		self.addCleanup(lambda: os.path.exists(path) and os.remove(path))

		core_hooks._write_local(ModernFile(path), b"scanned bytes")

		self.assertEqual(
			calls,
			[b"scanned bytes"],
			"check_content was not called on a frappe that provides it — the compatibility "
			"guard has become a way to skip a security control rather than to tolerate its "
			"absence",
		)

	def test_our_local_write_still_mirrors_every_check_core_makes(self):
		"""`_write_local` is a FORK of core's `File.write_file`. Nothing else notices it drift.

		The `check_content` incident surfaced loudly only by luck: frappe *added* a method, so
		the fork raised `AttributeError`. Two silent variants of the same drift exist and
		neither mutation above can reach them:

		  - **rename.** If core renames `check_content`, `getattr(file, "check_content", None)`
		    returns None, the scan is skipped on a version that HAS it, and the parity test
		    still passes — it asserts "call it when the attribute exists", and after a rename
		    the attribute does not exist. That is the compatibility guard decaying into a way
		    to skip a security control, in the one form the mutation cannot produce.
		  - **addition.** If core adds a second check to `write_file`, the fork omits it and
		    nothing anywhere errors.

		So compare against core's source directly. This fails on a rename (the name changes)
		and on an addition (the set grows).
		"""
		import inspect
		import re

		from frappe.core.doctype.file.file import File

		source = inspect.getsource(File.write_file)
		# `self\.(\w+)\(` — the trailing `)` was too narrow: it saw only zero-argument calls, so
		# a check added upstream WITH arguments would have slipped through, and the claim to
		# catch "an addition" was half true. This form still excludes
		# `frappe.db.after_rollback.add(self.on_rollback)`, which passes the method rather than
		# calling it.
		self_calls = set(re.findall(r"self\.(\w+)\(", source))

		# `get_full_path` is path computation, not a check, and `_write_local` calls it too.
		checks = self_calls - {"get_full_path"}

		self.assertLessEqual(
			checks,
			{"check_content"},
			f"core's File.write_file now calls {sorted(self_calls)}. `_write_local` forks that "
			f"function and mirrors only `check_content`, so any check beyond it is being "
			f"silently skipped on every cloud write. Mirror it or record why not.",
		)
		# Gated on the capability, not the version — and this assertion very nearly caused a
		# fifth failed floor attempt, in the guard written to prevent floor defects.
		#
		# At v15.16.0 `File.write_file` calls only `self.get_full_path()`; `check_content` does
		# not exist on the class, so core cannot be calling it. Demanding its presence
		# unconditionally fails the exact ref this guard protects.
		#
		# It is also nearly redundant: a RENAME is already caught above, because the new name
		# lands in `self_calls`, is not in the allowed set, and trips `assertLessEqual`. What is
		# left is REMOVAL — and if core removes the scan, our not calling it is parity rather
		# than a defect. Kept only where the platform has the scan, which is where "we still
		# mirror it" is a claim worth making.
		if hasattr(File, "check_content"):
			self.assertIn(
				"check_content",
				self_calls,
				"this frappe defines File.check_content but its write_file no longer calls it — "
				"`_write_local`'s getattr probe would now silently skip a scan the platform "
				"still performs elsewhere.",
			)
