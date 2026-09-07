"""The P6 completeness gate must actually bite (PLAN §B P6 row, F2).

PLAN's P6 exit is "the T-BACKUP suite plus all refusal guards", and a refusal guard that
stops running is the quietest possible regression: nothing downstream notices a check that
never fired. So CI names the guards, and this file drives the real script with synthetic
junit reports to prove the naming is enforced in both directions.

The last test is the one that keeps it honest: a gate may demand any list of identities,
and is worth having only if every identity on it exists.
"""

import ast
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import cloud_file_storage

APP_ROOT = Path(cloud_file_storage.__file__).resolve().parent.parent
GATE = APP_ROOT / ".github" / "helper" / "check_backup_completeness.sh"
CI_WORKFLOW = APP_ROOT / ".github" / "workflows" / "ci.yml"
TESTS_DIR = APP_ROOT / "cloud_file_storage" / "tests"

MODULE_ALIASES = {
	"TASKS": "cloud_file_storage.tests.test_backup",
	"LIFECYCLE": "cloud_file_storage.tests.test_backup_lifecycle",
	"RESTORE": "cloud_file_storage.tests.test_backup_restore",
	"PERMS": "cloud_file_storage.tests.test_backup_permissions",
	"CONTRACT": "cloud_file_storage.tests.contract.test_file_compat_contract",
	"DESK": "cloud_file_storage.tests.test_desk_permissions",
}

HEADER = '<?xml version="1.0" encoding="UTF-8"?>'


def required_identities() -> dict[str, str]:
	"""The `REQUIRED` map, read out of the gate script itself, never duplicated here."""
	text = GATE.read_text(encoding="utf-8")
	pairs = re.findall(
		r'"([^"]+)":\s*f"\{(TASKS|LIFECYCLE|RESTORE|PERMS|CONTRACT|DESK)\}\.([A-Za-z0-9_.]+)"', text
	)
	return {label: f"{MODULE_ALIASES[alias]}.{suffix}" for label, alias, suffix in pairs}


def _case(identity: str, body: str = "") -> str:
	classname, _, name = identity.rpartition(".")
	attributes = f'classname="{classname}" name="{name}"'
	if not body:
		return f"  <testcase {attributes}/>"
	return f"  <testcase {attributes}>{body}</testcase>"


def _report(cases: list[str]) -> str:
	return "\n".join([HEADER, '<testsuite name="cfs">', *cases, "</testsuite>", ""])


class TestBackupCompletenessGate(unittest.TestCase):
	def setUp(self):
		self.required = required_identities()
		self.assertGreaterEqual(
			len(self.required), 30, "the gate must name every P6 refusal guard, not a handful"
		)

	def _run(self, junit: str | None, *, with_module: bool = True):
		with tempfile.TemporaryDirectory() as tmp:
			workspace = Path(tmp)
			(workspace / ".github" / "helper").mkdir(parents=True)
			(workspace / ".github" / "helper" / GATE.name).write_bytes(GATE.read_bytes())

			if with_module:
				tests_dir = workspace / "cloud_file_storage" / "tests"
				tests_dir.mkdir(parents=True)
				(tests_dir / "test_backup.py").write_text("", encoding="utf-8")

			junit_path = workspace / "junit.xml"
			if junit is not None:
				junit_path.write_text(junit, encoding="utf-8")

			proc = subprocess.run(
				["bash", str(workspace / ".github" / "helper" / GATE.name)],
				env={
					"PATH": "/usr/bin:/bin:/usr/local/bin",
					"GITHUB_WORKSPACE": str(workspace),
					"JUNIT_XML": str(junit_path),
				},
				capture_output=True,
				text=True,
			)
			return proc.returncode, proc.stdout + proc.stderr

	def test_every_required_guard_executed_passes(self):
		code, out = self._run(_report([_case(i) for i in self.required.values()]))
		self.assertEqual(code, 0, out)
		self.assertIn("backup completeness OK", out)

	def test_a_missing_guard_fails(self):
		identities = list(self.required.values())
		code, out = self._run(_report([_case(i) for i in identities[:-1]]))
		self.assertEqual(code, 1, out)
		self.assertIn("did not run", out)
		self.assertIn(identities[-1], out)

	def test_a_skipped_guard_fails(self):
		identities = list(self.required.values())
		cases = [_case(i) for i in identities[:-1]]
		cases.append(_case(identities[-1], '<skipped message="no bucket"/>'))
		code, out = self._run(_report(cases))
		self.assertEqual(code, 1, out)
		self.assertIn("SKIPPED", out)

	def test_a_failed_guard_fails(self):
		identities = list(self.required.values())
		cases = [_case(i) for i in identities[:-1]]
		cases.append(_case(identities[-1], '<failure message="boom"/>'))
		code, out = self._run(_report(cases))
		self.assertEqual(code, 1, out)
		self.assertIn("failed", out)

	def test_a_skipped_non_required_backup_test_still_fails(self):
		cases = [_case(i) for i in self.required.values()]
		cases.append(
			_case(
				"cloud_file_storage.tests.test_backup.TestBackupKeyLayout.test_something_else",
				'<skipped message="no bucket"/>',
			)
		)
		code, out = self._run(_report(cases))
		self.assertEqual(code, 1, out)
		self.assertIn("test_something_else", out)

	def test_an_empty_report_fails(self):
		code, out = self._run(_report([]))
		self.assertEqual(code, 1, out)
		self.assertIn("no backup testcase appears", out)

	def test_a_missing_report_fails(self):
		code, out = self._run(None)
		self.assertEqual(code, 1, out)
		self.assertIn("no junit report", out)

	def test_a_missing_suite_fails(self):
		code, out = self._run(_report([]), with_module=False)
		self.assertEqual(code, 1, out)
		self.assertIn("backup suite is missing", out)

	def test_a_class_level_skip_is_recognised(self):
		name = "setUpClass (cloud_file_storage.tests.test_backup.TestBackupFreshness)"
		cases = [f'  <testcase classname="" name="{name}"><skipped message="no site"/></testcase>']
		code, out = self._run(_report(cases))
		self.assertEqual(code, 1, out)
		self.assertIn("whole backup class was SKIPPED", out)

	def test_the_gate_is_wired_into_ci(self):
		self.assertIn("check_backup_completeness.sh", CI_WORKFLOW.read_text(encoding="utf-8"))

	def test_every_identity_the_gate_demands_actually_exists(self):
		"""A gate can demand any list; it is only worth having if the list resolves."""
		found: set[str] = set()
		sources = sorted(TESTS_DIR.glob("test_backup*.py"))
		# Two named guards live outside the backup modules — the C10 matcher-symmetry test in
		# the contract suite, and the four L-6 permlevel-patch refusals in the Desk permission
		# suite. The scan has to reach both or this check would report them missing.
		sources.append(TESTS_DIR / "contract" / "test_file_compat_contract.py")
		sources.append(TESTS_DIR / "test_desk_permissions.py")
		for path in sources:
			module = f"cloud_file_storage.tests.{path.stem}"
			if path.parent.name == "contract":
				module = f"cloud_file_storage.tests.contract.{path.stem}"
			for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
				if not isinstance(node, ast.ClassDef):
					continue
				for child in node.body:
					if isinstance(child, ast.FunctionDef) and child.name.startswith("test_"):
						found.add(f"{module}.{node.name}.{child.name}")

		self.assertGreater(len(found), 50, "no backup tests were parsed; the check would be vacuous")
		missing = sorted(set(self.required.values()) - found)
		self.assertEqual(missing, [], f"the gate names tests that do not exist: {missing}")


class TestEveryMarkedGuardIsWatched(unittest.TestCase):
	"""F-2 — the direction the gate could not check about itself.

	`TestBackupCompletenessGate` proves the gate notices when a NAMED guard stops running.
	Nothing made anybody name one: a refusal guard could be written, pass, and be watched by
	no gate at all. `@refusal_guard` closes that from the test's own side, and these two tests
	assert both halves — that the marked set really is a subset of REQUIRED here and now, and
	that the shipped gate script fails when it is not.
	"""

	def marked_identities(self) -> dict[str, str]:
		marked = {}
		for path in sorted(TESTS_DIR.rglob("*.py")):
			module = ".".join(path.relative_to(APP_ROOT).with_suffix("").parts)
			for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
				if not isinstance(node, ast.ClassDef):
					continue
				for child in node.body:
					if not isinstance(child, ast.FunctionDef):
						continue
					names = {getattr(d, "id", None) or getattr(d, "attr", None) for d in child.decorator_list}
					if "refusal_guard" in names:
						marked[f"{module}.{node.name}.{child.name}"] = str(path)
		return marked

	def test_every_marked_refusal_guard_is_named_by_the_gate(self):
		marked = self.marked_identities()
		self.assertGreater(len(marked), 50, "the walk found almost no marked guards; it is vacuous")
		unlisted = sorted(set(marked) - set(required_identities().values()))
		self.assertEqual(
			unlisted,
			[],
			"these tests are marked @refusal_guard but no CI gate watches them: " + ", ".join(unlisted),
		)

	def _run_gate_against(self, workspace: Path) -> tuple[int, str]:
		(workspace / ".github" / "helper").mkdir(parents=True, exist_ok=True)
		(workspace / ".github" / "helper" / GATE.name).write_bytes(GATE.read_bytes())
		junit_path = workspace / "junit.xml"
		junit_path.write_text(_report([_case(i) for i in required_identities().values()]), encoding="utf-8")
		proc = subprocess.run(
			["bash", str(workspace / ".github" / "helper" / GATE.name)],
			env={
				"PATH": "/usr/bin:/bin:/usr/local/bin",
				"GITHUB_WORKSPACE": str(workspace),
				"JUNIT_XML": str(junit_path),
			},
			capture_output=True,
			text=True,
		)
		return proc.returncode, proc.stdout + proc.stderr

	def test_the_shipped_gate_refuses_a_marked_guard_it_does_not_name(self):
		"""Driven against the real script, over a real copy of the real tests tree.

		The tree is copied rather than stubbed so the walk sees the ~60 guards it normally
		sees — otherwise the floor below would fire first and this test would pass for the
		wrong reason.
		"""
		with tempfile.TemporaryDirectory() as tmp:
			workspace = Path(tmp)
			tests_dir = workspace / "cloud_file_storage" / "tests"
			shutil.copytree(TESTS_DIR, tests_dir)
			(tests_dir / "test_invented.py").write_text(
				"from cloud_file_storage.tests.markers import refusal_guard\n"
				"\n"
				"class TestInvented:\n"
				"\t@refusal_guard\n"
				"\tdef test_a_guard_nobody_listed(self):\n"
				"\t\tpass\n",
				encoding="utf-8",
			)
			code, output = self._run_gate_against(workspace)

		self.assertEqual(code, 1, output)
		self.assertIn("is not in this gate's REQUIRED map", output)
		self.assertIn("test_a_guard_nobody_listed", output)

	def test_the_shipped_gate_refuses_a_tree_whose_guards_have_all_vanished(self):
		"""The floor. Without it, a walk over a moved or renamed tree would pass vacuously."""
		with tempfile.TemporaryDirectory() as tmp:
			workspace = Path(tmp)
			tests_dir = workspace / "cloud_file_storage" / "tests"
			tests_dir.mkdir(parents=True)
			(tests_dir / "test_backup.py").write_text("", encoding="utf-8")
			(tests_dir / "markers.py").write_bytes((TESTS_DIR / "markers.py").read_bytes())
			code, output = self._run_gate_against(workspace)

		self.assertEqual(code, 1, output)
		self.assertIn("the walk found almost none", output)
