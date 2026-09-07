# Copyright (c) 2026, Finstein and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from cloud_file_storage.migration import names

#: Object lifecycle (migration-engine.md §5.3).
OBJECT_STATUSES = (
	"Pending",
	"Uploading",
	"Uploaded",
	"Verifying",
	"Verified",
	"DedupReused",
	"CleanupEligible",
	"Quarantined",
	"CleanedUp",
	"Adopted",
	"Failed",
	"Conflict",
	"Skipped",
)

#: Statuses that still need work from the engine. A31's finalize-CAS fires only when a
#: batch has none of these left, so this tuple is the definition of "residue".
NON_TERMINAL_UPLOAD_STATUSES = ("Pending", "Uploading")

#: Statuses whose bytes are known to be in the bucket and whose refs may therefore be
#: linked and, later, cleaned up.
UPLOADED_STATUSES = ("Uploaded", "DedupReused", "Adopted")

#: The nine analyzer classes (migration-engine.md §3.4).
CLASSIFICATIONS = (
	"healthy_unique",
	"shared_url",
	"shared_content",
	"conflicting_url",
	"missing_physical",
	"orphan_physical",
	"remote_url",
	"legacy_fork",
	"corrupt_metadata",
)


class CloudMigrationObject(Document):
	def autoname(self):
		"""A33 — `sha1(campaign‖url_hash)`.

		Deterministic so a re-run of an analyzer page collides on the primary key instead of
		inserting a duplicate identity. See `migration/names.py` for why the redundant
		`(campaign, url_hash)` unique index of migration-engine.md §2.3 is not created.
		"""
		self.name = names.object_name(self.campaign, self.url_hash)


def on_doctype_update():
	"""Composite indexes only, plus the A14 link index the JSON's `search_index` creates.

	Deliberately no standalone index on `campaign`, `batch`, `status` or `classification`:
	each is the leading column of one of the composites below, so a standalone copy would
	answer no query the composite cannot, while costing index bytes on ~1.2M rows that F4
	has to account for. There is no query in this engine that filters `status` or
	`classification` without also filtering `campaign` or `batch`.
	"""
	frappe.db.add_index("Cloud Migration Object", ["campaign", "status"])
	frappe.db.add_index("Cloud Migration Object", ["batch", "status"])
	frappe.db.add_index("Cloud Migration Object", ["campaign", "classification"])
	frappe.db.add_index("Cloud Migration Object", ["sha256"])
