"""Executing a role gate, rather than reading that one was written.

`frappe.only_for` returns early on **two** conditions (`frappe/__init__.py:937`):

	if local.flags.in_test or local.session.user == "Administrator":
		return

Both hold for every test in this suite by default, so a test that simply calls a whitelisted
endpoint and expects `PermissionError` gets a pass no matter what the endpoint does. That is
why P6's first round asserted the gates **structurally**, by parsing `inspect.getsource` for
the literal `"System Manager"` — which proves a string appears in a file and nothing about
whether anybody is ever refused.

`acting_as` clears the flag *and* switches the user, so the gate executes. Both halves are
required and neither is sufficient: leaving `in_test` set makes every refusal test vacuous,
and leaving the user as Administrator does the same.

**`TestTheHarnessCanFail` in `test_backup_permissions.py` is the control**, and it is not
decoration: a helper that silently failed to clear the flag would make every refusal test
here pass while proving nothing, and would look identical to a working one.

This duplicates P7's `tests/desk_utils.acting_as`. It is written locally rather than imported
because P7 is a separate worktree during P6∥P7; **at convergence this became the single implementation** and
`tests/desk_utils.acting_as` was made an alias of it, so both import paths resolve to
one behaviour rather than two that can diverge.
"""

from contextlib import contextmanager

import frappe


def only_for_honours_in_test() -> bool:
	"""Does *this* frappe's ``only_for`` still short-circuit on ``flags.in_test``?

	v15 returns early on ``local.flags.in_test`` **or** Administrator
	(``frappe/__init__.py:937``). v16 **removed the in_test bypass** (``:532``), so a
	non-Administrator is refused even in test mode -- which makes `only_for` strictly
	stricter there, not weaker.

	Probed from the real function rather than keyed to a version number, so the controls
	below follow the behaviour the suite is actually running against. `acting_as` stays
	required on both: it clears the flag *and* switches the user, and the Administrator
	bypass exists in every version.
	"""
	import inspect

	return "in_test" in inspect.getsource(frappe.only_for)


#: A role that exists on every site and holds none of our permissions.
POWERLESS_ROLE = "Guest"


@contextmanager
def acting_as(user: str, roles: tuple[str, ...] = ()):
	"""Run the block as `user`, with `frappe.only_for` actually enforcing.

	`roles` is applied to the user for the duration and removed afterwards, so a test can
	say "a Cloud Storage Manager who is not a System Manager" without inventing a fixture
	site.
	"""
	previous_user = frappe.session.user
	previous_flag = frappe.local.flags.in_test
	added: list[str] = []

	try:
		# Roles are granted BEFORE the user switch and the cache is cleared afterwards.
		# Granting them after `set_user` leaves `frappe.get_roles` serving the cached list
		# from before the grant, so `only_for` refuses a user who does hold the role — which
		# is how the positive controls in `test_backup_permissions.py` failed on first run.
		for role in roles:
			if role not in frappe.get_roles(user):
				frappe.get_doc("User", user).add_roles(role)
				added.append(role)
		if added:
			frappe.db.commit()
		frappe.clear_cache(user=user)

		frappe.set_user(user)
		# LAST, so nothing above (which legitimately needs test privileges) is affected.
		frappe.local.flags.in_test = False
		yield
	finally:
		frappe.local.flags.in_test = previous_flag
		frappe.set_user(previous_user)
		# NOT suppressed. The original wrapped this so a cleanup failure could not mask a
		# test failure — the right instinct on the wrong statement. A role removal that
		# silently half-works leaves the user permanently elevated, `ensure_test_user` finds
		# them already present on the next run and adds nothing, and every refusal test below
		# is then asserting against a user who holds the role it expects them to lack. A
		# cleanup that fails quietly removes the signal; this one fails loudly.
		for role in added:
			frappe.get_doc("User", user).remove_roles(role)
		if added:
			frappe.db.commit()
			frappe.clear_cache(user=user)


@contextmanager
def system_manager_floor_relaxed():
	"""Let a test user be trashed on frappe refs that refuse it, without skipping `on_trash`.

	`User.a_system_manager_should_exist` (frappe v15.16.0) throws unless another **enabled,
	non-Administrator** System Manager exists — and the query excludes Administrator explicitly.
	On a fresh CI site Administrator is the only System Manager, so that condition is never
	satisfied and **every** user deletion throws, whether or not the user being deleted is
	itself a System Manager. The guard is gone by v15.93.0, which is why only the floor ref ever
	saw it: 23 errors on v15.16.0 against a green v15.93.0.

	**Lives here, and every `delete_doc("User", ...)` in the suite uses it.** A first version
	wrapped only `desk_utils.drop_users` — but that is reachable from four `tearDownClass`
	bodies and one `addCleanup`, which cannot produce 23 errors. The arithmetic was the tell:
	the other ~18 came from `permission_utils.drop_test_user` and `request_utils`, and a fix
	applied at one of four identical sites would have failed the floor a fifth time.

	frappe offers exactly one documented early return — `is_system_manager_disabled()`, which
	reads the Role's own `disabled` flag — so that is what is used. `ignore_on_trash=True` would
	also work and is worse: `on_trash` clears the user cache, deletes their ToDos and logs them
	out, and a cleanup helper should not silently drop that.
	"""
	# Gated on the capability, not the version: on refs where the guard does not exist there is
	# nothing to relax, and mutating a global Role flag plus flushing the site cache on four
	# otherwise-green jobs buys nothing.
	from frappe.core.doctype.user.user import User

	if not hasattr(User, "a_system_manager_should_exist"):
		yield
		return

	role_was_disabled = frappe.db.get_value("Role", "System Manager", "disabled")
	try:
		if not role_was_disabled:
			frappe.db.set_value("Role", "System Manager", "disabled", 1, update_modified=False)
			# Committed deliberately. Deleting a User reaches this app's File hooks (a User's
			# `user_image` is a File) and per invariant 7 those commit their own transaction —
			# so the flip can become durable inside the window. If the delete then throws, an
			# uncommitted restore is rolled back and System Manager stays disabled for the rest
			# of the run: exactly the silent permission failure this helper exists to avoid.
			frappe.db.commit()
		yield
	finally:
		if not role_was_disabled:
			frappe.db.set_value("Role", "System Manager", "disabled", 0, update_modified=False)
			frappe.db.commit()
			# The DB channel is closed by the restore; the CACHE channel is not. `get_roles()`
			# filters disabled roles, so a memo taken while the flag was down would outlive the
			# window and hand a later test a user without System Manager.
			#
			# **The broad form is load-bearing — measured, not assumed.** A review round proposed
			# narrowing this to `frappe.cache.delete_value("roles")` +
			# `frappe.local.role_permissions = {}` on the grounds that role memoisation is the
			# only stated need, and that the no-arg flush is expensive once this guards every
			# per-test deletion rather than ~5 class teardowns. That is a fair cost argument and
			# the narrow form does NOT work: with it, `test_serving_private` went from 33 green
			# to 31 errors, `test_desk_permissions` lost 13 tests to collection errors, and the
			# damage compounded across suites — while `tabRole.disabled` was verifiably 0, so
			# the DB restore was fine and the surviving poison was purely cached.
			#
			# Whatever frappe memoises about role state after a Role row changes, it is not
			# reached by those two calls. Keeping the flush, with the cost acknowledged rather
			# than traded for a suite that does not run.
			frappe.clear_cache()


def drop_test_user(email: str):
	"""Remove a fixture user and its roles.

	Called from class teardown so the suite leaves no elevated account behind. Users are the
	one piece of this suite's footprint that both persists AND hides its own repair: a user
	that survives with a leaked role is found by `ensure_test_user` on the next run, which
	adds nothing because the roles it wanted are already there.
	"""
	if not frappe.db.exists("User", email):
		return
	frappe.db.delete("Has Role", {"parent": email})
	with system_manager_floor_relaxed():
		frappe.delete_doc("User", email, force=True, ignore_permissions=True)
	frappe.db.commit()
	frappe.clear_cache(user=email)


def ensure_test_user(email: str, roles: tuple[str, ...] = ()) -> str:
	"""A real, enabled User with exactly `roles` — created once, reused."""
	if not frappe.db.exists("User", email):
		user = frappe.new_doc("User")
		user.update({"email": email, "first_name": email.split("@")[0], "send_welcome_email": 0})
		user.insert(ignore_permissions=True)
	else:
		user = frappe.get_doc("User", email)

	user.enabled = 1
	existing = set(frappe.get_roles(email))
	for role in roles:
		if role not in existing:
			user.add_roles(role)
	user.save(ignore_permissions=True)
	frappe.db.commit()
	return email
