"""The load witness itself, driven — not fabricated by the fixture that asserts on it.

An independent review of the A1 work found that every existing A1 test asserted on a
`load_witness` dict its own helper had written, so the single most dangerous mutation in the
delta survived the whole suite:

    witness["load_state"] = "LOAD_PRESENT"      # unconditional

49/49 green. That mutation turns A1 into precisely the vacuous gate the feature exists to
prevent — a "loaded" run that carries no load passes every threshold, because an idle machine
is what makes the delta small. The nine mutations that *were* caught all lived inside
`a1_verdict` and its pure helpers, which is the code that already had tests: the mutation set
had been drawn from the tested region, which is circular.

These tests drive the assembly instead, by injecting its two real inputs — a job log on disk
and the fresh-snapshot progress count — and asserting the state that comes out.
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from cloud_file_storage.tests import rehearsal
from cloud_file_storage.tests.rehearsal import (
	A1_MAX_DELTA_SECONDS,
	A1_MAX_RATIO,
	A1_MIN_SAMPLES,
	ERP_QUEUES,
	_load_state,
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


def _witness(*, first: int, second: int, total: int | None = None, rows_read: int = 1) -> dict:
	return {
		"migration_jobs_first_half": first,
		"migration_jobs_second_half": second,
		"migration_jobs_during_probe": total if total is not None else first + second,
		"job_log_rows_read": rows_read,
	}


class TestTheFrozenConstantsArePinned(unittest.TestCase):
	"""The owner froze these on 2026-08-18. Nothing mechanical held them.

	Every A1 fixture derives its numbers *from* the constants, so `A1_MIN_SAMPLES = 30 -> 5`
	left 23/23 green and the ">=30 probes each side" clause was enforced by nothing but the
	current text of one line.
	"""

	def test_the_owner_ruling_values(self):
		self.assertEqual(A1_MAX_DELTA_SECONDS, 0.250)
		self.assertEqual(A1_MAX_RATIO, 2.0)
		self.assertEqual(A1_MIN_SAMPLES, 30)
		self.assertEqual(ERP_QUEUES, ("short", "default", "long"))


class TestLoadStateRequiresCoverageNotAnInstant(unittest.TestCase):
	"""'A migration job ran at some point' is not 'the window was loaded'."""

	def test_jobs_in_both_halves_is_load_present(self):
		self.assertEqual(_load_state(_witness(first=3, second=3), progressed=300), "LOAD_PRESENT")

	def test_jobs_only_in_the_first_half_is_partial_not_present(self):
		"""3 objects in the first 2s of a 36s probe: 34s of idle machine would have certified."""
		self.assertEqual(_load_state(_witness(first=3, second=0), progressed=3), "PARTIAL_LOAD")

	def test_jobs_only_in_the_second_half_is_partial(self):
		self.assertEqual(_load_state(_witness(first=0, second=3), progressed=3), "PARTIAL_LOAD")

	def test_progress_without_any_logged_job_is_partial_not_present(self):
		self.assertEqual(_load_state(_witness(first=0, second=0), progressed=300), "PARTIAL_LOAD")

	def test_nothing_at_all_is_no_load(self):
		self.assertEqual(_load_state(_witness(first=0, second=0), progressed=0), "NO_LOAD")

	def test_partial_load_cannot_carry_a_loaded_run(self):
		base = {"queues": _queues(0.30), "load_witness": _witness(first=0, second=0)}
		base["load_witness"]["load_state"] = "NO_LOAD"
		loaded = {"queues": _queues(0.31), "load_witness": _witness(first=3, second=0)}
		loaded["load_witness"]["load_state"] = "PARTIAL_LOAD"
		v = a1_verdict(base, loaded)
		self.assertEqual(v["measurement_state"], "INVALID_MEASUREMENT")
		self.assertFalse(v["passed"])

	def test_partial_load_on_the_baseline_also_invalidates(self):
		base = {"queues": _queues(0.30), "load_witness": _witness(first=1, second=0)}
		base["load_witness"]["load_state"] = "PARTIAL_LOAD"
		loaded = {"queues": _queues(0.31), "load_witness": _witness(first=3, second=3)}
		loaded["load_witness"]["load_state"] = "LOAD_PRESENT"
		v = a1_verdict(base, loaded)
		self.assertEqual(v["measurement_state"], "INVALID_MEASUREMENT")
		self.assertFalse(v["passed"])


def _queues(p95: float) -> dict:
	return {q: {"p95_seconds": p95, "completed": 35, "failures": 0} for q in ERP_QUEUES}


class TestAnUnreadInstrumentIsNotACleanInstrument(unittest.TestCase):
	def test_zero_rows_read_invalidates_the_measurement(self):
		base = {
			"queues": _queues(0.30),
			"load_witness": dict(
				_witness(first=0, second=0, rows_read=0),
				load_state="NO_LOAD",
				job_logs_missing=["/nope.jsonl"],
			),
		}
		loaded = {
			"queues": _queues(0.31),
			"load_witness": dict(_witness(first=3, second=3), load_state="LOAD_PRESENT"),
		}
		v = a1_verdict(base, loaded)
		self.assertEqual(v["measurement_state"], "INVALID_MEASUREMENT")
		self.assertIn("0 job-log rows", " ".join(v["measurement_reasons"]))


class TestErpViolationsFromEitherSide(unittest.TestCase):
	def test_a_violation_recorded_on_the_baseline_is_not_dropped(self):
		base = {
			"queues": _queues(0.30),
			"load_witness": dict(
				_witness(first=0, second=0), load_state="NO_LOAD", migration_jobs_on_erp_queues=2
			),
		}
		loaded = {
			"queues": _queues(0.31),
			"load_witness": dict(
				_witness(first=3, second=3), load_state="LOAD_PRESENT", migration_jobs_on_erp_queues=0
			),
		}
		v = a1_verdict(base, loaded)
		self.assertEqual(v["migration_jobs_on_erp_queues"], 2)
		self.assertFalse(v["passed"])

	def test_an_explicit_zero_override_cannot_suppress_a_witnessed_violation(self):
		base = {
			"queues": _queues(0.30),
			"load_witness": dict(_witness(first=0, second=0), load_state="NO_LOAD"),
		}
		loaded = {
			"queues": _queues(0.31),
			"load_witness": dict(
				_witness(first=3, second=3), load_state="LOAD_PRESENT", migration_jobs_on_erp_queues=3
			),
		}
		v = a1_verdict(base, loaded, migration_jobs_on_erp=0)
		self.assertEqual(v["migration_jobs_on_erp_queues"], 3)
		self.assertFalse(v["passed"])


class TestTheWitnessIsAssembledFromRealJobLogs(unittest.TestCase):
	"""Drive `_assemble_witness` against files on disk — no fabricated witness dict."""

	def _assemble(self, rows, *, extra_logs=(), start=100.0, end=200.0):
		with tempfile.TemporaryDirectory() as tmp:
			log = os.path.join(tmp, "jobs.jsonl")
			with open(log, "w") as fh:
				for row in rows:
					fh.write(json.dumps(row) + "\n")
			logs = [log] + [os.path.join(tmp, name) for name in extra_logs]
			return rehearsal._assemble_witness("C", logs, start, end, migration_queue="cloud_migration")

	@staticmethod
	def _mig(at, queue="cloud_migration"):
		return {"at": at, "queue": f"site:{queue}", "job_id": _mig_job_id("C::UploadDispatched::B1")}

	def test_jobs_spanning_the_window_give_load_present(self):
		w = self._assemble([self._mig(110.0), self._mig(190.0)])
		self.assertEqual((w["migration_jobs_first_half"], w["migration_jobs_second_half"]), (1, 1))
		self.assertEqual(_load_state(w, progressed=300), "LOAD_PRESENT")
		self.assertEqual(w["job_log_rows_read"], 2)

	def test_jobs_only_at_the_start_give_partial_load(self):
		w = self._assemble([self._mig(101.0), self._mig(102.0), self._mig(103.0)])
		self.assertEqual(w["migration_jobs_second_half"], 0)
		self.assertEqual(_load_state(w, progressed=3), "PARTIAL_LOAD")

	def test_an_empty_log_gives_no_load_and_records_that_it_read_nothing(self):
		w = self._assemble([])
		self.assertEqual(w["job_log_rows_read"], 0)
		self.assertEqual(_load_state(w, progressed=0), "NO_LOAD")

	def test_a_missing_log_is_named_in_the_witness(self):
		w = self._assemble([self._mig(110.0), self._mig(190.0)], extra_logs=("absent.jsonl",))
		self.assertEqual(len(w["job_logs_missing"]), 1)
		self.assertTrue(w["job_logs_missing"][0].endswith("absent.jsonl"))

	def test_rows_outside_the_window_are_not_counted(self):
		w = self._assemble([self._mig(50.0), self._mig(250.0)])
		self.assertEqual(w["migration_jobs_during_probe"], 0)
		self.assertEqual(_load_state(w, progressed=0), "NO_LOAD")

	def test_a_migration_job_on_an_erp_queue_is_witnessed(self):
		w = self._assemble([self._mig(110.0), self._mig(190.0, queue="long")])
		self.assertEqual(w["migration_jobs_on_erp_queues"], 1)

	def test_a_probe_ping_on_an_erp_queue_is_not_a_violation(self):
		rows = [
			self._mig(110.0),
			self._mig(190.0),
			{"at": 150.0, "queue": "site:short", "job_id": "cfs-probe-short-1-0"},
		]
		w = self._assemble(rows)
		self.assertEqual(w["migration_jobs_on_erp_queues"], 0)
		self.assertEqual(w["job_log_rows_read"], 3)


class TestContinuityByObjectProgress(unittest.TestCase):
	"""At release scale, job starts are too rare to sample continuity; progress is not.

	A 1000-object batch produces one job start per ~1000 objects, so a 40 s probe window during a
	100k migration logged 2 job starts, both in the same half — reported PARTIAL_LOAD for a window
	in which 783 objects demonstrably moved. Judging "was it loaded throughout" by job timestamps
	measures the batch size, not the load.
	"""

	def test_progress_in_both_halves_is_load_present_even_with_no_job_starts(self):
		w = dict(_witness(first=0, second=0), progress_samples=[0, 300, 600, 900])
		w["progressed_first_half"] = rehearsal._half_progress(w["progress_samples"], first=True)
		w["progressed_second_half"] = rehearsal._half_progress(w["progress_samples"], first=False)
		self.assertEqual(_load_state(w, progressed=900), "LOAD_PRESENT")

	def test_progress_only_at_the_start_is_still_partial(self):
		w = dict(_witness(first=0, second=0), progress_samples=[0, 780, 783, 783])
		w["progressed_first_half"] = rehearsal._half_progress(w["progress_samples"], first=True)
		w["progressed_second_half"] = rehearsal._half_progress(w["progress_samples"], first=False)
		self.assertEqual(w["progressed_second_half"], 0)
		self.assertEqual(_load_state(w, progressed=783), "PARTIAL_LOAD")

	def test_no_progress_anywhere_is_no_load(self):
		w = dict(_witness(first=0, second=0), progress_samples=[5, 5, 5, 5])
		w["progressed_first_half"] = rehearsal._half_progress(w["progress_samples"], first=True)
		w["progressed_second_half"] = rehearsal._half_progress(w["progress_samples"], first=False)
		self.assertEqual(_load_state(w, progressed=0), "NO_LOAD")

	def test_half_progress_is_none_when_no_campaign_was_named(self):
		self.assertIsNone(rehearsal._half_progress([None, None, None, None], first=True))
		self.assertIsNone(rehearsal._half_progress([], first=True))

	def test_job_starts_in_both_halves_still_count(self):
		"""Small-batch topologies keep working: either signal establishes continuity."""
		w = dict(_witness(first=3, second=3), progress_samples=[0, 0, 0, 0])
		w["progressed_first_half"] = 0
		w["progressed_second_half"] = 0
		self.assertEqual(_load_state(w, progressed=0), "LOAD_PRESENT")
