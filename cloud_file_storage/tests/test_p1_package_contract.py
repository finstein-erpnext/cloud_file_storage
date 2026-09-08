"""P1 package contract — asserted against the specs, not against install.py's mocks.

Every assertion here is derived from a binding document:

* ``docs/PLAN.md`` §B (the P1 row) and §C (hard invariants),
* ``docs/adr/amendments-register.md`` A10, A12, A13, A22, A24, A34,
* ``docs/ACCEPTANCE_GATES.md`` F6,
* ``docs/INVARIANTS.md`` HARD INVARIANTS 2, 6 and 10.

`test_install.py` exercises install.py through mocks, so it can only prove that the
installer calls what it calls. These tests read the *shipped artifacts* — the doctype
JSON, ``modules.txt``, ``patches.txt``, ``hooks.py``, ``pyproject.toml``, the locale
directory — and check them against the frozen specification. They need no site.
"""

import ast
import importlib
import json
import re
import unittest
from pathlib import Path

import cloud_file_storage

PACKAGE_DIR = Path(cloud_file_storage.__file__).resolve().parent
APP_ROOT = PACKAGE_DIR.parent
MODULE_DIR = PACKAGE_DIR / "cloud_file_storage"
DOCTYPE_DIR = MODULE_DIR / "doctype"

SETTINGS_JSON = DOCTYPE_DIR / "cloud_storage_settings" / "cloud_storage_settings.json"
IGNORED_JSON = DOCTYPE_DIR / "cloud_storage_ignored_doctype" / "cloud_storage_ignored_doctype.json"

# amendments-register.md A12: fieldname -> required default (None = no default mandated by
# the amendment), reconciled with PLAN.md §H where the two disagree — PLAN outranks the
# register. `public_bucket` is deliberately absent — single-bucket invariant (A24).
A12_FIELDS = {
	"bucket": None,
	"region": None,
	"endpoint_url": None,
	"addressing_style": None,
	"key_prefix": None,
	"use_default_credential_chain": "1",
	"access_key_id": None,
	"secret_access_key": None,
	"sse_mode": "SSE-S3",
	"kms_key_id": None,
	"storage_class": None,
	"operation_mode": None,
	"fail_insert_on_s3_error": None,
	"cdn_base_url": None,
	"public_presign_ttl": "3600",
	"private_presign_ttl": "300",
	"inline_mimetype_prefixes": None,
	"audit_public_fallback": None,
	"cache_max_size_mb": "5120",
	"cache_ttl_hours": "72",
	"ignored_doctypes": None,
	"multipart_threshold_mb": "64",
	"multipart_chunksize_mb": "16",
	"transfer_max_concurrency": "4",
	# 30, not A12's 7: PLAN.md outranks docs/adr/ (docs/INVARIANTS.md precedence), and PLAN §H
	# item 1 records the owner's adopted attachment recovery window as 30 days.
	# `object_delete_grace_days` IS that window, so A12's 7 is superseded — and with 7
	# it is impossible to satisfy A27 (`grace >= restore_window`, default 30) at all.
	"object_delete_grace_days": "30",
	"restore_window_days": "30",
	"migration_batch_size": "1000",
	"migration_parallelism": "2",
	"migration_bandwidth_limit_mbps": "0",
}

# PLAN.md §A / FD-5: live objects must be retrievable synchronously.
SYNC_RETRIEVAL_CLASSES = {"STANDARD", "STANDARD_IA", "INTELLIGENT_TIERING"}

# PLAN.md §A: all four storage modes.
OPERATION_MODES = {"LOCAL_ONLY", "DUAL_WRITE", "S3_PRIMARY_LOCAL_FALLBACK", "S3_ONLY"}


def _load(path: Path) -> dict:
	with open(path, encoding="utf-8") as handle:
		return json.load(handle)


def _doctype_json_paths() -> list[Path]:
	return sorted(DOCTYPE_DIR.glob("*/*.json"))


class TestModuleIdentity(unittest.TestCase):
	"""PLAN.md §B P1: package/dirs/hooks rename is coherent end to end."""

	def test_modules_txt_names_exactly_one_module_matching_the_module_dir(self):
		modules = [
			line.strip()
			for line in (PACKAGE_DIR / "modules.txt").read_text(encoding="utf-8").splitlines()
			if line.strip()
		]
		self.assertEqual(modules, ["Cloud File Storage"])
		scrubbed = modules[0].lower().replace(" ", "_").replace("-", "_")
		self.assertTrue(
			(PACKAGE_DIR / scrubbed).is_dir(),
			f"modules.txt names {modules[0]!r} but {PACKAGE_DIR / scrubbed} does not exist",
		)
		self.assertTrue((PACKAGE_DIR / scrubbed / "__init__.py").is_file())

	def test_every_doctype_json_declares_the_shipped_module(self):
		paths = _doctype_json_paths()
		self.assertTrue(paths, "no doctype JSON shipped")
		for path in paths:
			with self.subTest(doctype=path.name):
				self.assertEqual(_load(path)["module"], "Cloud File Storage")

	def test_doctype_directory_name_matches_the_doctype_name(self):
		for path in _doctype_json_paths():
			with self.subTest(doctype=path.name):
				data = _load(path)
				expected = data["name"].lower().replace(" ", "_").replace("-", "_")
				self.assertEqual(path.parent.name, expected)
				self.assertEqual(path.stem, expected)

	def test_no_stale_fork_package_directory_remains(self):
		self.assertFalse(
			(APP_ROOT / "frappe_s3_attachment").exists(),
			"the fork package directory must be gone after the rename",
		)


class TestDoctypeJsonValidity(unittest.TestCase):
	def test_every_doctype_json_parses_and_carries_the_required_keys(self):
		for path in _doctype_json_paths():
			with self.subTest(doctype=path.name):
				data = _load(path)
				for key in ("doctype", "name", "module", "engine", "fields", "field_order"):
					self.assertIn(key, data)
				self.assertEqual(data["doctype"], "DocType")
				self.assertEqual(data["engine"], "InnoDB")

	def test_field_order_matches_the_field_list(self):
		for path in _doctype_json_paths():
			with self.subTest(doctype=path.name):
				data = _load(path)
				self.assertEqual(data["field_order"], [f["fieldname"] for f in data["fields"]])

	def test_no_fieldtype_is_missing(self):
		for path in _doctype_json_paths():
			with self.subTest(doctype=path.name):
				for field in _load(path)["fields"]:
					self.assertTrue(field.get("fieldtype"), f"{field['fieldname']} has no fieldtype")


class TestSettingsSchemaMatchesA12(unittest.TestCase):
	"""amendments-register.md A12 — the settings schema is FROZEN."""

	def setUp(self):
		self.data = _load(SETTINGS_JSON)
		self.by_name = {f["fieldname"]: f for f in self.data["fields"]}

	def test_every_a12_field_is_present(self):
		missing = sorted(name for name in A12_FIELDS if name not in self.by_name)
		self.assertEqual(missing, [], f"A12 fields missing from the shipped schema: {missing}")

	def test_a12_defaults_are_exact(self):
		for name, default in A12_FIELDS.items():
			if default is None:
				continue
			with self.subTest(field=name):
				self.assertEqual(
					str(self.by_name[name].get("default")),
					default,
					f"{name} default must be {default!r} per A12",
				)

	def test_no_public_bucket_field(self):
		"""A24: single private live bucket; `public_bucket` was dropped."""
		self.assertNotIn("public_bucket", self.by_name)

	def test_secret_access_key_is_a_password_field(self):
		"""PLAN.md §C security: secrets live in Password fields or the IAM chain."""
		self.assertEqual(self.by_name["secret_access_key"]["fieldtype"], "Password")

	def test_no_other_field_stores_a_secret_in_plain_data(self):
		for name, field in self.by_name.items():
			if re.search(r"secret|password|private_key", name):
				with self.subTest(field=name):
					self.assertEqual(field["fieldtype"], "Password")

	def test_storage_class_offers_synchronous_retrieval_classes_only(self):
		options = set(self.by_name["storage_class"]["options"].split("\n")) - {""}
		self.assertEqual(options, SYNC_RETRIEVAL_CLASSES)

	def test_operation_mode_offers_all_four_modes(self):
		options = set(self.by_name["operation_mode"]["options"].split("\n")) - {""}
		self.assertEqual(options, OPERATION_MODES)

	def test_sse_mode_offers_s3_and_kms(self):
		options = set(self.by_name["sse_mode"]["options"].split("\n")) - {""}
		self.assertEqual(options, {"SSE-S3", "SSE-KMS"})

	def test_ignored_doctypes_points_at_the_shipped_child_table(self):
		field = self.by_name["ignored_doctypes"]
		self.assertEqual(field["fieldtype"], "Table")
		self.assertEqual(field["options"], "Cloud Storage Ignored DocType")
		self.assertEqual(_load(IGNORED_JSON)["istable"], 1)

	def test_settings_is_a_single_doctype(self):
		self.assertEqual(self.data["issingle"], 1)


class TestSelectAndCounterLint(unittest.TestCase):
	"""docs/INVARIANTS.md invariant 10 / A24 / ACCEPTANCE_GATES F6."""

	def test_every_select_has_an_explicit_default_or_a_leading_newline(self):
		violations = []
		for path in _doctype_json_paths():
			for field in _load(path)["fields"]:
				if field.get("fieldtype") != "Select":
					continue
				options = field.get("options") or ""
				if not field.get("default") and not options.startswith("\n"):
					violations.append(f"{path.name}:{field['fieldname']}")
		self.assertEqual(violations, [], f"Select fields without a default: {violations}")

	def test_byte_counters_are_long_int(self):
		violations = []
		for path in _doctype_json_paths():
			for field in _load(path)["fields"]:
				name = field["fieldname"]
				if re.search(r"(^|_)(bytes|size_bytes)$", name) and field["fieldtype"] != "Long Int":
					violations.append(f"{path.name}:{name}={field['fieldtype']}")
		self.assertEqual(violations, [], f"byte counters must be Long Int: {violations}")


class TestHooksContract(unittest.TestCase):
	"""A10 — the legacy endpoint keeps resolving after the rename."""

	def setUp(self):
		self.hooks = importlib.import_module("cloud_file_storage.hooks")

	def test_app_metadata(self):
		self.assertEqual(self.hooks.app_name, "cloud_file_storage")
		self.assertEqual(self.hooks.app_title, "Cloud File Storage")
		self.assertTrue(self.hooks.app_publisher)
		self.assertEqual(self.hooks.app_license, "MIT")
		self.assertEqual(self.hooks.app_version, cloud_file_storage.__version__)

	def test_no_whitelisted_method_override_is_declared(self):
		"""Overriding another app's whitelisted method fails the marketplace audit.

		`legacy_generate_file` itself is unchanged and still whitelisted -- only the remap
		that pointed the fork's dotted path at it is gone. See hooks.py for the one-liner an
		operator adds if they need fork-era URLs to keep resolving.
		"""
		self.assertFalse(
			hasattr(self.hooks, "override_whitelisted_methods"),
			"override_whitelisted_methods is rejected by the marketplace audit",
		)

	def test_every_hook_target_resolves(self):
		targets = [self.hooks.after_install, self.hooks.after_migrate]
		for events in self.hooks.doc_events.values():
			targets.extend(events.values())

		for dotted in targets:
			with self.subTest(target=dotted):
				module_name, _, attribute = dotted.rpartition(".")
				module = importlib.import_module(module_name)
				self.assertTrue(
					callable(getattr(module, attribute, None)),
					f"{dotted} does not resolve to a callable",
				)

	def test_remap_target_is_whitelisted(self):
		from cloud_file_storage.api import compat

		self.assertTrue(
			getattr(compat.legacy_generate_file, "__wrapped__", None)
			or getattr(compat.legacy_generate_file, "whitelisted", False),
			"legacy_generate_file must be decorated with @frappe.whitelist()",
		)


class TestPatchesContract(unittest.TestCase):
	"""PLAN.md §B P1: 'patches idempotent'."""

	def setUp(self):
		self.lines = (PACKAGE_DIR / "patches.txt").read_text(encoding="utf-8").splitlines()

	def _entries(self):
		return [line.strip() for line in self.lines if line.strip() and not line.strip().startswith("[")]

	def test_sections_are_declared_and_ordered(self):
		sections = [line.strip() for line in self.lines if line.strip().startswith("[")]
		self.assertEqual(sections, ["[pre_model_sync]", "[post_model_sync]"])

	def test_every_entry_resolves_to_a_module_with_execute(self):
		entries = self._entries()
		self.assertTrue(entries, "patches.txt declares no patches")
		for dotted in entries:
			with self.subTest(patch=dotted):
				self.assertTrue(dotted.startswith("cloud_file_storage."), "patch must live in this app")
				module = importlib.import_module(dotted)
				self.assertTrue(callable(getattr(module, "execute", None)))

	def test_no_entry_is_declared_twice(self):
		entries = self._entries()
		self.assertEqual(len(entries), len(set(entries)))

	def test_backfill_patch_predicate_excludes_rows_it_has_already_written(self):
		"""Idempotency by construction: the SELECT skips rows with a key already set.

		Re-running the patch must not re-touch a row, and must not depend on the *new*
		endpoint shape — A10 forbids rewriting stored URLs, so no row can carry the new
		path and still be missing its key.
		"""
		source = (PACKAGE_DIR / "patches" / "v0_2_0" / "backfill_s3_object_key.py").read_text(
			encoding="utf-8"
		)
		self.assertIn("s3_object_key IS NULL OR s3_object_key = ''", source)
		self.assertIn("content_hash IS NOT NULL", source)
		self.assertNotIn("cloud_file_storage.api.compat.legacy_generate_file%", source)

	def test_patch_modules_contain_no_local_deletion_calls(self):
		"""PLAN.md §C: no delete-before-verify anywhere in the migration-adjacent path."""
		banned = {"remove", "unlink", "rmtree", "rename"}
		for path in (PACKAGE_DIR / "patches").rglob("*.py"):
			tree = ast.parse(path.read_text(encoding="utf-8"))
			for node in ast.walk(tree):
				if isinstance(node, ast.Attribute) and node.attr in banned:
					value = getattr(node.value, "id", "")
					if value in {"os", "shutil"}:
						self.fail(f"{path.name} calls {value}.{node.attr}")


class TestNoAclContract(unittest.TestCase):
	"""ACCEPTANCE_GATES F6 / docs/INVARIANTS.md invariant 6: no ACL key in any S3 call."""

	def test_no_acl_key_appears_in_any_extraargs_or_s3_call(self):
		offenders = []
		for path in PACKAGE_DIR.rglob("*.py"):
			if "test" in path.name:
				continue
			tree = ast.parse(path.read_text(encoding="utf-8"))
			for node in ast.walk(tree):
				if isinstance(node, ast.Constant) and node.value == "ACL":
					offenders.append(f"{path.name}:{node.lineno}")
				if isinstance(node, ast.keyword) and node.arg == "ACL":
					offenders.append(f"{path.name}:{node.value.lineno}")
		self.assertEqual(offenders, [], f"ACL used in S3 calls at: {offenders}")

	def test_no_public_read_canned_acl_in_runtime_code(self):
		offenders = []
		for path in PACKAGE_DIR.rglob("*.py"):
			if "test" in path.name:
				continue
			if "public-read" in path.read_text(encoding="utf-8"):
				offenders.append(path.name)
		self.assertEqual(offenders, [])


class TestPackagingContract(unittest.TestCase):
	"""A34 — declared frappe floor; PLAN.md §A — v15-only for v1."""

	def setUp(self):
		self.pyproject = (APP_ROOT / "pyproject.toml").read_text(encoding="utf-8")

	def test_project_name_is_the_new_app_name(self):
		self.assertRegex(self.pyproject, r'(?m)^name\s*=\s*"cloud_file_storage"')

	def test_frappe_window_is_the_line_this_branch_releases(self):
		"""The declared frappe range must be exactly the line the app major names.

		The app major tracks the frappe major, as every multi-version app in this ecosystem
		does (erpnext 15.93.0 -> frappe v15, india_compliance 15.18.1 -> frappe v15). So the
		window is *derived* from `__version__` rather than written out a second time and left
		to drift: a `version-16` branch that still declared `<16.0.0` would install nowhere,
		and a `version-15` branch that declared `<17.0.0` would invite an untested pairing.

		Resolved through `SpecifierSet` rather than matched as a substring -- `"<17"` in the
		text proves nothing about which versions are admitted, and a substring check passes
		for a spec that admits no version at all.
		"""
		from packaging.specifiers import SpecifierSet

		match = re.search(r"(?m)^frappe\s*=\s*\"([^\"]+)\"", self.pyproject)
		self.assertIsNotNone(match, "[tool.bench.frappe-dependencies] frappe is not declared")
		spec = SpecifierSet(match.group(1))

		major = int(cloud_file_storage.__version__.split(".")[0])
		self.assertTrue(
			spec.contains(f"{major}.0.0") or spec.contains(f"{major}.16.0"),
			f"app is {major}.x but the declared frappe window admits no {major}.x release",
		)
		self.assertFalse(
			spec.contains(f"{major + 1}.0.0"),
			f"the window admits frappe v{major + 1}, which this line does not release for",
		)
		self.assertFalse(
			spec.contains(f"{major - 1}.99.0"),
			f"the window admits frappe v{major - 1}, which a different line releases for",
		)

		if major == 15:
			# A34: `find_file_by_url` and the `fid` query arg first exist at 15.16.0.
			self.assertFalse(spec.contains("15.15.0"), "A34 raises the declared floor to 15.16.0")

	def test_frappe_dependencies_table_is_where_bench_looks_for_it(self):
		self.assertIn("[tool.bench.frappe-dependencies]", self.pyproject)

	def test_version_is_consistent_across_package_and_changelog(self):
		version = cloud_file_storage.__version__
		self.assertTrue(version)
		changelog = (APP_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
		self.assertIn(f"[{version}]", changelog, "CHANGELOG has no section for the shipped version")

	def test_no_scheduler_entry_does_real_work_on_the_erp_shared_queue(self):
		"""F3 / `background.py`: GC and repair must not compete with the ERP's own queues.

		frappe routes a scheduled job by its frequency NAME -- `get_queue_name`
		(`scheduled_job_type.py`) sends `*Maintenance*` and `*Long*` to `long` and everything
		else to `default`, and the scheduler enqueues with no explicit timeout, so `default`
		carries 300s against `long`'s 1500s. Moving this app's hooks from
		`hourly_maintenance`/`daily_maintenance` to `hourly`/`daily` for v15.16.0 compatibility
		therefore moved four jobs that do real inline work -- batched S3 deletes, per-row
		commits, a whole-cache walk -- onto the shared `default` queue with a 5x shorter
		timeout. Nothing caught it: the full suite was green, because no test asserted the
		queue for any `scheduler_events` entry. This is that test.

		The rule enforced is the one this app already states in `background.py`: work that is
		not O(ms) goes through `enqueue_maintenance`, which targets `cloud_migration` and falls
		back loudly to `long`. So every non-cron scheduler target on a `default`-routed
		frequency must be a dispatcher -- its body must enqueue rather than do the work.
		"""
		import inspect

		from cloud_file_storage import hooks

		# Frequencies frappe routes to `long`; everything else lands on `default`.
		LONG_ROUTED = ("Maintenance", "Long")

		offenders = []
		for key, value in hooks.scheduler_events.items():
			if isinstance(value, dict):
				continue  # cron jobs, routed separately
			frequency = key.replace("_", " ").title()
			if any(token in frequency for token in LONG_ROUTED):
				continue  # already on `long`
			for dotted in value:
				module_name, _, func_name = dotted.rpartition(".")
				module = importlib.import_module(module_name)
				source = inspect.getsource(module)
				tree = ast.parse(source)
				fn = next(
					(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == func_name),
					None,
				)
				if fn is None:
					offenders.append(f"{dotted} (not found)")
					continue
				enqueues = any(
					isinstance(n, ast.Call)
					and (
						getattr(n.func, "id", "") in ("enqueue_maintenance", "enqueue")
						or getattr(n.func, "attr", "") in ("enqueue_maintenance", "enqueue")
					)
					for n in ast.walk(fn)
				)
				if not enqueues:
					offenders.append(f"{dotted} on '{key}' -> queue 'default'")

		self.assertEqual(
			offenders,
			[],
			"scheduler entries on a `default`-routed frequency that do work inline instead of "
			f"dispatching it: {offenders}. They run on the ERP's shared queue with a 300s "
			"timeout. Route them through `background.enqueue_maintenance(..., timeout=1500)` "
			"like `gc.repair_pending_uploads_dispatch` does.",
		)

	def test_every_scheduler_frequency_exists_at_the_declared_floor(self):
		"""The floor is a promise, and `hooks.py` broke it silently for eight months of releases.

		`hourly_maintenance` / `daily_maintenance` first exist in frappe **v15.79.0**. The
		declared floor is **v15.16.0**. Registering them made `bench install-app` refuse
		outright -- "Frequency cannot be \"Hourly Maintenance\"" -- so the app could not install
		on any supported frappe below 15.79. Nothing caught it: the bench here runs 15.93, and
		the reference matrix had never got past `bench init` on the older refs.

		The allow-list below is the `frequency` Select at the FLOOR, not at whatever frappe
		this bench happens to have. Hard-coded deliberately: reading the installed frappe would
		reproduce the exact blindness that let this ship.
		"""
		from cloud_file_storage import hooks

		# frappe v15.16.0, ScheduledJobType.frequency options, verbatim.
		AT_FLOOR = {
			"All",
			"Hourly",
			"Hourly Long",
			"Daily",
			"Daily Long",
			"Weekly",
			"Weekly Long",
			"Monthly",
			"Monthly Long",
			"Cron",
			"Yearly",
			"Annual",
		}
		# Frappe's own rule, read out of `scheduled_job_type.py` rather than assumed:
		# `insert_events` (:214-223) branches on the VALUE type -- a dict becomes Cron jobs
		# whatever the key is called -- and `insert_event_jobs` (:234-240) derives the frequency
		# as `event_type.replace("_", " ").title()`. An earlier version of this test exempted the
		# key literally named "cron", which is NOT the same rule: a dict under any other name
		# would have been title-cased and wrongly reported. Fail-safe rather than fail-open, but
		# it encoded my assumption instead of frappe's behaviour -- the defect class this suite
		# exists to catch, found by reading the source I should have read first.
		offenders = {
			key: key.replace("_", " ").title()
			for key, value in hooks.scheduler_events.items()
			if not isinstance(value, dict) and key.replace("_", " ").title() not in AT_FLOOR
		}
		self.assertFalse(
			offenders,
			f"scheduler_events uses frequencies absent at the declared floor 15.16.0: {offenders}. "
			"Either use one the floor has, or raise the floor deliberately in "
			"docs/supported-versions.md and pyproject -- but the floor is tied to A34 and is not "
			"free to move.",
		)

	def test_boto3_is_a_declared_runtime_dependency(self):
		self.assertRegex(self.pyproject, r'"boto3[><=]')


class TestLocaleDeliverable(unittest.TestCase):
	"""PLAN.md §B, P1 scope: 'locale regenerated'.

	Frappe resolves an app's catalog at ``frappe.get_app_path(app)/locale`` with the
	template always named ``main.pot`` (frappe/gettext/translate.py). The fork shipped
	``frappe_s3_attachment/locale/{main.pot,de.po}``; the renamed package must ship the
	regenerated equivalent, otherwise the rename silently drops every translation.
	"""

	def test_locale_directory_ships_with_the_package(self):
		self.assertTrue(
			(PACKAGE_DIR / "locale").is_dir(),
			"P1 deliverable 'locale regenerated' is missing: no cloud_file_storage/locale/",
		)

	def test_main_pot_is_regenerated_for_the_renamed_package(self):
		pot = PACKAGE_DIR / "locale" / "main.pot"
		self.assertTrue(pot.is_file(), f"missing translation template {pot}")
		body = pot.read_text(encoding="utf-8")
		self.assertNotIn(
			"frappe_s3_attachment",
			body,
			"the regenerated catalog still references the fork package name",
		)


if __name__ == "__main__":
	unittest.main()
