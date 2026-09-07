"""Thumbnails as cloud-managed derived objects (PLAN.md §A, superseding ADR-11/A17).

A thumbnail is derived data belonging to its source File/CSO, not an independent file:
core's `make_thumbnail` writes it straight to `public/files` and records only
`thumbnail_url` on the source row (`file.py:462-468`) — there is no File row of its own.
So the derived object is keyed off the SOURCE hash plus the rendered dimensions and linked
back with `derived_of`, and `PublicFileRenderer` finds it from the source row's
`thumbnail_url`.

The rule this module exists to keep honest: **a local thumbnail is never deleted until the
source object is verified AND thumbnail availability is guaranteed** —
:func:`can_release_local_thumbnail` is that gate, and it is what P5's CLEANUP must ask
before it moves a `_small.` file anywhere.
"""

import os
import re

import frappe
from frappe.utils import get_files_path
from frappe.utils.file_manager import is_safe_path

from cloud_file_storage.storage import engine, objects
from cloud_file_storage.storage.hashing import digest_bytes
from cloud_file_storage.storage.keys import build_derived_object_key
from cloud_file_storage.storage.modes import get_settings

#: `foo_small.png` → `small`. Core builds the name as `{filename}_{suffix}.{extn}`.
_SUFFIX_PATTERN = re.compile(r"_(?P<suffix>[A-Za-z0-9]+)$")

CSO_DOCTYPE = "Cloud Storage Object"


def suffix_from_url(thumbnail_url: str | None) -> str | None:
	"""The `suffix` core appended, read back off the URL."""
	if not thumbnail_url:
		return None
	stem = os.path.splitext(os.path.basename(thumbnail_url))[0]
	match = _SUFFIX_PATTERN.search(stem)
	return match.group("suffix") if match else None


#: The File field naming the object that serves that row's `thumbnail_url`.
THUMBNAIL_LINK_FIELD = "cloud_thumbnail_object"


def thumbnail_cso_for(file_doc):
	"""The object serving this File's `thumbnail_url`.

	The File-side link is authoritative: a thumbnail's bytes can be the source's own bytes
	(see `objects.ensure_derived_cso`), in which case there is no separate derived row to
	find. `derived_of` is consulted as a fallback for rows written before the link existed.
	"""
	linked = file_doc.get(THUMBNAIL_LINK_FIELD)
	if linked:
		cso = objects.get_cso(linked)
		if cso is not None:
			return cso

	resolver = getattr(file_doc, "_resolve_cso", None)
	source = resolver() if resolver else objects.get_cso(file_doc.get("cloud_storage_object"))
	if source is None:
		return None
	return find_derived_cso(source.name, suffix_from_url(file_doc.get("thumbnail_url")))


def find_derived_cso(source_cso_name: str, suffix: str | None):
	"""The derived object rendered from ``source_cso_name`` with this suffix.

	Newest first: re-rendering at a different size with the same suffix produces a second
	object, and the freshest is the one the source row's `thumbnail_url` refers to.
	"""
	if not source_cso_name or not suffix:
		return None

	name = frappe.db.get_value(
		CSO_DOCTYPE,
		{"derived_of": source_cso_name, "derived_suffix": suffix},
		"name",
		order_by="modified desc",
	)
	return objects.get_cso(name) if name else None


def store_thumbnail(
	source_cso,
	content: bytes,
	*,
	suffix: str,
	width: int,
	height: int,
	mime_type: str | None = None,
	settings=None,
):
	"""Upload one rendered thumbnail as a derived object. Returns the derived CSO.

	The derived object's visibility is the SOURCE's, never a hardcoded public: a thumbnail is
	a rendering of the source's bytes, so a public stamp on the thumbnail of a private file
	is a false statement about what the object holds (A19). It does not change the key —
	derived objects live under `thm/` whatever their visibility (`keys.build_derived_object_
	key`) — but it is what makes the byte-identical-to-source case resolve to the source's own
	object instead of minting a second, differently-labelled copy of private bytes.
	"""
	settings = settings or get_settings()
	digest = digest_bytes(content)
	s3_key = build_derived_object_key(
		source_cso.content_sha256, width, height, key_prefix=settings.key_prefix
	)

	derived = objects.ensure_derived_cso(
		s3_key=s3_key,
		derived_of=source_cso.name,
		derived_suffix=suffix,
		content_sha256=digest.sha256,
		content_hash_md5=digest.md5,
		file_size=digest.size,
		visibility=source_cso.visibility,
		mime_type=mime_type,
		settings=settings,
	)

	if objects.needs_upload(derived):
		engine.put_bytes(derived, content, settings=settings)
		objects.set_cso_status(derived.name, "uploaded")
		derived.reload()

	return derived


def local_thumbnail_path(thumbnail_url: str | None) -> str | None:
	"""Where a thumbnail's LOCAL copy belongs, from its URL. None for anything else.

	Core is inconsistent here and this app follows the safe half of it. Core *writes* every
	thumbnail under `public/` (`file.py:463`: `get_site_path("public", thumbnail_url.lstrip("/"))`),
	so a private file's thumbnail lands in `public/private/files/…` — which nginx then serves
	to anybody, because its rule is `try_files /<site>/public/$uri @webserver`. Core's own
	`delete_file` looks for that same thumbnail in `private/files/` instead
	(`file_manager.py:317-322`). A rendering of a private file must not be anonymously
	readable (A19), so the delete side wins: private thumbnails live in `private/files/`,
	where only `download_private_file` can reach them.

	**Why the URL is flattened to its basename.** Core's `delete_file` flattens the same way
	— `parts = os.path.split(path.strip("/"))` and then `parts[-1]` — so a thumbnail written
	under a subdirectory is not the file core would ever delete. Reconstructing the path with
	the subdirectory intact would produce a copy core cannot clean up, which is how the
	upstream leak documented above happens in the first place. Agreeing with `delete_file`
	byte for byte is therefore the point, not a simplification: every local thumbnail this app
	writes is one core can also remove.

	The consequence is that this function is authoritative rather than advisory.
	`serving/private.py` derives its `send_private_file` argument from the path this returns
	(`os.path.relpath` of the path that was just proved to exist) instead of re-deriving it
	from the URL, so the existence check and the send cannot be made to disagree by a later
	change to either; `overrides/file.py` uses it for both ends of the privacy-flip relocation
	for the same reason.
	"""
	if not thumbnail_url:
		return None

	if thumbnail_url.startswith("/private/files/"):
		is_private = 1
	elif thumbnail_url.startswith("/files/"):
		is_private = 0
	else:
		return None

	basename = os.path.basename(thumbnail_url)
	if not basename:
		return None

	path = get_files_path(basename, is_private=is_private)
	return path if is_safe_path(path) else None


def can_release_local_thumbnail(file_doc) -> tuple[bool, str]:
	"""PLAN.md §A: may this File's LOCAL thumbnail be quarantined or deleted?

	Both halves have to hold, and the second is the one that is easy to forget:

	1. the **source** object is `verified` — an unverified source cannot regenerate the
	   thumbnail if the derived object turns out to be bad;
	2. thumbnail **availability is guaranteed** — a derived object exists for this
	   thumbnail URL and is itself `verified`.

	Returns `(allowed, reason)`; the reason is what an operator sees in a blocked-cleanup
	conflict row, so it names which half failed.
	"""
	thumbnail_url = file_doc.get("thumbnail_url")
	if not thumbnail_url:
		return False, "no thumbnail_url on this File"

	resolver = getattr(file_doc, "_resolve_cso", None)
	source = resolver() if resolver else objects.get_cso(file_doc.get("cloud_storage_object"))
	if source is None:
		return False, "source object is not resolvable"
	if source.status != "verified":
		return False, f"source object is {source.status}, not verified"

	derived = thumbnail_cso_for(file_doc)
	if derived is None:
		return False, "no derived thumbnail object exists"
	if derived.status != "verified":
		return False, f"derived thumbnail object is {derived.status}, not verified"

	return True, "source and derived thumbnail are both verified"


def release_derived_objects(source_cso_name: str, *, settings=None) -> int:
	"""Dereference every thumbnail of a source object. Returns how many were released.

	Called when the source File goes away: the derived objects have no other referent, so
	they follow it into the normal deferred-GC path — scheduled, never deleted inline.

	The source object itself is skipped even if it is also serving as a thumbnail: it has
	just been through `release_reference`, which is the only place allowed to decide its
	fate against a locking recount.
	"""
	if not source_cso_name:
		return 0

	settings = settings or get_settings()
	released = 0
	for name in frappe.get_all(CSO_DOCTYPE, filters={"derived_of": source_cso_name}, pluck="name"):
		if name == source_cso_name:
			continue
		# `release_reference` is the reviewed path: it takes the row lock, re-counts under
		# it, and only then schedules. Hand-rolling the transition here would add a second
		# place that can put an object into `pending_delete` without a locking recount.
		if objects.release_reference(name, settings=settings):
			released += 1
	return released
