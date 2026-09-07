import unittest
from contextlib import ExitStack, contextmanager
from unittest.mock import MagicMock, patch

from cloud_file_storage import install
from cloud_file_storage.install import (
	CLOUD_STORAGE_MANAGER_ROLE,
	DEFAULT_IGNORED_DOCTYPES,
	MODULE_NAME,
	S3_OBJECT_KEY_FIELD,
	S3_OBJECT_KEY_INDEX_NAME,
	S3_OBJECT_KEY_INDEX_PREFIX,
	S3_OBJECT_KEY_LENGTH,
	ensure_cloud_storage_manager_role,
	ensure_default_ignored_doctypes,
	ensure_s3_object_key_custom_field,
	ensure_s3_object_key_index,
)


@contextmanager
def _patched_setup_steps():
	"""Patch every step `after_install`/`after_migrate` chains, and hand them back.

	Keeping the list in one place means a new installer step shows up as a failing
	assertion here rather than silently running against the live site during unit tests.
	"""
	targets = {
		"frappe": "cloud_file_storage.install.frappe",
		"owner": "cloud_file_storage.install.assert_single_file_owner",
		"role": "cloud_file_storage.install.ensure_cloud_storage_manager_role",
		"ignored": "cloud_file_storage.install.ensure_default_ignored_doctypes",
		"field": "cloud_file_storage.install.ensure_s3_object_key_custom_field",
		"index": "cloud_file_storage.install.ensure_s3_object_key_index",
		"cso_field": "cloud_file_storage.install.ensure_cloud_storage_object_field",
		"file_indexes": "cloud_file_storage.install.ensure_file_indexes",
		"cso_indexes": "cloud_file_storage.install.ensure_cloud_storage_object_indexes",
	}
	with ExitStack() as stack:
		yield {name: stack.enter_context(patch(target)) for name, target in targets.items()}


class TestDefaultIgnoredDocTypes(unittest.TestCase):
	def test_seeds_the_operational_trio_when_missing(self):
		settings = MagicMock()
		settings.ignored_doctypes = []
		with patch("cloud_file_storage.install.frappe.get_single", return_value=settings):
			ensure_default_ignored_doctypes()

		seeded = [call.args[1]["doctype_name"] for call in settings.append.call_args_list]
		self.assertEqual(seeded, ["Data Import", "Prepared Report", "Package Import"])
		self.assertEqual(tuple(seeded), DEFAULT_IGNORED_DOCTYPES)
		self.assertTrue(settings.flags.ignore_mandatory)
		settings.save.assert_called_once_with(ignore_permissions=True)

	def test_appends_only_the_missing_rows(self):
		settings = MagicMock()
		settings.ignored_doctypes = [MagicMock(doctype_name="Data Import")]
		with patch("cloud_file_storage.install.frappe.get_single", return_value=settings):
			ensure_default_ignored_doctypes()

		seeded = [call.args[1]["doctype_name"] for call in settings.append.call_args_list]
		self.assertEqual(seeded, ["Prepared Report", "Package Import"])
		settings.save.assert_called_once_with(ignore_permissions=True)

	def test_noop_when_all_present(self):
		settings = MagicMock()
		settings.ignored_doctypes = [MagicMock(doctype_name=name) for name in DEFAULT_IGNORED_DOCTYPES]
		with patch("cloud_file_storage.install.frappe.get_single", return_value=settings):
			ensure_default_ignored_doctypes()

		settings.append.assert_not_called()
		settings.save.assert_not_called()


class TestCloudStorageManagerRole(unittest.TestCase):
	def test_creates_desk_role_when_missing(self):
		role = MagicMock()
		mock_frappe = MagicMock()
		mock_frappe.db.exists.return_value = False
		mock_frappe.new_doc.return_value = role
		with patch("cloud_file_storage.install.frappe", mock_frappe):
			ensure_cloud_storage_manager_role()

		mock_frappe.new_doc.assert_called_once_with("Role")
		self.assertEqual(role.role_name, CLOUD_STORAGE_MANAGER_ROLE)
		self.assertEqual(role.desk_access, 1)
		role.insert.assert_called_once_with(ignore_permissions=True)

	def test_noop_when_role_exists(self):
		mock_frappe = MagicMock()
		mock_frappe.db.exists.return_value = True
		with patch("cloud_file_storage.install.frappe", mock_frappe):
			ensure_cloud_storage_manager_role()

		mock_frappe.db.exists.assert_called_once_with("Role", CLOUD_STORAGE_MANAGER_ROLE)
		mock_frappe.new_doc.assert_not_called()


class TestS3ObjectKeyCustomField(unittest.TestCase):
	def test_ensure_s3_object_key_custom_field_creates_visible_readonly_field(self):
		with patch("cloud_file_storage.install.create_custom_fields") as create:
			ensure_s3_object_key_custom_field()

		create.assert_called_once()
		(payload,), _ = create.call_args
		self.assertIn("File", payload)
		(field,) = payload["File"]
		self.assertEqual(field["fieldname"], S3_OBJECT_KEY_FIELD)
		self.assertEqual(field["fieldtype"], "Data")
		self.assertEqual(field["length"], S3_OBJECT_KEY_LENGTH)
		self.assertEqual(field["insert_after"], "content_hash")
		self.assertEqual(field["read_only"], 1)
		self.assertEqual(field["hidden"], 0)
		self.assertEqual(field["no_copy"], 1)
		self.assertEqual(field["module"], MODULE_NAME)

	def test_field_and_index_names_are_not_renamed(self):
		"""A13: the legacy names stay so external 0.2.x installs keep resolving."""
		self.assertEqual(S3_OBJECT_KEY_FIELD, "s3_object_key")
		self.assertEqual(S3_OBJECT_KEY_INDEX_NAME, "s3_object_key_index")


class TestS3ObjectKeyIndex(unittest.TestCase):
	def test_mariadb_uses_prefix_index(self):
		mock_db = MagicMock()
		mock_db.db_type = "mariadb"
		with patch("cloud_file_storage.install.frappe.db", mock_db):
			ensure_s3_object_key_index()

		mock_db.add_index.assert_called_once_with(
			"File",
			[f"{S3_OBJECT_KEY_FIELD}({S3_OBJECT_KEY_INDEX_PREFIX})"],
			index_name=S3_OBJECT_KEY_INDEX_NAME,
		)

	def test_postgres_uses_plain_index(self):
		mock_db = MagicMock()
		mock_db.db_type = "postgres"
		with patch("cloud_file_storage.install.frappe.db", mock_db):
			ensure_s3_object_key_index()

		mock_db.add_index.assert_called_once_with(
			"File",
			[S3_OBJECT_KEY_FIELD],
			index_name=S3_OBJECT_KEY_INDEX_NAME,
		)


class TestSetupHooks(unittest.TestCase):
	def test_after_install_chains_setup_steps(self):
		with _patched_setup_steps() as steps:
			install.after_install()

		for name, step in steps.items():
			if name == "frappe":
				continue
			with self.subTest(step=name):
				step.assert_called_once_with()

	def test_after_migrate_ensures_role_field_and_index(self):
		with _patched_setup_steps() as steps:
			install.after_migrate()

		steps["role"].assert_called_once_with()
		steps["field"].assert_called_once_with()
		steps["index"].assert_called_once_with()
		steps["cso_field"].assert_called_once_with()
		steps["file_indexes"].assert_called_once_with()
		steps["cso_indexes"].assert_called_once_with()

	def test_every_custom_field_is_created_before_any_index(self):
		"""Regression: a fresh install lost `s3_object_key_index`.

		`create_custom_fields` re-syncs the File table, and that sync drops indexes on
		columns whose DocField carries no `search_index`. An index added before the last
		custom field therefore disappears without a word — it survived `bench migrate` and
		vanished on `bench install-app`.
		"""
		order = []
		with _patched_setup_steps() as steps:
			for name in ("field", "cso_field", "index", "file_indexes", "cso_indexes"):
				steps[name].side_effect = lambda _n=name, *a, **k: order.append(_n)
			install.after_install()

		self.assertEqual(order, ["field", "cso_field", "index", "file_indexes", "cso_indexes"])

	def test_install_refuses_to_share_the_file_doctype_with_another_app(self):
		"""`write_file` and `override_doctype_class` are single-owner hooks."""
		from cloud_file_storage.storage.exceptions import CloudStorageConfigurationError

		with _patched_setup_steps() as steps:
			steps["owner"].side_effect = CloudStorageConfigurationError("another app owns File")
			with self.assertRaises(CloudStorageConfigurationError):
				install.after_install()

	def test_after_migrate_repairs_only_a_completely_unseeded_list(self):
		"""Migrate is the repair path for an install whose after_install died mid-way.

		It must never resurrect rows an operator deliberately removed, so it asks for the
		`only_when_unconfigured` behaviour rather than an unconditional reseed.
		"""
		with _patched_setup_steps() as steps:
			install.after_migrate()

		steps["ignored"].assert_called_once_with(only_when_unconfigured=True)

	def test_only_when_unconfigured_never_resurrects_removed_rows(self):
		"""A site that kept even one row counts as configured - leave it alone."""
		settings = MagicMock()
		settings.ignored_doctypes = [MagicMock(doctype_name="Data Import")]
		with patch("cloud_file_storage.install.frappe") as mock_frappe:
			mock_frappe.get_single.return_value = settings
			ensure_default_ignored_doctypes(only_when_unconfigured=True)

		settings.append.assert_not_called()
		settings.save.assert_not_called()

	def test_only_when_unconfigured_seeds_a_completely_empty_list(self):
		"""The failed-install case: nothing configured at all, so seed the full trio."""
		settings = MagicMock()
		settings.ignored_doctypes = []
		seeded = []
		settings.append.side_effect = lambda _field, row: seeded.append(row["doctype_name"])
		with patch("cloud_file_storage.install.frappe") as mock_frappe:
			mock_frappe.get_single.return_value = settings
			ensure_default_ignored_doctypes(only_when_unconfigured=True)

		self.assertEqual(tuple(seeded), DEFAULT_IGNORED_DOCTYPES)
		settings.save.assert_called_once_with(ignore_permissions=True)

	def test_after_install_clears_the_stale_doctype_module_cache(self):
		"""Regression: install-app died resolving our Single to frappe's Core module.

		`get_doctype_module` memoises the DocType -> Module map, and during install that
		map can predate our own sync, so `get_single` raised ImportError and the
		operational trio was never seeded.
		"""
		with _patched_setup_steps() as steps:
			install.after_install()

		steps["frappe"].clear_cache.assert_called_once_with()

	def test_upgrade_patch_delegates_to_the_idempotent_seeder(self):
		"""The patch body must reuse the seeder, so re-running a patch is a no-op."""
		from cloud_file_storage.patches.v0_2_1 import ensure_data_import_ignored_doctype as patch_module

		with patch(
			"cloud_file_storage.patches.v0_2_1.ensure_data_import_ignored_doctype."
			"ensure_default_ignored_doctypes"
		) as seeder:
			patch_module.execute()
			patch_module.execute()

		self.assertEqual(seeder.call_count, 2)


class TestBackfillPatchIdempotency(unittest.TestCase):
	"""PLAN.md §B P1: 'patches idempotent'.

	The candidate SELECT is the whole idempotency argument: a row leaves the candidate set
	the moment its `s3_object_key` is written, so a second `execute()` over a database the
	patch has already processed must issue zero writes.
	"""

	def _run_execute_with_rows(self, batches):
		from cloud_file_storage.patches.v0_2_0 import backfill_s3_object_key as patch_module

		with patch("cloud_file_storage.patches.v0_2_0.backfill_s3_object_key.frappe") as mock_frappe:
			with patch(
				"cloud_file_storage.patches.v0_2_0.backfill_s3_object_key.ensure_s3_object_key_custom_field"
			) as field:
				with patch(
					"cloud_file_storage.patches.v0_2_0.backfill_s3_object_key.ensure_s3_object_key_index"
				) as index:
					mock_frappe.db.sql.side_effect = batches
					patch_module.execute()

		return mock_frappe, field, index

	def test_second_run_over_an_already_backfilled_database_writes_nothing(self):
		mock_frappe, field, index = self._run_execute_with_rows([[]])

		mock_frappe.db.set_value.assert_not_called()
		mock_frappe.db.commit.assert_not_called()
		field.assert_called_once_with()
		index.assert_called_once_with()

	def test_first_run_clears_content_hash_and_stops_when_the_batch_is_empty(self):
		row = MagicMock()
		row.name = "FILE-1"
		row.content_hash = "2026/05/06/File/AB12CD34_x.pdf"
		mock_frappe, _, _ = self._run_execute_with_rows([[row], []])

		mock_frappe.db.set_value.assert_called_once_with(
			"File",
			"FILE-1",
			{"s3_object_key": "2026/05/06/File/AB12CD34_x.pdf", "content_hash": None},
			update_modified=False,
		)
		self.assertEqual(mock_frappe.db.sql.call_count, 2)

	def test_ensures_the_column_exists_before_selecting_on_it(self):
		"""A13: the custom field and its index are created by the patch, not assumed."""
		from cloud_file_storage.patches.v0_2_0 import backfill_s3_object_key as patch_module

		call_order = []
		with patch("cloud_file_storage.patches.v0_2_0.backfill_s3_object_key.frappe") as mock_frappe:
			with patch(
				"cloud_file_storage.patches.v0_2_0.backfill_s3_object_key.ensure_s3_object_key_custom_field",
				side_effect=lambda: call_order.append("field"),
			):
				with patch(
					"cloud_file_storage.patches.v0_2_0.backfill_s3_object_key.ensure_s3_object_key_index",
					side_effect=lambda: call_order.append("index"),
				):
					mock_frappe.db.sql.side_effect = lambda *a, **kw: call_order.append("select") or []
					patch_module.execute()

		self.assertEqual(call_order, ["field", "index", "select"])


class TestCloudStorageObjectLinkPatch(unittest.TestCase):
	"""Every schema change ships a patch (docs/INVARIANTS.md invariant 9)."""

	def test_it_delegates_to_the_idempotent_installer_helpers(self):
		from cloud_file_storage.patches.v0_3_0 import create_cloud_storage_object_link as patch_module

		with patch(f"{patch_module.__name__}.frappe"):
			with patch(f"{patch_module.__name__}.ensure_cloud_storage_object_field") as field:
				with patch(f"{patch_module.__name__}.ensure_file_indexes") as file_indexes:
					with patch(f"{patch_module.__name__}.ensure_cloud_storage_object_indexes") as cso:
						patch_module.execute()
						patch_module.execute()

		self.assertEqual(field.call_count, 2)
		self.assertEqual(file_indexes.call_count, 2)
		self.assertEqual(cso.call_count, 2)


class TestForkMigrationSettingsPatch(unittest.TestCase):
	def test_it_deletes_the_orphaned_singles_rows_for_both_removed_fields(self):
		from cloud_file_storage.patches.v0_3_0 import drop_fork_migration_settings as patch_module

		with patch(f"{patch_module.__name__}.frappe") as mock_frappe:
			mock_frappe.db.table_exists.return_value = True
			patch_module.execute()

		deleted = [call.args[1]["field"] for call in mock_frappe.db.delete.call_args_list]
		self.assertEqual(deleted, ["timeout_for_migration_job", "migrate_existing_files"])

	def test_it_is_a_no_op_without_a_singles_table(self):
		from cloud_file_storage.patches.v0_3_0 import drop_fork_migration_settings as patch_module

		with patch(f"{patch_module.__name__}.frappe") as mock_frappe:
			mock_frappe.db.table_exists.return_value = False
			patch_module.execute()

		mock_frappe.db.delete.assert_not_called()


class TestForkSurfacesAreGone(unittest.TestCase):
	"""The four registered P1 deviations and the fork's bulk endpoint (DECISIONS.md)."""

	def test_the_fork_controller_module_no_longer_exists(self):
		import importlib

		with self.assertRaises(ModuleNotFoundError):
			importlib.import_module("cloud_file_storage.controller")

	def test_the_bulk_migration_endpoint_is_gone(self):
		import ast
		from pathlib import Path

		import cloud_file_storage

		package = Path(cloud_file_storage.__file__).resolve().parent
		offenders = []
		for source in package.rglob("*.py"):
			if "test" in source.name:
				continue
			tree = ast.parse(source.read_text(encoding="utf-8"))
			for node in ast.walk(tree):
				if isinstance(node, ast.FunctionDef) and node.name in (
					"migrate_existing_files",
					"run_migrate_existing_files",
					"file_upload_to_s3",
					"delete_from_cloud",
				):
					offenders.append(f"{source.name}:{node.name}")
		self.assertEqual(offenders, [])

	def test_the_settings_schema_dropped_the_fork_only_fields(self):
		import json
		from pathlib import Path

		import cloud_file_storage

		package = Path(cloud_file_storage.__file__).resolve().parent
		schema = json.loads(
			(
				package
				/ "cloud_file_storage"
				/ "doctype"
				/ "cloud_storage_settings"
				/ "cloud_storage_settings.json"
			).read_text(encoding="utf-8")
		)
		fieldnames = {field["fieldname"] for field in schema["fields"]}
		self.assertNotIn("timeout_for_migration_job", fieldnames)
		self.assertNotIn("migrate_existing_files", fieldnames)


class TestGraceWindowPatch(unittest.TestCase):
	"""PLAN §H item 1 — the grace period IS the attachment recovery window."""

	def _execute(self, grace, restore_window):
		from cloud_file_storage.patches.v0_3_0 import (
			raise_object_delete_grace_to_recovery_window as patch_module,
		)

		with patch(f"{patch_module.__name__}.frappe") as mock_frappe:
			mock_frappe.db.table_exists.return_value = True
			mock_frappe.db.get_single_value.side_effect = lambda _dt, field: {
				"object_delete_grace_days": grace,
				"restore_window_days": restore_window,
			}[field]
			patch_module.execute()
		return mock_frappe

	def test_a_grace_inside_the_restore_window_is_raised_to_it(self):
		mock_frappe = self._execute(grace=7, restore_window=30)
		mock_frappe.db.set_single_value.assert_called_once_with(
			"Cloud Storage Settings", "object_delete_grace_days", 30
		)

	def test_an_operator_chosen_longer_grace_is_left_alone(self):
		mock_frappe = self._execute(grace=45, restore_window=30)
		mock_frappe.db.set_single_value.assert_not_called()

	def test_an_equal_grace_is_already_valid(self):
		mock_frappe = self._execute(grace=30, restore_window=30)
		mock_frappe.db.set_single_value.assert_not_called()

	def test_no_restore_window_means_nothing_to_couple_to(self):
		mock_frappe = self._execute(grace=7, restore_window=0)
		mock_frappe.db.set_single_value.assert_not_called()

	def test_the_shipped_default_satisfies_a27(self):
		"""The pairing A27 validates must be shippable straight out of the box."""
		import json
		from pathlib import Path

		import cloud_file_storage

		schema = json.loads(
			(
				Path(cloud_file_storage.__file__).resolve().parent
				/ "cloud_file_storage"
				/ "doctype"
				/ "cloud_storage_settings"
				/ "cloud_storage_settings.json"
			).read_text(encoding="utf-8")
		)
		defaults = {f["fieldname"]: f.get("default") for f in schema["fields"]}
		self.assertGreaterEqual(
			int(defaults["object_delete_grace_days"]),
			int(defaults["restore_window_days"]),
			"a fresh install must not start in the state A27 forbids",
		)
