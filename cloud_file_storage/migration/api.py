"""The campaign state machine's only entry points (A9, design §5.1).

Every transition here does the same five things in the same order: role check, row lock,
assert the *expected* current status, write, commit, audit. The lock and the re-assert are
what make this safe against two operators (or an operator and the CLI) pressing the same
button at the same time — a `before_save` validation alone can be passed by both callers
before either commits.

The Desk buttons P7 will add are thin wrappers over these functions, and `commands.py`
calls exactly the same ones, so there is a single validation path. Nothing in this module
takes a shortcut that the other caller would not get.
"""

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from cloud_file_storage.cloud_file_storage.doctype.cloud_migration_campaign.cloud_migration_campaign import (
	ACTIVE_STATUSES,
)
from cloud_file_storage.migration import audit, conflicts, engine, planner, preflight
from cloud_file_storage.storage.modes import OperationMode, get_mode

CAMPAIGN_DOCTYPE = "Cloud Migration Campaign"
OBJECT_DOCTYPE = "Cloud Migration Object"

MANAGER_ROLE = "System Manager"
OPERATOR_ROLE = "Cloud Storage Manager"

#: Who may drive a campaign. Design §3.2's reason for the Cloud Storage Manager role is that
#: ops staff should be able to run a migration **without holding the bucket credentials**;
#: the other half of that separation is A12's `permlevel: 1` on the credential fields, which
#: P7 ships. Destructive actions stay System-Manager-only via `_manager_guard`.
OPERATOR_ROLES = (MANAGER_ROLE, OPERATOR_ROLE)

#: The literal an operator must type before verified local copies are deleted (design §3.3).
#: Compared byte-for-byte: a phrase the server normalises is a phrase the operator can get
#: wrong and still be understood, which defeats the point of asking.
CLEANUP_CONFIRM_PHRASE = "DELETE LOCAL FILES"


#: Campaign policy a caller may choose at creation. Scope, dedup and retry shaping — nothing
#: here decides whether bytes survive.
CREATION_POLICY_FIELDS = (
	"include_public",
	"include_private",
	"adopt_legacy_fork_rows",
	"adopt_remote_https_rows",
	"dedupe_by_content_hash",
	"max_attempts",
	"retry_backoff_base_secs",
)

#: P7-H1. These two decide what happens to local bytes, and `create_campaign` is reachable by
#: the operator role since P7 widened `_guard`. A campaign created with
#: `cleanup_mode="Direct Delete"` ("no going back" — `cleanup.py:341`) would be approved and
#: started by a System Manager who was never shown that choice: the approval is real, but it
#: approves a decision taken elsewhere by someone else. `quarantine_ttl_days` is the same
#: shape one step later — it sets how long a quarantined local copy survives before
#: `purge_expired_quarantine` removes it.
#:
#: They take their DocType defaults instead (Quarantine / 14 days). Direct Delete remains
#: reachable, at the moment it matters: `start_cleanup(direct_delete=True)` is
#: `_manager_guard`ed, type-to-confirm gated and audited. Cloud Storage Manager holds no
#: write DocPerm on Cloud Migration Campaign, so there is no second route to either field.
RESERVED_POLICY_FIELDS = ("cleanup_mode", "quarantine_ttl_days")


def _guard():
	"""Operate a campaign: move bytes, pause, retry, report."""
	frappe.only_for(OPERATOR_ROLES)


def _manager_guard():
	"""Destroy something: approve or start a cleanup, reconcile, purge, fail a campaign.

	Separate from `_guard` so widening the operator role can never widen these by accident.
	"""
	frappe.only_for(MANAGER_ROLE)


def _assert_confirm_phrase(supplied: str | None):
	if supplied != CLEANUP_CONFIRM_PHRASE:
		frappe.throw(
			_("Type {0} exactly to confirm deleting verified local copies.").format(CLEANUP_CONFIRM_PHRASE),
			title=_("Confirmation Required"),
		)


def _lock(campaign: str) -> str:
	"""`SELECT ... FOR UPDATE` on the campaign row; returns the current status."""
	status = frappe.db.get_value(CAMPAIGN_DOCTYPE, campaign, "status", for_update=True)
	if status is None:
		frappe.throw(_("Campaign {0} does not exist.").format(campaign))
	return status


def _assert_status(campaign: str, current: str, expected: tuple[str, ...]):
	if current not in expected:
		frappe.throw(
			_("Campaign {0} is {1}; this action needs it to be one of {2}.").format(
				campaign, current, ", ".join(expected)
			),
			title=_("Invalid Campaign State"),
		)


def _assert_no_other_active(campaign: str):
	"""At most one campaign owns bytes at a time (design §2.1).

	`CloudMigrationCampaign.validate_single_active` is the cheap half and cannot be the only
	one: every transition in this module writes through `frappe.db.set_value`, which does not
	run `validate`. The locking read is what makes two operators racing the same button — or
	an operator and the CLI — resolve to one winner.
	"""
	other = frappe.db.get_value(
		CAMPAIGN_DOCTYPE,
		{"status": ("in", ACTIVE_STATUSES), "name": ("!=", campaign)},
		"name",
		for_update=True,
	)
	if other:
		frappe.throw(
			_("Campaign {0} is already active; only one campaign may run at a time.").format(other),
			title=_("Campaign Already Active"),
		)


def _transition(campaign: str, to: str, **values):
	frappe.db.set_value(CAMPAIGN_DOCTYPE, campaign, {"status": to, **values}, update_modified=False)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- durability before enqueue/throw: state must survive the worker handoff and any later abort
	audit.record("campaign_transition", campaign=campaign, to=to, **values)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- durability before enqueue/throw: state must survive the worker handoff and any later abort


# ---------------------------------------------------------------------------- lifecycle


@frappe.whitelist()
def create_campaign(title: str, **policy) -> str:
	"""A new Draft campaign. Policy fields fall back to the Settings defaults."""
	_guard()

	from cloud_file_storage.storage.modes import get_settings

	settings = get_settings()
	doc = frappe.new_doc(CAMPAIGN_DOCTYPE)
	doc.update(
		{
			"title": title,
			"status": "Draft",
			"batch_size": cint(policy.get("batch_size") or settings.migration_batch_size or 1000),
			"parallelism": cint(policy.get("parallelism") or settings.migration_parallelism or 2),
			"bandwidth_limit_mbps": cint(
				policy.get("bandwidth_limit_mbps") or settings.migration_bandwidth_limit_mbps or 0
			),
			"multipart_threshold_mb": cint(
				policy.get("multipart_threshold_mb") or settings.multipart_threshold_mb or 64
			),
			"verify_strategy_cutover_mb": cint(
				policy.get("verify_strategy_cutover_mb") or settings.multipart_threshold_mb or 64
			),
		}
	)
	for field in CREATION_POLICY_FIELDS:
		if field in policy and policy[field] not in (None, ""):
			doc.set(field, policy[field])

	rejected = sorted(set(policy) & set(RESERVED_POLICY_FIELDS))
	if rejected:
		frappe.throw(
			_(
				"{0} cannot be set when a campaign is created. The destructive choices are "
				"made at the destructive moment: pass direct_delete to start_cleanup, which "
				"is System-Manager-only and audited."
			).format(", ".join(rejected)),
			title=_("Not Settable At Creation"),
		)

	doc.insert(ignore_permissions=True)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- durability before enqueue/throw: state must survive the worker handoff and any later abort
	audit.record("campaign_transition", campaign=doc.name, to="Draft", title=title)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- durability before enqueue/throw: state must survive the worker handoff and any later abort
	return doc.name


@frappe.whitelist()
def start_analysis(campaign: str, *, now: bool = False) -> dict:
	"""Draft → Analyzing. The analyzer job runs on the migration queue."""
	_guard()
	current = _lock(campaign)
	_assert_status(campaign, current, ("Draft", "Analyzed", "Stopped", "Failed"))
	_assert_no_other_active(campaign)

	camp = frappe.get_doc(CAMPAIGN_DOCTYPE, campaign)
	_transition(campaign, "Analyzing", active_phase=None, started_at=now_datetime())

	from cloud_file_storage.migration import analyzer

	if now:
		return analyzer.run_analysis(campaign)

	frappe.enqueue(
		"cloud_file_storage.migration.analyzer.run_analysis",
		queue=engine.queue_for(camp),
		timeout=engine.JOB_TIMEOUT,
		job_id=f"cfs::{campaign}::analyze",
		deduplicate=True,
		campaign=campaign,
	)
	return {"queued": True}


@frappe.whitelist()
def plan(campaign: str, batch_size: int | None = None) -> dict:
	"""Analyzed → Planned."""
	_guard()
	current = _lock(campaign)
	_assert_status(campaign, current, ("Analyzed", "Planned", "Stopped"))
	return planner.plan_campaign(campaign, batch_size=batch_size)


@frappe.whitelist()
def start_migration(
	campaign: str,
	*,
	parallelism: int | None = None,
	bandwidth_mbps: int | None = None,
	force_fallback_queue: bool = False,
	skip_preflight: bool = False,
) -> dict:
	"""Planned/Paused/Stopped → Running. The queue guard and A29 preflight live here."""
	_guard()
	current = _lock(campaign)
	_assert_status(campaign, current, ("Planned", "Paused", "Stopped", "Running"))

	_assert_no_other_active(campaign)

	if not engine.migration_queue_available():
		if not cint(force_fallback_queue):
			frappe.throw(
				_(
					"The dedicated {0} queue is not configured on this bench, so migration jobs "
					"would share the queues the ERP itself uses. Configure it (see "
					"docs/runbooks/deployment.md) or start with force_fallback_queue."
				).format(engine.MIGRATION_QUEUE),
				title=_("Migration Queue Not Configured"),
			)
		frappe.log_error(
			title="Cloud migration started on the fallback queue",
			message=(
				f"Campaign {campaign} was started with force_fallback_queue: its jobs will run on "
				f"{engine.FALLBACK_QUEUE!r} alongside normal ERP work."
			),
		)

	preflight.assert_startable(campaign, skip_preflight=bool(skip_preflight))

	values = {
		"control_flag": None,
		"active_phase": "UPLOAD",
		"started_at": frappe.db.get_value(CAMPAIGN_DOCTYPE, campaign, "started_at") or now_datetime(),
		"queue": engine.MIGRATION_QUEUE if engine.migration_queue_available() else engine.FALLBACK_QUEUE,
		"fallback_queue_used": 1
		if cint(force_fallback_queue) and not engine.migration_queue_available()
		else 0,
	}
	if parallelism:
		values["parallelism"] = cint(parallelism)
	if bandwidth_mbps is not None:
		values["bandwidth_limit_mbps"] = cint(bandwidth_mbps)

	_transition(campaign, "Running", **values)
	dispatched = engine.dispatch_batches(campaign)
	return {"status": "Running", "dispatched": dispatched}


@frappe.whitelist()
def pause(campaign: str) -> dict:
	"""Running → Paused. Workers see the flag on their next object."""
	_guard()
	current = _lock(campaign)
	_assert_status(campaign, current, ("Running",))
	_transition(campaign, "Paused", control_flag="PAUSE")
	return {"status": "Paused"}


@frappe.whitelist()
def resume(campaign: str) -> dict:
	_guard()
	current = _lock(campaign)
	_assert_status(campaign, current, ("Paused",))
	_transition(campaign, "Running", control_flag=None)
	return {"status": "Running", "dispatched": engine.dispatch_batches(campaign)}


@frappe.whitelist()
def stop(campaign: str) -> dict:
	"""Running/Paused → Stopping. Workers drain the current object, then stop."""
	_guard()
	current = _lock(campaign)
	_assert_status(campaign, current, ("Running", "Paused"))
	_transition(campaign, "Stopping", control_flag="STOP")
	return {"status": "Stopping"}


@frappe.whitelist()
def finalize_stop(campaign: str) -> dict:
	"""Stopping → Stopped, once nothing is in flight."""
	_guard()
	current = _lock(campaign)
	_assert_status(campaign, current, ("Stopping", "Running", "Paused"))

	from cloud_file_storage.cloud_file_storage.doctype.cloud_migration_batch.cloud_migration_batch import (
		IN_FLIGHT_STATUSES,
	)

	in_flight = frappe.db.count(
		"Cloud Migration Batch", {"campaign": campaign, "status": ("in", IN_FLIGHT_STATUSES)}
	)
	if in_flight:
		return {"status": current, "in_flight": in_flight}

	_transition(campaign, "Stopped", active_phase=None)
	return {"status": "Stopped"}


@frappe.whitelist()
def retry_failed(campaign: str) -> dict:
	"""Put every Failed object that still has attempts left back in the queue.

	Deliberately **not** an attempt-counter reset. `max_attempts` is the bound the engine
	uses to stop retrying a transient error for ever, and a bulk button that cleared it would
	let an operator loop an object that keeps failing until the campaign never converges.
	Objects that have spent their attempts are already sitting in the conflict queue with a
	`upload_failed` row (`engine._record_object_failure`), and `conflicts.retry` — a decision
	taken on one triaged object — is the deliberate way to give one of those a clean slate.
	So this reports them as `exhausted` rather than quietly reviving them.
	"""
	_guard()
	current = _lock(campaign)
	_assert_status(campaign, current, ("Running", "Paused", "Stopped", "Failed"))

	camp = frappe.get_doc(CAMPAIGN_DOCTYPE, campaign)
	max_attempts = cint(camp.max_attempts or 5)

	rows = frappe.db.get_all(
		OBJECT_DOCTYPE,
		filters={"campaign": campaign, "status": "Failed"},
		fields=["name", "attempt_count"],
		limit_page_length=0,
	)

	retried = 0
	exhausted = 0
	for row in rows:
		if cint(row.attempt_count) >= max_attempts:
			exhausted += 1
			continue
		if engine.cas_object(
			row.name,
			expected="Failed",
			to="Pending",
			next_retry_at=None,
			error_class=None,
			last_error=None,
		):
			conflicts.reopen_batch(row.name)
			retried += 1

	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- durability before enqueue/throw: state must survive the worker handoff and any later abort
	audit.record(
		"campaign_transition",
		campaign=campaign,
		action="retry_failed",
		retried=retried,
		exhausted=exhausted,
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- durability before enqueue/throw: state must survive the worker handoff and any later abort

	# Only a Running campaign may put work on a worker. Dispatching from Paused/Stopped
	# would enqueue jobs for a campaign the operator has deliberately halted.
	dispatched = engine.dispatch_batches(campaign) if current == "Running" else 0
	return {"retried": retried, "exhausted": exhausted, "dispatched": dispatched}


@frappe.whitelist()
def start_verify(campaign: str) -> dict:
	"""Re-open the VERIFY phase for batches that failed it.

	The happy path needs no button: `engine._next_batches` prefers `Uploaded` batches, so
	verification follows upload on its own. This is the repair path — a batch whose verify
	job failed sits in `Failed` and nothing will pick it up again.

	The A31 residue check is re-applied per batch rather than trusted from the batch status:
	VERIFY must never run over a batch that still holds a non-terminal upload object, and
	this is a second entry point into that phase, so it re-derives the same precondition the
	first one does.
	"""
	_guard()
	current = _lock(campaign)
	_assert_status(campaign, current, ("Running", "Paused", "Stopped", "Failed"))

	batches = frappe.db.get_all(
		"Cloud Migration Batch",
		filters={"campaign": campaign, "status": "Failed", "phase": ("!=", "CLEANUP")},
		fields=["name"],
		order_by="batch_no asc",
		limit_page_length=0,
	)

	requeued = 0
	blocked = 0
	for batch in batches:
		if engine.batch_has_upload_residue(batch.name):
			blocked += 1
			continue
		if engine.cas_batch(batch.name, expected="Failed", to="Uploaded"):
			frappe.db.set_value(
				"Cloud Migration Batch",
				batch.name,
				{"phase": "VERIFY", "attempts": 0, "last_error": None},
				update_modified=False,
			)
			requeued += 1

	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- durability before enqueue/throw: state must survive the worker handoff and any later abort
	audit.record(
		"campaign_transition",
		campaign=campaign,
		action="start_verify",
		requeued=requeued,
		blocked=blocked,
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- durability before enqueue/throw: state must survive the worker handoff and any later abort

	dispatched = engine.dispatch_batches(campaign) if current == "Running" else 0
	return {"requeued": requeued, "blocked": blocked, "dispatched": dispatched}


@frappe.whitelist()
def approve_cleanup(campaign: str, note: str | None = None) -> dict:
	"""A9 — the first of the two audited cleanup actions. Approving deletes nothing.

	Separated from `start_cleanup` deliberately, so that approval is two audited actions rather
	than one. **Not a two-person rule**: nothing requires the approver and the starter to be
	different users, and the Desk flow chains them in a single dialog (security finding L-2,
	DECISIONS 2026-08-16). Approval is a
	statement that the operator has reviewed the campaign's conflicts and convergence, and
	it is recorded with who and when. The job re-reads both fields before it moves a file.
	"""
	_manager_guard()
	current = _lock(campaign)
	_assert_status(campaign, current, ("Running", "Stopped", "Paused", "Completed"))

	blockers = conflicts.blocker_count(campaign)
	if blockers:
		frappe.throw(
			_(
				"{0} Blocker conflict(s) are still open; resolve or skip them before approving cleanup."
			).format(blockers),
			title=_("Open Blockers"),
		)

	frappe.db.set_value(
		CAMPAIGN_DOCTYPE,
		campaign,
		{
			"cleanup_approved_by": frappe.session.user,
			"cleanup_approved_at": now_datetime(),
			"cleanup_approval_note": note,
		},
		update_modified=False,
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- durability before enqueue/throw: state must survive the worker handoff and any later abort
	audit.record("campaign_transition", campaign=campaign, action="approve_cleanup", note=note)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- durability before enqueue/throw: state must survive the worker handoff and any later abort
	return {"approved_by": frappe.session.user}


@frappe.whitelist()
def start_cleanup(campaign: str, *, confirm_phrase: str | None = None, direct_delete: bool = False) -> dict:
	"""A9 — the second audited action. Every gate is re-evaluated inside the job (ADR-M16).

	`confirm_phrase` must be exactly :data:`CLEANUP_CONFIRM_PHRASE`. It is checked **here**,
	on the server, rather than in the Desk dialog that collects it, because a confirmation a
	client can skip is not a confirmation: `/api/method/...start_cleanup` is reachable
	without ever loading the form. Checking it in this module rather than in the Desk
	endpoint is what makes the CLI carry the same gate (PLAN §C security).
	"""
	_manager_guard()
	_assert_confirm_phrase(confirm_phrase)
	current = _lock(campaign)
	_assert_status(campaign, current, ("Running", "Stopped", "Paused", "Completed", "Cleanup Running"))

	camp = frappe.get_doc(CAMPAIGN_DOCTYPE, campaign)
	if not (camp.cleanup_approved_by and camp.cleanup_approved_at):
		frappe.throw(_("Cleanup has not been approved for this campaign."), title=_("Not Approved"))

	mode = get_mode()
	if mode not in (OperationMode.S3_PRIMARY_LOCAL_FALLBACK, OperationMode.S3_ONLY):
		frappe.throw(
			_(
				"Cleanup is not allowed in {0}: that mode promises a local copy. Switch to "
				"S3_PRIMARY_LOCAL_FALLBACK or S3_ONLY first."
			).format(mode.value),
			title=_("Mode Does Not Allow Cleanup"),
		)

	values = {"active_phase": "CLEANUP", "control_flag": None}
	if cint(direct_delete):
		values["cleanup_mode"] = "Direct Delete"

	_transition(campaign, "Cleanup Running", **values)
	audit.record(
		"campaign_transition",
		campaign=campaign,
		action="start_cleanup",
		confirmed_phrase=CLEANUP_CONFIRM_PHRASE,
		direct_delete=bool(cint(direct_delete)),
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- durability before enqueue/throw: state must survive the worker handoff and any later abort
	_open_cleanup_batches(campaign)
	return {"status": "Cleanup Running", "dispatched": engine.dispatch_batches(campaign)}


def _open_cleanup_batches(campaign: str) -> int:
	"""Re-open every verified batch for the cleanup pass."""
	batches = frappe.db.get_all(
		"Cloud Migration Batch",
		filters={"campaign": campaign, "status": "Verified"},
		pluck="name",
		limit_page_length=0,
	)
	for batch in batches:
		frappe.db.set_value(
			"Cloud Migration Batch",
			batch,
			{"phase": "CLEANUP", "attempts": 0},
			update_modified=False,
		)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- durability before enqueue/throw: state must survive the worker handoff and any later abort
	return len(batches)


@frappe.whitelist()
def complete(campaign: str) -> dict:
	"""→ Completed, once no object can move any further."""
	_guard()
	current = _lock(campaign)
	_assert_status(campaign, current, ("Running", "Cleanup Running", "Stopped"))

	if not engine.campaign_is_complete(campaign):
		frappe.throw(_("Objects are still in flight; the campaign is not complete."))

	_transition(campaign, "Completed", active_phase=None, completed_at=now_datetime(), control_flag=None)
	return {"status": "Completed"}


@frappe.whitelist()
def fail(campaign: str, reason: str) -> dict:
	_manager_guard()
	_lock(campaign)
	_transition(campaign, "Failed", last_error=(reason or "")[:500], control_flag=None)
	return {"status": "Failed"}


# ------------------------------------------------------------------------------ reading


@frappe.whitelist()
def status(campaign: str) -> dict:
	frappe.only_for(OPERATOR_ROLES)
	from cloud_file_storage.migration import report

	return {
		"snapshot": report.campaign_snapshot(campaign),
		"summary": report.summary(campaign),
		"convergence": report.convergence(campaign),
	}


@frappe.whitelist()
def run_preflight(campaign: str, target_rows: int = preflight.PROJECTION_ROWS) -> dict:
	_guard()
	return preflight.run_preflight(campaign, target_rows=cint(target_rows))


@frappe.whitelist()
def export_report(campaign: str, fmt: str = "csv") -> str:
	_guard()
	from cloud_file_storage.migration import report

	return report.export_campaign_report(campaign, fmt)


@frappe.whitelist()
def reconcile(prefix: str | None = None) -> dict:
	"""Three-way diff, refused while a campaign is in flight.

	A sweep that ran during a campaign would read objects mid-transfer and report them as
	drift: a `pending_upload` row whose key is not in the bucket yet is indistinguishable,
	from the bucket's side, from an object that was lost. Reporting a false `missing_remote`
	is worse than making the operator wait — that counter raises an Error Log and is the one
	an operator is meant to act on immediately.
	"""
	_manager_guard()
	active = frappe.db.get_value(CAMPAIGN_DOCTYPE, {"status": ("in", ACTIVE_STATUSES)}, "name")
	if active:
		frappe.throw(
			_(
				"Campaign {0} is still active. Reconcile compares the bucket against the "
				"object rows, and a campaign in flight makes that comparison meaningless."
			).format(active),
			title=_("Campaign Already Active"),
		)

	from cloud_file_storage.migration import reconcile as reconcile_module

	return reconcile_module.run_reconcile(prefix=prefix)


@frappe.whitelist()
def purge_snapshot(campaign: str, older_than_days: int = 90) -> dict:
	"""Drop the snapshot rows of a finished campaign (ADR-M15).

	Keeps the Campaign, its Conflicts, the Aliases and the Audit Log — the record of what
	happened — and drops only the working set: Objects, File Refs and Batches. Refuses on a
	campaign that is not finished, because those rows *are* the plan.
	"""
	_manager_guard()
	current = _lock(campaign)
	_assert_status(campaign, current, ("Completed", "Failed", "Stopped"))

	modified = frappe.db.get_value(CAMPAIGN_DOCTYPE, campaign, "modified")
	from frappe.utils import add_to_date, get_datetime

	if get_datetime(modified) > add_to_date(now_datetime(), days=-cint(older_than_days)):
		frappe.throw(
			_("Campaign {0} was last touched less than {1} days ago.").format(campaign, older_than_days)
		)

	counts = {
		"objects": frappe.db.count(OBJECT_DOCTYPE, {"campaign": campaign}),
		"refs": frappe.db.count("Cloud Migration File Ref", {"campaign": campaign}),
		"batches": frappe.db.count("Cloud Migration Batch", {"campaign": campaign}),
	}
	frappe.db.delete("Cloud Migration File Ref", {"campaign": campaign})
	frappe.db.delete(OBJECT_DOCTYPE, {"campaign": campaign})
	frappe.db.delete("Cloud Migration Batch", {"campaign": campaign})
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- durability before enqueue/throw: state must survive the worker handoff and any later abort

	audit.record("campaign_transition", campaign=campaign, action="purge_snapshot", **counts)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- durability before enqueue/throw: state must survive the worker handoff and any later abort
	return counts
