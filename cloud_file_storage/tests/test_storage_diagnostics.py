"""Test Connection's probes — the checks the Settings panel paints.

Written after a near miss worth recording. `FakeObjectStore.bucket_versioning` was added so
the "versioning is not enabled" warning would be reachable from a unit test, and then no
test ever set it — a capability built to make a safeguard testable, sitting unused, which is
the same shape as a safeguard with no test at all. It surfaced when P6's tree turned out to
have added the same stub with the **opposite default**: whichever survives the merge would
silently change this app's reported health, and nothing here would have failed.

So every probe below sets its input explicitly and asserts both outcomes. That makes these
tests independent of which default the merged fake ends up carrying, which is the property
that matters at convergence.

**And it happened again, at the convergence merge, in this file.** The merge wired A27's full
rule in behind `_check_versioning` — versioning enabled AND `NoncurrentVersionExpiration` at
least the restore window — and `FakeObjectStore.lifecycle` was added to make the second half
reachable. No test set it. Every probe here drove only `bucket_versioning`, so reverting the
seam to a status-only step would have left the whole suite green: 1292 tests, three
consecutive runs, and not one of them touching the half of A27 that the merge existed to
preserve. Found by an independent reader, not by a run. The noncurrent tests below are the
mechanism that holds it in place.
"""

import datetime

import frappe

from cloud_file_storage.storage import diagnostics
from cloud_file_storage.tests.utils import CloudStorageTestCase, set_mode


def check(report: dict, name: str) -> dict:
	for entry in report["checks"]:
		if entry["check"] == name:
			return entry
	raise AssertionError(f"{name} is not among {[c['check'] for c in report['checks']]}")


class DiagnosticsTestCase(CloudStorageTestCase):
	def setUp(self):
		super().setUp()
		set_mode("S3_PRIMARY_LOCAL_FALLBACK")


class TestBucketVersioning(DiagnosticsTestCase):
	"""PLAN §H item 1 adopts "30 days + attachment-bucket versioning ON" as the recovery
	window. Versioning off is what turns an overwrite into unrecoverable loss, so the panel
	says so rather than leaving it to be discovered during a restore."""

	def test_versioning_off_is_reported_as_a_warning(self):
		self.store.bucket_versioning = None
		entry = check(diagnostics.test_connection(), "versioning")
		self.assertEqual(entry["status"], "warning")
		self.assertIn("not enabled", entry["message"])

	def test_versioning_on_is_reported_as_ok(self):
		self.store.bucket_versioning = "Enabled"
		entry = check(diagnostics.test_connection(), "versioning")
		self.assertEqual(entry["status"], "ok")

	def test_suspended_is_not_treated_as_enabled(self):
		"""S3's third value. `Suspended` keeps existing versions but stops making new ones,
		so it does not deliver the recovery window either."""
		self.store.bucket_versioning = "Suspended"
		entry = check(diagnostics.test_connection(), "versioning")
		self.assertEqual(entry["status"], "warning")

	def test_a_noncurrent_expiry_shorter_than_the_window_is_reported(self):
		"""A27's second half. Versioning ON with a 5-day noncurrent expiry against a 30-day
		restore window is a bucket that reports healthy and cannot honour the window — the
		exact configuration this half of the rule exists to catch."""
		self.store.bucket_versioning = "Enabled"
		self.store.lifecycle = {
			"Rules": [{"Status": "Enabled", "NoncurrentVersionExpiration": {"NoncurrentDays": 5}}]
		}
		entry = check(diagnostics.test_connection(), "versioning")
		self.assertEqual(entry["status"], "warning")
		self.assertIn("5", entry["message"])
		self.assertEqual(entry["noncurrent_days"], 5)

	def test_a_noncurrent_expiry_covering_the_window_is_ok(self):
		"""The other direction, so the test above cannot pass by rejecting everything."""
		self.store.bucket_versioning = "Enabled"
		self.store.lifecycle = {
			"Rules": [{"Status": "Enabled", "NoncurrentVersionExpiration": {"NoncurrentDays": 400}}]
		}
		entry = check(diagnostics.test_connection(), "versioning")
		self.assertEqual(entry["status"], "ok")

	def test_a_disabled_rule_does_not_count_against_the_window(self):
		"""A `Status: Disabled` rule expires nothing, so it cannot shorten the window."""
		self.store.bucket_versioning = "Enabled"
		self.store.lifecycle = {
			"Rules": [{"Status": "Disabled", "NoncurrentVersionExpiration": {"NoncurrentDays": 1}}]
		}
		entry = check(diagnostics.test_connection(), "versioning")
		self.assertEqual(entry["status"], "ok")

	def test_the_a27_severity_survives_into_the_check(self):
		"""The Desk paints this row red off `severity`, not off `status`.

		`status` stays `warning` deliberately — a reachable, writable bucket must not report
		the whole connection as failed over someone else's lifecycle policy. `severity` is what
		distinguishes a real A27 finding from "Skipped: the bucket could not be reached", which
		is also a warning. If the pass-through is dropped the two become indistinguishable.
		"""
		self.store.bucket_versioning = None
		entry = check(diagnostics.test_connection(), "versioning")
		self.assertEqual(entry["severity"], "error")

		self.store.bucket_versioning = "Enabled"
		self.store.lifecycle = {"Rules": []}
		self.assertEqual(check(diagnostics.test_connection(), "versioning")["severity"], "info")

	def test_the_warning_names_the_window_it_protects(self):
		self.store.bucket_versioning = None
		entry = check(diagnostics.test_connection(), "versioning")
		window = frappe.db.get_single_value("Cloud Storage Settings", "restore_window_days")
		self.assertIn(str(window), entry["message"])


class TestTheProbeObject(DiagnosticsTestCase):
	def test_the_probe_is_written_and_read_back(self):
		report = diagnostics.test_connection()
		self.assertEqual(check(report, "put_object")["status"], "ok")
		self.assertEqual(check(report, "get_object")["status"], "ok")

	def test_the_probe_is_never_deleted(self):
		"""docs/INVARIANTS.md invariant 4 reserves physical S3 deletes for deferred GC. The design
		specified put+get+delete; the delete is deliberately absent."""
		before = list(self.store.deleted)
		diagnostics.test_connection()
		self.assertEqual(self.store.deleted, before, "the probe deleted an object")

	def test_repeated_runs_leave_exactly_one_probe_object(self):
		"""The reason a retained probe is acceptable: the key is fixed."""
		for _ in range(3):
			diagnostics.test_connection()
		key = diagnostics.probe_key()
		self.assertIn(key, self.store.objects)
		self.assertEqual(len([k for k in self.store.objects if k.endswith(".cfs-connection-probe")]), 1)

	def test_the_probe_key_is_outside_the_object_namespace(self):
		"""`pub/` and `prv/` hold content-addressed objects; the probe is not one."""
		key = diagnostics.probe_key()
		self.assertNotIn("/pub/", key)
		self.assertNotIn("/prv/", key)
		self.assertIn(frappe.local.site, key)

	def test_the_payload_says_the_probe_was_retained(self):
		report = diagnostics.test_connection()
		self.assertTrue(report["probe_object_retained"])
		self.assertIn("left in place", report["probe_note"])


class TestUnreachableBucket(DiagnosticsTestCase):
	def test_a_failure_is_reported_and_the_later_checks_are_marked_skipped(self):
		"""A check that could not run is not a check that passed."""
		from cloud_file_storage.storage.exceptions import CloudStorageTransportError

		self.store.fail_with = CloudStorageTransportError("bucket unreachable")
		try:
			report = diagnostics.test_connection()
		finally:
			self.store.fail_with = None

		self.assertEqual(report["status"], "failed")
		self.assertEqual(check(report, "head_bucket")["status"], "failed")
		for name in ("put_object", "get_object", "versioning"):
			with self.subTest(check=name):
				entry = check(report, name)
				self.assertEqual(entry["status"], "warning")
				self.assertIn("Skipped", entry["message"])

	def test_an_unconfigured_bucket_does_not_raise(self):
		frappe.db.set_single_value("Cloud Storage Settings", "bucket", "")
		frappe.clear_document_cache("Cloud Storage Settings", "Cloud Storage Settings")
		report = diagnostics.test_connection()
		self.assertEqual(report["status"], "failed")
		self.assertEqual(check(report, "head_bucket")["status"], "failed")


class TestClockSkew(DiagnosticsTestCase):
	def test_a_large_skew_fails_rather_than_warns(self):
		"""SigV4 rejects beyond 900s, so past that every call fails and the error names the
		symptom rather than the cause."""
		stamp = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)
		response = {
			"ResponseMetadata": {"HTTPHeaders": {"date": stamp.strftime("%a, %d %b %Y %H:%M:%S GMT")}}
		}
		parsed = diagnostics.response_date(response)
		self.assertIsNotNone(parsed)
		self.assertGreater(
			abs((datetime.datetime.now(datetime.timezone.utc) - parsed).total_seconds()),
			diagnostics.CLOCK_SKEW_FAIL_SECONDS,
		)

	def test_a_missing_date_header_yields_no_reading(self):
		self.assertIsNone(diagnostics.response_date({"ResponseMetadata": {"HTTPHeaders": {}}}))
		self.assertIsNone(diagnostics.response_date(None))


class TestEveryDeclaredCheckIsReported(DiagnosticsTestCase):
	def test_the_report_covers_the_declared_check_list(self):
		"""`CHECK_NAMES` is what the panel iterates; a check that vanished would render as a
		missing row rather than as a failure."""
		reported = {entry["check"] for entry in diagnostics.test_connection()["checks"]}
		self.assertEqual(reported, set(diagnostics.CHECK_NAMES))
