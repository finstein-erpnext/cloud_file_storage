"""L2 — the same scenarios against a real S3 endpoint (MinIO).

Enable with::

    RUN_MINIO_INTEGRATION_TESTS=1 \\
    CLOUD_FILE_STORAGE_MINIO_ENDPOINT=http://127.0.0.1:9000 \\
    CLOUD_FILE_STORAGE_MINIO_ACCESS_KEY=minioadmin \\
    CLOUD_FILE_STORAGE_MINIO_SECRET_KEY=minioadmin \\
    CLOUD_FILE_STORAGE_MINIO_BUCKET=cloud-file-storage-test \\
    CLOUD_FILE_STORAGE_MINIO_REGION=us-east-1

What only a real endpoint can prove: that the ExtraArgs we build are actually accepted,
that the checksum is stored, that no ACL is granted, that a presigned URL really serves
the bytes, and that GC really removes the object.
"""

import os
import urllib.parse
from unittest import SkipTest

import boto3
import frappe
import requests
from botocore.client import Config
from botocore.exceptions import ClientError
from frappe.tests.utils import FrappeTestCase
from frappe.utils.password import set_encrypted_password

from cloud_file_storage import gc
from cloud_file_storage.storage import client, engine, objects
from cloud_file_storage.storage.hashing import digest_bytes
from cloud_file_storage.tests.utils import (
	CSO_DOCTYPE,
	SETTINGS_DOCTYPE,
	restore_settings,
	set_mode,
	snapshot_settings,
)

PUBLIC_ACL_URIS = {
	"http://acs.amazonaws.com/groups/global/AllUsers",
	"http://acs.amazonaws.com/groups/global/AuthenticatedUsers",
}


class TestMinioIntegration(FrappeTestCase):
	"""Real round trips. Every object created here is removed in tearDown."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if os.environ.get("RUN_MINIO_INTEGRATION_TESTS") != "1":
			raise SkipTest("set RUN_MINIO_INTEGRATION_TESTS=1 to run MinIO integration tests")

		cls.endpoint_url = os.environ.get("CLOUD_FILE_STORAGE_MINIO_ENDPOINT", "http://127.0.0.1:9000")
		cls.access_key_id = os.environ.get("CLOUD_FILE_STORAGE_MINIO_ACCESS_KEY", "minioadmin")
		cls.secret_access_key = os.environ.get("CLOUD_FILE_STORAGE_MINIO_SECRET_KEY", "minioadmin")
		cls.bucket = os.environ.get("CLOUD_FILE_STORAGE_MINIO_BUCKET", "cloud-file-storage-test")
		cls.region = os.environ.get("CLOUD_FILE_STORAGE_MINIO_REGION", "us-east-1")

		cls.minio = boto3.client(
			"s3",
			endpoint_url=cls.endpoint_url,
			aws_access_key_id=cls.access_key_id,
			aws_secret_access_key=cls.secret_access_key,
			region_name=cls.region,
			config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
		)
		try:
			cls.minio.list_buckets()
		except Exception as exc:  # noqa: BLE001
			raise RuntimeError(
				f"minio endpoint not reachable at {cls.endpoint_url}; "
				"check the CLOUD_FILE_STORAGE_MINIO_* environment variables"
			) from exc

		existing = {bucket["Name"] for bucket in cls.minio.list_buckets().get("Buckets", [])}
		if cls.bucket not in existing:
			cls.minio.create_bucket(Bucket=cls.bucket)

		# This suite writes a real endpoint, real credentials and a real bucket into the
		# singleton. Restore all of it as a class cleanup so a second run starts from the
		# same state as the first.
		cls._settings_snapshot = snapshot_settings()
		cls.addClassCleanup(restore_settings, cls._settings_snapshot)

	def setUp(self):
		super().setUp()
		self.created_files: list[str] = []
		self.created_keys: set[str] = set()
		self.addCleanup(self._cleanup)

		client.clear_client_cache()
		self._configure("S3_ONLY")

	def _configure(self, mode, **overrides):
		set_mode(
			mode,
			bucket=self.bucket,
			region=self.region,
			endpoint_url=self.endpoint_url,
			addressing_style="path",
			use_default_credential_chain=0,
			access_key_id=self.access_key_id,
			# This MinIO build has no KMS backend and rejects SSE headers outright; the
			# SSE-S3/SSE-KMS ExtraArgs are asserted at L1 in test_storage_primitives.
			sse_mode="",
			**overrides,
		)
		# A Single stores its values in `tabSingles`; the secret lives in `__Auth`.
		set_encrypted_password(
			SETTINGS_DOCTYPE, SETTINGS_DOCTYPE, self.secret_access_key, "secret_access_key"
		)
		frappe.clear_document_cache(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE)
		client.clear_client_cache()

	def _cleanup(self):
		for name in reversed(self.created_files):
			frappe.db.delete("File", {"name": name})
		frappe.db.delete(CSO_DOCTYPE, {"bucket": self.bucket})
		frappe.db.set_single_value(SETTINGS_DOCTYPE, dict(self._settings_snapshot))
		frappe.db.commit()
		frappe.clear_document_cache(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE)
		for key in self.created_keys:
			try:
				self.minio.delete_object(Bucket=self.bucket, Key=key)
			except ClientError:
				pass
		client.clear_client_cache()

	def make_file(self, *, file_name, content, is_private=1):
		doc = frappe.get_doc(
			{"doctype": "File", "file_name": file_name, "content": content, "is_private": is_private}
		).insert(ignore_permissions=True)
		self.created_files.append(doc.name)
		key = frappe.db.get_value(CSO_DOCTYPE, doc.cloud_storage_object, "s3_key")
		if key:
			self.created_keys.add(key)
		return doc

	# --- round trip ------------------------------------------------------------------

	def test_a_private_upload_lands_at_the_content_addressed_key(self):
		content = b"minio private payload"
		doc = self.make_file(file_name="minio-private.txt", content=content)
		cso = frappe.get_doc(CSO_DOCTYPE, doc.cloud_storage_object)

		digest = digest_bytes(content)
		self.assertTrue(cso.s3_key.endswith(digest.sha256))
		self.assertIn("/prv/", cso.s3_key)
		self.minio.head_object(Bucket=self.bucket, Key=cso.s3_key)

	def test_a_public_upload_uses_the_public_prefix_in_the_same_bucket(self):
		doc = self.make_file(file_name="minio-public.txt", content=b"public payload", is_private=0)
		cso = frappe.get_doc(CSO_DOCTYPE, doc.cloud_storage_object)

		self.assertIn("/pub/", cso.s3_key)
		self.assertEqual(cso.bucket, self.bucket)
		self.minio.head_object(Bucket=self.bucket, Key=cso.s3_key)

	def test_the_bytes_come_back_unchanged(self):
		content = b"\x00\x01\x02 binary \xff\xfe payload"
		doc = self.make_file(file_name="minio-binary.bin", content=content)

		self.assertEqual(frappe.get_doc("File", doc.name).get_content(encodings=[]), content)

	def test_no_object_is_granted_public_read(self):
		"""F6 / docs/INVARIANTS.md invariant 6 — the bucket stays private, always."""
		doc = self.make_file(file_name="minio-no-acl.txt", content=b"no acl", is_private=0)
		cso = frappe.get_doc(CSO_DOCTYPE, doc.cloud_storage_object)

		grants = self.minio.get_object_acl(Bucket=self.bucket, Key=cso.s3_key).get("Grants", [])
		granted = {grant.get("Grantee", {}).get("URI") for grant in grants}
		self.assertEqual(granted & PUBLIC_ACL_URIS, set())

	def test_the_upload_is_checksummed_and_verifies(self):
		content = b"minio checksummed payload"
		doc = self.make_file(file_name="minio-checksum.txt", content=content)
		cso = frappe.get_doc(CSO_DOCTYPE, doc.cloud_storage_object)

		response = engine.verify(cso)

		self.assertEqual(response["ContentLength"], len(content))
		self.assertEqual(response.get("ChecksumSHA256"), engine.expected_checksum_sha256(cso.content_sha256))

	def test_verify_detects_a_size_mismatch(self):
		doc = self.make_file(file_name="minio-mismatch.txt", content=b"honest bytes")
		cso = frappe.get_doc(CSO_DOCTYPE, doc.cloud_storage_object)
		frappe.db.set_value(CSO_DOCTYPE, cso.name, "file_size", 999999)

		from cloud_file_storage.storage.exceptions import CloudStorageIntegrityError

		with self.assertRaises(CloudStorageIntegrityError):
			engine.verify(frappe.get_doc(CSO_DOCTYPE, cso.name))

	def test_a_presigned_get_serves_the_bytes_with_the_pinned_disposition(self):
		content = b"presigned payload"
		doc = self.make_file(file_name="minio-presign.txt", content=content)
		cso = frappe.get_doc(CSO_DOCTYPE, doc.cloud_storage_object)

		url = engine.presign_get(cso, ttl=300, disposition="attachment", filename="minio-presign.txt")

		self.assertIn("X-Amz-Expires=300", url)
		response = requests.get(url, timeout=15)
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.content, content)
		self.assertIn("attachment", response.headers.get("Content-Disposition", ""))
		# PLAN.md §C: private bytes must not be cacheable.
		self.assertEqual(response.headers.get("Cache-Control"), "no-store")

	def test_a_presigned_url_cannot_be_replayed_with_another_disposition(self):
		doc = self.make_file(file_name="minio-pinned.html", content=b"<html>hi</html>")
		cso = frappe.get_doc(CSO_DOCTYPE, doc.cloud_storage_object)

		url = engine.presign_get(cso, ttl=300, disposition="attachment", filename="minio-pinned.html")
		tampered = url.replace(
			urllib.parse.quote("attachment", safe=""), urllib.parse.quote("inline", safe="")
		)

		if tampered != url:
			self.assertEqual(requests.get(tampered, timeout=15).status_code, 403)

	def test_an_unsigned_request_is_refused(self):
		doc = self.make_file(file_name="minio-unsigned.txt", content=b"private bytes")
		cso = frappe.get_doc(CSO_DOCTYPE, doc.cloud_storage_object)

		direct = f"{self.endpoint_url}/{self.bucket}/{urllib.parse.quote(cso.s3_key)}"
		self.assertIn(requests.get(direct, timeout=15).status_code, (401, 403))

	# --- modes -----------------------------------------------------------------------

	def test_dual_write_leaves_both_copies(self):
		self._configure("DUAL_WRITE")
		doc = self.make_file(file_name="minio-dual.txt", content=b"both copies")
		cso = frappe.get_doc(CSO_DOCTYPE, doc.cloud_storage_object)

		self.assertTrue(os.path.exists(doc._canonical_local_path()))
		self.minio.head_object(Bucket=self.bucket, Key=cso.s3_key)

	def test_s3_only_leaves_no_canonical_local_copy(self):
		doc = self.make_file(file_name="minio-s3only.txt", content=b"cloud only")
		self.assertFalse(os.path.exists(doc._canonical_local_path()))
		self.assertEqual(frappe.get_doc("File", doc.name).get_content(), "cloud only")

	def test_materialization_downloads_a_usable_path(self):
		doc = self.make_file(file_name="minio-materialize.txt", content=b"materialize me")
		path = frappe.get_doc("File", doc.name).get_full_path()

		with open(path, "rb") as handle:
			self.assertEqual(handle.read(), b"materialize me")

	# --- lifecycle -------------------------------------------------------------------

	def test_deleting_the_last_reference_does_not_remove_the_object(self):
		doc = self.make_file(file_name="minio-delete.txt", content=b"scheduled not deleted")
		cso_name = doc.cloud_storage_object
		key = frappe.db.get_value(CSO_DOCTYPE, cso_name, "s3_key")

		frappe.delete_doc("File", doc.name, ignore_permissions=True, force=True)

		self.assertEqual(frappe.db.get_value(CSO_DOCTYPE, cso_name, "status"), "pending_delete")
		self.minio.head_object(Bucket=self.bucket, Key=key)

	def test_gc_removes_the_object_after_the_grace_window(self):
		from frappe.utils import add_to_date, now_datetime

		doc = self.make_file(file_name="minio-gc.txt", content=b"collect me for real")
		cso_name = doc.cloud_storage_object
		key = frappe.db.get_value(CSO_DOCTYPE, cso_name, "s3_key")

		frappe.delete_doc("File", doc.name, ignore_permissions=True, force=True)
		frappe.db.set_value(
			CSO_DOCTYPE, cso_name, "deletion_scheduled_at", add_to_date(now_datetime(), days=-1)
		)

		gc.run_deferred_object_gc()

		self.assertEqual(frappe.db.get_value(CSO_DOCTYPE, cso_name, "status"), "deleted")
		with self.assertRaises(ClientError):
			self.minio.head_object(Bucket=self.bucket, Key=key)

	def test_a_tombstoned_object_revives_on_re_upload(self):
		"""A25, end to end against a real bucket."""
		from frappe.utils import add_to_date, now_datetime

		content = b"phoenix bytes for minio"
		doc = self.make_file(file_name="minio-revive.txt", content=content)
		cso_name = doc.cloud_storage_object
		key = frappe.db.get_value(CSO_DOCTYPE, cso_name, "s3_key")

		frappe.delete_doc("File", doc.name, ignore_permissions=True, force=True)
		frappe.db.set_value(
			CSO_DOCTYPE, cso_name, "deletion_scheduled_at", add_to_date(now_datetime(), days=-1)
		)
		gc.run_deferred_object_gc()
		self.assertEqual(frappe.db.get_value(CSO_DOCTYPE, cso_name, "status"), "deleted")

		recreated = self.make_file(file_name="minio-revive-again.txt", content=content)

		self.assertEqual(recreated.cloud_storage_object, cso_name)
		self.assertEqual(frappe.db.get_value(CSO_DOCTYPE, cso_name, "status"), "uploaded")
		self.minio.head_object(Bucket=self.bucket, Key=key)
		self.assertEqual(frappe.get_doc("File", recreated.name).get_content(encodings=[]), content)

	def test_a_privacy_flip_copies_server_side_to_the_public_prefix(self):
		doc = self.make_file(file_name="minio-flip.txt", content=b"flip me for real", is_private=1)
		old_cso = frappe.get_doc(CSO_DOCTYPE, doc.cloud_storage_object)

		reloaded = frappe.get_doc("File", doc.name)
		reloaded.is_private = 0
		reloaded.save(ignore_permissions=True)

		new_cso = frappe.get_doc(CSO_DOCTYPE, frappe.db.get_value("File", doc.name, "cloud_storage_object"))
		self.created_keys.add(new_cso.s3_key)

		self.assertIn("/pub/", new_cso.s3_key)
		self.minio.head_object(Bucket=self.bucket, Key=new_cso.s3_key)
		# the source object is scheduled, never deleted inline
		self.minio.head_object(Bucket=self.bucket, Key=old_cso.s3_key)

	def test_two_identical_uploads_share_one_object(self):
		content = b"deduplicated across rows"
		first = self.make_file(file_name="minio-dedup-a.txt", content=content)
		second = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "minio-dedup-b.txt",
				"content": content,
				"is_private": 1,
				"ignore_duplicate_entry_error": 1,
			}
		).insert(ignore_permissions=True)
		self.created_files.append(second.name)

		self.assertEqual(
			frappe.db.get_value("File", second.name, "cloud_storage_object"),
			first.cloud_storage_object,
		)
		self.assertEqual(objects.live_reference_count(first.cloud_storage_object), 2)

	def test_the_legacy_endpoint_redirects_to_a_working_url(self):
		from cloud_file_storage.api import compat

		content = b"legacy endpoint payload"
		doc = self.make_file(file_name="minio-legacy.txt", content=content)
		cso = frappe.get_doc(CSO_DOCTYPE, doc.cloud_storage_object)

		frappe.local.response = frappe._dict()
		compat.legacy_generate_file(key=cso.s3_key, file_name="minio-legacy.txt")

		self.assertEqual(frappe.local.response["type"], "redirect")
		response = requests.get(frappe.local.response["location"], timeout=15)
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.content, content)

	def test_the_configured_timeouts_reach_the_boto_client(self):
		"""A30 — the request-path client is the bounded one."""
		request_client = client.get_client(profile=client.PROFILE_REQUEST)
		job_client = client.get_client(profile=client.PROFILE_JOB)

		self.assertEqual(request_client.meta.config.connect_timeout, 3)
		self.assertEqual(request_client.meta.config.read_timeout, 15)
		self.assertEqual(job_client.meta.config.read_timeout, 300)
		self.assertIsNot(request_client, job_client)
