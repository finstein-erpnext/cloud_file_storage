"""Every whitelisted backup endpoint actually refuses somebody (gate finding M-1).

P6's first round asserted the role gates by parsing `inspect.getsource` for the literal
`"System Manager"`. That proves a string appears in a file. It cannot see `only_for` running
*after* a side effect, the function being reachable another way, or refusal simply not
working in this runtime — and the endpoint at the sharp end of that gap hands out a presigned
URL to the artifact carrying the database password.

Structure, in the order it has to be read:

1. `TestTheHarnessCanFail` — the control. `frappe.only_for` returns early when
   `flags.in_test` is set **or** the user is Administrator, so a harness that failed to
   clear either would make every test below pass while proving nothing. Written first and
   watched to fail before any of the rest existed.
2. one refusal per endpoint, asserting `frappe.PermissionError` **specifically** — not a
   base class, because these endpoints throw `ValidationError` for several unrelated reasons
   once past the gate, and a bare `assertRaises(Exception)` would pass against a completely
   ungated endpoint that merely failed later;
3. a positive control per endpoint — the same call succeeding as System Manager — because
   without it every refusal above could be an unrelated failure.
"""

import ast

import frappe

from cloud_file_storage.tests.backup_utils import BackupTestCase, set_backup_settings
from cloud_file_storage.tests.markers import refusal_guard
from cloud_file_storage.tests.permission_utils import (
	acting_as,
	drop_test_user,
	ensure_test_user,
	only_for_honours_in_test,
)

#: Someone with a Desk login and none of our roles.
OUTSIDER = "cfs-p6-outsider@example.invalid"
#: The operator role. It may read; it may not mint URLs or start backups.
OPERATOR = "cfs-p6-operator@example.invalid"


class PermissionTestCase(BackupTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_test_user(OUTSIDER)
		ensure_test_user(OPERATOR, roles=("Cloud Storage Manager",))
		# Removed at teardown, not left for the next run to find. See `drop_test_user`.
		cls.addClassCleanup(drop_test_user, OUTSIDER)
		cls.addClassCleanup(drop_test_user, OPERATOR)


class TestTheHarnessCanFail(PermissionTestCase):
	"""The control. Without this the whole file is decoration.

	Both early-return conditions of `frappe.only_for` are asserted directly, so if a future
	frappe changes them, or `acting_as` stops clearing the flag, this fails before the
	endpoint tests start passing for the wrong reason.
	"""

	def test_only_for_is_a_no_op_while_in_test_is_set(self):
		"""The trap the AST assertions were routing around. It is real."""
		self.assertTrue(frappe.local.flags.in_test, "the suite should be running with in_test set")
		frappe.set_user(OUTSIDER)
		self.addCleanup(frappe.set_user, "Administrator")

		if only_for_honours_in_test():
			frappe.only_for("System Manager")  # v15: must NOT raise: in_test short-circuits it
		else:
			# v16 removed that early return, so the gate bites even with in_test set. The trap
			# `acting_as` defeats is then Administrator alone, asserted by the sibling control.
			with self.assertRaises(frappe.PermissionError):
				frappe.only_for("System Manager")

	def test_only_for_is_a_no_op_for_administrator_even_outside_in_test(self):
		with acting_as("Administrator"):
			frappe.only_for("System Manager")  # must NOT raise: Administrator short-circuits it

	@refusal_guard
	def test_acting_as_makes_only_for_bite(self):
		"""The property every test below depends on."""
		with acting_as(OUTSIDER):
			self.assertFalse(frappe.local.flags.in_test)
			self.assertEqual(frappe.session.user, OUTSIDER)
			with self.assertRaises(frappe.PermissionError):
				frappe.only_for("System Manager")

	def test_acting_as_restores_the_flag_and_the_user(self):
		with acting_as(OUTSIDER):
			pass
		self.assertTrue(frappe.local.flags.in_test)
		self.assertEqual(frappe.session.user, "Administrator")

	def test_a_system_manager_passes_the_same_gate(self):
		"""Otherwise every refusal below could be refusing everyone, including operators."""
		with acting_as(OUTSIDER, roles=("System Manager",)):
			frappe.only_for("System Manager")


class TestBackupEndpointsRefuseAnOutsider(PermissionTestCase):
	"""One `PermissionError` per whitelisted endpoint.

	No count here on purpose. The number was "six" until the gate found two endpoints neither
	the implementer nor the reviewer had counted, and a number in a docstring is a coverage
	claim with no mechanism behind it. `TestTheGateRunsFirst.test_the_walk_finds_the_endpoints`
	counts the population by discovery and fails if it shrinks; that is where the number lives.
	"""

	def setUp(self):
		super().setUp()
		self.key = "site/backups/daily/20260816_010000/db.sql.gz"

	def test_backup_now_refuses(self):
		from cloud_file_storage.backup import tasks

		with acting_as(OUTSIDER), self.assertRaises(frappe.PermissionError):
			tasks.backup_now()

	def test_preview_lifecycle_policy_refuses(self):
		from cloud_file_storage.backup import lifecycle

		with acting_as(OUTSIDER), self.assertRaises(frappe.PermissionError):
			lifecycle.preview_lifecycle_policy()

	def test_apply_lifecycle_policy_refuses(self):
		from cloud_file_storage.backup import lifecycle

		with acting_as(OUTSIDER), self.assertRaises(frappe.PermissionError):
			lifecycle.apply_lifecycle_policy(confirm_phrase=lifecycle.APPLY_CONFIRM_PHRASE)

		self.assertEqual(self.bucket.put_lifecycle_calls, [], "nothing may reach the bucket")

	def test_list_backups_refuses(self):
		from cloud_file_storage.backup import restore

		with acting_as(OUTSIDER), self.assertRaises(frappe.PermissionError):
			restore.list_backups()

	def test_initiate_restore_refuses(self):
		from cloud_file_storage.backup import restore

		with acting_as(OUTSIDER), self.assertRaises(frappe.PermissionError):
			restore.initiate_restore(self.key)

		self.assertEqual(self.bucket.restore_requests, [])

	def test_restore_status_refuses(self):
		from cloud_file_storage.backup import restore

		with acting_as(OUTSIDER), self.assertRaises(frappe.PermissionError):
			restore.restore_status(self.key)

	@refusal_guard
	def test_download_url_refuses(self):
		"""The endpoint at the sharp end of H-1: a presigned GET to the database password."""
		from cloud_file_storage.backup import restore

		with acting_as(OUTSIDER), self.assertRaises(frappe.PermissionError):
			restore.download_url(self.key)

		self.assertEqual(self.bucket.presign_calls, [], "no URL may be minted for a refused caller")

	def test_get_backup_health_refuses(self):
		from cloud_file_storage.backup import health

		with acting_as(OUTSIDER), self.assertRaises(frappe.PermissionError):
			health.get_backup_health()


class TestTheOperatorRoleIsNotASystemManager(PermissionTestCase):
	"""Cloud Storage Manager may operate storage; it may not mint a URL to the database.

	The role exists so an operator can work without holding credentials (A12). An endpoint
	that accepted it would hand exactly the thing the role was built to withhold.
	"""

	@refusal_guard
	def test_the_operator_cannot_mint_a_download_url(self):
		from cloud_file_storage.backup import restore

		with acting_as(OPERATOR), self.assertRaises(frappe.PermissionError):
			restore.download_url("site/backups/daily/x/db.sql.gz")

		self.assertEqual(self.bucket.presign_calls, [])

	def test_the_operator_cannot_apply_a_lifecycle_policy(self):
		from cloud_file_storage.backup import lifecycle

		# Destructive: it schedules deletions.
		with acting_as(OPERATOR), self.assertRaises(frappe.PermissionError):
			lifecycle.apply_lifecycle_policy(confirm_phrase=lifecycle.APPLY_CONFIRM_PHRASE)

	def test_the_operator_cannot_start_a_backup(self):
		from cloud_file_storage.backup import tasks

		with acting_as(OPERATOR), self.assertRaises(frappe.PermissionError):
			tasks.backup_now()


class TestASystemManagerIsAllowedThrough(PermissionTestCase):
	"""The positive controls. A refusal test with no positive control cannot tell
	"refused correctly" from "broken for everyone"."""

	def setUp(self):
		super().setUp()
		self.log = self._make_log()

	def _make_log(self) -> str:
		import json

		doc = frappe.new_doc("Cloud Storage Backup Log")
		doc.update(
			{
				"status": "Success",
				"trigger": "manual",
				"backup_bucket": "cfs-unit-test-backups",
				"db_key": self.scoped_key(),
				"sha256_manifest": json.dumps(
					{self.scoped_key(): {"kind": "database", "sha256": "a" * 64, "size": 10}}
				),
			}
		)
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		return self.track_log(doc.name)

	@staticmethod
	def scoped_key() -> str:
		return f"{frappe.local.site}/backups/daily/20260816_010000/db.sql.gz"

	def test_a_system_manager_may_list_backups(self):
		from cloud_file_storage.backup import restore

		with acting_as(OUTSIDER, roles=("System Manager",)):
			rows = restore.list_backups(limit=5)
		self.assertTrue(rows)

	def test_a_system_manager_may_mint_a_download_url(self):
		from cloud_file_storage.backup import restore

		with acting_as(OUTSIDER, roles=("System Manager",)):
			url = restore.download_url(self.scoped_key())

		self.assertIn("X-Amz-Expires", url)
		self.assertTrue(self.bucket.presign_calls)

	def test_a_system_manager_may_preview_a_lifecycle_policy(self):
		from cloud_file_storage.backup import lifecycle

		with acting_as(OUTSIDER, roles=("System Manager",)):
			preview = lifecycle.preview_lifecycle_policy()
		self.assertTrue(preview["generated_rules"])

	def test_a_system_manager_may_read_backup_health(self):
		from cloud_file_storage.backup import health

		set_backup_settings()
		with acting_as(OUTSIDER, roles=("System Manager",)):
			report = health.get_backup_health()
		self.assertIn("findings", report)


class TestCredentialAndKeyFieldsAreLevelOne(PermissionTestCase):
	"""A12 for the backup doctypes (M-4, M-5), asserted on the installed meta.

	The Password field was never the exposure — frappe stores `'*****'` in the column. The
	exposures were the backup bucket's **Access Key ID**, readable at level 0 by exactly the
	role A12 exists to keep away from credentials, and every backup artifact's object key,
	which is the lower-privileged half of the presign path H-1 closed.
	"""

	EXPECTED = {
		"Cloud Backup Settings": ("access_key", "secret_key"),
		"Cloud Storage Backup Log": (
			"db_key",
			"config_key",
			"public_files_key",
			"private_files_key",
			"backup_bucket",
			"sha256_manifest",
		),
	}

	@staticmethod
	def _schema(name: str) -> dict:
		import json
		from pathlib import Path

		import cloud_file_storage

		root = Path(cloud_file_storage.__file__).parent / "cloud_file_storage" / "doctype"
		return json.loads((root / name / f"{name}.json").read_text())

	@refusal_guard
	def test_the_shipped_json_declares_permlevel_one(self):
		"""Read off disk, because that is what ships.

		The installed-meta check below only changes at `bench migrate`, so an edit to the
		DocType JSON would not move it — the pair is what makes a permlevel regression visible
		in the same commit that causes it. (Both JSON mutations survived until this existed.)
		"""
		for name, fieldnames in (
			("cloud_backup_settings", self.EXPECTED["Cloud Backup Settings"]),
			("cloud_storage_backup_log", self.EXPECTED["Cloud Storage Backup Log"]),
		):
			schema = self._schema(name)
			levels = {field["fieldname"]: field.get("permlevel") for field in schema["fields"]}
			for fieldname in fieldnames:
				with self.subTest(doctype=name, field=fieldname):
					self.assertEqual(levels.get(fieldname), 1)
			with self.subTest(doctype=name, check="docperm"):
				self.assertTrue(
					any(perm.get("permlevel") == 1 for perm in schema["permissions"]),
					f"{name}.json declares permlevel-1 fields and no permlevel-1 DocPerm",
				)

	@refusal_guard
	def test_the_credential_and_key_fields_are_at_permlevel_one(self):
		for doctype, fieldnames in self.EXPECTED.items():
			meta = frappe.get_meta(doctype)
			for fieldname in fieldnames:
				with self.subTest(doctype=doctype, field=fieldname):
					field = meta.get_field(fieldname)
					self.assertIsNotNone(field, f"{doctype}.{fieldname} is missing")
					self.assertEqual(int(field.permlevel or 0), 1)

	def test_each_doctype_has_a_matching_permlevel_one_docperm(self):
		"""Half of this change locks everyone out of the field, which is worse than not
		shipping it — the reason P1 deferred the equivalent rather than doing it in passing."""
		for doctype in self.EXPECTED:
			with self.subTest(doctype=doctype):
				level_one = [
					perm for perm in frappe.get_meta(doctype).permissions if int(perm.permlevel or 0) == 1
				]
				self.assertTrue(level_one, f"{doctype} has permlevel-1 fields and no level-1 DocPerm")
				self.assertTrue(any(p.role == "System Manager" and p.read for p in level_one))

	def test_the_operator_role_has_no_permlevel_one_access(self):
		"""The whole point: Cloud Storage Manager operates storage without holding credentials."""
		for doctype in self.EXPECTED:
			with self.subTest(doctype=doctype):
				for perm in frappe.get_meta(doctype).permissions:
					if perm.role == "Cloud Storage Manager":
						self.assertEqual(int(perm.permlevel or 0), 0)

	def _make_log_with_keys(self) -> str:
		import json

		doc = frappe.new_doc("Cloud Storage Backup Log")
		doc.update(
			{
				"status": "Success",
				"trigger": "manual",
				"db_key": f"{frappe.local.site}/backups/daily/x/db.sql.gz",
				"sha256_manifest": json.dumps({"k": {"sha256": "a" * 64, "size": 1}}),
			}
		)
		doc.insert(ignore_permissions=True)
		self.track_log(doc.name)
		frappe.db.commit()
		return doc.name

	@refusal_guard
	def test_the_operator_cannot_query_a_backup_artifact_key(self):
		"""Asserted against a real read, through the paths an operator actually has, and
		against what frappe actually does.

		Measured rather than assumed, because two plausible assertions are both wrong.
		`get_doc(...).as_dict()` returns everything — permlevel is not applied by a
		server-side document load, so a test asserting that would claim a protection frappe
		does not offer. And permlevel does not *raise*: `get_list` silently **drops** the
		field from the result and `frappe.client.get` returns it as **None**. Redaction, not
		refusal. So redaction is what is asserted.
		"""
		name = self._make_log_with_keys()

		with acting_as(OPERATOR):
			rows = frappe.get_list(
				"Cloud Storage Backup Log",
				fields=["name", "db_key"],
				filters={"name": name},
				limit_page_length=5,
			)
			fetched = frappe.client.get("Cloud Storage Backup Log", name)

		self.assertTrue(rows, "no row came back, so the next assertion would be vacuous")
		self.assertNotIn("db_key", rows[0], "get_list must drop the field entirely")
		self.assertIsNone(fetched.get("db_key"), "client.get must null the field")
		self.assertIsNone(fetched.get("sha256_manifest"))

	def test_a_system_manager_does_get_the_artifact_key(self):
		"""The positive control. Without it, the redaction above could be the field simply
		never having been written."""
		name = self._make_log_with_keys()

		with acting_as(OUTSIDER, roles=("System Manager",)):
			fetched = frappe.client.get("Cloud Storage Backup Log", name)

		self.assertTrue(fetched.get("db_key"), "a System Manager must still see the key")

	def test_the_operator_can_still_see_that_backups_ran(self):
		"""The reason the DocPerm was kept rather than dropped: withholding *where the
		artifact is* should not blind the operator to *whether backups are working*."""
		self._make_log_with_keys()

		with acting_as(OPERATOR):
			rows = frappe.get_list(
				"Cloud Storage Backup Log",
				fields=["name", "status", "trigger", "started_at"],
				limit_page_length=5,
			)

		self.assertTrue(rows)
		self.assertEqual(rows[0]["status"], "Success")

	@staticmethod
	def _patch_module():
		from cloud_file_storage.patches.v0_6_0 import enforce_backup_credential_permlevel as patch

		return patch

	def test_the_patch_fails_loudly_on_every_half(self):
		"""The patch is the thing that keeps this true on a site nobody runs tests on."""
		import ast
		import inspect

		source = inspect.getsource(self._patch_module())
		throws = [
			node
			for node in ast.walk(ast.parse(source))
			if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "throw"
		]
		self.assertGreaterEqual(len(throws), 5, "the patch must fail loudly on every half")

	@refusal_guard
	def test_the_patch_refuses_an_operator_level_one_row(self):
		"""M-9's first gap, driven through the patch's own helper rather than grepped.

		The first version of this test searched the source for `OPERATOR_ROLE` and the word
		"grants" — both of which survive the mutation that empties the offender list, because
		they also appear in prose. So it is executed instead: a synthetic meta carrying an
		operator level-1 row must make the helper throw.
		"""
		patch = self._patch_module()
		meta = frappe._dict(
			permissions=[
				frappe._dict(role="System Manager", permlevel=1, read=1),
				frappe._dict(role="Cloud Storage Manager", permlevel=1, read=1),
			]
		)

		with self.assertRaises(frappe.ValidationError) as caught:
			patch._assert_shipped_permissions("Cloud Backup Settings", meta)
		self.assertIn("Cloud Storage Manager", str(caught.exception))

	def test_the_patch_accepts_a_correct_permission_set(self):
		"""The positive control: the refusal above is not refusing everything."""
		patch = self._patch_module()
		meta = frappe._dict(permissions=[frappe._dict(role="System Manager", permlevel=1, read=1)])

		patch._assert_shipped_permissions("Cloud Backup Settings", meta)

	@refusal_guard
	def test_the_patch_reads_custom_docperm_and_throws_on_one(self):
		"""M-9's second gap, and the one that fails silently — executed, not grepped.

		`patch_handler` sets `frappe.flags.in_patch` and `meta.py` returns early on it BEFORE
		applying the Custom DocPerm override, so inside a patch `meta.permissions` is always
		the shipped JSON. On any site where Role Permission Manager has touched these
		doctypes, the rows that actually govern live in `Custom DocPerm`.
		"""
		patch = self._patch_module()
		row = frappe.get_doc(
			{
				"doctype": "Custom DocPerm",
				"parent": "Cloud Storage Backup Log",
				"parenttype": "DocType",
				"parentfield": "permissions",
				"role": "Cloud Storage Manager",
				"permlevel": 1,
				"read": 1,
			}
		).insert(ignore_permissions=True)
		# Registered as a safety net for the failure path; the test also drops it explicitly
		# below so the removal — and the cache clear that makes it visible — is asserted
		# rather than assumed.
		self.addCleanup(self._drop_custom_docperm, row.name)

		with self.assertRaises(frappe.ValidationError) as caught:
			patch._assert_custom_permissions("Cloud Storage Backup Log")

		message = str(caught.exception)
		self.assertIn("Role Permission Manager", message)
		self.assertIn("will NOT help", message, "the remediation must not say 'edit the JSON'")

		# The row overrides the shipped DocPerms while it exists, so the meta must show it —
		# otherwise the next assertion, that removal restores the meta, proves nothing.
		self.assertTrue(self._operator_level_one_rows(), "the override should be visible now")

		self._drop_custom_docperm(row.name)

		self.assertEqual(
			self._operator_level_one_rows(),
			[],
			"removing the row must also clear the cached meta, or every later test in this "
			"process reads a permission set that is no longer in the database",
		)

	@staticmethod
	def _operator_level_one_rows() -> list:
		return [
			perm
			for perm in frappe.get_meta("Cloud Storage Backup Log").permissions
			if perm.role == "Cloud Storage Manager" and int(perm.permlevel or 0) == 1
		]

	@staticmethod
	def _drop_custom_docperm(name: str):
		"""Delete the row AND clear the cached meta. Both halves, or the row is gone from the
		database and still governing this process."""
		frappe.db.delete("Custom DocPerm", {"name": name})
		frappe.db.commit()
		frappe.clear_cache(doctype="Cloud Storage Backup Log")

	def test_the_custom_docperm_check_passes_when_there_is_no_such_row(self):
		self._patch_module()._assert_custom_permissions("Cloud Storage Backup Log")

	@refusal_guard
	def test_execute_actually_runs_both_permission_checks(self):
		"""Calling the helpers proves the helpers work, not that the patch runs them.

		Deleting `_assert_custom_permissions(doctype)` from `execute()` left every other test
		green, because they call the helper themselves — the same shape as the lifecycle
		backstop whose call site could be removed unnoticed. This spies on the real calls.
		"""
		from unittest.mock import patch as mock_patch

		patch_module = self._patch_module()
		with (
			mock_patch.object(patch_module, "_assert_custom_permissions") as custom,
			mock_patch.object(patch_module, "_assert_shipped_permissions") as shipped,
			mock_patch.object(patch_module, "_assert_fields_are_level_one") as fields,
		):
			patch_module.execute()

		checked = {call.args[0] for call in custom.call_args_list}
		self.assertEqual(checked, set(patch_module.CREDENTIAL_FIELDS))
		self.assertEqual({call.args[0] for call in shipped.call_args_list}, checked)
		self.assertEqual({call.args[0] for call in fields.call_args_list}, checked)

	def test_the_patch_forces_the_doctype_reload(self):
		"""Asserted on the call node, not on the source text.

		The first version grepped for `force=True`, which also appears in the comment
		explaining why it is there — so it survived the mutation that removed the keyword.
		"""
		import inspect

		tree = ast.parse(inspect.getsource(self._patch_module()))
		reloads = [
			node
			for node in ast.walk(tree)
			if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "reload_doc"
		]
		self.assertTrue(reloads, "the patch must reload the doctypes")
		for call in reloads:
			forced = [kw for kw in call.keywords if kw.arg == "force"]
			self.assertTrue(forced, "reload_doc must pass force=True")
			self.assertIs(forced[0].value.value, True)

	@refusal_guard
	def test_the_operator_row_no_longer_grants_export(self):
		"""L-8. Export is a third read route (`reportview.export_query`) and the redaction
		tests cover two. Removing the capability beats testing it: an operator does not need
		to download backup logs to see that backups are running, and a route not granted is a
		route that cannot regress.

		Asserted on the shipped JSON as well as the installed meta — a JSON edit does not move
		the meta until `bench migrate`, which is why the first version of this survived its
		mutation.
		"""
		schema = self._schema("cloud_storage_backup_log")
		json_rows = [p for p in schema["permissions"] if p.get("role") == "Cloud Storage Manager"]
		self.assertTrue(json_rows, "no operator row in the JSON; this assertion would be vacuous")
		for perm in json_rows:
			self.assertFalse(perm.get("export"), "the operator role must not export backup logs")

		meta_rows = [
			perm
			for perm in frappe.get_meta("Cloud Storage Backup Log").permissions
			if perm.role == "Cloud Storage Manager"
		]
		self.assertTrue(meta_rows)
		for perm in meta_rows:
			self.assertFalse(perm.export)


class TestTheGateRunsFirst(PermissionTestCase):
	"""`frappe.only_for` is the FIRST executable statement of every whitelisted backup
	endpoint (gate finding L-7).

	Why an AST check here when M-1 was about AST checks being insufficient: "this endpoint is
	System Manager only" is a **behavioural** property, so reading the source is a *proxy* for
	it — that was M-1, and the eight behavioural refusal tests above do that work. "`only_for`
	is the first executable statement" is a **syntactic** property, so reading the source
	reads it *directly*, with no proxy in between. Same instrument; wrong tool there, right
	tool here. Do not replace this with more positive controls: they would re-prove the
	behavioural property and leave the ordering one verified only by someone reading eight
	functions by hand, which is where it was before this existed.

	The endpoints are **discovered**, never listed. A hardcoded eight covers eight of nine the
	day a ninth lands — silently — which is the C10 allow-list failure in miniature and would
	be an unusually poor way to earn another instance of it.
	"""

	PACKAGE_DIR = "backup"

	def whitelisted_endpoints(self) -> list[tuple[str, str, ast.FunctionDef]]:
		"""Every `@frappe.whitelist()` function in `backup/`, found by walking the source."""
		import ast
		from pathlib import Path

		import cloud_file_storage

		root = Path(cloud_file_storage.__file__).parent / self.PACKAGE_DIR
		found = []
		for path in sorted(root.rglob("*.py")):
			tree = ast.parse(path.read_text(encoding="utf-8"))
			for node in ast.walk(tree):
				if not isinstance(node, ast.FunctionDef):
					continue
				for decorator in node.decorator_list:
					target = decorator.func if isinstance(decorator, ast.Call) else decorator
					if getattr(target, "attr", None) == "whitelist":
						found.append((path.name, node.name, node))
		return found

	@staticmethod
	def first_executable(function) -> ast.stmt | None:
		"""The first statement that is not the docstring."""
		import ast

		body = list(function.body)
		if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
			body = body[1:]
		return body[0] if body else None

	@staticmethod
	def is_only_for(statement) -> bool:
		import ast

		return (
			isinstance(statement, ast.Expr)
			and isinstance(statement.value, ast.Call)
			and getattr(statement.value.func, "attr", None) == "only_for"
		)

	@refusal_guard
	def test_the_walk_finds_the_endpoints(self):
		"""A walk that matches nothing makes every assertion below vacuously true."""
		endpoints = self.whitelisted_endpoints()
		self.assertGreaterEqual(
			len(endpoints), 8, f"expected at least 8 whitelisted backup endpoints, found {endpoints}"
		)

	@refusal_guard
	def test_only_for_is_the_first_executable_statement_of_every_endpoint(self):
		endpoints = self.whitelisted_endpoints()
		self.assertTrue(endpoints, "no endpoints discovered")

		for module, name, function in endpoints:
			with self.subTest(endpoint=f"{module}::{name}"):
				statement = self.first_executable(function)
				self.assertIsNotNone(statement, f"{module}::{name} has an empty body")
				self.assertTrue(
					self.is_only_for(statement),
					f"{module}::{name} does something before its role gate — anything above "
					"`frappe.only_for` runs for a caller who is about to be refused",
				)

	def test_every_endpoint_names_system_manager(self):
		for module, name, function in self.whitelisted_endpoints():
			with self.subTest(endpoint=f"{module}::{name}"):
				statement = self.first_executable(function)
				self.assertEqual(statement.value.args[0].value, "System Manager")


class TestTheSuiteLeavesNothingBehind(PermissionTestCase):
	"""The persistent footprint this suite creates, asserted rather than assumed.

	The settings-singleton comparison the phase gate leans on has two limits, and both were
	found rather than designed around. It compares runs to **each other**, never to a clean
	install, so a leak that damages its own baseline reads as stable. And it watches two
	`tabSingles` rows while almost everything a suite can damage — table rows, users,
	DocPerms, cached metas — was never in its scope at all.

	These assertions cover the second limit for this suite's own footprint. They cannot prove
	the absence of a leak; they pin the three that a read of the suite actually found.
	"""

	@refusal_guard
	def test_no_custom_docperm_survives_on_the_backup_doctypes(self):
		"""A Custom DocPerm overrides the shipped rows entirely, so one left behind silently
		replaces the permission set every later test reads."""
		for doctype in ("Cloud Backup Settings", "Cloud Storage Backup Log"):
			with self.subTest(doctype=doctype):
				self.assertEqual(frappe.get_all("Custom DocPerm", filters={"parent": doctype}), [])

	@refusal_guard
	def test_drop_test_user_actually_removes_the_user(self):
		"""The teardown's own behaviour, on a throwaway account.

		The class cleanup cannot be asserted from inside the class — the fixture users must
		exist while its tests run — so the function it calls is tested directly and the
		registration is asserted separately.
		"""
		from cloud_file_storage.tests.permission_utils import drop_test_user, ensure_test_user

		throwaway = "cfs-p6-throwaway@example.invalid"
		ensure_test_user(throwaway, roles=("Cloud Storage Manager",))
		self.assertTrue(frappe.db.exists("User", throwaway))

		drop_test_user(throwaway)

		self.assertFalse(frappe.db.exists("User", throwaway))
		self.assertEqual(frappe.get_all("Has Role", filters={"parent": throwaway}), [])

	def test_the_fixture_users_are_registered_for_teardown(self):
		"""And that the class actually asks for it."""
		import inspect

		source = inspect.getsource(PermissionTestCase.setUpClass)
		self.assertIn("addClassCleanup(drop_test_user, OUTSIDER)", source)
		self.assertIn("addClassCleanup(drop_test_user, OPERATOR)", source)

	@refusal_guard
	def test_the_fixture_users_hold_only_their_declared_roles(self):
		"""`acting_as` grants a role for the duration and removes it afterwards. If a removal
		ever half-works the user stays elevated, `ensure_test_user` finds them present on the
		next run and adds nothing, and every refusal test above is then asserting against a
		user who holds the role it expects them to lack."""
		self.assertNotIn("System Manager", frappe.get_roles(OUTSIDER))
		self.assertNotIn("System Manager", frappe.get_roles(OPERATOR))
		self.assertIn("Cloud Storage Manager", frappe.get_roles(OPERATOR))
		self.assertNotIn("Cloud Storage Manager", frappe.get_roles(OUTSIDER))

	@refusal_guard
	def test_role_removal_is_not_silently_suppressed(self):
		"""The harness fails loudly on a cleanup it cannot complete.

		Asserted on the **call node**, not on the source text. The first version of this test
		searched the cleanup block for the string "suppress" — and failed, because the comment
		explaining that the removal is *not* suppressed contains the word. That is the same
		defect this project has now recorded several times: a grep matching the prose that
		explains the code. Written down here rather than only in DECISIONS, because this test
		is exactly where someone would reintroduce it.

		The property it guards is a silence: a suppressed failure and a successful removal are
		indistinguishable until a later run behaves oddly, which is why it is worth asserting
		structurally at all.
		"""
		import inspect
		import textwrap

		from cloud_file_storage.tests import permission_utils

		tree = ast.parse(textwrap.dedent(inspect.getsource(permission_utils.acting_as.__wrapped__)))
		removals = [
			node
			for node in ast.walk(tree)
			if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "remove_roles"
		]
		self.assertTrue(removals, "acting_as must remove the roles it granted")

		# Any `with` block anywhere in the function that swallows exceptions would do it.
		swallowing = [
			node
			for node in ast.walk(tree)
			if isinstance(node, ast.With)
			and any(
				getattr(getattr(item.context_expr, "func", None), "attr", None) == "suppress"
				for item in node.items
			)
		]
		self.assertEqual(swallowing, [], "a suppressed cleanup leaves a user elevated and says nothing")
