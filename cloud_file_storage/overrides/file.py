"""`CloudFile(File)` — the read side of cloud storage.

Registered through `extend_doctype_class` on frappe v16 (composed into the MRO as
`ExtendedFile -> CloudFile -> File`) and through `override_doctype_class` on v15, which
has no extension hook. The class is identical either way. Every signature is identical to v15.93 core
except `get_content`, which gains an optional keyword (the v16/develop superset).

The load-bearing piece is :meth:`CloudFile._resolve_cso` (A1). Cloud resolution must NOT
depend on `self.cloud_storage_object` alone, because core creates File rows from a
`file_url` and nothing else, before any link exists:

* amend/copy — `document.py:446-464` re-reads the bytes of the source File;
* `attach_files_to_document` — `file/utils.py:363-374`;
* `file_manager.save_file` — `file_manager.py:161-190`.

All three then run `before_insert`, which calls `get_content()` on a row whose link is
still empty. Without the fallback chain, an S3_ONLY site cannot amend a document.
"""

import os

import frappe
from frappe import _
from frappe.core.doctype.file.file import URL_PREFIXES, File
from frappe.core.doctype.file.utils import update_existing_file_docs
from frappe.utils import cint, get_files_path, get_url
from frappe.utils.file_manager import is_safe_path

from cloud_file_storage.cache import materialize
from cloud_file_storage.storage import engine, objects
from cloud_file_storage.storage.exceptions import CloudObjectNotFound
from cloud_file_storage.storage.keys import build_object_key, visibility_for
from cloud_file_storage.storage.modes import effective_mode, get_settings

#: `frappe.local` stash written by the legacy `write_file` convention, keyed by MD5. The
#: File row does not exist yet at that point, so there is nothing to link to.
LEGACY_STASH_ATTR = "cfs_legacy_write_stash"


def stash_legacy_cso(content_hash_md5: str, is_private, cso_name: str):
	stash = getattr(frappe.local, LEGACY_STASH_ATTR, None)
	if stash is None:
		stash = {}
		setattr(frappe.local, LEGACY_STASH_ATTR, stash)
	stash[(content_hash_md5, cint(is_private))] = cso_name


def peek_legacy_stash(content_hash_md5: str, is_private) -> str | None:
	stash = getattr(frappe.local, LEGACY_STASH_ATTR, None) or {}
	return stash.get((content_hash_md5, cint(is_private)))


class CloudFile(File):
	# --- resolution -------------------------------------------------------------------

	def _canonical_local_path(self) -> str | None:
		"""Where core would put this file on disk. Pure path math — never touches network.

		Returns None for remote URLs and for anything that fails core's containment rule,
		rather than throwing: callers here want "is there a local candidate", not a
		validation verdict.
		"""
		file_path = self.file_url or self.file_name
		if not file_path:
			return None

		site_url = get_url()
		if "/files/" in file_path and file_path.startswith(site_url):
			file_path = file_path.split(site_url, 1)[1]

		if "/" not in file_path:
			file_path = f"/private/files/{file_path}" if self.is_private else f"/files/{file_path}"

		if file_path.startswith("/private/files/"):
			file_path = get_files_path(*file_path.split("/private/files/", 1)[1].split("/"), is_private=1)
		elif file_path.startswith("/files/"):
			file_path = get_files_path(*file_path.split("/files/", 1)[1].split("/"))
		else:
			return None

		if not is_safe_path(file_path):
			return None
		return file_path

	def _resolve_cso(self):
		"""A1 fallback chain: own link → file_url sibling → content_hash sibling → stash."""
		cached = getattr(self, "_cfs_resolved_cso", None)
		if cached is not None:
			return cached or None

		cso = self._resolve_cso_uncached()
		# `False` distinguishes "resolved to nothing" from "not resolved yet".
		self._cfs_resolved_cso = cso or False
		return cso

	def _resolve_cso_uncached(self):
		if self.is_folder:
			return None

		if self.cloud_storage_object:
			cso = objects.get_cso(self.cloud_storage_object)
			if cso:
				return cso

		if self.file_url:
			sibling = frappe.db.get_value(
				"File",
				{
					"file_url": self.file_url,
					"cloud_storage_object": ("is", "set"),
					"name": ("!=", self.name or ""),
				},
				"cloud_storage_object",
			)
			if sibling:
				cso = objects.get_cso(sibling)
				if cso:
					return cso

		if self.content_hash:
			sibling = frappe.db.get_value(
				"File",
				{
					"content_hash": self.content_hash,
					"is_private": cint(self.is_private),
					"cloud_storage_object": ("is", "set"),
					"name": ("!=", self.name or ""),
				},
				"cloud_storage_object",
			)
			if sibling:
				cso = objects.get_cso(sibling)
				if cso:
					return cso

			stashed = peek_legacy_stash(self.content_hash, self.is_private)
			if stashed:
				cso = objects.get_cso(stashed)
				if cso:
					return cso

		return None

	def _forget_resolved_cso(self):
		self._cfs_resolved_cso = None

	def _is_cloud_backed(self) -> bool:
		return self._resolve_cso() is not None

	def _mode(self):
		return effective_mode(self.attached_to_doctype)

	# --- reads ------------------------------------------------------------------------

	def _read_bytes(self) -> bytes:
		"""Bytes for this File, wherever they live.

		Order: canonical local copy (DUAL_WRITE fast path, zero network) → materialization
		cache → object store. Absence raises :class:`CloudObjectNotFound`, which IS a
		`FileNotFoundError`; every transport failure raises a typed error that is NOT
		(contract #5).
		"""
		local_path = self._canonical_local_path()
		if local_path and os.path.exists(local_path):
			with open(local_path, "rb") as handle:  # nosemgrep: frappe-security-file-traversal
				return handle.read()

		cso = self._resolve_cso()
		if cso is not None and objects.is_servable(cso):
			return engine.get_bytes(cso)

		if cso is not None:
			raise CloudObjectNotFound(f"Cloud Storage Object {cso.name} for File {self.name} is {cso.status}")

		raise CloudObjectNotFound(f"File {self.name or self.file_url} has no readable content")

	def get_content(self, encodings=None) -> bytes | str:
		"""v15 superset.

		* no arguments → **exact** v15 behaviour (try UTF-8, fall back to bytes);
		* ``encodings=[]`` → raw bytes, no decode attempt — the India Compliance
		  compatibility vanilla v15 lacks (`gst_return_log.py:188`, contract #1/#2);
		* an explicit list → each encoding in order, bytes if none apply (v16 semantics).
		"""
		if self.is_folder:
			frappe.throw(_("Cannot get file contents of a Folder"))

		if self.get("content"):
			# Core's in-memory path, verbatim (file.py:570-576).
			self._content = self.content
			if self.decode:
				from frappe.core.doctype.file.utils import decode_file_content

				self._content = decode_file_content(self._content)
				self.decode = False
			return self._content

		if self.file_url:
			self.validate_file_url()

		raw = self._read_bytes()

		if encodings is None:
			try:
				self._content = raw.decode()
			except UnicodeDecodeError:
				self._content = raw
			return self._content

		for encoding in encodings:
			try:
				self._content = raw.decode(encoding)
				return self._content
			except (UnicodeDecodeError, LookupError):
				continue

		self._content = raw
		return self._content

	def get_full_path(self):
		"""A path `open()`, `zipfile`, `PIL` and `openpyxl` can consume (contract #7).

		Every path returned for a cloud-backed File is registered for write-back with its
		mtime and size (A4) — the canonical local path included, because a caller that
		writes to it is exactly as invisible as one writing into the cache.
		"""
		if self.flags.get("cfs_local_write"):
			# Our own local write is in progress; materializing here would hand the writer
			# a cache path and strand the canonical copy.
			local_path = self._canonical_local_path()
			if local_path:
				return local_path
			return super().get_full_path()

		local_path = self._canonical_local_path()
		cso = self._resolve_cso()

		if local_path and os.path.exists(local_path):
			if cso is not None:
				materialize.track_path(self, local_path, cso, is_cache_entry=False)
			return local_path

		if cso is not None and self._mode().uses_cloud and objects.is_servable(cso):
			return materialize.materialize(self, cso)

		return super().get_full_path()

	def exists_on_disk(self):
		"""Remote existence counts (contract #14).

		Core's `save_file` uses this to decide whether to reuse a duplicate's `file_url`;
		returning False for cloud-only twins breaks dedup and creates orphan objects. No
		network HEAD in this hot path — the CSO status is trusted, and GC/verify keep it
		honest.
		"""
		local_path = self._canonical_local_path()
		if local_path and os.path.exists(local_path):
			return True

		cso = self._resolve_cso()
		return cso is not None and cso.status in ("uploaded", "verified", "legacy_unverified")

	def validate_file_on_disk(self):
		"""Never throw for a cloud-backed row (contract #6). No materialization either."""
		if self._is_cloud_backed():
			return True
		return super().validate_file_on_disk()

	def validate_file_path(self):
		"""Core's containment rule, without requiring the file to exist locally."""
		if self.is_remote_file:
			return

		if not self._is_cloud_backed():
			return super().validate_file_path()

		local_path = self._canonical_local_path()
		if local_path is None:
			frappe.throw(
				_("The File URL you've entered is incorrect"),
				title=_("Invalid File URL"),
			)

		base_path = os.path.realpath(get_files_path(is_private=self.is_private))
		if not os.path.realpath(local_path).startswith(base_path):
			frappe.throw(
				_("The File URL you've entered is incorrect"),
				title=_("Invalid File URL"),
			)

	def generate_content_hash(self):
		"""Reuse the object's MD5 instead of reading a file that may not be local (#15)."""
		if self.content_hash or not self.file_url or self.is_remote_file:
			return

		cso = self._resolve_cso()
		if cso is not None and cso.content_hash_md5:
			self.content_hash = cso.content_hash_md5
			return

		return super().generate_content_hash()

	# --- thumbnails as derived objects (PLAN.md §A) -------------------------------------

	def make_thumbnail(
		self,
		set_as_thumbnail: bool = True,
		width: int = 300,
		height: int = 300,
		suffix: str = "small",
		crop: bool = False,
	) -> str | None:
		"""Render a thumbnail from wherever the source lives, and store it as a derived object.

		Core reads the source straight off local disk (`get_local_image`, `file/utils.py:86-94`),
		so on an S3-primary site `make_thumbnail` silently returns None and every Attach Image
		preview dies. Here the source is materialized first, the thumbnail is uploaded as a
		derived object of the source CSO, and a local copy is kept only in the modes that
		promise one — after S3_ONLY no permanent thumbnail needs local persistence, because
		`PublicFileRenderer` serves the derived object on the `/files/*_small.*` miss.
		"""
		if not self.file_url:
			return None

		cso = self._resolve_cso()
		if cso is None or not self._mode().uses_cloud or self.is_remote_file:
			return super().make_thumbnail(
				set_as_thumbnail=set_as_thumbnail, width=width, height=height, suffix=suffix, crop=crop
			)

		from PIL import Image

		try:
			content, image_format = self._render_thumbnail(width, height, crop)
		except Exception as exc:  # noqa: BLE001 - core returns None for any unreadable image
			frappe.log_error(
				title="cloud_file_storage thumbnail render failed",
				message=f"file={self.name} url={self.file_url} error={exc}",
			)
			return None

		# Core's own naming: `{filename}_{suffix}.{extn}` off the source URL
		# (`file.py:462`, `file/utils.py:104`).
		if "." in self.file_url:
			stem, extension = self.file_url.rsplit(".", 1)
		else:
			stem, extension = self.file_url, image_format.lower()
		thumbnail_url = f"{stem}_{suffix}.{extension}"

		from cloud_file_storage.serving import thumbnails

		try:
			derived = thumbnails.store_thumbnail(
				cso,
				content,
				suffix=suffix,
				width=width,
				height=height,
				mime_type=Image.MIME.get(image_format) or f"image/{image_format.lower()}",
			)
		except Exception as exc:  # noqa: BLE001 - a thumbnail is never worth failing a save for
			frappe.log_error(
				title="cloud_file_storage thumbnail upload failed",
				message=f"file={self.name} url={self.file_url} error={exc}",
			)
			return None

		if self._mode().keeps_local_copy:
			self._write_local_thumbnail(thumbnail_url, content)

		# The link is written whether or not `set_as_thumbnail` is on, because it names the
		# object behind the URL this call just made servable.
		self.db_set(thumbnails.THUMBNAIL_LINK_FIELD, derived.name, update_modified=False)
		if set_as_thumbnail:
			self.db_set("thumbnail_url", thumbnail_url)

		from cloud_file_storage.core_hooks import invalidate_public_404

		invalidate_public_404(thumbnail_url)

		return thumbnail_url

	def _render_thumbnail(self, width: int, height: int, crop: bool) -> tuple[bytes, str]:
		"""Core's resize, reading through materialization instead of off local disk."""
		import io

		from PIL import Image, ImageOps

		image = Image.open(self.get_full_path())
		image_format = (image.format or "PNG").upper()

		size = width, height
		if crop:
			image = ImageOps.fit(image, size, Image.Resampling.LANCZOS)
		else:
			image.thumbnail(size, Image.Resampling.LANCZOS)

		buffer = io.BytesIO()
		image.save(buffer, format=image_format)
		return buffer.getvalue(), image_format

	def _write_local_thumbnail(self, thumbnail_url: str, content: bytes):
		"""Keep a local copy in the modes that promise one (LOCAL_ONLY / DUAL_WRITE).

		The path comes from :func:`thumbnails.local_thumbnail_path`, not from core's
		`get_site_path("public", …)`: core's spelling puts a private file's thumbnail in
		`public/private/files/…`, where nginx serves it to anyone (A19).
		"""
		from cloud_file_storage.serving import thumbnails

		path = thumbnails.local_thumbnail_path(thumbnail_url)
		if not path:
			return
		try:
			os.makedirs(os.path.dirname(path), exist_ok=True)
			with open(path, "wb") as handle:  # nosemgrep: frappe-security-file-traversal
				handle.write(content)
		except OSError as exc:
			frappe.logger("cloud_file_storage").warning(f"could not write local thumbnail {path}: {exc}")

	# --- visibility flip (A16) ---------------------------------------------------------

	def handle_is_private_changed(self):
		"""Flip pub/ ↔ prv/ for a cloud-backed File, relinking every sharer (A16).

		Core moves the local file and rewrites the URL on every row sharing the
		`content_hash` (`update_existing_file_docs`). Those rows now point at an object
		whose visibility no longer matches their URL, so they are relinked to the new
		object **in the same transaction** — otherwise a sibling serves from the wrong
		prefix, or from an object that GC is entitled to collect.
		"""
		if self.is_remote_file:
			return

		cso = self._resolve_cso()
		if cso is None:
			return super().handle_is_private_changed()

		old_file_url = self.file_url
		file_name = self.file_url.split("/")[-1]
		url_prefix = "/private/files/" if cint(self.is_private) else "/files/"
		updated_file_url = f"{url_prefix}{file_name}"

		# Core's own guard: a dict-built doc can reach here without a real change.
		if updated_file_url == old_file_url:
			return

		settings = get_settings()
		new_visibility = visibility_for(self.is_private)
		target_key = build_object_key(cso.content_sha256, new_visibility, key_prefix=settings.key_prefix)

		new_cso = objects.ensure_cso(
			content_sha256=cso.content_sha256,
			content_hash_md5=cso.content_hash_md5,
			file_size=cso.file_size,
			visibility=new_visibility,
			mime_type=cso.mime_type,
			settings=settings,
		)

		if objects.needs_upload(new_cso):
			engine.copy_visibility(cso, target_key, settings=settings)
			objects.set_cso_status(new_cso.name, "uploaded")

		# Move the local copy too when there is one (DUAL_WRITE), reusing core's own
		# atomic-rename bookkeeping so rollback still restores it.
		self._move_local_copy(file_name)

		self.file_url = updated_file_url
		self.cloud_storage_object = new_cso.name
		self._forget_resolved_cso()

		# Only rows that genuinely share this file's hash, and only when there IS a hash.
		# A `None` filter value compiles to `IS NULL` in frappe's query builder
		# (`database/query.py:216-217`), so an unguarded lookup would return every hashless
		# File row on the site and repoint all of them at this object. Core's own
		# `update_existing_file_docs` renders `content_hash = NULL`, which matches nothing —
		# so those are precisely the rows core deliberately leaves alone, and this must too.
		# Reachable today: `patches/v0_2_0/backfill_s3_object_key` nulls the column on
		# fork-era rows.
		siblings = (
			frappe.get_all(
				"File",
				filters={"content_hash": self.content_hash, "name": ("!=", self.name)},
				pluck="name",
			)
			if self.content_hash
			else []
		)
		update_existing_file_docs(self)
		if siblings:
			objects.adopt_references(new_cso.name, siblings)

		old_thumbnail_url = self.get("thumbnail_url")
		self._relocate_thumbnails(siblings)

		if not self.is_new():
			self.db_set("cloud_storage_object", new_cso.name, update_modified=False)
		objects.refresh_reference_count(new_cso.name)

		# Refcount path only: never fast-track the old object to pending_delete (A16).
		objects.release_reference(cso.name, settings=settings)

		self._propagate_url_to_parent(old_file_url)

		# A8: the flip changes which URL is public, so both sides of it may hold a stale
		# `website_404` entry — the new one from before the file was public, the old one from
		# a request made after it stopped being. The thumbnail URLs move with them, and all
		# four go in one call because each one costs a scan of the negative cache.
		from cloud_file_storage.core_hooks import invalidate_public_404

		invalidate_public_404(old_file_url, self.file_url, old_thumbnail_url, self.get("thumbnail_url"))

	#: Local thumbnail moves made by a flip, restored by `on_rollback`. Core's own
	#: `original_path` flag holds a single pair and is already spoken for by the main file.
	THUMBNAIL_MOVE_FLAG = "cfs_thumbnail_moves"

	def _relocate_thumbnails(self, siblings: list[str]):
		"""Move `thumbnail_url` onto the new visibility's prefix — this row and its sharers.

		Core rewrites `file_url` on a flip and leaves `thumbnail_url` alone
		(`file.py:226-260`), which strands `/files/<name>_<suffix>.<ext>` on a row that is now
		private. That URL is a 300x300 rendering of private content reachable by an
		unauthenticated GET — through the public renderer's derived-object lookup, and in
		DUAL_WRITE through nginx's `try_files /<site>/public/$uri` straight off disk. Both
		halves move here: the URL, and the local copy behind it.

		The sharers matter for the same reason: `update_existing_file_docs` flips every
		content_hash sharer by raw SQL, and those rows never run this method themselves.

		The derived OBJECT is re-labelled too. `store_thumbnail` stamps it with its source's
		visibility, on the grounds that a public stamp on the thumbnail of a private file is a
		false statement about what the object holds; a flip would make that statement false
		again one save later, and an invariant that is asserted at creation and abandoned at
		update is how the next change comes to rely on something untrue. It is a label, not a
		location — a derived key is `thm/…` either way — so `objects.restamp_derived_visibility`
		is where the guards live, including the one that refuses to do this to a primary object.

		**The old URL is deliberately not aliased.** An alias would make the public renderer
		302 an anonymous caller to `/private/files/…`, which answers `Forbidden` — turning a
		clean 404 into "this file exists and is private", the existence oracle A24 forbids.
		The old URL is dead by design; the bytes it used to name are not.
		"""
		if cint(self.is_private):
			stale_prefix, new_prefix = "/files/", "/private/files/"
		else:
			stale_prefix, new_prefix = "/private/files/", "/files/"

		from cloud_file_storage.serving import thumbnails

		new_visibility = visibility_for(self.is_private)

		relocated = self._reprefixed(self.get("thumbnail_url"), stale_prefix, new_prefix)
		if relocated:
			self._move_local_thumbnail(self.thumbnail_url, relocated)
			self.thumbnail_url = relocated
		objects.restamp_derived_visibility(self.get(thumbnails.THUMBNAIL_LINK_FIELD), new_visibility)

		for sibling in siblings:
			current = frappe.db.get_value("File", sibling, "thumbnail_url")
			updated = self._reprefixed(current, stale_prefix, new_prefix)
			if updated:
				self._move_local_thumbnail(current, updated)
				frappe.db.set_value("File", sibling, "thumbnail_url", updated, update_modified=False)
			# Unconditional, unlike the URL rewrite: a row whose thumbnail_url was already
			# correctly prefixed still owns a derived object whose label just went stale.
			objects.restamp_derived_visibility(
				frappe.db.get_value("File", sibling, thumbnails.THUMBNAIL_LINK_FIELD), new_visibility
			)

	@staticmethod
	def _reprefixed(thumbnail_url: str | None, stale_prefix: str, new_prefix: str) -> str | None:
		"""The same thumbnail under the other canonical prefix, or None if it is not there.

		Keyed off the prefix the URL must NOT keep rather than off the old `file_url`, so a
		row that already carried a mismatched thumbnail URL (a fork-era row, an earlier flip
		made before this existed) is corrected by the next flip instead of preserved.
		"""
		if not thumbnail_url or not thumbnail_url.startswith(stale_prefix):
			return None
		return f"{new_prefix}{thumbnail_url[len(stale_prefix) :]}"

	def _move_local_thumbnail(self, old_url: str, new_url: str):
		"""Move the local thumbnail to match its new URL. Never deletes (invariant 1)."""
		import shutil

		from cloud_file_storage.serving import thumbnails

		source = thumbnails.local_thumbnail_path(old_url)
		target = thumbnails.local_thumbnail_path(new_url)
		if not source or not target or not os.path.exists(source) or os.path.exists(target):
			return

		try:
			os.makedirs(os.path.dirname(target), exist_ok=True)
			shutil.move(source, target)
		except OSError as exc:
			# A thumbnail that could not be moved is a thumbnail still sitting in the public
			# tree, so this is loud — but it must not fail the flip, whose DB side is what
			# actually stops the renderer serving it.
			frappe.log_error(
				title="cloud_file_storage could not move a local thumbnail",
				message=f"file={self.name} from={source} to={target} error={exc}",
			)
			return

		moves = self.flags.get(self.THUMBNAIL_MOVE_FLAG) or []
		moves.append({"old": source, "new": target})
		self.flags[self.THUMBNAIL_MOVE_FLAG] = moves
		frappe.db.after_rollback.add(self.on_rollback)

	def _move_local_copy(self, file_name: str):
		import shutil
		from pathlib import Path

		private_path = Path(frappe.get_site_path("private", "files", file_name))
		public_path = Path(frappe.get_site_path("public", "files", file_name))
		source, target = (public_path, private_path) if cint(self.is_private) else (private_path, public_path)

		if not source.exists() or target.exists():
			return

		shutil.move(source, target)
		self.flags.original_path = {"old": source, "new": target}
		frappe.db.after_rollback.add(self.on_rollback)

	def _propagate_url_to_parent(self, old_file_url: str):
		"""Core's Attach/Attach Image propagation, preserved verbatim (contract #18)."""
		if (
			not self.attached_to_doctype
			or not self.attached_to_name
			or not self.fetch_attached_to_field(old_file_url)
		):
			return

		if frappe.get_meta(self.attached_to_doctype).issingle:
			frappe.db.set_single_value(self.attached_to_doctype, self.attached_to_field, self.file_url)
		else:
			frappe.db.set_value(
				self.attached_to_doctype, self.attached_to_name, self.attached_to_field, self.file_url
			)

	# --- rollback ----------------------------------------------------------------------

	def on_rollback(self):
		"""Core's disk restore, skipped for rows that have no local copy to restore.

		Objects are immutable and content-addressed, so a DB rollback has already restored
		the `cloud_storage_object`/`content_hash` pointers; there is nothing on disk to put
		back, and calling core would materialize the object just to overwrite it.
		"""
		self._restore_moved_thumbnails()
		if self.flags.get("original_content") and not self._has_local_copy():
			self.flags.pop("original_content", None)
		return super().on_rollback()

	def _restore_moved_thumbnails(self):
		"""Put back every local thumbnail this flip moved, newest first."""
		import shutil

		for move in reversed(self.flags.pop(self.THUMBNAIL_MOVE_FLAG, None) or []):
			try:
				if os.path.exists(move["new"]) and not os.path.exists(move["old"]):
					shutil.move(move["new"], move["old"])
			except OSError as exc:
				frappe.logger("cloud_file_storage").warning(
					f"could not restore local thumbnail {move['new']}: {exc}"
				)

	def _has_local_copy(self) -> bool:
		local_path = self._canonical_local_path()
		return bool(local_path and os.path.exists(local_path))


def is_canonical_url(file_url: str | None) -> bool:
	"""`/files/...` or `/private/files/...` and nothing else (docs/INVARIANTS.md invariant 2)."""
	if not file_url:
		return False
	if file_url.startswith(URL_PREFIXES):
		return False
	return file_url.startswith(("/files/", "/private/files/"))
