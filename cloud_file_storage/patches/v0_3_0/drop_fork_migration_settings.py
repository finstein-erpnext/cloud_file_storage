"""Drop the two fork settings that died with the fork's bulk-upload endpoint.

`timeout_for_migration_job` and the `migrate_existing_files` button belonged to
`controller.run_migrate_existing_files` — an unbatched, uncoordinated bulk upload with no
campaign, no CAS, no heartbeat and no verification. It is gone (the migration engine in P5
is its supervised replacement), so the fields go with it; neither appears in the frozen A12
schema.

Removing a field from a Single leaves its row behind in `tabSingles`, where it would keep
being read back into the document. This deletes those rows.
"""

import frappe

SETTINGS_DOCTYPE = "Cloud Storage Settings"
REMOVED_FIELDS = ("timeout_for_migration_job", "migrate_existing_files")


def execute():
	if not frappe.db.table_exists("Singles"):
		return

	for fieldname in REMOVED_FIELDS:
		frappe.db.delete("Singles", {"doctype": SETTINGS_DOCTYPE, "field": fieldname})

	frappe.clear_cache(doctype=SETTINGS_DOCTYPE)
