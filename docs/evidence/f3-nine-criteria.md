# F3 — the nine pass/fail criteria, reconciled

`ACCEPTANCE_GATES.md:26-36` makes F3 pass/fail on nine items. F3 is **not** closed because A1 and
the disk-I/O criterion pass; every row below has to be PASS, and each states whether it is a
fresh measurement or carried evidence, the SHA that produced it, and — where carried — why the
carry-forward is still valid at the candidate.

The only code shipped since the carried measurements is `migration/engine.py`'s byte accounting
(`ThrottleBudget.consume` counts before the `enabled` gate, is called beside the PUT rather than
after the CAS, and now exposes `calls` and `slept`) plus `run_upload_batch` returning
`transferred_bytes` / `consume_calls` / `throttle_slept_seconds`. That touches **what is counted**, not what is dispatched, retried, deleted,
paused or converged — which is why the carries below hold, and it is named per row rather than
asserted once.

**The "evidence recorded at" column names the commit that recorded the artifact**, resolved with
`git log --follow` over the artifact file — not the commit that happened to be HEAD when a run was
executed. An earlier revision of this table used `7775793` in the second sense for criteria 1, 2
and 9; the A1 release-scale measurement was in fact recorded at `0902f89`.

| # | criterion (`ACCEPTANCE_GATES.md`) | status | fresh / carried | evidence recorded at | why the carry remains valid |
|---|---|---|---|---|---|
| 1 | migration executes only on dedicated `cloud_migration` workers | **PASS** | **fresh** + carried | `0902f89` (fresh) · `d9ea898` (100k) | **Scale: 100,020 objects (fresh A1 run) and the 100k rehearsal.** Fresh: the release-scale A1 run recorded `MIGRATION_JOBS_ON_ERP_QUEUES = 0` with migration jobs observed on `cloud_migration`, and the clean site produced a **red control** — with no `cloud_migration` queue configured the engine *refused to start* ("migration jobs would share the queues the ERP itself uses") instead of falling back silently. Carried: the 100k rehearsal ran on a real site-level `cloud_migration` queue. Dispatch routing is untouched by the byte-accounting change. |
| 2 | normal short/default/long ERP queues unaffected (probe-job latency at baseline) — **A1** | **PASS** | **fresh** | `0902f89` | **Scale: 100,020 objects.** Measured at release scale (100,020 objects) against the owner's frozen tolerance: deltas 0.0833 / 0.1001 / 0.1332 s vs `<= 0.250 s`, ratios `<= 1.505x` vs `<= 2.0x`, 0 probe failures, 0 migration jobs on ERP queues, depths recovered. `docs/evidence/f3-a1-erp-queue-latency.md`. |
| 3 | bounded CPU, DB load, Redis pressure, memory, disk I/O vs recorded baselines | **PASS** | carried | `f399fd4` · re-verified round 4 at `2df3bbb` | **Scale: 6,278 objects across two comparable runs — not release scale; see the limitation below.** `docs/evidence/f3-disk-io-scale.md`: growth **sublinear** — per-object device write *falls* 296.7 → 174.1 KB/object, app RSS flat at ~577 MB across 6,278 objects, `await` *improves* under load (4.03 → 2.05 ms), utilisation peaks at 30%; Redis peak 2.41 MB, F4's own peak RSS 57 MB / Redis 1.60 MB. Resource ceilings are a property of the batch loop and the S3 client, neither of which changed; the accounting change alters a counter, not the work done. |
| 4 | bandwidth throttle demonstrably enforced | **PASS** | **fresh** | this commit | **Scale: 2 comparable batches x 100 objects, ~30 MB each — deliberately byte-dominated, not release scale.** Clean scratch site `cfs-thr4.local`, campaign `CFS-CAMP-0009`, bucket `cfs-thr-remeasure-20260819g`, gated by the committed `throttle_preflight` stage: all six checks YES, `PREFLIGHT=PASS`, `batches_pending: 3` (`docs/evidence/f3-throttle-preflight.json`). **Throttled:** 100 objects, **30,172,702 bytes transferred**, 120.73 s -> **249,925.8 B/s** against a 250,000 B/s ceiling = **99.97%**, with the limiter asleep **115.84 s — a sleep fraction of 0.9595** against a 0.25 floor, so the ceiling and not the corpus bounded the run. **Unthrottled:** 100 objects, 30,253,262 B, 3.69 s -> 8,208,384.3 B/s (**32.84x faster**). `consume_calls` 100/100, `dedup_reused_objects` 0, `failed_objects` 0. `THROTTLE_CRITERION=PASS`, `measurement_state=VALID_MEASUREMENT`, `throttle_engaged=true` — all emitted by the harness, not derived by a reader. Artifact: `docs/evidence/f3-throttle-measurement.json`, carrying site, campaign, bucket and timestamp. |
| 5 | bounded RQ job count (≈ 2× batch count; no job explosion) | **PASS** | carried | `463e3d1` | **Scale: 5 batches / 270 objects — not release scale; see the limitation below.** `docs/evidence/f3-throttle-and-jobs.md`: **8 jobs across 5 batches = 1.60 per batch** against a design bound of ~2, **0 redelivered, 0 on ERP queues**, on a run that converged 270/270 with every executed job recorded with its queue. Job *enqueue* behaviour is unchanged by the byte-accounting commit. |
| 6 | crash/restart recovery (kill -9, Redis flush, bench restart) converges | **PASS** | carried | `3b9272e` (fault suite) · `d9ea898` | **Scale: 1,200 and 2,992 objects (fault suite); kill -9 at 100,020.** `docs/evidence/f3-crash-recovery.md`: Redis flush — 4 keys destroyed with 2 batches in flight, resumed in **20.1 s**, converged 1200/1200. Worker restart — both workers killed at 1,728 objects, resumed in **10.1 s**, converged 2992/2992. kill -9 at 100k evidenced durably by `B2.last_error = "heartbeat expired; recovered by dispatcher"`, a string written only by `engine.recover_stale_batches`, with that batch reaching `Verified` then `Cleaned`. Recovery is driven by heartbeats and CAS, untouched here. |
| 7 | pause/resume and stop-after-batch honored | **PASS** | carried | `d9ea898` (P5 attestation) · `d1ce1a8` (Desk smoke) | **Scale: 100 fixture files (Desk smoke); P5 attestation at 100,020.** Recorded MET in the P5 attestation and exercised again by the P7 Desk smoke, which drove Analyze → Plan → Start → **Pause** on a live campaign (100 fixture files, 17 objects uploaded, 83 still Pending when the pause landed). The control-flag check sits at the top of the batch loop and is unchanged. |
| 8 | convergence ≥ 99.9% verified | **PASS** | carried | `d9ea898` | **Scale: 100,020 objects.** Stated on a denominator fixed by principle rather than by outcome: exclude the **66 deliberately seeded missing files** (File rows pointing at bytes that were never written — unmigratable by construction, and seeded precisely to prove the engine refuses them) and the **53 objects already `Skipped` before the run**, and count everything else as owed. That gives **99,862 / 99,901 = 0.99961 ≥ 0.999**, with the **39 harness-gap legacy rows counted as NOT converged** even though their failure was the seeder's (`_seed_legacy_objects` was added after this corpus was built) and not the engine's. The 50 privacy mismatches converge on the operator-resolution path (A19) and are counted as converged. **The engine-only pre-triage ratio was 0.99845, which misses the target, and the post-triage 1.0 is not claimed as the pass** — triage moves unconverged objects into `Skipped`, and `Skipped` is excluded from the denominator, so that 1.0 is arithmetically inevitable and evidences nothing. **The 66-object exclusion is load-bearing and this row says so:** counting the
seeded missing files in the denominator gives 99,862 / 99,967 = **0.99895**, which misses the
target. The exclusion rests on their being unmigratable by construction — File rows pointing at
bytes that were never written — not on their being inconvenient. |
| 9 | zero delete-before-verify (lint + runtime assertion) | **PASS** | carried + **fresh lint** | `d9ea898` (runtime) · re-run at this candidate (lint) | **Scale: 100,020 objects (runtime); the lint is whole-repo by AST.** Runtime: the 100k rehearsal recorded **zero delete-before-verify**, runtime-asserted, with CLEANUP quarantining 99,862 objects into the trash tree rather than deleting. Lint, re-run fresh at the candidate by AST rather than grep: the UPLOAD path (`engine.py`) and VERIFY path (`verify.py`) contain **zero** deletion calls; the only deletions in `migration/` are the two in `cleanup.py`, the deferred-GC phase where deletion is permitted after verification and grace. |

**F3 = PASS.** All nine rows are PASS: two measured fresh at the candidate (A1, throttle), one
with a fresh lint half (delete-before-verify), and six carried with the reason stated and the
producing commit named.

## What is deliberately not claimed

* **Criterion 8 is a pass on a stated denominator, not on the post-triage 1.0.** The 1.0 is
  arithmetically inevitable — triage moves unconverged objects into `Skipped` and `Skipped` is
  excluded from the denominator — so it is reported and explicitly not relied on. The pass rests
  on 99,862 / 99,901 = 0.99961, with the 39 harness-gap rows counted against us.
  **One exclusion carries it.** Counting the 66 seeded missing files gives 99,862 / 99,967 =
  0.99895 — a miss. Correcting only for the 50 operator-resolved privacy conflicts and leaving the
  66 in gives the same 0.99895. So the criterion turns on accepting that a File row pointing at
  bytes which were never written is unmigratable by construction. That is stated here rather than
  left for a reader to discover by recomputing.
* **The throttle's engagement floor is a threshold, and thresholds can be argued with.**
  `THROTTLE_MIN_SLEEP_FRACTION = 0.25` requires the limiter to have been asleep for at least a
  quarter of the throttled run. `slept > 0` alone was not enough: one object large enough to
  overshoot the bucket once, followed by a long overhead-bound crawl, sleeps briefly and then
  finishes at ~3% of the ceiling with the limiter idle — the very run this criterion rejects. The
  floor is pinned from both sides by test, and the measured run sits at 96%, far above it.
* **Criteria 3 and 5 were measured well below release scale** (6,278 objects; 5 batches). They
  are carried on the argument that resource ceilings and job-enqueue counts are properties of the
  batch loop, which is unchanged — not on the claim that they were observed at 100k.
* **The throttle is measured on the object-upload path only.** Thumbnail derivatives are uploaded
  outside `ThrottleBudget.consume`, so they are neither rate-limited nor counted. On a
  thumbnail-heavy corpus real egress therefore exceeds the configured ceiling by the thumbnail
  volume. That is a product limitation, recorded here rather than fixed in this release.
* **Superseded measurements — the full list, with inputs, extended rather than rewritten.** Each
  earlier run of this criterion is kept with its disposition and the numbers it was derived from,
  so nothing has to be reconstructed later. None is retracted as *wrong on rate*; each was
  displaced because the code that gates or judges the measurement changed and the artifact could
  no longer honestly claim to come from it.

  | # | bytes | elapsed | rate B/s | % ceiling | sleep frac | free B/s | why it no longer stands |
  |---|---|---|---|---|---|---|---|
  | 1 | 34,194,160 | 136.82 s | 249,928.8 | 99.97 | — | — | **Withdrawn as evidence.** The harness never SELECTed `objects_failed`, so `failed_objects: 0` had consulted nothing, and there was no engagement witness at all. |
  | 2 | 29,744,458 | 119.01 s | 249,927.8 | 99.97 | 0.9615 | 7,233,904.7 | Predates the engagement floor and the committed preflight stage. |
  | 3 | 29,472,439 | 117.92 s | 249,937.5 | 99.97 | 0.9603 | 7,822,759.9 | Predates three corrections to `throttle_preflight`: a campaign status that does not exist, an unconsulted prefix, a worker scan blind to concurrent writers. |
  | 4 | 29,977,246 | 119.95 s | 249,906.8 | 99.96 | 0.9319 | 5,628,971.9 | Predates the starting-state fix — the precondition was satisfiable by a site with no migration objects at all. |
  | 5 | 30,927,079 | 123.79 s | 249,825.5 | 99.93 | 0.9318 | 5,721,489.1 | Predates the precondition being raised to the quantity the stage consumes: two Pending UPLOAD batches, not one Pending object. |
  | 6 | 30,258,880 | 121.12 s | 249,818.7 | 99.93 | 0.9332 | 6,317,921.9 | Predates `pending_batches` gaining the `phase: UPLOAD` filter — the count was one keyword short of the contract its own docstring stated. |
  | **shipped** | **30,172,702** | **120.73 s** | **249,925.8** | **99.97** | **0.9595** | **8,208,384.3** | Produced, gated and judged by the code that ships. |

* **The utilisation excursion (99.97 → 99.93 → 99.97) is arithmetic, not a limiter losing precision.**
  The token bucket sleeps until `elapsed == consumed / rate`, but after the **final** `consume` the
  batch still does uncompensated work — the last CAS, `finalize_upload_batch`, `refresh_counters`,
  `chain_next` — before `throttle()` samples `elapsed`. That tail is never slept against, so
  `achieved = consumed / (budget + tail)` and utilisation is exactly `1 − tail/elapsed`:

  | run | budget | elapsed | **tail** | 1 − tail/elapsed | reported |
  |---|---|---|---|---|---|
  | 2 | 118.9778 s | 119.0122 s | **0.0344 s** | 0.99971 | 0.9997 |
  | 3 | 117.8898 s | 117.9192 s | **0.0295 s** | 0.99975 | 0.9998 |
  | 4 | 119.9090 s | 119.9537 s | **0.0447 s** | 0.99963 | 0.9996 |
  | 5 | 123.7083 s | 123.7947 s | **0.0864 s** | 0.99930 | 0.9993 |
  | 6 | 121.0355 s | 121.1234 s | **0.0878 s** | 0.99927 | 0.9993 |
  | shipped | 120.6908 s | 120.7266 s | **0.0358 s** | 0.99970 | 0.9997 |

  Every reported figure reproduces to four decimal places. The whole drift is a finalisation tail
  moving between 29 ms and 88 ms with host load; **the sleeping portion tracks the ceiling to
  within milliseconds in every run.** The shipped run confirms the model rather than merely
  restating it: the host recovered, the tail fell back to **36 ms**, and utilisation returned to
  **99.97%** — a prediction the tail account makes and a "the limiter is drifting" account does
  not.

* **Seven runs at this ceiling is replication, not a file drawer — and the record says so because
  the question is fair.** The headline moved six times, every time because gating or judging code
  changed, never because a number disappointed. Three things make that checkable rather than
  asserted.

  **(a) The schedule was never the author's to choose.** Every regeneration was forced by an
  external review finding, and the seventh ran *against the reviewer's explicit written advice
  that it was unnecessary* — it argued the artifact-preserving case and the run happened anyway,
  with no way to know what number would come out. A file drawer requires the author to control
  when to stop; here the stopping points were set by findings, and the last one by overruling
  advice to stop.

  **(b) A 0.048% spread leaves nothing to select between.** Selection bias is only possible in a
  quantity that varies. Across the seven runs the throttled rate spans **249,818.7–249,937.5 B/s**
  — whichever run had shipped, the criterion reads the same to four significant figures. That
  argument does not depend on which run shipped, which makes it immune to the objection that
  retired this paragraph's first version. The unthrottled comparison, by contrast, spans **46%**
  (5.63–8.21 MB/s): one number is pinned by the limiter, the other by the machine's load that hour.

  **(c) Both runs the harness rejected were unfavourable and both are recorded below** — which is
  exactly what a file drawer does not do.

  *An earlier version of this paragraph claimed the shipped figure was "the least flattering of
  the set", with utilisation falling monotonically. That was true when written and false by the
  seventh run: the sequence is 99.97, 99.97, 99.97, 99.96, 99.93, 99.93, 99.97, and the shipped
  figure is the joint-highest. It is recorded here rather than quietly deleted because it was an
  overclaim in the one paragraph whose job is to show the evidence was not selected.*
* **Two runs during this measurement were rejected by the harness**, which is the guard working:
  one at 25,967 B across 100 objects reported `throttle_engaged: false` / `INVALID_MEASUREMENT`
  because the corpus was overhead-bound and the limiter never slept (2.68% of ceiling would
  otherwise have read as comfortable compliance); an earlier environment produced
  `bytes_transferred: 0` and was held at `NO_TRANSFER` / `INVALID_MEASUREMENT`.
* **The preflight is executable, not a description.** `rehearsal.py`'s `throttle_preflight`
  stage emits the six verdicts and `PREFLIGHT=PASS|FAIL`; its output for this run is committed at
  `docs/evidence/f3-throttle-preflight.json`, and its refusal on a dirty environment at
  `docs/evidence/f3-throttle-preflight-refusal.json` — re-run against the environment left behind
  by this very measurement, it returns `PREFLIGHT=FAIL` on three of six checks
  (`BUCKET_PREFIX_EMPTY_OR_UNIQUE`, `ACTIVE_CAMPAIGNS`, `STARTING_OBJECT_STATE_VALID`), so the
  gate is demonstrably capable of refusing. An earlier revision of this row asserted the
  six checks in prose with nothing in the tree behind them, which is the same defect as a gate
  whose application is optional.
* **The throttle criterion has not been independently replicated.** All seven runs at this ceiling
  were produced by the same author on one bench, one site and one MinIO; the criterion has never
  been reproduced on other hardware, and the manual stage is not covered by the three consecutive
  suite runs. What makes it checkable rather than trusted is that the preflight, the verdict and
  both thresholds are committed and mechanically applied — `THROTTLE_CRITERION` is emitted by
  `throttle_verdict`, the ceiling slack and the engagement floor are each pinned from *both* sides
  by test, so the pass condition cannot be moved without failing the suite — and that the bucket
  state corroborates the transferred bytes independently of the instrument: had `put_path`
  silently no-op'd, the refusal artifact could not show `residual_cso: 200` and a non-zero bucket.
* **The red control for criterion 1** is cited from this bench: with no `cloud_migration` queue
  configured, the engine refused to start — *"The dedicated cloud_migration queue is not
  configured on this bench, so migration jobs would share the queues the ERP itself uses"* — and
  did not silently fall back to the ERP queues. Observed on `cfs-thr4.local` before the queue was
  configured for it.
* The fresh throttle measurement ran with `sse_mode` empty **on the scratch site only**, because
  this bench's MinIO has no KMS and answers `NotImplemented` to `ServerSideEncryption: AES256`.
  That is a supported product setting, not a code change; the shipped default is unchanged and a
  production bucket must have encryption available.
* Criteria 3, 5, 6, 7 and 8 are carried, not re-measured at the candidate. Each names the code
  path it depends on and why the only shipped change does not touch it.
