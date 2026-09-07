"""The three File-lifecycle hooks v15 actually exposes.

`write_file`, `before_write_file` and `delete_file_data_content` are the correct seams
(`file.py:711-714`, `:743-747`). The fork's `after_insert` seam wrote the file locally and
then uploaded and deleted it, which cannot express an operation mode and cannot be made
safe — this module replaces it.

Things this module deliberately does NOT do, each an invariant:

* no `frappe.db.commit()` inside the write path — a mid-transaction commit turns a failed
  insert into a half-written File row;
* no `os.remove` of anything, ever, in the write path (F6 lint);
* no ACL, no non-canonical `file_url`, no nulling of `content_hash`;
* no inline S3 delete in the delete hook — dereferencing schedules, GC deletes.
"""

import os
import re

import frappe
from frappe import _
from frappe.utils import cint

from cloud_file_storage.cache import materialize
from cloud_file_storage.overrides.file import stash_legacy_cso
from cloud_file_storage.storage import breaker, engine, objects
from cloud_file_storage.storage.exceptions import (
	CloudStorageConfigurationError,
	CloudStorageError,
	CloudStorageUnavailableError,
)
from cloud_file_storage.storage.hashing import digest_bytes
from cloud_file_storage.storage.keys import visibility_for
from cloud_file_storage.storage.modes import (
	OperationMode,
	effective_mode,
	fail_insert_on_s3_error,
	get_mode,
	get_settings,
	is_ignored_doctype,
)

#: core `save_file_on_filesystem` (file.py:717-726) — the same sanitisation, so our URLs
#: are byte-identical to the ones core would have written.
_UNSAFE_URL_CHARS = re.compile(r"[/\\%?#]")


def write_file(*args, **kwargs):
	"""Dual-convention dispatcher (contract #12).

	`File.save_file` calls ``write_file(file_doc)``; `frappe.utils.file_manager.save_file`
	calls ``write_file(fname, content, content_type=…, is_private=…)`` and filters the
	returned dict through ``write_file_keys``. Both are live: e-Waybill
	(`e_waybill.py:741`) and supplier-invoice import (`import_supplier_invoice.py:104`)
	use the second.
	"""
	if len(args) == 1 and not kwargs and hasattr(args[0], "doctype"):
		return _write_file_doc(args[0])
	return _write_file_legacy(*args, **kwargs)


def before_write_file(file_size=None, **kwargs):
	"""Fail fast before core does any work, when the mode cannot possibly be satisfied."""
	settings = get_settings()
	mode = get_mode(settings)
	if mode is not OperationMode.S3_ONLY:
		return

	if not settings.bucket:
		frappe.throw(
			_("Operation mode is S3_ONLY but no bucket is configured in {0}.").format(
				_("Cloud Storage Settings")
			),
			title=_("Cloud Storage Not Configured"),
		)

	if breaker.is_open():
		frappe.throw(
			_(
				"Object storage is unreachable and the operation mode is S3_ONLY, so this "
				"file cannot be stored. Retry once connectivity is restored."
			),
			title=_("Object Storage Unavailable"),
			exc=CloudStorageUnavailableError,
		)


# --- convention (a): File document -------------------------------------------------------


def _write_file_doc(file) -> dict:
	settings = get_settings()
	mode = effective_mode(file.attached_to_doctype, settings)

	if not mode.uses_cloud:
		return _save_on_filesystem(file)

	content = file._content
	if isinstance(content, str):
		content = content.encode()

	digest = digest_bytes(content)
	file.file_url = _canonical_file_url(file, digest)

	cso = objects.ensure_cso(
		content_sha256=digest.sha256,
		content_hash_md5=digest.md5,
		file_size=digest.size,
		visibility=visibility_for(file.is_private),
		mime_type=file.content_type,
		settings=settings,
	)

	stored_locally = _store(file, cso, content, mode, settings)

	# content_hash is KEPT (invariant 3): core's dedup, `_delete_file_on_disk`'s refcount
	# gate and the A1 sibling lookup all read it.
	file.content_hash = digest.md5
	file.file_size = digest.size
	previous_cso = _persist_link(file, cso)

	if previous_cso and previous_cso != cso.name:
		# Only now that the new link is durable (A2): the object the row pointed at a
		# moment ago must never be scheduled for deletion before its replacement lands.
		objects.release_reference(previous_cso, settings=settings)

	objects.refresh_reference_count(cso.name)
	file._forget_resolved_cso()

	# A8: a public URL that 404'd before this upload is cached as a 404 by
	# `PathResolver.resolve` and would keep 404-ing after it. Clear it here, where the URL
	# is known to have become servable.
	invalidate_public_404(file.file_url)

	return {
		"file_name": os.path.basename(file.file_url),
		"file_url": file.file_url,
		"stored_locally": stored_locally,
	}


def _save_on_filesystem(file) -> dict:
	"""LOCAL_ONLY / ignored parent: core's own path, verbatim."""
	file.flags.cfs_local_write = True
	try:
		return file.save_file_on_filesystem()
	finally:
		file.flags.pop("cfs_local_write", None)


def _canonical_file_url(file, digest) -> str:
	"""`/files/<name>` or `/private/files/<name>` — never anything else (invariant 2).

	Core's collision check is `os.path.exists` (`utils.py:198-208`), which degenerates on a
	site with no local files, so the same question is asked of the database: a different
	File row already owning this URL with *different* bytes gets the hash-suffix rename
	core would have applied. Identical bytes keep the URL — that is dedup/overwrite.
	"""
	safe_file_name = _UNSAFE_URL_CHARS.sub("_", file.file_name)
	prefix = "/private/files/" if cint(file.is_private) else "/files/"
	candidate = f"{prefix}{safe_file_name}"

	if candidate == (file.file_url or ""):
		# An in-place overwrite: core skips the hash-suffix rename for `overwrite=True`
		# (file.py:705-710) precisely so the URL stays stable, and business fields such as
		# GST Return Log's store that URL (contract #11/#18). A URL-sharing sibling must
		# not be allowed to push this row onto a new URL.
		return candidate

	conflicting = frappe.db.get_value(
		"File",
		{
			"file_url": candidate,
			"name": ("!=", file.name or ""),
			"content_hash": ("!=", digest.md5),
		},
		"name",
	)
	if not conflicting:
		return candidate

	partial, extension = os.path.splitext(safe_file_name)
	renamed = f"{partial}{digest.sha256[:6]}{extension}"
	return f"{prefix}{renamed}"


def _store(file, cso, content: bytes, mode, settings) -> bool:
	"""Put the bytes where the mode says they belong. Returns whether a local copy exists.

	Failure is never silent: either the caller gets a typed exception, or the object is
	left in a queryable `pending_upload`/`failed` state with `last_error` set and the bytes
	are durable on local disk.
	"""
	if mode is OperationMode.DUAL_WRITE:
		local_path = _write_local(file, content)
		if not objects.needs_upload(cso):
			return True
		try:
			engine.put_path(cso, local_path, settings=settings)
		except CloudStorageError as exc:
			objects.record_failure(cso.name, str(exc))
			if fail_insert_on_s3_error(settings):
				raise
			frappe.log_error(
				title="cloud_file_storage upload deferred",
				message=f"file={file.name} key={cso.s3_key} mode={mode.value} error={exc}",
			)
			return True
		objects.set_cso_status(cso.name, "uploaded")
		return True

	if mode is OperationMode.S3_PRIMARY_LOCAL_FALLBACK:
		if not objects.needs_upload(cso):
			return False
		try:
			engine.put_bytes(cso, content, settings=settings)
		except CloudStorageError as exc:
			objects.record_failure(cso.name, str(exc))
			_write_local(file, content)
			frappe.log_error(
				title="cloud_file_storage upload deferred to local fallback",
				message=f"file={file.name} key={cso.s3_key} mode={mode.value} error={exc}",
			)
			return True
		objects.set_cso_status(cso.name, "uploaded")
		return False

	# S3_ONLY: an explicit, typed failure. No silent local fallback.
	if not objects.needs_upload(cso):
		return False
	try:
		engine.put_bytes(cso, content, settings=settings)
	except CloudStorageError as exc:
		objects.record_failure(cso.name, str(exc))
		raise
	objects.set_cso_status(cso.name, "uploaded")
	return False


def _write_local(file, content: bytes) -> str:
	"""Write the canonical local copy, with core's own checks and rollback bookkeeping."""
	file.flags.cfs_local_write = True
	try:
		local_path = file.get_full_path()
		os.makedirs(os.path.dirname(local_path), exist_ok=True)
		file._content = content

		# `File.check_content` (rejects PDFs carrying embedded JavaScript) first exists in
		# frappe **v15.80.0**, together with its helper `frappe.utils.pdf.pdf_contains_js`.
		# The declared floor is **v15.16.0** (docs/supported-versions.md), and the F1 reference
		# matrix caught this as 22 errors on that ref against a green v15.93.0.
		#
		# **Calling it only when present is exact parity with core, not a weakened check.**
		# Below v15.80.0 frappe performs no such scan anywhere — the helper does not exist to
		# call — so this app is never more permissive than the frappe it is running on. Writing
		# our own scanner instead would put a security behaviour on old sites that the platform
		# itself does not have, which is new architecture rather than compatibility.
		check_content = getattr(file, "check_content", None)
		if callable(check_content):
			check_content()
		with open(local_path, "wb+") as handle:  # nosemgrep: frappe-security-file-traversal
			handle.write(content)
			os.fsync(handle.fileno())
		frappe.db.after_rollback.add(file.on_rollback)
		return local_path
	finally:
		file.flags.pop("cfs_local_write", None)


def _persist_link(file, cso) -> str | None:
	"""A2 — an overwrite that never calls `.save()` must still repoint the DB row.

	`gst_return_log.py:103` calls `save_file(content=…, overwrite=True)` and then only
	`db_set(file_field, file.file_url)`; the File document is never saved. Without this the
	row keeps pointing at the previous object while the bytes it serves have changed.

	Returns the object the row referenced before, so the caller can release it afterwards.
	"""
	file.cloud_storage_object = cso.name

	if file.is_new():
		return None

	previous = frappe.db.get_value("File", file.name, "cloud_storage_object")
	file.db_set(
		{
			"cloud_storage_object": cso.name,
			"content_hash": file.content_hash,
			"file_size": file.file_size,
		},
		update_modified=False,
	)

	_relink_url_siblings(file, cso, previous)
	return previous


def _relink_url_siblings(file, cso, previous_cso: str | None):
	"""Rows sharing the mutated `file_url` follow it to the new object (A2 extension).

	`stock_ledger.py:308-318` and the GST log look Files up by `file_url`; a sibling left
	pointing at the old object would serve the pre-overwrite bytes from the same URL.
	"""
	if not file.file_url:
		return

	siblings = frappe.get_all(
		"File",
		filters={"file_url": file.file_url, "name": ("!=", file.name)},
		pluck="name",
	)
	if not siblings:
		return

	objects.adopt_references(cso.name, siblings)
	if previous_cso and previous_cso != cso.name:
		objects.refresh_reference_count(previous_cso)


# --- convention (b): file_manager.save_file ----------------------------------------------


def _write_file_legacy(fname, content, content_type=None, is_private=0) -> dict:
	"""`frappe.utils.file_manager.save_file`'s hook shape.

	The File row does not exist yet — core inserts it afterwards from the dict we return
	(`file_manager.py:169-189`). The object is stashed on `frappe.local` keyed by MD5 so
	`file_after_insert` and the A1 resolution chain can find it in the window before the
	link exists.
	"""
	from frappe.utils.file_manager import save_file_on_filesystem

	settings = get_settings()
	mode = get_mode(settings)

	if isinstance(content, str):
		content = content.encode()

	if not mode.uses_cloud:
		return save_file_on_filesystem(fname, content, content_type=content_type, is_private=is_private)

	digest = digest_bytes(content)
	cso = objects.ensure_cso(
		content_sha256=digest.sha256,
		content_hash_md5=digest.md5,
		file_size=digest.size,
		visibility=visibility_for(is_private),
		mime_type=content_type,
		settings=settings,
	)

	if objects.needs_upload(cso):
		try:
			engine.put_bytes(cso, content, settings=settings)
		except CloudStorageError as exc:
			objects.record_failure(cso.name, str(exc))
			if mode is OperationMode.S3_ONLY:
				raise
			frappe.log_error(
				title="cloud_file_storage legacy write deferred to local",
				message=f"fname={fname} key={cso.s3_key} mode={mode.value} error={exc}",
			)
			return save_file_on_filesystem(fname, content, content_type=content_type, is_private=is_private)
		objects.set_cso_status(cso.name, "uploaded")

	if mode is OperationMode.DUAL_WRITE:
		from frappe.utils.file_manager import write_file as write_local_file

		write_local_file(content, fname, is_private)

	stash_legacy_cso(digest.md5, is_private, cso.name)

	safe_file_name = _UNSAFE_URL_CHARS.sub("_", fname)
	prefix = "/private/files/" if cint(is_private) else "/files/"
	file_url = f"{prefix}{safe_file_name}"
	invalidate_public_404(file_url)
	return {"file_name": safe_file_name, "file_url": file_url}


def invalidate_public_404(*file_urls: str | None) -> int:
	"""Drop any `website_404` entry for URLs that have just become servable (A8).

	Wrapped rather than called directly so a Redis failure can never fail a file write, and
	so P5's VERIFY link transaction has one obvious symbol to call. Variadic because
	`invalidate_404` scans the whole cache once per call, so a caller with several URLs (the
	privacy flip has four) must be able to pay for one scan rather than four.
	"""
	try:
		from cloud_file_storage.serving.public import invalidate_404

		return invalidate_404(*file_urls)
	except Exception as exc:  # noqa: BLE001 - a stale negative cache is not worth a failed upload
		frappe.logger("cloud_file_storage").warning(
			f"could not invalidate website_404 for {', '.join(str(url) for url in file_urls)}: {exc}"
		)
		return 0


# --- delete ------------------------------------------------------------------------------


def delete_file_data_content(file_doc, only_thumbnail=False):
	"""Reached through core's refcount gate (`file.py:511-526`, contract #13).

	Dereferencing an object schedules it — `pending_delete` plus a grace window. The
	physical `DeleteObject` happens only in `gc.run_deferred_object_gc`, after a locking
	recount (A6). There is no code path from a user pressing delete to bytes leaving the
	bucket in the same request.
	"""
	from frappe.utils.file_manager import delete_file

	if only_thumbnail:
		delete_file(file_doc.thumbnail_url)
		return

	settings = get_settings()

	local_path = _local_path_for(file_doc)
	if local_path and os.path.exists(local_path):
		try:
			os.remove(local_path)
		except OSError:
			frappe.logger("cloud_file_storage").warning(f"could not remove local copy {local_path}")
	delete_file(file_doc.thumbnail_url)

	cso_name = _resolve_cso_name(file_doc)
	if cso_name:
		_adopt_url_sharers_before_release(file_doc, cso_name)
		scheduled = objects.release_reference(cso_name, excluding_file=file_doc.name, settings=settings)
		if scheduled:
			# The source object lost its last reference, so its thumbnails have nothing left
			# to be derived data OF. They follow it into the same deferred-GC path — never an
			# inline delete (invariant 4).
			from cloud_file_storage.serving import thumbnails

			thumbnails.release_derived_objects(cso_name, settings=settings)
			thumbnail_cso = file_doc.get(thumbnails.THUMBNAIL_LINK_FIELD)
			if thumbnail_cso and thumbnail_cso != cso_name:
				objects.release_reference(thumbnail_cso, excluding_file=file_doc.name, settings=settings)

	materialize.forget(file_doc.name)


def _adopt_url_sharers_before_release(file_doc, cso_name: str):
	"""Hand the link to link-less rows sharing this URL, before this row goes away.

	A link-less row resolves through the A1 `file_url` arm — through *this* row's link. Once
	this row is deleted that arm has nothing to resolve through, and if the survivor also has
	no `content_hash` (which `patches/v0_2_0/backfill_s3_object_key` produces on fork-era
	rows) it can no longer reach the object at all. GC will not delete the bytes — the
	recount sees the survivor — but the survivor would 404 on bytes that are still there.

	Canonical URLs identify a file uniquely, so a row sharing this one's `file_url` is the
	same file, and adopting it is simply making an inferred reference explicit.
	"""
	if not file_doc.file_url:
		return

	sharers = frappe.get_all(
		"File",
		filters={
			"file_url": file_doc.file_url,
			"name": ("!=", file_doc.name),
			"cloud_storage_object": ("in", (None, "")),
			"is_folder": 0,
		},
		pluck="name",
	)
	if sharers:
		objects.adopt_references(cso_name, sharers)


def _local_path_for(file_doc):
	resolver = getattr(file_doc, "_canonical_local_path", None)
	if resolver:
		return resolver()
	return None


def _resolve_cso_name(file_doc) -> str | None:
	"""The object this row references, via the A1 chain when the row carries no link."""
	if file_doc.get("cloud_storage_object"):
		return file_doc.cloud_storage_object

	resolver = getattr(file_doc, "_resolve_cso", None)
	if not resolver:
		return None
	cso = resolver()
	return cso.name if cso else None


# --- doc events ---------------------------------------------------------------------------


def file_after_insert(doc, method=None):
	"""Adopt an object for rows that never went through `write_file` (A1).

	Core's insert-time dedup (`file.py:687-702`) reuses a duplicate's `file_url` and skips
	the write hook entirely; amend/copy and `attach_files_to_document` create rows the same
	way. Two indexed queries, and a silent no-op in LOCAL_ONLY.
	"""
	if doc.is_folder or doc.cloud_storage_object or doc.is_remote_file:
		return

	settings = get_settings()
	if is_ignored_doctype(doc.attached_to_doctype, settings):
		return
	if not get_mode(settings).uses_cloud:
		return

	resolver = getattr(doc, "_resolve_cso", None)
	if not resolver:
		return

	cso = resolver()
	if cso is None:
		return

	objects.adopt_references(cso.name, [doc.name])
	doc.cloud_storage_object = cso.name


def validate_single_file_owner():
	"""Refuse to share a site with another File storage app, loudly and at install time.

	`write_file` is single-owner on every version -- `get_hook_method` takes index [0], so a
	second claimant silently disables one of them.

	`override_doctype_class` is last-wins (base_document.py). On v15 this app registers there,
	so a foreign claimant means one of the two is silently disabled. On v16 this app registers
	through `extend_doctype_class` instead, which *composes* rather than replaces -- but a
	foreign app still using the override would replace the composed class outright, so the
	check stays exactly as useful there.

	Deliberately not flagged: another app *extending* File on v16. Composition is the whole
	point of that hook, and a coexisting extension that also claimed the write path would be
	caught by the `write_file` check above.
	"""
	conflicts = []

	write_file_hooks = frappe.get_hooks("write_file") or []
	foreign = [hook for hook in write_file_hooks if not hook.startswith("cloud_file_storage.")]
	if foreign:
		conflicts.append(f"write_file is also claimed by: {', '.join(foreign)}")

	override = (frappe.get_hooks("override_doctype_class") or {}).get("File") or []
	foreign = [hook for hook in override if not hook.startswith("cloud_file_storage.")]
	if foreign:
		conflicts.append(f"override_doctype_class['File'] is also claimed by: {', '.join(foreign)}")

	if conflicts:
		message = "cloud_file_storage cannot share a site with another File storage app. " + " ".join(
			conflicts
		)
		frappe.logger("cloud_file_storage").error(message)
		raise CloudStorageConfigurationError(message)
