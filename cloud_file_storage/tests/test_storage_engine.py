"""StorageEngine against a mocked boto3 client.

`test_minio_integration.py` proves the same calls work against a real endpoint; this suite
proves the *arguments* — which is where the invariants live (no ACL, checksummed PUTs,
disposition pinned into the signature) — and the failure translation, which a healthy
endpoint cannot exercise.
"""

import base64
from unittest.mock import MagicMock, patch

import frappe
from botocore.exceptions import ClientError, ReadTimeoutError
from frappe.tests.utils import FrappeTestCase

from cloud_file_storage.storage import breaker, client, engine
from cloud_file_storage.storage.exceptions import (
	CloudObjectNotFound,
	CloudStorageIntegrityError,
	CloudStoragePermissionError,
	CloudStorageTransportError,
	CloudStorageUnavailableError,
)
from cloud_file_storage.storage.hashing import digest_bytes

CONTENT = b"engine payload"
DIGEST = digest_bytes(CONTENT)


def _settings(**overrides):
	return frappe._dict(
		{
			"bucket": "engine-bucket",
			"region": "us-east-1",
			"endpoint_url": "",
			"addressing_style": "auto",
			"key_prefix": "",
			"storage_class": "STANDARD",
			"sse_mode": "SSE-S3",
			"kms_key_id": None,
			"private_presign_ttl": 300,
			"multipart_threshold_mb": 64,
			"multipart_chunksize_mb": 16,
			"transfer_max_concurrency": 4,
			"modified": "2026-08-15 00:00:00",
			"use_default_credential_chain": 1,
			**overrides,
		}
	)


def _cso(**overrides):
	return frappe._dict(
		{
			"name": "cso-1",
			"bucket": "engine-bucket",
			"s3_key": f"site/prv/aa/bb/{DIGEST.sha256}",
			"content_sha256": DIGEST.sha256,
			"content_hash_md5": DIGEST.md5,
			"file_size": DIGEST.size,
			"mime_type": "text/plain",
			**overrides,
		}
	)


def _client_error(code, operation="GetObject"):
	return ClientError({"Error": {"Code": code}}, operation)


class EngineTestCase(FrappeTestCase):
	def setUp(self):
		super().setUp()
		breaker.reset()
		self.addCleanup(breaker.reset)
		client.clear_client_cache()
		self.addCleanup(client.clear_client_cache)

		self.s3 = MagicMock()
		patcher = patch("cloud_file_storage.storage.client.get_client", return_value=self.s3)
		patcher.start()
		self.addCleanup(patcher.stop)

		self.settings = _settings()
		self.cso = _cso()


class TestUploads(EngineTestCase):
	def test_a_small_put_sends_the_body_inline_with_the_checksum(self):
		engine.put_bytes(self.cso, CONTENT, settings=self.settings)

		kwargs = self.s3.put_object.call_args.kwargs
		self.assertEqual(kwargs["Bucket"], "engine-bucket")
		self.assertEqual(kwargs["Key"], self.cso.s3_key)
		self.assertEqual(kwargs["Body"], CONTENT)
		self.assertEqual(kwargs["ChecksumAlgorithm"], "SHA256")
		self.assertEqual(kwargs["ServerSideEncryption"], "AES256")
		self.assertEqual(kwargs["Metadata"], {"sha256": DIGEST.sha256, "app": "cloud_file_storage"})
		self.assertNotIn("ACL", kwargs)
		self.s3.upload_fileobj.assert_not_called()

	def test_a_put_above_the_multipart_threshold_uses_the_managed_transfer(self):
		self.s3.head_object.return_value = {"ContentLength": DIGEST.size}

		with patch("cloud_file_storage.storage.client.multipart_threshold_bytes", return_value=1):
			engine.put_bytes(self.cso, CONTENT, settings=self.settings)

		self.s3.upload_fileobj.assert_called_once()
		extra_args = self.s3.upload_fileobj.call_args.kwargs["ExtraArgs"]
		self.assertEqual(extra_args["ChecksumAlgorithm"], "SHA256")
		self.assertNotIn("ACL", extra_args)
		self.s3.put_object.assert_not_called()

	def test_put_path_uses_upload_file_with_the_transfer_config(self):
		self.s3.head_object.return_value = {"ContentLength": DIGEST.size}

		engine.put_path(self.cso, "/tmp/does-not-matter", settings=self.settings)

		args = self.s3.upload_file.call_args
		self.assertEqual(args.args[0], "/tmp/does-not-matter")
		self.assertEqual(args.args[1], "engine-bucket")
		self.assertEqual(args.args[2], self.cso.s3_key)
		self.assertNotIn("ACL", args.kwargs["ExtraArgs"])
		self.assertEqual(args.kwargs["Config"].multipart_threshold, 64 * 1024 * 1024)

	def test_an_upload_failure_is_typed_and_trips_the_breaker(self):
		self.s3.put_object.side_effect = _client_error("500", "PutObject")

		for _ in range(breaker.FAILURE_THRESHOLD):
			with self.assertRaises(CloudStorageTransportError):
				engine.put_bytes(self.cso, CONTENT, settings=self.settings)

		self.assertTrue(breaker.is_open())

	def test_an_open_breaker_refuses_before_calling_s3(self):
		for _ in range(breaker.FAILURE_THRESHOLD):
			breaker.record_failure()

		with self.assertRaises(CloudStorageUnavailableError):
			engine.put_bytes(self.cso, CONTENT, settings=self.settings)

		self.s3.put_object.assert_not_called()


class TestReads(EngineTestCase):
	def test_get_bytes_returns_the_body(self):
		self.s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=CONTENT))}

		self.assertEqual(engine.get_bytes(self.cso, settings=self.settings), CONTENT)

	def test_head_asks_for_the_stored_checksum(self):
		self.s3.head_object.return_value = {"ContentLength": DIGEST.size}

		engine.head(self.cso, settings=self.settings)

		self.assertEqual(self.s3.head_object.call_args.kwargs["ChecksumMode"], "ENABLED")

	def test_exists_is_false_for_a_missing_object_and_true_otherwise(self):
		self.s3.head_object.side_effect = _client_error("404", "HeadObject")
		self.assertFalse(engine.exists(self.cso, settings=self.settings))

		self.s3.head_object.side_effect = None
		self.s3.head_object.return_value = {"ContentLength": DIGEST.size}
		self.assertTrue(engine.exists(self.cso, settings=self.settings))

	def test_download_to_path_passes_the_transfer_config(self):
		engine.download_to_path(self.cso, "/tmp/target", settings=self.settings)

		args = self.s3.download_file.call_args
		self.assertEqual(args.args[2], "/tmp/target")
		self.assertIsNotNone(args.kwargs["Config"])


class TestVerification(EngineTestCase):
	def _head(self, **overrides):
		return {
			"ContentLength": DIGEST.size,
			"ETag": f'"{DIGEST.md5}"',
			"ChecksumSHA256": base64.b64encode(bytes.fromhex(DIGEST.sha256)).decode(),
			**overrides,
		}

	def test_a_matching_object_verifies(self):
		self.s3.head_object.return_value = self._head()
		self.assertEqual(engine.verify(self.cso, settings=self.settings)["ContentLength"], DIGEST.size)

	def test_a_size_mismatch_is_an_integrity_error(self):
		self.s3.head_object.return_value = self._head(ContentLength=1)
		with self.assertRaises(CloudStorageIntegrityError):
			engine.verify(self.cso, settings=self.settings)

	def test_a_checksum_mismatch_is_an_integrity_error(self):
		self.s3.head_object.return_value = self._head(ChecksumSHA256=base64.b64encode(b"x" * 32).decode())
		with self.assertRaises(CloudStorageIntegrityError):
			engine.verify(self.cso, settings=self.settings)

	def test_a_single_part_etag_is_compared_when_no_checksum_is_stored(self):
		self.s3.head_object.return_value = {"ContentLength": DIGEST.size, "ETag": '"deadbeef"'}
		with self.assertRaises(CloudStorageIntegrityError):
			engine.verify(self.cso, settings=self.settings)

	def test_a_multipart_etag_is_not_treated_as_an_md5(self):
		"""`<md5>-<parts>` is not the MD5 of the object and proves nothing either way."""
		self.s3.head_object.return_value = {"ContentLength": DIGEST.size, "ETag": '"deadbeef-4"'}
		engine.verify(self.cso, settings=self.settings)

	def test_an_absent_object_raises_the_file_not_found_subclass(self):
		self.s3.head_object.side_effect = _client_error("NoSuchKey", "HeadObject")
		with self.assertRaises(CloudObjectNotFound):
			engine.verify(self.cso, settings=self.settings)

	def test_streamed_verification_re_hashes_the_bytes(self):
		self.s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=CONTENT))}
		result = engine.verify_by_streaming(self.cso, settings=self.settings)
		self.assertEqual(result["ContentLength"], DIGEST.size)

		self.s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=b"different"))}
		with self.assertRaises(CloudStorageIntegrityError):
			engine.verify_by_streaming(self.cso, settings=self.settings)


class TestPresigning(EngineTestCase):
	def setUp(self):
		super().setUp()
		self.s3.generate_presigned_url.return_value = "https://signed.example/x"

	def test_the_disposition_and_filename_are_pinned_into_the_signature(self):
		engine.presign_get(
			self.cso,
			ttl=300,
			disposition="attachment",
			filename="Pflanzenrückgabe.pdf",
			settings=self.settings,
		)

		params = self.s3.generate_presigned_url.call_args.kwargs["Params"]
		disposition = params["ResponseContentDisposition"]
		self.assertTrue(disposition.startswith("attachment; filename*=UTF-8''"))
		# The whole percent-encoded name, not the ASCII prefix. The prefix form was mapped to
		# T-UNI and passed while `ückgabe` could be mangled freely -- the assertion that made
		# the scenario look covered when it was not.
		self.assertIn("Pflanzenr%C3%BCckgabe.pdf", disposition)
		self.assertEqual(params["ResponseContentType"], "text/plain")

	def test_the_ttl_comes_from_settings_and_is_clamped(self):
		engine.presign_get(self.cso, settings=self.settings)
		self.assertEqual(self.s3.generate_presigned_url.call_args.kwargs["ExpiresIn"], 300)

		engine.presign_get(self.cso, ttl=100_000, settings=self.settings)
		self.assertEqual(self.s3.generate_presigned_url.call_args.kwargs["ExpiresIn"], engine.MAX_PRESIGN_TTL)

		engine.presign_get(self.cso, ttl=0, settings=self.settings)
		self.assertEqual(self.s3.generate_presigned_url.call_args.kwargs["ExpiresIn"], 1)

	def test_an_inline_disposition_is_passed_through(self):
		engine.presign_get(self.cso, disposition="inline", settings=self.settings)
		params = self.s3.generate_presigned_url.call_args.kwargs["Params"]
		self.assertEqual(params["ResponseContentDisposition"], "inline")


class TestVisibilityCopyAndDelete(EngineTestCase):
	def test_copy_visibility_replaces_metadata_and_sends_no_acl(self):
		engine.copy_visibility(self.cso, "site/pub/aa/bb/x", settings=self.settings)

		kwargs = self.s3.copy_object.call_args.kwargs
		self.assertEqual(kwargs["CopySource"], {"Bucket": "engine-bucket", "Key": self.cso.s3_key})
		self.assertEqual(kwargs["Key"], "site/pub/aa/bb/x")
		self.assertEqual(kwargs["MetadataDirective"], "REPLACE")
		self.assertEqual(kwargs["ChecksumAlgorithm"], "SHA256")
		self.assertNotIn("ACL", kwargs)

	def test_delete_removes_exactly_that_key(self):
		engine.delete(self.cso, settings=self.settings)

		kwargs = self.s3.delete_object.call_args.kwargs
		self.assertEqual(kwargs, {"Bucket": "engine-bucket", "Key": self.cso.s3_key})

	def test_head_bucket_probes_the_configured_bucket(self):
		engine.head_bucket(settings=self.settings)
		self.assertEqual(self.s3.head_bucket.call_args.kwargs, {"Bucket": "engine-bucket"})


class TestFailureTranslation(EngineTestCase):
	def test_absence_and_permission_failures_do_not_trip_the_breaker(self):
		"""They are answers, not outages."""
		cases = {"404": CloudObjectNotFound, "AccessDenied": CloudStoragePermissionError}
		for code, expected in cases.items():
			with self.subTest(code=code):
				breaker.reset()
				self.s3.get_object.side_effect = _client_error(code)
				for _ in range(breaker.FAILURE_THRESHOLD + 2):
					with self.assertRaises(expected):
						engine.get_bytes(self.cso, settings=self.settings)
				self.assertFalse(breaker.is_open())

	def test_a_read_timeout_is_transport_not_absence(self):
		self.s3.get_object.side_effect = ReadTimeoutError(endpoint_url="https://s3.invalid")
		with self.assertRaises(CloudStorageTransportError) as caught:
			engine.get_bytes(self.cso, settings=self.settings)
		self.assertNotIsInstance(caught.exception, FileNotFoundError)

	def test_a_success_after_failures_closes_the_breaker_again(self):
		self.s3.get_object.side_effect = _client_error("500")
		for _ in range(breaker.FAILURE_THRESHOLD - 1):
			with self.assertRaises(CloudStorageTransportError):
				engine.get_bytes(self.cso, settings=self.settings)

		self.s3.get_object.side_effect = None
		self.s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=CONTENT))}
		engine.get_bytes(self.cso, settings=self.settings)

		self.s3.get_object.side_effect = _client_error("500")
		with self.assertRaises(CloudStorageTransportError):
			engine.get_bytes(self.cso, settings=self.settings)
		self.assertFalse(breaker.is_open())


class TestClientCaching(FrappeTestCase):
	def setUp(self):
		super().setUp()
		client.clear_client_cache()
		self.addCleanup(client.clear_client_cache)

	def test_one_client_is_reused_per_site_settings_and_profile(self):
		settings = _settings()
		with patch("cloud_file_storage.storage.client.boto3.client", side_effect=lambda *a, **k: MagicMock()):
			first = client.get_client(profile=client.PROFILE_REQUEST, settings=settings)
			second = client.get_client(profile=client.PROFILE_REQUEST, settings=settings)
			job = client.get_client(profile=client.PROFILE_JOB, settings=settings)

		self.assertIs(first, second)
		self.assertIsNot(first, job)

	def test_changing_settings_produces_a_new_client(self):
		with patch("cloud_file_storage.storage.client.boto3.client", side_effect=lambda *a, **k: MagicMock()):
			first = client.get_client(settings=_settings(modified="2026-08-15 00:00:00"))
			second = client.get_client(settings=_settings(modified="2026-08-15 01:00:00"))

		self.assertIsNot(first, second)

	def test_static_keys_are_only_passed_when_the_chain_is_off(self):
		with patch("cloud_file_storage.storage.client.boto3.client") as boto_client:
			settings = _settings(use_default_credential_chain=0, access_key_id="AKIA1")
			settings.get_password = lambda fieldname, raise_exception=False: "secret"
			client.get_client(settings=settings)
			self.assertEqual(boto_client.call_args.kwargs["aws_access_key_id"], "AKIA1")

			client.clear_client_cache()
			settings = _settings(use_default_credential_chain=1, access_key_id="AKIA1")
			settings.get_password = lambda fieldname, raise_exception=False: "secret"
			client.get_client(settings=settings)
			self.assertNotIn("aws_access_key_id", boto_client.call_args.kwargs)


class TestDeadlockIsNeverMistranslated(EngineTestCase):
	"""A lock conflict is not an outage and it is certainly not an absence.

	The design is lock-based (ADR-4/A6), so `QueryDeadlockError` is a real outcome under
	concurrency. InnoDB rolls back the whole victim transaction, so retrying inside the
	engine would silently discard the caller's earlier work — it must propagate untouched.
	Frappe already retries the enclosing background job (`background_jobs.py:230-245`); on
	the request path the transaction is rolled back and the caller sees an honest error.
	"""

	def test_a_deadlock_propagates_unchanged(self):
		self.s3.get_object.side_effect = frappe.QueryDeadlockError("deadlock")

		with self.assertRaises(frappe.QueryDeadlockError):
			engine.get_bytes(self.cso, settings=self.settings)

	def test_a_deadlock_is_never_turned_into_a_file_not_found_error(self):
		"""Otherwise a lock conflict would look like "the bytes are gone" (contract #5)."""
		self.s3.get_object.side_effect = frappe.QueryDeadlockError("deadlock")

		try:
			engine.get_bytes(self.cso, settings=self.settings)
		except Exception as exc:
			self.assertNotIsInstance(exc, FileNotFoundError)
			self.assertNotIsInstance(exc, CloudObjectNotFound)

	def test_a_deadlock_does_not_trip_the_breaker(self):
		"""The object store is fine; our own transaction lost a race."""
		self.s3.get_object.side_effect = frappe.QueryDeadlockError("deadlock")

		for _ in range(breaker.FAILURE_THRESHOLD + 2):
			with self.assertRaises(frappe.QueryDeadlockError):
				engine.get_bytes(self.cso, settings=self.settings)

		self.assertFalse(breaker.is_open())


class TestPresignedUrlsAreNoStore(EngineTestCase):
	"""PLAN.md §C — presigned URLs are short-TTL, disposition-pinned AND no-store."""

	def setUp(self):
		super().setUp()
		self.s3.generate_presigned_url.return_value = "https://signed.example/x"

	def test_no_store_is_pinned_into_the_signature(self):
		engine.presign_get(self.cso, ttl=300, settings=self.settings)

		params = self.s3.generate_presigned_url.call_args.kwargs["Params"]
		self.assertEqual(params["ResponseCacheControl"], "no-store")

	def test_it_is_set_on_every_disposition(self):
		for disposition in ("attachment", "inline"):
			with self.subTest(disposition=disposition):
				engine.presign_get(self.cso, disposition=disposition, settings=self.settings)
				params = self.s3.generate_presigned_url.call_args.kwargs["Params"]
				self.assertEqual(params["ResponseCacheControl"], "no-store")
