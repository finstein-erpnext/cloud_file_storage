"""Content-Disposition policy — including the HTML edge, Policy B.

Two rules, in order:

1. **Policy B (PLAN.md §A, FD-7, hard invariant "HTML/htm/svg/xml forced-attachment").**
   `.html`, `.htm`, `.svg` and `.xml` are ALWAYS served as an attachment: never inline,
   never through a CDN that would serve them inline. These are the extensions that execute
   script in the origin's security context, and an attachment-served one cannot. Core does
   the same for local private files (`response.py:299-301`) — this generalises it to every
   path we serve, public included, and makes it release behaviour with a test, not a
   deployment note.
2. Everything else is inline only when its MIME type matches one of the configured
   `inline_mimetype_prefixes` (images and PDFs by default), and an attachment otherwise.

The chosen disposition is pinned into the presigned URL's signature
(`engine.presign_get`), so a leaked URL cannot be replayed with a different one.
"""

import mimetypes
import os

from cloud_file_storage.storage.modes import get_settings

INLINE = "inline"
ATTACHMENT = "attachment"

#: Policy B. Lower-case, dot-prefixed. `.svg` and `.xml` ride along with the HTML pair
#: because both can carry script and both are rendered, not just displayed.
FORCED_ATTACHMENT_EXTENSIONS = frozenset({".html", ".htm", ".svg", ".xml"})

DEFAULT_INLINE_PREFIXES = ("image/", "application/pdf")


def extension_of(name_or_url: str | None) -> str:
	if not name_or_url:
		return ""
	path = name_or_url.split("?", 1)[0].split("#", 1)[0]
	return os.path.splitext(path)[1].lower()


def is_forced_attachment(name_or_url: str | None) -> bool:
	"""Policy B membership test. The single source of truth for the HTML edge."""
	return extension_of(name_or_url) in FORCED_ATTACHMENT_EXTENSIONS


def inline_prefixes(settings=None) -> tuple[str, ...]:
	settings = settings or get_settings()
	raw = settings.inline_mimetype_prefixes
	if raw is None:
		return DEFAULT_INLINE_PREFIXES
	prefixes = tuple(line.strip() for line in str(raw).splitlines() if line.strip())
	return prefixes


def disposition_for(name_or_url: str | None, mime_type: str | None = None, settings=None) -> str:
	"""`inline` or `attachment` for one file, Policy B first."""
	if is_forced_attachment(name_or_url):
		return ATTACHMENT

	mime_type = mime_type or mimetypes.guess_type(name_or_url or "")[0]
	if not mime_type:
		return ATTACHMENT

	mime_type = mime_type.lower()
	return INLINE if any(mime_type.startswith(prefix) for prefix in inline_prefixes(settings)) else ATTACHMENT


def may_use_cdn(name_or_url: str | None) -> bool:
	"""Whether a CDN URL may be handed out for this file.

	Forced-attachment types never go to the CDN: a CDN URL carries whatever disposition the
	stored object's metadata implies, so an `.html` object served from the edge would render
	inline — the exact outcome Policy B forbids. Those types are presigned instead, with the
	disposition pinned into the signature.
	"""
	return not is_forced_attachment(name_or_url)
