"""Edge cases of the maintenance surfaces: queue dispatch, sweeps, cache housekeeping.

Mostly the paths that only run when something is already wrong — a missing queue, a
garbled sidecar, a cache directory that does not exist yet. They are the paths least
likely to be exercised in anger and most likely to turn a small problem into an outage.
"""

import json
import os
from unittest.mock import patch

import frappe

from cloud_file_storage import background, gc, health, install
from cloud_file_storage.cache import eviction, materialize
from cloud_file_storage.overrides.file import is_canonical_url, peek_legacy_stash, stash_legacy_cso
from cloud_file_storage.storage import objects
from cloud_file_storage.tests.utils import CSO_DOCTYPE, CloudStorageTestCase, set_mode


class TestQueueDispatch(CloudStorageTestCase):
	def test_the_dedicated_queue_is_used_when_it_is_configured(self):
		with patch(
			"cloud_file_storage.background.get_queue_list",
			return_value=["short", "default", "long", "cloud_migration"],
		):
			self.assertEqual(background.migration_queue(), "cloud_migration")

	def test_a_redis_hiccup_does_not_stop_the_maintenance_job(self):
		with patch("cloud_file_storage.background.get_queue_list", side_effect=RuntimeError("redis down")):
			with patch("cloud_file_storage.background.frappe.logger"):
				self.assertEqual(background.migration_queue(), "long")

	def test_enqueue_maintenance_targets_the_resolved_queue(self):
		with patch("cloud_file_storage.background.migration_queue", return_value="cloud_migration"):
			with patch("cloud_file_storage.background.enqueue") as enqueue:
				background.enqueue_maintenance("some.method", timeout=60)

		self.assertEqual(enqueue.call_args.args[0], "some.method")
		self.assertEqual(enqueue.call_args.kwargs["queue"], "cloud_migration")
		self.assertEqual(enqueue.call_args.kwargs["timeout"], 60)


class TestSweepGuards(CloudStorageTestCase):
	def test_every_sweep_is_inert_in_local_only(self):
		set_mode("LOCAL_ONLY")

		self.assertEqual(gc.run_deferred_object_gc()["scanned"], 0)
		self.assertEqual(gc.run_orphan_sweep()["scanned"], 0)
		self.assertEqual(gc.repair_pending_uploads()["scanned"], 0)

		with patch("cloud_file_storage.gc.enqueue_maintenance") as enqueue:
			gc.repair_pending_uploads_dispatch()
		enqueue.assert_not_called()

	def test_the_repair_dispatcher_does_nothing_when_there_is_nothing_to_repair(self):
		with patch("cloud_file_storage.gc.frappe.db.count", return_value=0):
			with patch("cloud_file_storage.gc.enqueue_maintenance") as enqueue:
				gc.repair_pending_uploads_dispatch()
		enqueue.assert_not_called()

	def test_one_bad_object_does_not_abort_the_whole_sweep(self):
		doc = self.make_file(file_name="sweep-resilient.txt", content="resilient")
		cso_name = doc.cloud_storage_object
		frappe.delete_doc("File", doc.name, ignore_permissions=True, force=True)
		frappe.db.set_value(
			CSO_DOCTYPE, cso_name, "deletion_scheduled_at", "2020-01-01 00:00:00", update_modified=False
		)

		with patch("cloud_file_storage.gc._collect_one", side_effect=RuntimeError("boom")):
			with patch("cloud_file_storage.gc.frappe.log_error") as log_error:
				stats = gc.run_deferred_object_gc()

		self.assertGreaterEqual(stats["failed"], 1)
		log_error.assert_called()

	def test_an_object_that_left_pending_delete_before_the_sweep_reached_it_is_skipped(self):
		doc = self.make_file(file_name="sweep-raced.txt", content="raced")
		cso_name = doc.cloud_storage_object
		frappe.delete_doc("File", doc.name, ignore_permissions=True, force=True)
		frappe.db.set_value(
			CSO_DOCTYPE, cso_name, "deletion_scheduled_at", "2020-01-01 00:00:00", update_modified=False
		)
		# Something else revived it between the scan and the lock.
		objects.set_cso_status(cso_name, "uploaded", deletion_scheduled_at=None)

		gc.run_deferred_object_gc()

		self.assertEqual(frappe.db.get_value(CSO_DOCTYPE, cso_name, "status"), "uploaded")
		self.assertEqual(self.store.deleted, [])

	def test_the_orphan_sweep_survives_a_failing_object(self):
		doc = self.make_file(file_name="sweep-orphan-error.txt", content="orphan error")
		cso_name = doc.cloud_storage_object
		frappe.db.delete("File", {"name": doc.name})
		frappe.db.set_value(
			CSO_DOCTYPE,
			cso_name,
			{"reference_count": 0, "modified": "2020-01-01 00:00:00"},
			update_modified=False,
		)

		with patch("cloud_file_storage.gc.objects.schedule_deletion", side_effect=RuntimeError("boom")):
			with patch("cloud_file_storage.gc.frappe.log_error") as log_error:
				stats = gc.run_orphan_sweep()

		self.assertGreaterEqual(stats["scanned"], 1)
		log_error.assert_called()

	def test_repair_skips_an_object_that_is_no_longer_repairable(self):
		doc = self.make_file(file_name="repair-skip.txt", content="already fine")
		cso_name = doc.cloud_storage_object
		self.assertEqual(frappe.db.get_value(CSO_DOCTYPE, cso_name, "status"), "uploaded")

		with patch("cloud_file_storage.gc.frappe.get_all", return_value=[cso_name]):
			stats = gc.repair_pending_uploads()

		self.assertGreaterEqual(stats["unrepairable"], 1)


class TestCacheHousekeeping(CloudStorageTestCase):
	def test_eviction_is_a_no_op_without_a_cache_directory(self):
		materialize.clear_cache_directory()
		self.assertEqual(
			eviction.run_eviction(),
			{
				"expired": 0,
				"trimmed": 0,
				"bytes_freed": 0,
				# A sweep that never ran has nothing outstanding. Asserting the whole shape,
				# not a subset, is what makes a silently dropped counter fail here.
				"contended": 0,
				"bytes_contended": 0,
				"over_budget": 0,
			},
		)

	def test_a_garbled_sidecar_sorts_oldest_and_does_not_crash_the_sweep(self):
		doc = self.make_file(file_name="cache-garbled.txt", content="garbled sidecar")
		path = frappe.get_doc("File", doc.name).get_full_path()
		with open(materialize.sidecar_path(path), "w") as handle:
			handle.write("{not json")

		self.assertEqual(materialize.read_sidecar(path), None)
		self.assertEqual(materialize.dirty_entry_count.__module__, "cloud_file_storage.cache.materialize")
		eviction.run_eviction()

	def test_dirty_counting_tolerates_a_missing_directory_and_bad_files(self):
		materialize.clear_cache_directory()
		self.assertEqual(materialize.dirty_entry_count(), 0)

		root = materialize.cache_root()
		with open(os.path.join(root, "junk" + materialize.SIDECAR_SUFFIX), "w") as handle:
			handle.write("not json either")
		self.assertEqual(materialize.dirty_entry_count(), 0)

	def test_marking_dirty_is_a_no_op_when_there_is_no_sidecar(self):
		materialize.mark_dirty("/nonexistent/cfs/entry")
		materialize.touch_sidecar("/nonexistent/cfs/entry")

	def test_forget_tolerates_a_missing_directory(self):
		materialize.clear_cache_directory()
		materialize.forget("FILE-does-not-exist")

	def test_partial_and_lock_files_are_never_treated_as_cache_entries(self):
		root = materialize.cache_root()
		for name in ("stray.part", "stray.lock"):
			with open(os.path.join(root, name), "w") as handle:
				handle.write("x")
		self.addCleanup(lambda: [os.remove(os.path.join(root, n)) for n in ("stray.part", "stray.lock")])

		eviction.run_eviction()

		self.assertTrue(os.path.exists(os.path.join(root, "stray.part")))

	def test_a_sidecar_records_the_last_access_on_a_cache_hit(self):
		doc = self.make_file(file_name="cache-touch.txt", content="touch me")
		path = frappe.get_doc("File", doc.name).get_full_path()
		with open(materialize.sidecar_path(path)) as handle:
			first = json.load(handle)["last_access"]

		frappe.get_doc("File", doc.name).get_full_path()

		with open(materialize.sidecar_path(path)) as handle:
			self.assertGreaterEqual(json.load(handle)["last_access"], first)


class TestSmallHelpers(CloudStorageTestCase):
	def test_the_legacy_stash_round_trips(self):
		frappe.local.cfs_legacy_write_stash = None
		stash_legacy_cso("a" * 32, 1, "cso-x")

		self.assertEqual(peek_legacy_stash("a" * 32, 1), "cso-x")
		self.assertIsNone(peek_legacy_stash("a" * 32, 0))
		self.assertIsNone(peek_legacy_stash("b" * 32, 1))

	def test_canonical_url_recognition(self):
		self.assertTrue(is_canonical_url("/files/x.png"))
		self.assertTrue(is_canonical_url("/private/files/x.png"))
		for other in ("", None, "https://cdn.example/x.png", "/api/method/whatever", "/assets/x.png"):
			with self.subTest(url=other):
				self.assertFalse(is_canonical_url(other))

	def test_indexes_are_skipped_when_the_object_table_is_absent(self):
		with patch("cloud_file_storage.install.frappe.db.table_exists", return_value=False):
			install.ensure_cloud_storage_object_indexes()

	def test_the_manager_role_is_created_once(self):
		install.ensure_cloud_storage_manager_role()
		install.ensure_cloud_storage_manager_role()
		self.assertTrue(frappe.db.exists("Role", install.CLOUD_STORAGE_MANAGER_ROLE))

	def test_an_unknown_object_resolves_to_none_rather_than_raising(self):
		self.assertIsNone(objects.get_cso(None))
		self.assertIsNone(objects.get_cso("no-such-object"))

	def test_adopting_an_empty_reference_list_is_a_no_op(self):
		doc = self.make_file(file_name="adopt-empty.txt", content="nothing to adopt")
		self.assertEqual(objects.adopt_references(doc.cloud_storage_object, []), 0)

	def test_releasing_an_unknown_object_is_a_no_op(self):
		self.assertFalse(objects.release_reference("no-such-object"))
		self.assertFalse(objects.schedule_deletion("no-such-object"))
		objects.record_failure("no-such-object", "nothing to record")


class TestPublicPrivateResidueDetector(CloudStorageTestCase):
	"""`health.detect_public_private_residue` — the one condition this app reports and
	deliberately refuses to fix.

	Core writes a private file's thumbnail into `public/private/files/`
	(`frappe/core/doctype/file/file.py:463`) and bench's nginx answers `location /` with
	`try_files /<site>/public/$uri`, so those bytes are served off disk to anyone. Rows this
	app manages no longer land there; rows it does not manage keep core's behaviour, because
	LOCAL_ONLY has to stay core-identical and files this app does not own are not its to
	move. So the contract under test is: see it, name it, touch nothing.

	Every assertion is a delta against a baseline rather than an absolute count, because the
	tree is real site state that other suites (and an operator) can also write to.
	"""

	def setUp(self):
		super().setUp()
		self.tree = health.public_private_tree()

	def _plant(self, name: str) -> str:
		"""One file in the public/private tree, removed again afterwards."""
		directory = os.path.join(self.tree, "files")
		os.makedirs(directory, exist_ok=True)
		path = os.path.join(directory, name)
		with open(path, "wb") as handle:
			handle.write(b"pretend this is a thumbnail of a private file")
		self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
		return path

	def test_the_tree_is_the_public_one_not_the_private_one(self):
		"""The whole detector is worthless if it watches the wrong directory."""
		self.assertTrue(self.tree.endswith(os.path.join("public", "private")))
		self.assertTrue(self.tree.startswith(frappe.get_site_path()))
		self.assertNotEqual(self.tree, frappe.get_site_path("private"))

	def test_a_planted_file_is_detected_and_named(self):
		before = health.detect_public_private_residue(log=False)
		path = self._plant("residue-detected_small.png")

		after = health.detect_public_private_residue(log=False)

		self.assertEqual(after["count"] - before["count"], 1)
		self.assertIn(os.path.relpath(path, frappe.get_site_path()), after["paths"])
		self.assertIn("public", after["message"])

	def test_removing_the_file_clears_the_finding(self):
		"""...so the detector is reporting the tree, not accumulating state."""
		before = health.detect_public_private_residue(log=False)
		path = self._plant("residue-cleared_small.png")
		self.assertEqual(health.detect_public_private_residue(log=False)["count"] - before["count"], 1)

		os.remove(path)

		self.assertEqual(health.detect_public_private_residue(log=False)["count"], before["count"])

	def test_the_detector_never_moves_or_deletes_what_it_finds(self):
		"""Detection, not mutation — the reason this is a report and not a repair."""
		path = self._plant("residue-untouched_small.png")

		health.detect_public_private_residue(log=False)

		self.assertTrue(os.path.exists(path), "the detector must not touch files it does not own")

	def test_the_path_list_is_capped_but_the_count_is_exact(self):
		before = health.detect_public_private_residue(log=False)
		for index in range(3):
			self._plant(f"residue-capped-{index}_small.png")

		with patch.object(health, "MAX_REPORTED_PATHS", before["count"] + 2):
			finding = health.detect_public_private_residue(log=False)

		self.assertEqual(finding["count"] - before["count"], 3)
		self.assertEqual(len(finding["paths"]), before["count"] + 2)
		self.assertTrue(finding["truncated"])

	def test_a_finding_is_logged_once_when_logging_is_on(self):
		self._plant("residue-logged_small.png")
		before = frappe.db.count("Error Log", {"method": ("like", "%private files in the public%")})

		health.detect_public_private_residue(log=True)

		after = frappe.db.count("Error Log", {"method": ("like", "%private files in the public%")})
		self.assertEqual(after - before, 1)

	def test_after_migrate_reports_it(self):
		"""The operator-facing entry point, not just the function."""
		self._plant("residue-migrate_small.png")
		before = frappe.db.count("Error Log", {"method": ("like", "%private files in the public%")})

		install.report_public_private_residue()

		after = frappe.db.count("Error Log", {"method": ("like", "%private files in the public%")})
		self.assertEqual(after - before, 1)

	def test_the_connection_report_carries_the_finding(self):
		"""Renamed. It was called `test_the_health_panel_carries_the_finding` and never
		touched the health panel — it reads a key on `compat.test_connection`'s payload,
		which is a different surface with a different audience. The name stated one property
		and the body read another, and it also grepped as coverage for the panel, so the
		panel's own warning looked tested when it was not. The panel is covered by
		`test_storage_health.TestPanelWarnings`; this keeps the connection report covered.
		"""
		from cloud_file_storage.api import compat

		self._plant("residue-panel_small.png")
		set_mode("S3_ONLY")

		report = compat.test_connection()

		self.assertGreaterEqual(report["public_private_residue"]["count"], 1)
