"""Adoption of bytes that are already in the bucket (design §8, A15, A18).

Two populations, one mechanism:

* **legacy fork rows** — the 0.2.x app wrote `/api/method/frappe_s3_attachment.controller.
  generate_file?key=…` into `file_url` and the object key into `File.s3_object_key`;
* **remote https rows** — the fork's public path wrote raw bucket URLs into `file_url`.

Neither is re-uploaded and neither moves a byte. What adoption produces is a Cloud Storage
Object pointing at the existing key, so serving, GC and reference counting can see those
bytes at all.

**A15 honesty is structural here, not a convention.** `objects.adopt_legacy_cso` writes
`legacy_unverified` and takes no argument that could make it write anything better; the only
route to `verified` is :func:`promote_adopted_object`, which streams the object back and
hashes it. The ETag shortcut is applied only where the ETag provably *is* the MD5 — no `-`
in it (single-part) and server-side encryption absent or AES256 — because for a multipart or
SSE-KMS object the ETag is a different function of the bytes entirely, and writing it into
`content_hash` would corrupt core's dedup for every future upload of those bytes.
"""

import urllib.parse

import frappe
from frappe.utils import cint

from cloud_file_storage.migration import audit, engine
from cloud_file_storage.storage import engine as storage_engine
from cloud_file_storage.storage import objects
from cloud_file_storage.storage.exceptions import CloudObjectNotFound
from cloud_file_storage.storage.hashing import digest_bytes
from cloud_file_storage.storage.modes import get_settings

OBJECT_DOCTYPE = "Cloud Migration Object"

#: SSE modes under which an S3 ETag is still the object's MD5. Anything else (SSE-KMS,
#: SSE-C, or a multipart upload) makes it a different value with the same shape.
ETAG_IS_MD5_SSE = (None, "", "AES256")


def parse_remote_url(url: str | None) -> dict | None:
	"""`{bucket, key}` when a remote URL names the bucket this site is configured for (A18).

	Returns None for anything else, and that is the whole safety property: adopting a URL
	that points at somebody else's bucket would create a Cloud Storage Object claiming bytes
	this site does not own and cannot serve, and would mark the File "migrated".

	Recognises the three shapes a bucket URL takes: virtual-hosted
	(`https://bucket.s3.region.amazonaws.com/key`), path-style
	(`https://s3.region.amazonaws.com/bucket/key`), and a configured custom endpoint with
	either style (MinIO, Ceph, Wasabi).
	"""
	if not url:
		return None

	settings = get_settings()
	bucket = (settings.bucket or "").strip()
	if not bucket:
		return None

	parsed = urllib.parse.urlsplit(url.strip())
	if parsed.scheme not in ("http", "https") or not parsed.netloc:
		return None

	host = parsed.netloc.split("@")[-1].split(":")[0].lower()
	path = urllib.parse.unquote(parsed.path).lstrip("/")
	if not path:
		return None

	endpoint_host = ""
	if settings.endpoint_url:
		endpoint_host = urllib.parse.urlsplit(settings.endpoint_url).netloc.split(":")[0].lower()

	# Virtual-hosted: the bucket is the leading label of the host.
	if host.startswith(f"{bucket.lower()}."):
		return {"bucket": bucket, "key": path}

	# Path-style: the bucket is the first path segment, on an S3 or the configured host.
	first, _, rest = path.partition("/")
	if first == bucket and rest and (endpoint_host == host or ".amazonaws.com" in host):
		return {"bucket": bucket, "key": rest}

	if endpoint_host and host == endpoint_host and first == bucket and rest:
		return {"bucket": bucket, "key": rest}

	return None


def _remote_probe(bucket: str, key: str, settings):
	"""A CSO-shaped stand-in so `storage.engine.head` can look at an arbitrary key."""
	return frappe._dict({"bucket": bucket, "s3_key": key, "mime_type": None, "name": None})


def etag_md5(head: dict, settings) -> str | None:
	"""The ETag, but only when it is provably the MD5 (A15)."""
	etag = (head.get("ETag") or "").strip('"')
	if not etag or "-" in etag:
		return None
	sse = head.get("ServerSideEncryption")
	if sse not in ETAG_IS_MD5_SSE:
		return None
	if head.get("SSECustomerAlgorithm"):
		return None
	return etag.lower()


def adopt_object(obj, camp, *, settings=None) -> bool:
	"""Adopt one object's bytes in place. `Pending → Adopted`; VERIFY links its refs.

	Deliberately does NOT link the File rows itself, although design §8 step 3 describes it
	that way: the guarded link (A26) exists in exactly one place, and routing adoption
	through it is what keeps an adopted row from clobbering a File the live runtime has
	already relinked.
	"""
	settings = settings or get_settings()
	bucket = obj.get("legacy_bucket") or settings.bucket
	key = obj.get("legacy_key")

	if not key:
		engine._open_conflict(
			obj,
			camp,
			conflict_type="adoption_failed",
			severity="Blocker",
			details={"reason": "no legacy object key on this row", "file_url": obj.file_url},
			error_class="MissingLegacyKey",
		)
		return False

	if not engine.cas_object(obj.name, expected="Pending", to="Uploading"):
		return False

	try:
		head = storage_engine.head(_remote_probe(bucket, key, settings), settings=settings)
	except CloudObjectNotFound:
		engine._open_conflict(
			obj,
			camp,
			conflict_type="adoption_failed",
			severity="Blocker",
			details={"reason": "the legacy object does not exist", "bucket": bucket, "key": key},
			error_class="CloudObjectNotFound",
		)
		return False
	except Exception as exc:  # noqa: BLE001 - transient failures retry like any other object
		engine.cas_object(
			obj.name,
			expected="Uploading",
			to="Pending",
			attempt_count=cint(obj.attempt_count) + 1,
			error_class=type(exc).__name__,
			last_error=str(exc)[:500],
		)
		return False

	cso = objects.adopt_legacy_cso(
		s3_key=key,
		bucket=bucket,
		file_size=cint(head.get("ContentLength")),
		visibility="private" if cint(obj.is_private) else "public",
		content_hash_md5=etag_md5(head, settings),
		mime_type=head.get("ContentType"),
		etag=(head.get("ETag") or "").strip('"') or None,
		migration_batch=obj.get("batch"),
		settings=settings,
	)

	engine.cas_object(
		obj.name,
		expected="Uploading",
		to="Adopted",
		cloud_storage_object=cso.name,
		md5=cso.content_hash_md5,
		size_bytes=cint(head.get("ContentLength")),
		legacy_bucket=bucket,
		legacy_key=key,
		uploaded_at=frappe.utils.now_datetime(),
	)
	audit.record(
		"remote_adopt",
		campaign=camp.name,
		migration_object=obj.name,
		cloud_storage_object=cso.name,
		bucket=bucket,
		key=key,
		size_bytes=cint(head.get("ContentLength")),
		etag_is_md5=bool(cso.content_hash_md5),
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)
	return True


def promote_adopted_object(cso_name: str, *, settings=None) -> bool:
	"""A15 — stream the adopted object back, hash it, and only then call it `verified`.

	This is the *only* way a `legacy_unverified` object becomes `verified`. It is a real
	read of every byte, so it is a background job rather than part of the migration's
	critical path: an adopted object is servable while it waits.
	"""
	settings = settings or get_settings()
	cso = objects.get_cso(cso_name)
	if cso is None or cso.status != "legacy_unverified":
		return False

	content = storage_engine.get_bytes(cso, settings=settings)
	digest = digest_bytes(content)

	if cint(cso.file_size) and digest.size != cint(cso.file_size):
		objects.record_failure(cso.name, f"adopted object size drifted: {digest.size} != {cso.file_size}")
		return False
	if cso.content_hash_md5 and cso.content_hash_md5.lower() != digest.md5:
		objects.record_failure(cso.name, "adopted object MD5 does not match its ETag")
		return False

	objects.set_cso_status(
		cso.name,
		"verified",
		content_sha256=digest.sha256,
		content_hash_md5=digest.md5,
		file_size=digest.size,
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)
	audit.record("remote_adopt", cloud_storage_object=cso.name, action="promoted", sha256=digest.sha256)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)
	return True


def promote_adopted_batch(limit: int = 100, *, settings=None) -> dict:
	"""Background backfill: promote adopted objects a page at a time."""
	settings = settings or get_settings()
	names = frappe.db.get_all(
		objects.CSO_DOCTYPE,
		filters={"status": "legacy_unverified"},
		pluck="name",
		limit_page_length=limit,
	)
	promoted = 0
	for name in names:
		try:
			if promote_adopted_object(name, settings=settings):
				promoted += 1
		except Exception as exc:  # noqa: BLE001 - one bad object must not stop the backfill
			objects.record_failure(name, str(exc))
			frappe.db.commit()
	return {"candidates": len(names), "promoted": promoted}


def adoptable_objects(campaign: str) -> list[str]:
	return frappe.db.get_all(
		OBJECT_DOCTYPE,
		filters={
			"campaign": campaign,
			"status": "Pending",
			"identity_kind": ("in", ("legacy_fork_key", "remote_https")),
		},
		pluck="name",
		limit_page_length=0,
	)
