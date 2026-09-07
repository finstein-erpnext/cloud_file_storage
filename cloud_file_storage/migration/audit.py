"""The append-only audit writer (design §2.8).

Every destructive or mutating action in this engine writes one row **in the same
transaction as the action it describes**, so an audit row and the thing it records commit
or roll back together. There is no "log after the fact" path here on purpose: a quarantine
that happened without an audit row is indistinguishable from bytes that vanished.

`Cloud Storage Audit Log` grants no write and no delete role, and its controller refuses an
update even to `ignore_permissions` callers, so this module is the only way a row appears.
"""

import json

import frappe

AUDIT_DOCTYPE = "Cloud Storage Audit Log"

#: What the `actor` column says when nothing is logged in — a scheduler tick or a worker
#: job. `frappe.session.user` is "Administrator" in both cases, which would misattribute an
#: automated deletion to a person.
SYSTEM_ACTOR = "scheduler"


def current_actor() -> str:
	"""The session user, or `scheduler` when this is background work.

	Worker jobs run as Administrator, so `frappe.session.user` alone would attribute an
	automated quarantine to whoever happened to be Administrator. `engine.migration_job`
	sets the flag for exactly that reason.
	"""
	if frappe.flags.get("in_migration_job"):
		return SYSTEM_ACTOR
	return getattr(frappe.session, "user", None) or SYSTEM_ACTOR


def record(
	action: str,
	/,
	*,
	campaign: str | None = None,
	migration_object: str | None = None,
	file: str | None = None,
	cloud_storage_object: str | None = None,
	actor: str | None = None,
	**details,
) -> str:
	"""Write one audit row. Returns its name.

	`details` is serialised to JSON with a `default=str` fallback so a stray `datetime` or
	`Decimal` in a caller's context can never turn an audit write into an exception that
	rolls back the action being audited.

	`action` is **positional-only** for the same reason: several callers describe what they
	did with a detail key of their own called `action` (`conflict_action` + `action="retry"`),
	and a normal parameter would turn that into "got multiple values for argument 'action'"
	— an audit write raising inside the transaction of the thing it is auditing.
	"""
	doc = frappe.new_doc(AUDIT_DOCTYPE)
	doc.update(
		{
			"action": action,
			"actor": actor or current_actor(),
			"campaign": campaign,
			"migration_object": migration_object,
			"file": file,
			"cloud_storage_object": cloud_storage_object,
			"details": json.dumps(details, default=str, sort_keys=True) if details else None,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def count(action: str | None = None, *, campaign: str | None = None) -> int:
	filters = {}
	if action:
		filters["action"] = action
	if campaign:
		filters["campaign"] = campaign
	return frappe.db.count(AUDIT_DOCTYPE, filters)
