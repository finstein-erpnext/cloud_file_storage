"""Every Desk button endpoint. **Nothing in this module decides anything.**

A9 in one sentence: `api/admin.py` contains only thin `frappe.only_for` wrappers delegating
to `migration/api.py`, so the CLI and the Desk share one validation path. The failure that
rule exists to prevent is specific and has happened in real apps: a state check written into
the button endpoint is a check `bench cfs-migrate-start` does not have, and the two drift
apart the first time one of them is fixed.

So each function here is exactly two statements — a role gate and a delegation — and
`tests/test_admin_api.py::TestAdminIsOnlyWrappers` parses this file to prove it. If a
button needs a new precondition, the precondition belongs in the choke point, not here.

The choke points are:

* `migration.api` — everything that touches a campaign. It owns the frozen A9 state machine,
  the row lock, the status assertion, the audit row and the type-to-confirm phrase.
* `storage.diagnostics` — Test Connection.
* `health` — the storage health panel.

Roles follow design §3.2: `Cloud Storage Manager` may operate a migration without holding
the bucket credentials (A12 `permlevel: 1` is the other half of that separation), while the
destructive actions — approving and starting a cleanup, reconcile — stay System Manager. The
role named here is defence in depth: the choke point re-checks it, and where the two differ
the narrower one wins because it runs second.
"""

import frappe

from cloud_file_storage import health as health_module
from cloud_file_storage.migration import api as migration_api
from cloud_file_storage.migration.api import MANAGER_ROLE, OPERATOR_ROLES
from cloud_file_storage.storage import diagnostics

# ------------------------------------------------------------------------------- storage


@frappe.whitelist()
def test_connection() -> dict:
	frappe.only_for(OPERATOR_ROLES)
	return diagnostics.test_connection()


@frappe.whitelist()
def get_storage_health() -> dict:
	frappe.only_for(OPERATOR_ROLES)
	return health_module.storage_health()


@frappe.whitelist()
def reconcile(prefix: str | None = None) -> dict:
	frappe.only_for(MANAGER_ROLE)
	return migration_api.reconcile(prefix=prefix)


# ----------------------------------------------------------------------------- campaigns


@frappe.whitelist()
def create_campaign(title: str, **policy) -> str:
	frappe.only_for(OPERATOR_ROLES)
	return migration_api.create_campaign(title, **policy)


@frappe.whitelist()
def analyze_storage(campaign: str, now: bool = False) -> dict:
	frappe.only_for(OPERATOR_ROLES)
	return migration_api.start_analysis(campaign, now=now)


@frappe.whitelist()
def build_migration_plan(campaign: str, batch_size: int | None = None) -> dict:
	frappe.only_for(OPERATOR_ROLES)
	return migration_api.plan(campaign, batch_size=batch_size)


@frappe.whitelist()
def start_campaign(
	campaign: str,
	parallelism: int | None = None,
	bandwidth_mbps: int | None = None,
	force_fallback_queue: bool = False,
	skip_preflight: bool = False,
) -> dict:
	frappe.only_for(OPERATOR_ROLES)
	return migration_api.start_migration(
		campaign,
		parallelism=parallelism,
		bandwidth_mbps=bandwidth_mbps,
		force_fallback_queue=force_fallback_queue,
		skip_preflight=skip_preflight,
	)


@frappe.whitelist()
def pause_campaign(campaign: str) -> dict:
	frappe.only_for(OPERATOR_ROLES)
	return migration_api.pause(campaign)


@frappe.whitelist()
def resume_campaign(campaign: str) -> dict:
	frappe.only_for(OPERATOR_ROLES)
	return migration_api.resume(campaign)


@frappe.whitelist()
def stop_after_current_batch(campaign: str) -> dict:
	frappe.only_for(OPERATOR_ROLES)
	return migration_api.stop(campaign)


@frappe.whitelist()
def retry_failed(campaign: str) -> dict:
	frappe.only_for(OPERATOR_ROLES)
	return migration_api.retry_failed(campaign)


@frappe.whitelist()
def start_verify(campaign: str) -> dict:
	frappe.only_for(OPERATOR_ROLES)
	return migration_api.start_verify(campaign)


@frappe.whitelist()
def export_migration_report(campaign: str, fmt: str = "csv") -> str:
	frappe.only_for(OPERATOR_ROLES)
	return migration_api.export_report(campaign, fmt)


@frappe.whitelist()
def get_campaign_snapshot(campaign: str) -> dict:
	frappe.only_for(OPERATOR_ROLES)
	return migration_api.status(campaign)


# ------------------------------------------------- Delete Verified Local Copies (A9, two)


@frappe.whitelist()
def approve_cleanup(campaign: str, note: str | None = None) -> dict:
	frappe.only_for(MANAGER_ROLE)
	return migration_api.approve_cleanup(campaign, note=note)


@frappe.whitelist()
def start_cleanup(campaign: str, confirm_phrase: str | None = None, direct_delete: bool = False) -> dict:
	frappe.only_for(MANAGER_ROLE)
	return migration_api.start_cleanup(campaign, confirm_phrase=confirm_phrase, direct_delete=direct_delete)
