# Copyright (c) 2026, Finstein and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

#: Batch lifecycle (migration-engine.md §5.2, extended with the CLEANUP arm that §6.5 runs
#: "through the same dispatcher"). Every transition is a CAS UPDATE in `engine`, never a
#: `.save()`: a claim that is not `affected_rows == 1` must lose.
BATCH_STATUSES = (
	"Pending",
	"UploadDispatched",
	"Uploading",
	"Uploaded",
	"VerifyDispatched",
	"Verifying",
	"Verified",
	"CleanupDispatched",
	"Cleaning",
	"Cleaned",
	"Failed",
	"Stalled",
)

#: Batch statuses that mean a worker is supposed to be holding this batch right now. The
#: dispatcher's stale sweep looks only at these.
IN_FLIGHT_STATUSES = (
	"UploadDispatched",
	"Uploading",
	"VerifyDispatched",
	"Verifying",
	"CleanupDispatched",
	"Cleaning",
)


class CloudMigrationBatch(Document):
	pass


def on_doctype_update():
	frappe.db.add_index("Cloud Migration Batch", ["campaign", "status"])
	frappe.db.add_index("Cloud Migration Batch", ["campaign", "batch_no"])
	frappe.db.add_index("Cloud Migration Batch", ["status", "heartbeat_at"])
