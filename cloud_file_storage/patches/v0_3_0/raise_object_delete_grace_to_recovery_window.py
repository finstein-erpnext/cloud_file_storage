"""Raise `object_delete_grace_days` on sites still holding a grace shorter than the window.

The shipped default moved from 7 to 30 (PLAN.md §H item 1: the owner's adopted attachment
recovery window). A default change only affects new installs, so an existing site keeps a
7-day grace against a 30-day restore window — the state A27 forbids, and one where an
object can be physically deleted while a restorable backup still references it.

Deliberately narrow: this only touches sites that are actually in that invalid state
(`grace < restore_window`). An operator who has set a grace at or above their window has
made a choice, and it is left alone.
"""

import frappe
from frappe.utils import cint

SETTINGS_DOCTYPE = "Cloud Storage Settings"


def execute():
	if not frappe.db.table_exists("Singles"):
		return

	grace = cint(frappe.db.get_single_value(SETTINGS_DOCTYPE, "object_delete_grace_days"))
	restore_window = cint(frappe.db.get_single_value(SETTINGS_DOCTYPE, "restore_window_days"))
	if not restore_window or grace >= restore_window:
		return

	frappe.db.set_single_value(SETTINGS_DOCTYPE, "object_delete_grace_days", restore_window)
	frappe.clear_document_cache(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE)
	frappe.logger("cloud_file_storage").info(
		f"raised object_delete_grace_days from {grace} to {restore_window} "
		"so an object cannot be deleted inside its own restore window"
	)
