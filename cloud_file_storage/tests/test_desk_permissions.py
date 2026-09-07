"""A12's deferred item, and the role separation that is the reason for it.

The claim under test is not "the JSON says permlevel 1". It is that a **Cloud Storage
Manager cannot read or write the bucket credentials** while still being able to operate a
migration — design §3.2's whole rationale for the role, and the reason A12's permlevel item
was deferred to the phase that owns the roles (DECISIONS.md 2026-08-14).

So the reads and writes below go through the code paths the Desk goes through
(`apply_fieldlevel_read_permissions`, `Document.save`), not through the meta.
"""

import ast
import contextlib
import inspect
from unittest.mock import patch

import frappe

from cloud_file_storage.api import admin
from cloud_file_storage.migration import api as migration_api
from cloud_file_storage.tests.desk_utils import (
	CLOUD_STORAGE_MANAGER,
	SYSTEM_MANAGER,
	acting_as,
	drop_users,
	ensure_user,
)
from cloud_file_storage.tests.markers import refusal_guard
from cloud_file_storage.tests.permission_utils import only_for_honours_in_test
from cloud_file_storage.tests.utils import CloudStorageTestCase

SETTINGS_DOCTYPE = "Cloud Storage Settings"
CAMPAIGN_DOCTYPE = "Cloud Migration Campaign"
CREDENTIAL_FIELDS = ("access_key_id", "secret_access_key")


class DeskPermissionTestCase(CloudStorageTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.operator = ensure_user("cfs-operator@example.com", (CLOUD_STORAGE_MANAGER,))
		cls.manager = ensure_user("cfs-manager@example.com", (SYSTEM_MANAGER,))
		cls.nobody = ensure_user("cfs-nobody@example.com", ())

	@classmethod
	def tearDownClass(cls):
		drop_users(cls.operator, cls.manager, cls.nobody)
		super().tearDownClass()


class TestTheHarnessCanFail(DeskPermissionTestCase):
	"""Proves `acting_as` really gates, so every refusal asserted below means something."""

	def test_only_for_is_a_no_op_without_the_helper(self):
		frappe.set_user(self.nobody)
		try:
			if only_for_honours_in_test():
				# v15: in_test is still set: this is the trap. No exception, no permission.
				frappe.only_for(SYSTEM_MANAGER)
			else:
				# v16 dropped the in_test bypass, so the gate is live for a non-Administrator.
				# The remaining trap is Administrator alone (the sibling control asserts it);
				# this half asserts the gate really bites, which is the same guarantee.
				with self.assertRaises(frappe.PermissionError):
					frappe.only_for(SYSTEM_MANAGER)
		finally:
			frappe.set_user("Administrator")

	def test_only_for_is_a_no_op_for_administrator(self):
		"""`frappe.only_for` has TWO early returns — `flags.in_test` and Administrator.

		Both are load-bearing: a helper that cleared the flag but left the session as
		Administrator would gate nothing, and every refusal asserted in this file would pass
		against code with no role check at all. P6's control asserts both; this one did not,
		and an asymmetry between two controls guarding the same trap is how the weaker one
		becomes the one someone copies.
		"""
		self.assertEqual(frappe.session.user, "Administrator")
		frappe.local.flags.in_test = False
		try:
			frappe.only_for(SYSTEM_MANAGER)  # no exception: Administrator holds every role
		finally:
			frappe.local.flags.in_test = True

	def test_only_for_refuses_inside_the_helper(self):
		with acting_as(self.nobody), self.assertRaises(frappe.PermissionError):
			frappe.only_for(SYSTEM_MANAGER)

	def test_the_helper_restores_the_session(self):
		with acting_as(self.nobody):
			self.assertEqual(frappe.session.user, self.nobody)
		self.assertEqual(frappe.session.user, "Administrator")
		self.assertTrue(frappe.local.flags.in_test)


class TestCredentialFieldsArePermlevelOne(DeskPermissionTestCase):
	def test_both_credential_fields_carry_permlevel_1(self):
		meta = frappe.get_meta(SETTINGS_DOCTYPE)
		for fieldname in CREDENTIAL_FIELDS:
			self.assertEqual(
				int(meta.get_field(fieldname).permlevel or 0),
				1,
				f"{fieldname} is not at permlevel 1",
			)

	def test_a_permlevel_1_docperm_exists_for_system_manager_only(self):
		meta = frappe.get_meta(SETTINGS_DOCTYPE)
		level_one = {perm.role for perm in meta.permissions if int(perm.permlevel or 0) == 1}
		self.assertEqual(level_one, {SYSTEM_MANAGER})

	def test_the_operator_role_may_read_the_document_but_not_write_it(self):
		"""P7-C1. This shipped as `read: 1, write: 1`, copied from design §3.2's "rw".

		permlevel 1 covers the two credential fields and **nothing else**, so a level-0 write
		handed the operator role every other field — including `operation_mode`, the gate
		`start_cleanup` itself checks, and `object_delete_grace_days`, which PLAN §H item 1
		defines as the attachment recovery window and whose shortening brings forward an
		irreversible physical delete. Neither passes an endpoint: a plain document save was
		enough. Operating storage needs no settings write, because every operator capability
		this phase built is reached through a role-gated whitelisted endpoint.
		"""
		meta = frappe.get_meta(SETTINGS_DOCTYPE)
		level_zero = {perm.role: perm for perm in meta.permissions if int(perm.permlevel or 0) == 0}
		self.assertIn(CLOUD_STORAGE_MANAGER, level_zero)
		operator = level_zero[CLOUD_STORAGE_MANAGER]

		self.assertTrue(operator.read, "the panel could not render for the operator role")
		for capability in ("write", "create", "delete"):
			with self.subTest(capability=capability):
				self.assertFalse(
					operator.get(capability),
					f"the operator role holds {capability} on {SETTINGS_DOCTYPE} at permlevel 0",
				)

	def test_the_operator_cannot_read_the_credentials(self):
		frappe.db.set_single_value(SETTINGS_DOCTYPE, "access_key_id", "AKIA-SECRET-ID")
		frappe.db.commit()
		self.addCleanup(frappe.db.commit)
		self.addCleanup(frappe.db.set_single_value, SETTINGS_DOCTYPE, "access_key_id", None)

		with acting_as(self.operator):
			doc = frappe.get_doc(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE)
			doc.apply_fieldlevel_read_permissions()
			for fieldname in CREDENTIAL_FIELDS:
				self.assertIsNone(doc.get(fieldname), f"{fieldname} survived the field-level read filter")
			# The separation is worth nothing if it also hides the operational fields.
			self.assertTrue(doc.get("operation_mode"))

	def test_a_system_manager_can_read_the_credentials(self):
		frappe.db.set_single_value(SETTINGS_DOCTYPE, "access_key_id", "AKIA-SECRET-ID")
		frappe.db.commit()
		self.addCleanup(frappe.db.commit)
		self.addCleanup(frappe.db.set_single_value, SETTINGS_DOCTYPE, "access_key_id", None)

		with acting_as(self.manager):
			doc = frappe.get_doc(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE)
			doc.apply_fieldlevel_read_permissions()
			self.assertEqual(doc.access_key_id, "AKIA-SECRET-ID")

	def test_the_operator_has_no_permlevel_write_on_the_credentials(self):
		"""A12's half of the separation, isolated from P7-C1's.

		Since C1 the operator role cannot write this doctype at all, so a save-based test
		would now be refused before permlevel is consulted and would keep passing if the
		permlevel were removed. This asks the permlevel question directly, so the two
		protections are proved independently rather than one masking the other.
		"""
		with acting_as(self.operator):
			doc = frappe.get_doc(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE)
			for fieldname in CREDENTIAL_FIELDS:
				with self.subTest(fieldname=fieldname):
					self.assertFalse(
						doc.has_permlevel_access_to(fieldname, permission_type="write"),
						f"the operator role has permlevel write access to {fieldname}",
					)

		with acting_as(self.manager):
			doc = frappe.get_doc(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE)
			for fieldname in CREDENTIAL_FIELDS:
				with self.subTest(fieldname=fieldname, role="System Manager"):
					self.assertTrue(
						doc.has_permlevel_access_to(fieldname, permission_type="write"),
						"the System Manager lost permlevel write; nobody could configure the bucket",
					)

	def test_the_operator_cannot_overwrite_the_credentials(self):
		"""The write half, end to end. Reading being blocked would not stop a blind overwrite.

		The refusal now arrives from the DocPerm (C1) rather than from the permlevel reset,
		so this asserts the *outcome* — the stored credential is untouched — and accepts
		either mechanism. The mechanism-specific test is directly above.
		"""
		frappe.db.set_single_value(SETTINGS_DOCTYPE, "access_key_id", "AKIA-ORIGINAL")
		frappe.db.set_single_value(SETTINGS_DOCTYPE, "use_default_credential_chain", 1)
		frappe.db.commit()
		self.addCleanup(frappe.db.commit)
		self.addCleanup(frappe.db.set_single_value, SETTINGS_DOCTYPE, "access_key_id", None)

		with acting_as(self.operator):
			doc = frappe.get_doc(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE)
			doc.access_key_id = "AKIA-ATTACKER"
			doc.flags.skip_connection_probe = True
			with contextlib.suppress(frappe.PermissionError):
				doc.save()

		self.assertEqual(
			frappe.db.get_single_value(SETTINGS_DOCTYPE, "access_key_id"),
			"AKIA-ORIGINAL",
			"a Cloud Storage Manager rewrote a permlevel-1 credential",
		)


class TestTheOperatorCannotWriteSettingsAtAll(DeskPermissionTestCase):
	"""P7-C1, asserted through `Document.save` rather than through the meta.

	The meta assertion says what the JSON declares; these say what happens when the role
	actually tries, with `in_test` cleared. Both directions, because a refusal that also
	refused the System Manager would prove nothing about the separation.
	"""

	SAFE_FIELD = "audit_public_fallback"

	def _restore_later(self, fieldname):
		original = frappe.db.get_single_value(SETTINGS_DOCTYPE, fieldname)
		self.addCleanup(frappe.db.commit)
		self.addCleanup(frappe.db.set_single_value, SETTINGS_DOCTYPE, fieldname, original)
		return original

	def _save_as(self, user, **values):
		with acting_as(user):
			doc = frappe.get_doc(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE)
			doc.update(values)
			doc.flags.skip_connection_probe = True
			doc.save()

	def test_a_save_by_the_operator_raises(self):
		self._restore_later(self.SAFE_FIELD)
		with self.assertRaises(frappe.PermissionError):
			self._save_as(self.operator, **{self.SAFE_FIELD: 1})

	def test_the_system_manager_can_still_save(self):
		"""The positive control: without it the refusal above could be anything at all."""
		self._restore_later(self.SAFE_FIELD)
		self._save_as(self.manager, **{self.SAFE_FIELD: 1})
		self.assertEqual(frappe.db.get_single_value(SETTINGS_DOCTYPE, self.SAFE_FIELD), 1)

	def test_the_operator_cannot_move_the_mode_that_gates_cleanup(self):
		"""`start_cleanup` refuses unless the mode keeps no local copy, so writing the mode
		is writing the gate that constrains the very campaigns this role operates."""
		original = self._restore_later("operation_mode")
		# Asserted as "unchanged", not as "not S3_ONLY": another suite may have left the
		# site in S3_ONLY already, and a test that passes because the value it forbids was
		# there beforehand is not testing the refusal.
		target = "LOCAL_ONLY" if original == "S3_ONLY" else "S3_ONLY"

		with self.assertRaises(frappe.PermissionError):
			self._save_as(self.operator, operation_mode=target)

		self.assertEqual(frappe.db.get_single_value(SETTINGS_DOCTYPE, "operation_mode"), original)

	def test_the_operator_cannot_shorten_the_attachment_recovery_window(self):
		"""`object_delete_grace_days` is PLAN §H item 1's recovery window; shortening it
		brings forward a physical S3 delete, which nothing undoes."""
		original = self._restore_later("object_delete_grace_days")

		with self.assertRaises(frappe.PermissionError):
			self._save_as(self.operator, object_delete_grace_days=1)

		self.assertEqual(frappe.db.get_single_value(SETTINGS_DOCTYPE, "object_delete_grace_days"), original)

	def test_the_operator_cannot_repoint_the_bucket(self):
		"""Subsequent attachment bytes would go to a store the operator chose."""
		original = self._restore_later("bucket")

		with self.assertRaises(frappe.PermissionError):
			self._save_as(self.operator, bucket="attacker-owned-bucket")

		self.assertEqual(frappe.db.get_single_value(SETTINGS_DOCTYPE, "bucket"), original)

	def test_the_operator_can_still_read_the_panel_fields(self):
		"""Read has to survive: the health panel renders for this role."""
		with acting_as(self.operator):
			doc = frappe.get_doc(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE)
			doc.apply_fieldlevel_read_permissions()
			self.assertTrue(doc.get("operation_mode"))


class TestAdminEndpointRoles(DeskPermissionTestCase):
	"""Who may press which button. Every call runs with `in_test` cleared."""

	#: (endpoint, may the Cloud Storage Manager call it)
	ENDPOINTS = (
		("test_connection", True),
		("get_storage_health", True),
		("create_campaign", True),
		("analyze_storage", True),
		("build_migration_plan", True),
		("start_campaign", True),
		("pause_campaign", True),
		("resume_campaign", True),
		("stop_after_current_batch", True),
		("retry_failed", True),
		("start_verify", True),
		("export_migration_report", True),
		("get_campaign_snapshot", True),
		("reconcile", False),
		("approve_cleanup", False),
		("start_cleanup", False),
	)

	#: Campaigns this class actually created. `create_campaign` is the one endpoint in
	#: ENDPOINTS that succeeds rather than failing past the gate, so it leaves a row behind —
	#: one per suite invocation, owned by a user `drop_users` then force-deletes, which is how
	#: three sites ended up accumulating orphan-owner campaigns.
	created_campaigns: set[str] = set()

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.created_campaigns = set()
		cls.campaigns_before = frappe.db.count(CAMPAIGN_DOCTYPE)

	@classmethod
	def tearDownClass(cls):
		for name in sorted(cls.created_campaigns):
			frappe.delete_doc(CAMPAIGN_DOCTYPE, name, force=True, ignore_permissions=True)
		cls.created_campaigns.clear()
		frappe.db.commit()
		remaining = frappe.db.count(CAMPAIGN_DOCTYPE)
		super().tearDownClass()

		# The count, not just the deletion. A cleanup that only removes what it remembered
		# creating cannot see the next endpoint someone adds to ENDPOINTS that also writes a
		# row — this can, and it is the assertion the leak got past.
		if remaining != cls.campaigns_before:
			raise AssertionError(
				f"{cls.__name__} left {remaining - cls.campaigns_before} {CAMPAIGN_DOCTYPE} row(s) behind"
			)

	def _call(self, endpoint: str):
		"""Invoke past the role gate only. Anything after it may raise for other reasons."""
		function = getattr(admin, endpoint)
		arguments = {} if endpoint in ("test_connection", "get_storage_health", "reconcile") else {}
		if endpoint == "create_campaign":
			arguments = {"title": "role probe"}
		elif endpoint not in ("test_connection", "get_storage_health", "reconcile"):
			arguments = {"campaign": "CFS-DOES-NOT-EXIST"}

		result = function(**arguments)
		if endpoint == "create_campaign" and isinstance(result, str):
			type(self).created_campaigns.add(result)
		return result

	def test_a_user_with_no_role_is_refused_everywhere(self):
		for endpoint, _operator_allowed in self.ENDPOINTS:
			with self.subTest(endpoint=endpoint), acting_as(self.nobody):
				with self.assertRaises(frappe.PermissionError):
					self._call(endpoint)

	def test_the_operator_role_matches_the_documented_split(self):
		"""Allowed means "not refused for lack of a role", not "succeeds".

		Most of these are called against a campaign that does not exist, so they go on to
		fail in the state machine — which is the proof wanted here: the call got *past* the
		gate. Asserting a specific downstream exception instead would make this test track
		the engine's error taxonomy rather than the permission it is named for.
		"""
		for endpoint, operator_allowed in self.ENDPOINTS:
			with self.subTest(endpoint=endpoint), acting_as(self.operator):
				if not operator_allowed:
					with self.assertRaises(frappe.PermissionError):
						self._call(endpoint)
					continue
				try:
					self._call(endpoint)
				except frappe.PermissionError:
					self.fail(f"{endpoint} refused a Cloud Storage Manager")
				except Exception:  # noqa: BLE001 - anything but PermissionError is fine here
					pass

	def test_the_destructive_endpoints_are_system_manager_only_in_the_choke_point_too(self):
		"""The wrapper's role is defence in depth; migration/api is where it has to hold."""
		for endpoint in ("approve_cleanup", "start_cleanup", "reconcile", "purge_snapshot", "fail"):
			with self.subTest(endpoint=endpoint):
				source = inspect.getsource(getattr(migration_api, endpoint).__wrapped__)
				self.assertIn("_manager_guard()", source)
				self.assertNotIn("\t_guard()", source)


class TestOperatorRoleIsNotAccidentallyWide(DeskPermissionTestCase):
	def test_the_two_guards_name_different_role_sets(self):
		self.assertEqual(migration_api.OPERATOR_ROLES, (SYSTEM_MANAGER, CLOUD_STORAGE_MANAGER))
		self.assertEqual(migration_api.MANAGER_ROLE, SYSTEM_MANAGER)

	def test_no_admin_endpoint_gates_on_a_role_the_choke_point_does_not_have(self):
		"""Every `only_for` in api/admin names one of the two known role sets."""
		tree = ast.parse(inspect.getsource(admin))
		named = set()
		for node in ast.walk(tree):
			if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "only_for":
				self.assertEqual(len(node.args), 1, "only_for called with an unexpected signature")
				self.assertIsInstance(node.args[0], ast.Name)
				named.add(node.args[0].id)
		self.assertEqual(named, {"OPERATOR_ROLES", "MANAGER_ROLE"})


class TestTheC1PatchGuardBites(DeskPermissionTestCase):
	"""L-6 — the guard protecting a Critical had no committed test.

	Its fallibility was demonstrated once, by hand, in a chat message: the JSON was edited to
	reinstate `write: 1`, the Patch Log row deleted, and `bench migrate` watched to refuse.
	That was the right check and it re-runs never. These do.

	M-6 is the reason the Custom DocPerm case gets its own test rather than sharing one:
	`frappe.get_meta` swaps in customised rows whenever any exist, so on a site whose
	permissions have ever been edited the JSON the first test reads is not what governs.
	"""

	from cloud_file_storage.patches.v0_6_0 import apply_credential_permlevel as patch_module

	def drop_custom_docperm(self, name: str):
		"""Delete the row **and invalidate the meta cache**, which the delete does not do.

		`CustomDocPerm.on_update` calls `frappe.clear_cache(doctype=self.parent)`
		(`frappe/core/doctype/custom_docperm/custom_docperm.py:36-37`), so *inserting* the row
		makes the customised permissions take effect — which is what these tests need. There
		is **no `on_trash`**, so nothing invalidates the cache on the way out, and
		`Meta.set_custom_permissions` has already rebuilt `permissions` from the row
		(`frappe/model/meta.py:542-554`).

		Measured on a live site rather than reasoned about:

		    baseline           write=[0]
		    after insert       write=[1]
		    after db.delete    write=[1]   <- what this cleanup used to do
		    after delete_doc   write=[1]   <- and what `delete_doc` alone does
		    after clear_cache  write=[0]

		So the test that proves P7-C1 closed was leaving a cached permission set in which the
		operator role holds `write` on Cloud Storage Settings — the Critical itself, as cache.
		`delete_doc` does not fix it; only the explicit clear does.
		"""
		if frappe.db.exists("Custom DocPerm", name):
			frappe.delete_doc("Custom DocPerm", name, force=True, ignore_permissions=True)
		frappe.db.commit()
		frappe.clear_cache(doctype=SETTINGS_DOCTYPE)

	def operator_write_in_meta(self) -> list[int]:
		"""What `get_meta` currently reports for the operator role at permlevel 0."""
		return [
			int(perm.write or 0)
			for perm in frappe.get_meta(SETTINGS_DOCTYPE).permissions
			if perm.role == CLOUD_STORAGE_MANAGER and int(perm.permlevel or 0) == 0
		]

	def synthetic_meta(self, **capabilities):
		"""A meta carrying one operator DocPerm at level 0 with the given capabilities."""
		return frappe._dict(
			permissions=[frappe._dict({"role": CLOUD_STORAGE_MANAGER, "permlevel": 0, **capabilities})]
		)

	def test_the_shipped_row_is_accepted_when_read_only(self):
		"""The positive control: a refusal that fires on everything proves nothing."""
		self.patch_module.assert_operator_holds_no_write(self.synthetic_meta(read=1))

	def test_a_shipped_row_granting_write_is_refused(self):
		for capability in ("write", "create", "delete"):
			with self.subTest(capability=capability):
				with self.assertRaises(frappe.ValidationError) as caught:
					self.patch_module.assert_operator_holds_no_write(
						self.synthetic_meta(read=1, **{capability: 1})
					)
				message = str(caught.exception)
				self.assertIn("P7-C1", message)
				self.assertIn(capability, message)
				self.assertIn("cloud_storage_settings.json", message)

	def test_a_system_manager_row_is_not_confused_for_the_operator(self):
		meta = frappe._dict(permissions=[frappe._dict({"role": SYSTEM_MANAGER, "permlevel": 0, "write": 1})])
		self.patch_module.assert_operator_holds_no_write(meta)

	def test_a_permlevel_1_row_is_out_of_scope_for_this_check(self):
		"""Level 1 is A12's business and has its own assertion in the patch."""
		meta = frappe._dict(
			permissions=[frappe._dict({"role": CLOUD_STORAGE_MANAGER, "permlevel": 1, "write": 1})]
		)
		self.patch_module.assert_operator_holds_no_write(meta)

	def test_a_custom_docperm_granting_write_is_refused(self):
		"""M-6. The row that actually governs once anyone has edited permissions.

		Written as a real `Custom DocPerm` row rather than a synthetic one: the whole point of
		the finding is that this store is consulted by a different code path than the meta,
		and a stubbed version would test the stub.
		"""
		row = frappe.get_doc(
			{
				"doctype": "Custom DocPerm",
				"parent": SETTINGS_DOCTYPE,
				"parenttype": "DocType",
				"parentfield": "permissions",
				"role": CLOUD_STORAGE_MANAGER,
				"permlevel": 0,
				"read": 1,
				"write": 1,
			}
		)
		row.insert(ignore_permissions=True)
		frappe.db.commit()
		self.addCleanup(frappe.db.commit)
		self.addCleanup(self.drop_custom_docperm, row.name)

		with self.assertRaises(frappe.ValidationError) as caught:
			self.patch_module.assert_operator_holds_no_write(self.synthetic_meta(read=1))

		message = str(caught.exception)
		self.assertIn("P7-C1", message)
		self.assertIn("Custom DocPerm", message)
		self.assertIn("Role Permission Manager", message)
		# The property is "does not send the operator to edit JSON that no longer governs",
		# not "never says the word JSON". The message names the file precisely to say editing
		# it will not help, which is the useful thing to say. Asserting the absence of the
		# string was a proxy for the property and the wrong one — this asserts the property.
		self.assertIn("will NOT clear it", message)

	def test_a_read_only_custom_docperm_is_accepted(self):
		row = frappe.get_doc(
			{
				"doctype": "Custom DocPerm",
				"parent": SETTINGS_DOCTYPE,
				"parenttype": "DocType",
				"parentfield": "permissions",
				"role": CLOUD_STORAGE_MANAGER,
				"permlevel": 0,
				"read": 1,
			}
		)
		row.insert(ignore_permissions=True)
		frappe.db.commit()
		self.addCleanup(frappe.db.commit)
		self.addCleanup(self.drop_custom_docperm, row.name)

		self.patch_module.assert_operator_holds_no_write(self.synthetic_meta(read=1))

	def test_the_cleanup_leaves_no_cached_operator_write(self):
		"""The assertion that would have caught this, run against the real cleanup path.

		Inserts the row, confirms the meta really does report `write` while it exists — so
		the check cannot pass vacuously — then runs the cleanup this suite uses and asserts
		the cache no longer reports it.
		"""
		self.assertEqual(self.operator_write_in_meta(), [0], "the site started dirty")

		row = frappe.get_doc(
			{
				"doctype": "Custom DocPerm",
				"parent": SETTINGS_DOCTYPE,
				"parenttype": "DocType",
				"parentfield": "permissions",
				"role": CLOUD_STORAGE_MANAGER,
				"permlevel": 0,
				"read": 1,
				"write": 1,
			}
		)
		row.insert(ignore_permissions=True)
		frappe.db.commit()
		self.addCleanup(self.drop_custom_docperm, row.name)

		self.assertEqual(
			self.operator_write_in_meta(), [1], "the fixture never took effect; nothing to clean"
		)

		self.drop_custom_docperm(row.name)

		self.assertEqual(
			self.operator_write_in_meta(),
			[0],
			"the cleanup left a cached permission set granting the operator role write — "
			"which is P7-C1, re-created by the test that proves it closed",
		)

	def test_the_shipped_tree_passes_its_own_guard(self):
		"""Against the real meta, so a bad JSON edit fails here as well as at migrate time."""
		self.patch_module.assert_operator_holds_no_write()


class TestTheC1PatchBodyGuardsBite(DeskPermissionTestCase):
	"""L-6, the half that was still uncovered: `execute()`'s own three assertions.

	`TestTheC1PatchGuardBites` above drives `assert_operator_holds_no_write`, which is the
	level-0 half. The level-1 half — the A12 item the patch exists for — lives in `execute()`
	and had nothing running against it: that a credential field really came back from the sync
	at permlevel 1, that System Manager really has a level-1 DocPerm to configure it through,
	and that the operator role really does not.

	Driven against a synthetic meta with `reload_doc` neutralised, because the alternative is
	a test that rewrites the site's DocPerms to see what happens. Each case mutates exactly one
	field of a meta that otherwise passes, so a guard that stopped firing fails here naming the
	thing it stopped seeing.
	"""

	from cloud_file_storage.patches.v0_6_0 import apply_credential_permlevel as patch_module

	class _Meta:
		def __init__(self, fields: dict, permissions: list):
			self._fields = fields
			self.permissions = permissions

		def get_field(self, fieldname):
			return self._fields.get(fieldname)

	def healthy_meta(self, **overrides):
		"""A meta the patch accepts, before the per-test mutation."""
		permlevels = {field: 1 for field in CREDENTIAL_FIELDS}
		permlevels.update(overrides.pop("permlevels", {}))
		fields = {
			field: frappe._dict({"fieldname": field, "permlevel": permlevel})
			for field, permlevel in permlevels.items()
		}
		for missing in overrides.pop("missing_fields", ()):
			fields.pop(missing, None)

		permissions = overrides.pop(
			"permissions",
			[
				frappe._dict({"role": SYSTEM_MANAGER, "permlevel": 0, "read": 1, "write": 1}),
				frappe._dict({"role": SYSTEM_MANAGER, "permlevel": 1, "read": 1, "write": 1}),
				frappe._dict({"role": CLOUD_STORAGE_MANAGER, "permlevel": 0, "read": 1}),
			],
		)
		assert not overrides, f"unused overrides: {sorted(overrides)}"
		return self._Meta(fields, permissions)

	@contextlib.contextmanager
	def executing_against(self, meta):
		"""Run `execute()` with the schema reload and the meta lookup stubbed out."""
		module = self.patch_module
		with (
			patch.object(module.frappe, "reload_doc"),
			patch.object(module.frappe, "clear_cache"),
			patch.object(module.frappe, "get_meta", return_value=meta),
			# The Custom DocPerm sweep is `assert_operator_holds_no_write`'s business and has
			# its own tests; here it must not turn a level-1 assertion into a level-0 one.
			patch.object(module.frappe, "get_all", return_value=[]),
		):
			yield

	def test_the_healthy_meta_is_accepted(self):
		"""The positive control. A body that threw on everything would pass every case below."""
		with self.executing_against(self.healthy_meta()):
			self.patch_module.execute()

	@refusal_guard
	def test_a_credential_field_back_at_permlevel_zero_is_refused(self):
		for field in CREDENTIAL_FIELDS:
			with self.subTest(field=field):
				meta = self.healthy_meta(permlevels={field: 0})
				with self.executing_against(meta), self.assertRaises(frappe.ValidationError) as caught:
					self.patch_module.execute()
				self.assertIn(field, str(caught.exception))
				self.assertIn("permlevel 1", str(caught.exception))

	@refusal_guard
	def test_a_credential_field_missing_from_the_meta_is_refused(self):
		meta = self.healthy_meta(missing_fields=("secret_access_key",))
		with self.executing_against(meta), self.assertRaises(frappe.ValidationError) as caught:
			self.patch_module.execute()
		self.assertIn("secret_access_key", str(caught.exception))

	@refusal_guard
	def test_no_level_one_docperm_for_system_manager_is_refused(self):
		"""The lockout case: fields at permlevel 1 with no level-1 row means nobody can set them."""
		meta = self.healthy_meta(
			permissions=[
				frappe._dict({"role": SYSTEM_MANAGER, "permlevel": 0, "read": 1, "write": 1}),
				frappe._dict({"role": CLOUD_STORAGE_MANAGER, "permlevel": 0, "read": 1}),
			]
		)
		with self.executing_against(meta), self.assertRaises(frappe.ValidationError) as caught:
			self.patch_module.execute()
		self.assertIn("no permlevel-1 DocPerm for System Manager", str(caught.exception))

	@refusal_guard
	def test_an_operator_level_one_docperm_is_refused(self):
		"""design §3.2: the role exists so storage can be operated without credential access."""
		meta = self.healthy_meta(
			permissions=[
				frappe._dict({"role": SYSTEM_MANAGER, "permlevel": 1, "read": 1, "write": 1}),
				frappe._dict({"role": CLOUD_STORAGE_MANAGER, "permlevel": 1, "read": 1}),
			]
		)
		with self.executing_against(meta), self.assertRaises(frappe.ValidationError) as caught:
			self.patch_module.execute()
		self.assertIn(CLOUD_STORAGE_MANAGER, str(caught.exception))
		self.assertIn("permlevel-1 DocPerm", str(caught.exception))
