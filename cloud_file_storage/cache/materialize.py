"""Materialization cache — `sites/{site}/cloud_storage_cache/` (ADR-10).

The location matters: it is outside `public/` (never served by nginx `try_files` or the
dev `StaticDataMiddleware`) and outside `private/files` (never tarred by
`BackupGenerator.backup_files`, never swept by core's `delete_file`).

Entries are **per File row**, not per object: two File rows sharing one object must not be
able to corrupt each other through concurrent write-back, because mutating one of them
relinks only that row.

Every path this module (or `CloudFile.get_full_path`) hands out is registered for
write-back tracking with its mtime and size at hand-out (A4) — including canonical local
paths, which are not cache entries at all but are just as writable by a caller.
"""

import json
import os
import shutil

import frappe
from frappe.utils import cint, get_site_path, now_datetime
from frappe.utils.synchronization import filelock

from cloud_file_storage.storage import engine
from cloud_file_storage.storage.exceptions import CloudStorageIntegrityError
from cloud_file_storage.storage.hashing import digest_path, stat_signature

CACHE_DIRNAME = "cloud_storage_cache"
SIDECAR_SUFFIX = ".meta.json"
LOCK_TIMEOUT = 120

#: Key of the per-request/per-job tracking list on `frappe.local`.
TRACKING_ATTR = "cfs_materialized"
ROLLBACK_ATTR = "cfs_writeback_rolled_back"


def cache_root() -> str:
	path = get_site_path(CACHE_DIRNAME)
	os.makedirs(path, exist_ok=True)
	return path


class CachePathEscape(ValueError):
	"""A cache entry name that would resolve outside the cache root."""


def _confine(component: str) -> str:
	"""Reduce one name component to something that cannot traverse.

	Separators and `..` are stripped rather than escaped: every caller supplies an identifier
	(a File docname, a CSO name, a file extension), so a component containing a path separator
	is already wrong, and silently flattening it is safer than trusting it.
	"""
	component = (component or "").replace("\\", "/")
	component = component.rsplit("/", 1)[-1]
	return component.replace("..", "").strip()


def entry_path(file_name: str, cso_name: str, extension: str = "") -> str:
	"""A cache path that is confined to the cache root **by construction**.

	Previously this was `os.path.join(cache_root(), f"{file_name}__{cso_name}{ext}")`, which is
	only safe because today's callers pass frappe docnames. A caller passing `../../etc/passwd`
	escaped the cache root, so the confinement lived in the callers rather than here. Semgrep's
	`frappe-security-file-traversal` finding on the readers below was therefore **not** a false
	positive until this function enforced the property itself.

	Two independent guards: components are flattened to basenames with `..` removed, and the
	resolved path is then required to sit under the resolved cache root. The second catches
	anything the first misses, including symlinked roots.
	"""
	safe_extension = extension if extension and len(extension) <= 16 else ""
	name = f"{_confine(file_name)}__{_confine(cso_name)}{_confine(safe_extension)}"
	root = cache_root()
	path = os.path.join(root, name)
	if os.path.realpath(path) != os.path.realpath(root) and not os.path.realpath(path).startswith(
		os.path.realpath(root) + os.sep
	):
		raise CachePathEscape(f"cache entry would resolve outside {root}")
	return path


def sidecar_path(path: str) -> str:
	return path + SIDECAR_SUFFIX


def read_sidecar(path: str) -> dict | None:
	try:
		with open(sidecar_path(path)) as handle:  # nosemgrep: frappe-security-file-traversal
			return json.load(handle)
	except (OSError, ValueError):
		return None


def write_sidecar(path: str, **values):
	signature = stat_signature(path)
	payload = {
		"sha256": values.get("sha256"),
		"size": values.get("size"),
		"mtime": signature[0] if signature else None,
		"materialized_at": values.get("materialized_at") or str(now_datetime()),
		"last_access": str(now_datetime()),
		"dirty": bool(values.get("dirty")),
		"file": values.get("file"),
		"cloud_storage_object": values.get("cloud_storage_object"),
	}
	tmp = sidecar_path(path) + ".part"
	with open(tmp, "w") as handle:  # nosemgrep: frappe-security-file-traversal
		json.dump(payload, handle)
	os.replace(tmp, sidecar_path(path))
	return payload


def touch_sidecar(path: str):
	sidecar = read_sidecar(path)
	if sidecar is None:
		return
	sidecar["last_access"] = str(now_datetime())
	try:
		with open(sidecar_path(path), "w") as handle:  # nosemgrep: frappe-security-file-traversal
			json.dump(sidecar, handle)
	except OSError:
		pass


def _extension(file_doc) -> str:
	_, extension = os.path.splitext(file_doc.file_name or "")
	return extension


def has_unflushed_changes(path: str, sidecar: dict | None) -> bool:
	"""Whether the bytes on disk provably differ from what was materialized.

	"Provably" is the whole point. The **whole** `(mtime, size)` signature is compared, as
	`writeback._sync_back` does — size alone misses an in-place edit of identical length,
	which is the commonest shape of "same file, different contents". A sidecar that does not
	carry both halves cannot prove anything either way and is NOT reported as an edit: the
	local bytes then have no provenance, and pinning them would leave a file whose sidecar
	write once failed permanently stuck on stale content.
	"""
	if not sidecar:
		return False

	recorded_size = sidecar.get("size")
	recorded_mtime = sidecar.get("mtime")
	if recorded_size is None or recorded_mtime is None:
		return False

	signature = stat_signature(path)
	if signature is None:
		return False

	return cint(signature[1]) != cint(recorded_size) or signature[0] != recorded_mtime


def materialize(file_doc, cso) -> str:
	"""Return a local path holding this object's bytes, downloading it if needed.

	Downloads to `<path>.part`, fsyncs, verifies the SHA256 against the CSO, and only then
	renames into place: a truncated download can never be served as if it were the file.

	An entry carrying unflushed local changes is handed back untouched. Callers hand a path
	out, write to it, and ask for it again — erpnext's reposting loop does exactly that once
	per chunk against the same File (`stock_ledger.py:420-423`) — and re-downloading over
	those bytes would destroy an edit that write-back has not yet flushed.
	"""
	path = entry_path(file_doc.name, cso.name, _extension(file_doc))

	with filelock(f"cfs_mat_{file_doc.name}", timeout=LOCK_TIMEOUT):
		sidecar = read_sidecar(path)
		if os.path.exists(path) and sidecar:
			if sidecar.get("sha256") == cso.content_sha256 and not has_unflushed_changes(path, sidecar):
				touch_sidecar(path)
				track_path(file_doc, path, cso, is_cache_entry=True)
				return path

			if has_unflushed_changes(path, sidecar):
				if sidecar.get("sha256") != cso.content_sha256:
					# Both sides moved: the object was replaced while a local edit was still
					# pending. Local wins — bytes that exist only here are the ones that can
					# be lost — but the divergence is worth saying out loud.
					frappe.logger("cloud_file_storage").warning(
						{
							"operation": "materialize_conflict",
							"file": file_doc.name,
							"cloud_storage_object": cso.name,
							"detail": "kept unflushed local bytes over a replaced remote object",
						}
					)
				mark_dirty(path)
				track_path(file_doc, path, cso, is_cache_entry=True)
				return path

		make_room_for(cint(cso.file_size), holding_lock_for=file_doc.name)

		partial = path + ".part"
		try:
			engine.download_to_path(cso, partial)
			digest = digest_path(partial)
			if cso.content_sha256 and digest.sha256 != cso.content_sha256:
				raise CloudStorageIntegrityError(
					f"materialized bytes for {cso.s3_key} do not match the recorded SHA256"
				)
			os.replace(partial, path)
		finally:
			if os.path.exists(partial):
				try:
					os.remove(partial)
				except OSError:
					pass

		write_sidecar(
			path,
			sha256=cso.content_sha256,
			size=cint(cso.file_size) or os.path.getsize(path),
			file=file_doc.name,
			cloud_storage_object=cso.name,
		)

	track_path(file_doc, path, cso, is_cache_entry=True)
	return path


def track_path(file_doc, path: str, cso, *, is_cache_entry: bool):
	"""Register a handed-out path for write-back (A4).

	Called for EVERY path `get_full_path()` returns on a cloud-backed File — the canonical
	local path included. A caller that opens the canonical path and writes to it is exactly
	as invisible to us as one writing into the cache, so both are tracked.
	"""
	tracked = getattr(frappe.local, TRACKING_ATTR, None)
	if tracked is None:
		tracked = {}
		setattr(frappe.local, TRACKING_ATTR, tracked)

	# `after_request`/`after_job` run after the framework's own commit/rollback, so a flush
	# must know whether the enclosing transaction survived (A3). Registered on every call:
	# a commit resets the callback list, and a rollback after that commit still has to be
	# visible to the flush.
	frappe.db.after_rollback.add(mark_transaction_rolled_back)

	signature = stat_signature(path)
	previous = tracked.get(path)
	if previous is not None and previous.get("mtime") is not None:
		# The baseline is the state at the FIRST hand-out in this request, never the latest.
		# Callers ask for the same path more than once — erpnext's reposting loop hands it
		# out once per chunk — and re-baselining to the post-write state would tell
		# `_sync_back` that nothing changed, so the edit would never leave the disk. That
		# failure is silent and total: the bytes are correct locally and stale everywhere.
		baseline_mtime, baseline_size = previous["mtime"], previous.get("size")
	else:
		baseline_mtime = signature[0] if signature else None
		baseline_size = signature[1] if signature else None

	tracked[path] = {
		"file": file_doc.name,
		"cloud_storage_object": cso.name if cso else None,
		"content_sha256": cso.content_sha256 if cso else None,
		"is_cache_entry": is_cache_entry,
		"mtime": baseline_mtime,
		"size": baseline_size,
		"is_private": cint(file_doc.is_private),
		"file_name": file_doc.file_name,
		"attached_to_doctype": file_doc.attached_to_doctype,
	}


def mark_transaction_rolled_back():
	setattr(frappe.local, ROLLBACK_ATTR, True)


def transaction_rolled_back() -> bool:
	return bool(getattr(frappe.local, ROLLBACK_ATTR, False))


def tracked_paths() -> dict:
	return getattr(frappe.local, TRACKING_ATTR, None) or {}


def clear_tracking():
	setattr(frappe.local, TRACKING_ATTR, {})
	setattr(frappe.local, ROLLBACK_ATTR, False)


def mark_dirty(path: str, dirty: bool = True):
	sidecar = read_sidecar(path)
	if sidecar is None:
		return
	sidecar["dirty"] = dirty
	try:
		with open(sidecar_path(path), "w") as handle:  # nosemgrep: frappe-security-file-traversal
			json.dump(sidecar, handle)
	except OSError:
		pass


def dirty_entry_count() -> int:
	"""Cache entries holding unflushed local changes — the S3_ONLY transition gate reads this."""
	root = get_site_path(CACHE_DIRNAME)
	if not os.path.isdir(root):
		return 0

	count = 0
	for name in os.listdir(root):
		if not name.endswith(SIDECAR_SUFFIX):
			continue
		try:
			with open(os.path.join(root, name)) as handle:  # nosemgrep: frappe-security-file-traversal
				if json.load(handle).get("dirty"):
					count += 1
		except (OSError, ValueError):
			continue
	return count


def cache_size_bytes() -> int:
	"""Bytes currently held by cache entries (sidecars and part files excluded)."""
	root = get_site_path(CACHE_DIRNAME)
	if not os.path.isdir(root):
		return 0

	total = 0
	for name in os.listdir(root):
		if name.endswith((SIDECAR_SUFFIX, ".part", ".lock")):
			continue
		try:
			total += os.path.getsize(os.path.join(root, name))
		except OSError:
			continue
	return total


def make_room_for(incoming_bytes: int, *, holding_lock_for: str | None = None):
	"""Admission check (A24 bounded cache).

	The scheduled sweep runs daily; between two sweeps a busy site can materialize its way
	well past `cache_max_size_mb` and fill the disk out from under the ERP. So the budget is
	also enforced here, at the moment the cache is about to grow.

	An object larger than the entire budget is admitted with a warning rather than refused —
	refusing would make the file unreadable, which is a worse failure than a temporarily
	oversized cache.

	`holding_lock_for` is passed through to eviction: this runs inside
	`filelock(cfs_mat_<file>)` and frappe's filelock is not reentrant, so without it a
	materialize could never evict that File's own stale entries.
	"""
	from cloud_file_storage.cache import eviction

	budget = eviction.budget_bytes()

	# Make room first, whatever the outcome for this entry — a budget of 0 ("keep nothing
	# cached") still means everything else goes.
	if cache_size_bytes() + incoming_bytes > budget:
		eviction.trim_to(max(0, budget - incoming_bytes), holding_lock_for=holding_lock_for)

	if incoming_bytes > budget:
		frappe.logger("cloud_file_storage").warning(
			f"materializing {incoming_bytes} bytes exceeds the whole cache budget of {budget}; "
			"admitting it anyway so the file stays readable"
		)


def forget(file_name: str):
	"""Drop every cache entry belonging to a File row, tolerating absence."""
	root = get_site_path(CACHE_DIRNAME)
	if not os.path.isdir(root):
		return

	prefix = f"{file_name}__"
	for name in os.listdir(root):
		if not name.startswith(prefix):
			continue
		try:
			os.remove(os.path.join(root, name))
		except OSError:
			continue


def clear_cache_directory():
	"""Remove the whole cache tree. Operator/test utility — never called automatically."""
	root = get_site_path(CACHE_DIRNAME)
	if os.path.isdir(root):
		shutil.rmtree(root, ignore_errors=True)
