"""boto3 client construction, transfer settings and the ExtraArgs builder.

Two clients, deliberately (A30):

* the **request-path** client has hard 3s/15s timeouts and 2 retry attempts, so a hung S3
  can never hold a web worker;
* the **job** client has generous timeouts, because a 4GB multipart upload on the
  `cloud_migration` queue must not be killed by a 15-second read timeout.

Clients are cached per (site, settings.modified, profile): the fork rebuilt a client for
every single upload.
"""

import threading

import boto3
import frappe
from boto3.s3.transfer import TransferConfig
from botocore.client import Config
from frappe.utils import cint

from cloud_file_storage.storage.exceptions import CloudStorageConfigurationError
from cloud_file_storage.storage.modes import get_settings

#: A30 — request path. Bounded so an S3 outage degrades files, not the ERP.
REQUEST_CONNECT_TIMEOUT = 3
REQUEST_READ_TIMEOUT = 15
REQUEST_MAX_ATTEMPTS = 2

#: Background jobs (migration, repair, GC, backup): long transfers, more patience.
JOB_CONNECT_TIMEOUT = 10
JOB_READ_TIMEOUT = 300
JOB_MAX_ATTEMPTS = 5

PROFILE_REQUEST = "request"
PROFILE_JOB = "job"

#: PLAN.md §A — live objects must be synchronously retrievable. Archive classes belong to
#: the backup bucket and are rejected here (F6).
SYNC_RETRIEVAL_STORAGE_CLASSES = ("STANDARD", "STANDARD_IA", "INTELLIGENT_TIERING")

MB = 1024 * 1024

_clients: dict[tuple, "boto3.client"] = {}
_clients_lock = threading.Lock()


def _client_config(profile: str, addressing_style: str | None) -> Config:
	if profile == PROFILE_JOB:
		connect_timeout, read_timeout, attempts = (
			JOB_CONNECT_TIMEOUT,
			JOB_READ_TIMEOUT,
			JOB_MAX_ATTEMPTS,
		)
	else:
		connect_timeout, read_timeout, attempts = (
			REQUEST_CONNECT_TIMEOUT,
			REQUEST_READ_TIMEOUT,
			REQUEST_MAX_ATTEMPTS,
		)

	kwargs = {
		"signature_version": "s3v4",
		"connect_timeout": connect_timeout,
		"read_timeout": read_timeout,
		"retries": {"max_attempts": attempts, "mode": "standard"},
	}
	if addressing_style:
		kwargs["s3"] = {"addressing_style": addressing_style}
	return Config(**kwargs)


def resolve_addressing_style(settings) -> str:
	"""`auto` means path-style whenever a custom endpoint is configured.

	Custom S3 endpoints (MinIO, Hetzner) are only reliable with path-style requests.
	"""
	endpoint_url = (settings.endpoint_url or "").strip().rstrip("/")
	style = (settings.addressing_style or "auto").strip()
	if style == "auto":
		return "path" if endpoint_url else ""
	return style


def get_client(*, profile: str = PROFILE_REQUEST, settings=None):
	settings = settings or get_settings()
	cache_key = (frappe.local.site, settings.modified, profile)

	client = _clients.get(cache_key)
	if client is not None:
		return client

	with _clients_lock:
		client = _clients.get(cache_key)
		if client is not None:
			return client

		# Static keys are used only when the IAM default chain is switched off; with the
		# chain on (the A12 default) boto3 resolves credentials itself.
		access_key_id = secret_access_key = None
		if not cint(settings.use_default_credential_chain):
			secret = settings.get_password("secret_access_key", raise_exception=False)
			if settings.access_key_id and secret:
				access_key_id, secret_access_key = settings.access_key_id, secret

		# Keep the cache from growing without bound as settings are edited.
		if len(_clients) > 16:
			_clients.clear()
		client = build_s3_client(
			region=settings.region,
			endpoint_url=settings.endpoint_url,
			addressing_style=resolve_addressing_style(settings),
			access_key_id=access_key_id,
			secret_access_key=secret_access_key,
			profile=profile,
		)
		_clients[cache_key] = client
		return client


def build_s3_client(
	*,
	region: str | None = None,
	endpoint_url: str | None = None,
	addressing_style: str | None = None,
	access_key_id: str | None = None,
	secret_access_key: str | None = None,
	profile: str = PROFILE_REQUEST,
):
	"""Construct an S3 client from explicit connection parameters.

	`get_client` reads those parameters off the Cloud Storage Settings singleton; the backup
	domain reads them off `Cloud Backup Settings`, which may point at a different bucket in a
	different account. Both come through here so the timeout/retry profile (A30) is owned in
	exactly one place — a backup client with unbounded timeouts would be the same hazard on
	the job queue that `get_client` exists to prevent on the request path.
	"""
	client_kwargs = {"region_name": region or None}
	endpoint_url = (endpoint_url or "").strip().rstrip("/")
	if endpoint_url:
		client_kwargs["endpoint_url"] = endpoint_url
	if access_key_id and secret_access_key:
		client_kwargs["aws_access_key_id"] = access_key_id
		client_kwargs["aws_secret_access_key"] = secret_access_key
	client_kwargs["config"] = _client_config(profile, addressing_style)
	return boto3.client("s3", **client_kwargs)


def clear_client_cache():
	with _clients_lock:
		_clients.clear()


def get_transfer_config(settings=None) -> TransferConfig:
	settings = settings or get_settings()
	return TransferConfig(
		multipart_threshold=(cint(settings.multipart_threshold_mb) or 64) * MB,
		multipart_chunksize=(cint(settings.multipart_chunksize_mb) or 16) * MB,
		max_concurrency=cint(settings.transfer_max_concurrency) or 4,
		use_threads=True,
	)


def multipart_threshold_bytes(settings=None) -> int:
	settings = settings or get_settings()
	return (cint(settings.multipart_threshold_mb) or 64) * MB


def validate_storage_class(settings=None) -> str:
	"""Hard assert: the live attachment bucket never uses an archive class (F6)."""
	settings = settings or get_settings()
	storage_class = (settings.storage_class or "STANDARD").strip()
	if storage_class not in SYNC_RETRIEVAL_STORAGE_CLASSES:
		raise CloudStorageConfigurationError(
			f"storage_class {storage_class!r} is not synchronously retrievable; "
			f"live objects must use one of {', '.join(SYNC_RETRIEVAL_STORAGE_CLASSES)}"
		)
	return storage_class


def build_extra_args(*, content_type: str | None, content_sha256: str, settings=None) -> dict:
	"""ExtraArgs for every PUT.

	Invariants encoded here, not in the callers:

	* **no `ACL` key, ever** — the bucket is private and public delivery is a
	  CloudFront-OAC/presign concern (docs/INVARIANTS.md invariant 6, F6 grep gate);
	* checksummed PUT (`ChecksumAlgorithm: SHA256`) so S3 validates the bytes it stored;
	* metadata carries the sha256 and an app marker for orphan reconciliation — no
	  filename, no doctype, no PII.
	"""
	settings = settings or get_settings()
	storage_class = validate_storage_class(settings)

	extra_args = {
		"ContentType": content_type or "application/octet-stream",
		"ChecksumAlgorithm": "SHA256",
		"Metadata": {"sha256": content_sha256, "app": "cloud_file_storage"},
	}

	if storage_class != "STANDARD":
		extra_args["StorageClass"] = storage_class

	sse_mode = (settings.sse_mode or "").strip()
	if sse_mode == "SSE-KMS":
		extra_args["ServerSideEncryption"] = "aws:kms"
		if settings.kms_key_id:
			extra_args["SSEKMSKeyId"] = settings.kms_key_id
	elif sse_mode == "SSE-S3":
		extra_args["ServerSideEncryption"] = "AES256"

	return extra_args
