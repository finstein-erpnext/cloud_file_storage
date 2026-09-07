# F3 — measurements taken after the release audit's A1 finding

Site `cfs-throttle.local` (new scratch site, isolated MariaDB :3307, real MinIO, real RQ
workers on the site's own `cloud_migration` queue). Corpus purpose-seeded as bandwidth-bound:
250 objects, 32.5 MB, ~131 KB each — against the standard corpus's 120-400 B, which is why the
P5 attempt could not complete this.

## The harness gap that caused the P5 failure — fixed

P5 recorded: "a purpose-seeded bandwidth-bound corpus was swallowed by the site's existing
100k one." The deeper cause was that the harness had **no way to seed one**: the payload band
was a module constant. `seed` now accepts `--min-bytes/--max-bytes`, and `cfs-throttle.local`
is registered as a scratch site. That change is what made the measurement possible at all.

## 1. End-to-end bandwidth throttle — MET

Two comparable batches through the same `run_upload_batch` a worker calls, one limited and one
not, inline with no workers running so the throttle is the only difference:

    throttled   2 Mbps  60 objects  7,349,094 B  29.48s  ->    249,315.7 B/s  (ceiling 250,000)
    unthrottled  none   60 objects  7,421,958 B   3.45s  ->  2,151,848.1 B/s
    passed: true

99.7% of the configured ceiling; 8.6x faster unthrottled on a comparable batch.

## 2. Bounded RQ job count — MET

Full campaign through two real RQ workers on the dedicated queue, every executed job recorded
with the queue it came off:

    batches 5 · jobs_executed 8 · jobs_distinct 8 · jobs_redelivered 0
    jobs_per_batch 1.60                (design bound ~2 per batch)
    by_queue {"home-user-v15:cloud_migration": 8}
    jobs_on_erp_queues 0 · dedicated_queue_only_pass: true

Outcome of that run: 270/270 objects Verified, 5/5 batches Verified.

## 3. Disk I/O against a recorded baseline — ENVIRONMENT_BLOCKER

`/proc/self/io` reports zero for the measuring process on this WSL2 bench, as recorded at P5.
Not measurable here by any route available to this delivery. Recorded as unmeasured, never as
zero.

## 4. Redis flush / bench restart at campaign scale — STILL OPEN

Both remain unit-level only. The fault stages need work in flight; this campaign converged
(270/270) before a fault could be injected, and creating a fresh one is blocked by the campaign
lifecycle guard — "A campaign in state Planned cannot be deleted — it still owns migration
work" — which is the product behaving correctly. Removing the site to start over was refused by
`autonomy_guard` as a SAFETY_BOUNDARY action, also correctly.

## Net effect

Two of the four unmet criteria are now MET (throttle, bounded job count). One is
environment-blocked; one remains open. **F3 is still PARTIAL**, so the auditor's A1 finding
stands and RELEASE_CANDIDATE remains unreachable under ACCEPTANCE_GATES.md:3.

## Incidental findings from this exercise, none of them product defects

- The circuit breaker (A30) opened under repeated credential failures and refused further
  uploads — correct behaviour, observed live rather than in a unit test.
- The campaign lifecycle guard refused deletion of a campaign that still owns work.
- `autonomy_guard` refused the site-removal command as a SAFETY_BOUNDARY action.
- The scratch MinIO has no KMS, so `sse_mode = SSE-S3` made every PUT fail with
  `NotImplemented`. A scratch-environment limitation, not a product defect: SSE is correct
  against real S3. Disabled on this scratch site only.
