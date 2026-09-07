"""Test helpers: an in-memory object store and a base case that configures a mode.

The fake store replaces the `storage.engine` functions rather than boto3, so every test
that is not specifically about boto3 exercises the real hooks, the real Cloud Storage
Object state machine and the real cache — with the network removed. That is what lets the
C1-C19 contract suite run on every frappe ref with zero skips (A22/F1).
"""

import base64
import os
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from cloud_file_storage.storage.exceptions import CloudObjectNotFound
from cloud_file_storage.storage.hashing import digest_bytes

SETTINGS_DOCTYPE = "Cloud Storage Settings"
CSO_DOCTYPE = "Cloud Storage Object"

TEST_BUCKET = "cfs-unit-test-bucket"


class FakeObjectStore:
	"""An in-memory stand-in for the bucket, with failure injection."""

	def __init__(self):
		self.objects: dict[str, bytes] = {}
		self.deleted: list[str] = []
		self.put_calls: list[str] = []
		self.presign_calls: list[dict] = []
		#: Set to an exception instance to make every call raise it.
		self.fail_with: Exception | None = None
		#: What the fake bucket reports for the A27 restore-window probes, and the single
		#: source of truth the `bucket_versioning` property is a view onto. Versioned with no
		#: noncurrent expiry is the configuration A27 asks operators for, so the default here
		#: is a bucket that PASSES — a suite whose fixture failed the check would train every
		#: later `test_connection` assertion to expect a red step. (P6 and P7 each grew a
		#: versioning probe on this fake independently, with OPPOSITE defaults and different
		#: attribute names; every test that cares sets the value explicitly, so the default
		#: decides nothing, but there is one attribute rather than two that can disagree.)
		#:
		#: Per-INSTANCE, not class-level, like every dict above: a class dict mutated in place
		#: by one test would poison every later test in the process, silently, pointing
		#: nowhere near its cause.
		self.versioning: dict = {"Status": "Enabled"}
		self.lifecycle: dict = {"Rules": []}
		self._patchers = []

	# --- lifecycle ---------------------------------------------------------------

	def start(self):
		targets = {
			"put_bytes": self._put_bytes,
			"put_path": self._put_path,
			"get_bytes": self._get_bytes,
			"download_to_path": self._download_to_path,
			"head": self._head,
			"delete": self._delete,
			"copy_visibility": self._copy_visibility,
			"presign_get": self._presign_get,
			"head_bucket": self._head_bucket,
			"list_objects": self._list_objects,
			# The A27 probes. Unpatched, `test_connection` would dial out from an offline
			# unit suite and pay a real connect timeout per call.
			"get_bucket_versioning": self._get_bucket_versioning,
			"get_bucket_lifecycle": self._get_bucket_lifecycle,
		}
		for name, replacement in targets.items():
			patcher = patch(f"cloud_file_storage.storage.engine.{name}", replacement)
			patcher.start()
			self._patchers.append(patcher)

	def stop(self):
		for patcher in reversed(self._patchers):
			patcher.stop()
		self._patchers = []

	def _guard(self):
		if self.fail_with is not None:
			raise self.fail_with

	# --- engine surface ----------------------------------------------------------

	def _put_bytes(self, cso, content, **kwargs):
		self._guard()
		self.objects[cso.s3_key] = bytes(content)
		self.put_calls.append(cso.s3_key)
		return {"ETag": f'"{digest_bytes(content).md5}"'}

	def _put_path(self, cso, path, **kwargs):
		with open(path, "rb") as handle:
			return self._put_bytes(cso, handle.read())

	def _get_bytes(self, cso, **kwargs):
		self._guard()
		try:
			return self.objects[cso.s3_key]
		except KeyError as exc:
			raise CloudObjectNotFound(cso.s3_key) from exc

	def _download_to_path(self, cso, destination, **kwargs):
		content = self._get_bytes(cso)
		os.makedirs(os.path.dirname(destination), exist_ok=True)
		with open(destination, "wb") as handle:
			handle.write(content)

	def _head(self, cso, **kwargs):
		content = self._get_bytes(cso)
		digest = digest_bytes(content)
		return {
			"ContentLength": digest.size,
			"ETag": f'"{digest.md5}"',
			"ChecksumSHA256": base64.b64encode(bytes.fromhex(digest.sha256)).decode(),
		}

	#: `engine.verify` is deliberately NOT replaced. A fake that recomputes the checksum from
	#: whatever bytes it is holding can never disagree with itself, so every "verification
	#: bites" assertion written against it would be vacuous — and verification is the gate
	#: that decides whether a local file may ever be deleted. The real implementation runs
	#: against the fake `head` below, which is where the comparison it makes belongs.

	def _delete(self, cso, **kwargs):
		self._guard()
		self.objects.pop(cso.s3_key, None)
		self.deleted.append(cso.s3_key)
		return {}

	def _copy_visibility(self, cso, target_key, **kwargs):
		content = self._get_bytes(cso)
		self.objects[target_key] = content
		return {"CopyObjectResult": {}}

	def _presign_get(self, cso, *, ttl=None, disposition="attachment", filename=None, **kwargs):
		self._guard()
		if cso.s3_key not in self.objects:
			raise CloudObjectNotFound(cso.s3_key)
		# Echo the TTL the runtime actually passed. A `ttl or 300` default here would let an
		# `X-Amz-Expires=300` assertion pass even when the runtime forgot to pass a TTL.
		url = f"https://fake-s3.invalid/{cso.s3_key}?X-Amz-Expires={ttl}"
		self.presign_calls.append(
			{"key": cso.s3_key, "ttl": ttl, "disposition": disposition, "kwargs": kwargs, "url": url}
		)
		# Recorded so a caller can assert the URL it HANDED OUT is the one minted here.
		# Asserting `X-Amz-Expires=<n>` against this string proves nothing — the fake built
		# it from the test's own number. Whether a TTL reaches a real signature is proved
		# against real MinIO (`test_minio_integration.py:203`), and what a *runtime* test can
		# honestly assert is that it did not wrap, rewrite or reuse the URL.
		return url

	def _head_bucket(self, **kwargs):
		self._guard()
		return {}

	@property
	def bucket_versioning(self) -> str | None:
		"""P7's spelling: the status string, or None when the header is absent entirely."""
		return (self.versioning or {}).get("Status")

	@bucket_versioning.setter
	def bucket_versioning(self, status: str | None) -> None:
		self.versioning = {"Status": status} if status else {}

	def _get_bucket_versioning(self, **kwargs):
		self._guard()
		return dict(self.versioning or {})

	def _get_bucket_lifecycle(self, **kwargs):
		self._guard()
		return {"Rules": list((self.lifecycle or {}).get("Rules") or [])}

	def _list_objects(self, prefix=None, *, continuation_token=None, page_size=1000, **kwargs):
		"""Paginated listing for the reconcile sweep, with a real continuation token.

		Paginates rather than returning everything in one page even for a handful of keys:
		the resumability the reconcile run claims is about the token round-trip, and a fake
		that never truncates would never exercise it.
		"""
		self._guard()
		keys = sorted(key for key in self.objects if not prefix or key.startswith(prefix))
		start = keys.index(continuation_token) if continuation_token in keys else 0
		page = keys[start : start + page_size]
		truncated = (start + page_size) < len(keys)
		response = {
			"Contents": [{"Key": key, "Size": len(self.objects[key])} for key in page],
			"IsTruncated": truncated,
			"KeyCount": len(page),
		}
		if truncated:
			response["NextContinuationToken"] = keys[start + page_size]
		return response

	# --- assertions --------------------------------------------------------------

	def contains(self, cso) -> bool:
		return cso.s3_key in self.objects


#: Every settings field the suite is allowed to touch. Snapshotted once per test class and
#: force-restored afterwards, so a run leaves the site exactly as it found it.
MANAGED_SETTINGS_FIELDS = (
	"operation_mode",
	"bucket",
	"region",
	"endpoint_url",
	"addressing_style",
	"fail_insert_on_s3_error",
	"key_prefix",
	"storage_class",
	"sse_mode",
	"cache_max_size_mb",
	"cache_ttl_hours",
	"object_delete_grace_days",
	"restore_window_days",
	"multipart_threshold_mb",
	"private_presign_ttl",
	"use_default_credential_chain",
	"access_key_id",
	"multipart_chunksize_mb",
	"transfer_max_concurrency",
	# P3 serving: a leaked `inline_mimetype_prefixes` silently changes what every later
	# disposition assertion is testing, and a leaked `cdn_base_url` sends the public
	# renderer down the CDN branch in suites that never asked for one.
	"inline_mimetype_prefixes",
	"cdn_base_url",
	"public_presign_ttl",
	"audit_public_fallback",
)

#: Stored in `__Auth`, not in `tabSingles`, so it needs its own snapshot/restore. The MinIO
#: suite writes a real credential here.
MANAGED_SECRET_FIELD = "secret_access_key"


def snapshot_settings() -> dict:
	"""Current value of every field the suite may change, credential included."""
	from frappe.utils.password import get_decrypted_password

	snapshot = {
		fieldname: frappe.db.get_single_value(SETTINGS_DOCTYPE, fieldname)
		for fieldname in MANAGED_SETTINGS_FIELDS
	}
	try:
		snapshot[MANAGED_SECRET_FIELD] = get_decrypted_password(
			SETTINGS_DOCTYPE, SETTINGS_DOCTYPE, MANAGED_SECRET_FIELD, raise_exception=False
		)
	except Exception:  # noqa: BLE001 - no stored credential is a perfectly normal state
		snapshot[MANAGED_SECRET_FIELD] = None
	return snapshot


def restore_settings(snapshot: dict):
	"""Put the singleton back, at DB level, whatever state the tests left it in.

	This has to bypass `CloudStorageSettings.validate`, and not as a convenience: once a
	test has driven the site to S3_ONLY, the downgrade gate correctly refuses a normal
	`.save()` back to LOCAL_ONLY, so a document-level restore would be rejected and leave
	the site poisoned for every later run. The gate is right; the restore just has to go
	underneath it.
	"""
	from frappe.utils.password import remove_encrypted_password, set_encrypted_password

	values = {key: value for key, value in snapshot.items() if key != MANAGED_SECRET_FIELD}
	frappe.db.rollback()
	frappe.db.set_single_value(SETTINGS_DOCTYPE, values)

	# The credential lives in `__Auth`, so restoring the Singles rows does not touch it and
	# the MinIO suite's real secret would survive the run.
	secret = snapshot.get(MANAGED_SECRET_FIELD)
	try:
		if secret:
			set_encrypted_password(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE, secret, MANAGED_SECRET_FIELD)
		else:
			remove_encrypted_password(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE, MANAGED_SECRET_FIELD)
	except Exception:  # noqa: BLE001 - restoring the rest matters more than the credential
		frappe.logger("cloud_file_storage").warning("could not restore the settings credential")

	frappe.db.commit()
	frappe.clear_document_cache(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE)


def set_mode(mode: str, **fields):
	"""Set the operation mode straight in the DB.

	Deliberately bypasses `CloudStorageSettings.validate`: the transition gates are a
	separate concern with their own tests, and a test DB always carries File rows from
	other suites that would (correctly) block a jump to S3_ONLY.
	"""
	values = {
		"operation_mode": mode,
		"bucket": TEST_BUCKET,
		"region": "us-east-1",
		"endpoint_url": "",
		"addressing_style": "auto",
		"fail_insert_on_s3_error": 0,
		"key_prefix": "",
		"storage_class": "STANDARD",
		"sse_mode": "SSE-S3",
		"cache_max_size_mb": 5120,
		"cache_ttl_hours": 72,
		# PLAN §H item 1 / the P6 A27 ruling: the recovery window is 30 days.
		"object_delete_grace_days": 30,
		"restore_window_days": 30,
		"multipart_threshold_mb": 64,
		"private_presign_ttl": 300,
		"public_presign_ttl": 3600,
		"use_default_credential_chain": 1,
		"access_key_id": "",
		"inline_mimetype_prefixes": "image/\napplication/pdf",
		"cdn_base_url": "",
		"audit_public_fallback": 0,
		**fields,
	}
	# One statement, not one per field: this runs in every setUp and every cleanup, and a
	# field-by-field loop over `tabSingles` is enough lock churn to deadlock against any
	# other run sharing the site — which is exactly what an independent gate does.
	frappe.db.set_single_value(SETTINGS_DOCTYPE, values)
	frappe.clear_document_cache(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE)


class CloudStorageTestCase(FrappeTestCase):
	"""Base case: fake bucket started, mode configured, rows cleaned up afterwards."""

	MODE = "S3_ONLY"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# Snapshot once, from the state the class inherited, and register the restore as a
		# CLASS cleanup: unittest runs those even when a test errors mid-way, so a failure
		# inside a per-test cleanup can no longer strand the site in S3_ONLY.
		cls._settings_snapshot = snapshot_settings()
		cls.addClassCleanup(restore_settings, cls._settings_snapshot)

	def setUp(self):
		super().setUp()
		from cloud_file_storage.cache import materialize
		from cloud_file_storage.storage import breaker, client

		self.store = FakeObjectStore()
		self.store.start()
		self.addCleanup(self.store.stop)

		breaker.reset()
		client.clear_client_cache()
		materialize.clear_tracking()
		frappe.local.cfs_legacy_write_stash = {}

		self._created_files: list[str] = []
		self.addCleanup(self._cleanup)

		set_mode(self.MODE)

	def _cleanup(self):
		from cloud_file_storage.cache import materialize
		from cloud_file_storage.storage import breaker

		for name in reversed(self._created_files):
			self._remove_local_copy(name)
			frappe.db.delete("File", {"name": name})
			materialize.forget(name)
		frappe.db.delete(CSO_DOCTYPE, {"bucket": TEST_BUCKET})
		# Restore from the class snapshot, never from a value read mid-run: a per-test
		# snapshot taken after an earlier leak would faithfully restore the leak.
		frappe.db.set_single_value(SETTINGS_DOCTYPE, dict(self._settings_snapshot))
		frappe.db.commit()
		frappe.clear_document_cache(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE)
		breaker.reset()
		materialize.clear_tracking()

	@staticmethod
	def _remove_local_copy(file_name: str):
		"""Test hygiene only.

		A leftover local file would make core's own `generate_file_name` rename the next
		same-named upload, which has nothing to do with what is under test.
		"""
		file_url = frappe.db.get_value("File", file_name, "file_url")
		if not file_url:
			return
		basename = file_url.rsplit("/", 1)[-1]
		for candidate in (
			frappe.get_site_path("private", "files", basename),
			frappe.get_site_path("public", "files", basename),
		):
			if os.path.exists(candidate):
				os.remove(candidate)

	# --- fixtures ------------------------------------------------------------------

	def make_file(self, *, file_name, content, is_private=1, **extra):
		doc = frappe.get_doc(
			{"doctype": "File", "file_name": file_name, "content": content, "is_private": is_private, **extra}
		).insert(ignore_permissions=True)
		self._created_files.append(doc.name)
		return doc

	def track(self, file_doc):
		self._created_files.append(file_doc.name)
		return file_doc

	def cso_of(self, file_doc):
		name = frappe.db.get_value("File", file_doc.name, "cloud_storage_object")
		return frappe.get_doc(CSO_DOCTYPE, name) if name else None

	def local_path(self, file_doc) -> str | None:
		return file_doc._canonical_local_path()
