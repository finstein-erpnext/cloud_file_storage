"""The CI contract-completeness gate must actually bite (A22 / ACCEPTANCE_GATES F1).

A22 requires C1..C19 to be "junit-asserted per ref, zero silent skips". The gate is only
worth shipping if a report in which the C-tests were collected-but-skipped FAILS it — the
original grep-based implementation reported "all executed" for a run in which all nineteen
were skipped, which is the precise failure mode A22 exists to prevent.

These tests drive `.github/helper/check_contract_completeness.sh` with synthetic junit
reports. They need no site and no contract suite of their own.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

import cloud_file_storage

APP_ROOT = Path(cloud_file_storage.__file__).resolve().parent.parent
GATE = APP_ROOT / ".github" / "helper" / "check_contract_completeness.sh"

HEADER = '<?xml version="1.0" encoding="UTF-8"?>'


def _case(number: int, body: str = "") -> str:
	name = f'classname="contract.TestFileCompatContract" name="test_C{number}_invariant"'
	if not body:
		return f"  <testcase {name}/>"
	return f"  <testcase {name}>{body}</testcase>"


def _report(cases: list[str]) -> str:
	return "\n".join([HEADER, '<testsuite name="cfs">', *cases, "</testsuite>", ""])


class TestContractCompletenessGate(unittest.TestCase):
	def _run(self, junit: str | None, *, with_contract_dir: bool = True):
		"""Run the gate in a throwaway workspace; return (returncode, combined output)."""
		with tempfile.TemporaryDirectory() as tmp:
			workspace = Path(tmp)
			(workspace / ".github" / "helper").mkdir(parents=True)
			(workspace / ".github" / "helper" / GATE.name).write_bytes(GATE.read_bytes())

			if with_contract_dir:
				(workspace / "cloud_file_storage" / "tests" / "contract").mkdir(parents=True)

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

	def test_all_nineteen_executed_passes(self):
		code, out = self._run(_report([_case(n) for n in range(1, 20)]))
		self.assertEqual(code, 0, out)
		self.assertIn("C1..C19 all executed", out)

	def test_all_nineteen_skipped_fails(self):
		"""The regression this file exists for: skipped must never read as executed."""
		skipped = '<skipped message="boto3 missing"/>'
		code, out = self._run(_report([_case(n, skipped) for n in range(1, 20)]))
		self.assertEqual(code, 1, out)
		self.assertIn("SKIPPED", out)

	def test_one_skipped_invariant_fails(self):
		cases = [_case(n) for n in range(1, 19)]
		cases.append(_case(19, '<skipped message="no MinIO"/>'))
		code, out = self._run(_report(cases))
		self.assertEqual(code, 1, out)
		self.assertIn("C19", out)

	def test_absent_invariant_fails(self):
		cases = [_case(n) for n in range(1, 20) if n != 7]
		code, out = self._run(_report(cases))
		self.assertEqual(code, 1, out)
		self.assertIn("C7", out)

	def test_failing_invariant_fails(self):
		cases = [_case(n) for n in range(1, 20) if n != 3]
		cases.append(_case(3, '<failure message="boom"/>'))
		code, out = self._run(_report(cases))
		self.assertEqual(code, 1, out)
		self.assertIn("C3", out)

	def test_c1_is_not_matched_inside_c19(self):
		"""Token boundaries: a report containing only C19 must not satisfy C1."""
		code, out = self._run(_report([_case(19)]))
		self.assertEqual(code, 1, out)
		self.assertIn("C1", out)

	def test_non_contract_testcase_cannot_satisfy_an_invariant(self):
		"""A stray test whose name contains C3 must not count as the C3 invariant."""
		cases = [_case(n) for n in range(1, 20) if n != 3]
		cases.append('  <testcase classname="tests.test_misc.TestMisc" name="test_C3_unrelated"/>')
		code, out = self._run(_report(cases))
		self.assertEqual(code, 1, out)
		self.assertIn("C3", out)

	def test_report_with_no_contract_case_at_all_fails(self):
		"""If the suite exists but nothing contract-shaped ran, fail loudly, never green."""
		cases = ['  <testcase classname="tests.test_misc.TestMisc" name="test_something"/>']
		code, out = self._run(_report(cases))
		self.assertEqual(code, 1, out)
		self.assertIn("no contract testcase", out)

	def test_missing_junit_fails_when_the_suite_exists(self):
		code, out = self._run(None)
		self.assertEqual(code, 1, out)
		self.assertIn("no junit report", out)

	def test_unparseable_junit_fails(self):
		code, out = self._run("<testsuite><testcase name='test_C1'>")
		self.assertEqual(code, 1, out)
		self.assertIn("not parseable", out)

	def test_noop_until_the_contract_suite_lands(self):
		"""P1 ships the gate before P2 ships the suite; that must not fail CI."""
		code, out = self._run(_report([]), with_contract_dir=False)
		self.assertEqual(code, 0, out)
		self.assertIn("not present yet", out)
