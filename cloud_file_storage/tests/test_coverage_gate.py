"""The coverage gate's own anti-vacuity suite.

These drive `.github/helper/check_coverage.sh` **as shipped**, through `subprocess`, with
synthetic coverage artifacts. Testing a reimplementation of the gate would test a copy of the
logic rather than the artifact CI runs — the distinction this project has failed gates over.

The gate separates two verdicts that had been conflated:

    exit 1  COVERAGE FAILURE        the combined corpus is below the floor
    exit 2  GATE EXECUTION FAILURE  missing / corrupt / foreign / empty artifact, or no tool

That separation is the point. On `dcb2ada` two matrix jobs reported 76% and failed, and the
message said coverage had fallen below the floor. Nothing had regressed: a whole-corpus floor
was being applied to deliberately partial shards.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from coverage import CoverageData

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / ".github" / "helper" / "check_coverage.sh"

EXIT_OK = 0
EXIT_COVERAGE_FAILURE = 1
EXIT_GATE_FAILURE = 2

SHA = "a" * 40
OTHER_SHA = "b" * 40


class CoverageGateTestCase(unittest.TestCase):
	"""Builds real coverage databases over a real module, so percentages are genuine."""

	TOTAL_STATEMENTS = 100
	FOREIGN_BENCH = "/home/runner/frappe-bench/apps/cloud_file_storage"

	def setUp(self):
		self.tmp = Path(tempfile.mkdtemp())
		self.addCleanup(shutil.rmtree, self.tmp, True)
		self.artifacts = self.tmp / "artifacts"
		self.artifacts.mkdir()
		# The gate filters on `*cloud_file_storage*`, so the measured file must match it. The
		# directory is named exactly `cloud_file_storage` because the alias pattern
		# `*/apps/cloud_file_storage` rewrites onto `<source_root>/<tail>`, and in a real
		# checkout that tail begins with the package directory of that name.
		pkg = self.tmp / "cloud_file_storage"
		pkg.mkdir()
		self.module = pkg / "measured.py"
		# A module deliberately OUTSIDE the `*cloud_file_storage*` include pattern.
		other = self.tmp / "unrelated_package"
		other.mkdir()
		self.out_of_scope_module = other / "elsewhere.py"
		self.out_of_scope_module.write_text("y = 1\n", encoding="utf-8")
		self.module.write_text(
			"".join(f"x{i} = {i}\n" for i in range(1, self.TOTAL_STATEMENTS + 1)), encoding="utf-8"
		)

	def _shard(
		self,
		name,
		covered_lines,
		*,
		sha=SHA,
		corrupt=False,
		empty=False,
		out_of_scope=False,
		foreign_root=False,
	):
		"""Write one shard artifact exactly as a CI job would upload it."""
		d = self.artifacts / f"coverage-{name}"
		d.mkdir(parents=True, exist_ok=True)
		(d / "coverage-sha.txt").write_text(sha + "\n", encoding="utf-8")
		target = d / f".coverage.{name}"

		if empty:
			target.touch()
			return d
		if corrupt:
			target.write_bytes(b"this is not a coverage database, it is 43 bytes")
			return d

		measured = self.out_of_scope_module if out_of_scope else self.module
		if foreign_root:
			# What CI actually records: an absolute path under a bench that does not exist in
			# the job running this gate.
			measured = Path(self.FOREIGN_BENCH) / "cloud_file_storage" / self.module.name
		data = CoverageData(basename=str(target))
		data.add_lines({str(measured): list(covered_lines)})
		data.write()
		# CoverageData writes to <basename> exactly; confirm the harness itself is not vacuous.
		if not target.exists():  # pragma: no cover - defensive
			raise AssertionError("the synthetic shard was not written; the harness is broken")
		return d

	def _run(self, shards, *, sha=SHA, floor=90, source_root=None):
		env = dict(os.environ)
		if source_root is not None:
			env["COVERAGE_SOURCE_ROOT"] = str(source_root)
		else:
			env.pop("COVERAGE_SOURCE_ROOT", None)
		env.update(
			COVERAGE_ARTIFACTS_DIR=str(self.artifacts),
			COVERAGE_EXPECTED_SHARDS=" ".join(shards),
			COVERAGE_SHA=sha,
			COVERAGE_FLOOR=str(floor),
			COVERAGE_PYTHON=sys.executable,
			COVERAGE_INCLUDE="*cloud_file_storage*",
		)
		return subprocess.run(["bash", str(GATE)], env=env, capture_output=True, text=True, timeout=180)


class TestTheCombinedFloorIsWhatIsEnforced(CoverageGateTestCase):
	def test_a_union_at_or_above_the_floor_passes(self):
		"""Requirement 4."""
		self._shard("alpha", range(1, 61))
		self._shard("beta", range(55, 96))  # union = lines 1..95 = 95%

		result = self._run(["alpha", "beta"])

		self.assertEqual(result.returncode, EXIT_OK, f"stdout={result.stdout}\nstderr={result.stderr}")
		self.assertIn("coverage gate OK", result.stdout)

	def test_a_union_below_the_floor_is_a_coverage_failure_not_a_gate_failure(self):
		"""Requirement 3 — 89% must fail, and must fail as COVERAGE, not as infrastructure."""
		self._shard("alpha", range(1, 46))
		self._shard("beta", range(40, 90))  # union = lines 1..89 = 89%

		result = self._run(["alpha", "beta"])

		self.assertEqual(result.returncode, EXIT_COVERAGE_FAILURE, f"stdout={result.stdout}")
		self.assertIn("COVERAGE FAILURE", result.stdout + result.stderr)

	def test_one_partial_shard_at_76_percent_does_not_fail_before_aggregation(self):
		"""Requirement 5 — the exact `dcb2ada` failure, which was never a coverage problem.

		A shard at 76% is not a defect: the shards are deliberately partial. It may only be
		judged after the union is formed, and here the union clears the floor.
		"""
		self._shard("partial", range(1, 77))  # 76% alone — the number CI reported and failed on
		self._shard("rest", range(70, 96))  # union = 95%

		result = self._run(["partial", "rest"])

		self.assertEqual(
			result.returncode,
			EXIT_OK,
			"a 76% shard failed the gate before aggregation — this is the misapplied "
			f"whole-corpus floor that failed every matrix job on dcb2ada.\nstdout={result.stdout}",
		)


class TestTheGateCannotPassVacuously(CoverageGateTestCase):
	def test_a_missing_shard_is_a_gate_execution_failure(self):
		"""Requirement 1 — a skipped or failed job must not silently shrink the corpus."""
		self._shard("alpha", range(1, 100))  # on its own this would clear the floor

		result = self._run(["alpha", "beta"])

		self.assertEqual(result.returncode, EXIT_GATE_FAILURE, f"stdout={result.stdout}")
		self.assertIn("no coverage artifact for required shard 'beta'", result.stdout + result.stderr)

	def test_corrupt_coverage_data_is_a_gate_execution_failure(self):
		"""Requirement 2."""
		self._shard("alpha", range(1, 100))
		self._shard("beta", [], corrupt=True)

		result = self._run(["alpha", "beta"])

		self.assertEqual(result.returncode, EXIT_GATE_FAILURE, f"stdout={result.stdout}")
		self.assertIn("unreadable or corrupt", result.stdout + result.stderr)

	def test_an_empty_artifact_cannot_produce_a_false_green(self):
		"""Requirement 6 — a 0-byte file must be caught, not quietly skipped by `combine`."""
		self._shard("alpha", range(1, 100))
		self._shard("beta", [], empty=True)

		result = self._run(["alpha", "beta"])

		self.assertEqual(result.returncode, EXIT_GATE_FAILURE, f"stdout={result.stdout}")
		self.assertIn("EMPTY", result.stdout + result.stderr)

	def test_shards_measuring_only_out_of_scope_code_cannot_make_the_gate_green(self):
		"""The subtler empty: valid databases that measured nothing the floor is about.

		A shard can be perfectly readable and still contribute zero to the package's coverage.
		If every shard is like that, `coverage report` has nothing in scope to report — and the
		one outcome that must never follow is a pass. This asserts the gate refuses rather than
		treating "nothing to measure" as "nothing wrong".
		"""
		self._shard("alpha", [1], out_of_scope=True)
		self._shard("beta", [1], out_of_scope=True)

		result = self._run(["alpha", "beta"])

		self.assertNotEqual(
			result.returncode,
			EXIT_OK,
			"the gate went green on shards that measured no in-scope code at all.\n"
			f"stdout={result.stdout}\nstderr={result.stderr}",
		)

	def test_a_shard_from_another_commit_is_refused(self):
		"""Only artifacts belonging to the exact candidate SHA may be combined."""
		self._shard("alpha", range(1, 100))
		self._shard("beta", range(1, 100), sha=OTHER_SHA)

		result = self._run(["alpha", "beta"])

		self.assertEqual(result.returncode, EXIT_GATE_FAILURE, f"stdout={result.stdout}")
		self.assertIn("not the candidate", result.stdout + result.stderr)

	def test_an_undeclared_shard_list_is_refused(self):
		"""A gate that expects nothing passes on an empty directory — the vacuity to prevent."""
		env = dict(os.environ)
		env.update(
			COVERAGE_ARTIFACTS_DIR=str(self.artifacts),
			COVERAGE_SHA=SHA,
			COVERAGE_PYTHON=sys.executable,
		)
		env.pop("COVERAGE_EXPECTED_SHARDS", None)

		result = subprocess.run(["bash", str(GATE)], env=env, capture_output=True, text=True, timeout=120)

		self.assertEqual(result.returncode, EXIT_GATE_FAILURE)
		self.assertIn("cannot fail", result.stdout + result.stderr)


class TestTheGateCanResolveSourcesItDidNotMeasure(CoverageGateTestCase):
	"""C-1 — the condition CI is always in, which the rest of this suite never reproduced.

	frappe measures `<bench>/apps/<app>` and coverage stores ABSOLUTE canonicalised paths, so
	every shard names a bench path. The combining job has no bench — only a checkout. Verified
	by reproduction: `coverage report` must re-parse each source to count statements, raises
	`NoSource`, and exits 1; the gate mapped that to GATE EXECUTION FAILURE, so CI would have
	been red on every run with no coverage number ever computed.

	The original 17 tests could not see it: they write their measured module to a real path that
	still exists when the gate runs. They reproduced a convenient shape rather than the shipped
	one — the same failure class as the defect that started this whole cycle.
	"""

	def test_shards_naming_a_foreign_bench_still_produce_a_verdict(self):
		"""The fix: `[paths]` aliasing maps the recorded roots onto the tree that exists here."""
		self._shard("alpha", range(1, 96), foreign_root=True)

		result = self._run(["alpha"], source_root=self.module.parent.parent)

		self.assertNotEqual(
			result.returncode,
			EXIT_GATE_FAILURE,
			"the gate could not resolve sources recorded under a bench it cannot see. This is "
			f"the every-run CI failure.\nstdout={result.stdout}\nstderr={result.stderr}",
		)
		self.assertEqual(result.returncode, EXIT_OK, f"stdout={result.stdout}")
		self.assertIn("Aliasing shard source roots", result.stdout)

	def test_without_aliasing_the_failure_is_reported_as_the_gate_problem_it_is(self):
		"""The control: unresolvable sources must be a GATE failure naming the actual remedy.

		This is what the gate did before the fix — correct as a *classification*, since nothing
		about coverage had dropped. What was missing was aliasing, and the message now says so
		instead of leaving the reader to guess.
		"""
		self._shard("alpha", range(1, 96), foreign_root=True)

		result = self._run(["alpha"], source_root=None)

		self.assertEqual(result.returncode, EXIT_GATE_FAILURE, f"stdout={result.stdout}")
		self.assertIn("COVERAGE_SOURCE_ROOT", result.stdout + result.stderr)

	def test_a_source_root_that_resolves_nothing_says_so_rather_than_blaming_the_knob(self):
		"""The rc-1 branch, which was itself untested — in a suite whose thesis is the opposite.

		Once `COVERAGE_SOURCE_ROOT` IS configured, an rc of 1 no longer means "no source root";
		it means a measured file did not resolve under the one given. Pointing the reader back at
		a knob that is already correct is the misattribution this gate's header exists to prevent.
		"""
		self._shard("alpha", range(1, 96), foreign_root=True)

		# Exists (so the source-root check passes) but contains no `cloud_file_storage/` tree,
		# so aliasing lands on nothing.
		result = self._run(["alpha"], source_root=self.out_of_scope_module.parent)

		self.assertEqual(result.returncode, EXIT_GATE_FAILURE, f"stdout={result.stdout}")
		self.assertIn("a measured file did not resolve", result.stdout + result.stderr)

	def test_a_source_root_that_does_not_exist_is_refused(self):
		"""An alias destination that is absent would silently alias onto nothing."""
		self._shard("alpha", range(1, 96), foreign_root=True)

		result = self._run(["alpha"], source_root=self.tmp / "no-such-tree")

		self.assertEqual(result.returncode, EXIT_GATE_FAILURE, f"stdout={result.stdout}")
		self.assertIn("does not exist", result.stdout + result.stderr)


class TestTheGateDefaultsAreNotWeakened(CoverageGateTestCase):
	"""The floor and the include pattern are parameterised for these tests. Pin the defaults so
	a change to them is a visible edit here rather than a quiet change of meaning in CI."""

	def test_the_default_floor_is_ninety(self):
		text = GATE.read_text(encoding="utf-8")
		self.assertIn('COVERAGE_FLOOR="${COVERAGE_FLOOR:-90}"', text)

	def test_the_default_include_is_the_package(self):
		text = GATE.read_text(encoding="utf-8")
		self.assertIn('COVERAGE_INCLUDE="${COVERAGE_INCLUDE:-*cloud_file_storage*}"', text)

	def test_the_gate_declares_no_omit_patterns(self):
		"""Hiding uncovered code behind `--omit` is the prohibited remedy."""
		text = GATE.read_text(encoding="utf-8")
		self.assertNotIn("--omit", text)


class TestTheWorkflowAndTheGateAgree(unittest.TestCase):
	"""The wiring guard: the shards the gate REQUIRES must be the shards CI actually PUBLISHES.

	These can drift apart silently and in the dangerous direction. Add a frappe ref to the
	matrix without adding it to `COVERAGE_EXPECTED_SHARDS` and its coverage is published,
	quietly ignored, and never required — the union is judged without it and the gate still
	reports success. Nothing else in CI would notice.
	"""

	@classmethod
	def setUpClass(cls):
		import yaml

		cls.workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text())
		cls.jobs = cls.workflow["jobs"]

	def _matrix_refs(self):
		"""Every frappe ref the `tests` job runs, from either matrix shape.

		A bare `frappe_ref: [...]` list and an `include:` list of dicts are both valid
		GitHub matrices and the runner treats them identically. The job moved to `include`
		when the v16 leg was added, because each ref must be pinned to its own interpreter
		(v15 and v16 have disjoint `requires-python`). Reading only the bare form made this
		gate raise KeyError rather than check anything.
		"""
		matrix = self.jobs["tests"]["strategy"]["matrix"]
		if "frappe_ref" in matrix:
			return list(matrix["frappe_ref"])
		refs = [entry["frappe_ref"] for entry in matrix["include"] if "frappe_ref" in entry]
		self.assertTrue(refs, "the tests matrix declares no frappe_ref in either form")
		return refs

	def _expected_shards(self):
		env = self.jobs["coverage"]["steps"][-1]["env"]
		return set(env["COVERAGE_EXPECTED_SHARDS"].split())

	def test_every_matrix_ref_is_a_required_shard(self):
		refs = self._matrix_refs()
		expected = self._expected_shards()
		for ref in refs:
			self.assertIn(
				f"frappe-{ref}",
				expected,
				f"frappe ref {ref!r} runs in CI but 'frappe-{ref}' is not in "
				f"COVERAGE_EXPECTED_SHARDS, so its coverage would never be required",
			)

	def test_the_minio_and_ecosystem_shards_are_required(self):
		expected = self._expected_shards()
		self.assertIn("minio", expected)
		self.assertIn("ecosystem", expected)

	def test_no_expected_shard_is_unpublished(self):
		"""The other direction: a required shard nobody uploads is a permanent gate failure."""
		published = {
			step["with"]["name"]
			for job in self.jobs.values()
			for step in job.get("steps", [])
			if isinstance(step, dict) and str(step.get("uses", "")).startswith("actions/upload-artifact")
			if step.get("with", {}).get("name", "").startswith("coverage-")
		}
		refs = self._matrix_refs()
		# The matrix name is templated; resolve it the way the runner would.
		resolved = {n for n in published if "matrix.frappe_ref" not in n}
		if any("matrix.frappe_ref" in n for n in published):
			resolved |= {f"coverage-frappe-{r}" for r in refs}

		for shard in self._expected_shards():
			self.assertIn(
				f"coverage-{shard}",
				resolved,
				f"shard {shard!r} is required by the gate but no job publishes "
				f"'coverage-{shard}' — the gate would fail every run for a wiring reason",
			)

	def test_the_coverage_job_waits_for_every_publishing_job(self):
		needs = set(self.jobs["coverage"]["needs"])
		for job_name in ("tests", "tests-minio", "tests-ecosystem"):
			self.assertIn(
				job_name,
				needs,
				f"the coverage job does not depend on {job_name!r}, so it can run before that "
				f"job's shard exists",
			)

	def test_the_gate_is_given_a_source_root_to_alias_onto(self):
		"""Without this the gate cannot return a verdict at all — C-1, in one line of YAML.

		The shards record `<bench>/apps/cloud_file_storage/...`; this job has only a checkout.
		Without a root to alias onto, `coverage report` cannot re-parse the sources it must count
		statements from, exits 1, and the gate reports GATE EXECUTION FAILURE on every run.

		This test exists because deleting that one line leaves every other test in this file
		passing while CI goes red permanently — the exact silent-drift class this class was
		written for, aimed at the newest and most load-bearing knob.
		"""
		env = self.jobs["coverage"]["steps"][-1]["env"]

		self.assertIn(
			"COVERAGE_SOURCE_ROOT",
			env,
			"the coverage job does not set COVERAGE_SOURCE_ROOT, so the shards' bench paths "
			"cannot be resolved and the gate can never produce a coverage number",
		)
		self.assertTrue(str(env["COVERAGE_SOURCE_ROOT"]).strip(), "COVERAGE_SOURCE_ROOT is empty")

	def test_the_shard_uploads_include_hidden_files(self):
		"""`.coverage.*` is dot-prefixed; upload-artifact drops hidden files by default.

		Without this the artifact uploads containing only the sha stamp, the gate reports
		'contains no .coverage data file', and the cause looks like a missing test run.
		"""
		for job_name, job in self.jobs.items():
			for step in job.get("steps", []):
				if not isinstance(step, dict):
					continue
				with_ = step.get("with", {}) or {}
				if str(with_.get("name", "")).startswith("coverage-"):
					self.assertTrue(
						with_.get("include-hidden-files"),
						f"{job_name}: coverage artifact upload does not set include-hidden-files",
					)


if __name__ == "__main__":
	unittest.main()
