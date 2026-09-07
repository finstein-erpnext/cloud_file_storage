"""The F3 throttle measurement's own regression suite.

**Why this exists.** The harness computed its network rate as `bytes_done / elapsed`, and
`bytes_done` is *progress* accounting: a `DedupReused` object advanced, so it is counted at its
nominal size even though its content already existed in the bucket and nothing was sent.

That read correctly for as long as the measured batches happened to contain no duplicate
content. On the first corpus that did — `CFS-CAMP-0002-B3`, sixty objects: 35 uploaded, 25
dedup-reused — dividing the progress bytes reads **411,432.9 B/s against a 250,000 B/s ceiling**
and returns `passed: false`, on a throttle that was in fact holding at **99.8%**. The throttle
only ever saw the 35.

Every figure in this file is derived from the constants pinned below, from that one batch. An
earlier revision quoted 373,260 B/s here, which is not reachable from those numbers under any
elapsed consistent with them — the same defect the class below warns about.

A gate that fails a correct product is the same defect class as one that passes a broken one:
the number did not measure what its name claimed.

**Every test here drives `rehearsal.throttle_measurement_row`.** An earlier version of this file
defined its own `old_rate`/`new_rate` helpers and asserted against those. That made the suite
unfalsifiable with respect to the thing it names: deleting the harness's `NO_TRANSFER` branch
outright left all of these green, because none of them ever called it. A test that restates its
subject's arithmetic tests the restatement.
"""

import ast
import inspect
import json
import os
import sys
import unittest

from cloud_file_storage.migration import engine
from cloud_file_storage.migration.engine import ThrottleBudget
from cloud_file_storage.tests import rehearsal

CEILING_MBPS = 2
CEILING_BPS = CEILING_MBPS * 1_000_000 / 8  # 250,000


def row(objects_done=60, bytes_done=0, objects_failed=0):
	return {"objects_done": objects_done, "bytes_done": bytes_done, "objects_failed": objects_failed}


def result(transferred, calls=0, slept=0.0):
	return {
		"transferred_bytes": transferred,
		"consume_calls": calls,
		"throttle_slept_seconds": slept,
	}


class TestTheDedupCorpusThatExposedIt(unittest.TestCase):
	"""The exact numbers from the run that surfaced this, kept as the regression."""

	# One batch, one run — CFS-CAMP-0002-B3, instrumented. An earlier version of this class
	# paired the transferred bytes of one batch with the elapsed time of another and produced a
	# plausible-looking 0.889; the numbers must come from the same measurement or they are not
	# evidence of anything.
	TRANSFERRED = 4_538_804  # the 35 uploaded objects, from ThrottleBudget.consumed
	PROGRESS = 7_488_078  # bytes_done: all 60, including the 25 dedup-reused
	ELAPSED = 18.20

	def measure(self):
		return rehearsal.throttle_measurement_row(
			batch="CFS-CAMP-0002-B3",
			limit=CEILING_MBPS,
			elapsed=self.ELAPSED,
			row=row(bytes_done=self.PROGRESS, objects_done=60),
			result=result(self.TRANSFERRED, calls=35, slept=17.5),
		)

	def test_the_progress_bytes_would_have_falsely_exceeded_the_ceiling(self):
		"""Had the harness divided `bytes_progress_accounted`, it would have read 411,432.9 B/s."""
		measured = self.measure()

		false_rate = measured["bytes_progress_accounted"] / self.ELAPSED

		self.assertGreater(false_rate, CEILING_BPS, "the bug: progress bytes exceed the ceiling")
		self.assertAlmostEqual(false_rate, 411_432.9, delta=50.0)

	def test_the_harness_lands_on_the_ceiling(self):
		measured = self.measure()

		self.assertEqual(measured["status"], "MEASURED")
		self.assertLessEqual(measured["achieved_bytes_per_second"], CEILING_BPS)
		self.assertAlmostEqual(measured["achieved_bytes_per_second"], 249_384.8, delta=50.0)
		self.assertAlmostEqual(measured["percent_of_ceiling"], 99.75, delta=0.1)

	def test_the_dedup_reused_bytes_are_exactly_the_difference(self):
		measured = self.measure()

		self.assertEqual(measured["bytes_dedup_reused"], self.PROGRESS - self.TRANSFERRED)
		self.assertEqual(measured["uploaded_objects"], 35)
		self.assertEqual(measured["dedup_reused_objects"], 25)


class TestRemovingTheExclusionFailsByName(unittest.TestCase):
	"""If dedup bytes are ever counted as transferred again, this is the test that says so."""

	def test_counting_dedup_bytes_as_transferred_breaks_the_verdict(self):
		elapsed = 18.20
		transferred_correct, dedup = 4_538_804, 2_949_274

		correct = rehearsal.throttle_measurement_row(
			batch="b",
			limit=CEILING_MBPS,
			elapsed=elapsed,
			row=row(bytes_done=transferred_correct + dedup),
			result=result(transferred_correct, calls=35, slept=17.5),
		)
		mutated = rehearsal.throttle_measurement_row(  # the exclusion removed
			batch="b",
			limit=CEILING_MBPS,
			elapsed=elapsed,
			row=row(bytes_done=transferred_correct + dedup),
			result=result(transferred_correct + dedup, calls=35, slept=17.5),
		)

		self.assertTrue(rehearsal.throttle_verdict([correct, self._free()])["passed"])
		self.assertFalse(rehearsal.throttle_verdict([mutated, self._free()])["passed"])

	def _free(self):
		return rehearsal.throttle_measurement_row(
			batch="free",
			limit=0,
			elapsed=0.5,
			row=row(bytes_done=4_538_804),
			result=result(4_538_804, calls=35),
		)


class TestTheDegenerateCorporaAreHandled(unittest.TestCase):
	def test_an_all_dedup_corpus_is_reported_as_no_transfer_not_as_zero(self):
		"""0 B/s compares favourably with every ceiling. It must never be a rate."""
		measured = rehearsal.throttle_measurement_row(
			batch="b",
			limit=CEILING_MBPS,
			elapsed=12.5,
			row=row(bytes_done=3_000_000),
			result=result(0, calls=0),
		)

		self.assertEqual(measured["status"], "NO_TRANSFER")
		self.assertIsNone(measured["achieved_bytes_per_second"])
		self.assertIsNone(measured["percent_of_ceiling"])

	def test_zero_elapsed_does_not_divide_by_zero(self):
		measured = rehearsal.throttle_measurement_row(
			batch="b",
			limit=CEILING_MBPS,
			elapsed=0,
			row=row(bytes_done=1_000_000),
			result=result(1_000_000, calls=8),
		)

		self.assertEqual(measured["status"], "NOT_MEASURABLE_ZERO_ELAPSED")
		self.assertIsNone(measured["achieved_bytes_per_second"])

	def test_an_all_upload_corpus_keeps_the_original_behaviour(self):
		"""With no dedupe, transferred == progress and the fix changes nothing."""
		measured = rehearsal.throttle_measurement_row(
			batch="b",
			limit=CEILING_MBPS,
			elapsed=18.0,
			row=row(bytes_done=4_487_254),
			result=result(4_487_254, calls=60, slept=17.4),
		)

		self.assertEqual(measured["bytes_dedup_reused"], 0)
		self.assertEqual(measured["dedup_reused_objects"], 0)
		self.assertAlmostEqual(measured["achieved_bytes_per_second"], 249_291.9, delta=50.0)


class TestFailedObjectsAreActuallyRead(unittest.TestCase):
	"""M3. `objects_failed` was missing from the `get_value` field list for a whole campaign.

	`frappe._dict` returns None for an unselected column rather than raising, so
	`cint(row.objects_failed or 0)` produced a confident `"failed_objects": 0` that had never
	consulted a failure count. The evidence it generated was vacuous, not wrong-but-close.
	"""

	def test_a_failed_object_is_reported_not_swallowed(self):
		measured = rehearsal.throttle_measurement_row(
			batch="b",
			limit=CEILING_MBPS,
			elapsed=10.0,
			row=row(objects_done=60, bytes_done=1_000_000, objects_failed=7),
			result=result(1_000_000, calls=53, slept=1.0),
		)

		self.assertEqual(measured["failed_objects"], 7)
		# 60 advanced minus 7 failed = 53 real, all of which uploaded: no phantom dedupe.
		self.assertEqual(measured["dedup_reused_objects"], 0)

	def test_the_harness_selects_the_column_it_reports(self):
		"""The structural half: reading the field is useless if the query never fetched it.

		Parsed, not grepped — a substring check is satisfied by a comment mentioning the column,
		which is precisely the kind of evidence-free green this suite exists to prevent.
		"""
		tree = ast.parse(inspect.getsource(rehearsal.throttle))

		fields = set()
		for node in ast.walk(tree):
			if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
				continue
			if node.func.attr != "get_value":
				continue
			for arg in node.args:
				if isinstance(arg, ast.List):
					fields |= {el.value for el in arg.elts if isinstance(el, ast.Constant)}

		self.assertIn(
			"objects_failed",
			fields,
			"throttle()'s get_value field list must include objects_failed — an unselected "
			"column reads as None and reports 0 failures having consulted nothing",
		)


class TestTheRowSurvivesAnImperfectBatch(unittest.TestCase):
	"""Degenerate counter combinations must not produce a row that reads as nonsense."""

	def test_a_failure_after_consume_does_not_yield_negative_dedup_bytes(self):
		"""An object that fails between `consume` and its CAS adds to `consumed`, not to
		`bytes_done` — so the naive difference goes negative and reports "dedup reused: -N"."""
		measured = rehearsal.throttle_measurement_row(
			batch="b",
			limit=CEILING_MBPS,
			elapsed=10.0,
			row=row(objects_done=10, bytes_done=900_000, objects_failed=1),
			result=result(1_000_000, calls=10, slept=4.0),
		)

		self.assertGreaterEqual(measured["bytes_dedup_reused"], 0)
		self.assertEqual(measured["bytes_dedup_reused"], 0)

	def test_a_missing_batch_row_raises_rather_than_reading_as_zeroes(self):
		"""`get_value(..., as_dict=True)` returns None for a batch that does not exist.

		Reading that as a row of zeroes would emit `objects: 0, failed_objects: 0` beside a
		positive `bytes_transferred` — a measurement describing a batch nobody read.
		"""
		with self.assertRaises(ValueError):
			rehearsal.throttle_measurement_row(
				batch="does-not-exist",
				limit=CEILING_MBPS,
				elapsed=10.0,
				row=None,
				result=result(1_000_000, calls=10, slept=4.0),
			)


class TestThePreflightFiltersOnStatusesThatExist(unittest.TestCase):
	"""The preflight's in-flight set must be drawn from the DocType, not from memory.

	An earlier revision filtered campaigns on `["Running", "Dispatched"]`. `Dispatched` is not a
	Cloud Migration Campaign status at all, so that half of the filter matched nothing — and the
	half that was real missed `Analyzing`, `Stopping` and `Cleanup Running`. `Analyzing` is the
	one that matters: a second process writing Cloud Migration Object rows leaves no batch and
	every object still Pending, so every sibling check passes and a competing writer is waved
	through the gate that exists to catch it.
	"""

	def _options(self):
		path = os.path.join(
			os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
			"cloud_file_storage",
			"doctype",
			"cloud_migration_campaign",
			"cloud_migration_campaign.json",
		)
		with open(path) as handle:
			meta = json.load(handle)
		field = next(f for f in meta["fields"] if f["fieldname"] == "status")
		return [opt for opt in field["options"].split("\n") if opt.strip()]

	def test_every_status_the_preflight_filters_on_really_exists(self):
		options = self._options()

		unknown = [s for s in rehearsal.CAMPAIGN_IN_FLIGHT_STATUSES if s not in options]

		self.assertEqual(
			unknown,
			[],
			f"the preflight filters on {unknown}, which the DocType does not define — those "
			"clauses match nothing and the check silently weakens",
		)

	def test_it_covers_what_the_engine_itself_treats_as_active(self):
		"""`engine.dispatch_tick` picks up `Running` and `Cleanup Running`; both must count."""
		for status in ("Running", "Cleanup Running", "Analyzing"):
			self.assertIn(status, rehearsal.CAMPAIGN_IN_FLIGHT_STATUSES, status)

	def test_the_quiescent_starting_state_is_not_treated_as_active(self):
		"""`Planned` with every batch Pending IS the intended starting state, not a residue."""
		for status in ("Draft", "Analyzed", "Planned", "Completed"):
			self.assertNotIn(status, rehearsal.CAMPAIGN_IN_FLIGHT_STATUSES, status)


class TestTheProcessMatcherIgnoresShells(unittest.TestCase):
	"""A shell that names the module is not a process running it.

	The preflight's WORKERS_HEALTHY check broadened from "any rehearsal worker" to "any rehearsal
	process", which immediately began matching the `bash -c '... -m
	cloud_file_storage.tests.rehearsal ...'` wrapper that launched it — and refused a site with
	nothing running on it. Same false positive as `pkill -f` matching the shell that issued it.
	"""

	NUL = "\x00"

	def test_a_real_worker_matches(self):
		cmd = self.NUL.join(
			["/home/user/v15/env/bin/python", "-m", "cloud_file_storage.tests.rehearsal", "worker"]
		)

		self.assertTrue(rehearsal._is_rehearsal_cmdline(cmd))

	def test_a_worker_started_by_path_matches(self):
		"""Matching only the dotted form drops these from every sample as zero RSS."""
		cmd = self.NUL.join(
			["python3", "/home/user/v15/apps/cloud_file_storage/cloud_file_storage/tests/rehearsal.py"]
		)

		self.assertTrue(rehearsal._is_rehearsal_cmdline(cmd))

	def test_the_shell_that_launched_it_does_not_match(self):
		cmd = self.NUL.join(
			[
				"/bin/bash",
				"-c",
				"PYTHONPATH=/x python -m cloud_file_storage.tests.rehearsal throttle --site s",
			]
		)

		self.assertFalse(
			rehearsal._is_rehearsal_cmdline(cmd),
			"a shell carrying the command in its own cmdline is not a rehearsal process",
		)

	def test_this_benchs_interpreter_really_is_named_python(self):
		"""The predicate narrows in the UNSAFE direction, unlike the false positive it fixed.

		A missed process means `WORKERS_HEALTHY=YES` on a contaminated site and an under-measuring
		sampler. If this bench ever runs under a renamed interpreter or a shim, that must fail
		loudly here rather than leave the matcher quietly blind at three call sites.
		"""
		self.assertIn("python", os.path.basename(sys.executable))

	def test_an_unrelated_python_process_does_not_match(self):
		cmd = self.NUL.join(["/usr/bin/python3", "-m", "http.server"])

		self.assertFalse(rehearsal._is_rehearsal_cmdline(cmd))


class TestTheStartingStateCheckExaminesSomething(unittest.TestCase):
	"""A precondition must confirm what it needs is present, not only that nothing is wrong.

	`set(by_status) <= {"Pending", "Skipped"}` is satisfied by the empty set, so a site with no
	migration objects at all — nothing seeded, or a campaign just purged — returned a positive
	verdict on a state it had never examined.
	"""

	def test_the_intended_starting_state_passes(self):
		self.assertTrue(rehearsal._starting_state_valid(0, {"Pending": 240, "Skipped": 20}, 1, 3))

	def test_a_site_with_no_objects_at_all_is_not_valid(self):
		self.assertFalse(
			rehearsal._starting_state_valid(0, {}, 1, 3),
			"an empty status map satisfies the subset test while examining nothing",
		)

	def test_a_site_with_only_skipped_objects_is_not_valid(self):
		"""Nothing to upload means nothing to measure — the benchmark would report NO_TRANSFER."""
		self.assertFalse(rehearsal._starting_state_valid(0, {"Skipped": 20}, 1, 3))

	def test_partial_progress_is_not_valid(self):
		self.assertFalse(rehearsal._starting_state_valid(0, {"Pending": 40, "Uploaded": 200}, 1, 3))

	def test_residual_cloud_storage_objects_are_not_valid(self):
		self.assertFalse(rehearsal._starting_state_valid(200, {"Pending": 240}, 1, 3))

	def test_fewer_than_two_pending_batches_is_not_valid(self):
		"""`throttle()` compares two batches and refuses without them.

		A precondition that stops at "some Pending object exists" still passes a site the stage
		will reject — so it checks the quantity the stage actually consumes.
		"""
		self.assertFalse(rehearsal._starting_state_valid(0, {"Pending": 240}, 1, 1))
		self.assertFalse(rehearsal._starting_state_valid(0, {"Pending": 240}, 1, 0))
		self.assertTrue(rehearsal._starting_state_valid(0, {"Pending": 240}, 1, 2))

	def test_more_than_one_campaign_is_not_valid(self):
		self.assertFalse(rehearsal._starting_state_valid(0, {"Pending": 240}, 2, 3))


class TestTheCommittedEvidenceStillSatisfiesTheShippedGate(unittest.TestCase):
	"""The committed artifacts must still be what today's preflight would produce.

	This exists to retire a rule rather than to enforce one. The standing principle has been that
	committed evidence must come from the code that gates it, which in practice meant regenerating
	the artifacts after *every* preflight change — six times, with the marginal content of each
	change shrinking. A bright-line rule with no terminating condition is not more rigorous than a
	scoped one; it just moves the judgement to whether you are still willing to pay.

	So the suite answers it instead. A change to `_starting_state_valid` that would have refused
	the state the shipped artifact records turns this red, and the artifact must be regenerated. A
	change that would not is proven harmless here, and it stands. Same move as everywhere else in
	this work: replace a rule someone must remember to apply with a check that fires by itself.
	"""

	def _artifact(self, name):
		path = os.path.join(
			os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
			"docs",
			"evidence",
			name,
		)
		with open(path) as handle:
			return json.load(handle)

	def test_the_shipped_preflight_state_still_passes_todays_predicate(self):
		art = self._artifact("f3-throttle-preflight.json")
		notes = art["notes"]

		self.assertEqual(art["PREFLIGHT"], "PASS", "the committed gating artifact must be a PASS")
		self.assertTrue(
			rehearsal._starting_state_valid(
				notes["residual_cso"],
				notes["object_status"],
				notes["campaign_count"],
				notes["batches_pending"],
			),
			"the shipped measurement's own preflight state no longer satisfies the current "
			"predicate — the artifact must be regenerated from the changed gate",
		)

	def test_the_refusal_control_is_still_a_refusal(self):
		"""A gate never seen to refuse is not known to be a gate; that stays true as it changes."""
		art = self._artifact("f3-throttle-preflight-refusal.json")
		notes = art["notes"]

		self.assertEqual(art["PREFLIGHT"], "FAIL")
		self.assertFalse(
			rehearsal._starting_state_valid(
				notes["residual_cso"],
				notes["object_status"],
				notes["campaign_count"],
				notes["batches_pending"],
			),
			"the committed refusal control would now be accepted — it no longer evidences that "
			"the gate can say no",
		)


class TestTheCounterItselfIsAuthoritative(unittest.TestCase):
	"""`ThrottleBudget.consumed` must tally bytes whether or not a limit is configured."""

	def test_it_counts_when_limiting_is_off(self):
		"""The unlimited batch needs a transferred figure too, or it cannot be compared.

		An early return on `not enabled` left `consumed` at zero for the free-running batch,
		which would report NO_TRANSFER for a batch that transferred its whole corpus.
		"""
		budget = ThrottleBudget(0, 1)

		self.assertFalse(budget.enabled)
		budget.consume(1000)
		budget.consume(2500)

		self.assertEqual(budget.consumed, 3500, "an unlimited batch must still tally its bytes")
		self.assertEqual(budget.calls, 2)
		self.assertEqual(budget.slept, 0.0, "an unlimited budget never sleeps")

	def test_it_counts_and_limits_when_limiting_is_on(self):
		budget = ThrottleBudget(CEILING_MBPS, 1)

		self.assertTrue(budget.enabled)
		self.assertEqual(budget.bytes_per_second, CEILING_BPS)
		budget.consume(1000)

		self.assertEqual(budget.consumed, 1000)

	def test_a_limited_budget_records_the_time_it_actually_slept(self):
		"""H3's witness. Overshoot the ceiling hard enough that a sleep is unavoidable."""
		budget = ThrottleBudget(CEILING_MBPS, 1)

		budget.consume(int(CEILING_BPS // 2))  # ~0.5s of budget, consumed instantly

		self.assertGreater(budget.slept, 0.0, "the limiter must record its own back-pressure")


class TestConsumeIsBesideThePut(unittest.TestCase):
	"""M4. The tally must sit inside `if objects.needs_upload(cso):`, beside the real PUT.

	Structural rather than behavioural on purpose: the failure mode is a call drifting *out* of
	the upload branch during an unrelated edit, which restores the dedup-inflation bug at its
	source. Byte-level tests downstream cannot see where the call lives.
	"""

	def test_the_consume_call_is_nested_under_needs_upload(self):
		tree = ast.parse(inspect.getsource(engine.process_object_upload))

		guarded = []
		for node in ast.walk(tree):
			if not isinstance(node, ast.If):
				continue
			# The test must be a bare `objects.needs_upload(...)` call — not `not
			# needs_upload(...)`, and not a compound that merely mentions it. Matching on the
			# dumped string accepted the inversion, which is the opposite of the invariant.
			test = node.test
			if not (
				isinstance(test, ast.Call)
				and isinstance(test.func, ast.Attribute)
				and test.func.attr == "needs_upload"
			):
				continue
			# `node.body` only. Walking the whole `If` includes `node.orelse`, so a `consume`
			# in the else branch — bytes counted exactly when they did NOT cross the network —
			# satisfied the old form of this test.
			for stmt in node.body:
				for inner in ast.walk(stmt):
					if (
						isinstance(inner, ast.Call)
						and isinstance(inner.func, ast.Attribute)
						and inner.func.attr == "consume"
					):
						guarded.append(inner)

		all_calls = [
			n
			for n in ast.walk(tree)
			if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "consume"
		]

		self.assertEqual(len(all_calls), 1, "exactly one consume call belongs in this function")
		self.assertEqual(
			len(guarded),
			1,
			"throttle.consume must be inside `if objects.needs_upload(cso):` — a dedup-reused "
			"object must contribute zero transferred bytes by construction",
		)


if __name__ == "__main__":
	unittest.main()
