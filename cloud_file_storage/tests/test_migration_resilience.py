"""T-INTR / T-RESTART / T-RETRY / T-CHKSUM / T-PAUSE, and the A31 residue guard.

Every test here breaks the campaign in a way a real site breaks it — a worker killed
mid-transfer, Redis losing the queue, S3 returning 500s, an operator pausing, a bucket
handing back the wrong bytes — and asserts two things each time: the campaign converges, and
**no local file was touched**. The second half is the one that matters; the first is only
useful because of it.
"""

import os
import time
from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, cint, now_datetime

from cloud_file_storage.migration import api, engine, verify
from cloud_file_storage.storage import objects
from cloud_file_storage.storage.exceptions import CloudStorageTransportError
from cloud_file_storage.tests.migration_utils import MigrationTestCase
from cloud_file_storage.tests.utils import set_mode

BATCH_DOCTYPE = "Cloud Migration Batch"
OBJECT_DOCTYPE = "Cloud Migration Object"


class ResilienceTestCase(MigrationTestCase):
	def prepared(self, *, file_name, content=b"resilient bytes", **policy):
		"""A campaign analyzed, planned and started, with one fixture file."""
		doc = self.local_file(file_name=file_name, content=content)
		campaign = self.campaign(**policy)
		self.analyze(campaign)
		self.plan(campaign)
		set_mode("S3_PRIMARY_LOCAL_FALLBACK")
		return doc, campaign

	def batch_of(self, campaign: str, doc) -> str:
		obj = self.object_for(campaign, doc.file_url)
		return obj.batch


class TestInterruptedWorker(ResilienceTestCase):
	"""T-INTR — a worker dies mid-object."""

	def test_a_kill_mid_upload_leaves_the_local_file_and_converges_on_a_rerun(self):
		doc, campaign = self.prepared(file_name="intr-kill.txt", content=b"survive the kill")

		# KeyboardInterrupt is a BaseException, so it is NOT caught by the per-object
		# `except Exception` — it tears the job down exactly the way SIGKILL/SIGINT does,
		# leaving the rows mid-transition instead of neatly failed.
		with patch("cloud_file_storage.storage.engine.put_path", side_effect=KeyboardInterrupt("kill -9")):
			with self.assertRaises(KeyboardInterrupt):
				api.start_migration(campaign, skip_preflight=True)

		obj = self.object_for(campaign, doc.file_url)
		self.assertEqual(obj.status, "Uploading", "the interrupted object should be mid-flight")
		self.assertTrue(self.local_exists(doc), "an interrupt must never cost a local file")
		self.assertIsNone(self.link_of(doc), "nothing may be linked before verification")

		# The batch's heartbeat goes stale; the dispatcher is the watchdog.
		frappe.db.set_value(
			BATCH_DOCTYPE,
			obj.batch,
			"heartbeat_at",
			add_to_date(now_datetime(), seconds=-(engine.STALE_AFTER_SECONDS + 60)),
			update_modified=False,
		)
		frappe.db.commit()

		recovered = engine.recover_stale_batches(campaign)
		self.assertEqual(recovered, 1)
		self.assertEqual(
			frappe.db.get_value(OBJECT_DOCTYPE, obj.name, "status"),
			"Pending",
			"a stranded object must go BACK a step, never forward",
		)

		self.drain(campaign)
		self.assertEqual(self.object_for(campaign, doc.file_url).status, "Verified")
		self.assertTrue(self.local_exists(doc))

	def test_recovery_is_recorded_in_the_audit_log(self):
		"""The durable evidence that a crash was recovered from.

		`attempts` and `last_error` are both overwritten by later phases — which is how a
		reviewer came to find the original kill -9 claim unverifiable — so recovery is
		recorded in the append-only table instead.
		"""
		doc, campaign = self.prepared(file_name="intr-audited.txt")
		obj = self.object_for(campaign, doc.file_url)
		engine.cas_batch(obj.batch, expected="Pending", to="UploadDispatched", heartbeat=True)
		frappe.db.set_value(
			BATCH_DOCTYPE,
			obj.batch,
			"heartbeat_at",
			add_to_date(now_datetime(), seconds=-(engine.STALE_AFTER_SECONDS + 60)),
			update_modified=False,
		)
		frappe.db.commit()

		self.assertEqual(engine.recover_stale_batches(campaign), 1)

		rows = [
			row
			for row in frappe.db.get_all(
				"Cloud Storage Audit Log",
				filters={"campaign": campaign, "action": "campaign_transition"},
				fields=["details"],
				limit_page_length=0,
			)
			if "stale_batch_recovered" in (row.details or "")
		]
		self.assertEqual(len(rows), 1, "recovery was not recorded exactly once")
		self.assertIn(obj.batch, rows[0].details)

	def test_a_lost_recovery_race_records_nothing(self):
		"""The audit row is gated on the CAS.

		Two ticks can see the same stale batch. Only the one whose compare-and-set actually
		won may claim it recovered anything — an append-only table full of unconditional
		writes is no better evidence than the mutable counter it replaced.
		"""
		doc, campaign = self.prepared(file_name="intr-race.txt")
		obj = self.object_for(campaign, doc.file_url)
		engine.cas_batch(obj.batch, expected="Pending", to="UploadDispatched", heartbeat=True)
		frappe.db.set_value(
			BATCH_DOCTYPE,
			obj.batch,
			"heartbeat_at",
			add_to_date(now_datetime(), seconds=-(engine.STALE_AFTER_SECONDS + 60)),
			update_modified=False,
		)
		frappe.db.commit()

		self.assertEqual(engine.recover_stale_batches(campaign), 1)
		before = frappe.db.count("Cloud Storage Audit Log", {"campaign": campaign})

		# A second tick reading the same stale row it has already lost the race for.
		stale = frappe._dict(
			{"name": obj.batch, "status": "UploadDispatched", "attempts": 0, "phase": "UPLOAD"}
		)
		with patch("frappe.db.get_all", return_value=[stale]):
			self.assertEqual(engine.recover_stale_batches(campaign), 0, "the loser claimed a recovery")

		self.assertEqual(
			frappe.db.count("Cloud Storage Audit Log", {"campaign": campaign}),
			before,
			"the losing tick wrote an audit row",
		)

	def test_recovery_never_promotes_an_object(self):
		"""Stale recovery moves state backwards only — Verifying → Uploaded, never → Verified."""
		doc, campaign = self.prepared(file_name="intr-backwards.txt")
		api.start_migration(campaign, skip_preflight=True)
		self.drain(campaign)

		obj = self.object_for(campaign, doc.file_url)
		frappe.db.set_value(OBJECT_DOCTYPE, obj.name, "status", "Verifying", update_modified=False)
		frappe.db.set_value(
			BATCH_DOCTYPE,
			obj.batch,
			{
				"status": "Verifying",
				"heartbeat_at": add_to_date(now_datetime(), seconds=-(engine.STALE_AFTER_SECONDS + 60)),
			},
			update_modified=False,
		)
		frappe.db.commit()

		engine.recover_stale_batches(campaign)
		self.assertEqual(frappe.db.get_value(OBJECT_DOCTYPE, obj.name, "status"), "Uploaded")
		self.assertEqual(frappe.db.get_value(BATCH_DOCTYPE, obj.batch, "status"), "Uploaded")


class TestQueueLoss(ResilienceTestCase):
	"""T-RESTART — Redis is a dispatch hint; MariaDB is the source of truth (ADR-M4)."""

	def test_a_lost_queue_costs_no_state_and_the_next_tick_re_dispatches(self):
		doc, campaign = self.prepared(file_name="restart-redis.txt", content=b"queue may vanish")

		# The batch was dispatched and the job never ran: precisely what a Redis flush does.
		batch = self.batch_of(campaign, doc)
		engine.cas_batch(batch, expected="Pending", to="UploadDispatched", heartbeat=True)
		api.start_migration(campaign, skip_preflight=True)

		self.assertEqual(
			frappe.db.get_value(OBJECT_DOCTYPE, self.object_for(campaign, doc.file_url).name, "status"),
			"Pending",
		)

		frappe.db.set_value(
			BATCH_DOCTYPE,
			batch,
			"heartbeat_at",
			add_to_date(now_datetime(), seconds=-(engine.STALE_AFTER_SECONDS + 60)),
			update_modified=False,
		)
		frappe.db.commit()
		engine.recover_stale_batches(campaign)
		self.assertEqual(frappe.db.get_value(BATCH_DOCTYPE, batch, "status"), "Pending")

		self.drain(campaign)
		self.assertEqual(self.object_for(campaign, doc.file_url).status, "Verified")

	def test_a_second_copy_of_the_same_job_loses_the_claim_and_does_nothing(self):
		"""RQ dedup covers queued/started only; the batch CAS is the real guard."""
		doc, campaign = self.prepared(file_name="restart-double.txt")
		batch = self.batch_of(campaign, doc)

		self.assertTrue(engine.cas_batch(batch, expected="Pending", to="UploadDispatched"))
		self.assertFalse(
			engine.cas_batch(batch, expected="Pending", to="UploadDispatched"),
			"two claims succeeded — the batch could be processed twice",
		)

		first = engine.run_upload_batch(campaign, batch)
		self.assertTrue(first["claimed"])
		second = engine.run_upload_batch(campaign, batch)
		self.assertFalse(second["claimed"], "the second job must exit silently")


class TestRetryAndBackoff(ResilienceTestCase):
	"""T-RETRY — a transient failure retries with backoff; an exhausted one is a Conflict."""

	def test_a_transient_failure_backs_off_and_then_succeeds(self):
		doc, campaign = self.prepared(file_name="retry-transient.txt", content=b"flaky bucket")

		self.store.fail_with = CloudStorageTransportError("injected 503")
		api.start_migration(campaign, skip_preflight=True)

		obj = self.object_for(campaign, doc.file_url)
		self.assertEqual(obj.status, "Pending", "a transient failure must not condemn the object")
		self.assertEqual(cint(obj.attempt_count), 1)
		self.assertIsNotNone(obj.next_retry_at, "no backoff was scheduled")
		self.assertEqual(obj.error_class, "CloudStorageTransportError")
		self.assertTrue(self.local_exists(doc))

		self.store.fail_with = None
		frappe.db.set_value(OBJECT_DOCTYPE, obj.name, "next_retry_at", None, update_modified=False)
		frappe.db.commit()

		self.drain(campaign)
		self.assertEqual(self.object_for(campaign, doc.file_url).status, "Verified")

	def test_backoff_grows_and_is_capped(self):
		first = engine.backoff_seconds(1, 60)
		later = engine.backoff_seconds(4, 60)
		self.assertLess(first, later)
		self.assertLessEqual(engine.backoff_seconds(20, 60), engine.MAX_BACKOFF_SECONDS)
		self.assertGreaterEqual(engine.backoff_seconds(1, 60), 1)

	def test_an_exhausted_object_becomes_a_conflict_with_the_file_untouched(self):
		doc, campaign = self.prepared(
			file_name="retry-exhausted.txt", content=b"never uploads", max_attempts=1
		)
		self.store.fail_with = CloudStorageTransportError("permanently injected 503")

		api.start_migration(campaign, skip_preflight=True)

		obj = self.object_for(campaign, doc.file_url)
		self.assertEqual(obj.status, "Failed")
		self.assertTrue(self.conflicts_of(campaign, conflict_type="upload_failed"))
		self.assertTrue(self.local_exists(doc), "a failed upload must never cost the local file")
		self.assertIsNone(self.link_of(doc))


class TestChecksum(ResilienceTestCase):
	"""T-CHKSUM — verification is independent, and a mismatch links nothing."""

	def test_tampered_remote_bytes_produce_a_conflict_and_no_link(self):
		doc, campaign = self.prepared(file_name="chksum-tamper.txt", content=b"authentic bytes")
		self.upload_only(campaign)

		# Upload happened; corrupt the bucket copy before VERIFY looks at it.
		obj = self.object_for(campaign, doc.file_url)
		cso = objects.get_cso(obj.cloud_storage_object)
		self.assertEqual(obj.status, "Uploaded")
		self.store.objects[cso.s3_key] = b"corrupted in transit"

		self.verify_only(campaign)

		obj = self.object_for(campaign, doc.file_url)
		self.assertEqual(obj.status, "Failed")
		self.assertIsNone(self.link_of(doc), "a File row was linked to unverified bytes")
		self.assertEqual(
			frappe.db.get_value(objects.CSO_DOCTYPE, cso.name, "status"),
			"uploaded",
			"the object must not be promoted to verified",
		)
		self.assertTrue(self.conflicts_of(campaign, conflict_type="checksum_mismatch"))
		self.assertTrue(self.local_exists(doc))

	def test_a_missing_remote_object_is_a_conflict_not_a_silent_pass(self):
		doc, campaign = self.prepared(file_name="chksum-vanished.txt", content=b"gone from the bucket")
		self.upload_only(campaign)

		obj = self.object_for(campaign, doc.file_url)
		cso = objects.get_cso(obj.cloud_storage_object)
		self.store.objects.pop(cso.s3_key)

		self.verify_only(campaign)
		self.assertEqual(self.object_for(campaign, doc.file_url).status, "Failed")
		self.assertIsNone(self.link_of(doc))

	def test_the_verify_strategy_splits_at_the_configured_cutover(self):
		"""ADR-M7/M8 — HEAD below the threshold, streamed re-GET at or above it."""
		doc, campaign = self.prepared(file_name="chksum-split.txt", content=b"split me")
		self.upload_only(campaign)
		obj = self.object_for(campaign, doc.file_url)
		cso = objects.get_cso(obj.cloud_storage_object)

		with patch("cloud_file_storage.storage.engine.verify_by_streaming") as streamed:
			verify.verify_object(cso, cutover_mb=64)
			streamed.assert_not_called()

		cso.file_size = 64 * verify.MEGABYTE + 1
		with patch("cloud_file_storage.storage.engine.verify_by_streaming") as streamed:
			verify.verify_object(cso, cutover_mb=64)
			streamed.assert_called_once()


class TestBandwidthThrottle(ResilienceTestCase):
	"""F3 — the bandwidth limit is enforced, and it is a real budget rather than a label.

	Asserted on the arithmetic rather than on wall-clock sleeps: a test that measured elapsed
	time would be a test of how loaded the machine is. The end-to-end measurement — two
	comparable batches, one throttled and one not — is a rehearsal stage
	(`tests/rehearsal.py::throttle`), because it needs a corpus big enough for bandwidth
	rather than request rate to be the limit.
	"""

	def test_a_zero_limit_is_off_and_never_sleeps(self):
		budget = engine.ThrottleBudget(0, parallelism=2)
		self.assertFalse(budget.enabled)
		self.assertEqual(budget.consume(10_000_000), 0.0)

	def test_the_campaign_limit_is_divided_across_the_workers(self):
		"""The configured ceiling belongs to the campaign, not to each worker.

		Without the division, `parallelism` workers each throttling to the campaign's limit
		would together transfer `parallelism` times it — the limit would be a per-worker
		label rather than the bound an operator set.
		"""
		single = engine.ThrottleBudget(8, parallelism=1)
		shared = engine.ThrottleBudget(8, parallelism=4)
		self.assertEqual(single.bytes_per_second, 1_000_000.0)
		self.assertEqual(shared.bytes_per_second, 250_000.0)
		self.assertEqual(shared.bytes_per_second * 4, single.bytes_per_second)

	def test_consuming_faster_than_the_budget_sleeps_for_the_difference(self):
		budget = engine.ThrottleBudget(8, parallelism=1)  # 1,000,000 bytes/second
		budget.started = time.monotonic()  # pin the clock so the arithmetic is the assertion

		with patch("time.sleep") as slept:
			budget.consume(2_000_000)

		slept.assert_called_once()
		# Two megabytes at one megabyte per second owes about two seconds, less whatever the
		# call itself took.
		self.assertAlmostEqual(slept.call_args[0][0], 2.0, delta=0.2)

	def test_consuming_within_the_budget_does_not_sleep(self):
		"""The counterpart: a throttle that always slept would pass the test above."""
		budget = engine.ThrottleBudget(8, parallelism=1)
		budget.started = time.monotonic() - 5.0  # five seconds of credit accrued

		with patch("time.sleep") as slept:
			budget.consume(1_000_000)

		slept.assert_not_called()

	def test_an_upload_batch_builds_its_budget_from_the_campaign_policy(self):
		"""The wiring: the campaign's two fields are what reach the budget."""
		doc, campaign = self.prepared(file_name="throttle-wiring.txt")
		frappe.db.set_value(
			"Cloud Migration Campaign",
			campaign,
			{"bandwidth_limit_mbps": 16, "parallelism": 2},
			update_modified=False,
		)
		frappe.db.commit()

		seen = {}
		real = engine.ThrottleBudget

		def capture(mbps, parallelism=1):
			budget = real(mbps, parallelism)
			seen["bytes_per_second"] = budget.bytes_per_second
			return budget

		with patch.object(engine, "ThrottleBudget", capture):
			api.start_migration(campaign, skip_preflight=True)

		self.assertEqual(seen["bytes_per_second"], 16 * 1_000_000 / 8 / 2)


class TestPauseAndStop(ResilienceTestCase):
	"""T-PAUSE — pause, resume and stop-after-batch are honoured per object."""

	def test_pause_stops_the_worker_and_does_not_burn_an_attempt(self):
		docs = [
			self.local_file(file_name=f"pause-{index}.txt", content=f"pause payload {index}".encode())
			for index in range(4)
		]
		campaign = self.campaign(batch_size=4)
		self.analyze(campaign)
		self.plan(campaign)
		set_mode("S3_PRIMARY_LOCAL_FALLBACK")

		batch = self.object_for(campaign, docs[0].file_url).batch
		engine.cas_batch(batch, expected="Pending", to="UploadDispatched")
		frappe.db.set_value("Cloud Migration Campaign", campaign, "control_flag", "PAUSE")
		frappe.db.commit()

		result = engine.run_upload_batch(campaign, batch)
		self.assertTrue(result["claimed"])
		self.assertEqual(result["objects"], 0, "a paused worker must stop before the first object")

		row = frappe.db.get_value(BATCH_DOCTYPE, batch, ["status", "attempts"], as_dict=True)
		self.assertEqual(row.status, "Pending")
		self.assertEqual(cint(row.attempts), 0, "pausing is an operator action, not a failed attempt")
		for doc in docs:
			self.assertTrue(self.local_exists(doc))

	def test_resume_carries_the_campaign_to_completion(self):
		doc, campaign = self.prepared(file_name="pause-resume.txt")
		api.start_migration(campaign, skip_preflight=True)
		api.pause(campaign)
		self.assertEqual(frappe.db.get_value("Cloud Migration Campaign", campaign, "control_flag"), "PAUSE")
		self.assertEqual(engine.dispatch_batches(campaign), 0, "a paused campaign must dispatch nothing")

		api.resume(campaign)
		self.drain(campaign)
		self.assertEqual(self.object_for(campaign, doc.file_url).status, "Verified")

	def test_stop_drains_and_then_finalises(self):
		doc, campaign = self.prepared(file_name="pause-stop.txt")
		api.start_migration(campaign, skip_preflight=True)
		api.stop(campaign)

		self.assertEqual(frappe.db.get_value("Cloud Migration Campaign", campaign, "status"), "Stopping")
		self.assertEqual(engine.dispatch_batches(campaign), 0)
		self.assertEqual(api.finalize_stop(campaign)["status"], "Stopped")
		self.assertTrue(self.local_exists(doc))


class TestBatchResidue(ResilienceTestCase):
	"""A31 — a batch is never `Uploaded` while it still holds non-terminal objects."""

	def test_a_batch_with_residue_goes_back_to_pending_rather_than_uploaded(self):
		docs = [
			self.local_file(file_name=f"residue-{index}.txt", content=f"residue {index}".encode())
			for index in range(3)
		]
		campaign = self.campaign(batch_size=3)
		self.analyze(campaign)
		self.plan(campaign)
		set_mode("S3_PRIMARY_LOCAL_FALLBACK")

		batch = self.object_for(campaign, docs[0].file_url).batch
		engine.cas_batch(batch, expected="Pending", to="Uploading")
		camp = frappe.get_doc("Cloud Migration Campaign", campaign)

		outcome = engine.finalize_upload_batch(batch, camp)
		self.assertEqual(outcome, "Pending", "a batch with three Pending objects was called Uploaded")
		self.assertEqual(engine.batch_has_upload_residue(batch), 3)

	def test_verify_refuses_a_batch_that_has_not_finished_uploading(self):
		docs = [
			self.local_file(file_name=f"residue-verify-{index}.txt", content=f"rv {index}".encode())
			for index in range(2)
		]
		campaign = self.campaign(batch_size=2)
		self.analyze(campaign)
		self.plan(campaign)
		set_mode("S3_PRIMARY_LOCAL_FALLBACK")

		batch = self.object_for(campaign, docs[0].file_url).batch
		# A batch that claims to be ready for verification while its objects are not.
		frappe.db.set_value(BATCH_DOCTYPE, batch, "status", "VerifyDispatched", update_modified=False)
		frappe.db.commit()

		result = verify.run_verify_batch(campaign, batch)
		self.assertFalse(result["claimed"])
		self.assertEqual(result["residue"], 2)
		self.assertEqual(frappe.db.get_value(BATCH_DOCTYPE, batch, "status"), "Pending")
		for doc in docs:
			self.assertIsNone(self.link_of(doc), "verify linked rows for a batch it refused")

	def test_a_finished_batch_does_reach_uploaded(self):
		"""The counterpart: without it the residue assertions could hold vacuously."""
		doc, campaign = self.prepared(file_name="residue-clean.txt")
		api.start_migration(campaign, skip_preflight=True)
		batch = self.object_for(campaign, doc.file_url).batch
		self.assertEqual(frappe.db.get_value(BATCH_DOCTYPE, batch, "status"), "Verified")


class TestFileMutatedUnderUs(ResilienceTestCase):
	"""The live site keeps working while the campaign runs."""

	def test_a_file_deleted_mid_flight_is_skipped_not_failed(self):
		doc, campaign = self.prepared(file_name="mutate-deleted.txt", content=b"about to be deleted")
		obj_name = self.object_for(campaign, doc.file_url).name

		os.remove(doc._canonical_local_path())
		frappe.db.delete("File", {"name": doc.name})
		frappe.db.commit()

		api.start_migration(campaign, skip_preflight=True)
		self.assertEqual(
			frappe.db.get_value(OBJECT_DOCTYPE, obj_name, "skip_reason"), "deleted_during_migration"
		)

	def test_a_file_moved_mid_flight_is_re_identified_from_the_db(self):
		doc, campaign = self.prepared(file_name="mutate-moved.txt", content=b"moving target")
		obj_name = self.object_for(campaign, doc.file_url).name

		new_path = frappe.get_site_path("private", "files", "mutate-moved-elsewhere.txt")
		os.rename(doc._canonical_local_path(), new_path)
		self.addCleanup(lambda: os.path.exists(new_path) and os.remove(new_path))
		frappe.db.set_value(
			"File", doc.name, "file_url", "/private/files/mutate-moved-elsewhere.txt", update_modified=False
		)
		frappe.db.commit()

		api.start_migration(campaign, skip_preflight=True)
		row = frappe.db.get_value(OBJECT_DOCTYPE, obj_name, ["status", "disk_path"], as_dict=True)
		self.assertEqual(
			os.path.realpath(row.disk_path),
			os.path.realpath(new_path),
			"the identity was not refreshed from the File row",
		)
		self.assertTrue(os.path.exists(new_path), "the moved file must still be there")
