"""T-LEGACY / A10 — the legacy endpoint keeps every 0.2.x URL alive after the rename.

Those URLs live in business fields, emails and bookmarks, and `tabFile.file_url` is never
mass-rewritten, so this endpoint has to resolve both generations of key forever: the
deprecated `File.s3_object_key` locator (A13) and the content-addressed `s3_key` this
runtime writes.
"""

import frappe

from cloud_file_storage.api import compat
from cloud_file_storage.tests.utils import CloudStorageTestCase, set_mode

PROBE_USER = "cfs_compat_probe@test.local"


class TestLegacyGenerateFile(CloudStorageTestCase):
	def setUp(self):
		super().setUp()
		frappe.local.response = frappe._dict()
		self.addCleanup(frappe.set_user, "Administrator")

	def _ensure_probe_user(self):
		if not frappe.db.exists("User", PROBE_USER):
			user = frappe.new_doc("User")
			user.email = PROBE_USER
			user.first_name = "CFS Compat Probe"
			user.send_welcome_email = 0
			user.insert(ignore_permissions=True)
			user.add_roles("Desk User")

	def test_a_runtime_object_key_resolves(self):
		doc = self.make_file(file_name="compat-runtime.txt", content="runtime key")
		cso = self.cso_of(doc)

		compat.legacy_generate_file(key=cso.s3_key, file_name="compat-runtime.txt")

		self.assertEqual(frappe.local.response["type"], "redirect")
		self.assertIn(cso.s3_key, frappe.local.response["location"])

	def test_a_deprecated_s3_object_key_still_resolves(self):
		"""The fork wrote filename-derived keys; those rows must keep working."""
		doc = self.make_file(file_name="compat-legacy.txt", content="legacy key")
		legacy_key = "2026/05/06/File/AB12CD34_compat-legacy.txt"
		frappe.db.set_value("File", doc.name, "s3_object_key", legacy_key, update_modified=False)

		compat.legacy_generate_file(key=legacy_key, file_name="compat-legacy.txt")

		self.assertEqual(frappe.local.response["type"], "redirect")

	def test_the_read_permission_gate_runs_before_any_url_is_issued(self):
		doc = self.make_file(file_name="compat-secret.txt", content="classified")
		cso = self.cso_of(doc)
		self._ensure_probe_user()

		frappe.set_user(PROBE_USER)
		with self.assertRaises(frappe.PermissionError):
			compat.legacy_generate_file(key=cso.s3_key, file_name="compat-secret.txt")
		self.assertEqual(self.store.presign_calls, [])

	def _refusal(self, **kwargs) -> tuple[type, str]:
		"""Drive the endpoint into a refusal and report exactly what came back."""
		with self.assertRaises(Exception) as caught:  # noqa: B017 - the type IS the assertion
			compat.legacy_generate_file(**kwargs)
		return type(caught.exception), str(caught.exception)

	def test_every_refusal_is_indistinguishable(self):
		"""A24 — the endpoint must not be a file-existence oracle.

		The port this replaces answered `DoesNotExistError` for an unknown key and a
		permission error for a known one, so anyone who could guess a key could ask whether
		it existed. All four refusals now have to be the same exception with the same
		message: an unknown key, no key at all, a real key the caller may not read, and a
		real key pinned by a `fid` the caller may not read.
		"""
		doc = self.make_file(file_name="compat-oracle.txt", content="classified")
		cso = self.cso_of(doc)
		self._ensure_probe_user()
		frappe.set_user(PROBE_USER)

		unknown = self._refusal(key="no/such/key/anywhere")
		empty = self._refusal(key=None)
		unreadable = self._refusal(key=cso.s3_key)
		frappe.form_dict.fid = doc.name
		try:
			pinned = self._refusal(key=cso.s3_key)
		finally:
			frappe.form_dict.pop("fid", None)

		self.assertEqual(unknown[0], frappe.PermissionError)
		self.assertEqual({unknown, empty, unreadable, pinned}, {unknown})
		self.assertEqual(self.store.presign_calls, [])

	def test_any_one_readable_row_grants_access(self):
		"""A24 — core's any-one-readable rule, applied across the rows sharing a key.

		Two File rows point at one object; the probe user owns the second. Core's own
		`find_file_by_url` gate works this way (`file/utils.py:437-443`), and the legacy
		endpoint follows it rather than inventing a stricter or looser rule.
		"""
		mine = self.make_file(file_name="compat-shared.txt", content="shared bytes")
		cso = self.cso_of(mine)
		self._ensure_probe_user()

		frappe.set_user(PROBE_USER)
		theirs = self.track(
			frappe.get_doc(
				{
					"doctype": "File",
					"file_name": "compat-shared.txt",
					"file_url": mine.file_url,
					"is_private": mine.is_private,
					"content_hash": mine.content_hash,
					"file_size": mine.file_size,
					"cloud_storage_object": cso.name,
				}
			).insert(ignore_permissions=True)
		)
		# `set_user_and_timestamp` stamps the session user as owner, which is the whole point.
		self.assertEqual(frappe.db.get_value("File", theirs.name, "owner"), PROBE_USER)

		compat.legacy_generate_file(key=cso.s3_key)
		self.assertEqual(frappe.local.response["type"], "redirect")

	def test_the_endpoint_writes_exactly_one_access_log_row(self):
		"""Design §11: the legacy endpoint audits identically to /private/files/..."""
		from cloud_file_storage.tests.request_utils import access_log_count

		doc = self.make_file(file_name="compat-audit.txt", content="audited")
		cso = self.cso_of(doc)

		before = access_log_count(doc.name)
		compat.legacy_generate_file(key=cso.s3_key)
		self.assertEqual(access_log_count(doc.name) - before, 1)

	def test_a_refused_request_writes_no_access_log_row(self):
		from cloud_file_storage.tests.request_utils import access_log_count

		doc = self.make_file(file_name="compat-audit-denied.txt", content="audited")
		cso = self.cso_of(doc)
		self._ensure_probe_user()

		frappe.set_user(PROBE_USER)
		before = access_log_count(doc.name)
		with self.assertRaises(frappe.PermissionError):
			compat.legacy_generate_file(key=cso.s3_key)
		self.assertEqual(access_log_count(doc.name), before)

	def test_the_ttl_comes_from_settings(self):
		set_mode("S3_ONLY", private_presign_ttl=120)
		doc = self.make_file(file_name="compat-ttl.txt", content="ttl bytes")
		cso = self.cso_of(doc)

		compat.legacy_generate_file(key=cso.s3_key)

		self.assertEqual(self.store.presign_calls[-1]["ttl"], 120)

	def test_the_endpoint_is_whitelisted_and_not_open_to_guests(self):
		self.assertIn(compat.legacy_generate_file, frappe.whitelisted)
		self.assertNotIn(compat.legacy_generate_file, frappe.guest_methods)

	def test_this_app_registers_no_whitelisted_method_override(self):
		"""The app must not override another app's whitelisted method.

		The Frappe Cloud marketplace audit rejects it, so this is now a constraint rather
		than a preference. Asserted through the same lookup `handler.execute_cmd` uses
		(`handler.py:64-68`), so it fails if the remap is reintroduced by any route --
		hooks.py, a patch, or another app on the bench shipping it on this app's behalf.
		"""
		cmd = "frappe_s3_attachment.controller.generate_file"

		overrides = frappe.get_hooks("override_whitelisted_methods", {}).get(cmd, [])
		ours = [o for o in overrides if o.startswith("cloud_file_storage.")]

		self.assertEqual(ours, [], "this app must not remap the fork endpoint; see hooks.py")

	def test_the_legacy_endpoint_survives_as_an_app_owned_method(self):
		"""Removing the remap must not remove the capability.

		An operator adopting a 0.2.x site can still point the fork path at this handler from
		their own app or site config. If this endpoint is deleted outright that recovery is
		gone, so its existence and its gate are asserted here rather than left implied.
		"""
		self.assertIn(
			compat.legacy_generate_file,
			frappe.whitelisted,
			"the endpoint must stay whitelisted, or an operator cannot remap the fork path to it",
		)
		self.assertNotIn(
			compat.legacy_generate_file,
			frappe.guest_methods,
			"the endpoint must never be reachable by a guest",
		)


class TestDeprecatedHookDetector(CloudStorageTestCase):
	"""A24 / rename ADR R5 — `s3_key_generator` is not honoured, and says so.

	The hook let a 0.2.x site compute object keys from the file name. Keys here are
	content-addressed and ARE the object's identity, so honouring it would break dedup,
	refcounting and GC at once — and silently ignoring it would be worse than refusing.
	"""

	@staticmethod
	def _with_deprecated_hook(implementations=("some_app.keys.build",)):
		"""Patch only `s3_key_generator`; everything else keeps the real hooks.

		A blanket `return_value` here breaks `override_doctype_class` and every other
		consumer, which fails in places that have nothing to do with the detector.
		"""
		from unittest.mock import patch

		real_get_hooks = frappe.get_hooks

		def fake_get_hooks(hook=None, *args, **kwargs):
			if hook == "s3_key_generator":
				return list(implementations)
			return real_get_hooks(hook, *args, **kwargs)

		return patch.object(frappe, "get_hooks", side_effect=fake_get_hooks)

	def test_nothing_is_reported_when_no_site_defines_the_hook(self):
		self.assertEqual(compat.detect_deprecated_hooks(log=False), [])

	def test_a_defined_hook_is_reported(self):
		with self._with_deprecated_hook():
			findings = compat.detect_deprecated_hooks(log=False)

		self.assertEqual(len(findings), 1)
		self.assertEqual(findings[0]["hook"], "s3_key_generator")
		self.assertEqual(findings[0]["implementations"], ["some_app.keys.build"])
		self.assertIn("content-addressed", findings[0]["message"])

	def test_the_hook_is_never_actually_called(self):
		"""R5 — a shim that runs site code and discards the result is the worst option."""
		import ast
		import inspect

		source = inspect.getsource(compat)
		tree = ast.parse(source)
		calls = [
			node
			for node in ast.walk(tree)
			if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "get_attr"
		]
		self.assertEqual(calls, [], "compat.py must not resolve a deprecated hook to call it")

	def test_test_connection_surfaces_the_findings(self):
		with self._with_deprecated_hook():
			result = compat.test_connection()

		self.assertEqual(len(result["deprecated_hooks"]), 1)
		self.assertEqual(result["deprecated_hooks"][0]["hook"], "s3_key_generator")


class TestTestConnection(CloudStorageTestCase):
	def test_it_is_restricted_to_system_managers(self):
		"""Asserted structurally: `frappe.only_for` is a deliberate no-op under `in_test`."""
		import ast
		import inspect

		tree = ast.parse(inspect.getsource(compat.test_connection.__wrapped__))
		guards = [
			node.args[0].value
			for node in ast.walk(tree)
			if isinstance(node, ast.Call)
			and getattr(node.func, "attr", None) == "only_for"
			and node.args
			and isinstance(node.args[0], ast.Constant)
		]
		self.assertEqual(guards, ["System Manager"])

	def test_it_reports_each_step_rather_than_throwing(self):
		result = compat.test_connection()
		self.assertEqual(result["bucket"], "cfs-unit-test-bucket")
		self.assertTrue(result["steps"][0]["ok"])

	def test_a_failure_is_reported_not_hidden(self):
		from cloud_file_storage.storage.exceptions import CloudStorageTransportError

		self.store.fail_with = CloudStorageTransportError("bucket unreachable")
		try:
			result = compat.test_connection()
		finally:
			self.store.fail_with = None

		self.assertFalse(result["steps"][0]["ok"])
		self.assertIn("bucket unreachable", result["steps"][0]["error"])
