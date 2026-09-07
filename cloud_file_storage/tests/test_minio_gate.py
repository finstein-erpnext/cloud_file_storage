"""The MinIO completeness gate must actually bite (A22).

The gap this closes, stated plainly: `test_minio_integration.setUpClass` raises `SkipTest`
unless `RUN_MINIO_INTEGRATION_TESTS` is exactly `"1"`, and until now **nothing on the CI
side noticed** when it did. `check_backup_completeness.sh`'s `MODULES` tuple does not
include this module, and the contract gate only knows C1..C19 — which pass storage-mocked.
So a typo in any of the six `CLOUD_FILE_STORAGE_MINIO_*` / `RUN_MINIO_INTEGRATION_TESTS`
variables in `ci.yml` would have produced a green `tests-minio` job that never touched S3,
and the only evidence would have been nineteen skip lines in a log nobody reads.

Tested the same way as its two siblings: by driving the real script with synthetic junit
reports, including the exact report shape a class-level `SkipTest` produces, because that
is the shape the gap was hiding in.
"""

import ast
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import cloud_file_storage

APP_ROOT = Path(cloud_file_storage.__file__).resolve().parent.parent
GATE = APP_ROOT / ".github" / "helper" / "check_minio_completeness.sh"
CI_WORKFLOW = APP_ROOT / ".github" / "workflows" / "ci.yml"
MODULE_PATH = APP_ROOT / "cloud_file_storage" / "tests" / "test_minio_integration.py"
MODULE = "cloud_file_storage.tests.test_minio_integration"
CLASS = f"{MODULE}.TestMinioIntegration"

HEADER = '<?xml version="1.0" encoding="UTF-8"?>'

#: The reason the module itself gives, reproduced so the synthetic reports are the real shape.
SKIP_REASON = "set RUN_MINIO_INTEGRATION_TESTS=1 to run MinIO integration tests"


def required_identities() -> dict[str, str]:
	"""The `REQUIRED` map, read out of the gate script rather than copied into this file."""
	text = GATE.read_text(encoding="utf-8")
	pairs = re.findall(r'"([^"]+)":\s*f"\{MODULE\}\.([A-Za-z0-9_.]+)"', text)
	return {label: f"{MODULE}.{suffix}" for label, suffix in pairs}


def _case(identity: str, body: str = "") -> str:
	classname, _, name = identity.rpartition(".")
	attributes = f'classname="{classname}" name="{name}"'
	if not body:
		return f"  <testcase {attributes}/>"
	return f"  <testcase {attributes}>{body}</testcase>"


def _skipped(identity: str, reason: str = SKIP_REASON) -> str:
	return _case(identity, f'<skipped message="{reason}"/>')


def _report(cases: list[str]) -> str:
	return "\n".join([HEADER, '<testsuite name="cfs">', *cases, "</testsuite>", ""])


class TestMinioCompletenessGate(unittest.TestCase):
	def setUp(self):
		self.required = required_identities()
		self.assertEqual(
			len(self.required),
			19,
			"the gate must name every real-S3 test; nineteen is what the module ships",
		)

	def _run(self, junit: str | None, *, with_module: bool = True):
		with tempfile.TemporaryDirectory() as tmp:
			workspace = Path(tmp)
			(workspace / ".github" / "helper").mkdir(parents=True)
			(workspace / ".github" / "helper" / GATE.name).write_bytes(GATE.read_bytes())

			if with_module:
				tests_dir = workspace / "cloud_file_storage" / "tests"
				tests_dir.mkdir(parents=True)
				(tests_dir / "test_minio_integration.py").write_text("", encoding="utf-8")

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

	def test_every_real_s3_test_executed_passes(self):
		"""The positive control: a gate that failed on everything would prove nothing below."""
		code, out = self._run(_report([_case(i) for i in self.required.values()]))
		self.assertEqual(code, 0, out)
		self.assertIn("MinIO completeness OK", out)

	def test_the_env_var_typo_case_fails(self):
		"""Nineteen individual skips — what an unset or mistyped variable actually produces."""
		code, out = self._run(_report([_skipped(i) for i in self.required.values()]))
		self.assertEqual(code, 1, out)
		self.assertIn("was SKIPPED", out)
		self.assertIn("CLOUD_FILE_STORAGE_MINIO_", out)

	def test_a_class_level_setupclass_skip_fails(self):
		"""The report shape a `setUpClass` SkipTest really produces: one case, empty classname."""
		synthetic = f'  <testcase classname="" name="setUpClass ({CLASS})"><skipped message="{SKIP_REASON}"/></testcase>'
		code, out = self._run(_report([synthetic]))
		self.assertEqual(code, 1, out)
		self.assertIn("the whole MinIO class was SKIPPED", out)
		self.assertIn(SKIP_REASON, out)

	def test_one_skipped_test_fails(self):
		identities = list(self.required.values())
		cases = [_case(i) for i in identities[1:]] + [_skipped(identities[0])]
		code, out = self._run(_report(cases))
		self.assertEqual(code, 1, out)
		self.assertIn(identities[0], out)

	def test_a_missing_test_fails(self):
		identities = list(self.required.values())
		code, out = self._run(_report([_case(i) for i in identities[1:]]))
		self.assertEqual(code, 1, out)
		self.assertIn("absent from the report", out)
		self.assertIn(identities[0], out)

	def test_a_failed_test_fails_the_gate(self):
		identities = list(self.required.values())
		cases = [_case(i) for i in identities[1:]]
		cases.append(_case(identities[0], '<failure message="boom"/>'))
		code, out = self._run(_report(cases))
		self.assertEqual(code, 1, out)
		self.assertIn("failed:", out)

	def test_an_empty_report_fails(self):
		code, out = self._run(_report([]))
		self.assertEqual(code, 1, out)
		self.assertIn("no MinIO testcase appears", out)

	def test_a_missing_junit_report_fails(self):
		code, out = self._run(None)
		self.assertEqual(code, 1, out)
		self.assertIn("no junit report", out)

	def test_a_missing_module_fails_rather_than_no_ops(self):
		code, out = self._run(
			_report([_case(i) for i in self.required.values()]),
			with_module=False,
		)
		self.assertEqual(code, 1, out)
		self.assertIn("module is missing", out)

	def test_the_gate_runs_inside_the_minio_job(self):
		"""Present in the file is not enough — it has to be a step of the job it guards."""
		workflow = CI_WORKFLOW.read_text(encoding="utf-8")
		self.assertIn("check_minio_completeness.sh", workflow)

		minio_job = workflow[workflow.index("  tests-minio:") :]
		# The next top-level job begins the block that is no longer ours.
		next_job = re.search(r"\n  [a-z0-9-]+:\n", minio_job[len("  tests-minio:") :])
		if next_job:
			minio_job = minio_job[: len("  tests-minio:") + next_job.start()]
		self.assertIn("check_minio_completeness.sh", minio_job)
		self.assertIn('RUN_MINIO_INTEGRATION_TESTS: "1"', minio_job)

	def test_every_identity_the_gate_demands_exists_in_the_module(self):
		"""A gate may demand any list; it is worth having only if the list resolves."""
		found = set()
		for node in ast.walk(ast.parse(MODULE_PATH.read_text(encoding="utf-8"))):
			if not isinstance(node, ast.ClassDef):
				continue
			for child in node.body:
				if isinstance(child, ast.FunctionDef) and child.name.startswith("test_"):
					found.add(f"{MODULE}.{node.name}.{child.name}")

		self.assertEqual(
			sorted(set(self.required.values()) ^ found),
			[],
			"the gate's list and the module's tests have drifted apart",
		)
