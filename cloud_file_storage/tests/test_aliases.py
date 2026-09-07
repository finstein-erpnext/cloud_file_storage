"""T-ALIAS — the `Cloud File URL Alias` doctype itself (A11).

Consumption by the two serving paths lives in `test_serving_private.py` and
`test_serving_public.py`; this module is about the record: its schema constraints, its
deterministic naming, and the rule that an alias can only ever point at a canonical URL.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from cloud_file_storage.cloud_file_storage.doctype.cloud_file_url_alias.cloud_file_url_alias import (
	URL_HASH_INDEX,
	hash_url,
)
from cloud_file_storage.serving import aliases

ALIAS_DOCTYPE = "Cloud File URL Alias"


class TestUrlAliasSchema(FrappeTestCase):
	def tearDown(self):
		for name in frappe.get_all(ALIAS_DOCTYPE, pluck="name"):
			frappe.delete_doc(ALIAS_DOCTYPE, name, force=True, ignore_permissions=True)
		super().tearDown()

	def test_old_url_holds_500_characters(self):
		"""A11 — a private prefix plus a long attachment name overruns Data's 140 default."""
		meta = frappe.get_meta(ALIAS_DOCTYPE)
		self.assertEqual(meta.get_field("old_url").length, 500)
		self.assertEqual(meta.get_field("new_url").length, 500)

	def test_url_hash_is_data_40_and_unique(self):
		field = frappe.get_meta(ALIAS_DOCTYPE).get_field("url_hash")
		self.assertEqual(field.fieldtype, "Data")
		self.assertEqual(field.length, 40)
		self.assertTrue(field.unique)

	def test_the_unique_index_really_exists_in_the_database(self):
		"""The DocField flag is a promise; this is the table."""
		indexes = frappe.db.sql(f"SHOW INDEX FROM `tab{ALIAS_DOCTYPE}`", as_dict=True)
		matching = [row for row in indexes if row["Key_name"] == URL_HASH_INDEX]
		self.assertTrue(matching, f"no {URL_HASH_INDEX} index on tab{ALIAS_DOCTYPE}")
		self.assertEqual(matching[0]["Non_unique"], 0)
		self.assertEqual(matching[0]["Column_name"], "url_hash")

	def test_a_long_old_url_round_trips(self):
		long_url = "/private/files/" + ("a" * 400) + ".pdf"
		aliases.ensure_alias(long_url, "/private/files/short.pdf")

		self.assertEqual(frappe.db.get_value(ALIAS_DOCTYPE, hash_url(long_url), "old_url"), long_url)

	def test_the_hash_is_the_name_so_a_rerun_updates_rather_than_duplicates(self):
		first = aliases.ensure_alias("/files/a.txt", "/files/b.txt", reason="conflict")
		second = aliases.ensure_alias("/files/a.txt", "/files/c.txt", reason="relink")

		self.assertEqual(first, second)
		self.assertEqual(first, hash_url("/files/a.txt"))
		self.assertEqual(frappe.db.count(ALIAS_DOCTYPE, {"url_hash": hash_url("/files/a.txt")}), 1)
		self.assertEqual(frappe.db.get_value(ALIAS_DOCTYPE, first, "new_url"), "/files/c.txt")

	def test_a_non_canonical_target_is_refused(self):
		"""Invariant 2 — an alias must not launder a raw bucket URL back into the site."""
		for target in ("https://bucket.s3.amazonaws.com/key", "/api/method/legacy?key=x", "files/a.txt"):
			with self.subTest(target=target), self.assertRaises(frappe.ValidationError):
				frappe.get_doc(
					{"doctype": ALIAS_DOCTYPE, "old_url": "/files/old.txt", "new_url": target}
				).insert(ignore_permissions=True)

	def test_a_canonical_target_is_accepted(self):
		"""So the refusal above is about the shape, not about inserting at all."""
		for target in ("/files/a.txt", "/private/files/b.txt"):
			with self.subTest(target=target):
				doc = frappe.get_doc(
					{
						"doctype": ALIAS_DOCTYPE,
						"old_url": f"/files/old-{target.count('/')}.txt",
						"new_url": target,
					}
				).insert(ignore_permissions=True)
				self.assertEqual(doc.new_url, target)

	def test_a_self_referential_alias_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(
				{"doctype": ALIAS_DOCTYPE, "old_url": "/files/same.txt", "new_url": "/files/same.txt"}
			).insert(ignore_permissions=True)

	def test_ensure_alias_ignores_a_no_op(self):
		self.assertIsNone(aliases.ensure_alias("/files/x.txt", "/files/x.txt"))
		self.assertIsNone(aliases.ensure_alias("", "/files/x.txt"))
		self.assertIsNone(aliases.ensure_alias("/files/x.txt", ""))
		self.assertEqual(frappe.db.count(ALIAS_DOCTYPE), 0)

	def test_resolve_returns_none_without_an_alias(self):
		self.assertIsNone(aliases.resolve("/files/never-aliased.txt"))
		self.assertIsNone(aliases.resolve(None))

	def test_resolve_follows_a_chain(self):
		aliases.ensure_alias("/files/one.txt", "/files/two.txt")
		aliases.ensure_alias("/files/two.txt", "/files/three.txt")

		self.assertEqual(aliases.resolve("/files/one.txt"), "/files/three.txt")

	def test_the_redirect_code_is_302(self):
		"""A11 states it and both serving paths import it from here."""
		self.assertEqual(aliases.ALIAS_REDIRECT_CODE, 302)
