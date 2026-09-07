"""The health panel's one load-bearing property: the two byte quantities never merge.

PLAN §A requires the panel (and the release report) to distinguish **cloud-managed permanent
attachment bytes** from **bounded temporary/local operational bytes**. The reason is not
presentational. Under S3_ONLY the LOCAL_OPERATIONAL doctypes keep writing locally as a
documented mode exemption, so a panel that summed the two would either report a migration as
incomplete for ever or — worse, the direction that actually gets someone hurt — count Data
Import spreadsheets as attachments safely in the cloud.

So the assertions here are about *separation*, not just about the numbers being present: the
last test walks the whole payload and fails if any value anywhere equals the sum.
"""

from unittest.mock import patch

import frappe

from cloud_file_storage import health
from cloud_file_storage.tests.utils import CloudStorageTestCase, set_mode

OPERATIONAL_PARENT = "Data Import"
SETTINGS_DOCTYPE = "Cloud Storage Settings"


class StorageHealthTestCase(CloudStorageTestCase):
	def data_import(self) -> str:
		"""A real Data Import row to attach to.

		Setting `attached_to_doctype` on an already-inserted File was tried first and is
		wrong: the write hook decides where the bytes go **at insert time** from the parent
		doctype (`modes.effective_mode`), so a row relabelled afterwards has already been
		uploaded and is cloud-backed. The first version of this suite did exactly that, and
		the test caught it — which is the whole reason the fixture is built this way.
		"""
		doc = frappe.new_doc("Data Import")
		doc.update({"reference_doctype": "Event", "import_type": "Insert New Records"})
		doc.flags.ignore_mandatory = True
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		self.addCleanup(self._drop_import, doc.name)
		return doc.name

	def _drop_import(self, name: str):
		"""Drop the parent **and any File still attached to it**.

		Ordering bit me here: this cleanup can run before the base class removes the files it
		tracked, and deleting the parent first leaves the File pointing at a Data Import that
		no longer exists — which then fails to delete and survives on the site. One such
		orphan (`health-sum-operational.csv`) was enough to make
		`test_a_ref_on_an_ignored_doctype_is_a_hard_refusal` count two ignored refs where it
		expects one, in a different module, with nothing pointing back here. Deleting the
		attachments first makes the order irrelevant.
		"""
		frappe.db.delete("File", {"attached_to_doctype": "Data Import", "attached_to_name": name})
		frappe.db.delete("Data Import", {"name": name})
		frappe.db.commit()

	def operational_file(self, *, file_name: str, content: bytes):
		"""A File row belonging to a LOCAL_OPERATIONAL parent, created the way core does."""
		doc = self.make_file(
			file_name=file_name,
			content=content,
			is_private=1,
			attached_to_doctype=OPERATIONAL_PARENT,
			attached_to_name=self.data_import(),
		)
		self.assertIsNone(
			frappe.db.get_value("File", doc.name, "cloud_storage_object"),
			"the operational fixture went to the cloud; it would prove nothing",
		)
		return doc


class TestTheTwoQuantitiesAreSeparate(StorageHealthTestCase):
	def test_operational_bytes_are_not_counted_as_cloud_bytes(self):
		set_mode("S3_PRIMARY_LOCAL_FALLBACK")
		cloud_doc = self.make_file(file_name="health-cloud.txt", content=b"x" * 512)
		self.assertIsNotNone(
			frappe.db.get_value("File", cloud_doc.name, "cloud_storage_object"),
			"fixture is not cloud-backed, so this test would prove nothing",
		)

		before = health.storage_health()
		operational = self.operational_file(file_name="health-operational.csv", content=b"y" * 4096)
		after = health.storage_health()

		self.assertEqual(
			after["cloud_managed"]["attachment_bytes"],
			before["cloud_managed"]["attachment_bytes"],
			"an operational file moved the cloud-managed byte count",
		)
		self.assertEqual(
			after["local_operational"]["operational_bytes"]
			- before["local_operational"]["operational_bytes"],
			frappe.db.get_value("File", operational.name, "file_size"),
		)
		self.assertEqual(
			after["local_operational"]["operational_files"]
			- before["local_operational"]["operational_files"],
			1,
		)

	def test_a_cloud_object_is_not_counted_as_operational(self):
		set_mode("S3_PRIMARY_LOCAL_FALLBACK")
		before = health.storage_health()
		doc = self.make_file(file_name="health-cloud-2.txt", content=b"z" * 2048)
		after = health.storage_health()

		cso = self.cso_of(doc)
		self.assertIsNotNone(cso)
		self.assertEqual(
			after["cloud_managed"]["attachment_bytes"] - before["cloud_managed"]["attachment_bytes"],
			cso.file_size,
		)
		self.assertEqual(
			after["local_operational"]["operational_bytes"],
			before["local_operational"]["operational_bytes"],
			"a cloud attachment moved the operational byte count",
		)

	def test_no_value_in_the_payload_is_the_sum_of_the_two(self):
		"""The anti-conflation check. Sizes are chosen so the sum is unique."""
		set_mode("S3_PRIMARY_LOCAL_FALLBACK")
		self.make_file(file_name="health-sum-cloud.txt", content=b"c" * 7919)
		self.operational_file(file_name="health-sum-operational.csv", content=b"o" * 104_729)

		payload = health.storage_health()
		cloud = payload["cloud_managed"]["attachment_bytes"]
		operational = payload["local_operational"]["operational_bytes"]
		self.assertGreater(cloud, 0)
		self.assertGreater(operational, 0)

		conflated = cloud + operational
		found = [path for path, value in _walk(payload) if value == conflated]
		self.assertEqual(
			found,
			[],
			f"these keys report cloud+operational as one number: {found}",
		)

	def test_the_sections_carry_the_labels_the_release_report_has_to_use(self):
		payload = health.storage_health()
		self.assertIn("Cloud-managed", payload["cloud_managed"]["label"])
		self.assertIn("operational", payload["local_operational"]["label"].lower())
		self.assertIn(OPERATIONAL_PARENT, payload["local_operational"]["ignored_doctypes"])


class TestUnmigratedBytesAreTheirOwnBucket(StorageHealthTestCase):
	def test_a_local_only_attachment_is_neither_cloud_nor_operational(self):
		set_mode("LOCAL_ONLY")
		before = health.storage_health()
		doc = self.make_file(file_name="health-unmigrated.txt", content=b"u" * 1024)
		self.assertIsNone(frappe.db.get_value("File", doc.name, "cloud_storage_object"))
		after = health.storage_health()

		self.assertEqual(
			after["local_unmigrated"]["bytes"] - before["local_unmigrated"]["bytes"],
			frappe.db.get_value("File", doc.name, "file_size"),
		)
		self.assertEqual(
			after["cloud_managed"]["attachment_bytes"],
			before["cloud_managed"]["attachment_bytes"],
		)
		self.assertEqual(
			after["local_operational"]["operational_bytes"],
			before["local_operational"]["operational_bytes"],
		)


class TestPanelWarnings(StorageHealthTestCase):
	"""Every warning code the panel can emit, asserted from the panel, both directions.

	Four of the five had no test at all: the panel could have stopped emitting them
	entirely and nothing would have failed. That is not the recorded "check that reads the
	wrong property" pattern — there was no check — but it is the same end state, reached by
	a different route, and it is found by comparing emitted behaviour against asserted
	behaviour rather than by mutating code.

	`public_private_residue` looked covered: `test_maintenance` had a
	`test_the_health_panel_carries_the_finding` that read a key on `compat.test_connection`'s
	payload and never touched the panel. It has been renamed to say what it does.

	Each test asserts presence when the condition holds **and** absence when it does not. A
	warning that is always present carries no information, and one that is never present is
	dead code that reads as a safeguard.
	"""

	def codes(self) -> set[str]:
		return {warning["code"] for warning in health.storage_health()["warnings"]}

	def test_the_panel_emits_no_code_this_suite_does_not_cover(self):
		"""The guard on the guard: a new warning added without a test fails here."""
		import inspect
		import re

		emitted = set(re.findall(r'"code": "([a-z_]+)"', inspect.getsource(health)))
		covered = {
			"migration_queue_missing",
			"grace_below_restore_window",
			"no_ignored_doctypes",
			"public_private_residue",
			"deprecated_hook",
		}
		self.assertEqual(
			emitted,
			covered,
			"a panel warning was added or removed without updating this suite",
		)

	def test_the_missing_queue_is_warned_about(self):
		from cloud_file_storage.migration import engine

		with patch.object(engine, "migration_queue_available", return_value=False):
			self.assertIn("migration_queue_missing", self.codes())

	def test_the_queue_warning_is_absent_once_the_queue_exists(self):
		from cloud_file_storage.migration import engine

		with patch.object(engine, "migration_queue_available", return_value=True):
			self.assertNotIn("migration_queue_missing", self.codes())

	def test_an_empty_ignored_list_is_warned_about(self):
		"""Patched, not saved.

		This first emptied `ignored_doctypes` on the real Single and restored it in cleanup.
		The restore did not hold: the list stayed empty, and the next full run failed eight
		tests across four modules that depend on Data Import being ignored, plus the
		settings-singleton comparison the phase gate rests on. It also self-perpetuates —
		once empty, the "original" the next run captures is already empty.

		A warning test has no business writing shared configuration to prove a branch. The
		branch reads `modes.ignored_doctypes`, so that is what is patched.
		"""
		from cloud_file_storage.storage import modes

		with patch.object(modes, "ignored_doctypes", return_value=set()):
			self.assertIn("no_ignored_doctypes", self.codes())

	def test_the_ignored_list_warning_is_absent_at_the_seeded_defaults(self):
		self.assertNotIn("no_ignored_doctypes", self.codes())

	def test_the_seeded_defaults_are_still_in_place(self):
		"""Guards the repair: if any test leaks that list empty again, this names it."""
		from cloud_file_storage.storage.modes import get_settings, ignored_doctypes

		self.assertEqual(
			ignored_doctypes(get_settings()),
			{"Data Import", "Prepared Report", "Package Import"},
		)

	def test_a_deprecated_hook_is_warned_about(self):
		from cloud_file_storage.api import compat

		finding = [{"hook": "s3_key_generator", "implementations": ["x.y"], "message": "retired hook"}]
		with patch.object(compat, "detect_deprecated_hooks", return_value=finding):
			self.assertIn("deprecated_hook", self.codes())

	def test_the_deprecated_hook_warning_is_absent_on_a_clean_site(self):
		self.assertNotIn("deprecated_hook", self.codes())

	def test_private_files_in_the_public_tree_are_warned_about(self):
		"""The condition is an unauthenticated disclosure served by nginx off disk, so the
		panel is where an operator is meant to see it."""
		finding = {"tree": "/x", "count": 3, "paths": ["a"], "truncated": False, "message": "residue"}
		with patch.object(health, "detect_public_private_residue", return_value=finding):
			self.assertIn("public_private_residue", self.codes())

	def test_the_residue_warning_is_absent_when_the_tree_is_clean(self):
		finding = {"tree": "/x", "count": 0, "paths": [], "truncated": False, "message": "none"}
		with patch.object(health, "detect_public_private_residue", return_value=finding):
			self.assertNotIn("public_private_residue", self.codes())


class TestPanelContext(StorageHealthTestCase):
	def test_it_reports_the_current_mode(self):
		set_mode("S3_ONLY")
		self.assertEqual(health.storage_health()["mode"], "S3_ONLY")

	def test_a_grace_shorter_than_the_restore_window_is_warned_about(self):
		set_mode("LOCAL_ONLY")
		frappe.db.set_single_value("Cloud Storage Settings", "object_delete_grace_days", 7)
		frappe.db.commit()
		frappe.clear_document_cache("Cloud Storage Settings", "Cloud Storage Settings")

		codes = {warning["code"] for warning in health.storage_health()["warnings"]}
		self.assertIn("grace_below_restore_window", codes)

	def test_that_warning_is_absent_at_the_shipped_defaults(self):
		"""The negative half: a warning that is always present says nothing."""
		set_mode("LOCAL_ONLY")
		frappe.db.set_single_value("Cloud Storage Settings", "object_delete_grace_days", 30)
		frappe.db.set_single_value("Cloud Storage Settings", "restore_window_days", 30)
		frappe.db.commit()
		frappe.clear_document_cache("Cloud Storage Settings", "Cloud Storage Settings")

		codes = {warning["code"] for warning in health.storage_health()["warnings"]}
		self.assertNotIn("grace_below_restore_window", codes)


def _walk(payload, path=""):
	"""Every (path, numeric value) pair in a nested payload."""
	if isinstance(payload, dict):
		for key, value in payload.items():
			yield from _walk(value, f"{path}.{key}" if path else key)
	elif isinstance(payload, list):
		for index, value in enumerate(payload):
			yield from _walk(value, f"{path}[{index}]")
	elif isinstance(payload, int) and not isinstance(payload, bool):
		yield path, payload
