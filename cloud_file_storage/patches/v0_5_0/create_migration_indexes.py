"""Composite indexes for the migration tables (design §2.2–§2.5, A14).

Idempotent: `frappe.db.add_index` is a no-op when the index already exists. Guarded on
table existence so the patch is safe on a site whose DocType sync has not run yet — and so
`install.ensure_migration_indexes` can call it on a fresh install too.

`add_index` commits before its DDL, which is why this lives in a patch and in
`after_install`/`after_migrate` rather than inside any locked transition (A24).
"""

import frappe

INDEXES = {
	"Cloud Migration Campaign": [["status"]],
	"Cloud Migration Batch": [["campaign", "status"], ["campaign", "batch_no"], ["status", "heartbeat_at"]],
	"Cloud Migration Object": [
		["campaign", "status"],
		["batch", "status"],
		["campaign", "classification"],
		["sha256"],
	],
	"Cloud Migration File Ref": [["campaign", "linked"]],
	"Cloud Migration Conflict": [["campaign", "status"], ["campaign", "conflict_type"]],
	"Cloud Storage Audit Log": [["campaign", "action"]],
}


def execute():
	for doctype, index_sets in INDEXES.items():
		if not frappe.db.table_exists(doctype):
			continue
		for fields in index_sets:
			frappe.db.add_index(doctype, fields)
