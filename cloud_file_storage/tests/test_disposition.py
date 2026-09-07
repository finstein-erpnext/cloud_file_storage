"""T-HTML — Content-Disposition policy, and Policy B in particular.

Policy B is a release-behaviour invariant (PLAN.md §A and §C): `.html/.htm/.svg/.xml` are
always served as an attachment, never inline, never through a CDN. These are unit tests of
the decision; `test_serving_private.py` and `test_serving_public.py` prove the decision
actually reaches the signed URL on both paths.
"""

from cloud_file_storage.serving import disposition
from cloud_file_storage.tests.utils import CloudStorageTestCase, set_mode

POLICY_B_EXTENSIONS = (".html", ".htm", ".svg", ".xml")


class TestPolicyB(CloudStorageTestCase):
	def test_the_forced_set_is_exactly_the_four_extensions(self):
		self.assertEqual(disposition.FORCED_ATTACHMENT_EXTENSIONS, frozenset(POLICY_B_EXTENSIONS))

	def test_every_forced_extension_is_an_attachment(self):
		for extension in POLICY_B_EXTENSIONS:
			with self.subTest(extension=extension):
				self.assertTrue(disposition.is_forced_attachment(f"/files/report{extension}"))
				self.assertEqual(disposition.disposition_for(f"/files/report{extension}"), "attachment")

	def test_policy_b_beats_the_configured_inline_prefixes(self):
		"""An operator cannot opt an HTML file back into inline rendering."""
		set_mode("S3_ONLY", inline_mimetype_prefixes="text/\nimage/\napplication/")

		for extension, mime in (
			(".html", "text/html"),
			(".htm", "text/html"),
			(".svg", "image/svg+xml"),
			(".xml", "application/xml"),
		):
			with self.subTest(extension=extension):
				self.assertEqual(disposition.disposition_for(f"/files/x{extension}", mime), "attachment")

	def test_case_and_query_strings_do_not_evade_the_policy(self):
		self.assertTrue(disposition.is_forced_attachment("/files/REPORT.HTML"))
		self.assertTrue(disposition.is_forced_attachment("/files/report.Html?download=1"))
		self.assertTrue(disposition.is_forced_attachment("/files/report.svg#frag"))

	def test_forced_types_are_never_cdn_eligible(self):
		for extension in POLICY_B_EXTENSIONS:
			with self.subTest(extension=extension):
				self.assertFalse(disposition.may_use_cdn(f"/files/x{extension}"))

	def test_ordinary_types_are_cdn_eligible(self):
		"""So the exclusion above is a real exclusion, not a function that returns False."""
		for name in ("/files/photo.png", "/files/manual.pdf", "/files/data.csv"):
			with self.subTest(name=name):
				self.assertTrue(disposition.may_use_cdn(name))


class TestInlinePolicy(CloudStorageTestCase):
	def test_configured_prefixes_are_served_inline(self):
		set_mode("S3_ONLY", inline_mimetype_prefixes="image/\napplication/pdf")

		self.assertEqual(disposition.disposition_for("/files/photo.png"), "inline")
		self.assertEqual(disposition.disposition_for("/files/manual.pdf"), "inline")

	def test_anything_else_is_an_attachment(self):
		set_mode("S3_ONLY", inline_mimetype_prefixes="image/\napplication/pdf")

		self.assertEqual(disposition.disposition_for("/files/sheet.xlsx"), "attachment")
		self.assertEqual(disposition.disposition_for("/files/notes.txt"), "attachment")
		self.assertEqual(disposition.disposition_for("/files/archive.zip"), "attachment")

	def test_an_empty_prefix_list_means_everything_is_an_attachment(self):
		set_mode("S3_ONLY", inline_mimetype_prefixes="")

		self.assertEqual(disposition.disposition_for("/files/photo.png"), "attachment")
		self.assertEqual(disposition.inline_prefixes(), ())

	def test_an_unguessable_type_is_an_attachment(self):
		self.assertEqual(disposition.disposition_for("/files/mystery"), "attachment")
		self.assertEqual(disposition.disposition_for(None), "attachment")

	def test_an_explicit_mime_type_wins_over_the_extension(self):
		set_mode("S3_ONLY", inline_mimetype_prefixes="image/")

		self.assertEqual(disposition.disposition_for("/files/photo.bin", "image/png"), "inline")
		self.assertEqual(
			disposition.disposition_for("/files/photo.png", "application/octet-stream"), "attachment"
		)
