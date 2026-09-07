"""Acceptance gates for a loaded F3 measurement: three states, never two.

Both false readings in this cycle came from collapsing distinct states into one number:

* the **campaign-concurrency rejection** — a second campaign started while another was
  `Running`. The product correctly refused it (`only one campaign may run at a time`), the
  wrapper sent stderr to `/dev/null`, and the outcome presented as `wall 0s` with an empty
  artifact. That is `RUN_FAILED`: the workload never ran.
* the **out-of-tree driver** — real work happened (79 MB, then 131 MB transferred) but the
  sampler matched no application process, so peak RSS and every app I/O figure read `0`. That
  is `INVALID_MEASUREMENT`: measured nothing, looked like measured-and-idle.

Neither is a product defect, and neither may contribute a number to a scaling verdict.
"""

import unittest

from cloud_file_storage.tests.rehearsal import (
	INVALID_MEASUREMENT,
	RUN_FAILED,
	VALID_MEASUREMENT,
	classify_loaded_run,
)

GOOD = dict(
	run_stage_rc=0,
	result_parsed=True,
	matched_process_count=5,
	bytes_transferred=130_579_165,
	expected_objects=1020,
	verified=1020,
	pending=0,
	failed=0,
)


class TestTheWorkloadNeverRan(unittest.TestCase):
	def test_the_concurrency_rejection_is_run_failed_not_a_measurement(self):
		"""The exact failure: ValidationError swallowed, empty artifact, wall 0s."""
		state, reasons = classify_loaded_run(**{**GOOD, "run_stage_rc": 1, "result_parsed": False})

		self.assertEqual(state, RUN_FAILED)
		self.assertTrue(any("exited 1" in r for r in reasons))
		self.assertTrue(any("artifact" in r for r in reasons))

	def test_rc_zero_with_an_empty_artifact_is_still_run_failed(self):
		"""A wrapper that hides stderr can return rc=0 over a failure it never saw."""
		state, reasons = classify_loaded_run(**{**GOOD, "run_stage_rc": 0, "result_parsed": False})

		self.assertEqual(state, RUN_FAILED)

	def test_a_nonzero_rc_is_run_failed_even_with_plausible_sampler_metrics(self):
		"""Sampler numbers look real because the sampler ran. The workload did not."""
		state, _ = classify_loaded_run(
			**{**GOOD, "run_stage_rc": 2, "matched_process_count": 3, "bytes_transferred": 0}
		)

		self.assertEqual(
			state,
			RUN_FAILED,
			"a failed run must not be reclassified as a merely-invalid measurement; the "
			"distinction is whether the workload happened at all",
		)


class TestTheEvidenceIsUnusable(unittest.TestCase):
	def test_zero_matched_processes_on_a_loaded_run_is_invalid(self):
		"""The out-of-tree driver: real transfer, zero matched, all app metrics zero."""
		state, reasons = classify_loaded_run(**{**GOOD, "matched_process_count": 0})

		self.assertEqual(state, INVALID_MEASUREMENT)
		self.assertTrue(any("zero-by-absence" in r for r in reasons))

	def test_matched_processes_but_no_transfer_is_invalid(self):
		"""The first A2 attempt: 3 matched, 147 MB RSS, 0 bytes, 606 still Pending."""
		state, reasons = classify_loaded_run(
			**{**GOOD, "matched_process_count": 3, "bytes_transferred": 0, "verified": 0, "pending": 606}
		)

		self.assertEqual(state, INVALID_MEASUREMENT)
		self.assertTrue(any("no bytes were transferred" in r for r in reasons))

	def test_transfer_but_unconverged_corpus_is_invalid(self):
		state, reasons = classify_loaded_run(**{**GOOD, "verified": 900, "pending": 120})

		self.assertEqual(state, INVALID_MEASUREMENT)
		self.assertTrue(any("did not converge" in r for r in reasons))

	def test_unexplained_failures_are_invalid(self):
		state, reasons = classify_loaded_run(**{**GOOD, "failed": 3})

		self.assertEqual(state, INVALID_MEASUREMENT)
		self.assertTrue(any("failed object" in r for r in reasons))

	def test_permitted_failures_are_allowed_when_explicitly_declared(self):
		state, _ = classify_loaded_run(**{**GOOD, "failed": 3, "allow_failed": 3})

		self.assertEqual(state, VALID_MEASUREMENT)


class TestAValidRunIsAccepted(unittest.TestCase):
	def test_the_real_point_b_numbers_are_accepted(self):
		"""Point B as actually measured: 1020/1020 Verified, 5 matched, 130.5 MB."""
		state, reasons = classify_loaded_run(**GOOD)

		self.assertEqual(state, VALID_MEASUREMENT)
		self.assertEqual(reasons, [])

	def test_the_three_states_are_distinct(self):
		"""Anti-vacuity: if any two collapsed, the classifier would prove nothing."""
		self.assertEqual(
			len({VALID_MEASUREMENT, INVALID_MEASUREMENT, RUN_FAILED}),
			3,
			"the three states must be distinct values; collapsing them is the defect",
		)


if __name__ == "__main__":
	unittest.main()
