# Copyright (c) 2026, Finstein and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

#: A9 — the frozen campaign state machine (migration-engine.md §5.1 verbatim).
CAMPAIGN_STATUSES = (
	"Draft",
	"Analyzing",
	"Analyzed",
	"Planned",
	"Running",
	"Paused",
	"Stopping",
	"Stopped",
	"Cleanup Running",
	"Completed",
	"Failed",
)

#: Statuses in which a campaign still owns bytes and work. At most one campaign may be in
#: this set at a time (migration-engine.md §2.1), and A14 counts their objects as live
#: references so a GC sweep cannot delete out from under a running campaign.
ACTIVE_STATUSES = ("Analyzing", "Running", "Paused", "Stopping", "Cleanup Running")

#: A campaign may only be deleted once it can no longer be resumed.
DELETABLE_STATUSES = ("Draft", "Completed", "Failed", "Stopped")


class CloudMigrationCampaign(Document):
	def validate(self):
		self.validate_status()
		self.validate_policy()
		self.validate_single_active()

	def validate_status(self):
		if self.status not in CAMPAIGN_STATUSES:
			frappe.throw(_("{0} is not a valid campaign status.").format(self.status))

	def validate_policy(self):
		# A12's deferred validator, ruled into P5 scope because the cutover lives on the
		# campaign rather than in Settings (DECISIONS.md 2026-08-14). Above the multipart
		# threshold S3's `ChecksumSHA256` is a checksum *of checksums*, so a cutover above
		# the threshold would compare a composite digest against a full-object one and fail
		# every large object — or, worse, be "fixed" by trusting it.
		if cint(self.verify_strategy_cutover_mb) > cint(self.multipart_threshold_mb):
			frappe.throw(
				_(
					"Verify strategy cutover ({0} MB) must not exceed the multipart threshold "
					"({1} MB): above the threshold the object's S3 checksum is composite and "
					"cannot be compared against a full-object SHA256."
				).format(self.verify_strategy_cutover_mb, self.multipart_threshold_mb),
				title=_("Invalid Verification Policy"),
			)

		if cint(self.batch_size) < 1:
			frappe.throw(_("Batch size must be at least 1."))
		if cint(self.parallelism) < 1:
			frappe.throw(_("Parallelism must be at least 1."))
		if cint(self.max_attempts) < 1:
			frappe.throw(_("Max attempts must be at least 1."))
		if cint(self.quarantine_ttl_days) < 0:
			frappe.throw(_("Quarantine TTL cannot be negative."))

	def validate_single_active(self):
		"""One campaign moving bytes at a time (migration-engine.md §2.1, failure table).

		This is the cheap half of the rule; `api.start_migration` re-checks it under a row
		lock, because two `before_save` calls can both pass before either commits.
		"""
		if self.status not in ACTIVE_STATUSES:
			return

		other = frappe.db.get_value(
			"Cloud Migration Campaign",
			{"status": ("in", ACTIVE_STATUSES), "name": ("!=", self.name or "")},
			"name",
		)
		if other:
			frappe.throw(
				_("Campaign {0} is already active. Only one campaign may run at a time.").format(other),
				title=_("Campaign Already Active"),
			)

	def on_trash(self):
		if self.status not in DELETABLE_STATUSES:
			frappe.throw(
				_("A campaign in state {0} cannot be deleted — it still owns migration work.").format(
					self.status
				),
				title=_("Campaign In Progress"),
			)


def on_doctype_update():
	frappe.db.add_index("Cloud Migration Campaign", ["status"])
