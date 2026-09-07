# Copyright (c) 2026, Finstein and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from cloud_file_storage.migration import names


class CloudMigrationFileRef(Document):
	def autoname(self):
		"""A33 — `sha1(campaign‖file)`. One row per (campaign, File), by construction."""
		self.name = names.file_ref_name(self.campaign, self.file)


def on_doctype_update():
	"""`(campaign, linked)` drives the per-campaign link sweep and covers `campaign` alone.

	`migration_object` and `file` keep their own indexes (JSON `search_index`): the A26
	guarded UPDATE joins on the first and conflict/relink work looks up by the second.
	"""
	frappe.db.add_index("Cloud Migration File Ref", ["campaign", "linked"])
