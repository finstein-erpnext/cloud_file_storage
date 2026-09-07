"""Deferred garbage collection — the only path from a delete to bytes leaving the bucket.

These tests commit, because the commit-per-object discipline (A6) is itself part of what
is being tested; `CloudStorageTestCase` cleans up the rows explicitly afterwards.
"""

import ast
import os
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, now_datetime

import cloud_file_storage
from cloud_file_storage import gc
from cloud_file_storage.storage import objects
from cloud_file_storage.storage.exceptions import CloudStorageTransportError
from cloud_file_storage.storage.hashing import digest_bytes
from cloud_file_storage.tests.utils import CSO_DOCTYPE, CloudStorageTestCase, set_mode


def _backdate(cso_name):
	frappe.db.set_value(
		CSO_DOCTYPE,
		cso_name,
		"deletion_scheduled_at",
		add_to_date(now_datetime(), days=-1),
		update_modified=False,
	)


class TestDeleteSchedulesRatherThanDeletes(CloudStorageTestCase):
	def test_trashing_the_last_reference_schedules_with_a_grace_window(self):
		set_mode("S3_ONLY", object_delete_grace_days=7)
		doc = self.make_file(file_name="gc-last.txt", content="last one")
		cso_name = doc.cloud_storage_object

		frappe.delete_doc("File", doc.name, ignore_permissions=True, force=True)

		row = frappe.db.get_value(
			CSO_DOCTYPE, cso_name, ["status", "deletion_scheduled_at", "status_before_delete"], as_dict=True
		)
		self.assertEqual(row.status, "pending_delete")
		self.assertEqual(row.status_before_delete, "uploaded")
		self.assertGreater(row.deletion_scheduled_at, now_datetime())
		self.assertTrue(self.store.contains(frappe.get_doc(CSO_DOCTYPE, cso_name)))

	def test_the_delete_hook_never_calls_the_bucket(self):
		doc = self.make_file(file_name="gc-nobucket.txt", content="no inline delete")
		frappe.delete_doc("File", doc.name, ignore_permissions=True, force=True)
		self.assertEqual(self.store.deleted, [])

	def test_the_local_copy_is_removed_but_only_after_core_cleared_the_refcount_gate(self):
		set_mode("DUAL_WRITE")
		doc = self.make_file(file_name="gc-local.txt", content="local copy")
		local_path = self.local_path(doc)
		self.assertTrue(os.path.exists(local_path))

		frappe.delete_doc("File", doc.name, ignore_permissions=True, force=True)

		self.assertFalse(os.path.exists(local_path))

	def test_the_cache_entry_is_dropped_with_the_row(self):
		doc = self.make_file(file_name="gc-cache.txt", content="cached then deleted")
		cache_path = frappe.get_doc("File", doc.name).get_full_path()
		self.assertTrue(os.path.exists(cache_path))

		frappe.delete_doc("File", doc.name, ignore_permissions=True, force=True)

		self.assertFalse(os.path.exists(cache_path))


class TestDeferredCollection(CloudStorageTestCase):
	def _dereferenced_object(self, name, content):
		doc = self.make_file(file_name=name, content=content)
		cso_name = doc.cloud_storage_object
		frappe.delete_doc("File", doc.name, ignore_permissions=True, force=True)
		return cso_name

	def test_nothing_is_collected_before_the_grace_window_elapses(self):
		cso_name = self._dereferenced_object("gc-grace.txt", "wait for me")

		gc.run_deferred_object_gc()

		self.assertEqual(frappe.db.get_value(CSO_DOCTYPE, cso_name, "status"), "pending_delete")
		self.assertTrue(self.store.contains(frappe.get_doc(CSO_DOCTYPE, cso_name)))

	def test_after_the_grace_window_the_object_is_deleted_and_tombstoned(self):
		cso_name = self._dereferenced_object("gc-collect.txt", "collect me")
		key = frappe.db.get_value(CSO_DOCTYPE, cso_name, "s3_key")
		_backdate(cso_name)

		gc.run_deferred_object_gc()

		row = frappe.db.get_value(
			CSO_DOCTYPE, cso_name, ["status", "s3_key", "deletion_scheduled_at"], as_dict=True
		)
		self.assertEqual(row.status, "deleted")
		self.assertEqual(row.s3_key, key, "the tombstone keeps its key for reconciliation")
		self.assertIsNone(row.deletion_scheduled_at)
		self.assertNotIn(key, self.store.objects)
		self.assertIn(key, self.store.deleted)

	def test_a_re_referenced_object_is_revived_instead_of_deleted(self):
		"""The locking recount is the decision; the schedule was only a hint."""
		cso_name = self._dereferenced_object("gc-revive.txt", "referenced again")
		_backdate(cso_name)

		revived_file = self.make_file(file_name="gc-revive-new.txt", content="referenced again")
		self.assertEqual(revived_file.cloud_storage_object, cso_name)

		gc.run_deferred_object_gc()

		self.assertEqual(frappe.db.get_value(CSO_DOCTYPE, cso_name, "status"), "uploaded")
		self.assertTrue(self.store.contains(frappe.get_doc(CSO_DOCTYPE, cso_name)))
		self.assertEqual(self.store.deleted, [])

	def test_the_recount_is_a_locking_read_taken_before_the_count(self):
		"""A6 — a non-locking read under REPEATABLE READ can be stale."""
		cso_name = self._dereferenced_object("gc-lock.txt", "lock me")
		_backdate(cso_name)

		order = []
		real_lock = objects.lock_cso
		real_count = objects.live_reference_count
		for_update_flags = []

		def spy_lock(name):
			order.append(("lock", name))
			return real_lock(name)

		def spy_count(name, **kwargs):
			order.append(("count", name))
			return real_count(name, **kwargs)

		real_get_value = frappe.db.get_value

		def spy_get_value(*args, **kwargs):
			if args and args[0] == CSO_DOCTYPE and kwargs.get("for_update"):
				for_update_flags.append(args[1])
			return real_get_value(*args, **kwargs)

		with patch("cloud_file_storage.gc.objects.lock_cso", side_effect=spy_lock):
			with patch("cloud_file_storage.gc.objects.live_reference_count", side_effect=spy_count):
				with patch.object(frappe.db, "get_value", side_effect=spy_get_value):
					gc.run_deferred_object_gc()

		relevant = [entry for entry in order if entry[1] == cso_name]
		self.assertEqual(relevant[0][0], "lock", "the row must be locked before it is counted")
		self.assertIn("count", [entry[0] for entry in relevant])
		self.assertIn(cso_name, for_update_flags, "the status read must be FOR UPDATE")

	def test_each_object_is_committed_on_its_own(self):
		first = self._dereferenced_object("gc-commit-1.txt", "commit one")
		second = self._dereferenced_object("gc-commit-2.txt", "commit two")
		_backdate(first)
		_backdate(second)

		commits = []
		real_commit = frappe.db.commit

		def counting_commit():
			commits.append(1)
			return real_commit()

		with patch.object(frappe.db, "commit", side_effect=counting_commit):
			gc.run_deferred_object_gc()

		self.assertGreaterEqual(len(commits), 2)

	def test_a_bucket_failure_leaves_the_object_scheduled_rather_than_tombstoned(self):
		cso_name = self._dereferenced_object("gc-failure.txt", "bucket is down")
		_backdate(cso_name)
		self.store.fail_with = CloudStorageTransportError("bucket unavailable")

		try:
			gc.run_deferred_object_gc()
		finally:
			self.store.fail_with = None

		row = frappe.db.get_value(CSO_DOCTYPE, cso_name, ["status", "last_error"], as_dict=True)
		self.assertEqual(row.status, "pending_delete")
		self.assertIn("bucket unavailable", row.last_error)

	def test_an_object_already_gone_from_the_bucket_still_gets_its_tombstone(self):
		cso_name = self._dereferenced_object("gc-already-gone.txt", "vanished early")
		key = frappe.db.get_value(CSO_DOCTYPE, cso_name, "s3_key")
		self.store.objects.pop(key, None)
		_backdate(cso_name)

		gc.run_deferred_object_gc()

		self.assertEqual(frappe.db.get_value(CSO_DOCTYPE, cso_name, "status"), "deleted")

	def test_a_migration_owned_object_is_never_collected(self):
		"""A14 — a non-terminal campaign counts as a live reference."""
		cso_name = self._dereferenced_object("gc-campaign.txt", "campaign owns me")
		_backdate(cso_name)

		with patch("cloud_file_storage.storage.objects.migration_reference_count", return_value=1):
			gc.run_deferred_object_gc()

		self.assertEqual(self.store.deleted, [])
		self.assertNotEqual(frappe.db.get_value(CSO_DOCTYPE, cso_name, "status"), "deleted")

	def test_gc_is_inert_in_local_only(self):
		cso_name = self._dereferenced_object("gc-local-only.txt", "no cloud here")
		_backdate(cso_name)
		set_mode("LOCAL_ONLY")

		stats = gc.run_deferred_object_gc()

		self.assertEqual(stats["scanned"], 0)
		self.assertEqual(self.store.deleted, [])


class TestTombstoneRevival(CloudStorageTestCase):
	"""A25 — delete, force GC, re-upload identical bytes; the object must be servable."""

	def test_re_uploading_identical_bytes_after_collection_works_end_to_end(self):
		doc = self.make_file(file_name="revive-e2e.txt", content="phoenix bytes")
		cso_name = doc.cloud_storage_object
		key = doc.get("cloud_storage_object") and frappe.db.get_value(CSO_DOCTYPE, cso_name, "s3_key")

		frappe.delete_doc("File", doc.name, ignore_permissions=True, force=True)
		_backdate(cso_name)
		gc.run_deferred_object_gc()

		self.assertEqual(frappe.db.get_value(CSO_DOCTYPE, cso_name, "status"), "deleted")
		self.assertNotIn(key, self.store.objects)

		recreated = self.make_file(file_name="revive-e2e-again.txt", content="phoenix bytes")

		self.assertEqual(recreated.cloud_storage_object, cso_name)
		row = frappe.db.get_value(CSO_DOCTYPE, cso_name, ["status", "s3_key"], as_dict=True)
		self.assertEqual(row.status, "uploaded")
		self.assertEqual(row.s3_key, key)
		self.assertIn(key, self.store.objects)
		self.assertEqual(frappe.get_doc("File", recreated.name).get_content(), "phoenix bytes")


class TestOrphanSweep(CloudStorageTestCase):
	def test_an_aged_unreferenced_object_is_scheduled(self):
		"""Closes the `only_thumbnail` leak: core's gate ignores `is_private`."""
		doc = self.make_file(file_name="orphan.txt", content="orphan bytes")
		cso_name = doc.cloud_storage_object
		frappe.db.delete("File", {"name": doc.name})
		frappe.db.set_value(
			CSO_DOCTYPE,
			cso_name,
			{"reference_count": 0, "modified": add_to_date(now_datetime(), days=-30)},
			update_modified=False,
		)

		gc.run_orphan_sweep()

		self.assertEqual(frappe.db.get_value(CSO_DOCTYPE, cso_name, "status"), "pending_delete")
		self.assertTrue(self.store.contains(frappe.get_doc(CSO_DOCTYPE, cso_name)))

	def test_a_referenced_object_is_left_alone_even_when_the_counter_is_stale(self):
		doc = self.make_file(file_name="orphan-referenced.txt", content="still referenced")
		cso_name = doc.cloud_storage_object
		frappe.db.set_value(
			CSO_DOCTYPE,
			cso_name,
			{"reference_count": 0, "modified": add_to_date(now_datetime(), days=-30)},
			update_modified=False,
		)

		gc.run_orphan_sweep()

		self.assertEqual(frappe.db.get_value(CSO_DOCTYPE, cso_name, "status"), "uploaded")


class TestRepairDispatch(CloudStorageTestCase):
	def test_a_failed_upload_is_repaired_from_the_local_copy(self):
		set_mode("DUAL_WRITE")
		self.store.fail_with = CloudStorageTransportError("outage during insert")
		doc = self.make_file(file_name="repair.txt", content="repair me")
		self.store.fail_with = None
		cso_name = doc.cloud_storage_object
		self.assertEqual(frappe.db.get_value(CSO_DOCTYPE, cso_name, "status"), "failed")

		stats = gc.repair_pending_uploads()

		self.assertGreaterEqual(stats["uploaded"], 1)
		self.assertEqual(frappe.db.get_value(CSO_DOCTYPE, cso_name, "status"), "uploaded")
		self.assertTrue(self.store.contains(frappe.get_doc(CSO_DOCTYPE, cso_name)))

	def test_an_object_with_no_local_copy_is_recorded_unrepairable_not_retried_forever(self):
		"""No File row, no local bytes: say so instead of retrying forever."""
		from cloud_file_storage.storage.hashing import digest_bytes

		digest = digest_bytes(b"nowhere to read from")
		cso = objects.ensure_cso(
			content_sha256=digest.sha256,
			content_hash_md5=digest.md5,
			file_size=digest.size,
			visibility="private",
		)
		objects.record_failure(cso.name, "outage during insert")

		stats = gc.repair_pending_uploads()

		self.assertGreaterEqual(stats["unrepairable"], 1)
		self.assertIn("no local copy", frappe.db.get_value(CSO_DOCTYPE, cso.name, "last_error") or "")

	def test_the_dispatcher_targets_the_dedicated_queue(self):
		"""docs/INVARIANTS.md invariant 8 — repair work never lands on the ERP's own queues."""
		doc = self.make_file(file_name="repair-dispatch.txt", content="dispatch me")
		frappe.db.set_value(CSO_DOCTYPE, doc.cloud_storage_object, "status", "pending_upload")

		with patch("cloud_file_storage.gc.enqueue_maintenance") as enqueue:
			gc.repair_pending_uploads_dispatch()

		enqueue.assert_called_once()
		self.assertEqual(enqueue.call_args.args[0], "cloud_file_storage.gc.repair_pending_uploads")

	def test_the_queue_helper_falls_back_loudly_never_silently(self):
		from cloud_file_storage import background

		logged = []
		with patch("cloud_file_storage.background.get_queue_list", return_value=["short", "default", "long"]):
			with patch(
				"cloud_file_storage.background.frappe.logger",
				return_value=frappe._dict(warning=logged.append),
			):
				queue = background.migration_queue()

		self.assertEqual(queue, "long")
		self.assertTrue(logged, "the fallback must be loud")
		self.assertIn("cloud_migration", logged[0])


class TestPhysicalDeleteIsSingleSited(CloudStorageTestCase):
	def test_only_gc_calls_engine_delete(self):
		"""docs/INVARIANTS.md invariant 4 / F6 — grep-enforced, here as a real AST check."""
		package = Path(cloud_file_storage.__file__).resolve().parent
		callers = []
		for path in package.rglob("*.py"):
			relative = path.relative_to(package).as_posix()
			if "test" in path.name or relative in ("storage/engine.py",):
				continue
			tree = ast.parse(path.read_text(encoding="utf-8"))
			for node in ast.walk(tree):
				if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
					if node.func.attr == "delete" and getattr(node.func.value, "id", "") == "engine":
						callers.append(relative)

		self.assertEqual(sorted(set(callers)), ["gc.py"])

	def test_no_upload_path_module_deletes_a_local_file(self):
		"""F6 — UPLOAD/VERIFY modules contain no deletion calls at all."""
		package = Path(cloud_file_storage.__file__).resolve().parent
		banned = {"remove", "unlink", "rmtree", "rename", "replace"}
		offenders = []
		for relative in ("core_hooks.py", "storage/engine.py", "storage/objects.py"):
			tree = ast.parse((package / relative).read_text(encoding="utf-8"))
			for node in ast.walk(tree):
				if isinstance(node, ast.Attribute) and node.attr in banned:
					if getattr(node.value, "id", "") in {"os", "shutil"}:
						offenders.append(f"{relative}:{node.lineno}:{node.attr}")

		# `delete_file_data_content` removes the local copy core has already cleared for
		# deletion; that is core's own contract (#13) and is not an upload path.
		self.assertEqual(
			[entry for entry in offenders if not entry.startswith("core_hooks.py")],
			[],
			f"deletion calls found in upload/verify modules: {offenders}",
		)


class TestRepairNeverPublishesUnderTheWrongKey(CloudStorageTestCase):
	"""The key IS the hash (PLAN §A), so repair must re-hash before it uploads.

	Reachable in one outage: a DUAL_WRITE file created while S3 is down (object
	`pending_upload`, bytes local), then edited through `get_full_path()`. The hourly repair
	would otherwise publish the new bytes under the old sha256 key, breaking
	content-addressing and pre-poisoning P5's `engine.verify()`.
	"""

	def _drifted_object(self):
		set_mode("DUAL_WRITE")
		self.store.fail_with = CloudStorageTransportError("outage during insert")
		doc = self.make_file(file_name="repair-drift.txt", content="original bytes")
		self.store.fail_with = None

		local_path = self.local_path(doc)
		with open(local_path, "w") as handle:
			handle.write("bytes that drifted after the fact")
		return doc, local_path

	def test_a_drifted_local_copy_is_refused_and_recorded(self):
		doc, _ = self._drifted_object()
		cso_name = doc.cloud_storage_object
		expected_key = frappe.db.get_value(CSO_DOCTYPE, cso_name, "s3_key")

		stats = gc.repair_pending_uploads()

		self.assertGreaterEqual(stats["unrepairable"], 1)
		self.assertNotIn(expected_key, self.store.objects)
		last_error = frappe.db.get_value(CSO_DOCTYPE, cso_name, "last_error") or ""
		self.assertIn("wrong key", last_error)

	def test_an_intact_local_copy_still_repairs(self):
		"""The guard must not break the repair path it is protecting."""
		set_mode("DUAL_WRITE")
		self.store.fail_with = CloudStorageTransportError("outage during insert")
		doc = self.make_file(file_name="repair-intact.txt", content="unchanged bytes")
		self.store.fail_with = None

		stats = gc.repair_pending_uploads()

		self.assertGreaterEqual(stats["uploaded"], 1)
		self.assertEqual(frappe.db.get_value(CSO_DOCTYPE, doc.cloud_storage_object, "status"), "uploaded")

	def test_the_published_bytes_always_hash_to_their_key(self):
		doc, _ = self._drifted_object()
		gc.repair_pending_uploads()

		for key, content in self.store.objects.items():
			with self.subTest(key=key[-12:]):
				self.assertEqual(key.rsplit("/", 1)[-1], digest_bytes(content).sha256)
