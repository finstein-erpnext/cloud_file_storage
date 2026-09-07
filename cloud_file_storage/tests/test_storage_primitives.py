"""Storage primitives: keys, hashing, typed errors, client config, breaker, modes."""

import hashlib
from unittest.mock import MagicMock, patch

import frappe
from botocore.exceptions import ClientError, ConnectTimeoutError, ReadTimeoutError
from frappe.tests.utils import FrappeTestCase

from cloud_file_storage.storage import breaker, client, keys
from cloud_file_storage.storage.exceptions import (
	CloudObjectNotFound,
	CloudStorageConfigurationError,
	CloudStoragePermissionError,
	CloudStorageTransportError,
	CloudStorageUnavailableError,
	is_transport_failure,
	translate_client_error,
)
from cloud_file_storage.storage.hashing import digest_bytes, digest_path, stat_signature
from cloud_file_storage.storage.modes import OperationMode

SHA = "9f3ab8" + "0" * 58


def _client_error(code):
	return ClientError({"Error": {"Code": code}}, "GetObject")


class TestKeyScheme(FrappeTestCase):
	def test_key_is_content_addressed_and_sharded(self):
		key = keys.build_object_key(SHA, "private", key_prefix="", site="erp.example")
		self.assertEqual(key, f"erp.example/prv/9f/3a/{SHA}")

	def test_public_and_private_get_different_prefixes(self):
		public = keys.build_object_key(SHA, "public", site="s")
		private = keys.build_object_key(SHA, "private", site="s")
		self.assertTrue(public.startswith("s/pub/"))
		self.assertTrue(private.startswith("s/prv/"))
		self.assertNotEqual(public, private)

	def test_key_prefix_is_applied(self):
		key = keys.build_object_key(SHA, "private", key_prefix="/tenant-a/", site="s")
		self.assertEqual(key, f"tenant-a/s/prv/9f/3a/{SHA}")

	def test_key_never_contains_the_filename(self):
		key = keys.build_object_key(SHA, "private", site="s")
		self.assertNotIn("invoice", key)
		self.assertNotIn(".pdf", key)

	def test_traversal_in_key_prefix_is_rejected(self):
		for bad in ("../etc", "a/../../b", "-leading-dash/x "):
			with self.subTest(prefix=bad), self.assertRaises(ValueError):
				keys.build_object_key(SHA, "private", key_prefix=bad, site="s")

	def test_non_sha256_identity_is_rejected(self):
		for bad in ("", "xyz", SHA.upper()[:63], "z" * 64):
			with self.subTest(sha=bad), self.assertRaises(ValueError):
				keys.build_object_key(bad, "private", site="s")

	def test_parse_round_trips_and_declines_foreign_keys(self):
		key = keys.build_object_key(SHA, "public", key_prefix="p", site="erp.example")
		parsed = keys.parse_object_key(key)
		self.assertEqual(parsed.content_sha256, SHA)
		self.assertEqual(parsed.visibility, "public")
		self.assertEqual(parsed.site, "erp.example")
		self.assertEqual(parsed.key_prefix, "p")
		self.assertIsNone(keys.parse_object_key("some/other/app/object.bin"))
		self.assertIsNone(keys.parse_object_key(""))

	def test_visibility_for_matches_is_private(self):
		self.assertEqual(keys.visibility_for(1), "private")
		self.assertEqual(keys.visibility_for(0), "public")
		self.assertEqual(keys.visibility_for(None), "public")


class TestHashing(FrappeTestCase):
	def test_one_pass_digest_matches_hashlib(self):
		content = b"the quick brown fox" * 100
		digest = digest_bytes(content)
		self.assertEqual(digest.md5, hashlib.md5(content).hexdigest())
		self.assertEqual(digest.sha256, hashlib.sha256(content).hexdigest())
		self.assertEqual(digest.size, len(content))

	def test_str_is_encoded_utf8_like_core_does(self):
		self.assertEqual(digest_bytes("héllo"), digest_bytes("héllo".encode()))

	def test_digest_path_matches_digest_bytes(self):
		content = b"\x00\x01\x02" * 5000
		path = frappe.get_site_path("private", "files", "cfs-digest-probe.bin")
		with open(path, "wb") as handle:
			handle.write(content)
		self.addCleanup(lambda: __import__("os").remove(path))

		self.assertEqual(digest_path(path), digest_bytes(content))

	def test_stat_signature_is_none_for_a_missing_path(self):
		self.assertIsNone(stat_signature("/nonexistent/cfs/probe"))


class TestTypedErrors(FrappeTestCase):
	def test_only_absence_is_a_file_not_found_error(self):
		"""Contract #5: the GST Return Log nulls its own pointer on FileNotFoundError."""
		self.assertTrue(issubclass(CloudObjectNotFound, FileNotFoundError))
		for cls in (
			CloudStorageTransportError,
			CloudStoragePermissionError,
			CloudStorageConfigurationError,
			CloudStorageUnavailableError,
		):
			with self.subTest(error=cls.__name__):
				self.assertFalse(issubclass(cls, FileNotFoundError))

	def test_client_errors_map_to_the_right_type(self):
		cases = {
			"404": CloudObjectNotFound,
			"NoSuchKey": CloudObjectNotFound,
			"AccessDenied": CloudStoragePermissionError,
			"InvalidAccessKeyId": CloudStoragePermissionError,
			"NoSuchBucket": CloudStorageConfigurationError,
			"500": CloudStorageTransportError,
			"SlowDown": CloudStorageTransportError,
			"RequestTimeout": CloudStorageTransportError,
		}
		for code, expected in cases.items():
			with self.subTest(code=code):
				self.assertIsInstance(translate_client_error(_client_error(code)), expected)

	def test_timeouts_are_transport_not_absence(self):
		for exc in (
			ConnectTimeoutError(endpoint_url="https://s3.invalid"),
			ReadTimeoutError(endpoint_url="https://s3.invalid"),
		):
			with self.subTest(exc=type(exc).__name__):
				translated = translate_client_error(exc)
				self.assertIsInstance(translated, CloudStorageTransportError)
				self.assertNotIsInstance(translated, FileNotFoundError)

	def test_only_transport_failures_trip_the_breaker(self):
		self.assertTrue(is_transport_failure(CloudStorageTransportError("x")))
		self.assertFalse(is_transport_failure(CloudObjectNotFound("x")))
		self.assertFalse(is_transport_failure(CloudStoragePermissionError("x")))
		# an already-open breaker must not count its own refusals as new failures
		self.assertFalse(is_transport_failure(CloudStorageUnavailableError("x")))


class TestClientConfiguration(FrappeTestCase):
	"""A30 — the request path is bounded; jobs get their own patience."""

	def test_request_profile_uses_the_a30_timeouts(self):
		config = client._client_config(client.PROFILE_REQUEST, "path")
		self.assertEqual(config.connect_timeout, 3)
		self.assertEqual(config.read_timeout, 15)
		self.assertEqual(config.retries, {"max_attempts": 2, "mode": "standard"})
		self.assertEqual(config.s3.get("addressing_style"), "path")

	def test_job_profile_is_a_separate_longer_lived_client(self):
		request_config = client._client_config(client.PROFILE_REQUEST, "")
		job_config = client._client_config(client.PROFILE_JOB, "")
		self.assertGreater(job_config.read_timeout, request_config.read_timeout)
		self.assertGreater(job_config.connect_timeout, request_config.connect_timeout)

	def test_addressing_style_auto_is_path_only_with_a_custom_endpoint(self):
		self.assertEqual(
			client.resolve_addressing_style(
				frappe._dict(endpoint_url="https://minio.invalid", addressing_style="auto")
			),
			"path",
		)
		self.assertEqual(
			client.resolve_addressing_style(frappe._dict(endpoint_url="", addressing_style="auto")), ""
		)
		self.assertEqual(
			client.resolve_addressing_style(
				frappe._dict(endpoint_url="https://x", addressing_style="virtual")
			),
			"virtual",
		)


class TestExtraArgs(FrappeTestCase):
	"""F6 / docs/INVARIANTS.md invariant 6."""

	def _settings(self, **overrides):
		settings = frappe._dict(
			{"storage_class": "STANDARD", "sse_mode": "SSE-S3", "kms_key_id": None, **overrides}
		)
		return settings

	def test_no_acl_key_is_ever_produced(self):
		for sse in ("SSE-S3", "SSE-KMS", ""):
			for storage_class in client.SYNC_RETRIEVAL_STORAGE_CLASSES:
				with self.subTest(sse=sse, storage_class=storage_class):
					args = client.build_extra_args(
						content_type="application/pdf",
						content_sha256=SHA,
						settings=self._settings(sse_mode=sse, storage_class=storage_class, kms_key_id="k"),
					)
					self.assertNotIn("ACL", args)

	def test_puts_are_checksummed(self):
		args = client.build_extra_args(
			content_type="application/pdf", content_sha256=SHA, settings=self._settings()
		)
		self.assertEqual(args["ChecksumAlgorithm"], "SHA256")

	def test_metadata_carries_identity_not_pii(self):
		args = client.build_extra_args(
			content_type="application/pdf", content_sha256=SHA, settings=self._settings()
		)
		self.assertEqual(args["Metadata"], {"sha256": SHA, "app": "cloud_file_storage"})

	def test_sse_modes_are_translated(self):
		s3 = client.build_extra_args(
			content_type=None, content_sha256=SHA, settings=self._settings(sse_mode="SSE-S3")
		)
		self.assertEqual(s3["ServerSideEncryption"], "AES256")

		kms = client.build_extra_args(
			content_type=None,
			content_sha256=SHA,
			settings=self._settings(sse_mode="SSE-KMS", kms_key_id="key-1"),
		)
		self.assertEqual(kms["ServerSideEncryption"], "aws:kms")
		self.assertEqual(kms["SSEKMSKeyId"], "key-1")

	def test_archive_storage_classes_are_refused_for_the_live_bucket(self):
		for archive in ("GLACIER", "DEEP_ARCHIVE", "GLACIER_IR", "OUTPOSTS"):
			with self.subTest(storage_class=archive), self.assertRaises(CloudStorageConfigurationError):
				client.build_extra_args(
					content_type=None, content_sha256=SHA, settings=self._settings(storage_class=archive)
				)

	def test_standard_class_is_not_sent_but_the_others_are(self):
		standard = client.build_extra_args(
			content_type=None, content_sha256=SHA, settings=self._settings(storage_class="STANDARD")
		)
		self.assertNotIn("StorageClass", standard)

		tiering = client.build_extra_args(
			content_type=None,
			content_sha256=SHA,
			settings=self._settings(storage_class="INTELLIGENT_TIERING"),
		)
		self.assertEqual(tiering["StorageClass"], "INTELLIGENT_TIERING")


class TestCircuitBreaker(FrappeTestCase):
	"""A30 — an S3 outage degrades file operations, never the ERP."""

	def setUp(self):
		super().setUp()
		breaker.reset()
		self.addCleanup(breaker.reset)

	def test_starts_closed(self):
		self.assertFalse(breaker.is_open())
		breaker.assert_closed()

	def test_opens_after_the_threshold_and_then_fails_fast(self):
		for _ in range(breaker.FAILURE_THRESHOLD):
			breaker.record_failure()

		self.assertTrue(breaker.is_open())
		with self.assertRaises(CloudStorageUnavailableError):
			breaker.assert_closed("get_object")

	def test_stays_closed_below_the_threshold(self):
		for _ in range(breaker.FAILURE_THRESHOLD - 1):
			breaker.record_failure()
		self.assertFalse(breaker.is_open())

	def test_a_success_resets_the_streak(self):
		for _ in range(breaker.FAILURE_THRESHOLD - 1):
			breaker.record_failure()
		breaker.record_success()
		breaker.record_failure()
		self.assertFalse(breaker.is_open())

	def test_it_still_opens_after_a_process_has_already_seen_it_closed(self):
		"""Regression: `get_value` memoises a miss in `frappe.local.cache`.

		A long-lived worker consults the breaker on every request. If the first "closed"
		answer is cached per process, the breaker can never open there again — the exact
		outage behaviour A30 exists to provide.
		"""
		self.assertFalse(breaker.is_open())

		for _ in range(breaker.FAILURE_THRESHOLD):
			breaker.record_failure()

		self.assertTrue(breaker.is_open())

	def test_a_cache_outage_leaves_file_operations_working(self):
		with patch("cloud_file_storage.storage.breaker.frappe.cache", side_effect=RuntimeError("redis down")):
			self.assertFalse(breaker.is_open())
			self.assertEqual(breaker.record_failure(), 0)
			breaker.assert_closed()


class TestOperationModeSemantics(FrappeTestCase):
	def test_only_local_only_avoids_the_cloud(self):
		self.assertFalse(OperationMode.LOCAL_ONLY.uses_cloud)
		for mode in (
			OperationMode.DUAL_WRITE,
			OperationMode.S3_PRIMARY_LOCAL_FALLBACK,
			OperationMode.S3_ONLY,
		):
			with self.subTest(mode=mode):
				self.assertTrue(mode.uses_cloud)

	def test_only_local_only_and_dual_write_keep_a_canonical_local_copy(self):
		self.assertTrue(OperationMode.LOCAL_ONLY.keeps_local_copy)
		self.assertTrue(OperationMode.DUAL_WRITE.keeps_local_copy)
		self.assertFalse(OperationMode.S3_PRIMARY_LOCAL_FALLBACK.keeps_local_copy)
		self.assertFalse(OperationMode.S3_ONLY.keeps_local_copy)

	def test_s3_only_never_falls_back(self):
		self.assertFalse(OperationMode.S3_ONLY.allows_local_fallback)
		self.assertTrue(OperationMode.S3_PRIMARY_LOCAL_FALLBACK.allows_local_fallback)

	def test_an_unknown_mode_degrades_to_local_only_not_to_cloud_writes(self):
		from cloud_file_storage.storage.modes import get_mode

		with patch("cloud_file_storage.storage.modes.frappe.logger", return_value=MagicMock()):
			self.assertIs(get_mode(frappe._dict(operation_mode="NONSENSE")), OperationMode.LOCAL_ONLY)
