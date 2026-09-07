"""`serving/runtime_patches` — installation, reversal, A36, and the dev statics shim."""

import ast
import os
import sys
import types
from pathlib import Path
from unittest.mock import patch

import frappe
import frappe.app  # noqa: F401 - the statics loader reads `frappe.app._site` at call time
import frappe.utils.file_manager as file_manager_module
import frappe.utils.response as response_module
from frappe.tests.utils import FrappeTestCase
from werkzeug.exceptions import NotFound

from cloud_file_storage.serving import private, runtime_patches
from cloud_file_storage.tests.utils import CloudStorageTestCase, set_mode

_ABSENT = object()


def _ic_utils_source() -> Path:
	"""Where India Compliance's `gst_india.utils` lives on THIS bench — computed, never imported.

	Derived, never hardcoded. The previous version was an absolute path to one developer's
	bench, so in CI it simply did not exist and the consistency branch below correctly reported
	"IC is installed but I cannot find its source" — which is what an anti-vacuity guard is for,
	and how this was found. It failed the L3 ecosystem job on `dcb2ada`.

	**Pure path arithmetic, deliberately.** An earlier version of this used
	`frappe.get_app_path`, whose docstring here claimed it "does not import IC". That was false:
	`get_app_path` -> `get_pymodule_path` -> `get_module` -> `importlib.import_module`, so it
	imports the package at test-module import time. Two consequences followed, both real:
	the real `india_compliance` landed in `sys.modules` before `_install_fake_ic` replaced it,
	and `_remove_fake_modules` then *deleted* the entry rather than restoring it, evicting the
	real package for the rest of the process; and the `except` that was meant to mean "app
	absent" also swallowed genuine import failures, so a present-but-broken IC produced an
	existing fallback path and the test passed anyway.

	`frappe.__file__` is `<bench>/apps/frappe/frappe/__init__.py`, so `parents[2]` is
	`<bench>/apps`. No import, nothing to swallow, and `.exists()` is False when the app is
	genuinely absent.
	"""
	apps_dir = Path(frappe.__file__).resolve().parents[2]
	return apps_dir / "india_compliance" / "india_compliance" / "gst_india" / "utils" / "__init__.py"


IC_UTILS_SOURCE = _ic_utils_source()


class TestPatchInstallation(FrappeTestCase):
	def setUp(self):
		super().setUp()
		self.addCleanup(runtime_patches.uninstall)

	def test_installing_replaces_the_dispatch_target_and_uninstalling_restores_it(self):
		core_function = response_module.download_private_file

		runtime_patches.ensure_installed()
		self.assertIs(response_module.download_private_file, private.download_private_file_cloud)
		self.assertIs(runtime_patches.original_download_private_file(), core_function)

		runtime_patches.uninstall()
		self.assertIs(response_module.download_private_file, core_function)

	def test_installation_is_idempotent(self):
		"""`before_request` calls this on every request; a second install must not capture
		our own function as "the original" and make the patch recurse."""
		core_function = response_module.download_private_file

		runtime_patches.ensure_installed()
		runtime_patches.ensure_installed()
		runtime_patches.ensure_installed()

		self.assertIs(runtime_patches.original_download_private_file(), core_function)
		runtime_patches.uninstall()
		self.assertIs(response_module.download_private_file, core_function)

	def test_get_file_path_is_patched_too(self):
		core_function = file_manager_module.get_file_path

		runtime_patches.ensure_installed()
		self.assertIs(file_manager_module.get_file_path, runtime_patches.get_file_path_cloud)

		runtime_patches.uninstall()
		self.assertIs(file_manager_module.get_file_path, core_function)

	def test_uninstall_on_a_clean_process_is_a_no_op(self):
		core_function = response_module.download_private_file
		runtime_patches.uninstall()
		self.assertIs(response_module.download_private_file, core_function)


class TestGetFilePathPatch(CloudStorageTestCase):
	"""A36 — the bypass IC's GSTR ingest and erpnext's EDI readers go through."""

	MODE = "S3_ONLY"

	def setUp(self):
		super().setUp()
		runtime_patches.ensure_installed()
		self.addCleanup(runtime_patches.uninstall)

	def test_a_cloud_only_file_resolves_to_a_readable_path(self):
		doc = self.make_file(file_name="a36-gstr.json", content=b'{"gstr": 1}', is_private=1)
		self.assertFalse(os.path.exists(self.local_path(doc)))

		path = file_manager_module.get_file_path(doc.file_name)

		self.assertTrue(os.path.exists(path))
		with open(path, "rb") as handle:
			self.assertEqual(handle.read(), b'{"gstr": 1}')

	def test_frappe_get_file_json_reads_it(self):
		"""The actual shape of `india_compliance/gst_india/utils/__init__.py:672`."""
		self.make_file(file_name="a36-json.json", content=b'{"value": 42}', is_private=1)

		self.assertEqual(
			frappe.get_file_json(file_manager_module.get_file_path("a36-json.json"))["value"], 42
		)

	def test_a_file_url_argument_resolves(self):
		"""erpnext's `code_list_import.py:30` passes a `file_url`, not a name."""
		doc = self.make_file(file_name="a36-url.txt", content=b"edi bytes", is_private=1)

		path = file_manager_module.get_file_path(doc.file_url)

		self.assertTrue(os.path.exists(path))

	def test_a_file_row_name_resolves(self):
		doc = self.make_file(file_name="a36-name.txt", content=b"by name", is_private=1)

		path = file_manager_module.get_file_path(doc.name)

		self.assertTrue(os.path.exists(path))

	def test_an_unknown_name_falls_back_to_core_behaviour(self):
		expected = runtime_patches.original_get_file_path()("a36-not-a-file.txt")

		self.assertEqual(file_manager_module.get_file_path("a36-not-a-file.txt"), expected)

	def test_traversal_is_left_to_core(self):
		self.assertIsNone(file_manager_module.get_file_path("../../etc/passwd"))

	def test_a_local_only_file_is_left_to_core(self):
		set_mode("LOCAL_ONLY")
		doc = self.make_file(file_name="a36-local.txt", content=b"local", is_private=1)

		patched = file_manager_module.get_file_path(doc.file_name)
		core = runtime_patches.original_get_file_path()(doc.file_name)

		self.assertEqual(patched, core)

	def test_the_file_url_arm_only_matches_a_canonical_url(self):
		"""A36 widened core's matcher; the widening must not reach arbitrary text.

		`get_file_path` performs no permission check (core's does not either) and this
		replacement materializes whatever it resolves, so every extra row the matcher can
		reach is a row a caller could be steered into reading. The `file_url` arm exists for
		erpnext's `code_list_import.py:30`, which passes a canonical URL — and only that.
		"""
		doc = self.make_file(file_name="a36-narrow.txt", content=b"narrow", is_private=1)
		self.assertIsNotNone(
			runtime_patches._find_file_doc(doc.file_url), "precondition: a canonical URL resolves"
		)
		frappe.db.set_value("File", doc.name, "file_url", "a36 arbitrary caller text", update_modified=False)

		self.assertIsNone(runtime_patches._find_file_doc("a36 arbitrary caller text"))

	def test_the_handed_out_path_is_registered_for_write_back(self):
		"""A4 — the path IC writes through has to be tracked, or the edit is lost."""
		from cloud_file_storage.cache import materialize

		doc = self.make_file(file_name="a36-writeback.json", content=b'{"v": 1}', is_private=1)

		path = file_manager_module.get_file_path(doc.file_name)

		self.assertIn(path, materialize.tracked_paths(), "get_file_path handed out an untracked path")
		self.assertEqual(materialize.tracked_paths()[path]["file"], doc.name)


class TestIndiaComplianceImportBoundPatch(FrappeTestCase):
	"""A36's second half: IC binds its own copy at import time, so ours must too."""

	def setUp(self):
		super().setUp()
		self.addCleanup(runtime_patches.uninstall)
		self.addCleanup(self._remove_fake_modules)
		self._snapshot_ic_modules()

	IC_MODULE_NAMES = (
		"india_compliance.gst_india.utils",
		"india_compliance.gst_india",
		"india_compliance",
	)

	def _snapshot_ic_modules(self):
		"""Restore whatever was in `sys.modules`, rather than deleting our stand-ins.

		Deleting evicts a REAL `india_compliance` if one was imported earlier in the process —
		on L3 it is — leaving every later importer to re-import it, or to fail. Snapshot and
		put back exactly what was there.
		"""
		saved = {name: sys.modules.get(name, _ABSENT) for name in self.IC_MODULE_NAMES}

		def restore():
			for name, previous in saved.items():
				if previous is _ABSENT:
					if getattr(sys.modules.get(name), "_cfs_fake", False):
						del sys.modules[name]
				else:
					sys.modules[name] = previous

		self.addCleanup(restore)

	@staticmethod
	def _remove_fake_modules():
		for name in ("india_compliance.gst_india.utils", "india_compliance.gst_india", "india_compliance"):
			if getattr(sys.modules.get(name), "_cfs_fake", False):
				del sys.modules[name]

	def test_the_ic_source_path_is_derived_from_this_bench(self):
		"""The guard for the fix, not for the product — a hardcoded path is silently vacuous.

		`IC_UTILS_SOURCE` used to be an absolute path to one developer's bench. Anywhere else
		it does not exist, so the `.exists()` branch below never runs and the assertion that
		IC still import-binds `get_file_path` is never made — the test passes by not testing.
		In the L3 ecosystem job it did worse: the consistency branch fired and failed the job,
		which is the guard working correctly and is how this was found.

		Asserting the path sits under the running bench's `apps/` makes a re-hardcoded path
		fail here rather than degrade into a silent skip somewhere else.
		"""
		self.assertEqual(
			IC_UTILS_SOURCE,
			_ic_utils_source(),
			"IC_UTILS_SOURCE is not what the derivation produces — it has been pinned to a "
			"literal, and on any other checkout the source assertion below will never run",
		)

		# The prefix check alone could not fail HERE: the old hardcoded value
		# `/home/user/v15/apps/india_compliance/...` sits under this bench's apps directory and
		# satisfied it. A guard that cannot fail on the machine where it is written is the
		# vacuity it exists to prevent, so the module text is checked for absolute literals too.
		# **Parsed, not grepped.** Two weaker versions of this guard failed to catch the exact
		# defect it exists for, and both failures are instructive:
		#
		#  - a prefix check ("is it under this bench's apps dir?") accepted the original
		#    hardcoded `/home/user/v15/apps/india_compliance/...`, because on THIS bench it is;
		#  - the equality check above cannot distinguish them here either, for the same reason —
		#    the literal and the derivation produce the same string on the machine where the
		#    literal was written. It still earns its place: it fails on every other checkout.
		#  - a text scan matched its own search string, and then scanned the wrong region.
		#
		# An AST walk has none of those problems: it sees no comments, no docstrings, and cannot
		# match itself, because the thing it looks for is a `Path(...)` call whose argument is an
		# absolute string literal — which this test does not contain.
		#
		# **It is not exhaustive, and should not be described as such.** It misses `Path(_ROOT)`
		# where `_ROOT` is a module constant, and `pathlib.Path("/abs")` (an attribute call has
		# no `.id`). It catches the shape that actually shipped, which is what it is for and what
		# the mutation proves; a guard aimed at one real defect is worth more than a general one
		# nobody can check.
		tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
		offenders = []
		for node in ast.walk(tree):
			if not isinstance(node, ast.Call):
				continue
			if getattr(node.func, "id", None) != "Path":
				continue
			for arg in node.args:
				if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
					if arg.value.startswith("/"):
						offenders.append(f"line {node.lineno}: Path({arg.value!r})")

		self.assertEqual(
			offenders,
			[],
			"absolute path literal(s) in this module — a hardcoded bench path makes the source "
			f"assertion below silently unreachable anywhere else: {offenders}",
		)

	def test_the_real_india_compliance_still_binds_its_own_copy(self):
		"""The reason the second patch exists. Read off disk — IC is never imported here."""
		if IC_UTILS_SOURCE.exists():
			self.assertIn(
				"from frappe.utils.file_manager import get_file_path",
				IC_UTILS_SOURCE.read_text(encoding="utf-8"),
				"IC no longer import-binds get_file_path; the second patch may be removable",
			)
		else:
			self.assertNotIn("india_compliance", frappe.get_installed_apps())

	def test_an_installed_ic_gets_its_bound_copy_patched_and_restored(self):
		original = object()
		utils_module = self._install_fake_ic(original)

		with patch.object(frappe, "get_installed_apps", return_value=["frappe", "india_compliance"]):
			runtime_patches.ensure_installed()

		self.assertIs(utils_module.get_file_path, runtime_patches.get_file_path_cloud)

		runtime_patches.uninstall()
		self.assertIs(utils_module.get_file_path, original)

	def test_a_site_without_ic_does_not_decide_for_the_next_site_that_has_it(self):
		"""The multi-site worker: two requests, two sites, one interpreter.

		`_state["installed"]` is per PROCESS while "is India Compliance installed" is per
		SITE, so a one-shot install lets the first site to serve a request decide for every
		site after it — and the failure is silent: IC's GSTR ingest just opens a local path
		that does not exist on an S3_ONLY site.
		"""
		original = object()
		utils_module = self._install_fake_ic(original)

		with patch.object(frappe, "get_installed_apps", return_value=["frappe"]):
			runtime_patches.ensure_installed()
		self.assertIs(utils_module.get_file_path, original, "precondition: site 1 has no IC to patch")

		with patch.object(frappe, "get_installed_apps", return_value=["frappe", "india_compliance"]):
			runtime_patches.ensure_installed()

		self.assertIs(utils_module.get_file_path, runtime_patches.get_file_path_cloud)

		runtime_patches.uninstall()
		self.assertIs(utils_module.get_file_path, original)

	def test_ic_is_left_alone_when_it_is_not_installed(self):
		original = object()
		utils_module = self._install_fake_ic(original)

		with patch.object(frappe, "get_installed_apps", return_value=["frappe"]):
			runtime_patches.ensure_installed()

		self.assertIs(utils_module.get_file_path, original)

	@staticmethod
	def _install_fake_ic(get_file_path):
		package = types.ModuleType("india_compliance")
		package._cfs_fake = True
		gst_india = types.ModuleType("india_compliance.gst_india")
		gst_india._cfs_fake = True
		utils_module = types.ModuleType("india_compliance.gst_india.utils")
		utils_module._cfs_fake = True
		utils_module.get_file_path = get_file_path

		package.gst_india = gst_india
		gst_india.utils = utils_module
		sys.modules["india_compliance"] = package
		sys.modules["india_compliance.gst_india"] = gst_india
		sys.modules["india_compliance.gst_india.utils"] = utils_module
		return utils_module


class TestDevStaticsShim(FrappeTestCase):
	"""A24 — `bench serve` 404s a public miss before any renderer runs, unless shimmed.

	werkzeug falls through to the wrapped app only when a loader *returns* no file loader
	(`SharedDataMiddleware.__call__`); frappe's loader raises `NotFound`, which becomes the
	response. These tests drive a real `StaticDataMiddleware` instance.
	"""

	def _middleware(self):
		from frappe.middlewares import StaticDataMiddleware

		def dummy_app(environ, start_response):  # pragma: no cover - never reached here
			return []

		middleware = StaticDataMiddleware(
			dummy_app, {"/files": str(os.path.abspath(frappe.local.sites_path))}
		)
		middleware.environ = {"HTTP_HOST": frappe.local.site}
		return middleware

	def test_the_unshimmed_loader_raises_on_a_miss(self):
		"""The premise. If frappe ever returns instead of raising, the shim is dead code."""
		_prefix, loader = self._middleware().exports[0]

		with self.assertRaises(NotFound):
			loader("cfs-definitely-not-here.txt")

	def test_the_shimmed_loader_reports_not_handled_instead(self):
		_prefix, loader = self._middleware().exports[0]

		wrapped = runtime_patches.falling_through_loader(loader)

		self.assertEqual(wrapped("cfs-definitely-not-here.txt"), (None, None))
		self.assertTrue(wrapped.cfs_falls_through)

	def test_the_shim_still_serves_a_file_that_does_exist(self):
		path = Path(frappe.get_site_path("public", "files", "cfs-statics-probe.txt"))
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_bytes(b"probe")
		self.addCleanup(path.unlink, missing_ok=True)

		_prefix, loader = self._middleware().exports[0]
		wrapped = runtime_patches.falling_through_loader(loader)

		name, file_loader = wrapped("cfs-statics-probe.txt")
		self.assertEqual(name, "cfs-statics-probe.txt")
		self.assertIsNotNone(file_loader)

	def test_the_middleware_is_found_through_a_wsgi_chain(self):
		middleware = self._middleware()
		outer = types.SimpleNamespace(app=types.SimpleNamespace(app=middleware))

		self.assertIs(runtime_patches.find_static_data_middleware(outer), middleware)

	def test_a_chain_without_the_middleware_returns_none(self):
		chain = types.SimpleNamespace(app=types.SimpleNamespace(app=None))

		self.assertIsNone(runtime_patches.find_static_data_middleware(chain))

	def test_the_shim_is_not_installed_outside_a_dev_server(self):
		previous = getattr(frappe.local, "dev_server", 0)
		frappe.local.dev_server = 0
		self.addCleanup(setattr, frappe.local, "dev_server", previous)
		self.addCleanup(runtime_patches.uninstall)

		runtime_patches.ensure_installed()

		self.assertIsNone(runtime_patches._state["dev_statics"])

	def test_the_shim_is_installed_on_a_dev_server_and_removed_again(self):
		"""...and the dev branch really does wrap the instance's loaders."""
		middleware = self._middleware()
		previous_dev_server = getattr(frappe.local, "dev_server", 0)
		previous_application = frappe.app.application

		frappe.local.dev_server = 1
		frappe.app.application = types.SimpleNamespace(app=middleware)
		self.addCleanup(setattr, frappe.app, "application", previous_application)
		self.addCleanup(setattr, frappe.local, "dev_server", previous_dev_server)
		self.addCleanup(runtime_patches.uninstall)

		original_exports = list(middleware.exports)
		runtime_patches.ensure_installed()

		self.assertTrue(all(getattr(loader, "cfs_falls_through", False) for _p, loader in middleware.exports))

		runtime_patches.uninstall()
		self.assertEqual(middleware.exports, original_exports)

	def test_installing_twice_does_not_wrap_the_loaders_twice(self):
		middleware = self._middleware()
		previous_dev_server = getattr(frappe.local, "dev_server", 0)
		previous_application = frappe.app.application

		frappe.local.dev_server = 1
		frappe.app.application = types.SimpleNamespace(app=middleware)
		self.addCleanup(setattr, frappe.app, "application", previous_application)
		self.addCleanup(setattr, frappe.local, "dev_server", previous_dev_server)
		self.addCleanup(runtime_patches.uninstall)

		runtime_patches.ensure_installed()
		wrapped_once = list(middleware.exports)
		runtime_patches.uninstall()
		middleware.exports = wrapped_once  # simulate a second process-level install attempt
		runtime_patches.ensure_installed()

		self.assertEqual(middleware.exports, wrapped_once)
