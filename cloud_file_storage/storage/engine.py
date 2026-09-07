"""StorageEngine — every S3 call the app makes goes through here.

Responsibilities that are *not* the callers':

* bounded timeouts and the circuit breaker (A30) — a hung bucket never hangs a request;
* typed errors (`exceptions.py`) — `FileNotFoundError` only on true absence (contract #5);
* checksummed PUTs and no `ACL` key, ever (F6);
* independent verification (`verify`) — a fresh HEAD, never a cached belief.

`delete` is here because it needs the same plumbing, but it is called from exactly one
place: `gc.run_deferred_object_gc`. Nothing on the request path deletes an object.
"""

import base64
import io
import urllib.parse

import frappe
from frappe.utils import cint

from cloud_file_storage.storage import breaker, client
from cloud_file_storage.storage.exceptions import (
	TRANSLATABLE_ERRORS,
	CloudObjectNotFound,
	CloudStorageError,
	CloudStorageIntegrityError,
	is_transport_failure,
	translate_client_error,
)
from cloud_file_storage.storage.modes import get_settings

DEFAULT_PRIVATE_PRESIGN_TTL = 300
DEFAULT_PUBLIC_PRESIGN_TTL = 3600
#: S3's own ceiling for SigV4 presigned URLs, and our settings ceiling.
MAX_PRESIGN_TTL = 3600


def _call(operation: str, func, *args, **kwargs):
	"""Run one S3 call behind the breaker, translating the object store's own failures.

	The `except` clause is deliberately narrow. A frappe exception raised inside the call —
	a `QueryDeadlockError`, say — is not an S3 outage, and translating it would let
	`core_hooks._store`'s `except CloudStorageError` handlers degrade past a lock conflict
	as though the bucket were unreachable.
	"""
	breaker.assert_closed(operation)
	try:
		result = func(*args, **kwargs)
	except TRANSLATABLE_ERRORS as exc:
		error = translate_client_error(exc, context=operation)
		if is_transport_failure(error):
			breaker.record_failure()
		raise error from exc
	breaker.record_success()
	return result


def _bucket(cso, settings) -> str:
	return cso.bucket or settings.bucket


def put_bytes(cso, content: bytes, *, profile: str = client.PROFILE_REQUEST, settings=None) -> dict:
	"""Upload in-memory bytes with a SHA256 checksum. Returns the S3 response."""
	settings = settings or get_settings()
	s3 = client.get_client(profile=profile, settings=settings)
	extra_args = client.build_extra_args(
		content_type=cso.mime_type, content_sha256=cso.content_sha256, settings=settings
	)

	if len(content) >= client.multipart_threshold_bytes(settings):
		# Managed transfer: s3transfer aborts its own multipart upload on failure, which is
		# why no manual abort_multipart_upload appears here.
		_call(
			"upload_fileobj",
			s3.upload_fileobj,
			io.BytesIO(content),
			_bucket(cso, settings),
			cso.s3_key,
			ExtraArgs=extra_args,
			Config=client.get_transfer_config(settings),
		)
		return head(cso, profile=profile, settings=settings)

	return _call(
		"put_object",
		s3.put_object,
		Bucket=_bucket(cso, settings),
		Key=cso.s3_key,
		Body=content,
		**extra_args,
	)


def put_path(cso, path: str, *, profile: str = client.PROFILE_JOB, settings=None) -> dict:
	"""Upload a local file. Used by DUAL_WRITE (local first) and by migration UPLOAD."""
	settings = settings or get_settings()
	s3 = client.get_client(profile=profile, settings=settings)
	extra_args = client.build_extra_args(
		content_type=cso.mime_type, content_sha256=cso.content_sha256, settings=settings
	)
	_call(
		"upload_file",
		s3.upload_file,
		path,
		_bucket(cso, settings),
		cso.s3_key,
		ExtraArgs=extra_args,
		Config=client.get_transfer_config(settings),
	)
	return head(cso, profile=profile, settings=settings)


def get_bytes(cso, *, profile: str = client.PROFILE_REQUEST, settings=None) -> bytes:
	settings = settings or get_settings()
	s3 = client.get_client(profile=profile, settings=settings)
	response = _call("get_object", s3.get_object, Bucket=_bucket(cso, settings), Key=cso.s3_key)
	return response["Body"].read()


def download_to_path(cso, destination: str, *, profile: str = client.PROFILE_REQUEST, settings=None):
	settings = settings or get_settings()
	s3 = client.get_client(profile=profile, settings=settings)
	_call(
		"download_file",
		s3.download_file,
		_bucket(cso, settings),
		cso.s3_key,
		destination,
		Config=client.get_transfer_config(settings),
	)


def head(cso, *, profile: str = client.PROFILE_REQUEST, settings=None) -> dict:
	settings = settings or get_settings()
	s3 = client.get_client(profile=profile, settings=settings)
	return _call(
		"head_object",
		s3.head_object,
		Bucket=_bucket(cso, settings),
		Key=cso.s3_key,
		ChecksumMode="ENABLED",
	)


def exists(cso, **kwargs) -> bool:
	try:
		head(cso, **kwargs)
	except CloudObjectNotFound:
		return False
	return True


def expected_checksum_sha256(content_sha256: str) -> str:
	"""S3 reports SHA256 checksums base64-encoded; the CSO stores hex."""
	return base64.b64encode(bytes.fromhex(content_sha256)).decode()


def verify(cso, *, profile: str = client.PROFILE_JOB, settings=None) -> dict:
	"""Independent verification against a fresh HEAD (A7 public API).

	Raises :class:`CloudObjectNotFound` when the object is absent and
	:class:`CloudStorageIntegrityError` on a size/checksum mismatch. Returns the HEAD
	response on success. Never mutates the CSO — the caller decides what the result means.
	"""
	settings = settings or get_settings()
	response = head(cso, profile=profile, settings=settings)

	remote_size = cint(response.get("ContentLength"))
	if cint(cso.file_size) and remote_size != cint(cso.file_size):
		raise CloudStorageIntegrityError(
			f"size mismatch for {cso.s3_key}: remote {remote_size} != recorded {cint(cso.file_size)}"
		)

	remote_checksum = response.get("ChecksumSHA256")
	if remote_checksum and cso.content_sha256:
		if remote_checksum != expected_checksum_sha256(cso.content_sha256):
			raise CloudStorageIntegrityError(f"SHA256 checksum mismatch for {cso.s3_key}")
		return response

	# No stored checksum: ETag equals the MD5 only for single-part uploads, so it is
	# evidence only below the multipart threshold. Above it, the caller (migration VERIFY)
	# falls back to a streamed re-GET — never to "assume fine".
	etag = (response.get("ETag") or "").strip('"')
	if etag and "-" not in etag and cso.content_hash_md5:
		if etag.lower() != cso.content_hash_md5.lower():
			raise CloudStorageIntegrityError(f"ETag/MD5 mismatch for {cso.s3_key}")

	return response


def verify_by_streaming(cso, *, profile: str = client.PROFILE_JOB, settings=None) -> dict:
	"""Re-GET the object and re-hash it. The strongest check, used above the MPU threshold."""
	from cloud_file_storage.storage.hashing import digest_bytes

	content = get_bytes(cso, profile=profile, settings=settings)
	digest = digest_bytes(content)
	if digest.sha256 != cso.content_sha256:
		raise CloudStorageIntegrityError(f"streamed SHA256 mismatch for {cso.s3_key}")
	return {"ContentLength": digest.size, "ChecksumSHA256": expected_checksum_sha256(digest.sha256)}


def copy_visibility(cso, target_key: str, *, profile: str = client.PROFILE_REQUEST, settings=None) -> dict:
	"""Server-side copy to the pub/↔prv/ twin key. The source object is left untouched."""
	settings = settings or get_settings()
	s3 = client.get_client(profile=profile, settings=settings)
	extra_args = client.build_extra_args(
		content_type=cso.mime_type, content_sha256=cso.content_sha256, settings=settings
	)
	# CopyObject takes MetadataDirective rather than a plain Metadata merge.
	extra_args.pop("ChecksumAlgorithm", None)
	metadata = extra_args.pop("Metadata")
	return _call(
		"copy_object",
		s3.copy_object,
		Bucket=_bucket(cso, settings),
		Key=target_key,
		CopySource={"Bucket": _bucket(cso, settings), "Key": cso.s3_key},
		MetadataDirective="REPLACE",
		Metadata=metadata,
		ChecksumAlgorithm="SHA256",
		**extra_args,
	)


def presign_get(
	cso,
	*,
	ttl: int | None = None,
	disposition: str = "attachment",
	filename: str | None = None,
	content_type: str | None = None,
	settings=None,
) -> str:
	"""Short-TTL presigned GET with disposition, content type and `no-store` pinned in.

	Pinning these into the signature means a leaked URL cannot be replayed with a different
	disposition to turn an attachment into an inline document, and cannot be replayed to get
	a cacheable response for private bytes.
	"""
	settings = settings or get_settings()
	s3 = client.get_client(profile=client.PROFILE_REQUEST, settings=settings)

	if ttl is None:
		ttl = cint(settings.private_presign_ttl) or DEFAULT_PRIVATE_PRESIGN_TTL
	ttl = max(1, min(cint(ttl), MAX_PRESIGN_TTL))

	params = {"Bucket": _bucket(cso, settings), "Key": cso.s3_key}
	if filename:
		quoted = urllib.parse.quote(filename)
		params["ResponseContentDisposition"] = f"{disposition}; filename*=UTF-8''{quoted}"
	else:
		params["ResponseContentDisposition"] = disposition
	if content_type or cso.mime_type:
		params["ResponseContentType"] = content_type or cso.mime_type
	# PLAN.md §C: presigned URLs are short-TTL, disposition-pinned AND no-store. Signed like
	# the disposition, so a leaked URL cannot be replayed to get a cacheable response.
	params["ResponseCacheControl"] = "no-store"

	return _call(
		"generate_presigned_url",
		s3.generate_presigned_url,
		"get_object",
		Params=params,
		ExpiresIn=ttl,
	)


def delete(cso, *, profile: str = client.PROFILE_JOB, settings=None):
	"""Physically delete the object.

	**The only caller is `gc.run_deferred_object_gc`.** Nothing on the request path, in the
	delete hook, or in migration UPLOAD/VERIFY may call this (docs/INVARIANTS.md invariant 1/4).
	"""
	settings = settings or get_settings()
	s3 = client.get_client(profile=profile, settings=settings)
	return _call("delete_object", s3.delete_object, Bucket=_bucket(cso, settings), Key=cso.s3_key)


def head_bucket(*, settings=None):
	"""Connectivity probe used by the mode-transition gate and `test_connection`."""
	settings = settings or get_settings()
	s3 = client.get_client(profile=client.PROFILE_REQUEST, settings=settings)
	return _call("head_bucket", s3.head_bucket, Bucket=settings.bucket)


def get_bucket_versioning(*, settings=None) -> dict:
	"""Versioning state of the live attachment bucket (A27).

	Here rather than in `backup/retention.py` or `storage/diagnostics.py` for this module's
	founding reason: every S3 call the app makes goes through one place that owns the
	timeouts, the breaker and the typed errors. A probe that reached for its own boto3
	client would also be invisible to the test fake, and would make an offline unit suite
	dial out.

	PLAN §H item 1 adopts attachment-bucket versioning as half of the recovery window, so
	whether it is on is an operator-facing fact rather than a deployment assumption. Both
	phases arrived at this function independently and for those two reasons; both are kept
	because both are load-bearing.
	"""
	settings = settings or get_settings()
	s3 = client.get_client(profile=client.PROFILE_REQUEST, settings=settings)
	return _call("get_bucket_versioning", s3.get_bucket_versioning, Bucket=settings.bucket)


def get_bucket_lifecycle(*, settings=None) -> dict:
	"""Lifecycle configuration of the live attachment bucket; `{"Rules": []}` when unset."""
	from botocore.exceptions import ClientError

	settings = settings or get_settings()
	s3 = client.get_client(profile=client.PROFILE_REQUEST, settings=settings)
	try:
		response = _call(
			"get_bucket_lifecycle_configuration",
			s3.get_bucket_lifecycle_configuration,
			Bucket=settings.bucket,
		)
	except CloudStorageError as exc:
		cause = exc.__cause__
		if isinstance(cause, ClientError) and cause.response.get("Error", {}).get("Code") in (
			"NoSuchLifecycleConfiguration",
			"NoSuchLifecycleConfigurationError",
		):
			return {"Rules": []}
		raise
	return {"Rules": response.get("Rules") or []}


def log_operation(operation: str, cso, **context):
	frappe.logger("cloud_file_storage").info(
		{
			"operation": operation,
			"cso": getattr(cso, "name", None),
			"key": getattr(cso, "s3_key", None),
			**context,
		}
	)


def list_objects(
	prefix: str | None = None,
	*,
	continuation_token: str | None = None,
	page_size: int = 1000,
	profile: str = client.PROFILE_JOB,
	settings=None,
) -> dict:
	"""One page of `list_objects_v2`, for the reconcile sweep (design §10).

	Added in P5. It is here rather than in `migration/reconcile.py` for the same reason
	everything else in this module is: every S3 call the app makes goes through one place
	that owns the timeouts, the breaker and the typed errors. Returns the raw response so
	the caller can persist `NextContinuationToken` and resume.
	"""
	settings = settings or get_settings()
	s3 = client.get_client(profile=profile, settings=settings)
	params = {"Bucket": settings.bucket, "MaxKeys": cint(page_size) or 1000}
	if prefix:
		params["Prefix"] = prefix
	if continuation_token:
		params["ContinuationToken"] = continuation_token
	return _call("list_objects_v2", s3.list_objects_v2, **params)
