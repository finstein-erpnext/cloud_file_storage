"""Thumbnail migration — existing local thumbnails become derived objects (PLAN §A).

The policy this implements, in the order it matters:

* a thumbnail is **derived data of its source**, not an independent attachment: it has no
  File row of its own, only a `thumbnail_url` on the source row, so its object is keyed off
  the source's content hash plus the rendered dimensions;
* an existing local thumbnail is **migrated** (uploaded under the derived key) rather than
  re-rendered, so the bytes users have been seeing stay the bytes they see;
* a **missing or corrupt** one is regenerated from the source — and only from a *verified*
  source, because regeneration reads the source's bytes and a thumbnail rendered from
  unverified bytes is a second unverified object rather than a repair;
* nothing local is deleted here. `can_release_local_thumbnail` is CLEANUP's gate, and it
  demands both the source object and the derived object be `verified`.

Building the derived *key* needs only the source's `content_sha256`, so migrating an
existing thumbnail is allowed while the source is still merely `uploaded`. Regenerating is
not. That asymmetry is deliberate and is the only place the two paths differ in what they
require of the source.
"""

import io
import os

import frappe
from frappe.utils import cint, now_datetime

from cloud_file_storage.migration import audit, engine
from cloud_file_storage.serving import thumbnails as serving_thumbnails
from cloud_file_storage.storage import objects
from cloud_file_storage.storage.hashing import digest_bytes
from cloud_file_storage.storage.modes import get_settings

OBJECT_DOCTYPE = "Cloud Migration Object"
REF_DOCTYPE = "Cloud Migration File Ref"

#: How long a thumbnail object waits when its source is not ready yet.
SOURCE_WAIT_SECONDS = 120


def source_cso_for(obj):
	"""The Cloud Storage Object of this thumbnail's source, or None while it is not ready."""
	if not obj.get("parent_object"):
		return None
	cso_name = frappe.db.get_value(OBJECT_DOCTYPE, obj.parent_object, "cloud_storage_object")
	if not cso_name:
		return None
	cso = objects.get_cso(cso_name)
	if cso is None or not cso.content_sha256:
		return None
	return cso


def upload_thumbnail_object(obj, camp, *, settings=None) -> int:
	"""Turn one thumbnail row into a derived object. Returns the bytes moved."""
	settings = settings or get_settings()

	source = source_cso_for(obj)
	if source is None:
		_wait_or_skip(obj, camp)
		return 0

	if not engine.cas_object(obj.name, expected="Pending", to="Uploading"):
		return 0

	try:
		content = _local_thumbnail_bytes(obj)
		if content is None:
			return _regenerate(obj, camp, source, settings=settings)

		width, height = _dimensions(content)
		if not width or not height:
			return _regenerate(obj, camp, source, settings=settings)

		derived = serving_thumbnails.store_thumbnail(
			source,
			content,
			suffix=serving_thumbnails.suffix_from_url(obj.file_url) or "small",
			width=width,
			height=height,
			mime_type=_mime_type(obj.file_url),
			settings=settings,
		)
		_link_thumbnail(obj, derived)

		digest = digest_bytes(content)
		engine.cas_object(
			obj.name,
			expected="Uploading",
			to="Uploaded",
			cloud_storage_object=derived.name,
			sha256=digest.sha256,
			md5=digest.md5,
			size_bytes=digest.size,
			uploaded_at=now_datetime(),
			error_class=None,
			last_error=None,
		)
		return digest.size

	except Exception as exc:  # noqa: BLE001 - per-object isolation, same as any other object
		engine._record_object_failure(obj, camp, exc)
		return 0


def _local_thumbnail_bytes(obj) -> bytes | None:
	from cloud_file_storage.migration import analyzer

	path = obj.disk_path or serving_thumbnails.local_thumbnail_path(obj.file_url)
	if not path or not os.path.exists(path):
		return None
	# Same Desk-writable column, same publication risk: these bytes are uploaded as a derived
	# object. `local_thumbnail_path` is already confined; `obj.disk_path` was not.
	if not analyzer.confined_to_site_files(path):
		return None
	try:
		with open(path, "rb") as handle:  # nosemgrep: frappe-security-file-traversal
			return handle.read()
	except OSError:
		return None


def _dimensions(content: bytes) -> tuple[int, int]:
	"""The rendered size, read from the bytes — the derived key encodes it."""
	try:
		from PIL import Image

		with Image.open(io.BytesIO(content)) as image:
			return int(image.width), int(image.height)
	except Exception:  # noqa: BLE001 - an unreadable thumbnail is a regeneration case
		return 0, 0


def _mime_type(url: str | None) -> str | None:
	import mimetypes

	return mimetypes.guess_type(url)[0] if url else None


def _regenerate(obj, camp, source, *, settings=None) -> int:
	"""PLAN §A — re-render from the source, but only from a **verified** one."""
	if source.status != "verified":
		engine.cas_object(obj.name, expected="Uploading", to="Pending")
		_defer(obj, "source object is not verified yet; regeneration needs verified bytes")
		return 0

	files = frappe.db.get_all(
		REF_DOCTYPE, filters={"migration_object": obj.parent_object}, pluck="file", limit_page_length=0
	)
	for name in files:
		doc = frappe.get_doc("File", name)
		if doc.make_thumbnail(set_as_thumbnail=True):
			break
	else:
		engine._open_conflict(
			obj,
			camp,
			conflict_type="missing_physical",
			severity="Warning",
			details={
				"reason": "local thumbnail is missing or unreadable and could not be regenerated",
				"thumbnail_url": obj.file_url,
			},
			error_class="ThumbnailUnavailable",
		)
		return 0

	derived = serving_thumbnails.thumbnail_cso_for(frappe.get_doc("File", files[0]))
	engine.cas_object(
		obj.name,
		expected="Uploading",
		to="Uploaded",
		cloud_storage_object=derived.name if derived else None,
		size_bytes=cint(derived.file_size) if derived else 0,
		sha256=derived.content_sha256 if derived else None,
		md5=derived.content_hash_md5 if derived else None,
		uploaded_at=now_datetime(),
		skip_reason="thumbnail_regenerated",
	)
	audit.record(
		"remote_adopt",
		campaign=camp.name,
		migration_object=obj.name,
		cloud_storage_object=derived.name if derived else None,
		action="thumbnail_regenerated",
		thumbnail_url=obj.file_url,
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)
	return cint(derived.file_size) if derived else 0


def _link_thumbnail(obj, derived):
	"""Point the source's File rows at the object that now serves their `thumbnail_url`.

	Written through `frappe.db.set_value`, not raw SQL: docs/INVARIANTS.md invariant 5 sanctions three
	raw-SQL sites against `tabFile` and this is not one of them. It is also not the A26 link
	— `cloud_thumbnail_object` names the object behind the *thumbnail* URL, which no runtime
	path competes for.
	"""
	for name in frappe.db.get_all(
		REF_DOCTYPE, filters={"migration_object": obj.parent_object}, pluck="file", limit_page_length=0
	):
		current = frappe.db.get_value("File", name, serving_thumbnails.THUMBNAIL_LINK_FIELD)
		if current != derived.name:
			frappe.db.set_value(
				"File",
				name,
				serving_thumbnails.THUMBNAIL_LINK_FIELD,
				derived.name,
				update_modified=False,
			)


def _wait_or_skip(obj, camp):
	"""Wait for the source — but not forever.

	A thumbnail whose source cannot produce an object (its File rows are gone, it is in
	Conflict, an operator skipped it) would otherwise defer on every pass, and each pass is a
	dispatched job. Bounding it here is what keeps a stale `_small.png` on a site from
	consuming a batch's whole attempt budget and marking it Failed. Skipping costs nothing:
	no local file is touched, and the thumbnail keeps being served from disk.
	"""
	parent_status = (
		frappe.db.get_value(OBJECT_DOCTYPE, obj.parent_object, "status") if obj.get("parent_object") else None
	)
	unreachable = parent_status in (None, "Conflict", "Skipped", "Failed")
	exhausted = cint(obj.attempt_count) >= cint(camp.max_attempts or 5)

	if unreachable or exhausted:
		frappe.db.set_value(
			OBJECT_DOCTYPE,
			obj.name,
			{
				"status": "Skipped",
				"skip_reason": "thumbnail_source_unavailable",
				"next_retry_at": None,
				"last_error": f"source object status: {parent_status}",
			},
			update_modified=False,
		)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)
		return

	frappe.db.set_value(
		OBJECT_DOCTYPE, obj.name, {"attempt_count": cint(obj.attempt_count) + 1}, update_modified=False
	)
	_defer(obj, "source object is not uploaded yet")


def _defer(obj, reason: str):
	"""Wait for the source rather than failing — this is ordering, not an error."""
	from frappe.utils import add_to_date

	frappe.db.set_value(
		OBJECT_DOCTYPE,
		obj.name,
		{
			"next_retry_at": add_to_date(now_datetime(), seconds=SOURCE_WAIT_SECONDS),
			"last_error": reason,
		},
		update_modified=False,
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)
