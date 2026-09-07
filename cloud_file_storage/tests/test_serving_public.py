"""The public `/files/...` miss path — `PublicFileRenderer` and the A8 negative cache.

The renderer is driven the way `PathResolver` drives it: instantiated with the *endpoint*
(the path after `resolve_path`, which strips a trailing `.html`) while `frappe.local.request`
carries the real path. Getting that pair right is the whole of A24's "resolve both
`/{endpoint}` and `/{endpoint}.html`".
"""

import frappe
from frappe.website.path_resolver import PathResolver, resolve_path
from frappe.website.utils import can_cache

from cloud_file_storage.serving import aliases
from cloud_file_storage.serving.public import CACHE_404, PublicFileRenderer, invalidate_404
from cloud_file_storage.storage import objects
from cloud_file_storage.tests.request_utils import access_log_count, http_request
from cloud_file_storage.tests.utils import CloudStorageTestCase, set_mode


class PublicServingTestCase(CloudStorageTestCase):
	MODE = "S3_ONLY"

	def setUp(self):
		super().setUp()
		self.addCleanup(self._drop_aliases)
		self.addCleanup(self._clear_404_cache)
		self.addCleanup(self._clear_no_cache)

	@staticmethod
	def _clear_no_cache():
		if hasattr(frappe.local, "no_cache"):
			frappe.local.no_cache = False

	@staticmethod
	def _clear_404_cache():
		frappe.cache.delete_value(CACHE_404)

	@staticmethod
	def _drop_aliases():
		frappe.set_user("Administrator")
		for name in frappe.get_all("Cloud File URL Alias", pluck="name"):
			frappe.delete_doc("Cloud File URL Alias", name, force=True, ignore_permissions=True)

	def public_file(self, *, file_name, content=b"public bytes"):
		return self.make_file(file_name=file_name, content=content, is_private=0)

	def render(self, request_path: str, endpoint: str | None = None):
		"""Instantiate and drive the renderer exactly as `PathResolver` would."""
		with http_request(request_path):
			renderer = PublicFileRenderer(endpoint if endpoint is not None else request_path.lstrip("/"))
			if not renderer.can_render():
				return None
			return renderer.render()


class TestPublicRendererResolution(PublicServingTestCase):
	def test_a_cloud_backed_public_file_gets_a_302(self):
		doc = self.public_file(file_name="pub-hit.txt")
		cso = self.cso_of(doc)

		response = self.render(doc.file_url)

		self.assertIsNotNone(response)
		self.assertEqual(response.status_code, 302)
		self.assertIn(cso.s3_key, response.headers["Location"])

	def test_the_ttl_is_the_configured_public_ttl(self):
		set_mode("S3_ONLY", public_presign_ttl=900)
		doc = self.public_file(file_name="pub-ttl.txt")

		self.render(doc.file_url)

		self.assertEqual(self.store.presign_calls[-1]["ttl"], 900)

	def test_a_path_with_no_file_row_is_declined(self):
		"""Verified miss-fallthrough: the renderer must not swallow unrelated 404s."""
		self.assertIsNone(self.render("/files/pub-nothing-here.txt"))
		self.assertEqual(self.store.presign_calls, [])

	def test_a_non_files_path_is_declined(self):
		self.assertIsNone(self.render("/some/website/page"))

	def test_a_private_row_is_declined(self):
		"""A19 — `is_private = 0` is the public path's entire security boundary."""
		private_doc = self.make_file(file_name="pub-private.txt", content=b"secret", is_private=1)
		public_looking_url = private_doc.file_url.replace("/private/files/", "/files/")

		self.assertIsNone(self.render(public_looking_url))
		self.assertEqual(self.store.presign_calls, [])

	def test_a_row_whose_object_is_gone_is_declined(self):
		doc = self.public_file(file_name="pub-orphaned.txt")
		cso = self.cso_of(doc)
		objects.set_cso_status(cso.name, "orphaned")

		self.assertIsNone(self.render(doc.file_url))

	def test_local_only_declines_everything(self):
		doc = self.public_file(file_name="pub-localonly.txt")
		set_mode("LOCAL_ONLY")

		self.assertIsNone(self.render(doc.file_url))

	def test_a_link_less_row_resolves_through_its_url_sibling(self):
		"""A35 — the A1 chain, on the public path too."""
		linked = self.public_file(file_name="pub-chain.txt")
		cso = self.cso_of(linked)
		sibling = self.track(
			frappe.get_doc(
				{
					"doctype": "File",
					"file_name": "pub-chain.txt",
					"file_url": linked.file_url,
					"is_private": 0,
					"content_hash": linked.content_hash,
					"file_size": linked.file_size,
				}
			).insert(ignore_permissions=True)
		)
		frappe.db.set_value("File", sibling.name, "cloud_storage_object", None, update_modified=False)
		# Remove the linked row so only the link-less one is left at this URL.
		frappe.db.set_value(
			"File", linked.name, "file_url", "/files/pub-chain-moved.txt", update_modified=False
		)

		response = self.render(linked.file_url)

		self.assertIsNotNone(response)
		self.assertIn(cso.s3_key, response.headers["Location"])


class TestPublicRendererHtmlEndpoints(PublicServingTestCase):
	"""A24 — `resolve_path` strips `.html`, so both spellings have to resolve."""

	def test_resolve_path_really_does_strip_the_html_extension(self):
		"""The premise. If frappe stops doing this, the workaround below is dead weight."""
		with http_request("/files/pub-page.html"):
			self.assertEqual(resolve_path("files/pub-page.html"), "files/pub-page")

	def test_an_html_file_resolves_from_the_stripped_endpoint(self):
		doc = self.public_file(file_name="pub-page.html", content=b"<h1>page</h1>")

		# Exactly what PathResolver hands a renderer for this request.
		response = self.render("/files/pub-page.html", endpoint="files/pub-page")

		self.assertIsNotNone(response)
		self.assertEqual(response.status_code, 302)
		self.assertIn(self.cso_of(doc).s3_key, response.headers["Location"])

	def test_an_extensionless_endpoint_still_matches_its_own_file(self):
		doc = self.public_file(file_name="pub-plain", content=b"no extension")

		response = self.render(doc.file_url)

		self.assertIsNotNone(response)


class TestPublicRendererHtmlPolicyB(PublicServingTestCase):
	"""T-HTML — Policy B is release behaviour on the public path, not a deployment note."""

	def test_html_is_forced_to_attachment(self):
		set_mode("S3_ONLY", inline_mimetype_prefixes="image/\napplication/pdf\ntext/")
		self.public_file(file_name="pub-policy.html", content=b"<script>1</script>")

		self.render("/files/pub-policy.html", endpoint="files/pub-policy")

		self.assertEqual(self.store.presign_calls[-1]["disposition"], "attachment")

	def test_svg_and_xml_are_forced_to_attachment(self):
		set_mode("S3_ONLY", inline_mimetype_prefixes="image/\ntext/\napplication/")
		self.public_file(file_name="pub-policy.svg", content=b"<svg/>")
		self.public_file(file_name="pub-policy.xml", content=b"<x/>")

		self.render("/files/pub-policy.svg")
		self.assertEqual(self.store.presign_calls[-1]["disposition"], "attachment")

		self.render("/files/pub-policy.xml")
		self.assertEqual(self.store.presign_calls[-1]["disposition"], "attachment")

	def test_html_is_never_served_from_the_cdn(self):
		"""A CDN URL carries no disposition we control, so it would render inline."""
		set_mode("S3_ONLY", cdn_base_url="https://cdn.example.invalid")
		self.public_file(file_name="pub-cdn.html", content=b"<h1>x</h1>")

		response = self.render("/files/pub-cdn.html", endpoint="files/pub-cdn")

		self.assertNotIn("cdn.example.invalid", response.headers["Location"])
		self.assertEqual(self.store.presign_calls[-1]["disposition"], "attachment")

	def test_an_image_does_go_to_the_cdn_when_one_is_configured(self):
		"""Proves the CDN branch works, so the html test above is a real exclusion."""
		set_mode("S3_ONLY", cdn_base_url="https://cdn.example.invalid")
		doc = self.public_file(file_name="pub-cdn.png", content=_png_bytes())

		response = self.render(doc.file_url)

		self.assertIn("cdn.example.invalid", response.headers["Location"])
		self.assertIn(self.cso_of(doc).s3_key, response.headers["Location"])
		self.assertEqual(self.store.presign_calls, [])


class TestPublicRendererOutage(PublicServingTestCase):
	"""T-OUTAGE, public half — degrade to 503, never to a cached 404."""

	def test_a_storage_failure_is_503_with_a_retry_after(self):
		from cloud_file_storage.storage.exceptions import CloudStorageUnavailableError

		doc = self.public_file(file_name="pub-outage.txt")
		self.store.fail_with = CloudStorageUnavailableError("breaker open")
		self.addCleanup(setattr, self.store, "fail_with", None)

		with http_request(doc.file_url):
			renderer = PublicFileRenderer(doc.file_url.lstrip("/"))
			# The renderer still claims the request: the row and the object both exist, so
			# declining would hand the URL to NotFoundPage and cache a 404 for a live file.
			self.assertTrue(renderer.can_render())
			response = renderer.render()

		self.assertEqual(response.status_code, 503)
		self.assertEqual(response.headers["Retry-After"], "30")
		self.assertEqual(response.headers["Cache-Control"], "no-store")


class TestPublicRendererAudit(PublicServingTestCase):
	def test_public_serving_is_not_logged_by_default(self):
		doc = self.public_file(file_name="pub-audit-off.txt")

		before = access_log_count(doc.name)
		self.render(doc.file_url)

		self.assertEqual(access_log_count(doc.name), before)

	def test_public_serving_is_logged_when_audit_is_enabled(self):
		set_mode("S3_ONLY", audit_public_fallback=1)
		doc = self.public_file(file_name="pub-audit-on.txt")

		before = access_log_count(doc.name)
		self.render(doc.file_url)

		self.assertEqual(access_log_count(doc.name) - before, 1)


class TestPublicRendererAliases(PublicServingTestCase):
	"""A11 — the public path consults the alias before declining."""

	def test_an_aliased_public_url_redirects_to_the_canonical_one(self):
		doc = self.public_file(file_name="pub-new.txt")
		old_url = "/files/pub-old-name.txt"
		aliases.ensure_alias(old_url, doc.file_url, file=doc.name, reason="conflict")

		response = self.render(old_url)

		self.assertIsNotNone(response)
		self.assertEqual(response.status_code, 302)
		self.assertEqual(response.headers["Location"], doc.file_url)

	def test_an_alias_to_a_private_row_is_still_declined(self):
		"""The alias must not be a way around A19."""
		private_doc = self.make_file(file_name="pub-alias-private.txt", content=b"secret", is_private=1)
		aliases.ensure_alias("/files/pub-alias-private-old.txt", private_doc.file_url)

		self.assertIsNone(self.render("/files/pub-alias-private-old.txt"))


class TestPrivacyFlipServing(PublicServingTestCase):
	"""T-PRIVFLIP, the serving half.

	A16's relink is covered at the storage layer in `test_cloud_file.py`; what matters
	here is that after the flip each URL is served by the right path and neither is left
	pinned by a stale negative-cache entry.
	"""

	def test_a_file_flipped_to_public_becomes_renderable(self):
		doc = self.make_file(file_name="flip-serve.txt", content=b"flip bytes", is_private=1)
		self.assertIsNone(self.render(doc.file_url.replace("/private/files/", "/files/")))

		flipped = frappe.get_doc("File", doc.name)
		flipped.is_private = 0
		flipped.save()

		response = self.render(flipped.file_url)

		self.assertIsNotNone(response)
		self.assertEqual(response.status_code, 302)
		self.assertIn(self.cso_of(flipped).s3_key, response.headers["Location"])

	def test_a_file_flipped_to_private_stops_being_renderable(self):
		doc = self.make_file(file_name="flip-serve-back.txt", content=b"flip back", is_private=0)
		public_url = doc.file_url
		self.assertIsNotNone(self.render(public_url))

		flipped = frappe.get_doc("File", doc.name)
		flipped.is_private = 1
		flipped.save()

		self.assertIsNone(self.render(public_url))

	def test_the_flip_clears_the_negative_cache_for_both_urls(self):
		"""A8 names `handle_is_private_changed` as an invalidation point for exactly this."""
		doc = self.make_file(file_name="flip-404.txt", content=b"flip cache", is_private=1)
		public_url = doc.file_url.replace("/private/files/", "/files/")

		with http_request(public_url) as request:
			poisoned = request.url
		frappe.cache.hset(CACHE_404, poisoned, True)
		self.assertTrue(frappe.cache.hget(CACHE_404, poisoned))

		flipped = frappe.get_doc("File", doc.name)
		flipped.is_private = 0
		flipped.save()

		self.assertFalse(frappe.cache.hget(CACHE_404, poisoned))


class TestWebsite404NegativeCache(PublicServingTestCase):
	"""A8 — the cache that would otherwise pin a 404 in place after the upload fixed it."""

	def setUp(self):
		super().setUp()
		# A8 is production-only behaviour and this bench runs `developer_mode: 1`, under
		# which `can_cache()` is always False and none of this exists. So developer_mode is
		# turned off for the duration — process-local, the site config is untouched.
		#
		# NOT `frappe.flags.force_website_cache`: that flag returns True from `can_cache()`
		# *before* the `frappe.local.no_cache` check (`website/utils.py:52-59`), so forcing
		# the cache that way would defeat the very mechanism under test and every assertion
		# below would be measuring the flag.
		conf = frappe.local.conf
		self._previous_developer_mode = conf.get("developer_mode")
		self._previous_disable_cache = conf.get("disable_website_cache")
		conf.developer_mode = 0
		conf.disable_website_cache = 0
		frappe.local.no_cache = False
		self.addCleanup(self._restore_cache_conf)

	def _restore_cache_conf(self):
		frappe.local.conf.developer_mode = self._previous_developer_mode
		frappe.local.conf.disable_website_cache = self._previous_disable_cache
		frappe.local.no_cache = False

	def test_the_cache_is_really_on_for_this_test(self):
		"""Precondition. If this fails, every other test in the class proves nothing."""
		self.assertTrue(can_cache())

	def test_the_renderer_stops_a_files_404_being_cached(self):
		"""A `/files/...` miss must not poison the URL for the upload that follows it."""
		path = "/files/pub-404-guard.txt"

		with http_request(path):
			self.assertTrue(can_cache())
			renderer = PublicFileRenderer(path.lstrip("/"))
			self.assertFalse(renderer.can_render())
			# `NotFoundPage.render` consults exactly this before writing its entry.
			self.assertFalse(can_cache())

	def test_a_non_files_404_is_still_cacheable(self):
		"""The suppression is scoped: the rest of the website keeps its negative cache."""
		with http_request("/some/website/page"):
			renderer = PublicFileRenderer("some/website/page")
			self.assertFalse(renderer.can_render())
			self.assertTrue(can_cache())

	@staticmethod
	def _poison(path: str) -> str:
		"""Cache a 404 for ``path`` under the key a real request would produce."""
		with http_request(path) as request:
			url = request.url
		frappe.cache.hset(CACHE_404, url, True)
		return url

	def test_a_stale_entry_is_cleared_when_the_file_is_written(self):
		url = self._poison("/files/pub-404-stale.txt")
		self.assertTrue(frappe.cache.hget(CACHE_404, url))

		# The write hook is what calls this, on the URL it just made servable.
		self.public_file(file_name="pub-404-stale.txt")

		self.assertFalse(frappe.cache.hget(CACHE_404, url))

	def test_invalidation_matches_the_html_spelling_too(self):
		url = self._poison("/files/pub-404-page.html")

		# `resolve_path` strips `.html`, so a stored URL may be either spelling.
		removed = invalidate_404("/files/pub-404-page")

		self.assertEqual(removed, 1)
		self.assertFalse(frappe.cache.hget(CACHE_404, url))

	def test_invalidation_leaves_unrelated_entries_alone(self):
		mine = self._poison("/files/pub-404-mine.txt")
		theirs = self._poison("/files/pub-404-theirs.txt")

		self.assertEqual(invalidate_404("/files/pub-404-mine.txt"), 1)

		self.assertFalse(frappe.cache.hget(CACHE_404, mine))
		self.assertTrue(frappe.cache.hget(CACHE_404, theirs))

	def test_a_cached_404_short_circuits_before_any_renderer_runs(self):
		"""Why invalidation is needed at all, and not just `no_cache`.

		`PathResolver.resolve` checks the cache first (`path_resolver.py:34`), so a poisoned
		entry means our renderer is never consulted — asserted here rather than assumed.
		"""
		from frappe.website.page_renderers.not_found_page import NotFoundPage

		doc = self.public_file(file_name="pub-404-shortcircuit.txt")
		self._poison(doc.file_url)

		with http_request(doc.file_url):
			_endpoint, renderer = PathResolver(doc.file_url).resolve()
			self.assertIsInstance(renderer, NotFoundPage)

		invalidate_404(doc.file_url)
		with http_request(doc.file_url):
			_endpoint, renderer = PathResolver(doc.file_url).resolve()
			self.assertIsInstance(renderer, PublicFileRenderer)


def _png_bytes() -> bytes:
	import io

	from PIL import Image

	buffer = io.BytesIO()
	Image.new("RGB", (4, 4), (7, 8, 9)).save(buffer, format="PNG")
	return buffer.getvalue()
