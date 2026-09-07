"""The L3 ecosystem completeness gate must actually bite (F5 / A22).

The ecosystem module skips loudly when erpnext or India Compliance is absent — right for
the unit site, catastrophic on the L3 job, where a skip means the three apps were never
installed and the job would otherwise report green having tested nothing. That is the same
failure the C1..C19 gate exists to prevent, so it is tested the same way: by driving the
real script with synthetic junit reports.

The last test here is the one that keeps the gate honest over time. A gate is free to
demand any list of test identities; it is only worth having if those identities exist.
"""

import ast
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import cloud_file_storage

APP_ROOT = Path(cloud_file_storage.__file__).resolve().parent.parent
GATE = APP_ROOT / ".github" / "helper" / "check_ecosystem_completeness.sh"
MODULE_PATH = APP_ROOT / "cloud_file_storage" / "tests" / "test_ecosystem.py"
MODULE = "cloud_file_storage.tests.test_ecosystem"

HEADER = '<?xml version="1.0" encoding="UTF-8"?>'


def required_identities() -> dict[str, str]:
	"""The `REQUIRED` map, read out of the gate script itself.

	Parsed rather than duplicated: a copy here would keep asserting the old list after the
	gate's changed, which is precisely the drift the last test is looking for.
	"""
	text = GATE.read_text(encoding="utf-8")
	pairs = re.findall(r'"([^"]+)":\s*f"\{MODULE\}\.([A-Za-z0-9_.]+)"', text)
	return {label: f"{MODULE}.{suffix}" for label, suffix in pairs}


def _case(identity: str, body: str = "") -> str:
	classname, _, name = identity.rpartition(".")
	attributes = f'classname="{classname}" name="{name}"'
	if not body:
		return f"  <testcase {attributes}/>"
	return f"  <testcase {attributes}>{body}</testcase>"


def _report(cases: list[str]) -> str:
	return "\n".join([HEADER, '<testsuite name="cfs">', *cases, "</testsuite>", ""])


class TestEcosystemCompletenessGate(unittest.TestCase):
	def setUp(self):
		self.required = required_identities()
		self.assertTrue(self.required, "no REQUIRED entries were parsed out of the gate script")

	def _run(self, junit: str | None, *, with_module: bool = True):
		"""Run the gate in a throwaway workspace; return (returncode, combined output)."""
		with tempfile.TemporaryDirectory() as tmp:
			workspace = Path(tmp)
			(workspace / ".github" / "helper").mkdir(parents=True)
			(workspace / ".github" / "helper" / GATE.name).write_bytes(GATE.read_bytes())

			if with_module:
				tests_dir = workspace / "cloud_file_storage" / "tests"
				tests_dir.mkdir(parents=True)
				(tests_dir / "test_ecosystem.py").write_text("", encoding="utf-8")

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

	def test_every_required_test_executed_passes(self):
		code, out = self._run(_report([_case(i) for i in self.required.values()]))
		self.assertEqual(code, 0, out)
		self.assertIn("ecosystem completeness OK", out)

	def test_all_skipped_fails(self):
		"""The regression this file exists for: the L3 job's whole point is not skipping."""
		skipped = '<skipped message="ecosystem test needs erpnext installed on this site"/>'
		code, out = self._run(_report([_case(i, skipped) for i in self.required.values()]))
		self.assertEqual(code, 1, out)
		self.assertIn("SKIPPED", out)

	def test_one_skipped_required_test_fails(self):
		identities = list(self.required.values())
		cases = [_case(i) for i in identities[:-1]]
		cases.append(_case(identities[-1], '<skipped message="no india_compliance"/>'))
		code, out = self._run(_report(cases))
		self.assertEqual(code, 1, out)
		self.assertIn(identities[-1].rsplit(".", 1)[-1], out)

	def test_a_missing_required_test_fails(self):
		identities = list(self.required.values())
		code, out = self._run(_report([_case(i) for i in identities[:-1]]))
		self.assertEqual(code, 1, out)
		self.assertIn("did not run", out)
		self.assertIn(identities[-1], out)

	def test_a_failed_required_test_fails_the_gate(self):
		identities = list(self.required.values())
		cases = [_case(i) for i in identities[:-1]]
		cases.append(_case(identities[-1], '<failure message="boom"/>'))
		code, out = self._run(_report(cases))
		self.assertEqual(code, 1, out)
		self.assertIn("failed", out)

	def test_a_skipped_non_required_ecosystem_test_still_fails(self):
		"""A skip anywhere in the module means the apps were not there."""
		cases = [_case(i) for i in self.required.values()]
		cases.append(_case(f"{MODULE}.TestItemImage.test_something_else", '<skipped message="no erpnext"/>'))
		code, out = self._run(_report(cases))
		self.assertEqual(code, 1, out)
		self.assertIn("test_something_else", out)

	def test_a_class_level_setupclass_skip_is_recognised_as_a_skip(self):
		"""The shape a real run produces when the ecosystem apps are absent.

		unittest reports a `setUpClass` SkipTest as one synthetic case per class, with an
		EMPTY classname and the module path inside the name. Taken from an actual run on
		`cfs-autonomous.local`, not from a guess about the format — a gate matching only on
		classname fails with "absent from the report" and buries the message that says the
		apps were never installed.
		"""
		reason = "ecosystem test needs erpnext, india_compliance installed on this site"
		cases = [
			'  <testcase classname="" name="setUpClass '
			f'({MODULE}.TestItemImage)"><skipped message="{reason}"/></testcase>'
		]
		code, out = self._run(_report(cases))

		self.assertEqual(code, 1, out)
		self.assertIn("whole ecosystem class was SKIPPED", out)
		self.assertIn("TestItemImage", out)
		self.assertNotIn("no ecosystem testcase appears", out)

	def test_a_report_without_any_ecosystem_case_fails(self):
		code, out = self._run(_report([_case("cloud_file_storage.tests.test_gc.TestGC.test_x")]))
		self.assertEqual(code, 1, out)
		self.assertIn("no ecosystem testcase", out)

	def test_a_missing_junit_report_fails(self):
		code, out = self._run(None)
		self.assertEqual(code, 1, out)
		self.assertIn("no junit report", out)

	def test_a_missing_module_fails_rather_than_no_ops(self):
		"""Unlike the C1..C19 gate, this module ships now; absence is a deletion, not a phase."""
		code, out = self._run(_report([_case(i) for i in self.required.values()]), with_module=False)
		self.assertEqual(code, 1, out)
		self.assertIn("ecosystem module is missing", out)

	def test_every_identity_the_gate_demands_exists_in_the_module(self):
		"""Without this the gate can be satisfied by a list of tests nobody ever wrote.

		Parsed with `ast`, not imported: importing the module pulls in frappe and, on a site
		without erpnext, would skip the very thing being checked.
		"""
		tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
		present = {
			f"{MODULE}.{node.name}.{child.name}"
			for node in tree.body
			if isinstance(node, ast.ClassDef)
			for child in node.body
			if isinstance(child, ast.FunctionDef) and child.name.startswith("test_")
		}
		self.assertTrue(present, "no test methods were parsed out of the ecosystem module")

		for label, identity in sorted(self.required.items()):
			self.assertIn(identity, present, f"the gate demands {label!r}, which no test provides")
