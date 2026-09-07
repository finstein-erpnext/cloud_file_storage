"""Content-addressed S3 key scheme (FROZEN — PLAN.md §A "Identity").

	<key_prefix>/<site>/<pub|prv>/<sha256[:2]>/<sha256[2:4]>/<sha256>

The human filename is NEVER part of the key: it is neither unique nor free of PII. The
original name lives in `tabFile.file_name` and is applied at serve time through
`ResponseContentDisposition`. Two 2-hex shard levels give 65,536 prefixes, so 1.2M objects
never build a hot prefix.
"""

import re

import frappe

PUBLIC_SEGMENT = "pub"
PRIVATE_SEGMENT = "prv"
#: Derived (thumbnail) objects live under their own segment, keyed by the SOURCE sha256 plus
#: the rendered dimensions (PLAN.md §A). Same source + same size ⇒ same key, so a rerender
#: converges instead of accumulating.
DERIVED_SEGMENT = "thm"
VISIBILITY_PUBLIC = "public"
VISIBILITY_PRIVATE = "private"

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
# A prefix is a plain path fragment: no traversal, no leading/trailing slashes, no spaces.
_PREFIX_ALLOWED = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/]*$")


def visibility_for(is_private) -> str:
	from frappe.utils import cint

	return VISIBILITY_PRIVATE if cint(is_private) else VISIBILITY_PUBLIC


def visibility_segment(visibility: str) -> str:
	return PRIVATE_SEGMENT if visibility == VISIBILITY_PRIVATE else PUBLIC_SEGMENT


def normalize_key_prefix(key_prefix: str | None) -> str:
	"""Strip slashes and reject traversal. Returns "" when unset."""
	prefix = (key_prefix or "").strip().strip("/")
	if not prefix:
		return ""
	if ".." in prefix.split("/") or not _PREFIX_ALLOWED.match(prefix):
		raise ValueError(f"invalid key_prefix: {key_prefix!r}")
	return prefix


def site_segment(site: str | None = None) -> str:
	return site or frappe.local.site


def build_object_key(
	content_sha256: str,
	visibility: str,
	*,
	key_prefix: str | None = None,
	site: str | None = None,
) -> str:
	sha256 = (content_sha256 or "").lower()
	if not _SHA256_PATTERN.match(sha256):
		raise ValueError(f"content_sha256 must be 64 lowercase hex characters, got {content_sha256!r}")

	segments = []
	prefix = normalize_key_prefix(key_prefix)
	if prefix:
		segments.append(prefix)
	segments += [
		site_segment(site),
		visibility_segment(visibility),
		sha256[:2],
		sha256[2:4],
		sha256,
	]
	return "/".join(segments)


def build_derived_object_key(
	source_sha256: str,
	width: int,
	height: int,
	*,
	key_prefix: str | None = None,
	site: str | None = None,
) -> str:
	"""`<prefix>/<site>/thm/<src[:2]>/<src[2:4]>/<src>_<w>x<h>` (PLAN.md §A).

	Sharded on the SOURCE hash so a source and its thumbnails sit in the same prefix, and
	suffixed with the dimensions so two sizes of one image are two objects.
	"""
	sha256 = (source_sha256 or "").lower()
	if not _SHA256_PATTERN.match(sha256):
		raise ValueError(f"source_sha256 must be 64 lowercase hex characters, got {source_sha256!r}")

	width, height = int(width), int(height)
	if width <= 0 or height <= 0:
		raise ValueError(f"thumbnail dimensions must be positive, got {width}x{height}")

	segments = []
	prefix = normalize_key_prefix(key_prefix)
	if prefix:
		segments.append(prefix)
	segments += [
		site_segment(site),
		DERIVED_SEGMENT,
		sha256[:2],
		sha256[2:4],
		f"{sha256}_{width}x{height}",
	]
	return "/".join(segments)


def parse_object_key(key: str) -> frappe._dict | None:
	"""Reverse :func:`build_object_key`. Returns None for keys this app did not write.

	Used by the orphan/reconcile sweeps to tell our objects from anything else that shares
	the bucket, without trusting object metadata.
	"""
	parts = (key or "").strip("/").split("/")
	if len(parts) < 5:
		return None
	sha256 = parts[-1]
	if not _SHA256_PATTERN.match(sha256):
		return None
	if parts[-2] != sha256[2:4] or parts[-3] != sha256[:2]:
		return None
	segment = parts[-4]
	if segment not in (PUBLIC_SEGMENT, PRIVATE_SEGMENT):
		return None
	return frappe._dict(
		{
			"content_sha256": sha256,
			"visibility": VISIBILITY_PRIVATE if segment == PRIVATE_SEGMENT else VISIBILITY_PUBLIC,
			"site": parts[-5],
			"key_prefix": "/".join(parts[:-5]),
		}
	)
