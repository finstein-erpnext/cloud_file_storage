"""The throttle criterion's verdict is emitted by the harness, and zero bytes can never pass.

The owner's instruction is explicit: a loaded run with `bytes_transferred == 0` must remain
`NO_TRANSFER` / `INVALID_MEASUREMENT` and never PASS, and the harness — not manual arithmetic
over a JSON blob — must emit `THROTTLE_CRITERION`. A rate of 0 B/s compares favourably with
every ceiling there is, so an all-dedup or credential-starved corpus would otherwise read as the
most compliant throttle ever measured.

These call `rehearsal.throttle_verdict` itself rather than a copy of its rule — a test that
recomputes the logic it checks passes whatever that logic becomes. The measurement rows are the
decision's *input*; their provenance is covered by `test_f3_throttle_measurement.py` and by the
engine's `consume` accounting.
"""

import unittest

from cloud_file_storage.tests import rehearsal


def _verdict(throttled: dict, free: dict) -> dict:
	"""Drive the real decision — `rehearsal.throttle_verdict` — not a copy of it."""
	return rehearsal.throttle_verdict([throttled, free])


def _row(limit, rate, status="MEASURED", slept=9.0, seconds=10.0):
	# `slept` defaults to a dominant share of `seconds` because every case in this file other
	# than the engagement tests is about the *rate* rule, and a row that fails the engagement
	# floor is INVALID by construction — it would mask the rule under test.
	return {
		"limit_mbps": limit,
		"limit_bytes_per_second": (limit * 1_000_000 / 8) if limit else None,
		"achieved_bytes_per_second": rate,
		"status": status,
		"throttle_slept_seconds": slept,
		"seconds": seconds,
	}


class TestZeroBytesNeverPasses(unittest.TestCase):
	def test_no_transfer_on_the_throttled_batch_is_invalid_not_pass(self):
		v = _verdict(_row(2, None, "NO_TRANSFER"), _row(0, 5_000_000.0))
		self.assertEqual(v["THROTTLE_CRITERION"], "FAIL")
		self.assertEqual(v["measurement_state"], "INVALID_MEASUREMENT")
		self.assertFalse(v["passed"])

	def test_no_transfer_on_the_free_batch_is_invalid_not_pass(self):
		v = _verdict(_row(2, 240_000.0), _row(0, None, "NO_TRANSFER"))
		self.assertEqual(v["THROTTLE_CRITERION"], "FAIL")
		self.assertEqual(v["measurement_state"], "INVALID_MEASUREMENT")

	def test_both_no_transfer_is_invalid_not_pass(self):
		v = _verdict(_row(2, None, "NO_TRANSFER"), _row(0, None, "NO_TRANSFER"))
		self.assertEqual(v["THROTTLE_CRITERION"], "FAIL")
		self.assertEqual(v["measurement_state"], "INVALID_MEASUREMENT")

	def test_zero_elapsed_is_invalid_not_pass(self):
		v = _verdict(_row(2, None, "NOT_MEASURABLE_ZERO_ELAPSED"), _row(0, 5_000_000.0))
		self.assertEqual(v["THROTTLE_CRITERION"], "FAIL")


class TestTheVerdictTracksTheRealConditions(unittest.TestCase):
	def test_within_ceiling_and_slower_than_free_passes(self):
		v = _verdict(_row(2, 249_000.0), _row(0, 2_100_000.0))
		self.assertEqual(v["THROTTLE_CRITERION"], "PASS")
		self.assertEqual(v["measurement_state"], "VALID_MEASUREMENT")
		self.assertIn("utilisation_of_ceiling", v)
		self.assertIn("unthrottled_speedup", v)

	def test_over_the_ceiling_fails(self):
		v = _verdict(_row(2, 400_000.0), _row(0, 2_100_000.0))
		self.assertEqual(v["THROTTLE_CRITERION"], "FAIL")

	def test_unthrottled_not_faster_fails(self):
		"""If the limit changes nothing, the limit was not demonstrably enforced."""
		v = _verdict(_row(2, 240_000.0), _row(0, 239_000.0))
		self.assertEqual(v["THROTTLE_CRITERION"], "FAIL")

	def test_the_five_percent_tolerance_is_inclusive_at_the_bound(self):
		v = _verdict(_row(2, 250_000.0 * 1.05), _row(0, 2_000_000.0))
		self.assertEqual(v["THROTTLE_CRITERION"], "PASS")

	def test_the_five_percent_tolerance_is_pinned_from_above(self):
		"""M2. Bounded on both sides, or the constant is free to drift upward unnoticed.

		The slack absorbs the last partial sleep of a token bucket. Pinned only from below, it
		could be widened to 1.5 and every test would still pass — which is how a tolerance
		becomes a negotiating margin.
		"""
		just_over = _verdict(_row(2, 250_000.0 * 1.0501), _row(0, 2_000_000.0))

		self.assertEqual(just_over["THROTTLE_CRITERION"], "FAIL")
		self.assertEqual(rehearsal.THROTTLE_CEILING_SLACK, 1.05)


class TestTheLimiterMustHaveActuallyEngaged(unittest.TestCase):
	"""H3. Under the ceiling is not the same as held by the ceiling.

	A small or overhead-bound corpus finishes far under any limit with `time.sleep` never called.
	The rate then evidences the corpus, not the throttle, and the criterion would be satisfied by
	a limiter that did nothing at all.
	"""

	def test_a_run_where_the_limiter_never_slept_is_invalid_not_pass(self):
		v = _verdict(_row(2, 12_000.0, slept=0.0), _row(0, 2_000_000.0))

		self.assertEqual(v["THROTTLE_CRITERION"], "FAIL")
		self.assertEqual(v["measurement_state"], "INVALID_MEASUREMENT")
		self.assertFalse(v["throttle_engaged"])
		self.assertIn("not what bounded this run", v["reason"])

	def test_one_early_overshoot_does_not_buy_engagement(self):
		"""The `slept > 0` hole: one large object first, then a long overhead-bound crawl.

		A single 1 MB object overshoots the bucket and sleeps ~0.3s; 99 tiny objects then take
		two minutes on per-object overhead. The limiter slept, the rate is 3% of the ceiling,
		and the run evidences the corpus rather than the limit.
		"""
		v = _verdict(_row(2, 8_400.0, slept=0.3, seconds=120.0), _row(0, 2_000_000.0))

		self.assertEqual(v["THROTTLE_CRITERION"], "FAIL")
		self.assertEqual(v["measurement_state"], "INVALID_MEASUREMENT")
		self.assertFalse(v["throttle_engaged"])
		self.assertAlmostEqual(v["sleep_fraction"], 0.0025, places=4)

	def test_the_engagement_floor_is_pinned_from_both_sides(self):
		"""Or the floor could be relaxed to nothing and every test here would still pass."""
		self.assertEqual(rehearsal.THROTTLE_MIN_SLEEP_FRACTION, 0.25)

		just_under = _verdict(_row(2, 249_000.0, slept=2.49, seconds=10.0), _row(0, 2_000_000.0))
		just_over = _verdict(_row(2, 249_000.0, slept=2.51, seconds=10.0), _row(0, 2_000_000.0))

		self.assertEqual(just_under["THROTTLE_CRITERION"], "FAIL")
		self.assertEqual(just_over["THROTTLE_CRITERION"], "PASS")

	def test_a_missing_sleep_witness_is_treated_as_no_engagement(self):
		"""An older harness emitted no such field. Absent evidence is not evidence."""
		row = _row(2, 12_000.0)
		del row["throttle_slept_seconds"]

		v = _verdict(row, _row(0, 2_000_000.0))

		self.assertEqual(v["THROTTLE_CRITERION"], "FAIL")
		self.assertFalse(v["throttle_engaged"])

	def test_engagement_alone_does_not_rescue_an_over_ceiling_run(self):
		v = _verdict(_row(2, 400_000.0, slept=9.9), _row(0, 2_000_000.0))

		self.assertEqual(v["THROTTLE_CRITERION"], "FAIL")
		self.assertTrue(v["throttle_engaged"])

	def test_a_genuinely_held_run_reports_engaged_and_passes(self):
		"""Shaped on the shipped measurement: 113.23s asleep out of 117.92s elapsed."""
		v = _verdict(_row(2, 249_937.5, slept=113.23, seconds=117.92), _row(0, 7_822_759.9))

		self.assertEqual(v["THROTTLE_CRITERION"], "PASS")
		self.assertEqual(v["measurement_state"], "VALID_MEASUREMENT")
		self.assertTrue(v["throttle_engaged"])


class TestConsumeCountsTransfersNotAttempts(unittest.TestCase):
	"""`consume_calls` is what separates uploaded objects from dedupe-converged ones."""

	def test_consume_increments_once_per_call_and_tallies_bytes(self):
		b = rehearsal_throttle_budget(mbps=0)
		b.consume(1000)
		b.consume(2500)
		self.assertEqual(b.calls, 2)
		self.assertEqual(b.consumed, 3500)

	def test_disabled_budget_still_counts(self):
		"""Counting is unconditional; only the sleeping is gated on a limit being set."""
		b = rehearsal_throttle_budget(mbps=0)
		self.assertFalse(b.enabled)
		b.consume(4096)
		self.assertEqual((b.calls, b.consumed), (1, 4096))


def rehearsal_throttle_budget(*, mbps):
	from cloud_file_storage.migration.engine import ThrottleBudget

	return ThrottleBudget(mbps, 1)
