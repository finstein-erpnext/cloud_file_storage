"""A1's frozen tolerance, and the load witness that makes the comparison meaningful.

A1 asks whether the ERP's own queues stay responsive *while the migration runs*. The verdict
is therefore only as trustworthy as the claim that the loaded run was loaded. This cycle
produced a concrete near-miss: a loaded probe read `883` Pending objects at its start and
`883` at its end and looked like a stalled migration, when in fact 6 migration jobs executed
inside that window. The reading was wrong because MariaDB runs REPEATABLE-READ here and both
counts came from one long-lived transaction — measured directly, same-transaction counts
returned 0 and 0 across a committed insert and 1 only after a rollback.

The dangerous failure is the mirror image: if a "loaded" run genuinely carries no load, every
queue sits at its baseline and A1 passes trivially. That is why an unwitnessed pair returns
`INVALID_MEASUREMENT` and can never return `passed: True`, no matter how good the latencies
look. The thresholds themselves are frozen constants — never derived from the measurement.
"""

import json
import os
import tempfile
import unittest

from cloud_file_storage.tests.rehearsal import (
	A1_MAX_DELTA_SECONDS,
	A1_MAX_RATIO,
	A1_MIN_SAMPLES,
	ERP_QUEUES,
	_is_migration_job,
	_job_rows,
	_migration_jobs_on_erp,
	_queue_of,
	a1_verdict,
)


def _mig_job_id(tail: str) -> str:
	"""A job id exactly as the engine + frappe would render it on *this* version.

	v15 namespaces ``<site>::<id>``; v16 rewrites ``:`` to ``|`` and joins with ``||``.
	Hardcoding either spelling is what let the v16 marker mismatch go unnoticed, so the
	positive fixtures are rendered rather than written out. The negative fixtures
	(probe pings, empty ids) stay literal on purpose -- they are what keeps
	``_is_migration_job`` able to fail.
	"""
	from frappe.utils.background_jobs import create_job_id

	return create_job_id(f"cfs::{tail}")


def _queues(p95: float, *, completed: int = 35, failures: int = 0) -> dict:
	return {q: {"p95_seconds": p95, "completed": completed, "failures": failures} for q in ERP_QUEUES}


def _probe(p95: float, *, load: str, completed: int = 35, failures: int = 0, on_erp: int = 0) -> dict:
	return {
		"queues": _queues(p95, completed=completed, failures=failures),
		"load_witness": {
			"load_state": load,
			"migration_jobs_on_erp_queues": on_erp,
			"migration_jobs_during_probe": 6 if load == "LOAD_PRESENT" else 0,
		},
	}


class TestA1Verdict(unittest.TestCase):
	def test_valid_witnessed_pair_within_tolerance_passes(self):
		v = a1_verdict(_probe(0.30, load="NO_LOAD"), _probe(0.45, load="LOAD_PRESENT"))
		self.assertEqual(v["measurement_state"], "VALID_MEASUREMENT")
		self.assertTrue(v["passed"])

	def test_unwitnessed_loaded_run_cannot_pass_however_good_the_latencies(self):
		"""The anti-vacuity case: identical p95s pass every threshold and must still not pass."""
		v = a1_verdict(_probe(0.30, load="NO_LOAD"), _probe(0.30, load="NO_LOAD"))
		self.assertEqual(v["measurement_state"], "INVALID_MEASUREMENT")
		self.assertFalse(v["passed"])
		self.assertTrue(all(q["pass"] for q in v["per_queue"].values()))

	def test_missing_witness_entirely_is_invalid(self):
		loaded = {"queues": _queues(0.35)}
		v = a1_verdict(_probe(0.30, load="NO_LOAD"), loaded)
		self.assertEqual(v["measurement_state"], "INVALID_MEASUREMENT")
		self.assertFalse(v["passed"])

	def test_baseline_taken_under_load_is_invalid(self):
		v = a1_verdict(_probe(0.30, load="LOAD_PRESENT"), _probe(0.35, load="LOAD_PRESENT"))
		self.assertEqual(v["measurement_state"], "INVALID_MEASUREMENT")
		self.assertIn("baseline", " ".join(v["measurement_reasons"]))
		self.assertFalse(v["passed"])

	def test_delta_at_the_frozen_bound_is_inclusive_pass(self):
		v = a1_verdict(_probe(0.30, load="NO_LOAD"), _probe(0.30 + A1_MAX_DELTA_SECONDS, load="LOAD_PRESENT"))
		self.assertTrue(v["passed"])

	def test_delta_one_millisecond_over_fails(self):
		v = a1_verdict(
			_probe(0.30, load="NO_LOAD"), _probe(0.30 + A1_MAX_DELTA_SECONDS + 0.001, load="LOAD_PRESENT")
		)
		self.assertFalse(v["passed"])

	def test_ratio_at_the_frozen_bound_is_inclusive_pass(self):
		v = a1_verdict(_probe(0.10, load="NO_LOAD"), _probe(0.10 * A1_MAX_RATIO, load="LOAD_PRESENT"))
		self.assertTrue(v["passed"])

	def test_ratio_over_the_bound_fails_even_when_delta_is_tiny(self):
		v = a1_verdict(_probe(0.010, load="NO_LOAD"), _probe(0.0201 + 0.0, load="LOAD_PRESENT"))
		self.assertFalse(v["passed"])
		self.assertIn("ratio", " ".join(v["per_queue"]["short"]["reasons"]))

	def test_any_probe_failure_fails(self):
		v = a1_verdict(_probe(0.30, load="NO_LOAD"), _probe(0.31, load="LOAD_PRESENT", failures=1))
		self.assertFalse(v["passed"])

	def test_fewer_than_thirty_completed_probes_fails(self):
		v = a1_verdict(
			_probe(0.30, load="NO_LOAD"), _probe(0.31, load="LOAD_PRESENT", completed=A1_MIN_SAMPLES - 1)
		)
		self.assertFalse(v["passed"])

	def test_one_bad_queue_fails_the_whole_gate(self):
		loaded = _probe(0.31, load="LOAD_PRESENT")
		loaded["queues"]["long"]["p95_seconds"] = 0.30 + A1_MAX_DELTA_SECONDS + 0.5
		v = a1_verdict(_probe(0.30, load="NO_LOAD"), loaded)
		self.assertFalse(v["passed"])
		self.assertTrue(v["per_queue"]["short"]["pass"])
		self.assertFalse(v["per_queue"]["long"]["pass"])

	def test_migration_job_on_an_erp_queue_is_derived_from_the_witness_and_fails(self):
		v = a1_verdict(_probe(0.30, load="NO_LOAD"), _probe(0.31, load="LOAD_PRESENT", on_erp=2))
		self.assertEqual(v["migration_jobs_on_erp_queues"], 2)
		self.assertFalse(v["passed"])

	def test_explicit_override_still_honoured(self):
		v = a1_verdict(
			_probe(0.30, load="NO_LOAD"), _probe(0.31, load="LOAD_PRESENT"), migration_jobs_on_erp=1
		)
		self.assertFalse(v["passed"])


class TestLoadWitnessHelpers(unittest.TestCase):
	def test_queue_of_strips_the_bench_prefix(self):
		self.assertEqual(_queue_of({"queue": "home-user-v15:cloud_migration"}), "cloud_migration")
		self.assertEqual(_queue_of({}), "")

	def test_probe_pings_on_erp_queues_are_not_violations(self):
		"""A1 enqueues ~105 pings onto these three queues; the instrument is not the finding."""
		rows = [
			{"queue": "home-user-v15:short", "job_id": "cfs-probe-short-123-0"},
			{"queue": "home-user-v15:default", "job_id": "cfs-probe-default-123-0"},
			{"queue": "home-user-v15:long", "job_id": "cfs-probe-long-123-0"},
		]
		self.assertEqual(_migration_jobs_on_erp(rows), 0)

	def test_a_real_migration_job_on_an_erp_queue_is_a_violation(self):
		rows = [
			{"queue": "site:cloud_migration", "job_id": _mig_job_id("CAMP-1::UploadDispatched::B1")},
			{"queue": "site:long", "job_id": _mig_job_id("CAMP-1::UploadDispatched::B2")},
			{"queue": "site:short", "job_id": _mig_job_id("CAMP-1::VerifyDispatched::B3")},
			{"queue": "site:short", "job_id": "cfs-probe-short-9-1"},
		]
		self.assertEqual(_migration_jobs_on_erp(rows), 2)

	def test_migration_marker_distinguishes_the_two(self):
		self.assertTrue(_is_migration_job({"job_id": _mig_job_id("C::UploadDispatched::B1")}))
		self.assertFalse(_is_migration_job({"job_id": "cfs-probe-short-1-0"}))
		self.assertFalse(_is_migration_job({}))

	def test_job_rows_filters_to_the_probe_window(self):
		with tempfile.TemporaryDirectory() as tmp:
			path = os.path.join(tmp, "jobs.jsonl")
			with open(path, "w") as fh:
				for at in (100.0, 150.0, 200.0, 250.0):
					fh.write(json.dumps({"at": at, "queue": "b:cloud_migration"}) + "\n")
				fh.write("not json\n")
				fh.write("\n")
			_paths, rows = _job_rows(path, 150.0, 200.0)
			self.assertEqual([r["at"] for r in rows], [150.0, 200.0])

	def test_job_rows_merges_several_logs(self):
		"""Migration progress and ERP-queue contamination are written by different workers."""
		with tempfile.TemporaryDirectory() as tmp:
			mig = os.path.join(tmp, "mig.jsonl")
			erp = os.path.join(tmp, "erp.jsonl")
			with open(mig, "w") as fh:
				fh.write(
					json.dumps({"at": 10.0, "queue": "s:cloud_migration", "job_id": _mig_job_id("C::U::B1")})
					+ "\n"
				)
			with open(erp, "w") as fh:
				fh.write(
					json.dumps({"at": 11.0, "queue": "s:long", "job_id": _mig_job_id("C::U::B2")}) + "\n"
				)
			_paths, rows = _job_rows([mig, erp], 0.0, 100.0)
			self.assertEqual(len(rows), 2)
			self.assertEqual(_migration_jobs_on_erp(rows), 1)

	def test_one_missing_log_does_not_lose_the_other(self):
		with tempfile.TemporaryDirectory() as tmp:
			good = os.path.join(tmp, "good.jsonl")
			with open(good, "w") as fh:
				fh.write(
					json.dumps({"at": 5.0, "queue": "s:cloud_migration", "job_id": _mig_job_id("C::U::B1")})
					+ "\n"
				)
			paths, rows = _job_rows([good, os.path.join(tmp, "absent.jsonl")], 0.0, 100.0)
			self.assertEqual(len(rows), 1)
			self.assertEqual(len(paths), 2)

	def test_missing_job_log_yields_no_rows_rather_than_raising(self):
		self.assertEqual(_job_rows("/nonexistent/path/jobs.jsonl", 0.0, 1e12)[1], [])


class TestFrozenBoundIsNotDefeatedByFloatNoise(unittest.TestCase):
	"""The bound is inclusive, and binary floating point must not silently make it exclusive.

	`0.30 + A1_MAX_DELTA_SECONDS` is 0.55 to a human and 0.5500000000000000444 to the machine,
	so the naive subtraction exceeded the threshold by 6e-17 and failed a measurement the report
	simultaneously described as exactly on the bound.
	"""

	def test_delta_bound_holds_for_many_baselines(self):
		# Baselines >= the delta bound, so the ratio condition is not the one under test here:
		# a 0.010s baseline plus 250ms is a 26x regression and A1 fails it on ratio, correctly.
		for base in (0.25, 0.30, 0.3333, 0.7, 1.234):
			with self.subTest(base=base):
				v = a1_verdict(
					_probe(base, load="NO_LOAD"),
					_probe(base + A1_MAX_DELTA_SECONDS, load="LOAD_PRESENT"),
				)
				self.assertEqual(v["per_queue"]["short"]["delta"], A1_MAX_DELTA_SECONDS)
				self.assertTrue(v["passed"], v["per_queue"]["short"]["reasons"])

	def test_just_over_the_bound_still_fails_after_rounding(self):
		v = a1_verdict(
			_probe(0.30, load="NO_LOAD"), _probe(0.30 + A1_MAX_DELTA_SECONDS + 0.001, load="LOAD_PRESENT")
		)
		self.assertFalse(v["passed"])
