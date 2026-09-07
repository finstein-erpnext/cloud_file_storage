# Copyright (c) 2026, Finstein and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

CONFLICT_TYPES = (
	"conflicting_url",
	"checksum_mismatch",
	"checksum_unstable",
	"missing_physical",
	"corrupt_metadata",
	"ambiguous_privacy",
	"upload_failed",
	"verify_failed",
	"adoption_failed",
	"cleanup_blocked",
)

#: A19 — a privacy mismatch halts the object until an operator decides the true visibility.
#: The other Blockers are the ones where continuing would mean guessing at bytes.
BLOCKER_TYPES = (
	"conflicting_url",
	"checksum_mismatch",
	"checksum_unstable",
	"missing_physical",
	"ambiguous_privacy",
	"adoption_failed",
)


class CloudMigrationConflict(Document):
	pass


def on_doctype_update():
	frappe.db.add_index("Cloud Migration Conflict", ["campaign", "status"])
	frappe.db.add_index("Cloud Migration Conflict", ["campaign", "conflict_type"])
