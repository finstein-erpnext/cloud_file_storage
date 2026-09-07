"""Adopt a `frappe_s3_attachment` 0.2.x install — the half that must precede `sync_all` (A21).

`[pre_model_sync]`, and the placement is load-bearing for one reason, not the two an earlier
draft of this docstring claimed:

* `sync_all` (`frappe/model/sync.py:43`) iterates `frappe.get_installed_apps()`. Until that
  list names this app, a migrate on a legacy site creates none of our doctypes. That is the
  whole of it, and it is enough: nothing else in the migrate can fix the list, because the
  unfiltered read is also what `patch_handler` and the `before_migrate` hooks do first.

**What this patch does NOT save the site from**, corrected against frappe's source rather than
against expectation: `remove_orphan_doctypes` (`frappe/model/sync.py:143-176`) does not reap
the fork's DocTypes at all. It treats `ImportError` as the orphan signal, and resolving a
module whose app is off the bench raises `frappe.DoesNotExistError` from `get_module_app`
(`frappe/modules/utils.py:277-281`) — caught by that function's bare `except Exception:
continue` and skipped. The fork's DocType rows survive the migrate, which is what the
rehearsal evidence records. (`ModuleNotFoundError` is a *subclass* of `ImportError`, so the
distinction that matters here is `DoesNotExistError` vs either of them, not one of them vs the
other.)

Double-guarded, in the shape `erpnext/patches/v15_0/remove_loan_management_module.py:5-6`
uses: no fork residue at all ⇒ four indexed reads and a return. Nothing here is destructive
and nothing already set by this app is overwritten, so a second run is a no-op by
construction rather than by a Patch Log entry.
"""

import frappe

from cloud_file_storage import legacy_install


def execute():
	if not frappe.db.table_exists("Singles"):
		return

	report = legacy_install.bootstrap()
	if not report.get("legacy_install"):
		return

	print(f"cloud_file_storage: adopted a frappe_s3_attachment install — {report}")
