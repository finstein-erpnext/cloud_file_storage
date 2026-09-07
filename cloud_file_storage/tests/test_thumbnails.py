"""Thumbnails as derived objects (PLAN.md §A, superseding ADR-11/A17).

Core's `make_thumbnail` reads the source straight off local disk, so on an S3-primary site
it silently returns None and every Attach Image preview disappears. These tests cover the
override that fixes that, the derived-object identity it produces, the renderer path that
serves it after S3_ONLY, and the availability gate that stops a local thumbnail being
deleted before its replacement is provably there.
"""

import io
import os

import frappe
from PIL import Image

from cloud_file_storage.serving import thumbnails
from cloud_file_storage.serving.public import PublicFileRenderer
from cloud_file_storage.storage import objects
from cloud_file_storage.storage.keys import build_derived_object_key
from cloud_file_storage.tests.request_utils import http_request
from cloud_file_storage.tests.utils import CSO_DOCTYPE, CloudStorageTestCase


def png_bytes(size=(800, 600), colour=(120, 60, 30)) -> bytes:
	"""A source image large enough that a 300x300 box genuinely downscales it.

	`Image.thumbnail` only ever shrinks, and PIL's PNG encoder is deterministic, so a
	source SMALLER than the box re-encodes to byte-identical output — a case with its own
	tests below, and one that must not be the accidental default here.
	"""
	buffer = io.BytesIO()
	Image.new("RGB", size, colour).save(buffer, format="PNG")
	return buffer.getvalue()


class ThumbnailTestCase(CloudStorageTestCase):
	MODE = "S3_ONLY"

	def setUp(self):
		super().setUp()
		self.addCleanup(self._drop_derived_objects)

	@staticmethod
	def _drop_derived_objects():
		for name in frappe.get_all(CSO_DOCTYPE, filters={"derived_of": ("is", "set")}, pluck="name"):
			frappe.db.delete(CSO_DOCTYPE, {"name": name})

	def image_file(self, *, file_name="thumb-source.png", is_private=0, size=(800, 600)):
		return self.make_file(file_name=file_name, content=png_bytes(size), is_private=is_private)


class TestDerivedObjectKeys(ThumbnailTestCase):
	def test_the_key_is_sharded_on_the_source_hash_under_thm(self):
		source_sha = "ab" + "c" * 62
		key = build_derived_object_key(source_sha, 300, 300)

		self.assertEqual(key.split("/")[-4], "thm")
		self.assertEqual(key.split("/")[-3], source_sha[:2])
		self.assertEqual(key.split("/")[-2], source_sha[2:4])
		self.assertEqual(key.split("/")[-1], f"{source_sha}_300x300")

	def test_the_same_source_and_size_produce_the_same_key(self):
		source_sha = "de" + "f" * 62
		self.assertEqual(
			build_derived_object_key(source_sha, 300, 300),
			build_derived_object_key(source_sha, 300, 300),
		)

	def test_a_different_size_is_a_different_object(self):
		source_sha = "de" + "f" * 62
		self.assertNotEqual(
			build_derived_object_key(source_sha, 300, 300),
			build_derived_object_key(source_sha, 120, 120),
		)

	def test_the_key_prefix_is_honoured(self):
		source_sha = "01" + "2" * 62
		self.assertTrue(
			build_derived_object_key(source_sha, 10, 10, key_prefix="team/a").startswith("team/a/")
		)

	def test_invalid_input_is_refused(self):
		with self.assertRaises(ValueError):
			build_derived_object_key("not-a-sha", 300, 300)
		with self.assertRaises(ValueError):
			build_derived_object_key("ab" + "c" * 62, 0, 300)


class TestMakeThumbnail(ThumbnailTestCase):
	def test_a_cloud_only_image_still_produces_a_thumbnail(self):
		"""Core returns None here: `get_local_image` opens a path that does not exist."""
		doc = self.image_file()
		self.assertFalse(os.path.exists(doc._canonical_local_path()))

		thumbnail_url = doc.make_thumbnail()

		self.assertEqual(thumbnail_url, "/files/thumb-source_small.png")
		self.assertEqual(frappe.db.get_value("File", doc.name, "thumbnail_url"), thumbnail_url)

	def test_the_thumbnail_is_uploaded_as_a_derived_object(self):
		doc = self.image_file(file_name="thumb-derived.png")
		source_cso = self.cso_of(doc)

		doc.make_thumbnail()

		derived = thumbnails.find_derived_cso(source_cso.name, "small")
		self.assertIsNotNone(derived)
		self.assertEqual(derived.derived_of, source_cso.name)
		self.assertEqual(derived.derived_suffix, "small")
		self.assertTrue(self.store.contains(derived))

	def test_the_derived_key_is_under_thm_and_names_the_source(self):
		doc = self.image_file(file_name="thumb-key.png")
		source_cso = self.cso_of(doc)

		doc.make_thumbnail(width=120, height=120)

		derived = thumbnails.find_derived_cso(source_cso.name, "small")
		self.assertEqual(
			derived.s3_key,
			build_derived_object_key(source_cso.content_sha256, 120, 120),
		)

	def test_the_uploaded_bytes_are_a_real_resized_image(self):
		doc = self.image_file(file_name="thumb-bytes.png")
		source_cso = self.cso_of(doc)

		doc.make_thumbnail(width=16, height=16)

		derived = thumbnails.find_derived_cso(source_cso.name, "small")
		rendered = Image.open(io.BytesIO(self.store.objects[derived.s3_key]))
		self.assertLessEqual(max(rendered.size), 16)
		self.assertEqual(rendered.format, "PNG")

	def test_s3_only_writes_no_local_thumbnail(self):
		"""PLAN §A: after S3_ONLY no permanent thumbnail requires local persistence."""
		doc = self.image_file(file_name="thumb-nolocal.png")

		thumbnail_url = doc.make_thumbnail()

		local = frappe.get_site_path("public", thumbnail_url.lstrip("/"))
		self.assertFalse(os.path.exists(local))

	def test_dual_write_keeps_a_local_thumbnail(self):
		"""...and the mode that promises a local copy still writes one."""
		from cloud_file_storage.tests.utils import set_mode

		set_mode("DUAL_WRITE")
		doc = self.image_file(file_name="thumb-dual.png")

		thumbnail_url = doc.make_thumbnail()

		local = frappe.get_site_path("public", thumbnail_url.lstrip("/"))
		self.addCleanup(lambda: os.path.exists(local) and os.remove(local))
		self.assertTrue(os.path.exists(local))

	def test_re_rendering_converges_on_one_object(self):
		doc = self.image_file(file_name="thumb-idempotent.png")
		source_cso = self.cso_of(doc)

		doc.make_thumbnail()
		doc.make_thumbnail()

		derived = frappe.get_all(CSO_DOCTYPE, filters={"derived_of": source_cso.name}, pluck="name")
		self.assertEqual(len(derived), 1)

	def test_a_thumbnail_identical_to_its_source_reuses_the_source_object(self):
		"""A box larger than the image re-encodes to the same bytes.

		The frozen `(content_sha256, visibility, bucket)` unique index forbids a second row
		for those bytes, and content addressing says it should: one set of bytes, one
		object. The File-side link is what still makes the thumbnail URL servable.
		"""
		doc = self.image_file(file_name="thumb-tiny.png", size=(32, 24))
		source = self.cso_of(doc)
		before = frappe.db.count(CSO_DOCTYPE, {"bucket": source.bucket})

		thumbnail_url = doc.make_thumbnail(width=300, height=300)

		self.assertEqual(frappe.db.count(CSO_DOCTYPE, {"bucket": source.bucket}), before)
		reloaded = frappe.get_doc("File", doc.name)
		self.assertEqual(reloaded.cloud_thumbnail_object, source.name)
		self.assertEqual(thumbnails.thumbnail_cso_for(reloaded).name, source.name)

		with http_request(thumbnail_url):
			renderer = PublicFileRenderer(thumbnail_url.lstrip("/"))
			self.assertTrue(renderer.can_render(), "a reused-source thumbnail must still serve")

	def test_a_downscaled_thumbnail_really_is_a_separate_object(self):
		"""...so the reuse test above is about identical bytes, not about never creating one."""
		doc = self.image_file(file_name="thumb-distinct.png")
		source = self.cso_of(doc)

		doc.make_thumbnail(width=64, height=64)

		derived = thumbnails.thumbnail_cso_for(frappe.get_doc("File", doc.name))
		self.assertNotEqual(derived.name, source.name)
		self.assertEqual(derived.derived_of, source.name)

	def test_a_local_only_file_uses_core(self):
		from cloud_file_storage.tests.utils import set_mode

		set_mode("LOCAL_ONLY")
		doc = self.image_file(file_name="thumb-core.png")

		thumbnail_url = doc.make_thumbnail()

		local = frappe.get_site_path("public", (thumbnail_url or "").lstrip("/"))
		self.addCleanup(lambda: os.path.exists(local) and os.remove(local))
		self.assertTrue(os.path.exists(local))
		self.assertEqual(frappe.db.count(CSO_DOCTYPE, {"derived_of": ("is", "set")}), 0)

	def test_an_unreadable_source_returns_none_rather_than_throwing(self):
		doc = self.make_file(file_name="thumb-not-an-image.txt", content=b"definitely not a png")

		self.assertIsNone(doc.make_thumbnail())


class TestDerivedObjectServing(ThumbnailTestCase):
	"""`/files/*_small.*` misses resolve through the derived object."""

	def test_a_thumbnail_url_miss_is_served_from_the_derived_object(self):
		doc = self.image_file(file_name="thumb-serve.png")
		thumbnail_url = doc.make_thumbnail()
		derived = thumbnails.find_derived_cso(self.cso_of(doc).name, "small")

		with http_request(thumbnail_url):
			renderer = PublicFileRenderer(thumbnail_url.lstrip("/"))
			self.assertTrue(renderer.can_render())
			response = renderer.render()

		self.assertEqual(response.status_code, 302)
		self.assertIn(derived.s3_key, response.headers["Location"])

	def test_a_thumbnail_url_with_no_derived_object_is_declined(self):
		doc = self.image_file(file_name="thumb-missing.png")
		frappe.db.set_value("File", doc.name, "thumbnail_url", "/files/thumb-missing_small.png")

		with http_request("/files/thumb-missing_small.png"):
			renderer = PublicFileRenderer("files/thumb-missing_small")
			self.assertFalse(renderer.can_render())

	def test_the_suffix_is_read_back_off_the_url(self):
		self.assertEqual(thumbnails.suffix_from_url("/files/photo_small.png"), "small")
		self.assertEqual(thumbnails.suffix_from_url("/files/photo_thumb.jpg"), "thumb")
		self.assertIsNone(thumbnails.suffix_from_url("/files/photo.png"))
		self.assertIsNone(thumbnails.suffix_from_url(None))


class TestThumbnailAvailabilityGate(ThumbnailTestCase):
	"""PLAN §A: a local thumbnail is never deleted until the source is verified AND
	thumbnail availability is guaranteed. Both halves, separately."""

	def test_both_verified_allows_release(self):
		doc = self.image_file(file_name="thumb-gate-ok.png")
		doc.make_thumbnail()
		source = self.cso_of(doc)
		objects.set_cso_status(source.name, "verified")
		derived = thumbnails.thumbnail_cso_for(frappe.get_doc("File", doc.name))
		objects.set_cso_status(derived.name, "verified")

		allowed, reason = thumbnails.can_release_local_thumbnail(frappe.get_doc("File", doc.name))

		self.assertTrue(allowed, reason)

	def test_an_unverified_source_blocks_release(self):
		doc = self.image_file(file_name="thumb-gate-source.png")
		doc.make_thumbnail()
		source = self.cso_of(doc)
		derived = thumbnails.thumbnail_cso_for(frappe.get_doc("File", doc.name))
		objects.set_cso_status(derived.name, "verified")
		self.assertEqual(frappe.db.get_value(CSO_DOCTYPE, source.name, "status"), "uploaded")

		allowed, reason = thumbnails.can_release_local_thumbnail(frappe.get_doc("File", doc.name))

		self.assertFalse(allowed)
		self.assertIn("source object is uploaded", reason)

	def test_an_unverified_derived_object_blocks_release(self):
		doc = self.image_file(file_name="thumb-gate-derived.png")
		doc.make_thumbnail()
		source = self.cso_of(doc)
		objects.set_cso_status(source.name, "verified")

		allowed, reason = thumbnails.can_release_local_thumbnail(frappe.get_doc("File", doc.name))

		self.assertFalse(allowed)
		self.assertIn("derived thumbnail object is uploaded", reason)

	def test_a_missing_derived_object_blocks_release(self):
		doc = self.image_file(file_name="thumb-gate-none.png")
		frappe.db.set_value("File", doc.name, "thumbnail_url", "/files/thumb-gate-none_small.png")
		objects.set_cso_status(self.cso_of(doc).name, "verified")

		allowed, reason = thumbnails.can_release_local_thumbnail(frappe.get_doc("File", doc.name))

		self.assertFalse(allowed)
		self.assertIn("no derived thumbnail object", reason)

	def test_a_file_without_a_thumbnail_is_not_releasable(self):
		doc = self.image_file(file_name="thumb-gate-absent.png")

		allowed, reason = thumbnails.can_release_local_thumbnail(frappe.get_doc("File", doc.name))

		self.assertFalse(allowed)
		self.assertIn("no thumbnail_url", reason)


class TestDerivedObjectLifecycle(ThumbnailTestCase):
	def test_deleting_the_source_schedules_its_thumbnails(self):
		doc = self.image_file(file_name="thumb-lifecycle.png")
		doc.make_thumbnail()
		source = self.cso_of(doc)
		derived = thumbnails.thumbnail_cso_for(frappe.get_doc("File", doc.name))

		frappe.delete_doc("File", doc.name, force=True, ignore_permissions=True)

		self.assertEqual(frappe.db.get_value(CSO_DOCTYPE, source.name, "status"), "pending_delete")
		self.assertEqual(frappe.db.get_value(CSO_DOCTYPE, derived.name, "status"), "pending_delete")
		# Scheduled, never deleted inline (invariant 4).
		self.assertTrue(self.store.contains(derived))
		self.assertEqual(self.store.deleted, [])

	def test_a_still_referenced_source_keeps_its_thumbnails(self):
		doc = self.image_file(file_name="thumb-shared.png")
		doc.make_thumbnail()
		source = self.cso_of(doc)
		derived = thumbnails.thumbnail_cso_for(frappe.get_doc("File", doc.name))

		sibling = self.track(
			frappe.get_doc(
				{
					"doctype": "File",
					"file_name": "thumb-shared-copy.png",
					"file_url": "/files/thumb-shared-copy.png",
					"is_private": 0,
					"content_hash": doc.content_hash,
					"file_size": doc.file_size,
					"cloud_storage_object": source.name,
				}
			).insert(ignore_permissions=True)
		)
		self.assertTrue(sibling.name)

		frappe.delete_doc("File", doc.name, force=True, ignore_permissions=True)

		self.assertNotEqual(frappe.db.get_value(CSO_DOCTYPE, source.name, "status"), "pending_delete")
		self.assertNotEqual(frappe.db.get_value(CSO_DOCTYPE, derived.name, "status"), "pending_delete")


class TestDerivedThumbnailPrivacy(ThumbnailTestCase):
	"""A19 on the derived path — a thumbnail is a rendering of its source's bytes.

	`is_private = 0` is the public path's entire security boundary, and a thumbnail has no
	File row of its own to carry it: the renderer reads it off the SOURCE row. Every test
	here names one route by which a 300x300 rendering of a private file could reach an
	unauthenticated caller.
	"""

	def can_render(self, url: str) -> bool:
		with http_request(url):
			return PublicFileRenderer(url.lstrip("/")).can_render()

	@staticmethod
	def _unlink(path: str):
		if path and os.path.exists(path):
			os.remove(path)

	# --- the filter itself ------------------------------------------------------------

	def test_a_private_row_with_a_public_thumbnail_url_is_declined(self):
		"""The DB filter, independent of how the row reached that state.

		`frappe.db.set_value` is the point of the test: a fork-era row, a compat patch or an
		operator's UPDATE can all leave `is_private = 1` beside a `/files/...` thumbnail_url,
		and the renderer has to refuse on what the row says now.
		"""
		doc = self.image_file(file_name="thumb-priv-stale.png", is_private=0)
		thumbnail_url = doc.make_thumbnail()
		self.assertTrue(self.can_render(thumbnail_url), "precondition: it serves while the row is public")

		frappe.db.set_value("File", doc.name, "is_private", 1, update_modified=False)

		self.assertFalse(self.can_render(thumbnail_url))

	def test_an_alias_to_a_private_rows_thumbnail_is_declined(self):
		"""The alias branch re-enters `_resolve`, so it must inherit the same filter."""
		from cloud_file_storage.serving import aliases

		doc = self.image_file(file_name="thumb-priv-alias.png", is_private=0)
		thumbnail_url = doc.make_thumbnail()
		aliases.ensure_alias("/files/thumb-priv-alias-old_small.png", thumbnail_url)
		self.addCleanup(self._drop_aliases)
		self.assertTrue(
			self.can_render("/files/thumb-priv-alias-old_small.png"),
			"precondition: the alias resolves while the row is public",
		)

		frappe.db.set_value("File", doc.name, "is_private", 1, update_modified=False)

		self.assertFalse(self.can_render("/files/thumb-priv-alias-old_small.png"))

	@staticmethod
	def _drop_aliases():
		for name in frappe.get_all("Cloud File URL Alias", pluck="name"):
			frappe.delete_doc("Cloud File URL Alias", name, force=True, ignore_permissions=True)

	# --- the flip (A16) ---------------------------------------------------------------

	def test_flipping_to_private_stops_the_thumbnail_being_served(self):
		doc = self.image_file(file_name="thumb-flip-serve.png", is_private=0)
		thumbnail_url = doc.make_thumbnail()
		self.assertTrue(self.can_render(thumbnail_url), "precondition: it serves while public")

		flipped = frappe.get_doc("File", doc.name)
		flipped.is_private = 1
		flipped.save()

		self.assertFalse(self.can_render(thumbnail_url))

	def test_the_flip_rewrites_the_thumbnail_url_to_the_new_prefix(self):
		"""No `/files/...` thumbnail_url survives on a private row — defence in depth."""
		doc = self.image_file(file_name="thumb-flip-url.png", is_private=0)
		self.assertEqual(doc.make_thumbnail(), "/files/thumb-flip-url_small.png")

		flipped = frappe.get_doc("File", doc.name)
		flipped.is_private = 1
		flipped.save()

		self.assertEqual(
			frappe.db.get_value("File", doc.name, "thumbnail_url"),
			"/private/files/thumb-flip-url_small.png",
		)

	def test_flipping_back_to_public_makes_the_thumbnail_servable_again(self):
		"""...and the fix is not "break thumbnails whenever anything moves"."""
		doc = self.image_file(file_name="thumb-flip-back.png", is_private=0)
		doc.make_thumbnail()

		flipped = frappe.get_doc("File", doc.name)
		flipped.is_private = 1
		flipped.save()
		back = frappe.get_doc("File", doc.name)
		back.is_private = 0
		back.save()

		restored = frappe.db.get_value("File", doc.name, "thumbnail_url")
		self.assertEqual(restored, "/files/thumb-flip-back_small.png")
		self.assertTrue(self.can_render(restored))

	def test_the_flip_rewrites_the_thumbnail_url_of_the_rows_it_takes_with_it(self):
		"""`update_existing_file_docs` flips every content_hash sharer by raw SQL.

		Those rows never run their own `handle_is_private_changed`, so nothing else would
		move their thumbnail_url off the public prefix.
		"""
		doc = self.image_file(file_name="thumb-flip-sibling.png", is_private=0)
		doc.make_thumbnail()
		sibling = self.track(
			frappe.get_doc(
				{
					"doctype": "File",
					"file_name": "thumb-flip-sibling-copy.png",
					"file_url": doc.file_url,
					"is_private": 0,
					"content_hash": doc.content_hash,
					"file_size": doc.file_size,
					"thumbnail_url": "/files/thumb-flip-sibling-copy_small.png",
				}
			).insert(ignore_permissions=True)
		)

		flipped = frappe.get_doc("File", doc.name)
		flipped.is_private = 1
		flipped.save()

		self.assertEqual(frappe.db.get_value("File", sibling.name, "is_private"), 1)
		self.assertEqual(
			frappe.db.get_value("File", sibling.name, "thumbnail_url"),
			"/private/files/thumb-flip-sibling-copy_small.png",
		)

	# --- the derived object's own identity ---------------------------------------------

	def test_the_derived_object_of_a_private_source_is_stamped_private(self):
		"""`store_thumbnail` used to stamp every derived object public, whatever it held."""
		doc = self.image_file(file_name="thumb-vis-priv.png", is_private=1)

		doc.make_thumbnail(width=64, height=64)

		derived = thumbnails.thumbnail_cso_for(frappe.get_doc("File", doc.name))
		self.assertIsNotNone(derived)
		self.assertEqual(derived.visibility, "private")

	def test_the_derived_object_of_a_public_source_stays_public(self):
		doc = self.image_file(file_name="thumb-vis-pub.png", is_private=0)

		doc.make_thumbnail(width=64, height=64)

		derived = thumbnails.thumbnail_cso_for(frappe.get_doc("File", doc.name))
		self.assertIsNotNone(derived)
		self.assertEqual(derived.visibility, "public")

	def test_the_flip_restamps_the_derived_objects_visibility(self):
		"""An invariant asserted at creation and abandoned at update is worse than none.

		No disclosure rides on this label — a derived key is `thm/…` whatever it says, and
		every derived byte is presigned — but `store_thumbnail` states that a derived
		object's visibility is its source's, and a flip would make that false one save later.
		"""
		doc = self.image_file(file_name="thumb-restamp.png", is_private=0)
		doc.make_thumbnail(width=64, height=64)
		derived = thumbnails.thumbnail_cso_for(frappe.get_doc("File", doc.name))
		self.assertEqual(derived.visibility, "public", "precondition: a public source, a public label")

		flipped = frappe.get_doc("File", doc.name)
		flipped.is_private = 1
		flipped.save()

		self.assertEqual(frappe.db.get_value(CSO_DOCTYPE, derived.name, "visibility"), "private")

	def test_the_flip_back_restamps_it_again(self):
		doc = self.image_file(file_name="thumb-restamp-back.png", is_private=1)
		doc.make_thumbnail(width=64, height=64)
		derived = thumbnails.thumbnail_cso_for(frappe.get_doc("File", doc.name))
		self.assertEqual(derived.visibility, "private")

		flipped = frappe.get_doc("File", doc.name)
		flipped.is_private = 0
		flipped.save()

		self.assertEqual(frappe.db.get_value(CSO_DOCTYPE, derived.name, "visibility"), "public")

	def test_the_flip_restamps_a_sharers_derived_object_too(self):
		"""`update_existing_file_docs` flips content_hash sharers by raw SQL; their derived
		objects are just as mislabelled and just as invisible to their own handler."""
		doc = self.image_file(file_name="thumb-restamp-sib.png", is_private=0)
		doc.make_thumbnail(width=64, height=64)
		derived = thumbnails.thumbnail_cso_for(frappe.get_doc("File", doc.name))
		sibling = self.track(
			frappe.get_doc(
				{
					"doctype": "File",
					"file_name": "thumb-restamp-sib-copy.png",
					"file_url": doc.file_url,
					"is_private": 0,
					"content_hash": doc.content_hash,
					"file_size": doc.file_size,
					"thumbnail_url": "/files/thumb-restamp-sib-copy_small.png",
					thumbnails.THUMBNAIL_LINK_FIELD: derived.name,
				}
			).insert(ignore_permissions=True)
		)
		frappe.db.set_value("File", doc.name, thumbnails.THUMBNAIL_LINK_FIELD, None, update_modified=False)
		self.assertEqual(
			frappe.db.get_value("File", sibling.name, thumbnails.THUMBNAIL_LINK_FIELD),
			derived.name,
			"precondition: only the SHARER carries the link",
		)

		flipped = frappe.get_doc("File", doc.name)
		flipped.is_private = 1
		flipped.save()

		self.assertEqual(frappe.db.get_value(CSO_DOCTYPE, derived.name, "visibility"), "private")

	def test_a_primary_object_is_never_restamped(self):
		"""Guard 1. A primary object's visibility IS its key; re-labelling one without moving
		its bytes would make the row lie about where they are. That is the flip's own job,
		through `ensure_cso` and a server-side copy.
		"""
		doc = self.image_file(file_name="thumb-restamp-primary.png", is_private=0)
		source = self.cso_of(doc)
		self.assertEqual(source.visibility, "public")

		self.assertFalse(objects.restamp_derived_visibility(source.name, "private"))

		self.assertEqual(frappe.db.get_value(CSO_DOCTYPE, source.name, "visibility"), "public")
		self.assertEqual(frappe.db.get_value(CSO_DOCTYPE, source.name, "s3_key"), source.s3_key)

	def test_a_taken_identity_leaves_the_label_alone(self):
		"""Guard 2. `(content_sha256, visibility, bucket)` is unique; a stale label is a
		documentation defect, a failed flip would be a data defect."""
		doc = self.image_file(file_name="thumb-restamp-clash.png", is_private=0)
		doc.make_thumbnail(width=64, height=64)
		derived = thumbnails.thumbnail_cso_for(frappe.get_doc("File", doc.name))
		# Somebody else already owns (these bytes, private): the thumbnail's own bytes stored
		# as a private object in their own right.
		occupier = objects.ensure_cso(
			content_sha256=derived.content_sha256,
			content_hash_md5=derived.content_hash_md5,
			file_size=derived.file_size,
			visibility="private",
		)
		self.assertNotEqual(occupier.name, derived.name)

		self.assertFalse(objects.restamp_derived_visibility(derived.name, "private"))

		self.assertEqual(frappe.db.get_value(CSO_DOCTYPE, derived.name, "visibility"), "public")

	def test_an_unknown_object_is_a_no_op(self):
		self.assertFalse(objects.restamp_derived_visibility(None, "private"))
		self.assertFalse(objects.restamp_derived_visibility("no-such-object", "private"))

	# --- the local copy (nginx serves `sites/<site>/public/$uri` directly) --------------

	def test_a_private_thumbnail_is_not_written_into_the_public_tree(self):
		"""Core writes every thumbnail under `public/` (`file.py:463`) even when the URL
		says `/private/files/...`, which nginx then serves to anyone. Core's own
		`delete_file` looks for it in `private/files/` (`file_manager.py:317-322`); this
		app follows the delete side.
		"""
		from cloud_file_storage.tests.utils import set_mode

		set_mode("DUAL_WRITE")
		doc = self.image_file(file_name="thumb-local-priv.png", is_private=1)

		thumbnail_url = doc.make_thumbnail()

		self.assertEqual(thumbnail_url, "/private/files/thumb-local-priv_small.png")
		public_path = frappe.get_site_path("public", thumbnail_url.lstrip("/"))
		private_path = frappe.get_site_path("private", "files", "thumb-local-priv_small.png")
		self.addCleanup(self._unlink, public_path)
		self.addCleanup(self._unlink, private_path)
		self.assertTrue(os.path.exists(private_path), "the local copy still has to exist somewhere")
		self.assertFalse(os.path.exists(public_path))

	def test_the_flip_moves_the_local_thumbnail_out_of_the_public_tree(self):
		from cloud_file_storage.tests.utils import set_mode

		set_mode("DUAL_WRITE")
		doc = self.image_file(file_name="thumb-local-flip.png", is_private=0)
		thumbnail_url = doc.make_thumbnail()
		public_path = frappe.get_site_path("public", thumbnail_url.lstrip("/"))
		private_path = frappe.get_site_path("private", "files", "thumb-local-flip_small.png")
		self.addCleanup(self._unlink, public_path)
		self.addCleanup(self._unlink, private_path)
		self.assertTrue(os.path.exists(public_path), "precondition: DUAL_WRITE keeps a local copy")

		flipped = frappe.get_doc("File", doc.name)
		flipped.is_private = 1
		flipped.save()

		self.assertFalse(os.path.exists(public_path))
		self.assertTrue(os.path.exists(private_path), "the bytes are moved, never deleted")
