"""A3/A4 — write-back commit discipline and the bounded materialization cache.

`after_request` and `after_job` both run **after** the framework's own commit or rollback,
and the request path swallows exceptions from them. So the properties worth testing are
not "does it detect a change" alone, but: does it commit its own transaction, does it stay
silent-but-logged on failure, and does it refuse to act when the enclosing transaction was
rolled back.
"""

import json
import os
from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, now_datetime

from cloud_file_storage.cache import eviction, materialize, writeback
from cloud_file_storage.storage.exceptions import CloudStorageTransportError
from cloud_file_storage.tests.utils import CSO_DOCTYPE, CloudStorageTestCase, set_mode


class TestCommitDiscipline(CloudStorageTestCase):
	def test_the_flush_commits_its_own_transaction(self):
		"""Nothing else is going to: the framework already committed before we ran."""
		doc = self.make_file(file_name="wb-commit.txt", content="before")
		frappe.db.commit()
		reloaded = frappe.get_doc("File", doc.name)
		original_cso = reloaded.cloud_storage_object

		path = reloaded.get_full_path()
		with open(path, "w") as handle:
			handle.write("after")

		writeback.flush_request()

		# `FrappeTestCase.secondary_connection` first exists in frappe **v15.50.0**
		# (`frappe/tests/utils.py`); the declared floor is **v15.16.0**. Skipping LOUDLY rather
		# than silently, and naming the version, because A22 forbids a silent skip — a control
		# that vanishes quietly reads as green. There is no substitute to write here: the test
		# needs a genuine second database connection to observe cross-transaction visibility,
		# and hand-rolling one would be testing our connection helper, not the locking.
		if not hasattr(self, "secondary_connection"):
			self.skipTest(
				"frappe.tests.utils.FrappeTestCase.secondary_connection does not exist before "
				"frappe v15.50.0, so cross-connection visibility cannot be observed on this "
				"ref. Covered on every ref at or above v15.50.0 by the same CI matrix."
			)

		with self.secondary_connection():
			# A different connection can only see committed rows.
			from_other_connection = frappe.db.get_value("File", doc.name, "cloud_storage_object")
		self.assertIsNotNone(from_other_connection)
		self.assertNotEqual(from_other_connection, original_cso)

	def test_a_rolled_back_transaction_is_never_written_back(self):
		"""The File rows those paths belong to never existed; relinking would resurrect them."""
		doc = self.make_file(file_name="wb-rollback.txt", content="before")
		reloaded = frappe.get_doc("File", doc.name)
		original_cso = reloaded.cloud_storage_object

		path = reloaded.get_full_path()
		with open(path, "w") as handle:
			handle.write("after")

		materialize.mark_transaction_rolled_back()
		writeback.flush_request()

		self.assertEqual(frappe.db.get_value("File", doc.name, "cloud_storage_object"), original_cso)

	def test_a_real_rollback_sets_the_flag_through_the_callback(self):
		doc = self.make_file(file_name="wb-callback.txt", content="callback")
		frappe.db.commit()
		frappe.get_doc("File", doc.name).get_full_path()
		self.assertFalse(materialize.transaction_rolled_back())

		frappe.db.rollback()

		self.assertTrue(materialize.transaction_rolled_back())

	def test_a_failure_is_logged_and_never_propagated_to_the_request(self):
		doc = self.make_file(file_name="wb-failure.txt", content="before")
		frappe.db.commit()
		reloaded = frappe.get_doc("File", doc.name)

		path = reloaded.get_full_path()
		with open(path, "w") as handle:
			handle.write("after")

		with patch("cloud_file_storage.cache.writeback._sync_back", side_effect=RuntimeError("boom")):
			with patch("cloud_file_storage.cache.writeback.frappe.log_error") as log_error:
				writeback.flush_request()

		log_error.assert_called_once()
		self.assertEqual(materialize.tracked_paths(), {})

	def test_tracking_is_cleared_after_every_flush(self):
		doc = self.make_file(file_name="wb-clear.txt", content="tracked once")
		frappe.get_doc("File", doc.name).get_full_path()
		self.assertNotEqual(materialize.tracked_paths(), {})

		writeback.flush_request()

		self.assertEqual(materialize.tracked_paths(), {})

	def test_the_job_hook_takes_the_background_signature(self):
		doc = self.make_file(file_name="wb-job.txt", content="before")
		frappe.db.commit()
		reloaded = frappe.get_doc("File", doc.name)
		original_cso = reloaded.cloud_storage_object

		path = reloaded.get_full_path()
		with open(path, "wb") as handle:
			handle.write(b"after in a job")

		writeback.flush_job(method="some.method", kwargs={}, result=None)

		self.assertNotEqual(frappe.db.get_value("File", doc.name, "cloud_storage_object"), original_cso)


class TestChangeDetection(CloudStorageTestCase):
	def test_a_canonical_local_path_write_is_written_back_too(self):
		"""A4 — the canonical path is handed out and is just as writable as the cache."""
		set_mode("DUAL_WRITE")
		doc = self.make_file(file_name="wb-canonical.txt", content="before")
		frappe.db.commit()
		reloaded = frappe.get_doc("File", doc.name)
		original_cso = reloaded.cloud_storage_object

		path = reloaded.get_full_path()
		self.assertEqual(path, self.local_path(reloaded))
		with open(path, "w") as handle:
			handle.write("after via canonical path")

		writeback.flush_request()

		new_cso = frappe.db.get_value("File", doc.name, "cloud_storage_object")
		self.assertNotEqual(new_cso, original_cso)
		self.assertTrue(self.store.contains(frappe.get_doc(CSO_DOCTYPE, new_cso)))

	def test_rewriting_the_same_bytes_does_not_create_a_new_object(self):
		doc = self.make_file(file_name="wb-same.txt", content="identical")
		reloaded = frappe.get_doc("File", doc.name)
		original_cso = reloaded.cloud_storage_object

		path = reloaded.get_full_path()
		with open(path, "w") as handle:
			handle.write("identical")

		writeback.flush_request()

		self.assertEqual(frappe.db.get_value("File", doc.name, "cloud_storage_object"), original_cso)

	def test_a_deleted_path_is_not_treated_as_a_content_change(self):
		doc = self.make_file(file_name="wb-vanished.txt", content="here")
		reloaded = frappe.get_doc("File", doc.name)
		original_cso = reloaded.cloud_storage_object

		path = reloaded.get_full_path()
		os.remove(path)

		writeback.flush_request()

		self.assertEqual(frappe.db.get_value("File", doc.name, "cloud_storage_object"), original_cso)

	def test_the_old_object_is_released_only_after_the_new_link_lands(self):
		doc = self.make_file(file_name="wb-release.txt", content="before")
		reloaded = frappe.get_doc("File", doc.name)
		original_cso = reloaded.cloud_storage_object

		path = reloaded.get_full_path()
		with open(path, "w") as handle:
			handle.write("after")
		writeback.flush_request()

		new_cso = frappe.db.get_value("File", doc.name, "cloud_storage_object")
		self.assertEqual(frappe.db.get_value(CSO_DOCTYPE, new_cso, "status"), "uploaded")
		self.assertEqual(frappe.db.get_value(CSO_DOCTYPE, original_cso, "status"), "pending_delete")

	def test_an_upload_failure_keeps_the_old_link_and_marks_the_entry_dirty(self):
		"""The local bytes are the newest copy and are safe; do not point at bytes S3 lacks."""
		doc = self.make_file(file_name="wb-dirty.txt", content="before")
		reloaded = frappe.get_doc("File", doc.name)
		original_cso = reloaded.cloud_storage_object

		path = reloaded.get_full_path()
		with open(path, "w") as handle:
			handle.write("after but S3 is down")

		self.store.fail_with = CloudStorageTransportError("outage during write-back")
		try:
			writeback.flush_request()
		finally:
			self.store.fail_with = None

		self.assertEqual(frappe.db.get_value("File", doc.name, "cloud_storage_object"), original_cso)
		self.assertTrue(materialize.read_sidecar(path)["dirty"])
		self.assertGreaterEqual(materialize.dirty_entry_count(), 1)


class TestCacheLayout(CloudStorageTestCase):
	def test_the_sidecar_records_the_identity_and_stat_signature(self):
		doc = self.make_file(file_name="cache-sidecar.txt", content="sidecar bytes")
		cso = self.cso_of(doc)
		path = frappe.get_doc("File", doc.name).get_full_path()

		sidecar = materialize.read_sidecar(path)
		self.assertEqual(sidecar["sha256"], cso.content_sha256)
		self.assertEqual(sidecar["size"], len("sidecar bytes"))
		self.assertEqual(sidecar["cloud_storage_object"], cso.name)
		self.assertFalse(sidecar["dirty"])

	def test_a_partial_download_is_never_promoted_to_the_cache_entry(self):
		doc = self.make_file(file_name="cache-partial.txt", content="complete bytes")
		cso = self.cso_of(doc)
		self.store.objects[cso.s3_key] = b"truncated"

		from cloud_file_storage.storage.exceptions import CloudStorageIntegrityError

		with self.assertRaises(CloudStorageIntegrityError):
			frappe.get_doc("File", doc.name).get_full_path()

		entry = materialize.entry_path(doc.name, cso.name, ".txt")
		self.assertFalse(os.path.exists(entry))
		self.assertFalse(os.path.exists(entry + ".part"))

	def test_forget_removes_every_entry_belonging_to_a_row(self):
		doc = self.make_file(file_name="cache-forget.txt", content="forget me")
		path = frappe.get_doc("File", doc.name).get_full_path()
		self.assertTrue(os.path.exists(path))

		materialize.forget(doc.name)

		self.assertFalse(os.path.exists(path))
		self.assertFalse(os.path.exists(materialize.sidecar_path(path)))


class TestEviction(CloudStorageTestCase):
	def _age(self, path, hours):
		sidecar = materialize.read_sidecar(path)
		sidecar["last_access"] = str(add_to_date(now_datetime(), hours=-hours))
		with open(materialize.sidecar_path(path), "w") as handle:
			json.dump(sidecar, handle)

	def test_an_expired_entry_is_evicted(self):
		set_mode("S3_ONLY", cache_ttl_hours=72)
		doc = self.make_file(file_name="evict-old.txt", content="stale cache")
		path = frappe.get_doc("File", doc.name).get_full_path()
		self._age(path, 100)

		result = eviction.run_eviction()

		self.assertGreaterEqual(result["expired"], 1)
		self.assertFalse(os.path.exists(path))

	def test_a_fresh_entry_survives(self):
		set_mode("S3_ONLY", cache_ttl_hours=72)
		doc = self.make_file(file_name="evict-fresh.txt", content="fresh cache")
		path = frappe.get_doc("File", doc.name).get_full_path()

		eviction.run_eviction()

		self.assertTrue(os.path.exists(path))

	def test_a_dirty_entry_is_never_evicted(self):
		"""Evicting unflushed local changes would be data loss, TTL or no TTL."""
		set_mode("S3_ONLY", cache_ttl_hours=1)
		doc = self.make_file(file_name="evict-dirty.txt", content="unflushed changes")
		path = frappe.get_doc("File", doc.name).get_full_path()
		materialize.mark_dirty(path)
		self._age(path, 500)

		eviction.run_eviction()

		self.assertTrue(os.path.exists(path))

	def test_the_size_budget_trims_least_recently_used_entries_first(self):
		set_mode("S3_ONLY", cache_ttl_hours=10_000, cache_max_size_mb=0)
		doc = self.make_file(file_name="evict-budget.txt", content="over budget")
		path = frappe.get_doc("File", doc.name).get_full_path()

		result = eviction.run_eviction()

		self.assertGreaterEqual(result["trimmed"], 1)
		self.assertFalse(os.path.exists(path))


class TestCacheIsBounded(CloudStorageTestCase):
	"""A24 — the cache is bounded, and not only once a day.

	`run_eviction` is dispatched from `daily`; without an admission check a busy site can
	materialize its way past `cache_max_size_mb` between two sweeps and fill the disk.
	"""

	def _materialize(self, name, payload):
		doc = self.make_file(file_name=name, content=payload)
		return frappe.get_doc("File", doc.name).get_full_path()

	def test_materializing_enforces_the_budget_without_waiting_for_the_sweep(self):
		body = "x" * 4096
		first = self._materialize("budget-1.txt", body + "1")
		self.assertTrue(os.path.exists(first))

		# Budget now smaller than what is already cached plus the next entry.
		set_mode("S3_ONLY", cache_max_size_mb=0)
		self._materialize("budget-2.txt", body + "2")

		self.assertFalse(os.path.exists(first), "the admission check never ran")

	def test_an_object_larger_than_the_whole_budget_is_still_readable(self):
		"""Refusing would make the file unreadable, which is the worse failure."""
		set_mode("S3_ONLY", cache_max_size_mb=0)
		path = self._materialize("budget-oversized.txt", "y" * 8192)

		self.assertTrue(os.path.exists(path))
		with open(path) as handle:
			self.assertEqual(handle.read(), "y" * 8192)

	def test_the_budget_honours_an_explicit_zero_but_defaults_when_unset(self):
		from cloud_file_storage.cache import eviction

		self.assertEqual(eviction.budget_bytes(frappe._dict(cache_max_size_mb=0)), 0)
		self.assertEqual(eviction.budget_bytes(frappe._dict(cache_max_size_mb=None)), 5120 * eviction.MB)


class TestEvictionNeverDiscardsUnflushedBytes(CloudStorageTestCase):
	def _aged_entry(self, name, content, hours=500):
		doc = self.make_file(file_name=name, content=content)
		path = frappe.get_doc("File", doc.name).get_full_path()
		sidecar = materialize.read_sidecar(path)
		sidecar["last_access"] = str(add_to_date(now_datetime(), hours=-hours))
		with open(materialize.sidecar_path(path), "w") as handle:
			json.dump(sidecar, handle)
		return path

	def test_an_entry_whose_bytes_drifted_is_treated_as_dirty(self):
		"""The explicit dirty flag is only set on upload failure, so it cannot be the only signal.

		The edit is **in place and the same length** on purpose. A size-only drift check
		classes that as clean and evicts it — which is the commonest shape of "same file,
		different contents" and exactly the write-back loss this guard exists to stop. The
		full `(mtime, size)` signature catches it, as `_sync_back` already does.
		"""
		set_mode("S3_ONLY", cache_ttl_hours=1)
		original = "before the edit"
		path = self._aged_entry("evict-drifted.txt", original)

		edited = "BEFORE THE EDIT"
		self.assertEqual(len(edited), len(original), "the edit must not change the size")
		with open(path, "w") as handle:
			handle.write(edited)
		self.assertEqual(
			os.path.getsize(path),
			materialize.read_sidecar(path)["size"],
			"the sidecar size still matches, so only a full-signature check can see this",
		)

		eviction.run_eviction()

		self.assertTrue(os.path.exists(path), "an unflushed same-length local edit was evicted")

	def test_an_entry_whose_lock_is_held_is_left_for_the_next_sweep(self):
		from frappe.utils.synchronization import filelock

		set_mode("S3_ONLY", cache_ttl_hours=1)
		doc = self.make_file(file_name="evict-locked.txt", content="being downloaded")
		path = frappe.get_doc("File", doc.name).get_full_path()
		sidecar = materialize.read_sidecar(path)
		sidecar["last_access"] = str(add_to_date(now_datetime(), hours=-500))
		with open(materialize.sidecar_path(path), "w") as handle:
			json.dump(sidecar, handle)

		with filelock(f"cfs_mat_{doc.name}", timeout=5):
			eviction.run_eviction()

		self.assertTrue(os.path.exists(path), "an entry was evicted while its lock was held")

	def test_the_admission_check_can_free_its_own_files_stale_entries(self):
		"""NEW-2: `materialize` holds `cfs_mat_<file>`, and frappe's filelock is not reentrant.

		Without passing the held identity through, a materialize could never evict that
		File's own entries — the probe would time out, be swallowed, and the trim would
		silently under-free.
		"""
		doc = self.make_file(file_name="reentrant.txt", content="z" * 4096)
		reloaded = frappe.get_doc("File", doc.name)
		stale = reloaded.get_full_path()
		self.assertTrue(os.path.exists(stale))

		from frappe.utils.synchronization import filelock

		with filelock(f"cfs_mat_{doc.name}", timeout=5):
			freed = eviction.trim_to(0, holding_lock_for=doc.name)

		self.assertGreaterEqual(freed["trimmed"], 1, "the caller's own entry was not freeable")
		self.assertFalse(os.path.exists(stale))

	def test_another_files_locked_entry_is_still_protected(self):
		"""The reentrancy escape hatch must not disable the lock for everyone else."""
		from frappe.utils.synchronization import filelock

		doc = self.make_file(file_name="reentrant-other.txt", content="w" * 4096)
		path = frappe.get_doc("File", doc.name).get_full_path()

		with filelock(f"cfs_mat_{doc.name}", timeout=5):
			eviction.trim_to(0, holding_lock_for="some-other-file")

		self.assertTrue(os.path.exists(path))

	def test_a_clean_aged_entry_is_still_evicted(self):
		"""The protections must not disable eviction itself."""
		set_mode("S3_ONLY", cache_ttl_hours=1)
		path = self._aged_entry("evict-clean.txt", "nothing changed here")

		eviction.run_eviction()

		self.assertFalse(os.path.exists(path))


class TestVanishedPathsAreReported(CloudStorageTestCase):
	def test_a_tracked_path_that_disappears_is_logged_not_swallowed(self):
		"""Silence here is how an A4 write-back loss leaves no trace at all."""
		doc = self.make_file(file_name="wb-vanish-log.txt", content="here for now")
		reloaded = frappe.get_doc("File", doc.name)
		path = reloaded.get_full_path()
		os.remove(path)

		warnings = []
		with patch(
			"cloud_file_storage.cache.writeback.frappe.logger",
			return_value=frappe._dict(warning=warnings.append, info=lambda *a, **k: None),
		):
			writeback.flush_request()

		self.assertTrue(warnings, "a vanished tracked path was swallowed silently")
		self.assertEqual(warnings[0]["operation"], "writeback_path_vanished")
		self.assertEqual(warnings[0]["file"], doc.name)


class TestRepeatedHandOuts(CloudStorageTestCase):
	"""A4 — a path handed out twice in one request must still be written back.

	erpnext's reposting loop calls `create_json_gz_file` once per chunk against the same
	File (`stock_ledger.py:415-423`), so hand-out → write → hand-out is an ordinary
	sequence, not a corner case. Both failure modes are silent: the caller sees correct
	bytes locally, and the bucket keeps the old ones.
	"""

	def _entry(self, name, content):
		doc = self.make_file(file_name=name, content=content)
		frappe.db.commit()
		reloaded = frappe.get_doc("File", doc.name)
		return doc, reloaded, reloaded.get_full_path()

	def test_the_baseline_recorded_is_the_first_hand_outs(self):
		doc, reloaded, path = self._entry("rehand-baseline.txt", "original")
		first_baseline = materialize.tracked_paths()[path]["mtime"]
		self.assertIsNotNone(first_baseline, "precondition: the first hand-out recorded a baseline")

		with open(path, "w") as handle:
			handle.write("mutated")

		reloaded.get_full_path()

		self.assertEqual(
			materialize.tracked_paths()[path]["mtime"],
			first_baseline,
			"the second hand-out re-baselined write-back to the post-write state",
		)

	def test_an_edit_still_reaches_the_bucket_after_a_second_hand_out(self):
		doc, reloaded, path = self._entry("rehand-flush.txt", "original")
		original_cso = frappe.db.get_value("File", doc.name, "cloud_storage_object")

		with open(path, "w") as handle:
			handle.write("mutated")
		reloaded.get_full_path()

		writeback.flush_request()

		repointed = frappe.db.get_value("File", doc.name, "cloud_storage_object")
		self.assertNotEqual(repointed, original_cso, "the edit never left the disk")
		self.assertEqual(
			self.store.objects[frappe.get_doc(CSO_DOCTYPE, repointed).s3_key],
			b"mutated",
		)

	def test_a_second_hand_out_never_re_downloads_over_unflushed_bytes(self):
		"""The size-changing case: the entry no longer matches its sidecar."""
		doc, reloaded, path = self._entry("rehand-clobber.txt", "short")
		with open(path, "w") as handle:
			handle.write("a considerably longer replacement body")
		self.assertNotEqual(
			os.path.getsize(path),
			materialize.read_sidecar(path)["size"],
			"the rewrite must change the size, or this test cannot see a clobber",
		)

		second = reloaded.get_full_path()

		self.assertEqual(second, path)
		with open(second) as handle:
			self.assertEqual(handle.read(), "a considerably longer replacement body")

	def test_a_conflicting_object_replacement_keeps_the_local_bytes_and_says_so(self):
		"""Both sides moved. Bytes that exist only locally are the ones that can be lost."""
		doc, reloaded, path = self._entry("rehand-conflict.txt", "short")
		with open(path, "w") as handle:
			handle.write("a considerably longer replacement body")

		cso = self.cso_of(doc)
		cso.db_set("content_sha256", "0" * 64, update_modified=False)
		reloaded._forget_resolved_cso()

		warnings = []
		with patch(
			"cloud_file_storage.cache.materialize.frappe.logger",
			return_value=frappe._dict(warning=warnings.append, info=lambda *a, **k: None),
		):
			second = reloaded.get_full_path()

		with open(second) as handle:
			self.assertEqual(handle.read(), "a considerably longer replacement body")
		self.assertTrue(warnings, "a materialization conflict was resolved silently")
		self.assertEqual(warnings[0]["operation"], "materialize_conflict")
		self.assertTrue(materialize.read_sidecar(path)["dirty"], "the entry was not protected")

	def test_a_clean_entry_is_still_reused_rather_than_re_downloaded(self):
		"""The protection must not disable the cache-hit path it sits in front of."""
		doc, reloaded, path = self._entry("rehand-clean.txt", "unchanged")
		before = os.stat(path).st_mtime_ns

		with patch("cloud_file_storage.storage.engine.download_to_path") as download:
			again = reloaded.get_full_path()

		download.assert_not_called()
		self.assertEqual(again, path)
		self.assertEqual(os.stat(path).st_mtime_ns, before)


class TestEvictionReportsWhatItCouldNotFree(CloudStorageTestCase):
	"""The cache bound is only a bound if a sweep that cannot meet it says so.

	A probe timing out and an entry being dirty both mean "not evictable now". Counting
	neither, and measuring the budget over evictable entries alone, is how a cache grows
	past `cache_max_size_mb` while every sweep reports success.
	"""

	def _entry(self, name, content):
		doc = self.make_file(file_name=name, content=content)
		return frappe.get_doc("File", doc.name).get_full_path()

	def test_dirty_entries_count_towards_the_budget(self):
		dirty = self._entry("budget-dirty.txt", "d" * 4096)
		materialize.mark_dirty(dirty)
		clean = self._entry("budget-clean.txt", "c" * 512)

		result = eviction.trim_to(2048)

		self.assertFalse(
			os.path.exists(clean),
			"the sweep measured its budget over evictable entries only and freed nothing",
		)
		self.assertTrue(os.path.exists(dirty), "an unflushed entry was evicted")
		self.assertGreater(result["over_budget"], 0, "the unmet budget was not reported")

	def test_a_contended_entry_is_counted_not_swallowed(self):
		from frappe.utils.synchronization import filelock

		doc = self.make_file(file_name="budget-locked.txt", content="l" * 4096)
		path = frappe.get_doc("File", doc.name).get_full_path()

		with filelock(f"cfs_mat_{doc.name}", timeout=5):
			result = eviction.trim_to(0)

		self.assertTrue(os.path.exists(path))
		self.assertGreaterEqual(result["contended"], 1, "a contended entry was not reported")
		self.assertGreaterEqual(result["bytes_contended"], 4096)
		self.assertGreater(result["over_budget"], 0)

	def test_an_over_budget_sweep_logs_a_warning(self):
		path = self._entry("budget-warn.txt", "w" * 4096)
		materialize.mark_dirty(path)
		set_mode("S3_ONLY", cache_max_size_mb=0, cache_ttl_hours=10_000)

		warnings = []
		with patch(
			"cloud_file_storage.cache.eviction.frappe.logger",
			return_value=frappe._dict(warning=warnings.append, info=lambda *a, **k: None),
		):
			result = eviction.run_eviction()

		self.assertGreater(result["over_budget"], 0)
		self.assertTrue(warnings, "a sweep that could not meet the budget said nothing")
		self.assertEqual(warnings[0]["operation"], "cache_eviction_over_budget")

	def test_a_sweep_that_meets_its_budget_reports_nothing_outstanding(self):
		"""The counters must not be permanently non-zero, or nobody will read them."""
		self._entry("budget-met.txt", "m" * 4096)
		set_mode("S3_ONLY", cache_max_size_mb=0, cache_ttl_hours=10_000)

		result = eviction.run_eviction()

		self.assertEqual(result["over_budget"], 0)
		self.assertEqual(result["contended"], 0)


class TestTheEvictionLockProbe(CloudStorageTestCase):
	"""The probe must be silent and must lock the same file frappe's helper locks.

	`frappe.utils.synchronization.filelock` calls `frappe.log_error` on every timeout, so
	going through it turned "this entry is in use, skip it" into an Error Log document —
	with a full traceback capture — per contended entry per sweep. Those rows appeared
	during green runs, which is how a real under-freeing bug would have looked too.
	"""

	def test_the_probe_targets_the_lockfile_frappe_locks(self):
		"""Constructed locally, so it is only correct while it matches frappe's path."""
		from frappe.utils.synchronization import filelock

		name = f"cfs_mat_probe_{frappe.generate_hash(length=8)}"
		expected = eviction.probe_lock_path(name)

		with filelock(name, timeout=5):
			self.assertTrue(
				os.path.exists(expected),
				f"frappe's filelock did not create {expected}; the probe locks the wrong file",
			)

	def test_the_probe_does_not_hold_a_lock_frappe_would_consider_free(self):
		"""The other direction of the same contract: our lock must actually block frappe's.

		Taken from the second session that briefly shared this worktree — it is the half
		this class was missing. Asserting that `probe_lock_path` names the same file frappe
		locks says nothing about whether holding it excludes anything: a probe that agreed on
		the path but acquired it in a way frappe's `FileLock` did not see would pass the test
		above while protecting nothing, and an entry being written back would be evicted out
		from under its writer. Path identity and mutual exclusion are two properties, so they
		get two tests.

		`frappe.log_error` is patched out because the timeout being provoked here is frappe's
		helper doing its documented thing, not our probe misbehaving.
		"""
		from filelock import FileLock
		from frappe.utils.file_lock import LockTimeoutError
		from frappe.utils.synchronization import filelock

		lock_name = f"cfs_probe_mutual_{frappe.generate_hash(length=8)}"
		path = eviction.probe_lock_path(lock_name)
		os.makedirs(os.path.dirname(path), exist_ok=True)
		self.addCleanup(lambda: os.path.exists(path) and os.remove(path))

		with FileLock(path, timeout=5):
			with patch("frappe.log_error"):
				with self.assertRaises(LockTimeoutError):
					with filelock(lock_name, timeout=0.2):
						pass

	def test_a_contended_probe_writes_no_error_log_row(self):
		from frappe.utils.synchronization import filelock

		doc = self.make_file(file_name="probe-quiet.txt", content="q" * 4096)
		frappe.get_doc("File", doc.name).get_full_path()

		before = frappe.db.count("Error Log")
		with filelock(f"cfs_mat_{doc.name}", timeout=5):
			eviction.trim_to(0)

		self.assertEqual(
			frappe.db.count("Error Log"),
			before,
			"a contended eviction probe logged an error; an in-use entry is not an error",
		)
