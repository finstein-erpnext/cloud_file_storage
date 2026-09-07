"""The F3 sampler's own regression suite: unmatched must never read as idle.

**The defect, twice.** `_rehearsal_pids` matches only processes whose command line names this
module. Work driven any other way is invisible to it — and every application field then reports
zero: peak RSS 0, read 0, write 0. That is indistinguishable from a beautifully behaved
application, and it is a measurement of nothing.

`docs/evidence/f3-disk-io-scale.md` records the first occurrence (the 4188-object run, whose
application metrics were excluded rather than reported as zeros). It recurred during the F3
re-measurement: two loaded runs moved **79 MB and 131 MB** while the report showed
`app write 0.0 B/s`, because the driver was an out-of-tree script.

The transferred-byte counters are what exposed it — 79 MB moved and 0 B/s written cannot both be
true. This suite makes the contradiction mechanical rather than dependent on someone noticing.
"""

import json
import os
import tempfile
import unittest

# `sample()` selects the processes it measures with `rehearsal._is_rehearsal_cmdline`, which is
# shared with `_work_horse_pids` and the throttle preflight. Its tests live in
# `test_f3_throttle_measurement.TestTheProcessMatcherIgnoresShells` — including the case that
# matters here, a shell wrapper whose own cmdline names this module. A change to that predicate
# silently changes what this file's subject measures, so the contract is cross-referenced rather
# than left invisible from this side.
from cloud_file_storage.tests.rehearsal import sample_report


def _rows(matched_pids, *, samples=4, rss=0, wchar=0, rchar=0):
	"""Sample lines shaped like the real sampler's output."""
	out = []
	for i in range(samples):
		per_proc = {
			pid: {"rss_bytes": rss, "wchar": wchar * (i + 1), "rchar": rchar * (i + 1), "cpu_ticks": 0}
			for pid in matched_pids
		}
		out.append(
			{
				"at": 1_700_000_000 + i * 5,
				"cpu_total_ticks": 1000 * (i + 1),
				"loadavg": 1.0,
				"rehearsal": per_proc,
				"mysqld": {"rss_bytes": 1, "wchar": 1, "rchar": 1, "cpu_ticks": 0},
				"matched_process_count": len(matched_pids),
				"matched_pids": list(matched_pids),
				"tables": {},
				"queue_depths": {"short": 0, "default": 0, "long": 0, "cloud_migration": 0},
				"redis_used_memory_bytes": 1,
				"redis_peak_memory_bytes": 1,
				"disk_free_bytes": 10**12,
			}
		)
	return out


def _write(rows):
	fd, path = tempfile.mkstemp(suffix=".jsonl")
	with os.fdopen(fd, "w") as handle:
		for row in rows:
			handle.write(json.dumps(row) + "\n")
	return path


class TestALoadedRunWithNoMatchedProcessIsInvalid(unittest.TestCase):
	"""The exact defect: real bytes moved, matcher saw nothing."""

	def setUp(self):
		# Zero matched processes, exactly as when the driver ran from outside this module.
		self.path = _write(_rows([]))
		self.addCleanup(os.remove, self.path)

	def test_it_is_rejected_rather_than_reported_as_zero(self):
		result = sample_report(self.path)

		self.assertEqual(
			result["status"],
			"INVALID_UNMATCHED_PROCESS",
			"a loaded run that matched no application process must invalidate, not report",
		)

	def test_the_application_fields_say_invalid_not_zero(self):
		"""`0` is the answer that caused the defect; the fields must refuse to say it."""
		result = sample_report(self.path)

		for field in ("app_peak_rss", "app_read_bytes", "app_write_bytes"):
			self.assertEqual(
				result[field],
				"INVALID_UNMATCHED_PROCESS",
				f"{field} reported a number for a window in which nothing was measured — that "
				"is the zero-by-absence reading this guard exists to prevent",
			)

	def test_the_reason_names_the_cause(self):
		result = sample_report(self.path)

		self.assertIn("matched no application process", result["reason"])


class TestTheOldBehaviourIsWhatWeReplaced(unittest.TestCase):
	"""Anti-vacuity: prove the pre-fix arithmetic produced a clean-looking zero."""

	def test_summing_an_empty_process_set_yields_zero(self):
		rows = _rows([])

		# This is precisely what the report used to do, and why it read as idle.
		app_write = sum(
			(metrics or {}).get("wchar", 0) or 0
			for row in rows
			for metrics in (row.get("rehearsal") or {}).values()
		)

		self.assertEqual(
			app_write,
			0,
			"if summing an empty match set did NOT yield 0, this defect could not have "
			"happened and this suite would be guarding nothing",
		)


class TestAMatchedRunIsAccepted(unittest.TestCase):
	def test_metrics_are_reported_when_processes_matched(self):
		path = _write(_rows(["4242", "4243"], rss=500_000_000, wchar=1_000_000, rchar=2_000_000))
		self.addCleanup(os.remove, path)

		result = sample_report(path)

		self.assertEqual(result["status"], "MEASURED")
		self.assertEqual(result["matched_process_count"], 2)
		self.assertEqual(sorted(result["matched_pids"]), ["4242", "4243"])
		self.assertGreater(result["app_peak_rss"], 0)
		self.assertGreater(result["app_write_bytes"], 0)


class TestTheIdleBaselineExceptionIsNarrow(unittest.TestCase):
	def test_an_idle_baseline_may_legitimately_match_nothing(self):
		"""Nothing is running, by design — that window is allowed to report zero matches."""
		path = _write(_rows([]))
		self.addCleanup(os.remove, path)

		result = sample_report(path, idle_baseline=True)

		self.assertEqual(result["status"], "IDLE_BASELINE")
		self.assertEqual(result["matched_process_count"], 0)

	def test_a_loaded_run_cannot_borrow_that_exception(self):
		"""The exception is opt-in per report. Without the flag, the same data is refused."""
		path = _write(_rows([]))
		self.addCleanup(os.remove, path)

		self.assertEqual(sample_report(path, idle_baseline=False)["status"], "INVALID_UNMATCHED_PROCESS")
		self.assertEqual(sample_report(path, idle_baseline=True)["status"], "IDLE_BASELINE")


if __name__ == "__main__":
	unittest.main()
