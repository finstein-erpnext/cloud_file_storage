"""One-pass MD5 + SHA256 hashing.

MD5 is `tabFile.content_hash` (core computes it in `file/utils.py:get_content_hash`, and
docs/INVARIANTS.md invariant 3 keeps it populated); SHA256 is the physical object identity (ADR-3).
Both are computed in a single pass so a 100MB attachment is read once, not twice.
"""

import hashlib
import os

# 1 MiB: large enough that syscall overhead disappears, small enough to stay off the heap
# for concurrent transfers.
STREAM_CHUNK_SIZE = 1024 * 1024


class ContentDigest:
	"""The identity of a blob of bytes."""

	__slots__ = ("md5", "sha256", "size")

	def __init__(self, md5: str, sha256: str, size: int):
		self.md5 = md5
		self.sha256 = sha256
		self.size = size

	def __repr__(self) -> str:  # pragma: no cover - debugging aid
		return f"ContentDigest(sha256={self.sha256[:12]}…, md5={self.md5[:8]}…, size={self.size})"

	def __eq__(self, other) -> bool:
		if not isinstance(other, ContentDigest):
			return NotImplemented
		return (self.md5, self.sha256, self.size) == (other.md5, other.sha256, other.size)


def digest_bytes(content: bytes | str) -> ContentDigest:
	"""Hash in-memory content. ``str`` is encoded UTF-8 exactly like core does."""
	if isinstance(content, str):
		content = content.encode()

	md5 = hashlib.md5(content, usedforsecurity=False)
	sha256 = hashlib.sha256(content)
	return ContentDigest(md5.hexdigest(), sha256.hexdigest(), len(content))


def digest_path(path: str) -> ContentDigest:
	"""Hash a file on disk in one streamed pass."""
	md5 = hashlib.md5(usedforsecurity=False)
	sha256 = hashlib.sha256()
	size = 0
	with open(path, "rb") as handle:  # nosemgrep: frappe-security-file-traversal
		while True:
			chunk = handle.read(STREAM_CHUNK_SIZE)
			if not chunk:
				break
			size += len(chunk)
			md5.update(chunk)
			sha256.update(chunk)
	return ContentDigest(md5.hexdigest(), sha256.hexdigest(), size)


def stat_signature(path: str) -> tuple[float, int] | None:
	"""``(mtime, size)`` of ``path``, or None when it is not there.

	The cheap drift check used by write-back tracking (A4) before paying for a re-hash.
	"""
	try:
		stat = os.stat(path)
	except OSError:
		return None
	return (stat.st_mtime, stat.st_size)
