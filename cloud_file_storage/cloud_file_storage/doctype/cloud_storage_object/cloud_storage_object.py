# Copyright (c) 2026, Finstein and contributors
# For license information, please see license.txt

import uuid

import frappe
from frappe import _
from frappe.model.document import Document

#: A7 — the frozen status set. Any code outside this list is a bug, not a new state.
STATUSES = (
	"pending_upload",
	"uploaded",
	"verified",
	"failed",
	"orphaned",
	"pending_delete",
	"deleted",
	"legacy_unverified",
)

#: Statuses that mean "the bytes are in the bucket right now" for serving/read purposes.
#: `pending_delete` is included per A16: an object awaiting GC is still servable.
SERVABLE_STATUSES = ("uploaded", "verified", "legacy_unverified", "pending_delete")

#: A25 — the upload gate is positive: upload unless the object is already known present.
PRESENT_STATUSES = ("uploaded", "verified", "legacy_unverified")

UNIQUE_IDENTITY_CONSTRAINT = "unique_cso_identity"
GC_SCAN_INDEX = "status_deletion_scheduled_at_index"


class CloudStorageObject(Document):
	def autoname(self):
		# v15 has no `autoname: UUID` naming rule (naming.py:275-280), so identity lives in
		# the unique indexes and the name stays an opaque, immutable UUID (ADR-12).
		self.name = uuid.uuid4().hex

	def validate(self):
		self.validate_identity()
		self.validate_status()

	def validate_identity(self):
		# Adopted legacy rows have not been hashed yet (A15): they are the only rows allowed
		# to exist without a content hash, and they may never be promoted without one.
		if self.status == "legacy_unverified":
			return

		missing = [
			label
			for label, value in (
				(_("Content SHA256"), self.content_sha256),
				(_("Content Hash (MD5)"), self.content_hash_md5),
			)
			if not value
		]
		if missing:
			frappe.throw(
				_("{0} is required unless the object is legacy_unverified.").format(", ".join(missing)),
				title=_("Incomplete Cloud Storage Object"),
			)

	def validate_status(self):
		if self.status not in STATUSES:
			frappe.throw(_("{0} is not a valid Cloud Storage Object status.").format(self.status))


def on_doctype_update():
	"""Indexes that carry the identity and GC guarantees.

	`add_unique`/`add_index` commit before the DDL, which is why this runs from
	`on_doctype_update` and `install.after_migrate` rather than inside a locked transition
	(A24).
	"""
	frappe.db.add_unique(
		"Cloud Storage Object",
		["content_sha256", "visibility", "bucket"],
		constraint_name=UNIQUE_IDENTITY_CONSTRAINT,
	)
	frappe.db.add_index("Cloud Storage Object", ["status", "deletion_scheduled_at"], GC_SCAN_INDEX)
