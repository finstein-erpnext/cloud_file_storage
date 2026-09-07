"""`PublicFileRenderer` — the public `/files/...` miss path.

nginx serves `public/files` directly (`try_files /{{site}}/public/$uri @webserver`), so this
renderer only ever sees requests local disk could not satisfy. It then verifies against the
**database** that the URL really belongs to a public, cloud-backed File before claiming the
request — DFP's swallow-everything regex is the anti-pattern here — and declines otherwise
so the miss falls through to a genuine 404.

Amendments implemented:

* **A8** — `website_404` is a negative cache keyed on the full request URL and consulted in
  `PathResolver.resolve()` *before* any renderer runs (`path_resolver.py:34`). Once a
  `/files/x.png` 404 lands in it, the file can be uploaded and the URL still 404s. So the
  renderer sets `frappe.local.no_cache` for every `files/*` path, which makes
  `can_cache()` false and stops `NotFoundPage.render()` writing the entry at all
  (`not_found_page.py:23-27`); and :func:`invalidate_404` clears any entry that predates an
  upload. Invisible under `developer_mode`, where `can_cache()` is already false.
* **A19** — `can_render` requires `is_private = 0`, on the primary lookup AND on the
  derived/thumbnail fallback. A private row is never served here, whatever its URL says,
  and a thumbnail is served only when the row it hangs off is public.
* **A24** — resolution uses `frappe.local.request.path` as well as the endpoint, because
  `resolve_path` strips a trailing `.html` (`path_resolver.py:159-160`), so `/files/a.html`
  reaches renderers as `files/a`.
* **A35** — the object is resolved through the A1 chain across URL-sharing rows.
* **A11** — an alias is consulted before declining.
* **Policy B** — `.html/.htm/.svg/.xml` are never served inline and never through the CDN.
"""

import os
from urllib.parse import quote, unquote, urlparse

import frappe
from frappe.utils import cint
from frappe.website.page_renderers.base_renderer import BaseRenderer
from werkzeug.utils import redirect
from werkzeug.wrappers import Response

from cloud_file_storage.serving import aliases
from cloud_file_storage.serving.disposition import disposition_for, may_use_cdn
from cloud_file_storage.storage import engine, objects
from cloud_file_storage.storage.exceptions import CloudStorageError
from cloud_file_storage.storage.modes import OperationMode, get_mode, get_settings

#: frappe's negative-404 cache (`path_resolver.py:34`, `not_found_page.py:24`).
CACHE_404 = "website_404"

PUBLIC_PREFIX = "files/"

#: Ceiling on how long a browser may reuse our redirect. Never longer than the signature.
MAX_REDIRECT_CACHE_SECONDS = 300


class PublicFileRenderer(BaseRenderer):
	def __init__(self, path=None, http_status_code=None):
		super().__init__(path, http_status_code)
		self.file_url = None
		self.file_doc = None
		self.cso = None
		self.redirect_target = None

	# --- can_render ---------------------------------------------------------------------

	def can_render(self) -> bool:
		if not self._is_public_files_path():
			return False

		# A8: from here on this request must not leave a `website_404` entry behind, whether
		# we serve it or decline it — the file may exist a second from now.
		frappe.local.no_cache = True

		if not self._app_serves_cloud_files():
			return False

		for candidate in self._candidate_urls():
			if self._resolve(candidate):
				return True

		for candidate in self._candidate_urls():
			target = aliases.resolve(candidate)
			if target and self._resolve(target):
				# The alias hop is a redirect, not a serve: the browser re-requests the
				# canonical URL and gets its own signed redirect there.
				self.redirect_target = target
				return True

		return False

	def _is_public_files_path(self) -> bool:
		path = (self.path or "").lstrip("/")
		if path.startswith(PUBLIC_PREFIX):
			return True
		request_path = self._request_path()
		return bool(request_path and request_path.lstrip("/").startswith(PUBLIC_PREFIX))

	@staticmethod
	def _request_path() -> str | None:
		request = getattr(frappe.local, "request", None)
		path = getattr(request, "path", None) if request else None
		return unquote(path) if path else None

	def _candidate_urls(self) -> list[str]:
		"""Every canonical URL this request could be asking for.

		`resolve_path` strips a trailing `.html`, so the endpoint alone cannot distinguish
		`/files/a` from `/files/a.html` — A24 requires both to resolve.
		"""
		candidates = []
		request_path = self._request_path()
		if request_path:
			candidates.append(request_path if request_path.startswith("/") else f"/{request_path}")

		endpoint = (self.path or "").lstrip("/")
		if endpoint:
			candidates.append(f"/{endpoint}")
			candidates.append(f"/{endpoint}.html")

		seen = []
		for candidate in candidates:
			if candidate not in seen and candidate.startswith("/files/"):
				seen.append(candidate)
		return seen

	@staticmethod
	def _app_serves_cloud_files() -> bool:
		try:
			if "cloud_file_storage" not in frappe.get_installed_apps():
				return False
			return get_mode() is not OperationMode.LOCAL_ONLY
		except Exception:  # noqa: BLE001 - a settings failure must not break the website
			return False

	def _resolve(self, file_url: str) -> bool:
		"""DB-verified: a public, non-folder File row at this URL with servable bytes."""
		row = frappe.db.get_value(
			"File",
			{"file_url": file_url, "is_private": 0, "is_folder": 0},
			["name", "file_name"],
			as_dict=True,
		)
		if row:
			doc = frappe.get_doc("File", row.name)
			cso = _resolve_cso(doc)
			if cso is not None and objects.is_servable(cso):
				self.file_url, self.file_doc, self.cso = file_url, doc, cso
				return True
			return False

		return self._resolve_derived(file_url)

	def _resolve_derived(self, file_url: str) -> bool:
		"""A `/files/<name>_<suffix>.<ext>` thumbnail, stored as a derived object.

		After S3_ONLY there is no local thumbnail, and no File row owns the thumbnail URL —
		core's `make_thumbnail` only writes `thumbnail_url` onto the source row
		(`file.py:462-468`). So the source row is found by its `thumbnail_url` and the
		derived object by its link to that row's object (PLAN.md §A thumbnail policy).

		**A19 applies here exactly as it does to `_resolve`, and it is easier to lose.** A
		thumbnail is a 300x300 rendering of the source's bytes, so serving one is serving the
		file; the source row is the only thing carrying `is_private`, and this query is the
		only place that can consult it. A private row can hold a `/files/...` thumbnail_url
		for reasons this renderer cannot see — a row flipped private, a fork-era row, a
		compat patch — so the filter is on what the row says now, never on how it got there.
		"""
		from cloud_file_storage.serving import thumbnails

		row = frappe.db.get_value(
			"File",
			{"thumbnail_url": file_url, "is_private": 0, "is_folder": 0},
			["name", "file_name"],
			as_dict=True,
		)
		if not row:
			return False

		doc = frappe.get_doc("File", row.name)
		derived = thumbnails.thumbnail_cso_for(doc)
		if derived is None or not objects.is_servable(derived):
			return False

		self.file_url, self.file_doc, self.cso = file_url, doc, derived
		return True

	# --- render -------------------------------------------------------------------------

	def render(self) -> Response:
		if self.redirect_target:
			response = redirect(self.redirect_target, code=aliases.ALIAS_REDIRECT_CODE)
			response.headers["Cache-Control"] = "no-store"
			return response

		settings = get_settings()
		file_name = os.path.basename(self.file_url or "")

		if cint(settings.audit_public_fallback):
			from frappe.core.doctype.access_log.access_log import make_access_log

			make_access_log(
				doctype="File",
				document=self.file_doc.name,
				file_type=os.path.splitext(file_name)[-1][1:],
			)

		ttl = cint(settings.public_presign_ttl) or engine.DEFAULT_PUBLIC_PRESIGN_TTL

		cdn_url = self._cdn_url(settings, file_name)
		if cdn_url:
			return self._redirect(cdn_url, ttl)

		try:
			signed_url = engine.presign_get(
				self.cso,
				ttl=ttl,
				disposition=disposition_for(file_name, self.cso.mime_type, settings),
				filename=file_name,
				content_type=self.cso.mime_type,
				settings=settings,
			)
		except CloudStorageError as exc:
			frappe.logger("cloud_file_storage").warning(f"public serving unavailable: {exc}")
			response = Response("Temporarily unavailable", status=503, content_type="text/plain")
			response.headers["Retry-After"] = "30"
			response.headers["Cache-Control"] = "no-store"
			return response

		return self._redirect(signed_url, ttl)

	def _cdn_url(self, settings, file_name: str) -> str | None:
		cdn_base_url = (settings.cdn_base_url or "").strip().rstrip("/")
		if not cdn_base_url:
			return None
		# Policy B: an `.html`/`.svg`/`.xml` object served from the edge would render inline,
		# because a CDN cannot be handed a disposition the way a signature can.
		if not may_use_cdn(file_name):
			return None
		return f"{cdn_base_url}/{quote(self.cso.s3_key)}"

	@staticmethod
	def _redirect(location: str, ttl: int) -> Response:
		"""**Public bytes only.** `private, max-age=…` is safe here and nowhere else.

		The rows this renderer serves have already passed the `is_private = 0` filter, so the
		Location header is a capability for bytes anyone may fetch; letting one browser reuse
		it for a few minutes saves a round trip and discloses nothing. A private redirect is a
		bearer capability for bytes the *caller* was permitted to read, and must carry
		`no-store` — which is why `private.py:_redirect` is a separate function rather than
		this one imported. Do not reuse this for a private path.

		`ttl - 60` keeps the cached redirect strictly shorter-lived than the signature it
		points at, so a reused 302 can never land on an already-expired URL.
		"""
		response = redirect(location, code=302)
		max_age = max(0, min(MAX_REDIRECT_CACHE_SECONDS, ttl - 60))
		response.headers["Cache-Control"] = f"private, max-age={max_age}"
		return response


def _resolve_cso(file_doc):
	"""A35 — the A1 chain, so a row that never carried a link still resolves."""
	resolver = getattr(file_doc, "_resolve_cso", None)
	if resolver:
		return resolver()
	return objects.get_cso(file_doc.get("cloud_storage_object"))


# --- A8: negative-cache invalidation ------------------------------------------------------


def invalidate_404(*file_urls: str | None) -> int:
	"""Drop `website_404` entries for these URLs. Returns how many were removed.

	The cache is keyed on the whole request URL (scheme, host and query string included),
	so the entries are found by comparing the parsed *path* rather than by reconstructing a
	key. `.html` gets both spellings because `resolve_path` strips the extension.

	**Known cost, deferred to P4 (recorded in DECISIONS.md):** that key shape forces a scan
	of every `website_404` hkey, on every file write. It is O(cache), not O(1), and the
	cache is sized by unrelated 404 traffic. Hence the variadic signature — a caller with
	several URLs pays for one scan, not one each — and hence this is not "fixed" by
	dropping the whole hash, which would trade a bounded per-write cost for unbounded
	re-computation of every other 404 on the site.
	"""
	paths = set()
	for file_url in file_urls:
		if not file_url:
			continue
		paths.add(file_url)
		if file_url.endswith(".html"):
			paths.add(file_url[:-5])
		else:
			paths.add(f"{file_url}.html")

	if not paths:
		return 0

	try:
		keys = frappe.cache.hkeys(CACHE_404) or []
	except Exception:  # noqa: BLE001 - a Redis hiccup must not fail a file write
		return 0

	removed = 0
	for key in keys:
		# redis hands back bytes; the delete has to go in as the decoded string, because
		# `RedisWrapper.hdel` also evicts `frappe.local.cache[name][key]` and that memo is
		# keyed by the string `hget` was called with (`redis_wrapper.py:245-254`). Deleting
		# by the bytes key clears redis and leaves the process-local copy answering True.
		url = key.decode() if isinstance(key, bytes) else str(key)
		try:
			cached_path = unquote(urlparse(url).path)
		except ValueError:
			continue
		if cached_path in paths:
			try:
				frappe.cache.hdel(CACHE_404, url)
				removed += 1
			except Exception:  # noqa: BLE001
				continue

	return removed
