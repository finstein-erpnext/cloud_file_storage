# Copyright (c) 2026, Finstein and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

AUDIT_ACTIONS = (
	"local_quarantine",
	"local_delete",
	"quarantine_purge",
	"url_rewrite",
	"file_link",
	"remote_adopt",
	"conflict_action",
	"campaign_transition",
	"link_skipped",
	"cleanup_refused",
)


class CloudStorageAuditLog(Document):
	"""Append-only by permission AND by controller.

	The DocType grants no write and no delete role, which stops the Desk and the REST API.
	`on_update` closes the remaining hole — `frappe.get_doc(...).save(ignore_permissions=True)`
	from server code — so an audit row cannot be edited after the fact by the same code that
	is allowed to write one.
	"""

	def on_update(self):
		if not self.is_new() and self.get_doc_before_save():
			frappe.throw(_("Audit log rows are append-only."), title=_("Immutable Record"))


def on_doctype_update():
	frappe.db.add_index("Cloud Storage Audit Log", ["campaign", "action"])
