"""T-RELINK and the rest of the conflict queue (design §7).

`relink` is the only flow in the engine that renames a healthy local URL, so it carries the
most weight here: the row that keeps the URL must keep its bytes untouched, every other row
must get its **own copy** (never a move), and each rewritten URL must arrive with an alias,
a corrected parent field and an audit row — because a `file_url` lives in business columns
that integrations read, and a File row rewritten without its parent is a broken document.
"""

import os

import frappe

from cloud_file_storage.migration import aliases, conflicts
from cloud_file_storage.serving import aliases as serving_aliases
from cloud_file_storage.tests.migration_utils import MigrationTestCase

OBJECT_DOCTYPE = "Cloud Migration Object"
REF_DOCTYPE = "Cloud Migration File Ref"
ALIAS_DOCTYPE = "Cloud File URL Alias"


class ConflictTestCase(MigrationTestCase):
	def conflicting_pair(self, *, file_name="relink-source.txt", content=b"the original bytes"):
		"""One URL, two File rows, two different byte expectations."""
		doc = self.local_file(file_name=file_name, content=content)
		sibling = self.url_sibling(doc, file_name="relink-sibling.txt")
		frappe.db.set_value("File", doc.name, "content_hash", "a" * 32, update_modified=False)
		frappe.db.set_value("File", sibling.name, "content_hash", "b" * 32, update_modified=False)
		frappe.db.commit()

		campaign = self.campaign()
		self.analyze(campaign)
		obj = self.object_for(campaign, doc.file_url)
		self.assertEqual(obj.classification, "conflicting_url")

		# Filtered to THIS object: the scratch site carries File rows from every other suite,
		# and taking `[0]` would hand the test somebody else's conflict.
		conflict = next(
			row
			for row in self.conflicts_of(campaign, conflict_type="conflicting_url")
			if row.migration_object == obj.name
		)
		return doc, sibling, campaign, obj, conflict


class TestRelink(ConflictTestCase):
	def test_the_kept_row_is_left_completely_alone(self):
		doc, sibling, campaign, obj, conflict = self.conflicting_pair()
		original_url = doc.file_url
		original_path = doc._canonical_local_path()
		with open(original_path, "rb") as handle:
			original_bytes = handle.read()

		conflicts.relink(conflict.name, keep_file=doc.name)

		self.assertEqual(frappe.db.get_value("File", doc.name, "file_url"), original_url)
		self.assertTrue(os.path.exists(original_path))
		with open(original_path, "rb") as handle:
			self.assertEqual(handle.read(), original_bytes)

	def test_the_other_row_gets_its_own_physical_copy(self):
		doc, sibling, campaign, obj, conflict = self.conflicting_pair()
		original_path = doc._canonical_local_path()

		result = conflicts.relink(conflict.name, keep_file=doc.name)

		self.assertEqual(len(result["split_off"]), 1)
		new_url = frappe.db.get_value("File", sibling.name, "file_url")
		self.assertNotEqual(new_url, doc.file_url, "the sibling still shares the disputed URL")

		from cloud_file_storage.migration import analyzer

		new_path = analyzer.local_disk_path(new_url)
		self.addCleanup(lambda: os.path.exists(new_path) and os.remove(new_path))
		self.assertTrue(os.path.exists(new_path), "the copy was not made")
		self.assertTrue(os.path.exists(original_path), "the bytes were MOVED, not copied")
		with open(new_path, "rb") as handle, open(original_path, "rb") as original:
			self.assertEqual(handle.read(), original.read())

	def test_the_old_url_keeps_resolving_through_an_alias(self):
		doc, sibling, campaign, obj, conflict = self.conflicting_pair()
		old_url = sibling.file_url

		conflicts.relink(conflict.name, keep_file=doc.name)

		new_url = frappe.db.get_value("File", sibling.name, "file_url")
		self.addCleanup(lambda: os.path.exists(_path(new_url)) and os.remove(_path(new_url)))
		# The alias maps the *sibling's* old URL, which is the same string the kept row still
		# uses, so resolution has to survive that overlap rather than break on it.
		self.assertEqual(serving_aliases.resolve(old_url), new_url)
		self.assertEqual(serving_aliases.ALIAS_REDIRECT_CODE, 302)

	def test_the_split_row_becomes_its_own_migration_object(self):
		doc, sibling, campaign, obj, conflict = self.conflicting_pair()
		result = conflicts.relink(conflict.name, keep_file=doc.name)
		new_url = frappe.db.get_value("File", sibling.name, "file_url")
		self.addCleanup(lambda: os.path.exists(_path(new_url)) and os.remove(_path(new_url)))

		new_object = result["split_off"][0]["object"]
		row = frappe.db.get_value(OBJECT_DOCTYPE, new_object, "*", as_dict=True)
		self.assertEqual(row.status, "Pending")
		self.assertEqual(row.classification, "healthy_unique")
		self.assertEqual(row.file_url, new_url)

		ref = frappe.db.get_value(
			REF_DOCTYPE,
			{"campaign": campaign, "file": sibling.name},
			["migration_object", "url_rewritten"],
			as_dict=True,
		)
		self.assertEqual(ref.migration_object, new_object)
		self.assertEqual(ref.url_rewritten, 1)

	def test_the_rewrite_is_audited(self):
		doc, sibling, campaign, obj, conflict = self.conflicting_pair()
		conflicts.relink(conflict.name, keep_file=doc.name)
		new_url = frappe.db.get_value("File", sibling.name, "file_url")
		self.addCleanup(lambda: os.path.exists(_path(new_url)) and os.remove(_path(new_url)))

		actions = self.audit_actions(campaign)
		self.assertIn("url_rewrite", actions)
		self.assertIn("conflict_action", actions)

	def test_relink_refuses_a_file_that_is_not_a_reference(self):
		doc, sibling, campaign, obj, conflict = self.conflicting_pair()
		with self.assertRaises(frappe.ValidationError):
			conflicts.relink(conflict.name, keep_file="not-a-file")

	def test_relink_refuses_when_the_original_bytes_are_gone(self):
		doc, sibling, campaign, obj, conflict = self.conflicting_pair()
		os.remove(doc._canonical_local_path())
		with self.assertRaises(frappe.ValidationError):
			conflicts.relink(conflict.name, keep_file=doc.name)


class TestParentFieldConsistency(ConflictTestCase):
	"""Contract #18 — a `file_url` lives in business columns, not only on the File row."""

	def test_an_attach_image_field_holding_the_old_url_is_updated(self):
		"""`User.user_image` is a real Attach Image and is also the doctype's `image_field`."""
		from cloud_file_storage.tests.request_utils import make_user

		doc = self.local_file(file_name="parent-attach.png", content=b"attached bytes")
		user = make_user(self, "cfs-parent-probe@example.com")
		frappe.db.set_value("User", user.name, "user_image", doc.file_url, update_modified=False)
		frappe.db.set_value(
			"File",
			doc.name,
			{
				"attached_to_doctype": "User",
				"attached_to_name": user.name,
				"attached_to_field": "user_image",
			},
			update_modified=False,
		)
		frappe.db.commit()

		new_url = "/private/files/parent-attach-renamed.png"
		result = aliases.rewrite_file_url(doc.name, doc.file_url, new_url, reason="relink")

		self.assertTrue(result["rewritten"])
		self.assertEqual(frappe.db.get_value("File", doc.name, "file_url"), new_url)
		self.assertEqual(
			frappe.db.get_value("User", user.name, "user_image"),
			new_url,
			"the parent field still points at a URL that no longer resolves",
		)
		self.assertEqual(
			[entry["field"] for entry in result["parents_updated"]],
			["user_image"],
		)

	def test_a_field_that_is_not_an_attachment_is_left_alone(self):
		"""Meta validation: writing a URL into a rich-text body would corrupt the document."""
		doc = self.local_file(file_name="parent-nonattach.txt", content=b"not an attachment")
		note = frappe.get_doc({"doctype": "Note", "title": "cfs-parent-probe"}).insert(
			ignore_permissions=True
		)
		self.addCleanup(frappe.delete_doc, "Note", note.name, force=True, ignore_permissions=True)
		frappe.db.set_value("Note", note.name, "content", doc.file_url, update_modified=False)
		frappe.db.set_value(
			"File",
			doc.name,
			{
				"attached_to_doctype": "Note",
				"attached_to_name": note.name,
				"attached_to_field": "content",
			},
			update_modified=False,
		)
		frappe.db.commit()

		result = aliases.rewrite_file_url(
			doc.name, doc.file_url, "/private/files/parent-nonattach-renamed.txt", reason="relink"
		)
		self.assertTrue(result["rewritten"])
		self.assertEqual(result["parents_updated"], [])
		self.assertEqual(frappe.db.get_value("Note", note.name, "content"), doc.file_url)

	def test_a_reason_the_alias_doctype_does_not_know_is_refused(self):
		doc = self.local_file(file_name="parent-badreason.txt")
		with self.assertRaises(frappe.ValidationError):
			aliases.rewrite_file_url(doc.name, doc.file_url, "/private/files/x.txt", reason="invented_reason")

	def test_a_rewrite_to_a_non_canonical_url_is_refused(self):
		doc = self.local_file(file_name="parent-noncanonical.txt")
		with self.assertRaises(frappe.ValidationError):
			aliases.rewrite_file_url(doc.name, doc.file_url, "https://example.invalid/x.txt", reason="relink")

	def test_a_rewrite_is_dropped_when_the_runtime_moved_the_row_first(self):
		doc = self.local_file(file_name="parent-raced.txt")
		result = aliases.rewrite_file_url(
			doc.name, "/private/files/some-other-url.txt", "/private/files/new.txt", reason="relink"
		)
		self.assertFalse(result["rewritten"])
		self.assertEqual(result["reason"], "file_url changed under us")


class TestConflictActions(ConflictTestCase):
	def test_retry_reopens_the_object_with_a_clean_counter(self):
		doc, sibling, campaign, obj, conflict = self.conflicting_pair()
		frappe.db.set_value(OBJECT_DOCTYPE, obj.name, "attempt_count", 4, update_modified=False)
		frappe.db.commit()

		conflicts.retry(conflict.name)

		row = frappe.db.get_value(OBJECT_DOCTYPE, obj.name, ["status", "attempt_count"], as_dict=True)
		self.assertEqual(row.status, "Pending")
		self.assertEqual(row.attempt_count, 0)
		self.assertEqual(frappe.db.get_value("Cloud Migration Conflict", conflict.name, "status"), "Retrying")

	def test_skip_leaves_the_file_local_and_says_so(self):
		doc, sibling, campaign, obj, conflict = self.conflicting_pair()
		conflicts.skip(conflict.name, note="not worth migrating")

		row = frappe.db.get_value(OBJECT_DOCTYPE, obj.name, ["status", "skip_reason"], as_dict=True)
		self.assertEqual(row.status, "Skipped")
		self.assertEqual(row.skip_reason, "operator_skipped")
		self.assertTrue(self.local_exists(doc))

	def test_mark_resolved_records_who_and_when(self):
		doc, sibling, campaign, obj, conflict = self.conflicting_pair()
		conflicts.mark_resolved(conflict.name, note="handled out of band")

		row = frappe.db.get_value(
			"Cloud Migration Conflict", conflict.name, ["status", "resolved_by", "resolved_at"], as_dict=True
		)
		self.assertEqual(row.status, "Resolved")
		self.assertEqual(row.resolved_by, "Administrator")
		self.assertIsNotNone(row.resolved_at)

	def test_resolving_a_privacy_conflict_makes_the_row_consistent(self):
		"""A19 — the operator declares the visibility, and both halves are made to agree."""
		doc = self.local_file(file_name="privacy-resolve.txt", content=b"whose bytes")
		frappe.db.set_value("File", doc.name, "is_private", 0, update_modified=False)
		frappe.db.commit()

		campaign = self.campaign()
		self.analyze(campaign)
		conflict = self.conflicts_of(campaign, conflict_type="ambiguous_privacy")[0]

		conflicts.resolve_privacy(conflict.name, is_private=1)

		self.assertEqual(frappe.db.get_value("File", doc.name, "is_private"), 1)
		obj = self.object_for(campaign, doc.file_url)
		self.assertEqual(obj.privacy_mismatch, 0)
		self.assertEqual(obj.status, "Pending", "the object must be able to migrate now")

	def test_resolve_privacy_refuses_the_wrong_conflict_type(self):
		doc, sibling, campaign, obj, conflict = self.conflicting_pair()
		with self.assertRaises(frappe.ValidationError):
			conflicts.resolve_privacy(conflict.name, is_private=1)


def _path(url: str) -> str:
	from cloud_file_storage.migration import analyzer

	return analyzer.local_disk_path(url) or ""
