# F3 / A1 — ERP queue latency under migration load

`ACCEPTANCE_GATES.md:28` requires "normal short/default/long ERP queues unaffected (probe-job
latency at baseline)". The gate stated no tolerance, so four consecutive release audits recorded
F3 as `BLOCKED(OWNER_DECISION_REQUIRED)` rather than negotiate one. The owner ruled the tolerance
on 2026-08-18.

## The frozen threshold

Per queue, at the same SHA / site / topology / probe implementation:

* **>= 30** completed probe jobs on each side (`A1_MIN_SAMPLES = 30`)
* **p95**, nearest-rank
* PASS requires **both** `loaded_p95 - baseline_p95 <= 250 ms` (`A1_MAX_DELTA_SECONDS`)
  **and** `loaded_p95 / baseline_p95 <= 2.0` (`A1_MAX_RATIO`)
* zero probe failures/timeouts, no sustained queue-depth accumulation, zero migration jobs on
  `short`/`default`/`long`, depths recovering to baseline
* **one queue failing any condition fails A1**

The constants live in `cloud_file_storage/tests/rehearsal.py` and are pinned by
`test_f3_a1_witness.py::TestTheFrozenConstantsArePinned`. They were frozen before the
measurement and **were never adjusted**.

## Measurement of record — release scale

Site `cfs-throttle.local`, campaign **`CFS-CAMP-0008`**, **100,020 objects** in 100 batches
(batch_size 1000, parallelism 2), 156,322,820 bytes, bucket `cfs-a1scale` on local MinIO.
Topology: **2** `cloud_migration` workers, **1** accounting ERP worker per queue. Probe: 35 jobs
per queue per side, `frappe.handler.ping`, 5 ms completion poll.

| queue | baseline p95 | loaded p95 | delta | ratio | pass |
|---|---|---|---|---|---|
| short | 0.3311s | 0.4144s | 0.0833s | 1.252x | yes |
| default | 0.2945s | 0.3946s | 0.1001s | 1.340x | yes |
| long | 0.2638s | 0.3970s | 0.1332s | 1.505x | yes |

`MEASUREMENT_STATE = VALID_MEASUREMENT` · `PROBE_FAILURES = 0` ·
`MIGRATION_JOBS_ON_ERP_QUEUES = 0` · depths `{short:0, default:0, long:0, cloud_migration:0}` on
both sides · baseline `NO_LOAD` (0 progressed) · loaded `LOAD_PRESENT` (2,000 objects progressed,
migration job starts in both halves of the window) · 105 and 109 job-log rows actually read, no
log missing. **F3_A1 = PASS.**

Artifacts: `scale_base5.json`, `scale_loaded5.json`, `scale_verdict5.json`.

## Why scale mattered, and what it changed

A1 had only ever been measured on a **1,020-object** campaign. `ACCEPTANCE_GATES.md:25` scopes F3
to "release-scale: 100,000 synthetic files", and the effect grows with contention — so measuring
at 1% of scale biases **toward** passing. An independent review raised this; a 100k corpus was
seeded and the measurement re-taken there. The release-scale deltas (0.083-0.133s) turned out
**smaller** than the 1k ones (0.101-0.205s), because the larger batches spend proportionally more
time in S3 transfer and less in the per-object database work that contends with the ERP queues.
The direction was not assumed either way; it was measured.

## Measurements rejected, and why

Nothing here was re-rolled for a better number. Every rejection is on a precondition stated
before the run, and each is listed:

| # | scale | why rejected |
|---|---|---|
| baseline-1 | 1k | `long` recorded 1 probe failure and a 54.55 s outlier; the ruling requires zero failures. Cause: a pre-existing `long` backlog (depth 22) on the shared bench. |
| pair 2 | 1k | baseline taken while the previous campaign was still draining -> `INVALID_MEASUREMENT`. All three queues sat inside the bounds; without the witness this would have been recorded as a clean PASS. |
| pair 1 | 100k | `PARTIAL_LOAD`: all migration job starts fell in the second half of the window. |
| pair 2 | 100k | baseline `PARTIAL_LOAD` — queue depth 0 is not quiescence when a dispatched batch is 1000 objects. The quiesce now waits for object progress to stop moving. |
| pair 4 | 100k | `PARTIAL_LOAD`, halves (2,0) — **the instrument was wrong, not the run**: 783 objects demonstrably moved. Judging continuity from job-start timestamps measures the batch size. `probe()` now samples progress between queues. |

## The one measurement that exceeded the bound

At 1k scale, in an **uncontrolled environment** (two consumers per ERP queue, one of them an
unlogged worker from an earlier session, plus leftover run-stage pollers), one valid pair
exceeded: `default` delta **0.2613s** against the 250 ms bound. It did not reproduce in any of
the five subsequent measurements. It is recorded here rather than dropped, and the environmental
difference is stated as the distinguishing fact rather than used to explain it away.

## Limits of this evidence

* **Headroom is not comfortable.** The worst clean 1k delta was 0.2052s — 82% of the bound. The
  release-scale figures are better, but the spread across runs (0.00s to 0.26s) is wider than the
  margin, so a busier production topology could plausibly exceed it. This is a deployment-sizing
  consideration for the owner, not a gate failure.
* **The load witness proves load, not absence of other load.** It establishes that migration work
  ran during the loaded window and did not run during the baseline. It cannot detect unrelated
  background load inflating the *baseline*, which is what made one earlier 1k pair look
  excellent. The clean protocol addresses that by construction (quiesce, single run stage, one
  worker per queue), not by measurement.
* **One valid pair at release scale.** The three clean 1k pairs and this release-scale pair all
  pass; only one release-scale pair reached `VALID_MEASUREMENT`, because each attempt takes
  roughly an hour and three were rejected on preconditions.
* **Single-machine bench.** ERP workers, migration workers, MariaDB, Redis and MinIO share one
  host, so this measures contention on a co-located deployment.
