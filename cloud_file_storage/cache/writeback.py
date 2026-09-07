"""Write-back of paths handed out by `get_full_path()` (A3/A4).

Both entry points run **after** the framework has already committed or rolled back
(`app.py:152` runs `after_request` inside the `finally`, after `sync_database`;
`background_jobs.py:263` runs `after_job` in its own `finally`), and the request path
swallows exceptions from these hooks. So this module owns three things itself:

* a try/except around everything, with `frappe.log_error` — a write-back failure is
  recorded, never silent and never fatal to the request;
* an explicit `frappe.db.commit()` — nothing else is going to commit for us;
* a rollback check — if the enclosing transaction was rolled back, its File rows never
  existed, and relinking them would resurrect a phantom.

Known writers this covers: `bank_statement_import.py:202-212` (`open(path, "w")` +
`write_xlsx`) and `stock_ledger.py:420-422` (`open(path, "wb")`), both of which mutate
within one request or job.
"""

import mimetypes

import frappe
from frappe.utils import cint

from cloud_file_storage.cache import materialize
from cloud_file_storage.storage import engine, objects
from cloud_file_storage.storage.exceptions import CloudStorageError
from cloud_file_storage.storage.hashing import digest_path, stat_signature
from cloud_file_storage.storage.keys import visibility_for
from cloud_file_storage.storage.modes import effective_mode, get_settings


def flush_request(response=None, request=None):
	"""`after_request` hook."""
	_flush("request")


def flush_job(method=None, kwargs=None, result=None):
	"""`after_job` hook."""
	_flush("job")


def _flush(context: str):
	tracked = materialize.tracked_paths()
	if not tracked:
		return

	try:
		if materialize.transaction_rolled_back():
			# The File rows these paths belong to were rolled back. Relinking them would
			# write rows that, as far as the rest of the system is concerned, never existed.
			return

		changed = 0
		for path, meta in list(tracked.items()):
			changed += 1 if _sync_back(path, meta) else 0

		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)
		if changed:
			frappe.logger("cloud_file_storage").info(
				{"operation": "writeback", "context": context, "synced": changed}
			)
	except Exception:  # noqa: BLE001 - hook boundary: log, never propagate
		try:
			frappe.db.rollback()
			frappe.log_error(title=f"cloud_file_storage write-back ({context})")
			frappe.db.commit()
		except Exception:  # noqa: BLE001
			frappe.logger("cloud_file_storage").error("write-back failed and could not be logged")
	finally:
		materialize.clear_tracking()


def _sync_back(path: str, meta: dict) -> bool:
	"""Detect and propagate a local mutation of one handed-out path.

	Returns whether bytes actually changed. The cheap `(mtime, size)` comparison runs
	first; only a real difference pays for a re-hash.
	"""
	signature = stat_signature(path)
	if signature is None:
		# The path is gone. Usually harmless (the caller cleaned up, or nothing was ever
		# written), but it is also what an evicted-mid-flight cache entry looks like, and in
		# that case a local edit has just been lost. Say so — silence here is how an A4
		# write-back loss leaves no trace at all.
		frappe.logger("cloud_file_storage").warning(
			{
				"operation": "writeback_path_vanished",
				"path": path,
				"file": meta.get("file"),
				"is_cache_entry": meta.get("is_cache_entry"),
			}
		)
		return False

	if meta.get("mtime") is not None and signature == (meta.get("mtime"), meta.get("size")):
		return False

	digest = digest_path(path)
	if digest.sha256 == meta.get("content_sha256"):
		# Touched but identical (a rewrite of the same bytes). Refresh the sidecar so the
		# next request does not re-hash it.
		if meta.get("is_cache_entry"):
			materialize.write_sidecar(
				path,
				sha256=digest.sha256,
				size=digest.size,
				file=meta.get("file"),
				cloud_storage_object=meta.get("cloud_storage_object"),
			)
		return False

	file_name = meta.get("file")
	if not file_name or not frappe.db.exists("File", file_name):
		return False

	mode = effective_mode(meta.get("attached_to_doctype"))
	if not mode.uses_cloud:
		return False

	settings = get_settings()
	visibility = visibility_for(meta.get("is_private"))
	old_cso_name = meta.get("cloud_storage_object")

	cso = objects.ensure_cso(
		content_sha256=digest.sha256,
		content_hash_md5=digest.md5,
		file_size=digest.size,
		visibility=visibility,
		# `content_type` is a transient attribute core sets during save_file, not a column.
		mime_type=mimetypes.guess_type(meta.get("file_name") or "")[0],
		settings=settings,
	)

	if objects.needs_upload(cso):
		try:
			engine.put_path(cso, path, settings=settings)
		except CloudStorageError as exc:
			# The local bytes are the newest copy and they are safe on disk. Mark the entry
			# dirty so eviction skips it and the S3_ONLY gate refuses, and leave the File
			# pointing at the old object rather than at bytes that are not in the bucket.
			objects.record_failure(cso.name, str(exc))
			if meta.get("is_cache_entry"):
				materialize.mark_dirty(path)
			frappe.log_error(
				title="cloud_file_storage write-back upload failed",
				message=f"file={file_name} key={cso.s3_key} error={exc}",
			)
			return False
		objects.set_cso_status(cso.name, "uploaded")

	# New link first, old reference released only afterwards (A2 ordering): the object the
	# File still points at must never be scheduled for deletion before its replacement is
	# durable.
	frappe.db.set_value(
		"File",
		file_name,
		{
			"cloud_storage_object": cso.name,
			"content_hash": digest.md5,
			"file_size": digest.size,
		},
		update_modified=False,
	)
	objects.refresh_reference_count(cso.name)

	if old_cso_name and old_cso_name != cso.name:
		objects.release_reference(old_cso_name, settings=settings)

	if meta.get("is_cache_entry"):
		materialize.write_sidecar(
			path,
			sha256=digest.sha256,
			size=digest.size,
			file=file_name,
			cloud_storage_object=cso.name,
		)
		materialize.mark_dirty(path, False)

	meta["content_sha256"] = digest.sha256
	meta["cloud_storage_object"] = cso.name
	meta["mtime"] = signature[0]
	meta["size"] = cint(signature[1])
	return True
