"""T-BACKUP (restore) — A27: the restore window, its coupling, and the pre-restore check.

The single most consequential assertion in this file is the last one: an artifact that does
not match the Backup Log is refused **before** `bench restore` gets near it, because once a
restore starts the thing you would have compared against no longer exists.
"""

import ast
import inspect

import frappe
from frappe.utils import now_datetime

from cloud_file_storage.backup import restore, retention
from cloud_file_storage.backup.exceptions import BackupVerificationError
from cloud_file_storage.tests.backup_utils import BackupTestCase
from cloud_file_storage.tests.markers import refusal_guard
from cloud_file_storage.tests.utils import set_mode

BACKUP_LOG_DOCTYPE = "Cloud Storage Backup Log"


class TestRestoreWindowCoupling(BackupTestCase):
	"""A27: the grace period must cover the window it promises."""

	def settings_doc(self, *, grace: int, window: int):
		return frappe._dict(object_delete_grace_days=grace, restore_window_days=window)

	@refusal_guard
	def test_a_grace_shorter_than_the_window_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			retention.assert_grace_covers_restore_window(self.settings_doc(grace=7, window=30))

	def test_an_equal_grace_is_accepted(self):
		retention.assert_grace_covers_restore_window(self.settings_doc(grace=30, window=30))

	def test_a_longer_grace_is_accepted(self):
		retention.assert_grace_covers_restore_window(self.settings_doc(grace=60, window=30))

	def test_a_zero_window_promises_nothing_and_is_accepted(self):
		retention.assert_grace_covers_restore_window(self.settings_doc(grace=1, window=0))

	def test_the_shipped_defaults_satisfy_the_coupling(self):
		"""Supersession S1 raised the grace default to 30 precisely so this holds."""
		import json
		from pathlib import Path

		import cloud_file_storage

		schema = json.loads(
			(
				Path(cloud_file_storage.__file__).parent
				/ "cloud_file_storage"
				/ "doctype"
				/ "cloud_storage_settings"
				/ "cloud_storage_settings.json"
			).read_text()
		)
		defaults = {field["fieldname"]: field.get("default") for field in schema["fields"]}
		self.assertGreaterEqual(
			int(defaults["object_delete_grace_days"]), int(defaults["restore_window_days"])
		)

	def test_the_settings_validator_runs_the_coupling_check(self):
		from cloud_file_storage.cloud_file_storage.doctype.cloud_storage_settings.cloud_storage_settings import (
			CloudStorageSettings,
		)

		doc = frappe.get_doc(
			{
				"doctype": "Cloud Storage Settings",
				"object_delete_grace_days": 7,
				"restore_window_days": 30,
			}
		)
		with self.assertRaises(frappe.ValidationError):
			CloudStorageSettings.validate_restore_window(doc)


class TestAttachmentBucketVersioning(BackupTestCase):
	"""A27's other half: versioning is what makes an out-of-band delete recoverable."""

	def setUp(self):
		super().setUp()
		self.storage_bucket = FakeVersionedBucket()
		self._patcher = frappe._dict()
		import unittest.mock

		self._mock = unittest.mock.patch(
			"cloud_file_storage.storage.client.get_client", return_value=self.storage_bucket
		)
		self._mock.start()
		self.addCleanup(self._mock.stop)

	def test_versioning_enabled_with_a_long_enough_noncurrent_rule_passes(self):
		self.storage_bucket.versioning = {"Status": "Enabled"}
		self.storage_bucket.lifecycle = {
			"Rules": [{"Status": "Enabled", "NoncurrentVersionExpiration": {"NoncurrentDays": 45}}]
		}
		step = retention.check_attachment_bucket_versioning()
		self.assertTrue(step["ok"])
		self.assertEqual(step["noncurrent_days"], 45)

	@refusal_guard
	def test_versioning_disabled_is_a_hard_warning(self):
		self.storage_bucket.versioning = {}
		step = retention.check_attachment_bucket_versioning()
		self.assertFalse(step["ok"])
		self.assertEqual(step["severity"], "error")
		self.assertIn("Versioning is not enabled", step["error"])

	def test_a_noncurrent_expiry_shorter_than_the_window_is_a_hard_warning(self):
		self.storage_bucket.versioning = {"Status": "Enabled"}
		self.storage_bucket.lifecycle = {
			"Rules": [{"Status": "Enabled", "NoncurrentVersionExpiration": {"NoncurrentDays": 5}}]
		}
		step = retention.check_attachment_bucket_versioning()
		self.assertFalse(step["ok"])
		self.assertIn("shorter than the 30-day restore window", step["error"])

	def test_the_shortest_noncurrent_rule_wins(self):
		"""Two rules can both match an object; the earliest expiry is the real one."""
		self.storage_bucket.versioning = {"Status": "Enabled"}
		self.storage_bucket.lifecycle = {
			"Rules": [
				{"Status": "Enabled", "NoncurrentVersionExpiration": {"NoncurrentDays": 90}},
				{"Status": "Enabled", "NoncurrentVersionExpiration": {"NoncurrentDays": 3}},
			]
		}
		step = retention.check_attachment_bucket_versioning()
		self.assertEqual(step["noncurrent_days"], 3)
		self.assertFalse(step["ok"])

	def test_a_disabled_rule_is_ignored(self):
		self.storage_bucket.versioning = {"Status": "Enabled"}
		self.storage_bucket.lifecycle = {
			"Rules": [{"Status": "Disabled", "NoncurrentVersionExpiration": {"NoncurrentDays": 1}}]
		}
		step = retention.check_attachment_bucket_versioning()
		self.assertTrue(step["ok"])

	def test_no_lifecycle_configuration_means_versions_are_kept_for_ever(self):
		self.storage_bucket.versioning = {"Status": "Enabled"}
		self.storage_bucket.lifecycle = None
		step = retention.check_attachment_bucket_versioning()
		self.assertTrue(step["ok"])
		self.assertIsNone(step["noncurrent_days"])

	def test_a_shorter_restore_window_relaxes_the_requirement(self):
		set_mode("S3_ONLY", restore_window_days=2, object_delete_grace_days=30)
		self.storage_bucket.versioning = {"Status": "Enabled"}
		self.storage_bucket.lifecycle = {
			"Rules": [{"Status": "Enabled", "NoncurrentVersionExpiration": {"NoncurrentDays": 5}}]
		}
		self.assertTrue(retention.check_attachment_bucket_versioning()["ok"])

	@refusal_guard
	def test_test_connection_reports_the_versioning_step(self):
		from cloud_file_storage.api import compat

		self.storage_bucket.versioning = {}
		report = compat.test_connection()

		steps = {step["step"]: step for step in report["steps"]}
		self.assertIn(retention.VERSIONING_STEP, steps)
		self.assertFalse(steps[retention.VERSIONING_STEP]["ok"])

	def test_test_connection_reports_a_noncurrent_expiry_shorter_than_the_window(self):
		"""The SECOND entry point for A27's second half — the one the merge nearly lost.

		The test above drives the versioning-OFF branch, which returns before the lifecycle
		read is ever reached. So until this existed, no test in the tree drove the
		noncurrent-expiration comparison through an operator-facing surface: reverting
		`diagnostics._check_versioning` to a status-only step left the entire suite green.
		Versioning ON here, so the comparison is the only thing that can fail it.
		"""
		from cloud_file_storage.api import compat

		self.storage_bucket.versioning = {"Status": "Enabled"}
		self.storage_bucket.lifecycle = {
			"Rules": [{"Status": "Enabled", "NoncurrentVersionExpiration": {"NoncurrentDays": 5}}]
		}
		report = compat.test_connection()

		steps = {step["step"]: step for step in report["steps"]}
		self.assertIn(retention.VERSIONING_STEP, steps)
		self.assertFalse(
			steps[retention.VERSIONING_STEP]["ok"],
			"a 5-day noncurrent expiry cannot honour the restore window, and the "
			"operator-facing step must say so",
		)
		self.assertIn("5", steps[retention.VERSIONING_STEP]["error"])

	def test_test_connection_passes_when_the_noncurrent_rule_covers_the_window(self):
		"""The other direction, so the test above cannot pass by rejecting everything."""
		from cloud_file_storage.api import compat

		self.storage_bucket.versioning = {"Status": "Enabled"}
		self.storage_bucket.lifecycle = {
			"Rules": [{"Status": "Enabled", "NoncurrentVersionExpiration": {"NoncurrentDays": 400}}]
		}
		steps = {step["step"]: step for step in compat.test_connection()["steps"]}
		self.assertTrue(steps[retention.VERSIONING_STEP]["ok"])


class FakeVersionedBucket:
	"""Minimal attachment-bucket client for the A27 versioning probe."""

	def __init__(self):
		self.versioning = {"Status": "Enabled"}
		self.lifecycle = None

	def head_bucket(self, Bucket):
		return {}

	def get_bucket_versioning(self, Bucket):
		return dict(self.versioning)

	def get_bucket_lifecycle_configuration(self, Bucket):
		if self.lifecycle is None:
			from cloud_file_storage.tests.backup_utils import no_such_lifecycle_error

			raise no_such_lifecycle_error()
		return {"Rules": list(self.lifecycle["Rules"])}


class RestoreTestCase(BackupTestCase):
	def make_success_log(self, manifest: dict, *, db_key: str | None = None) -> str:
		import json

		doc = frappe.new_doc(BACKUP_LOG_DOCTYPE)
		doc.update(
			{
				"status": "Success",
				"trigger": "manual",
				"backup_bucket": "cfs-unit-test-backups",
				"db_key": db_key or next(iter(manifest)),
				"total_bytes": sum(entry["size"] for entry in manifest.values()),
				"sha256_manifest": json.dumps(manifest),
			}
		)
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		return self.track_log(doc.name)


class TestArtifactVerificationBeforeRestore(RestoreTestCase):
	"""The runbook's mandatory step, implemented as code so it cannot be skipped by prose."""

	def artifact(self, content: bytes, name: str = "20260816_010000-cfs-database.sql.gz") -> str:
		import time

		return self.temp_artifact(name, content, mtime=time.time())

	def test_a_matching_artifact_verifies(self):
		content = b"-- a database dump\n"
		path = self.artifact(content)
		key = "site/backups/daily/20260816_010000/20260816_010000-cfs-database.sql.gz"
		import hashlib

		log = self.make_success_log(
			{key: {"kind": "database", "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}}
		)

		result = restore.verify_downloaded_artifact(path, key)

		self.assertTrue(result["verified"])
		self.assertEqual(result["backup_log"], log)

	def test_a_truncated_artifact_is_refused(self):
		import hashlib

		content = b"-- a database dump\n"
		key = "site/backups/daily/x/db.sql.gz"
		self.make_success_log(
			{key: {"kind": "database", "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}}
		)
		path = self.artifact(content[:5])

		with self.assertRaises(BackupVerificationError) as caught:
			restore.verify_downloaded_artifact(path, key)
		self.assertIn("DO NOT restore", str(caught.exception))

	@refusal_guard
	def test_a_corrupted_artifact_of_the_right_length_is_refused(self):
		"""Size alone is not a check: same length, different bytes."""
		import hashlib

		content = b"-- a database dump\n"
		key = "site/backups/daily/x/db.sql.gz"
		self.make_success_log(
			{key: {"kind": "database", "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}}
		)
		path = self.artifact(b"-- a corrupt dump!\n")

		with self.assertRaises(BackupVerificationError):
			restore.verify_downloaded_artifact(path, key)

	def test_an_artifact_no_backup_log_records_is_refused(self):
		path = self.artifact(b"whatever")
		with self.assertRaises(BackupVerificationError) as caught:
			restore.verify_downloaded_artifact(path, "site/backups/daily/unknown/db.sql.gz")
		self.assertIn("nothing to verify this file against", str(caught.exception))

	def test_a_missing_file_is_refused(self):
		with self.assertRaises(BackupVerificationError):
			restore.verify_downloaded_artifact("/nonexistent/db.sql.gz", "k")

	def test_the_key_can_be_recovered_from_the_filename(self):
		"""An operator who downloaded through the browser still has a usable check."""
		import hashlib

		content = b"-- dump\n"
		key = "site/backups/daily/20260816_010000/20260816_010000-cfs-database.sql.gz"
		self.make_success_log(
			{key: {"kind": "database", "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}}
		)
		path = self.artifact(content)

		result = restore.verify_downloaded_artifact(path)

		self.assertEqual(result["key"], key)

	def test_a_failed_backup_is_not_a_source_of_truth(self):
		"""Only a Success row proves the remote artifact was ever verified."""
		import hashlib
		import json

		content = b"-- dump\n"
		key = "site/backups/daily/x/db.sql.gz"
		doc = frappe.new_doc(BACKUP_LOG_DOCTYPE)
		doc.update(
			{
				"status": "Failed",
				"trigger": "manual",
				"sha256_manifest": json.dumps(
					{
						key: {
							"kind": "database",
							"sha256": hashlib.sha256(content).hexdigest(),
							"size": len(content),
						}
					}
				),
			}
		)
		doc.insert(ignore_permissions=True)
		self.track_log(doc.name)
		frappe.db.commit()

		with self.assertRaises(BackupVerificationError):
			restore.verify_downloaded_artifact(self.artifact(content), key)

	@refusal_guard
	def test_verification_never_deletes_the_artifact_it_rejected(self):
		import hashlib
		import os

		content = b"-- a database dump\n"
		key = "site/backups/daily/x/db.sql.gz"
		self.make_success_log(
			{key: {"kind": "database", "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}}
		)
		path = self.artifact(b"corrupt bytes here")

		with self.assertRaises(BackupVerificationError):
			restore.verify_downloaded_artifact(path, key)

		self.assertTrue(os.path.exists(path), "a rejected artifact is evidence, not rubbish")


class TestRestoreHelpers(RestoreTestCase):
	"""Every key these drive is a real, scoped, Backup-Log-recorded artifact.

	It has to be: since H-1 the endpoints refuse anything else, and a test using a bare `k`
	would now be asserting the refusal rather than the behaviour it names.
	"""

	def setUp(self):
		super().setUp()
		self.key = f"{frappe.local.site}/backups/daily/20260816_010000/db.sql.gz"
		self.make_success_log({self.key: {"kind": "database", "sha256": "a" * 64, "size": 10}})
		self.bucket.objects[self.key] = b"x"

	def test_list_backups_returns_rows_with_a_parsed_manifest(self):
		key = "site/backups/daily/x/db.sql.gz"
		self.make_success_log({key: {"kind": "database", "sha256": "a" * 64, "size": 10}})

		rows = restore.list_backups(limit=5)

		self.assertTrue(rows)
		self.assertIn(key, rows[0]["manifest"])

	def test_initiate_restore_sends_the_tier_and_days(self):
		result = restore.initiate_restore(self.key, days=5, tier="Bulk")

		self.assertTrue(result["ok"])
		request = self.bucket.restore_requests[-1]["request"]
		self.assertEqual(request["Days"], 5)
		self.assertEqual(request["GlacierJobParameters"]["Tier"], "Bulk")

	def test_an_unknown_restore_tier_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			restore.initiate_restore(self.key, tier="Instant")
		self.assertEqual(self.bucket.restore_requests, [])

	def test_restore_status_parses_an_in_progress_thaw(self):
		original_head = self.bucket.head_object
		self.bucket.head_object = lambda **kwargs: {
			**original_head(**kwargs),
			"Restore": 'ongoing-request="true"',
		}

		status = restore.restore_status(self.key)

		self.assertTrue(status["ongoing"])
		self.assertFalse(status["ready"])

	def test_restore_status_parses_a_completed_thaw(self):
		original_head = self.bucket.head_object
		self.bucket.head_object = lambda **kwargs: {
			**original_head(**kwargs),
			"Restore": 'ongoing-request="false", expiry-date="Wed, 20 Aug 2026 00:00:00 GMT"',
		}

		status = restore.restore_status(self.key)

		self.assertTrue(status["ready"])
		self.assertIn("20 Aug 2026", status["expiry"])

	def test_a_download_url_is_short_lived(self):
		url = restore.download_url(self.key, ttl=100000)
		self.assertLessEqual(self.bucket.presign_calls[-1]["ttl"], restore.DOWNLOAD_TTL)
		self.assertIn("X-Amz-Expires", url)

	def test_a_download_url_pins_attachment_disposition_and_no_store(self):
		"""The site-config artifact is JSON holding the database password."""
		config_key = f"{frappe.local.site}/backups/daily/20260816_010000/site_config_backup.json"
		self.make_success_log({config_key: {"kind": "site_config", "sha256": "b" * 64, "size": 9}})

		restore.download_url(config_key)

		params = self.bucket.presign_calls[-1]["params"]
		self.assertIn("attachment;", params["ResponseContentDisposition"])
		self.assertIn("site_config_backup.json", params["ResponseContentDisposition"])
		self.assertEqual(params["ResponseCacheControl"], "no-store")

	def test_every_restore_endpoint_is_system_manager_only(self):
		for function in (
			restore.list_backups,
			restore.initiate_restore,
			restore.restore_status,
			restore.download_url,
		):
			with self.subTest(function=function.__name__):
				tree = ast.parse(inspect.getsource(function.__wrapped__))
				calls = [
					node
					for node in ast.walk(tree)
					if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "only_for"
				]
				self.assertTrue(calls, f"{function.__name__} must call frappe.only_for")
				self.assertEqual(calls[0].args[0].value, "System Manager")

	def test_no_desk_endpoint_restores_a_database(self):
		"""`bench restore` drops and recreates the database; it stays on the CLI (design §2.5)."""
		source = inspect.getsource(restore)
		self.assertNotIn("bench restore", source.replace("`bench restore`", ""))
		for name in dir(restore):
			attribute = getattr(restore, name)
			if getattr(attribute, "whitelisted", False):
				self.assertNotIn("restore_database", name)


class TestBackupHealth(BackupTestCase):
	"""Findings the Settings health panel renders (design §2.2b, §2.6).

	Detected here rather than in the panel so the CLI, the panel and any later release
	report cannot disagree about whether a site's backups are healthy.
	"""

	def codes(self, **fields) -> dict:
		from cloud_file_storage.backup import health
		from cloud_file_storage.tests.backup_utils import set_backup_settings

		set_backup_settings(**fields)
		report = health.backup_health()
		return {finding["code"]: finding for finding in report["findings"]}

	def test_disabled_backups_are_reported(self):
		self.assertIn("backups_disabled", self.codes(enabled=0))

	def test_a_site_that_has_never_backed_up_is_reported(self):
		self.assertEqual(self.codes(last_backup_on=None)["no_backup_yet"]["level"], "error")

	def test_a_recent_backup_raises_no_staleness_finding(self):
		from frappe.utils import add_to_date

		codes = self.codes(
			frequency="Daily",
			last_backup_on=add_to_date(now_datetime(), hours=-2),
			last_backup_status="Success",
			lifecycle_applied_hash=self._current_hash(),
		)
		self.assertNotIn("backup_stale", codes)
		self.assertNotIn("no_backup_yet", codes)

	def test_a_backup_older_than_twice_its_interval_is_stale(self):
		from frappe.utils import add_to_date

		codes = self.codes(
			frequency="Daily",
			last_backup_on=add_to_date(now_datetime(), hours=-60),
			last_backup_status="Success",
		)
		self.assertEqual(codes["backup_stale"]["level"], "error")

	def test_the_staleness_window_follows_the_frequency(self):
		from frappe.utils import add_to_date

		three_hours_ago = add_to_date(now_datetime(), hours=-3)
		self.assertIn(
			"backup_stale",
			self.codes(frequency="Hourly", last_backup_on=three_hours_ago, last_backup_status="Success"),
		)
		self.assertNotIn(
			"backup_stale",
			self.codes(frequency="Daily", last_backup_on=three_hours_ago, last_backup_status="Success"),
		)

	def test_a_failed_last_backup_is_reported(self):
		codes = self.codes(last_backup_on=now_datetime(), last_backup_status="Failed")
		self.assertEqual(codes["last_backup_failed"]["level"], "error")

	def test_file_tarballs_after_the_cutover_are_reported(self):
		from cloud_file_storage.tests.utils import set_mode

		set_mode("S3_ONLY")
		codes = self.codes(
			include_public_files=1, last_backup_on=now_datetime(), last_backup_status="Success"
		)
		self.assertIn("tarballs_after_cutover", codes)

	def test_file_tarballs_are_not_reported_on_a_local_only_site(self):
		from cloud_file_storage.tests.utils import set_mode

		set_mode("LOCAL_ONLY")
		codes = self.codes(
			include_public_files=1, last_backup_on=now_datetime(), last_backup_status="Success"
		)
		self.assertNotIn("tarballs_after_cutover", codes)

	def test_a_lifecycle_policy_that_was_never_applied_is_reported(self):
		codes = self.codes(
			lifecycle_applied_hash="", last_backup_on=now_datetime(), last_backup_status="Success"
		)
		self.assertIn("lifecycle_never_applied", codes)

	def test_a_settings_change_since_the_last_apply_is_reported(self):
		applied = self._current_hash()
		codes = self.codes(
			lifecycle_applied_hash=applied,
			delete_after_days=200,
			last_backup_on=now_datetime(),
			last_backup_status="Success",
		)
		self.assertIn("lifecycle_may_have_drifted", codes)

	def test_an_unchanged_policy_raises_no_drift_finding(self):
		codes = self.codes(
			lifecycle_applied_hash=self._current_hash(),
			last_backup_on=now_datetime(),
			last_backup_status="Success",
		)
		self.assertNotIn("lifecycle_may_have_drifted", codes)

	def test_an_enabled_backup_with_no_bucket_is_an_error(self):
		codes = self.codes(backup_bucket="", last_backup_on=now_datetime(), last_backup_status="Success")
		self.assertEqual(codes["no_backup_bucket"]["level"], "error")

	def test_the_health_endpoint_is_system_manager_only(self):
		from cloud_file_storage.backup import health

		tree = ast.parse(inspect.getsource(health.get_backup_health.__wrapped__))
		calls = [
			node
			for node in ast.walk(tree)
			if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "only_for"
		]
		self.assertTrue(calls)
		self.assertEqual(calls[0].args[0].value, "System Manager")

	def _current_hash(self) -> str:
		from cloud_file_storage.backup import lifecycle
		from cloud_file_storage.backup.settings import get_backup_settings

		return lifecycle.policy_hash(lifecycle.build_lifecycle_policy(get_backup_settings()))


class TestDownloadUrlIsScopedAndAudited(RestoreTestCase):
	"""H-1's gate test, cited by C10's allow-list.

	`download_url` is the fourth sanctioned minting site in the product. The other three
	resolve a key to a File row the caller may read and write exactly one access log before
	they sign anything. A backup artifact has no File row and no CSO — it is not an
	attachment, which is why `api/compat.legacy_generate_file` refuses it and why this
	endpoint exists at all — so it reuses that *shape*: scope the key, refuse, record, then
	mint.
	"""

	AUDIT = "Cloud Storage Audit Log"

	def setUp(self):
		super().setUp()
		self.key = f"{frappe.local.site}/backups/daily/20260816_010000/db.sql.gz"
		self.make_success_log({self.key: {"kind": "database", "sha256": "a" * 64, "size": 10}})
		frappe.db.delete(self.AUDIT, {"action": ("like", "backup_presign%")})
		frappe.db.commit()
		self.addCleanup(self._drop_audit)

	def _drop_audit(self):
		frappe.db.delete(self.AUDIT, {"action": ("like", "backup_presign%")})
		frappe.db.commit()

	def presign_rows(self) -> list[dict]:
		return frappe.get_all(
			self.AUDIT, filters={"action": "backup_presign_issued"}, fields=["name", "actor", "details"]
		)

	# --- scoping -------------------------------------------------------------------

	@refusal_guard
	def test_a_key_outside_the_backup_prefix_is_refused(self):
		with self.assertRaises(frappe.PermissionError):
			restore.download_url("prv/ab/cd/abcdef0123456789")

		self.assertEqual(self.bucket.presign_calls, [], "nothing may be minted for a refused key")

	@refusal_guard
	def test_a_recorded_key_outside_the_prefix_is_still_refused(self):
		"""Isolates the PREFIX check from the manifest check.

		The test above is refused by either guard, so it cannot tell them apart — and it kept
		passing with the prefix check disabled. This records an out-of-prefix key in a real
		Success manifest, so the manifest check is satisfied and only the prefix check can
		refuse. The case is reachable: change `backup_prefix` after a backup and its own log
		rows now name keys outside the new scope.
		"""
		foreign = "some-other-site/backups/daily/x/db.sql.gz"
		self.make_success_log({foreign: {"kind": "database", "sha256": "c" * 64, "size": 10}})
		self.assertIsNotNone(
			restore.recorded_digest(foreign), "the manifest check must be satisfied, or this proves nothing"
		)

		with self.assertRaises(frappe.PermissionError) as caught:
			restore.download_url(foreign)

		self.assertIn("not under", str(caught.exception))
		self.assertEqual(self.bucket.presign_calls, [])

	@refusal_guard
	def test_an_unrecorded_key_inside_the_prefix_isolates_the_manifest_check(self):
		"""The mirror image: prefix satisfied, manifest not."""
		inside = f"{frappe.local.site}/backups/daily/x/never-uploaded.sql.gz"

		with self.assertRaises(frappe.PermissionError) as caught:
			restore.download_url(inside)

		self.assertIn("No successful", str(caught.exception))

	@refusal_guard
	def test_an_attachment_object_key_is_refused(self):
		"""The specific bypass: a live attachment object signed through the backup door."""
		with self.assertRaises(frappe.PermissionError):
			restore.download_url("pub/00/11/deadbeef")
		self.assertEqual(self.bucket.presign_calls, [])

	@refusal_guard
	def test_a_key_under_the_prefix_but_in_no_manifest_is_refused(self):
		"""Prefix scoping alone is not enough — the artifact must be one we uploaded."""
		with self.assertRaises(frappe.PermissionError):
			restore.download_url(f"{frappe.local.site}/backups/daily/x/not-ours.sql.gz")
		self.assertEqual(self.bucket.presign_calls, [])

	def test_an_empty_key_is_refused(self):
		with self.assertRaises(frappe.PermissionError):
			restore.download_url("")

	def test_a_traversal_style_key_is_refused(self):
		with self.assertRaises(frappe.PermissionError):
			restore.download_url(f"../{frappe.local.site}/backups/daily/x/db.sql.gz")

	def test_restore_status_is_scoped_the_same_way(self):
		with self.assertRaises(frappe.PermissionError):
			restore.restore_status("prv/ab/cd/abcdef0123456789")

	def test_initiate_restore_is_scoped_the_same_way(self):
		with self.assertRaises(frappe.PermissionError):
			restore.initiate_restore("prv/ab/cd/abcdef0123456789")
		self.assertEqual(self.bucket.restore_requests, [])

	def test_a_scoped_recorded_key_is_allowed(self):
		"""The positive control: the refusals above are not refusing everything."""
		url = restore.download_url(self.key)
		self.assertIn("X-Amz-Expires", url)

	# --- audit ---------------------------------------------------------------------

	def test_a_mint_writes_exactly_one_audit_row(self):
		restore.download_url(self.key)

		rows = self.presign_rows()
		self.assertEqual(len(rows), 1)

	def test_the_audit_row_records_key_ttl_bucket_and_actor(self):
		restore.download_url(self.key, ttl=120)

		details = frappe.parse_json(self.presign_rows()[0]["details"])
		self.assertEqual(details["key"], self.key)
		self.assertEqual(details["ttl"], 120)
		self.assertEqual(details["bucket"], "cfs-unit-test-backups")
		self.assertTrue(self.presign_rows()[0]["actor"])

	@refusal_guard
	def test_the_audit_row_never_contains_the_url_or_a_signature(self):
		"""An audit log holding the URL would be a second copy of the credential."""
		url = restore.download_url(self.key)

		serialised = frappe.as_json(self.presign_rows())
		self.assertNotIn(url, serialised)
		for secret in ("X-Amz-Signature", "X-Amz-Credential", "Signature="):
			self.assertNotIn(secret, serialised)

	def test_a_refused_key_writes_no_audit_row(self):
		with self.assertRaises(frappe.PermissionError):
			restore.download_url("prv/ab/cd/abcdef0123456789")
		self.assertEqual(self.presign_rows(), [])

	@refusal_guard
	def test_the_audit_row_is_committed_before_the_url_exists(self):
		"""Ordering, asserted by making the mint fail after the record is written.

		If the process dies between the two, the safe residue is a record of an attempt with
		no URL — never a URL with no record.
		"""

		def exploding_presign(*args, **kwargs):
			raise RuntimeError("bucket unreachable at the moment of signing")

		self.bucket.generate_presigned_url = exploding_presign

		with self.assertRaises(RuntimeError):
			restore.download_url(self.key)

		self.assertEqual(len(self.presign_rows()), 1, "the attempt must still be recorded")
