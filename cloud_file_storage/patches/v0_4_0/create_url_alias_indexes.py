"""Indexes for the P3 serving schema: the alias lookup and the derived-object lookup.

Both are created by `on_doctype_update` when the DocType syncs, but only on a site whose
sync actually ran the hook — a site that carried a hand-made table, or one whose earlier
sync predates these fields, would keep serving from a full scan. Cheap, idempotent, and
re-runnable, so it also stands as the schema change's patch (docs/INVARIANTS.md invariant 9).
"""

import frappe

from cloud_file_storage.cloud_file_storage.doctype.cloud_file_url_alias.cloud_file_url_alias import (
	URL_HASH_INDEX,
)

ALIAS_DOCTYPE = "Cloud File URL Alias"
CSO_DOCTYPE = "Cloud Storage Object"
DERIVED_INDEX = "derived_of_index"


def execute():
	if frappe.db.table_exists(ALIAS_DOCTYPE):
		# A11: `url_hash` is the indexable stand-in for a 500-char `old_url`, and the index
		# is unique because two aliases for one old URL would make resolution ambiguous.
		frappe.db.add_unique(ALIAS_DOCTYPE, ["url_hash"], constraint_name=URL_HASH_INDEX)

	if frappe.db.table_exists(CSO_DOCTYPE) and frappe.db.has_column(CSO_DOCTYPE, "derived_of"):
		# The public renderer looks thumbnails up by `derived_of` on every `_small.` miss.
		frappe.db.add_index(CSO_DOCTYPE, ["derived_of"], index_name=DERIVED_INDEX)
