"""T-BACKUP (lifecycle) — A20: prefix-scoped rules, merge-not-replace, economics.

The two properties that make a lifecycle policy safe are both negative — "no rule without a
prefix" and "no rule of yours is discarded" — so each is asserted against a case designed
to violate it, not against a policy that happens to be well-formed.
"""

import ast
import inspect

import frappe
from frappe.utils import now_datetime

from cloud_file_storage.backup import lifecycle
from cloud_file_storage.backup import settings as backup_settings
from cloud_file_storage.tests.backup_utils import (
	TEST_BACKUP_BUCKET,
	BackupTestCase,
	set_backup_settings,
)
from cloud_file_storage.tests.markers import refusal_guard


class TestGeneratedRulesAreAlwaysScoped(BackupTestCase):
	"""The rule that has no safe exception: every generated rule carries a prefix."""

	@refusal_guard
	def test_every_generated_rule_has_a_non_empty_filter_prefix(self):
		policy = lifecycle.build_lifecycle_policy()

		self.assertTrue(policy["Rules"], "an empty policy would make this assertion vacuous")
		for rule in policy["Rules"]:
			with self.subTest(rule=rule["ID"]):
				self.assertIn("Filter", rule)
				self.assertIn("Prefix", rule["Filter"])
				self.assertTrue(rule["Filter"]["Prefix"], "an empty prefix matches the whole bucket")
				self.assertTrue(rule["Filter"]["Prefix"].startswith(frappe.local.site))

	def test_every_generated_rule_id_is_namespaced(self):
		policy = lifecycle.build_lifecycle_policy()
		self.assertTrue(policy["Rules"])
		for rule in policy["Rules"]:
			self.assertTrue(rule["ID"].startswith(lifecycle.RULE_ID_PREFIX))

	def test_the_prefix_is_scoped_per_frequency(self):
		prefixes = {
			rule["Filter"]["Prefix"]
			for rule in lifecycle.build_lifecycle_policy()["Rules"]
			if rule["ID"].startswith("cfs-expire-")
		}
		self.assertIn(f"{frappe.local.site}/backups/hourly/", prefixes)
		self.assertIn(f"{frappe.local.site}/backups/daily/", prefixes)

	def test_the_site_token_is_expanded(self):
		set_backup_settings(backup_prefix="{site}/dumps")
		prefixes = {rule["Filter"]["Prefix"] for rule in lifecycle.build_lifecycle_policy()["Rules"]}
		self.assertTrue(all(prefix.startswith(f"{frappe.local.site}/dumps") for prefix in prefixes))
		self.assertFalse(any("{site}" in prefix for prefix in prefixes))


class TestAnEmptyPrefixIsRefused(BackupTestCase):
	"""F-1: an empty prefix is not a narrow scope, it is the absence of one.

	`Filter: {"Prefix": ""}` matches every object in the bucket, so `cfs-noncurrent` and
	`cfs-abort-mpu` generated from an empty `backup_prefix` would expire and abort things
	this app never put there. The existing prefix tests only ran the default, which is how
	this survived until the gate.
	"""

	@refusal_guard
	def test_generating_a_policy_with_an_empty_prefix_is_refused(self):
		set_backup_settings(backup_prefix="")
		with self.assertRaises(frappe.ValidationError):
			lifecycle.build_lifecycle_policy()

	def test_a_whitespace_only_prefix_is_refused(self):
		set_backup_settings(backup_prefix="   /  ")
		with self.assertRaises(frappe.ValidationError):
			lifecycle.build_lifecycle_policy()

	def test_a_prefix_that_expands_to_nothing_is_refused(self):
		"""`{site}` is the only token; a prefix of just slashes expands to nothing."""
		set_backup_settings(backup_prefix="///")
		with self.assertRaises(frappe.ValidationError):
			lifecycle.build_lifecycle_policy()

	@refusal_guard
	def test_the_apply_is_refused_with_an_empty_prefix(self):
		set_backup_settings(backup_prefix="")

		with self.assertRaises(frappe.ValidationError):
			lifecycle.apply_lifecycle_policy(confirm_phrase=lifecycle.APPLY_CONFIRM_PHRASE)

		self.assertEqual(self.bucket.put_lifecycle_calls, [], "no unscoped rule may be written")

	def test_the_preview_is_refused_with_an_empty_prefix(self):
		set_backup_settings(backup_prefix="")
		with self.assertRaises(frappe.ValidationError):
			lifecycle.preview_lifecycle_policy()

	def test_a_backup_is_refused_with_an_empty_prefix(self):
		"""An artifact under no prefix is an artifact no expiration rule can reach."""
		from cloud_file_storage.backup import tasks

		set_backup_settings(backup_prefix="")
		with self.assertRaises(frappe.ValidationError):
			tasks.take_cloud_backup()

	def test_the_health_panel_reports_it_instead_of_raising(self):
		from cloud_file_storage.backup import health

		set_backup_settings(backup_prefix="", last_backup_on=now_datetime(), last_backup_status="Success")

		codes = {finding["code"] for finding in health.backup_health()["findings"]}

		self.assertIn("no_backup_prefix", codes)

	@refusal_guard
	def test_a_configured_prefix_reaches_all_four_generated_rule_shapes(self):
		"""The other direction of F-1, named shape by shape.

		Asserting "every rule has a prefix" over whatever the generator happened to emit
		would pass if a shape stopped being generated at all. This names the four shapes,
		asserts each is present, and asserts each carries the configured prefix — including
		`cfs-noncurrent` and `cfs-abort-mpu`, the two that were bucket-wide before the fix.
		"""
		set_backup_settings(backup_prefix="acme/dumps", subdaily_retention_days=90)

		rules = {rule["ID"]: rule for rule in lifecycle.build_lifecycle_policy()["Rules"]}
		expected = {
			"cfs-archive-daily": "acme/dumps/daily/",
			"cfs-expire-daily": "acme/dumps/daily/",
			"cfs-noncurrent": "acme/dumps/",
			"cfs-abort-mpu": "acme/dumps/",
		}
		for rule_id, prefix in expected.items():
			with self.subTest(rule=rule_id):
				self.assertIn(rule_id, rules, f"{rule_id} was not generated at all")
				self.assertEqual(rules[rule_id]["Filter"]["Prefix"], prefix)

	@refusal_guard
	def test_the_two_bucket_wide_shapes_are_scoped_under_the_prefix_not_the_root(self):
		"""`cfs-noncurrent` deletes noncurrent versions; unscoped it would delete other
		people's. This is the specific harm F-1 described."""
		set_backup_settings(backup_prefix="acme/dumps")

		rules = {rule["ID"]: rule for rule in lifecycle.build_lifecycle_policy()["Rules"]}
		for rule_id in ("cfs-noncurrent", "cfs-abort-mpu"):
			with self.subTest(rule=rule_id):
				prefix = rules[rule_id]["Filter"]["Prefix"]
				self.assertTrue(prefix.startswith("acme/dumps"))
				self.assertNotIn(prefix, ("", "/"))

	def test_the_structural_backstop_catches_a_rule_built_without_a_prefix(self):
		"""A fifth rule shape added later inherits the requirement, or fails here."""
		from cloud_file_storage.storage.exceptions import CloudStorageConfigurationError

		with self.assertRaises(CloudStorageConfigurationError):
			lifecycle.assert_every_rule_is_scoped(
				[{"ID": "cfs-something-new", "Status": "Enabled", "Filter": {"Prefix": ""}}]
			)

	def test_the_backstop_accepts_the_real_policy(self):
		lifecycle.assert_every_rule_is_scoped(lifecycle.build_lifecycle_policy()["Rules"])

	@refusal_guard
	def test_the_generator_actually_runs_the_backstop(self):
		"""Calling the backstop directly proves the backstop works, not that it is wired in.

		Removing the call from `build_lifecycle_policy` left every other test green, because
		`assert_prefix_is_scoped` refuses first in normal operation and the direct tests call
		the backstop themselves. This spies on the real call, which is the only thing that
		fails when the wiring goes.
		"""
		from unittest.mock import patch

		with patch.object(
			lifecycle, "assert_every_rule_is_scoped", wraps=lifecycle.assert_every_rule_is_scoped
		) as spy:
			policy = lifecycle.build_lifecycle_policy()

		spy.assert_called_once()
		self.assertEqual(spy.call_args.args[0], policy["Rules"])

	@refusal_guard
	def test_no_generated_rule_is_ever_scoped_to_the_bucket_root(self):
		"""The property F-1 violated, asserted directly over every rule."""
		rules = lifecycle.build_lifecycle_policy()["Rules"]
		self.assertTrue(rules, "an empty policy would make this vacuous")
		for rule in rules:
			with self.subTest(rule=rule["ID"]):
				self.assertNotEqual(rule["Filter"]["Prefix"], "")
				self.assertNotEqual(rule["Filter"]["Prefix"], "/")


class TestRetentionDrivesTheRules(BackupTestCase):
	"""A20: `hot_retention_days` is what drives the archive transition."""

	def archive_rules(self, **fields) -> dict:
		set_backup_settings(**fields)
		return {
			rule["ID"]: rule
			for rule in lifecycle.build_lifecycle_policy()["Rules"]
			if rule["ID"].startswith("cfs-archive-")
		}

	def test_the_transition_day_is_hot_retention_days(self):
		rules = self.archive_rules(hot_retention_days=21, delete_after_days=400)
		self.assertTrue(rules)
		for rule in rules.values():
			self.assertEqual(rule["Transitions"][0]["Days"], 21)

	def test_the_transition_class_is_the_configured_archive_class(self):
		rules = self.archive_rules(archive_storage_class="DEEP_ARCHIVE", delete_after_days=400)
		self.assertTrue(rules)
		for rule in rules.values():
			self.assertEqual(rule["Transitions"][0]["StorageClass"], "DEEP_ARCHIVE")

	def test_no_archive_rule_is_generated_without_an_archive_class(self):
		self.assertEqual(self.archive_rules(archive_storage_class=""), {})

	def test_no_archive_rule_is_generated_for_a_prefix_that_expires_first(self):
		"""Sub-daily artifacts die at 7 days; a day-14 transition could never fire."""
		rules = self.archive_rules(hot_retention_days=14, subdaily_retention_days=7)
		self.assertNotIn("cfs-archive-hourly", rules)
		self.assertIn("cfs-archive-daily", rules)

	def test_sub_daily_prefixes_expire_on_their_own_retention(self):
		set_backup_settings(subdaily_retention_days=5, delete_after_days=400)
		rules = {rule["ID"]: rule for rule in lifecycle.build_lifecycle_policy()["Rules"]}
		self.assertEqual(rules["cfs-expire-hourly"]["Expiration"]["Days"], 5)
		self.assertEqual(rules["cfs-expire-every-6-hours"]["Expiration"]["Days"], 5)
		self.assertEqual(rules["cfs-expire-daily"]["Expiration"]["Days"], 400)

	def test_a_zero_retention_generates_no_expiration_rule(self):
		set_backup_settings(delete_after_days=0)
		rule_ids = {rule["ID"] for rule in lifecycle.build_lifecycle_policy()["Rules"]}
		self.assertNotIn("cfs-expire-daily", rule_ids)
		self.assertIn("cfs-expire-hourly", rule_ids)

	def test_noncurrent_and_abort_rules_cover_the_whole_backup_prefix(self):
		rules = {rule["ID"]: rule for rule in lifecycle.build_lifecycle_policy()["Rules"]}
		self.assertEqual(rules["cfs-noncurrent"]["NoncurrentVersionExpiration"]["NoncurrentDays"], 7)
		self.assertEqual(rules["cfs-abort-mpu"]["AbortIncompleteMultipartUpload"]["DaysAfterInitiation"], 7)
		self.assertEqual(rules["cfs-noncurrent"]["Filter"]["Prefix"], f"{frappe.local.site}/backups/")


class TestMergeNeverReplaces(BackupTestCase):
	"""A20: rules whose ID does not start `cfs-` survive an apply untouched."""

	FOREIGN = {
		"ID": "ops-compliance-hold",
		"Status": "Enabled",
		"Filter": {"Prefix": "legal-hold/"},
		"Expiration": {"Days": 3650},
	}

	def test_a_foreign_rule_is_preserved_byte_for_byte(self):
		merged = lifecycle.merge_rules([self.FOREIGN], lifecycle.build_lifecycle_policy()["Rules"])

		self.assertIn(self.FOREIGN, merged["rules"])
		self.assertEqual(merged["preserved"], [self.FOREIGN])
		self.assertEqual(merged["dropped"], [])

	def test_a_rule_with_no_id_is_treated_as_foreign_and_preserved(self):
		anonymous = {"Status": "Enabled", "Filter": {"Prefix": "x/"}, "Expiration": {"Days": 1}}
		merged = lifecycle.merge_rules([anonymous], lifecycle.build_lifecycle_policy()["Rules"])
		self.assertIn(anonymous, merged["preserved"])

	def test_a_stale_cfs_rule_is_reported_as_dropped(self):
		stale = {"ID": "cfs-expire-fortnightly", "Status": "Enabled", "Filter": {"Prefix": "x/"}}
		merged = lifecycle.merge_rules([self.FOREIGN, stale], lifecycle.build_lifecycle_policy()["Rules"])

		self.assertEqual([rule["ID"] for rule in merged["dropped"]], ["cfs-expire-fortnightly"])
		self.assertIn(self.FOREIGN, merged["rules"])
		self.assertNotIn(stale, merged["rules"])

	def test_a_current_cfs_rule_is_reported_as_replaced_not_dropped(self):
		outdated = {"ID": "cfs-expire-daily", "Status": "Enabled", "Filter": {"Prefix": "old/"}}
		merged = lifecycle.merge_rules([outdated], lifecycle.build_lifecycle_policy()["Rules"])

		self.assertEqual([rule["ID"] for rule in merged["replaced"]], ["cfs-expire-daily"])
		self.assertEqual(merged["dropped"], [])
		self.assertNotIn(outdated, merged["rules"])

	@refusal_guard
	def test_the_applied_policy_contains_the_foreign_rule(self):
		"""End to end: the bytes actually written to the bucket still carry it."""
		self.bucket.lifecycle = {"Rules": [self.FOREIGN]}

		lifecycle.apply_lifecycle_policy(confirm_phrase=lifecycle.APPLY_CONFIRM_PHRASE)

		written = self.bucket.put_lifecycle_calls[-1]["policy"]["Rules"]
		self.assertIn(self.FOREIGN, written)
		self.assertTrue(any(rule["ID"].startswith("cfs-") for rule in written))

	@refusal_guard
	def test_the_preview_lists_the_rules_that_would_be_dropped(self):
		stale = {"ID": "cfs-legacy-rule", "Status": "Enabled", "Filter": {"Prefix": "x/"}}
		self.bucket.lifecycle = {"Rules": [self.FOREIGN, stale]}

		preview = lifecycle.preview_lifecycle_policy()

		self.assertEqual([rule["ID"] for rule in preview["dropped_rules"]], ["cfs-legacy-rule"])
		self.assertEqual([rule["ID"] for rule in preview["preserved_rules"]], ["ops-compliance-hold"])
		self.assertEqual(preview["confirm_phrase"], lifecycle.APPLY_CONFIRM_PHRASE)

	def test_the_preview_flags_a_foreign_rule_that_overlaps_our_prefix(self):
		"""S3 takes the union of matching rules; the earliest expiration silently wins."""
		aggressive = {
			"ID": "ops-cleanup",
			"Status": "Enabled",
			"Filter": {"Prefix": ""},
			"Expiration": {"Days": 1},
		}
		self.bucket.lifecycle = {"Rules": [aggressive]}

		preview = lifecycle.preview_lifecycle_policy()

		self.assertEqual([finding["id"] for finding in preview["overlapping_rules"]], ["ops-cleanup"])

	def test_a_bucket_with_no_policy_previews_cleanly(self):
		self.bucket.lifecycle = None
		preview = lifecycle.preview_lifecycle_policy()
		self.assertEqual(preview["current_bucket_policy"], {"Rules": []})
		self.assertEqual(preview["dropped_rules"], [])


class TestApplyRefusals(BackupTestCase):
	"""Every guard on the destructive path, each driven with an input that trips it."""

	@refusal_guard
	def test_the_apply_is_refused_without_the_confirmation_phrase(self):
		for phrase in (None, "", "apply lifecycle", "APPLY", "APPLY LIFECYCLE NOW", "yes"):
			with self.subTest(phrase=phrase):
				with self.assertRaises(frappe.ValidationError):
					lifecycle.apply_lifecycle_policy(confirm_phrase=phrase)
		self.assertEqual(self.bucket.put_lifecycle_calls, [])

	def test_the_exact_phrase_is_accepted(self):
		lifecycle.apply_lifecycle_policy(confirm_phrase=lifecycle.APPLY_CONFIRM_PHRASE)
		self.assertEqual(len(self.bucket.put_lifecycle_calls), 1)

	@refusal_guard
	def test_a_near_miss_is_refused_by_the_confirm_gate(self):
		"""Byte for byte, including whitespace — the L-1 ruling, 2026-08-16.

		This test previously asserted the opposite (that surrounding whitespace was
		tolerated), because `apply_lifecycle_policy` used to `.strip()`. The two
		type-to-confirm gates in this app guard the same class of action — remote deletion —
		and normalised differently, so one of them had to change. The gate did, and this
		assertion follows it: `migration.api.start_cleanup` refuses
		`" DELETE LOCAL FILES"`, and so does this.
		"""
		for phrase in (
			f" {lifecycle.APPLY_CONFIRM_PHRASE}",
			f"{lifecycle.APPLY_CONFIRM_PHRASE} ",
			f"  {lifecycle.APPLY_CONFIRM_PHRASE}\n",
			"APPLY  LIFECYCLE",
			"APPLY_LIFECYCLE",
			"Apply Lifecycle",
		):
			with self.subTest(phrase=phrase):
				with self.assertRaises(frappe.ValidationError):
					lifecycle.apply_lifecycle_policy(confirm_phrase=phrase)
		self.assertEqual(self.bucket.put_lifecycle_calls, [])

	@refusal_guard
	def test_the_apply_is_refused_when_the_backup_bucket_is_the_attachment_bucket(self):
		from cloud_file_storage.tests.utils import TEST_BUCKET

		set_backup_settings(backup_bucket=TEST_BUCKET)

		with self.assertRaises(frappe.ValidationError):
			lifecycle.apply_lifecycle_policy(confirm_phrase=lifecycle.APPLY_CONFIRM_PHRASE)

		self.assertEqual(self.bucket.put_lifecycle_calls, [], "no rule may reach the live bucket")

	def test_the_preview_is_refused_on_a_shared_bucket_too(self):
		from cloud_file_storage.tests.utils import TEST_BUCKET

		set_backup_settings(backup_bucket=TEST_BUCKET)
		with self.assertRaises(frappe.ValidationError):
			lifecycle.preview_lifecycle_policy()

	def test_the_apply_is_refused_without_a_backup_bucket(self):
		set_backup_settings(backup_bucket="")
		with self.assertRaises(frappe.ValidationError):
			lifecycle.apply_lifecycle_policy(confirm_phrase=lifecycle.APPLY_CONFIRM_PHRASE)

	@refusal_guard
	def test_the_apply_is_refused_when_the_economics_are_an_error(self):
		set_backup_settings(hot_retention_days=30, delete_after_days=60, archive_storage_class="GLACIER")

		with self.assertRaises(frappe.ValidationError):
			lifecycle.apply_lifecycle_policy(confirm_phrase=lifecycle.APPLY_CONFIRM_PHRASE)

		self.assertEqual(self.bucket.put_lifecycle_calls, [])

	def test_apply_and_preview_are_system_manager_only(self):
		for function in (lifecycle.apply_lifecycle_policy, lifecycle.preview_lifecycle_policy):
			with self.subTest(function=function.__name__):
				tree = ast.parse(inspect.getsource(function.__wrapped__))
				calls = [
					node
					for node in ast.walk(tree)
					if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "only_for"
				]
				self.assertTrue(calls, f"{function.__name__} must call frappe.only_for")
				self.assertEqual(calls[0].args[0].value, "System Manager")

	def test_a_successful_apply_records_a_hash_and_an_audit_row(self):
		before = frappe.db.count("Cloud Storage Audit Log", {"action": "backup_lifecycle_applied"})

		result = lifecycle.apply_lifecycle_policy(confirm_phrase=lifecycle.APPLY_CONFIRM_PHRASE)

		self.assertEqual(len(result["policy_hash"]), 64)
		self.assertEqual(
			frappe.db.get_single_value("Cloud Backup Settings", "lifecycle_applied_hash"),
			result["policy_hash"],
		)
		self.assertEqual(
			frappe.db.count("Cloud Storage Audit Log", {"action": "backup_lifecycle_applied"}),
			before + 1,
		)
		frappe.db.delete("Cloud Storage Audit Log", {"action": "backup_lifecycle_applied"})
		frappe.db.commit()


class TestArchiveClassesNeverTouchLiveObjects(BackupTestCase):
	"""Invariant 6 / F6, from both directions."""

	@refusal_guard
	def test_an_archive_class_is_refused_for_the_attachment_bucket(self):
		for storage_class in ("GLACIER", "GLACIER_IR", "DEEP_ARCHIVE", "", None):
			with self.subTest(storage_class=storage_class):
				with self.assertRaises(frappe.ValidationError):
					lifecycle.assert_synchronous_retrieval(storage_class, "the live bucket")

	def test_synchronous_classes_are_accepted(self):
		for storage_class in lifecycle.SYNC_RETRIEVAL_CLASSES:
			lifecycle.assert_synchronous_retrieval(storage_class, "the live bucket")

	def test_the_settings_validator_uses_the_same_guard(self):
		from cloud_file_storage.cloud_file_storage.doctype.cloud_storage_settings.cloud_storage_settings import (
			CloudStorageSettings,
		)

		doc = frappe.get_doc({"doctype": "Cloud Storage Settings", "storage_class": "GLACIER"})
		with self.assertRaises(frappe.ValidationError):
			CloudStorageSettings.validate_storage_class(doc)

	def test_all_three_sync_retrieval_lists_are_the_same_object(self):
		"""One tuple, three names. Three copies would be three chances to drift."""
		from cloud_file_storage.cloud_file_storage.doctype.cloud_storage_settings import (
			cloud_storage_settings,
		)
		from cloud_file_storage.storage import client

		self.assertIs(lifecycle.SYNC_RETRIEVAL_CLASSES, client.SYNC_RETRIEVAL_STORAGE_CLASSES)
		self.assertIs(
			cloud_storage_settings.SYNC_RETRIEVAL_STORAGE_CLASSES,
			client.SYNC_RETRIEVAL_STORAGE_CLASSES,
		)

	def test_no_generated_rule_ever_targets_the_attachment_bucket_prefix(self):
		"""The generated prefixes are the backup prefix, never `pub/` or `prv/`."""
		prefixes = {rule["Filter"]["Prefix"] for rule in lifecycle.build_lifecycle_policy()["Rules"]}
		self.assertTrue(prefixes)
		for prefix in prefixes:
			self.assertFalse(prefix.startswith("pub/"))
			self.assertFalse(prefix.startswith("prv/"))


class TestEconomicsValidator(BackupTestCase):
	"""The math the operator is shown before they pay for it."""

	def codes(self, *, stats=None, **fields) -> dict:
		set_backup_settings(**fields)
		findings = lifecycle.validate_economics(object_stats=stats if stats is not None else {})
		return {finding["code"]: finding for finding in findings}

	def test_a_clean_default_policy_has_no_errors(self):
		findings = self.codes()
		self.assertEqual([code for code, f in findings.items() if f["level"] == "error"], [])

	def test_archiving_shorter_than_the_class_minimum_is_an_error(self):
		findings = self.codes(hot_retention_days=30, delete_after_days=60, archive_storage_class="GLACIER")
		self.assertEqual(findings["archive_before_minimum"]["level"], "error")
		self.assertIn("30 + 90 = 120 > 60", findings["archive_before_minimum"]["math"])

	def test_deep_archive_raises_the_minimum_to_180_days(self):
		"""Same retention, different class: 14+90 fits inside 150, 14+180 does not."""
		self.assertNotIn(
			"archive_before_minimum",
			self.codes(hot_retention_days=14, delete_after_days=150, archive_storage_class="GLACIER"),
		)
		self.assertIn(
			"archive_before_minimum",
			self.codes(hot_retention_days=14, delete_after_days=150, archive_storage_class="DEEP_ARCHIVE"),
		)

	def test_a_transition_that_can_never_fire_is_a_warning_not_an_error(self):
		findings = self.codes(hot_retention_days=14, subdaily_retention_days=7)
		self.assertEqual(findings["archive_never_fires"]["level"], "warn")

	def test_hourly_frequency_times_retention_is_reported_with_its_math(self):
		findings = self.codes(frequency="Hourly", subdaily_retention_days=30)
		self.assertIn("subdaily_volume", findings)
		self.assertIn("24/day x 30d", findings["subdaily_volume"]["math"])

	def test_a_daily_frequency_raises_no_volume_finding(self):
		self.assertNotIn("subdaily_volume", self.codes(frequency="Daily"))

	def test_never_deleting_is_reported(self):
		self.assertIn("no_expiration", self.codes(delete_after_days=0))

	def test_small_artifacts_are_reported_against_the_40kb_overhead(self):
		findings = self.codes(stats={"mean_bytes": 20 * 1024})
		self.assertIn("small_objects", findings)
		self.assertIn("200%", findings["small_objects"]["math"])

	def test_large_artifacts_raise_no_overhead_finding(self):
		self.assertNotIn("small_objects", self.codes(stats={"mean_bytes": 500 * 1024 * 1024}))

	def test_deep_archive_retrieval_time_is_reported(self):
		self.assertIn(
			"deep_archive_retrieval",
			self.codes(archive_storage_class="DEEP_ARCHIVE", hot_retention_days=14, delete_after_days=400),
		)

	def test_object_stats_are_measured_from_this_site_s_own_backups(self):
		# `collect_object_stats` reads the last 30 Success rows on the site, so this assertion
		# is about the whole table and not just the row below. Any Success row an earlier
		# aborted run left behind would change the mean — which is how this test started
		# failing after an unrelated crash left rows uncleaned. Start from a known table.
		frappe.db.delete("Cloud Storage Backup Log")
		frappe.db.commit()

		doc = frappe.new_doc("Cloud Storage Backup Log")
		doc.update(
			{
				"status": "Success",
				"trigger": "manual",
				"total_bytes": 4096,
				"sha256_manifest": '{"a": {"size": 2048}, "b": {"size": 2048}}',
			}
		)
		doc.insert(ignore_permissions=True)
		self.track_log(doc.name)
		frappe.db.commit()

		stats = lifecycle.collect_object_stats()
		self.assertEqual(stats["artifacts"], 2)
		self.assertEqual(stats["mean_bytes"], 2048)


class TestBackupSettingsValidator(BackupTestCase):
	"""The controller refuses on save what the apply would refuse later."""

	def settings_doc(self, **fields):
		from cloud_file_storage.tests.backup_utils import DEFAULT_BACKUP_SETTINGS

		doc = frappe.get_doc({"doctype": "Cloud Backup Settings", **DEFAULT_BACKUP_SETTINGS, **fields})
		doc.flags.skip_connection_probe = True
		return doc

	def test_a_policy_that_pays_a_minimum_it_deletes_cannot_be_saved(self):
		doc = self.settings_doc(hot_retention_days=30, delete_after_days=60)
		with self.assertRaises(frappe.ValidationError):
			doc.validate()

	def test_a_sane_policy_saves(self):
		self.settings_doc().validate()

	def test_the_attachment_bucket_cannot_be_used_as_the_backup_bucket(self):
		from cloud_file_storage.tests.utils import TEST_BUCKET

		with self.assertRaises(frappe.ValidationError):
			self.settings_doc(backup_bucket=TEST_BUCKET).validate()

	def test_an_empty_prefix_is_refused_while_enabled(self):
		with self.assertRaises(frappe.ValidationError):
			self.settings_doc(backup_prefix="").validate()

	def test_an_archive_class_without_a_hot_window_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self.settings_doc(hot_retention_days=0, archive_storage_class="GLACIER").validate()

	def test_no_archive_class_and_no_hot_window_is_fine(self):
		self.settings_doc(hot_retention_days=0, archive_storage_class="").validate()

	def test_a_half_credential_pair_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self.settings_doc(use_storage_credentials=0, access_key="AKIA").validate()

	def test_a_bucket_is_required_once_enabled(self):
		with self.assertRaises(frappe.ValidationError):
			self.settings_doc(backup_bucket="").validate()

	def test_the_isolation_guard_ignores_an_unconfigured_backup_bucket(self):
		"""A disabled, unconfigured singleton must not throw on every save."""
		backup_settings.assert_backup_bucket_isolated(frappe._dict(backup_bucket=""), context="unit test")


class TestShippedDefaults(BackupTestCase):
	"""What an operator gets before they touch anything.

	A Single with no `tabSingles` rows is loaded through `frappe.new_doc`
	(`document.load_from_db`), so the DocType JSON defaults ARE what a fresh install runs
	on. That makes those defaults shipped behaviour, and it makes the test harness's
	`DEFAULT_BACKUP_SETTINGS` a claim about them rather than an arbitrary fixture.
	"""

	DOCUMENTED = {
		"enabled": 0,
		"backup_prefix": "{site}/backups",
		"use_storage_credentials": 1,
		"sse_type": "Inherit",
		"frequency": "Daily",
		"backup_hour": 1,
		"backup_weekday": "Monday",
		"include_site_config": 1,
		"include_public_files": 0,
		"include_private_files": 0,
		"compress_files": 1,
		"hot_retention_days": 14,
		"archive_storage_class": "GLACIER",
		"delete_after_days": 365,
		"subdaily_retention_days": 7,
		"noncurrent_retention_days": 7,
		"abort_multipart_days": 7,
		"log_retention_days": 90,
	}

	@staticmethod
	def _schema(name: str) -> dict:
		import json
		from pathlib import Path

		import cloud_file_storage

		root = Path(cloud_file_storage.__file__).parent / "cloud_file_storage" / "doctype"
		return json.loads((root / name / f"{name}.json").read_text())

	def test_the_shipped_json_carries_the_documented_defaults(self):
		"""Read off disk, because that is what ships.

		The `frappe.new_doc` check below reads the *installed* meta, which only changes at
		`bench migrate` — so an edit to the DocType JSON would not move it, and this pair is
		what makes a default drift visible in the same commit that causes it.
		"""
		defaults = {
			field["fieldname"]: field.get("default")
			for field in self._schema("cloud_backup_settings")["fields"]
		}
		for fieldname, expected in self.DOCUMENTED.items():
			with self.subTest(fieldname=fieldname):
				self.assertEqual(str(defaults[fieldname]), str(expected))

	def test_the_installed_schema_agrees_with_the_shipped_json(self):
		fresh = frappe.new_doc("Cloud Backup Settings")
		for fieldname, expected in self.DOCUMENTED.items():
			with self.subTest(fieldname=fieldname):
				actual = fresh.get(fieldname)
				self.assertEqual(
					int(actual) if isinstance(expected, int) else actual,
					expected,
					f"{fieldname} default drifted",
				)

	def test_the_test_harness_starts_from_the_shipped_defaults(self):
		"""A fixture that drifted from the shipped defaults tests a product nobody has."""
		from cloud_file_storage.tests.backup_utils import DEFAULT_BACKUP_SETTINGS

		for fieldname, expected in self.DOCUMENTED.items():
			if fieldname == "enabled":
				continue  # the suite deliberately runs with backups enabled
			with self.subTest(fieldname=fieldname):
				self.assertEqual(DEFAULT_BACKUP_SETTINGS[fieldname], expected)

	def test_the_defaults_generate_a_policy_with_no_economics_errors(self):
		findings = lifecycle.validate_economics(frappe.new_doc("Cloud Backup Settings"), object_stats={})
		self.assertEqual([f for f in findings if f["level"] == "error"], [])

	def test_every_select_field_has_an_explicit_default_or_a_leading_newline(self):
		"""docs/INVARIANTS.md invariant 10, for the two doctypes P6 adds."""
		import json
		from pathlib import Path

		import cloud_file_storage

		root = Path(cloud_file_storage.__file__).parent / "cloud_file_storage" / "doctype"
		checked = 0
		for name in ("cloud_backup_settings", "cloud_storage_backup_log"):
			schema = json.loads((root / name / f"{name}.json").read_text())
			for field in schema["fields"]:
				if field["fieldtype"] != "Select":
					continue
				checked += 1
				with self.subTest(doctype=name, field=field["fieldname"]):
					self.assertTrue(
						field.get("default") is not None or field["options"].startswith("\n"),
						f"{name}.{field['fieldname']} has neither a default nor a leading newline",
					)
		self.assertGreaterEqual(checked, 6, "no Select fields were checked; the loop is vacuous")

	def test_byte_counters_are_long_int(self):
		"""docs/INVARIANTS.md invariant 10: a byte counter in an Int overflows at 2GB."""
		import json
		from pathlib import Path

		import cloud_file_storage

		schema = json.loads(
			(
				Path(cloud_file_storage.__file__).parent
				/ "cloud_file_storage"
				/ "doctype"
				/ "cloud_storage_backup_log"
				/ "cloud_storage_backup_log.json"
			).read_text()
		)
		byte_fields = [f for f in schema["fields"] if f["fieldname"].endswith("_bytes")]
		self.assertTrue(byte_fields, "no byte counters were found; the loop is vacuous")
		for field in byte_fields:
			self.assertEqual(field["fieldtype"], "Long Int", field["fieldname"])


class TestLifecycleStaysOffTheAttachmentBucket(BackupTestCase):
	def test_the_apply_writes_only_to_the_backup_bucket(self):
		lifecycle.apply_lifecycle_policy(confirm_phrase=lifecycle.APPLY_CONFIRM_PHRASE)
		self.assertEqual(self.bucket.put_lifecycle_calls[-1]["bucket"], TEST_BACKUP_BUCKET)


class TestBucketIsolationIsTwoSided(BackupTestCase):
	"""M-2: the guard must fire whichever bucket field is being moved.

	It was called from every path that edits the backup side and none that edits the
	attachment side, so the invariant held while `backup_bucket` moved and broke silently
	when `bucket` was moved to match. The consequence is not cosmetic: with the two equal,
	`download_url` would presign arbitrary attachment objects with no File permission check
	and no access log — PLAN §C's serving gate bypassed through the backup door.
	"""

	def storage_doc(self, **fields):
		from cloud_file_storage.cloud_file_storage.doctype.cloud_storage_settings.cloud_storage_settings import (
			CloudStorageSettings,
		)

		doc = frappe.get_doc(
			{
				"doctype": "Cloud Storage Settings",
				"object_delete_grace_days": 30,
				"restore_window_days": 30,
				**fields,
			}
		)
		return CloudStorageSettings, doc

	@refusal_guard
	def test_pointing_the_attachment_bucket_at_the_backup_bucket_is_refused(self):
		set_backup_settings(backup_bucket="shared-bucket")
		cls, doc = self.storage_doc(bucket="shared-bucket")

		with self.assertRaises(frappe.ValidationError):
			cls.validate_bucket_isolation(doc)

	def test_a_distinct_attachment_bucket_is_accepted(self):
		set_backup_settings(backup_bucket="backups-only")
		cls, doc = self.storage_doc(bucket="attachments-only")

		cls.validate_bucket_isolation(doc)

	def test_the_guard_reads_the_value_being_saved_not_the_stored_one(self):
		"""The edit itself is what must be refused; checking the stored bucket would pass
		the save that creates the collision and refuse every save afterwards."""
		from cloud_file_storage.tests.utils import TEST_BUCKET

		set_backup_settings(backup_bucket="shared-bucket")
		cls, doc = self.storage_doc(bucket="shared-bucket")
		# The DB still holds the old, distinct attachment bucket.
		self.assertNotEqual(frappe.db.get_single_value("Cloud Storage Settings", "bucket"), "shared-bucket")
		self.assertEqual(TEST_BUCKET, "cfs-unit-test-bucket")

		with self.assertRaises(frappe.ValidationError):
			cls.validate_bucket_isolation(doc)

	def test_the_guard_is_wired_into_the_storage_validator(self):
		import inspect

		from cloud_file_storage.cloud_file_storage.doctype.cloud_storage_settings.cloud_storage_settings import (
			CloudStorageSettings,
		)

		self.assertIn("self.validate_bucket_isolation()", inspect.getsource(CloudStorageSettings.validate))
