"""Fixtures for testing the Desk surfaces — and one trap worth naming.

`frappe.only_for` returns immediately when `frappe.local.flags.in_test` is set **or** when
the session user is Administrator (`frappe/__init__.py:930-938`). A test suite runs with
both of those true, so every role assertion written the obvious way passes against code
that has no role gate at all. `acting_as` clears the flag and switches user for the
duration of the block, which is what makes a refusal mean something.

`docs/DECISIONS.md` records six checks in this project that ran, passed, and read something
other than the property they named. `TestTheHarnessCanFail` below exists so this helper
never becomes the seventh.
"""

import frappe

CLOUD_STORAGE_MANAGER = "Cloud Storage Manager"
SYSTEM_MANAGER = "System Manager"

#: Every user `ensure_user` has minted this process, so a suite can drop exactly its own.
CREATED_USERS: set[str] = set()


#: **Convergence decision.** P6 and P7 each grew an `acting_as`. They were NOT equivalent:
#: `permission_utils.acting_as` takes `roles=` and grants them BEFORE the user switch with a
#: cache clear — DECISIONS records that ordering as the fix for a trap producing refusals
#: indistinguishable from real ones. This one did neither. Rather than keep two helpers and a
#: note about which to prefer — which is how someone adds a role grant inside the weaker one
#: and reproduces the trap — this name is an alias. Both import paths, one behaviour.
from cloud_file_storage.tests.permission_utils import (  # noqa: F401,E402
	acting_as,
	system_manager_floor_relaxed,
)


def ensure_user(email: str, roles: tuple[str, ...]) -> str:
	"""A test user holding exactly `roles` (plus the implicit ones frappe gives everyone)."""
	if frappe.db.exists("User", email):
		user = frappe.get_doc("User", email)
		user.set("roles", [])
	else:
		user = frappe.new_doc("User")
		user.update({"email": email, "first_name": email.split("@")[0], "send_welcome_email": 0})

	for role in roles:
		user.append("roles", {"role": role})
	user.flags.ignore_permissions = True
	user.save(ignore_permissions=True)
	frappe.db.commit()
	CREATED_USERS.add(email)
	return email


def drop_users(*emails: str):
	"""Remove test users and **verify they are gone**.

	These were created and never deleted. They hold roles, so they accumulate in
	`report.dashboard_audience()` — a role-derived set the realtime publisher iterates — and
	the set grows with every run on a site. `request_utils.py` has deleted its own users all
	along, so there was a precedent here and it was not followed.

	The assertion matters as much as the deletion: a cleanup whose failure is silent is how a
	fixture outlives its test, which is the shape of both leaks this phase produced.
	"""
	for email in emails or tuple(CREATED_USERS):
		if frappe.db.exists("User", email):
			with system_manager_floor_relaxed():
				frappe.delete_doc("User", email, force=True, ignore_permissions=True)
		CREATED_USERS.discard(email)
	frappe.db.commit()

	survivors = [email for email in emails or () if frappe.db.exists("User", email)]
	if survivors:
		raise AssertionError(f"test users survived their cleanup: {survivors}")
