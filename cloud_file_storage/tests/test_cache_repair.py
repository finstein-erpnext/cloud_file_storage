"""The repair path for permanently-dirty cache entries (carried from P4, built in P6).

An entry whose sidecar is missing or garbled can never be evicted — `_has_drifted` says it
might hold an unflushed edit, and nothing ever answers that question — so it occupies the
budget for ever and `run_eviction` warns `cache_eviction_over_budget` on every run with no
remedy. These tests hold both halves of the fix: the entry that CAN be proven safe is
returned to the evictable population, and the entry that cannot is moved, never deleted.
"""

import json
import os
import shutil

import frappe

from cloud_file_storage.cache import eviction
from cloud_file_storage.cache.materialize import cache_root, sidecar_path
from cloud_file_storage.storage.hashing import digest_bytes
from cloud_file_storage.tests.utils import CloudStorageTestCase


class TestCacheRepair(CloudStorageTestCase):
	MODE = "S3_ONLY"

	def setUp(self):
		super().setUp()
		self.addCleanup(self._clear_cache_tree)
		self._clear_cache_tree()

	def _clear_cache_tree(self):
		for root in (cache_root(), eviction.repair_quarantine_root()):
			if os.path.isdir(root):
				shutil.rmtree(root, ignore_errors=True)

	# --- fixtures ------------------------------------------------------------------

	def make_cso(self, content: bytes):
		digest = digest_bytes(content)
		doc = frappe.new_doc("Cloud Storage Object")
		doc.update(
			{
				"content_sha256": digest.sha256,
				"content_hash_md5": digest.md5,
				"file_size": digest.size,
				"bucket": "cfs-unit-test-bucket",
				"visibility": "private",
				"s3_key": f"prv/{digest.sha256}",
				"status": "verified",
			}
		)
		doc.insert(ignore_permissions=True)
		return doc

	def make_entry(self, content: bytes, *, cso, sidecar: dict | None = None) -> str:
		path = os.path.join(cache_root(), f"FILE-TEST__{cso.name}.bin")
		with open(path, "wb") as handle:
			handle.write(content)
		if sidecar is not None:
			with open(sidecar_path(path), "w") as handle:
				json.dump(sidecar, handle)
		return path

	def quarantined_names(self) -> list[str]:
		root = eviction.repair_quarantine_root()
		return sorted(os.listdir(root)) if os.path.isdir(root) else []

	# --- the gap, before the fix ----------------------------------------------------

	def test_an_entry_with_no_sidecar_is_unevictable_to_begin_with(self):
		"""The premise. Without this, every assertion below could be vacuously true."""
		content = b"the object bytes"
		cso = self.make_cso(content)
		self.make_entry(content, cso=cso)

		entries = eviction._entries(cache_root())

		self.assertEqual(len(entries), 1)
		self.assertTrue(entries[0]["dirty"], "a sidecar-less entry must read as dirty")
		self.assertEqual(eviction.trim_to(0)["trimmed"], 0, "and must survive a trim to zero")

	# --- repair ---------------------------------------------------------------------

	def test_an_entry_whose_bytes_match_the_object_gets_a_fresh_sidecar(self):
		content = b"the object bytes"
		cso = self.make_cso(content)
		path = self.make_entry(content, cso=cso)

		result = eviction.repair_unevictable_entries()

		self.assertEqual(result["repaired"], 1)
		self.assertEqual(result["quarantined"], 0)
		sidecar = json.load(open(sidecar_path(path)))
		self.assertEqual(sidecar["sha256"], cso.content_sha256)
		self.assertIsNotNone(sidecar["mtime"])
		self.assertFalse(sidecar["dirty"])

	def test_a_repaired_entry_becomes_evictable(self):
		"""The whole point: the budget can be met again."""
		content = b"the object bytes"
		cso = self.make_cso(content)
		self.make_entry(content, cso=cso)

		eviction.repair_unevictable_entries()

		entries = eviction._entries(cache_root())
		self.assertFalse(entries[0]["dirty"])
		self.assertEqual(eviction.trim_to(0)["trimmed"], 1)

	def test_a_garbled_sidecar_is_repaired_too(self):
		content = b"the object bytes"
		cso = self.make_cso(content)
		path = self.make_entry(content, cso=cso, sidecar={"materialized_at": "yesterday"})

		self.assertEqual(eviction.repair_unevictable_entries()["repaired"], 1)
		self.assertEqual(json.load(open(sidecar_path(path)))["sha256"], cso.content_sha256)

	# --- quarantine -----------------------------------------------------------------

	def test_bytes_that_do_not_match_the_object_are_moved_not_deleted(self):
		"""These may be an unflushed local edit whose provenance was lost."""
		cso = self.make_cso(b"the object bytes")
		path = self.make_entry(b"LOCALLY EDITED BYTES", cso=cso)

		result = eviction.repair_unevictable_entries()

		self.assertEqual(result["quarantined"], 1)
		self.assertEqual(result["repaired"], 0)
		self.assertFalse(os.path.exists(path), "the entry left the cache")
		self.assertEqual(len(self.quarantined_names()), 1)
		quarantined = os.path.join(eviction.repair_quarantine_root(), self.quarantined_names()[0])
		self.assertEqual(open(quarantined, "rb").read(), b"LOCALLY EDITED BYTES")

	def test_an_entry_whose_object_no_longer_exists_is_quarantined(self):
		cso = self.make_cso(b"the object bytes")
		name = cso.name
		frappe.db.delete("Cloud Storage Object", {"name": name})
		self.make_entry(b"orphan bytes", cso=frappe._dict(name=name))

		self.assertEqual(eviction.repair_unevictable_entries()["quarantined"], 1)
		self.assertEqual(len(self.quarantined_names()), 1)

	def test_the_quarantine_lives_outside_the_cache_and_stops_counting_towards_the_budget(self):
		cso = self.make_cso(b"the object bytes")
		self.make_entry(b"LOCALLY EDITED BYTES", cso=cso)

		eviction.repair_unevictable_entries()

		self.assertEqual(eviction._entries(cache_root()), [])
		self.assertNotIn(
			eviction.REPAIR_QUARANTINE_DIRNAME, os.path.relpath(cache_root(), frappe.get_site_path())
		)

	def test_nothing_is_ever_deleted_by_the_repair(self):
		"""Both outcomes preserve the bytes: one in place, one moved."""
		matching = self.make_cso(b"aaaa")
		mismatching = self.make_cso(b"bbbb")
		kept = self.make_entry(b"aaaa", cso=matching)
		moved_path = os.path.join(cache_root(), f"FILE-OTHER__{mismatching.name}.bin")
		with open(moved_path, "wb") as handle:
			handle.write(b"cccc")

		eviction.repair_unevictable_entries()

		self.assertTrue(os.path.exists(kept))
		self.assertEqual(len(self.quarantined_names()), 1)
		body = open(os.path.join(eviction.repair_quarantine_root(), self.quarantined_names()[0]), "rb").read()
		self.assertEqual(body, b"cccc")

	# --- what the repair leaves alone ------------------------------------------------

	def test_a_healthy_entry_is_not_touched(self):
		content = b"the object bytes"
		cso = self.make_cso(content)
		digest = digest_bytes(content)
		path = self.make_entry(content, cso=cso)
		from cloud_file_storage.cache.materialize import write_sidecar

		write_sidecar(path, sha256=digest.sha256, size=digest.size, dirty=False)
		before = json.load(open(sidecar_path(path)))

		result = eviction.repair_unevictable_entries()

		self.assertEqual(result["scanned"], 0)
		self.assertEqual(json.load(open(sidecar_path(path))), before)

	def test_an_explicitly_dirty_entry_with_a_complete_sidecar_is_left_alone(self):
		"""A failed write-back is a different condition with a different owner."""
		content = b"the object bytes"
		cso = self.make_cso(content)
		path = self.make_entry(content, cso=cso)
		from cloud_file_storage.cache.materialize import write_sidecar

		write_sidecar(path, sha256="deadbeef", size=len(content), dirty=True)

		result = eviction.repair_unevictable_entries()

		self.assertEqual(result["scanned"], 0)
		self.assertEqual(result["quarantined"], 0)
		self.assertTrue(os.path.exists(path))

	def test_an_entry_being_materialized_is_skipped_not_repaired(self):
		from filelock import FileLock

		content = b"the object bytes"
		cso = self.make_cso(content)
		self.make_entry(content, cso=cso)

		with FileLock(eviction.probe_lock_path("cfs_mat_FILE-TEST")):
			result = eviction.repair_unevictable_entries()

		self.assertEqual(result["contended"], 1)
		self.assertEqual(result["repaired"], 0)

	def test_an_empty_cache_reports_nothing(self):
		self.assertEqual(
			eviction.repair_unevictable_entries(),
			{"scanned": 0, "repaired": 0, "quarantined": 0, "contended": 0, "quarantine_paths": []},
		)

	def test_the_repair_is_scheduled_before_the_sweep_that_reports_the_problem(self):
		"""The ordering invariant, asserted against the names actually registered.

		Both entries are the `_dispatch` wrappers: `scheduler_events` cannot target a custom
		queue, so each registered entry is an O(ms) dispatcher that enqueues the real job on
		`cloud_migration`. The invariant is unchanged by that indirection -- repair must still
		precede the eviction sweep, or an entry repaired today is only evictable tomorrow.

		Membership is asserted before ordering so a future rename fails by name here rather
		than as a bare `ValueError` out of `.index()`. That is exactly how this test caught the
		dispatcher rename: loudly, but without saying which entry had moved.
		"""
		from cloud_file_storage import hooks

		repair = "cloud_file_storage.cache.eviction.repair_unevictable_entries_dispatch"
		sweep = "cloud_file_storage.cache.eviction.run_eviction_dispatch"
		daily = hooks.scheduler_events["daily"]

		self.assertIn(repair, daily, f"{repair} is not registered in scheduler_events['daily']")
		self.assertIn(sweep, daily, f"{sweep} is not registered in scheduler_events['daily']")
		self.assertLess(
			daily.index(repair),
			daily.index(sweep),
			"the cache repair must run before the eviction sweep, or an entry repaired today "
			"stays unevictable until tomorrow's pass",
		)
