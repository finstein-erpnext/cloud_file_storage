"""The P6 ⇄ P7 seam: backup findings are **detected** in `backup/health`, **rendered** here.

The failure this suite exists to prevent is a second detector. If the panel grew its own
staleness arithmetic, a site would have two answers to "is the backup stale" and an operator
would act on whichever surface they opened. So the tests assert the findings arrive
unchanged — same objects, same order, same wording — rather than merely that a plausible
list appears.

**Post-merge reality.** The backup module IS present now — it arrived with P6 — so the
pre-merge tripwire that asserted its absence has been retired in favour of
`TestTheRealBackupModuleIsWiredAtConvergence`, which checks the seam resolves to the real
module and the panel renders its findings verbatim.

Both directions are still asserted. The `available: False` branch is reached by substituting
the seam rather than by the module genuinely being missing. It is unreachable on a shipped
install — but `health.py` carries the branch, and a branch nobody drives is a branch nobody
notices breaking.
"""

import types
from unittest.mock import patch

import frappe

from cloud_file_storage import health
from cloud_file_storage.tests.desk_utils import CLOUD_STORAGE_MANAGER, acting_as, drop_users, ensure_user
from cloud_file_storage.tests.utils import CloudStorageTestCase

STUB_FINDINGS = [
	{"level": "error", "code": "backup_stale", "message": "The last cloud backup was 71.0 hours ago"},
	{"level": "warn", "code": "tarballs_after_cutover", "message": "still tarring local files"},
	{"level": "warn", "code": "lifecycle_may_have_drifted", "message": "retention settings changed"},
]

STUB_RECENT = [{"name": "LOG-0001", "status": "Success", "total_bytes": 4096}]


def stub_backup_module(findings=None, recent=None, calls=None):
	"""A stand-in for `cloud_file_storage.backup.health`, returned by the patched seam.

	Faithful to the real module's documented return shape (`backup/health.backup_health`):
	`{"enabled", "findings": [{level, code, message}], "last_backup_on", "last_backup_status"}`.

	Substituted through `health._backup_health_module` rather than through `sys.modules`.
	Injecting a module into `sys.modules` was tried first and is wrong here: the Settings
	Single is pickled on cache write, and with the module table mutated pickle resolves the
	controller class to a different object than the instance's own, so twelve tests errored
	inside `frappe.get_cached_doc` — a failure with nothing to do with the property under
	test.
	"""
	module = types.ModuleType("cloud_file_storage.backup.health")

	def backup_health():
		if calls is not None:
			calls.append("backup_health")
		return {
			"enabled": True,
			"findings": findings if findings is not None else STUB_FINDINGS,
			"last_backup_on": "2026-08-13 02:00:00",
			"last_backup_status": "Success",
		}

	def recent_backups(limit=5):
		if calls is not None:
			calls.append(f"recent_backups({limit})")
		return recent if recent is not None else STUB_RECENT

	module.backup_health = backup_health
	module.recent_backups = recent_backups
	return module


def patched_backup(**kwargs):
	return patch.object(health, "_backup_health_module", return_value=stub_backup_module(**kwargs))


class BackupPanelTestCase(CloudStorageTestCase):
	def section(self, **kwargs) -> dict:
		with patched_backup(**kwargs):
			return health.storage_health()["backup"]


class TestTheRealBackupModuleIsWiredAtConvergence(CloudStorageTestCase):
	"""Replaces P7's pre-merge tripwire, which asserted the backup module was ABSENT.

	P7 built that deliberately: in its isolated worktree `cloud_file_storage.backup` did not
	exist, so a seam test could have passed for the wrong reason for ever. Asserting the
	precondition made the seam fail LOUDLY at the P6 merge instead of rotting into a vacuous
	pass — and it did exactly that, failing both of its tests on the first merged run.

	This is what it was forcing. With P6 present, the panel must render P6's OWN findings
	rather than a stand-in, and the seam must resolve to the real module.
	"""

	def test_the_backup_module_is_importable_now(self):
		from cloud_file_storage.backup import health as backup_health

		self.assertTrue(callable(backup_health.backup_health))

	def test_the_seam_resolves_to_the_real_module(self):
		from cloud_file_storage.backup import health as backup_health

		self.assertIs(health._backup_health_module(), backup_health)

	def test_the_panel_renders_the_real_module_s_own_findings(self):
		"""Verbatim, not paraphrased — the panel is a renderer, not a second detector."""
		from cloud_file_storage.backup import health as backup_health

		section = health.storage_health()["backup"]
		self.assertTrue(section["available"])

		expected = backup_health.backup_health()["findings"]
		self.assertTrue(
			expected,
			"no findings to compare — this assertion would pass while proving nothing",
		)
		self.assertEqual(
			[(f["code"], f["level"], f["message"]) for f in section["findings"]],
			[(f["code"], f["level"], f["message"]) for f in expected],
		)


class TestTheSectionSaysSoWhenTheModuleIsAbsent(CloudStorageTestCase):
	"""The `available: False` branch, kept covered after the tripwire was retired."""

	def test_the_panel_says_so_and_invents_nothing(self):
		with patch.object(health, "_backup_health_module", return_value=None):
			section = health.storage_health()["backup"]
		self.assertFalse(section["available"])
		self.assertIn("backup module", section["reason"])
		self.assertNotIn("findings", section)


class TestFindingsArriveUnchanged(BackupPanelTestCase):
	def test_the_findings_are_the_module_s_own(self):
		section = self.section()
		self.assertTrue(section["available"])
		self.assertTrue(section["visible"])
		self.assertEqual(section["findings"], STUB_FINDINGS)

	def test_no_finding_is_reworded_reordered_or_added(self):
		"""A panel that paraphrases is a second source of truth with extra steps."""
		section = self.section()
		self.assertEqual(
			[(finding["code"], finding["level"], finding["message"]) for finding in section["findings"]],
			[(finding["code"], finding["level"], finding["message"]) for finding in STUB_FINDINGS],
		)

	def test_an_empty_report_stays_empty(self):
		"""The other direction: the panel must not manufacture a finding of its own."""
		section = self.section(findings=[])
		self.assertEqual(section["findings"], [])

	def test_the_module_is_actually_called(self):
		calls: list[str] = []
		self.section(calls=calls)
		self.assertEqual(calls, ["backup_health", "recent_backups(5)"])

	def test_the_history_strip_comes_from_the_module_too(self):
		section = self.section()
		self.assertEqual(section["recent"], STUB_RECENT)

	def test_the_last_backup_fields_are_passed_through(self):
		section = self.section()
		self.assertEqual(section["last_backup_status"], "Success")
		self.assertEqual(section["last_backup_on"], "2026-08-13 02:00:00")


class TestThePanelDoesNotDetectBackupConditions(CloudStorageTestCase):
	"""Structural: no copy of P6's arithmetic can appear here without failing this."""

	def source(self) -> str:
		import inspect

		return inspect.getsource(health)

	def test_health_py_names_no_backup_doctype_or_field(self):
		source = self.source()
		for token in (
			"Cloud Backup Settings",
			"Cloud Storage Backup Log",
			"lifecycle_applied_hash",
			"include_public_files",
			"include_private_files",
			"FREQUENCY_HOURS",
			"time_diff_in_hours",
		):
			with self.subTest(token=token):
				self.assertNotIn(token, source)

	def test_the_only_backup_import_is_the_delegation(self):
		import ast
		import inspect

		tree = ast.parse(inspect.getsource(health))
		importers = {
			node.lineno: node
			for node in ast.walk(tree)
			if isinstance(node, ast.ImportFrom) and "backup" in (node.module or "")
		}
		self.assertEqual(len(importers), 1, "more than one place imports the backup domain")

		function = next(
			node
			for node in ast.walk(tree)
			if isinstance(node, ast.FunctionDef) and node.name == "_backup_health_module"
		)
		self.assertIn(
			next(iter(importers)),
			range(function.lineno, function.end_lineno + 1),
			"the backup import is not inside the _backup_health_module seam",
		)


class TestBackupFindingsNeverTouchTheByteAccounting(BackupPanelTestCase):
	"""PLAN §A: the two byte quantities stay distinct, and a finding is not bytes.

	`tarballs_after_cutover` is *about* local bytes, which is exactly the finding most likely
	to be folded into a total by someone tidying the panel.
	"""

	def test_the_byte_sections_are_identical_with_and_without_findings(self):
		# Only the byte sections are compared here. Asserting anything about the findings
		# themselves would make this fail for reasons its name does not cover — which it did
		# on the first mutation run, alongside the three tests that were meant to catch it.
		with patched_backup(findings=[]):
			quiet = health.storage_health()
		with patched_backup():
			noisy = health.storage_health()

		for key in ("cloud_managed", "local_operational", "local_unmigrated"):
			with self.subTest(section=key):
				self.assertEqual(quiet[key], noisy[key])

	def test_the_backup_section_carries_no_byte_total_of_its_own(self):
		section = self.section()
		for key in section:
			with self.subTest(key=key):
				self.assertNotIn("bytes", key)


class TestVisibilityFollowsP6sGate(BackupPanelTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.operator = ensure_user("cfs-backup-operator@example.com", (CLOUD_STORAGE_MANAGER,))

	@classmethod
	def tearDownClass(cls):
		drop_users(cls.operator)
		super().tearDownClass()

	def test_a_system_manager_sees_the_findings(self):
		self.assertTrue(self.section()["visible"])

	def test_the_operator_role_is_told_it_is_withheld_rather_than_shown_nothing(self):
		"""`backup/health.get_backup_health` is System Manager only; this does not widen it."""
		with acting_as(self.operator), patched_backup():
			section = health.storage_health()["backup"]

		self.assertTrue(section["available"])
		self.assertFalse(section["visible"])
		self.assertIn("System Manager", section["reason"])
		self.assertNotIn("findings", section)

	def test_the_module_is_not_even_called_for_the_operator_role(self):
		"""Withheld means not computed, not computed-then-hidden."""
		calls: list[str] = []
		with acting_as(self.operator), patched_backup(calls=calls):
			health.storage_health()
		self.assertEqual(calls, [])

	def test_the_rest_of_the_panel_still_renders_for_the_operator(self):
		with acting_as(self.operator), patched_backup():
			payload = health.storage_health()
		self.assertIn("Cloud-managed", payload["cloud_managed"]["label"])
