"""T-PRIV / T-PERM / T-OUTAGE / T-ALIAS — the private serving path.

Every test here drives `serving.private.download_private_file_cloud` through a real
request (`tests/request_utils.http_request`), which is what makes the permission gate, the
`fid` argument and `make_access_log` behave the way they do in production. The mechanism
these rely on — that this function is what `frappe/app.py` reaches, after `validate_auth`
— is proven separately in `test_serving_spike.py`.
"""

import frappe
from werkzeug.exceptions import Forbidden, NotFound

from cloud_file_storage.serving import aliases, private
from cloud_file_storage.storage import objects
from cloud_file_storage.storage.exceptions import (
	CloudStorageTransportError,
	CloudStorageUnavailableError,
)
from cloud_file_storage.tests.request_utils import (
	access_log_count,
	as_user,
	http_request,
	make_api_credentials,
	make_user,
)
from cloud_file_storage.tests.utils import CloudStorageTestCase, set_mode


class PrivateServingTestCase(CloudStorageTestCase):
	MODE = "S3_ONLY"

	def setUp(self):
		super().setUp()
		self.owner = make_user(self, "cfs-serve-owner@example.com")
		self.stranger = make_user(self, "cfs-serve-stranger@example.com")
		self.addCleanup(self._drop_aliases)

	def _drop_aliases(self):
		frappe.set_user("Administrator")
		for name in frappe.get_all("Cloud File URL Alias", pluck="name"):
			frappe.delete_doc("Cloud File URL Alias", name, force=True, ignore_permissions=True)

	def private_file(self, *, file_name="serve.txt", content=b"private bytes", owner=None):
		with as_user(owner or self.owner.name):
			return self.make_file(file_name=file_name, content=content, is_private=1)

	def serve(self, path, *, user, **kwargs):
		with http_request(path, user=user, **kwargs):
			return private.download_private_file_cloud(path)


class TestPrivateServingRedirect(PrivateServingTestCase):
	"""T-PRIV — the permitted case: one access log, one short-TTL signed redirect."""

	def test_a_permitted_user_gets_a_302_to_a_presigned_url(self):
		doc = self.private_file()
		cso = self.cso_of(doc)

		before = access_log_count(doc.name)
		response = self.serve(doc.file_url, user=self.owner.name)

		self.assertEqual(response.status_code, 302)
		self.assertIn(cso.s3_key, response.headers["Location"])
		self.assertEqual(access_log_count(doc.name) - before, 1)

	def test_the_ttl_is_the_configured_private_ttl(self):
		set_mode("S3_ONLY", private_presign_ttl=120)
		doc = self.private_file(file_name="serve-ttl.txt")

		response = self.serve(doc.file_url, user=self.owner.name)

		self.assertEqual(self.store.presign_calls[-1]["ttl"], 120)
		# NOT `assertIn("X-Amz-Expires=120", …)`: the fake builds that string out of the ttl
		# it was handed, so it would read like end-to-end proof while asserting the test's
		# own number back. The honest runtime claim is that the redirect hands out exactly
		# the URL that was minted, unwrapped and unrecycled.
		self.assertEqual(response.headers["Location"], self.store.presign_calls[-1]["url"])

	def test_the_redirect_is_never_cached(self):
		"""The Location header is a bearer capability for the TTL (PLAN §C)."""
		doc = self.private_file(file_name="serve-nostore.txt")

		response = self.serve(doc.file_url, user=self.owner.name)

		self.assertEqual(response.headers["Cache-Control"], "no-store")

	def test_the_disposition_is_pinned_per_file_type(self):
		text = self.private_file(file_name="serve-doc.txt")
		image = self.private_file(file_name="serve-image.png", content=_png_bytes())

		self.serve(text.file_url, user=self.owner.name)
		self.assertEqual(self.store.presign_calls[-1]["disposition"], "attachment")

		self.serve(image.file_url, user=self.owner.name)
		self.assertEqual(self.store.presign_calls[-1]["disposition"], "inline")

	def test_html_is_forced_to_attachment_even_when_private(self):
		"""T-HTML, Policy B: never inline, whatever the inline prefixes say."""
		set_mode("S3_ONLY", inline_mimetype_prefixes="image/\napplication/pdf\ntext/")
		doc = self.private_file(file_name="serve-page.html", content=b"<h1>hi</h1>")

		self.serve(doc.file_url, user=self.owner.name)

		self.assertEqual(self.store.presign_calls[-1]["disposition"], "attachment")

	def test_a_token_authenticated_client_is_served(self):
		api_user = make_user(self, "cfs-serve-token@example.com")
		api_key, api_secret = make_api_credentials(api_user.name)
		doc = self.private_file(file_name="serve-token.txt", owner=api_user.name)

		with http_request(
			doc.file_url,
			user="Guest",
			headers={"Authorization": f"token {api_key}:{api_secret}"},
			run_validate_auth=True,
		):
			self.assertEqual(frappe.session.user, api_user.name)
			response = private.download_private_file_cloud(doc.file_url)

		self.assertEqual(response.status_code, 302)


class TestPrivateServingPermissions(PrivateServingTestCase):
	"""T-PERM — nothing is issued before the gate, and refusals reveal nothing."""

	def test_guest_is_forbidden_and_no_url_is_minted(self):
		doc = self.private_file(file_name="serve-guest.txt")

		with self.assertRaises(Forbidden):
			self.serve(doc.file_url, user="Guest")

		self.assertEqual(self.store.presign_calls, [])

	def test_an_unpermitted_user_is_forbidden_and_writes_no_access_log(self):
		doc = self.private_file(file_name="serve-denied.txt")

		before = access_log_count(doc.name)
		with self.assertRaises(Forbidden):
			self.serve(doc.file_url, user=self.stranger.name)

		self.assertEqual(access_log_count(doc.name), before)
		self.assertEqual(self.store.presign_calls, [])

	def test_an_unknown_url_is_forbidden_not_not_found(self):
		"""Same outcome as an unreadable one — the path is not an existence oracle."""
		with self.assertRaises(Forbidden):
			self.serve("/private/files/serve-nothing-here.txt", user=self.stranger.name)

	def test_a_shared_url_is_served_when_any_one_row_is_readable(self):
		mine = self.private_file(file_name="serve-shared.txt", content=b"shared")
		with as_user(self.stranger.name):
			theirs = self.track(
				frappe.get_doc(
					{
						"doctype": "File",
						"file_name": "serve-shared.txt",
						"file_url": mine.file_url,
						"is_private": 1,
						"content_hash": mine.content_hash,
						"file_size": mine.file_size,
					}
				).insert(ignore_permissions=True)
			)

		before = access_log_count(theirs.name)
		response = self.serve(mine.file_url, user=self.stranger.name)

		self.assertEqual(response.status_code, 302)
		# The log names the row that granted access, not the row that happens to be first.
		self.assertEqual(access_log_count(theirs.name) - before, 1)

	def test_fid_pins_the_row_and_a_pinned_refusal_is_still_forbidden(self):
		mine = self.private_file(file_name="serve-fid.txt", content=b"fid")
		with as_user(self.stranger.name):
			self.track(
				frappe.get_doc(
					{
						"doctype": "File",
						"file_name": "serve-fid.txt",
						"file_url": mine.file_url,
						"is_private": 1,
						"content_hash": mine.content_hash,
						"file_size": mine.file_size,
					}
				).insert(ignore_permissions=True)
			)

		with self.assertRaises(Forbidden):
			self.serve(mine.file_url, user=self.stranger.name, query_string={"fid": mine.name})
		self.assertEqual(self.store.presign_calls, [])


class TestPrivateServingResolutionChain(PrivateServingTestCase):
	"""A35 — serving resolves through the A1 chain, not through the row's own link."""

	def test_a_link_less_row_is_served_through_its_url_sibling(self):
		linked = self.private_file(file_name="serve-chain.txt", content=b"chain bytes")
		cso = self.cso_of(linked)

		with as_user(self.owner.name):
			unlinked = self.track(
				frappe.get_doc(
					{
						"doctype": "File",
						"file_name": "serve-chain.txt",
						"file_url": linked.file_url,
						"is_private": 1,
						"content_hash": linked.content_hash,
						"file_size": linked.file_size,
					}
				).insert(ignore_permissions=True)
			)
		# The row genuinely carries no link of its own — otherwise the chain is untested.
		frappe.db.set_value("File", unlinked.name, "cloud_storage_object", None, update_modified=False)
		self.assertFalse(frappe.db.get_value("File", unlinked.name, "cloud_storage_object"))

		with http_request(linked.file_url, user=self.owner.name, query_string={"fid": unlinked.name}):
			response = private.download_private_file_cloud(linked.file_url)

		self.assertEqual(response.status_code, 302)
		self.assertIn(cso.s3_key, response.headers["Location"])

	def test_a_pending_delete_object_is_still_servable(self):
		"""A16 — an object inside its GC grace window still has readers."""
		doc = self.private_file(file_name="serve-grace.txt", content=b"grace bytes")
		cso = self.cso_of(doc)
		objects.set_cso_status(cso.name, "pending_delete", status_before_delete=cso.status)

		response = self.serve(doc.file_url, user=self.owner.name)

		self.assertEqual(response.status_code, 302)


class TestPrivateServingLocalCopy(PrivateServingTestCase):
	"""DUAL_WRITE — a local copy is served directly, and logged exactly once (A24)."""

	MODE = "DUAL_WRITE"

	def test_a_local_copy_is_streamed_rather_than_redirected(self):
		doc = self.private_file(file_name="serve-local.txt", content=b"local bytes")
		self.assertTrue(self.local_path(doc))

		before = access_log_count(doc.name)
		response = self.serve(doc.file_url, user=self.owner.name)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(self.store.presign_calls, [])
		self.assertEqual(access_log_count(doc.name) - before, 1)

	def test_the_local_branch_does_not_double_log(self):
		"""Delegating to core's `download_private_file` here would write a second row.

		A24 is explicit that the local-copy path calls `send_private_file` directly for
		exactly this reason, so the count is the assertion.
		"""
		doc = self.private_file(file_name="serve-local-audit.txt", content=b"audit")

		before = access_log_count(doc.name)
		self.serve(doc.file_url, user=self.owner.name)
		self.serve(doc.file_url, user=self.owner.name)

		self.assertEqual(access_log_count(doc.name) - before, 2)


class TestPrivateServingModes(PrivateServingTestCase):
	"""T-MODEGATE — what each mode does on the serving path."""

	def test_local_only_delegates_to_core_untouched(self):
		set_mode("LOCAL_ONLY")
		doc = self.private_file(file_name="serve-localonly.txt", content=b"core bytes")

		self.assertFalse(private.is_active())
		response = self.serve(doc.file_url, user=self.owner.name)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(self.store.presign_calls, [])

	def test_s3_only_with_an_open_breaker_is_503_never_404(self):
		"""T-OUTAGE — a 404 would invite a caller to conclude the bytes are gone."""
		doc = self.private_file(file_name="serve-outage.txt", content=b"outage")
		self.store.fail_with = CloudStorageUnavailableError("breaker open")
		self.addCleanup(setattr, self.store, "fail_with", None)

		response = self.serve(doc.file_url, user=self.owner.name)

		self.assertEqual(response.status_code, 503)
		self.assertEqual(response.headers["Retry-After"], "30")

	def test_a_transport_failure_is_503_not_404(self):
		doc = self.private_file(file_name="serve-transport.txt", content=b"transport")
		self.store.fail_with = CloudStorageTransportError("endpoint refused")
		self.addCleanup(setattr, self.store, "fail_with", None)

		response = self.serve(doc.file_url, user=self.owner.name)

		self.assertEqual(response.status_code, 503)

	def test_an_object_not_yet_uploaded_is_503_not_404(self):
		doc = self.private_file(file_name="serve-pending.txt", content=b"pending")
		cso = self.cso_of(doc)
		objects.set_cso_status(cso.name, "failed", last_error="injected")

		response = self.serve(doc.file_url, user=self.owner.name)

		self.assertEqual(response.status_code, 503)

	def test_a_row_with_no_object_at_all_is_404_for_a_permitted_caller(self):
		"""No local copy and nothing the A1 chain resolves to: there is genuinely nothing.

		Built by removing the object under a healthy row rather than by inserting a
		bytes-less one — core's `before_insert` calls `save_file(content=get_content())`
		(`file.py:111`), so a File with no readable content cannot be inserted at all.
		"""
		doc = self.private_file(file_name="serve-no-object.txt", content=b"about to vanish")
		cso = self.cso_of(doc)
		frappe.db.set_value("File", doc.name, "cloud_storage_object", None, update_modified=False)
		frappe.db.set_value("File", doc.name, "content_hash", None, update_modified=False)
		frappe.db.delete("Cloud Storage Object", {"name": cso.name})

		with self.assertRaises(NotFound):
			self.serve(doc.file_url, user=self.owner.name)

	def test_a_genuinely_absent_object_is_404_for_a_permitted_caller(self):
		doc = self.private_file(file_name="serve-absent.txt", content=b"absent")
		cso = self.cso_of(doc)
		objects.set_cso_status(cso.name, "orphaned")

		with self.assertRaises(NotFound):
			self.serve(doc.file_url, user=self.owner.name)


class TestPrivateServingAliases(PrivateServingTestCase):
	"""T-ALIAS — an old private URL keeps resolving, without becoming an oracle."""

	def test_an_aliased_url_redirects_to_the_canonical_one(self):
		doc = self.private_file(file_name="serve-new.txt", content=b"aliased")
		old_url = "/private/files/serve-old-name.txt"
		aliases.ensure_alias(old_url, doc.file_url, file=doc.name, reason="relink")

		response = self.serve(old_url, user=self.owner.name)

		self.assertEqual(response.status_code, 302)
		self.assertEqual(response.headers["Location"], doc.file_url)
		self.assertEqual(response.headers["Cache-Control"], "no-store")

	def test_the_alias_redirect_is_302_and_never_301(self):
		"""A11 — a 301 is cached indefinitely and could never be corrected."""
		doc = self.private_file(file_name="serve-301.txt", content=b"code check")
		old_url = "/private/files/serve-301-old.txt"
		aliases.ensure_alias(old_url, doc.file_url)

		response = self.serve(old_url, user=self.owner.name)

		self.assertEqual(response.status_code, aliases.ALIAS_REDIRECT_CODE)
		self.assertEqual(aliases.ALIAS_REDIRECT_CODE, 302)

	def test_the_permission_gate_is_re_run_on_the_alias_target(self):
		"""A11 — otherwise the alias hands an unreadable file to anyone who knows an old URL."""
		doc = self.private_file(file_name="serve-alias-denied.txt", content=b"secret")
		old_url = "/private/files/serve-alias-denied-old.txt"
		aliases.ensure_alias(old_url, doc.file_url)

		with self.assertRaises(Forbidden):
			self.serve(old_url, user=self.stranger.name)

	def test_an_alias_to_a_missing_target_is_forbidden(self):
		aliases.ensure_alias("/private/files/serve-dangling.txt", "/private/files/serve-gone.txt")

		with self.assertRaises(Forbidden):
			self.serve("/private/files/serve-dangling.txt", user=self.owner.name)

	def test_a_cyclic_alias_chain_terminates(self):
		"""A → B → A must stop at B rather than spin the request thread."""
		aliases.ensure_alias("/private/files/a.txt", "/private/files/b.txt")
		aliases.ensure_alias("/private/files/b.txt", "/private/files/a.txt")

		self.assertEqual(aliases.resolve("/private/files/a.txt"), "/private/files/b.txt")

	def test_a_chain_longer_than_the_hop_limit_is_truncated_not_followed(self):
		urls = [f"/private/files/hop{index}.txt" for index in range(aliases.MAX_HOPS + 3)]
		for source, target in zip(urls, urls[1:], strict=False):
			aliases.ensure_alias(source, target)

		resolved = aliases.resolve(urls[0])

		self.assertEqual(resolved, urls[aliases.MAX_HOPS])
		self.assertNotEqual(resolved, urls[-1])


class PrivateThumbnailTestCase(PrivateServingTestCase):
	"""A private File's thumbnail is served through the same gate as its bytes.

	Nothing else can serve it: the public renderer refuses a private row (A19), and nginx
	only ever reaches `sites/<site>/public`. Leaving the URL dead is what tempts a later
	change to point it back at `/files/`, which is the A19 bypass this suite exists for.
	"""

	def thumbnailed_file(self, *, file_name, owner=None):
		doc = self.private_file(file_name=file_name, content=_png_bytes((800, 600)), owner=owner)
		# Rendered small enough that the thumbnail is genuinely a separate derived object
		# rather than the byte-identical-to-source case.
		doc.make_thumbnail(width=64, height=64)
		return frappe.get_doc("File", doc.name)

	def derived_of(self, file_doc):
		from cloud_file_storage.serving import thumbnails

		return thumbnails.thumbnail_cso_for(file_doc)

	def setUp(self):
		super().setUp()
		self.addCleanup(self._drop_derived_objects)

	@staticmethod
	def _drop_derived_objects():
		frappe.set_user("Administrator")
		for name in frappe.get_all(
			"Cloud Storage Object", filters={"derived_of": ("is", "set")}, pluck="name"
		):
			frappe.db.delete("Cloud Storage Object", {"name": name})


class TestPrivateThumbnailServing(PrivateThumbnailTestCase):
	def test_a_permitted_user_gets_a_302_to_the_derived_object(self):
		doc = self.thumbnailed_file(file_name="serve-thumb.png")
		derived = self.derived_of(doc)
		self.assertIsNotNone(derived, "precondition: the thumbnail is a derived object")

		response = self.serve(doc.thumbnail_url, user=self.owner.name)

		self.assertEqual(response.status_code, 302)
		self.assertIn(derived.s3_key, response.headers["Location"])
		self.assertEqual(response.headers["Cache-Control"], "no-store")

	def test_the_thumbnail_is_logged_exactly_once_against_its_source(self):
		doc = self.thumbnailed_file(file_name="serve-thumb-audit.png")

		before = access_log_count(doc.name)
		self.serve(doc.thumbnail_url, user=self.owner.name)

		self.assertEqual(access_log_count(doc.name) - before, 1)

	def test_an_unpermitted_user_is_forbidden_and_no_url_is_minted(self):
		doc = self.thumbnailed_file(file_name="serve-thumb-denied.png")

		before = access_log_count(doc.name)
		with self.assertRaises(Forbidden):
			self.serve(doc.thumbnail_url, user=self.stranger.name)

		self.assertEqual(self.store.presign_calls, [])
		self.assertEqual(access_log_count(doc.name), before)

	def test_guest_is_forbidden(self):
		doc = self.thumbnailed_file(file_name="serve-thumb-guest.png")

		with self.assertRaises(Forbidden):
			self.serve(doc.thumbnail_url, user="Guest")

		self.assertEqual(self.store.presign_calls, [])

	def test_an_unknown_thumbnail_url_is_forbidden_not_not_found(self):
		"""Same refusal as an unknown file URL — no existence oracle (A24)."""
		with self.assertRaises(Forbidden):
			self.serve("/private/files/serve-thumb-nothing_small.png", user=self.stranger.name)


class TestPrivateThumbnailLocalCopy(PrivateThumbnailTestCase):
	"""DUAL_WRITE — the local copy is streamed, from outside the public tree."""

	MODE = "DUAL_WRITE"

	def test_a_local_thumbnail_is_streamed_rather_than_redirected(self):
		import os

		doc = self.thumbnailed_file(file_name="serve-thumb-local.png")
		local_path = frappe.get_site_path("private", "files", "serve-thumb-local_small.png")
		self.addCleanup(lambda: os.path.exists(local_path) and os.remove(local_path))
		self.assertTrue(os.path.exists(local_path), "precondition: DUAL_WRITE keeps a local copy")

		before = access_log_count(doc.name)
		response = self.serve(doc.thumbnail_url, user=self.owner.name)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(self.store.presign_calls, [])
		self.assertEqual(access_log_count(doc.name) - before, 1)


def _png_bytes(size=(4, 4)) -> bytes:
	import io

	from PIL import Image

	buffer = io.BytesIO()
	Image.new("RGB", size, (1, 2, 3)).save(buffer, format="PNG")
	return buffer.getvalue()
