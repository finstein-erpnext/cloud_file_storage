"""T-BACKUP — the backup job: freshness (A28), verification (A5), refusals.

Every refusal in here is proved to fire against an input that should trigger it, and every
"kept, not deleted" assertion reads `FakeBackupBucket.deleted`, which the fake appends to
on any `delete_object` — so the claim "the artifact survives a failed verification" is a
statement about a call that did not happen, not about a comment.
"""

import ast
import inspect
import os
import time
from unittest.mock import patch

import frappe
from frappe.utils import add_days, add_to_date, get_datetime, now_datetime

from cloud_file_storage.backup import tasks
from cloud_file_storage.backup.exceptions import BackupFreshnessError, BackupVerificationError
from cloud_file_storage.tests.backup_utils import (
	TEST_BACKUP_BUCKET,
	BackupTestCase,
	FakeDump,
	set_backup_settings,
)
from cloud_file_storage.tests.markers import refusal_guard

BACKUP_LOG_DOCTYPE = "Cloud Storage Backup Log"


class BackupJobTestCase(BackupTestCase):
	"""Shared fixture: a fake `new_backup` returning artifacts this test wrote."""

	def make_dump(
		self,
		*,
		db_content: bytes = b"-- database dump\n",
		conf_content: bytes = b'{"db_name": "x"}',
		db_age_seconds: float = 0,
		conf_age_seconds: float = 0,
		timestamp: str = "20260816_010000",
		public_content: bytes | None = None,
		private_content: bytes | None = None,
	) -> FakeDump:
		db_path = self.temp_artifact(f"{timestamp}-cfs-database.sql.gz", db_content)
		conf_path = self.temp_artifact(f"{timestamp}-cfs-site_config_backup.json", conf_content)
		public_path = private_path = None
		if public_content is not None:
			public_path = self.temp_artifact(f"{timestamp}-cfs-files.tgz", public_content)
		if private_content is not None:
			private_path = self.temp_artifact(f"{timestamp}-cfs-private-files.tgz", private_content)

		dump = FakeDump(
			backup_path_db=db_path,
			backup_path_conf=conf_path,
			backup_path_files=public_path,
			backup_path_private_files=private_path,
			todays_date=timestamp,
		)
		# Ages are recorded, not applied now, and `run_backup` stamps them when the fake
		# `new_backup` is CALLED. The real generator writes its artifacts during the job, so
		# a fixture that stamped them beforehand would model the wrong ordering — and, at
		# age 0, would land a few microseconds before `job_start_epoch()` and fail the
		# freshness assertion at random.
		dump.ages = {db_path: db_age_seconds, conf_path: conf_age_seconds}
		for path in (public_path, private_path):
			if path:
				dump.ages[path] = 0
		return dump

	def run_backup(self, dump: FakeDump, *, trigger: str = "scheduled"):
		def produce(**kwargs):
			dump.stamp()
			return dump

		with patch("frappe.utils.backups.new_backup", side_effect=produce) as new_backup:
			self.new_backup_mock = new_backup
			try:
				return tasks.take_cloud_backup(trigger=trigger)
			finally:
				self.track_new_logs()

	def last_log(self) -> dict:
		name = frappe.get_all(BACKUP_LOG_DOCTYPE, order_by="creation desc", limit=1, pluck="name")[0]
		return frappe.get_doc(BACKUP_LOG_DOCTYPE, name)


class TestBackupFreshness(BackupJobTestCase):
	"""A28 — the dump uploaded is the dump this run produced."""

	def test_a_fresh_dump_is_uploaded_and_logged_success(self):
		log_name = self.run_backup(self.make_dump())

		log = frappe.get_doc(BACKUP_LOG_DOCTYPE, log_name)
		self.assertEqual(log.status, "Success")
		self.assertEqual(len(self.bucket.uploads), 2, "database and site config should both upload")
		self.assertTrue(log.db_key)
		self.assertTrue(log.config_key)

	@refusal_guard
	def test_new_backup_is_asked_to_force_a_fresh_dump(self):
		"""`force=True` is the request; the mtime assertion below is the proof."""
		self.run_backup(self.make_dump())
		self.assertTrue(self.new_backup_mock.call_args.kwargs["force"])

	@refusal_guard
	def test_a_dump_older_than_the_job_is_refused_before_anything_uploads(self):
		dump = self.make_dump(db_age_seconds=7200)

		with self.assertRaises(BackupFreshnessError):
			self.run_backup(dump)

		self.assertEqual(self.bucket.uploads, [], "a stale dump must never reach the bucket")
		self.assertEqual(self.last_log().status, "Failed")

	@refusal_guard
	def test_freshness_is_checked_on_every_artifact_not_only_the_database(self):
		"""The database is fresh; the site config was handed back from an earlier run."""
		dump = self.make_dump(conf_age_seconds=7200)

		with self.assertRaises(BackupFreshnessError):
			self.run_backup(dump)

		self.assertEqual(self.bucket.uploads, [])

	def test_a_missing_database_dump_fails_the_run(self):
		dump = self.make_dump()
		os.remove(dump.backup_path_db)

		with self.assertRaises(BackupFreshnessError):
			self.run_backup(dump)

		self.assertEqual(self.bucket.uploads, [])
		self.assertEqual(self.last_log().status, "Failed")

	def test_the_job_start_is_floored_to_the_second(self):
		"""A filesystem with one-second mtime granularity must not fail a fresh dump."""
		with patch("time.time", return_value=1_800_000_000.75):
			self.assertEqual(tasks.job_start_epoch(), 1_800_000_000)

	def test_an_artifact_written_during_the_job_passes_the_assertion(self):
		path = self.temp_artifact("fresh.sql.gz", b"x", mtime=time.time())
		tasks.assert_artifact_is_fresh(path, tasks.job_start_epoch() - 1, kind="database")

	def test_an_artifact_written_before_the_job_fails_the_assertion(self):
		path = self.temp_artifact("stale.sql.gz", b"x", mtime=time.time() - 3600)
		with self.assertRaises(BackupFreshnessError):
			tasks.assert_artifact_is_fresh(path, tasks.job_start_epoch(), kind="database")


class TestBackupVerification(BackupJobTestCase):
	"""A5 — the bucket holds exactly the bytes we sent, and a failure keeps them."""

	def test_uploads_use_an_explicit_transfer_config_matching_the_threshold(self):
		from boto3.s3.transfer import TransferConfig

		self.run_backup(self.make_dump())

		config = self.bucket.uploads[0]["config"]
		self.assertIsInstance(config, TransferConfig)
		self.assertEqual(config.multipart_threshold, self.MULTIPART_THRESHOLD_MB * 1024 * 1024)

	def test_head_object_is_called_with_checksum_mode_enabled(self):
		self.run_backup(self.make_dump())
		head = self.bucket.head_object(TEST_BACKUP_BUCKET, self.bucket.uploads[0]["key"], "ENABLED")
		self.assertEqual(head["ChecksumMode"], "ENABLED")
		source = inspect.getsource(tasks.upload_and_verify)
		self.assertIn('ChecksumMode="ENABLED"', source)

	def test_uploads_carry_a_sha256_checksum_and_never_an_acl(self):
		self.run_backup(self.make_dump())
		self.assertTrue(self.bucket.uploads, "nothing uploaded, so nothing was asserted")
		for upload in self.bucket.uploads:
			self.assertEqual(upload["extra_args"]["ChecksumAlgorithm"], "SHA256")
			self.assertNotIn("ACL", upload["extra_args"])
			self.assertIn("ServerSideEncryption", upload["extra_args"])

	@refusal_guard
	def test_a_size_mismatch_fails_verification_and_keeps_the_remote_artifact(self):
		self.bucket.injectors["report_size"] = 1

		with self.assertRaises(BackupVerificationError):
			self.run_backup(self.make_dump())

		self.assertEqual(self.bucket.deleted, [], "A5: the remote artifact is never deleted")
		self.assertTrue(self.bucket.objects, "the artifact must still be in the bucket")
		self.assertEqual(self.last_log().status, "Failed")

	@refusal_guard
	def test_a_checksum_mismatch_fails_verification_and_keeps_the_remote_artifact(self):
		"""The bucket quietly stored different bytes; HEAD reports what it really holds."""
		self.bucket.injectors["store_instead"] = b"not the dump at all"

		with self.assertRaises(BackupVerificationError):
			self.run_backup(self.make_dump())

		self.assertEqual(self.bucket.deleted, [])
		self.assertEqual(self.last_log().status, "Failed")

	def test_a_large_artifact_is_verified_by_a_streamed_reget(self):
		set_backup_settings(include_site_config=0)
		big = os.urandom(2 * 1024 * 1024)
		self.bucket.injectors["composite_checksum"] = True

		log_name = self.run_backup(self.make_dump(db_content=big))

		manifest = frappe.parse_json(frappe.db.get_value(BACKUP_LOG_DOCTYPE, log_name, "sha256_manifest"))
		strategies = {entry["verified_by"] for key, entry in manifest.items() if entry["kind"] == "database"}
		self.assertEqual(strategies, {"streamed_reget"})

	@refusal_guard
	def test_a_lying_head_on_a_large_artifact_is_caught_by_the_reget(self):
		"""The strongest case A5 exists for: correct metadata over corrupt bytes.

		A multipart object's `ChecksumSHA256` is a checksum-of-checksums, so it can never be
		compared to the whole-object digest. If the re-GET were skipped, this upload would be
		recorded as a verified backup of bytes that are not the database.

		The site config is switched off so the database artifact is the ONLY upload in the
		run. With it on, the injected `report_size` also mismatches the small config artifact,
		and this test would pass on the config's size check while the database sailed
		through — green for a reason that has nothing to do with the property named.
		"""
		set_backup_settings(include_site_config=0)
		big = os.urandom(2 * 1024 * 1024)
		self.bucket.injectors["report_checksum_of"] = big  # HEAD claims the right digest ...
		self.bucket.injectors["store_instead"] = b"corrupt"  # ... over the wrong bytes
		self.bucket.injectors["report_size"] = len(big)  # ... and the right length

		with self.assertRaises(BackupVerificationError):
			self.run_backup(self.make_dump(db_content=big))

		self.assertEqual(len(self.bucket.uploads), 1, "only the database artifact may be in play")
		self.assertEqual(self.bucket.deleted, [])
		self.assertEqual(self.last_log().status, "Failed")

	def test_a_small_artifact_is_verified_by_the_head_checksum(self):
		log_name = self.run_backup(self.make_dump())

		manifest = frappe.parse_json(frappe.db.get_value(BACKUP_LOG_DOCTYPE, log_name, "sha256_manifest"))
		self.assertTrue(manifest, "an empty manifest would make the next assertion vacuous")
		self.assertEqual({entry["verified_by"] for entry in manifest.values()}, {"head_checksum"})

	@refusal_guard
	def test_a_failed_verification_is_never_logged_success(self):
		self.bucket.injectors["store_instead"] = b"corrupt"

		with self.assertRaises(BackupVerificationError):
			self.run_backup(self.make_dump())

		statuses = frappe.get_all(
			BACKUP_LOG_DOCTYPE, filters={"name": ("in", self._log_names)}, pluck="status"
		)
		self.assertTrue(statuses, "no log row was created, so the next assertion proves nothing")
		self.assertNotIn("Success", statuses)
		self.assertEqual(frappe.db.get_single_value("Cloud Backup Settings", "last_backup_status"), "Failed")

	def test_the_manifest_records_a_digest_and_size_for_every_artifact(self):
		log_name = self.run_backup(self.make_dump())
		log = frappe.get_doc(BACKUP_LOG_DOCTYPE, log_name)
		manifest = frappe.parse_json(log.sha256_manifest)

		self.assertEqual(set(manifest), {log.db_key, log.config_key})
		for entry in manifest.values():
			self.assertEqual(len(entry["sha256"]), 64)
			self.assertGreater(entry["size"], 0)
		self.assertEqual(log.total_bytes, sum(entry["size"] for entry in manifest.values()))


class TestBackupNeverDeletes(BackupJobTestCase):
	"""The lint half of A5: the module has no deletion path at all."""

	@refusal_guard
	def test_the_backup_module_contains_no_delete_call(self):
		source = inspect.getsource(tasks)
		for forbidden in ("delete_object", "abort_multipart_upload", "os.remove", "os.unlink"):
			self.assertNotIn(forbidden, source, f"{forbidden} must not appear in backup/tasks.py")

	@refusal_guard
	def test_a_head_failure_after_a_successful_upload_keeps_the_remote_artifact(self):
		"""The path the grep gate covers and no behavioural test did.

		The bytes are already in the bucket when `head_object` raises, so this is the failure
		mode where a "tidy up what we could not verify" reflex would destroy a good artifact
		on a transient error. Nothing deletes; the exception propagates; the run is Failed.
		"""
		self.bucket.injectors["fail_head_with"] = RuntimeError("bucket unreachable during HEAD")

		with self.assertRaises(RuntimeError):
			self.run_backup(self.make_dump())

		self.assertTrue(self.bucket.objects, "the artifact reached the bucket before HEAD failed")
		self.assertEqual(self.bucket.deleted, [], "A5: an unverifiable artifact is still kept")
		self.assertEqual(self.last_log().status, "Failed")

	@refusal_guard
	def test_an_upload_failure_deletes_nothing(self):
		"""Nothing was stored, and nothing is reached for either."""
		self.bucket.injectors["fail_upload_with"] = RuntimeError("connection reset during PUT")

		with self.assertRaises(RuntimeError):
			self.run_backup(self.make_dump())

		self.assertEqual(self.bucket.objects, {})
		self.assertEqual(self.bucket.deleted, [])
		self.assertEqual(self.last_log().status, "Failed")

	def test_a_head_failure_on_a_large_artifact_also_keeps_it(self):
		"""The multipart branch reaches the re-GET only after HEAD; it must fail the same way."""
		set_backup_settings(include_site_config=0)
		self.bucket.injectors["fail_head_with"] = RuntimeError("bucket unreachable during HEAD")

		with self.assertRaises(RuntimeError):
			self.run_backup(self.make_dump(db_content=os.urandom(2 * 1024 * 1024)))

		self.assertTrue(self.bucket.objects)
		self.assertEqual(self.bucket.deleted, [])

	@refusal_guard
	def test_a_reget_failure_keeps_the_artifact_too(self):
		"""The strongest verification step is also a step that can fail on transport.

		Driven through the `fail_get_with` injector rather than by monkeypatching
		`get_object`: a test that reaches around the fake's own failure surface is invisible
		to the registry check below, which is how this path sat outside it until the gate.
		"""
		set_backup_settings(include_site_config=0)
		self.bucket.injectors["fail_get_with"] = RuntimeError("connection reset during re-GET")

		with self.assertRaises(RuntimeError):
			self.run_backup(self.make_dump(db_content=os.urandom(2 * 1024 * 1024)))

		self.assertTrue(self.bucket.objects)
		self.assertEqual(self.bucket.deleted, [])

	def test_every_failure_injector_the_fake_offers_is_exercised(self):
		"""An injector nothing drives is a failure path nothing tests.

		`fail_head_with` and `fail_upload_with` sat on `FakeBackupBucket` unused until this
		class was written: the capability to prove "the artifact survives a transport error"
		existed and proved nothing, which is the same shape as a guard that cannot fire.

		The list comes from `FakeBackupBucket.FAILURE_INJECTORS`, which is the registry every
		injection site reads — so an injector cannot be *used* without appearing here, and
		`InjectorRegistry` raises on an unregistered key rather than binding a value nothing
		consults. The first version discovered by attribute-name prefix, which was itself a
		restated list and would have missed an injector named `raise_on_get`: a guard against
		stale restated lists, resting on one.
		"""
		from pathlib import Path

		import cloud_file_storage
		from cloud_file_storage.tests.backup_utils import FakeBackupBucket

		suite = Path(cloud_file_storage.__file__).parent / "tests" / "test_backup.py"
		source = suite.read_text(encoding="utf-8")

		injectors = list(FakeBackupBucket.FAILURE_INJECTORS)
		self.assertGreaterEqual(len(injectors), 7, "no injectors were discovered; this is vacuous")
		for injector in injectors:
			with self.subTest(injector=injector):
				self.assertIn(
					f'self.bucket.injectors["{injector}"] =',
					source,
					f"{injector} is registered but no test drives it",
				)

	def test_an_unregistered_injector_fails_loudly_instead_of_being_ignored(self):
		"""What makes the registry the mechanism rather than a description of one.

		Setting an injector the fake does not implement used to bind an attribute nothing
		read: the test would then pass or fail for reasons unrelated to the failure it
		believed it had injected. It now raises at the point of use.
		"""
		from cloud_file_storage.tests.backup_utils import FakeBackupBucket

		with self.assertRaises(KeyError):
			FakeBackupBucket().injectors["raise_on_get"] = RuntimeError("boom")

	def test_every_registered_injector_is_read_by_an_injection_site(self):
		"""A registered injector nothing consults is inert, and inert is indistinguishable
		from absent to the test that sets it."""
		from pathlib import Path

		import cloud_file_storage
		from cloud_file_storage.tests.backup_utils import FakeBackupBucket

		fake_source = (Path(cloud_file_storage.__file__).parent / "tests" / "backup_utils.py").read_text(
			encoding="utf-8"
		)

		for injector in FakeBackupBucket.FAILURE_INJECTORS:
			with self.subTest(injector=injector):
				self.assertIn(
					f'self.injectors["{injector}"]',
					fake_source,
					f"{injector} is registered but no injection site reads it",
				)

	@refusal_guard
	def test_upload_and_verify_raises_without_touching_the_bucket_again(self):
		"""Called directly, so the assertion is about the function, not about the job."""
		path = self.temp_artifact("direct.bin", b"hello world", mtime=time.time())
		self.bucket.injectors["store_instead"] = b"different"

		with self.assertRaises(BackupVerificationError):
			tasks.upload_and_verify(
				self.bucket, TEST_BACKUP_BUCKET, "k/direct.bin", path, {}, threshold_bytes=1024
			)

		self.assertEqual(self.bucket.deleted, [])
		self.assertIn("k/direct.bin", self.bucket.objects)


class TestBackupTimeout(BackupJobTestCase):
	"""A job killed at its timeout re-enqueues itself, boundedly (design §2.2)."""

	def run_until_timeout(self, *, attempt: int = 0):
		from rq.timeouts import JobTimeoutException

		dump = self.make_dump()

		def produce(**kwargs):
			dump.stamp()
			raise JobTimeoutException("job exceeded its timeout")

		with patch("frappe.utils.backups.new_backup", side_effect=produce):
			with patch("frappe.enqueue") as enqueue:
				with self.assertRaises(JobTimeoutException):
					tasks.take_cloud_backup(attempt=attempt)
		self.track_new_logs()
		return enqueue

	def test_a_timeout_re_enqueues_the_job_with_the_next_attempt_number(self):
		enqueue = self.run_until_timeout(attempt=0)

		self.assertTrue(enqueue.called)
		self.assertEqual(enqueue.call_args.kwargs["attempt"], 1)

	def test_the_re_enqueue_is_bounded(self):
		"""A job that times out three times is a condition to see, not a loop to hide in."""
		self.assertFalse(self.run_until_timeout(attempt=tasks.MAX_TIMEOUT_RETRIES).called)

	def test_a_timeout_is_logged_failed_and_never_success(self):
		self.run_until_timeout()

		self.assertEqual(self.last_log().status, "Failed")
		self.assertIn("timed out", self.last_log().error)

	def test_a_timeout_uploads_nothing(self):
		self.run_until_timeout()
		self.assertEqual(self.bucket.uploads, [])


class TestBackupKeyLayout(BackupJobTestCase):
	"""A20's per-frequency prefixes, seen from the writer's side."""

	def test_artifacts_land_under_the_configured_frequency_prefix(self):
		set_backup_settings(frequency="Hourly")

		log_name = self.run_backup(self.make_dump())

		db_key = frappe.db.get_value(BACKUP_LOG_DOCTYPE, log_name, "db_key")
		self.assertIn("/hourly/", db_key)
		self.assertIn(frappe.local.site, db_key)

	def test_a_manual_backup_lands_under_the_manual_prefix(self):
		log_name = self.run_backup(self.make_dump(), trigger="manual")

		db_key = frappe.db.get_value(BACKUP_LOG_DOCTYPE, log_name, "db_key")
		self.assertIn("/manual/", db_key)
		self.assertEqual(frappe.db.get_value(BACKUP_LOG_DOCTYPE, log_name, "trigger"), "manual")

	def test_the_key_carries_the_dump_timestamp_so_a_rerun_overwrites(self):
		first = self.run_backup(self.make_dump(timestamp="20260816_020000"))
		first_key = frappe.db.get_value(BACKUP_LOG_DOCTYPE, first, "db_key")
		self.assertIn("/20260816_020000/", first_key)

		second = self.run_backup(self.make_dump(timestamp="20260816_020000"))
		self.assertEqual(frappe.db.get_value(BACKUP_LOG_DOCTYPE, second, "db_key"), first_key)

	def test_file_tarballs_are_uploaded_only_when_their_checkbox_is_on(self):
		set_backup_settings(include_public_files=1)

		log_name = self.run_backup(self.make_dump(public_content=b"tarball", private_content=b"priv"))

		log = frappe.get_doc(BACKUP_LOG_DOCTYPE, log_name)
		self.assertTrue(log.public_files_key)
		self.assertFalse(log.private_files_key, "private files were not enabled")

	def test_the_site_config_is_skipped_when_its_checkbox_is_off(self):
		set_backup_settings(include_site_config=0)

		log_name = self.run_backup(self.make_dump())

		self.assertFalse(frappe.db.get_value(BACKUP_LOG_DOCTYPE, log_name, "config_key"))
		self.assertEqual(len(self.bucket.uploads), 1)


class TestBackupRefusals(BackupJobTestCase):
	"""Guards that must fire. Each is driven with the input that should trip it."""

	@refusal_guard
	def test_a_backup_is_refused_when_the_backup_bucket_is_the_attachment_bucket(self):
		from cloud_file_storage.tests.utils import TEST_BUCKET

		set_backup_settings(backup_bucket=TEST_BUCKET)

		with self.assertRaises(frappe.ValidationError):
			tasks.take_cloud_backup()

		self.assertEqual(self.bucket.uploads, [])

	def test_a_backup_is_refused_while_backups_are_disabled(self):
		set_backup_settings(enabled=0)

		with self.assertRaises(frappe.ValidationError):
			tasks.take_cloud_backup()

	def test_a_backup_is_refused_without_a_bucket(self):
		set_backup_settings(backup_bucket="")

		with self.assertRaises(frappe.ValidationError):
			tasks.take_cloud_backup()

	def test_backup_now_is_system_manager_only(self):
		"""`frappe.only_for` is a deliberate no-op under `in_test`, so assert it structurally."""
		tree = ast.parse(inspect.getsource(tasks.backup_now.__wrapped__))
		calls = [
			node
			for node in ast.walk(tree)
			if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "only_for"
		]
		self.assertTrue(calls, "backup_now must call frappe.only_for")
		self.assertEqual(calls[0].args[0].value, "System Manager")


class TestTheHarnessRestoresExactly(BackupTestCase):
	"""The suite's own "left the site as it found it" contract.

	This is here because it failed: the first version of `snapshot_backup_settings` read
	through `frappe.db.get_single_value`, which casts, and `cast("Datetime", None)` is
	`datetime.min`. Restoring that wrote the string `0001-01-01T00:00:00`, reading it back
	gave `None`, and the stored bytes alternated between consecutive full-suite runs — a
	leak in the very helper whose job is to prevent leaks.
	"""

	DOCTYPE = "Cloud Backup Settings"

	def raw(self, fieldname):
		rows = frappe.db.sql(
			"select value from tabSingles where doctype = %s and field = %s",
			(self.DOCTYPE, fieldname),
		)
		return rows[0][0] if rows else None

	def row_exists(self, fieldname) -> bool:
		return bool(
			frappe.db.sql(
				"select 1 from tabSingles where doctype = %s and field = %s",
				(self.DOCTYPE, fieldname),
			)
		)

	def test_a_null_datetime_survives_a_snapshot_and_restore_unchanged(self):
		from cloud_file_storage.tests.backup_utils import (
			restore_backup_settings,
			snapshot_backup_settings,
		)

		frappe.db.set_single_value(self.DOCTYPE, {"last_backup_on": None})
		frappe.db.commit()
		before = self.raw("last_backup_on")

		snapshot = snapshot_backup_settings()
		frappe.db.set_single_value(self.DOCTYPE, {"last_backup_on": now_datetime()})
		frappe.db.commit()
		self.assertNotEqual(self.raw("last_backup_on"), before, "the mutation did not take")

		restore_backup_settings(snapshot)

		self.assertEqual(self.raw("last_backup_on"), before)

	def test_a_field_with_no_row_still_has_no_row_afterwards(self):
		from cloud_file_storage.tests.backup_utils import (
			restore_backup_settings,
			snapshot_backup_settings,
		)

		frappe.db.delete("Singles", {"doctype": self.DOCTYPE, "field": "notify_email"})
		frappe.db.commit()
		self.assertFalse(self.row_exists("notify_email"))

		snapshot = snapshot_backup_settings()
		frappe.db.set_single_value(self.DOCTYPE, {"notify_email": "ops@example.invalid"})
		frappe.db.commit()
		self.assertTrue(self.row_exists("notify_email"), "the mutation did not take")

		restore_backup_settings(snapshot)

		self.assertFalse(self.row_exists("notify_email"))

	def test_the_snapshot_covers_every_field_the_backup_doctype_has(self):
		"""A managed-field list that misses a field is a place a leak hides."""
		from cloud_file_storage.tests.backup_utils import MANAGED_BACKUP_SETTINGS_FIELDS

		stored = {
			field.fieldname
			for field in frappe.get_meta(self.DOCTYPE).fields
			if field.fieldtype not in ("Section Break", "Column Break", "Button", "HTML")
		}
		self.assertTrue(stored, "no fields were read; the comparison would be vacuous")
		missing = stored - set(MANAGED_BACKUP_SETTINGS_FIELDS) - {"secret_key"}
		self.assertEqual(missing, set(), f"unmanaged Cloud Backup Settings fields: {missing}")


class TestBackupScheduling(BackupTestCase):
	"""B3 — one hourly dispatcher that self-gates."""

	def enqueued(self, **fields):
		set_backup_settings(**fields)
		with patch("frappe.enqueue") as enqueue:
			tasks.run_scheduled_backup()
		return enqueue

	def test_the_dispatcher_enqueues_nothing_while_backups_are_disabled(self):
		self.assertFalse(self.enqueued(enabled=0, last_backup_on=None).called)

	def test_the_dispatcher_enqueues_when_a_backup_is_due(self):
		enqueue = self.enqueued(frequency="Hourly", last_backup_on=add_to_date(now_datetime(), hours=-3))
		self.assertTrue(enqueue.called)
		self.assertEqual(enqueue.call_args.kwargs["queue"], tasks.BACKUP_QUEUE)
		self.assertEqual(enqueue.call_args.kwargs["job_id"], tasks.BACKUP_JOB_ID)

	def test_the_dispatcher_enqueues_nothing_when_the_last_backup_is_recent(self):
		enqueue = self.enqueued(frequency="Hourly", last_backup_on=add_to_date(now_datetime(), minutes=-5))
		self.assertFalse(enqueue.called)

	def test_hourly_is_due_an_hour_after_the_last_one(self):
		settings = frappe._dict(frequency="Hourly", last_backup_on="2026-08-16 01:00:00")
		self.assertTrue(tasks.is_backup_due(settings, get_datetime("2026-08-16 02:00:00")))
		self.assertFalse(tasks.is_backup_due(settings, get_datetime("2026-08-16 01:30:00")))

	def test_daily_waits_for_the_configured_hour(self):
		settings = frappe._dict(frequency="Daily", backup_hour=3, last_backup_on=None)
		self.assertFalse(tasks.is_backup_due(settings, get_datetime("2026-08-16 02:59:00")))
		self.assertTrue(tasks.is_backup_due(settings, get_datetime("2026-08-16 03:01:00")))

	def test_daily_does_not_run_twice_in_one_day(self):
		settings = frappe._dict(frequency="Daily", backup_hour=1, last_backup_on="2026-08-16 01:05:00")
		self.assertFalse(tasks.is_backup_due(settings, get_datetime("2026-08-16 23:00:00")))
		self.assertTrue(tasks.is_backup_due(settings, get_datetime("2026-08-17 01:05:00")))

	def test_a_daily_window_missed_by_a_paused_scheduler_still_runs_later(self):
		"""The gate is "this slot has not been backed up", not "the clock reads 01:00"."""
		settings = frappe._dict(frequency="Daily", backup_hour=1, last_backup_on="2026-08-13 01:05:00")
		self.assertTrue(tasks.is_backup_due(settings, get_datetime("2026-08-16 22:00:00")))

	def test_weekly_runs_on_the_configured_weekday_only(self):
		settings = frappe._dict(
			frequency="Weekly", backup_hour=1, backup_weekday="Monday", last_backup_on=None
		)
		# 2026-08-17 is a Monday; 2026-08-18 is a Tuesday.
		self.assertTrue(tasks.is_backup_due(settings, get_datetime("2026-08-17 02:00:00")))
		self.assertFalse(tasks.is_backup_due(settings, get_datetime("2026-08-18 02:00:00")))


class TestBackupLogRetention(BackupTestCase):
	def make_log(self, *, age_days: int, status: str = "Success") -> str:
		doc = frappe.new_doc(BACKUP_LOG_DOCTYPE)
		doc.update({"status": status, "trigger": "manual", "started_at": now_datetime()})
		doc.insert(ignore_permissions=True)
		frappe.db.set_value(
			BACKUP_LOG_DOCTYPE,
			doc.name,
			"creation",
			add_days(now_datetime(), -age_days),
			update_modified=False,
		)
		frappe.db.commit()
		return self.track_log(doc.name)

	def test_logs_past_the_retention_window_are_purged_and_newer_ones_kept(self):
		old = self.make_log(age_days=200)
		recent = self.make_log(age_days=3)
		set_backup_settings(log_retention_days=90)

		tasks.purge_backup_logs()

		self.assertFalse(frappe.db.exists(BACKUP_LOG_DOCTYPE, old))
		self.assertTrue(frappe.db.exists(BACKUP_LOG_DOCTYPE, recent))

	def test_a_zero_retention_window_keeps_everything(self):
		old = self.make_log(age_days=900)
		set_backup_settings(log_retention_days=0)

		tasks.purge_backup_logs()

		self.assertTrue(frappe.db.exists(BACKUP_LOG_DOCTYPE, old))


class TestBackupAgainstTheRealGenerator(BackupTestCase):
	"""One end-to-end run through frappe's own `new_backup`.

	Everything above fakes the generator, which means everything above would still pass if
	`new_backup`'s keyword arguments had drifted. This one proves the integration: a real
	mysqldump, real artifact paths, and the A28 assertion applied to a dump this process
	genuinely produced a moment ago.
	"""

	def test_a_real_dump_is_produced_uploaded_and_verified(self):
		try:
			log_name = tasks.take_cloud_backup(trigger="manual")
		finally:
			self.track_new_logs()

		log = frappe.get_doc(BACKUP_LOG_DOCTYPE, log_name)
		self.addCleanup(self._remove_real_artifacts, log.frappe_backup_path, log.sha256_manifest)

		self.assertEqual(log.status, "Success")
		self.assertGreater(log.total_bytes, 0)
		manifest = frappe.parse_json(log.sha256_manifest)
		self.assertIn("database", {entry["kind"] for entry in manifest.values()})
		for key, entry in manifest.items():
			self.assertIn(key, self.bucket.objects)
			self.assertEqual(len(self.bucket.objects[key]), entry["size"])

	@staticmethod
	def _remove_real_artifacts(backup_path, manifest_json):
		"""Test hygiene: the dump this test asked frappe to write is not the site's backup."""
		if not backup_path:
			return
		for key in frappe.parse_json(manifest_json or "{}"):
			candidate = os.path.join(backup_path, key.rsplit("/", 1)[-1])
			if os.path.exists(candidate):
				os.remove(candidate)
