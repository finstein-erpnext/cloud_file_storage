# Cloud File Storage v1.0.0 — Release Evidence

> **Status: F1–F6 PASS · F7 PENDING EXTERNAL RELEASE-MANAGER ATTESTATION.** F3 is **PASS on all
> nine of its criteria**: A1 measured at release scale, the bandwidth throttle measured in a
> clean preflighted scratch environment, delete-before-verify re-linted at the candidate, and the
> remaining six carried with the reason stated per row. The reconciliation is
> `docs/evidence/f3-nine-criteria.md`.
>
> **This document deliberately does not record an F7 verdict, and no commit will be made to add
> one.** F7 is the independent release-manager attestation, and a tree cannot contain the verdict
> of an audit performed against itself: any commit flipping `PENDING` to `PASS` would be a SHA the
> audit never examined. The attestation is therefore recorded **outside this repository**, keyed
> immutably to the SHA it approves. That is an owner ruling, made after the `203d67e` audit showed
> the reconciliation could not otherwise terminate.
> Round 4 of the independent release audit, at `2df3bbb`, verified F1, F2, F4, F5 and F6 as
> PASS, with F3 blocked on **A1**. That blocker is closed: the owner ruled A1's tolerance on
> 2026-08-18 (per queue, p95, `delta <= 250 ms` **and** `ratio <= 2.0`, >=30 probes each side,
> zero probe failures, zero migration jobs on ERP queues, depths recovering), and A1 was
> measured against that frozen threshold. An independent review then found that A1 had only ever
> been measured on a **1,020-object** campaign while `ACCEPTANCE_GATES.md:25` scopes F3 to
> release-scale 100,000 files — and since the effect grows with contention, measuring at 1% of
> scale biases toward passing. A 100k corpus was seeded and A1 re-measured there.
>
> **A1 at release scale** (`CFS-CAMP-0008`, 100,020 objects, 100 batches, 156 MB):
> deltas **0.0833s / 0.1001s / 0.1332s**, ratios <= **1.505x**, `VALID_MEASUREMENT`,
> PROBE_FAILURES **0**, MIGRATION_JOBS_ON_ERP **0**, depths recovered — **F3_A1 = PASS**.
> Full artifact, including every rejected measurement and the limits of the evidence:
> `docs/evidence/f3-a1-erp-queue-latency.md`.
>
> **F3's throttle criterion is now PASS**, measured in a clean scratch environment. The review
> prompted a fix to `ThrottleBudget.consume` (it was counting, and sleeping for, bytes on the
> dedupe path where nothing crosses the network), which invalidated the previous figure under
> Rule 2. Re-establishing it took a purpose-built site because three environmental conditions
> blocked it in turn — a credential that never persisted (a Password field handled through
> `hasattr` instead of `get_password`), a duplicate-key collision against shared-site content, and
> the A30 circuit breaker left open by the resulting failures — followed by a fourth, `SSE-S3`
> against a MinIO with no KMS. **Every one of those runs is `ENVIRONMENT_INVALID`: none is a PASS,
> a FAIL, or zero-throughput evidence.** On the clean site: **249,925.8 B/s against a 250,000 B/s
> ceiling — 99.97%** — from 30,172,702 genuinely transferred bytes, with the limiter asleep for
> **96% of the run against a 25% floor**, 32.8x faster unthrottled, gated by the committed
> `throttle_preflight` stage and with `THROTTLE_CRITERION=PASS` emitted by the harness.
> (An earlier revision of this summary quoted 249,928.8 B/s over 34,194,160 bytes at 36.59x. That
> run is **formally withdrawn** — see below — and citing it here offered a withdrawn measurement
> as the evidence for a release gate.) Details and the recorded `sse_mode` deviation:
> `docs/evidence/f3-nine-criteria.md` and `DECISIONS.md`.
>
> **F7 is PENDING EXTERNAL RELEASE-MANAGER ATTESTATION.** It names the independent
> release-manager audit, which **must** be run freshly against the terminal docs SHA and whose
> verdict is recorded outside this repository. An earlier revision said the owner had directed
> that F7 not be run yet; that instruction was **superseded** by the terminal-documentation
> ruling, which requires the opposite — F1-F6 may be inherited after positive verification that
> a docs-only delta cannot affect them, and F7 is then run against that SHA.
>
> **Local requalification is complete at the candidate**, re-derived rather than carried forward —
> a pristine `cfs-req3.local` created from nothing (`new-site` + `install-app` rc=0,
> `migrate` rc=0, **12 patches applied including both bootstrap patches**
> `adopt_legacy_fork_install` and `link_legacy_fork_objects`), plus the real co-installation on
> `cfs-ecosystem.local` (erpnext · hrms · payments · india_compliance):
>
> | layer | runs | result |
> |---|---|---|
> | app (MinIO enabled) | **1511 × 3** consecutive | **OK (skipped=5)** |
> | L3 ecosystem | **1502 × 2** consecutive | **OK (skipped=2)** |
>
> Contract **C1..C19 19/19 executed** · backup **61 refusal guards, 0 skipped** · MinIO **19
> real-S3, 0 skipped** · **F2 mission matrix 43/43 scenarios + 12 A-series + thumbnail
> availability across both junits, 0 missing / 0 skipped / 0 failed** · ecosystem completeness
> **9 exit-criterion tests, 0 skipped** · F3 regression suites **72/72** · Semgrep **0
> findings** · `pre-commit run -a` clean with **zero files modified**.
>
> F6 invariants re-checked at this SHA by AST rather than grep (a prior grep FAILed on
> `engine.py`'s own docstring saying there are no deletion calls): no `ACL` key anywhere; the
> archive storage classes exist only in `backup/lifecycle.py` and are never applied to the live
> bucket; and the UPLOAD path (`engine.py`) and VERIFY path (`verify.py`) contain **zero**
> deletion calls — every deletion in the migration package is the 2 in `cleanup.py`, which is
> the deferred-GC phase where deletion is permitted after verification and grace.
> Branch `feat/enterprise-cloud-storage-v15`. **The frozen SHA is deliberately not written
> here**: three consecutive rounds of this audit found it stale by the very commit that
> recorded it, because a document that hand-records its own SHA is out of date the moment it
> is written. Derive it at tag time — `git rev-parse HEAD` — and read the phase table below
> for what happened at each SHA.
>
> **Every figure here is re-derived at the SHA it names rather than carried forward** (Rule 2,
> below). **Current, at `a76ace6`** — both layers, pristine `cfs-qual.local` plus the real
> co-installation on `cfs-ecosystem.local`:
>
> | layer | runs | result |
> |---|---|---|
> | app (MinIO enabled) | **1406 × 3** consecutive | **OK (skipped=5)** |
> | L3 ecosystem (erpnext · hrms · payments · india_compliance) | **1420 × 2** consecutive | **OK (skipped=2)** |
>
> Contract **19/19** · backup **61 refusal guards, 0 skipped** · MinIO **19 real-S3, 0 skipped**
> · **F2 mission matrix 43/43 scenarios + 12 A-series + thumbnail availability across both
> junits, 0 missing / 0 skipped / 0 failed** · `pre-commit run -a` clean with **zero files
> modified** · `new-site`/`install-app`/`migrate` all rc=0.
>
> Earlier figures below are labelled with the SHA that produced them (1365×3 at `8093fda`,
> 1367 at the pass-ceiling SHA) and are **not** the current attestation. The F2 figure is stated
> as spanning two junits because a single-layer run reports T-RIV unexecuted — that is the gate
> working, not a gap: T-RIV needs erpnext and skips loudly without it.
>
> `docs/ACCEPTANCE_GATES.md:3` defines the terminal state as **F1–F7 all PASS**. It is not yet
> reached. **F1 PASS · F2 PASS · F4 PASS · F5 PASS · F6 PASS** — F1, F2, F4 and F5 **re-measured
> and re-cited at `203d67e`** (run 32254745359), each labelled with its own at-candidate job id in
> its row; F6 carries the round-4 verification at `2df3bbb`, re-executed by the `203d67e` release
> auditor. An earlier revision of this sentence attributed all five to `2df3bbb` and said no
> re-measurement had been taken — that contradicted the table below and is corrected here. **F3 is PASS** on all nine
> criteria (`docs/evidence/f3-nine-criteria.md`).
> **F7 NOT MET** — F7 names both F1–F6 and the release-manager audit, and that audit has **not
> been re-run** at this candidate. F1 and F5 moved to
> PASS in round 4: the CI push happened and all three matrix refs plus the mandatory L3 job are
> green. The code is complete and independently gated, and **every F1-F6 criterion is closed** —
> including A1, whose tolerance the owner ruled on 2026-08-18 and which now passes at release
> scale. What remains outstanding is **F7 only**, which is an external release-manager
> attestation and not something this tree can contain. See the F1-F7 table below,
> which is the authority — this paragraph summarises it and must not disagree with it.
>
> Every figure is recorded against the SHA that produced it. Nothing is carried forward across a
> SHA change (see "Evidence rules"). Unmet items are stated as unmet, not omitted.

## Evidence rules this release was held to

Three rules were adopted mid-delivery after each was learned expensively. They govern how every
figure below should be read.

1. **A green is evidence only if the run could have gone red.** Not "could this fail in
   principle" — *did THIS run exercise an input that would have failed?* Both of this project's
   worst misses would have passed the weaker question.
2. **A tree change invalidates prior evidence, regardless of whether the change was semantic.**
   Applied literally: a whitespace-only `ruff-format` commit invalidated a full gate and it was
   re-run. "The change was only whitespace" is a claim about the change; the gate is evidence
   about the tree.
3. **"Green twice" states nothing without the site state that produced it.** A leak that damages
   its own baseline makes a *worn* site agree with itself. The bar is twice-consecutive **from a
   pristine install**, with the singleton compared between runs 2 and 3 (run 1 legitimately
   differs on a fresh site).

A fourth, earned at the convergence gate: **a phase gate needs a reader AND a runner.** The one
HIGH found there was invisible to six green suite runs and a passing security review.

## Phase results

| Phase | SHA | Verdict | Independent gates |
|---|---|---|---|
| P0 | — | PASS | recorded in PROGRESS |
| P0.5 | — | PASS | independent architecture review |
| P1 | — | PASS | reviewer + test |
| P2 | — | PASS | reviewer + security |
| P3 | — | PASS | reviewer + security |
| P4 | — | PASS | reviewer |
| P5 | — | PASS | reviewer (2 rounds) |
| P6 | `6d954dc` | **PASS** | reviewer PASS, security PASS, orchestrator attestation |
| P7 | `45022e8` | **PASS** | reviewer PASS, security PASS, orchestrator attestation |
| P6∥P7 convergence | `2bdec72` | **PASS** | reviewer PASS, test-engineer PASS, security PASS |
| P8 | `917b463` | **PASS** | reviewer PASS, security PASS, test-engineer PASS (zero survivors) |
| P8 merge | `1f10e66` | **PASS** | orchestrator attestation on the merged tree |
| G5 fix | `8093fda` | **PASS** | orchestrator-implemented; re-verified against all three axes, and independently re-attacked by the release auditor on a fourth |
| Release evidence | `3f73401` | — | this document, first published |
| Release audit round 1 | `3f73401` | **NO-GO** | 5 blockers; A1 (F3 PARTIAL) with the owner |
| Audit remediation 1 | `463e3d1` | — | A2–A5, B4, B5 closed; F3 throttle + job count measured |
| Release audit round 2 | `463e3d1` | **NO-GO** | 4 blockers verified closed; 6 new findings, 1 a real Mode 1 defect in the new test |
| Audit remediation 2 | `a903d03` | — | N1–N6 closed; gate accounting corrected |
| Release audit round 3 | `a903d03` | **NO-GO** | N1–N6 verified closed; 5 new findings, all non-blocking; **A1 remains the only blocker** |
| CI qualification | `2df3bbb` | — | first green 3-ref matrix + MinIO + L3 in the delivery; combined coverage gate ran to a verdict and **failed at 76%** |
| Owner ruling | 2026-08-18 | — | coverage = `NOT_APPLICABLE_TO_F1_F7`, non-blocking quality debt; 90% target unchanged |
| Release audit round 4 | `2df3bbb` | **NO-GO** | **F1, F2, F4, F5, F6 PASS** (F1 and F5 newly closed by CI; F6 re-derived, not carried). **F3 BLOCKED(OWNER_DECISION_REQUIRED) on A1** — unchanged across all four rounds. Coverage ruling verified legitimate against the frozen text; CodeQL verified unweakened. Two documentation defects found and fixed in this round: this document contradicted itself on F3 (table said PASS, "remains unmet" said never completed), and the CHANGELOG's `1.0.0` compare link pointed at the upstream ALYF repository where `v1.0.0` will never exist. |

## Attestation figures

Each phase's attestation — pristine site, run counts, residue, mutation results — is recorded
in **`docs/PROGRESS.md`**, one row per phase, which is the durable record of exactly that.
It is not duplicated here: two homes for the same numbers is how they drift, which is the
argument this document makes about itself elsewhere. The current-tree figures are in the
header and below.

## Security

**Independent security review: PASS (release-scope)** at the convergence SHA. No Critical, no
High. Every finding filed across the engagement is closed and verified except **three** Lows carried
as recorded debt, none a reachable path to harm.

Release-scope statement, from the reviewer:

> Every control tested is server-side, reachable over `/api/method/` without a browser, and
> covered by tests that need no browser. **The operator role is read-only on every one of the
> twelve doctypes the app ships.** Every destructive action is System-Manager-gated,
> type-to-confirm gated where it deletes, and audited. No presigned URL is minted outside four
> enumerated sites, each with its own permission gate and its own record.

Invariants **1, 4, 5, 6, 7, 9, 10** all verified HOLD on the merged tree, each against a
mechanism rather than a claim.

### Static analysis — Semgrep PASS, CodeQL blocked by org configuration

**Semgrep: PASS.** Every blocking finding classified individually in
`docs/security/semgrep-adjudication.md`, with **no wildcard or global suppression**, and no
suppression applied to any category the owner prohibited (string formatting/injection,
exception-string leakage, unsafe path construction, permission bypasses, credential/logging
issues, avoidable manual transaction control).

**CodeQL: `BLOCKED_EXTERNAL_ADMIN_CONFIGURATION` — explicitly NOT a PASS.**

Owner policy amendment of 2026-08-18 classifies this as an `EXTERNAL_PLATFORM_LIMITATION`:
Code Security / code scanning is controlled by the GitHub **organisation** administrator and
cannot be enabled by the project owner, so the SARIF upload is refused with "Code scanning is not
enabled for this repository". That refusal is a platform permission fact, **not an application or
runtime defect** — but the amendment states in its own terms that it "does not constitute CodeQL
PASS", and this document does not record one.

The amendment supersedes the earlier owner statement requiring CodeQL upload to pass before this
release candidate. It does **not** move any F-gate: F1–F7 do not require CodeQL upload, so this
limitation was never the binding blocker, and removing it changes nothing about what is.

**The workflow is intact and is verified unweakened at the terminal tree — this SHA** rather than assumed — none of
the five prohibited weakenings is present:

| prohibited | state at the terminal tree (this SHA) |
|---|---|
| `upload: false` | absent |
| CodeQL job removed | present — `analyze`, python matrix |
| `continue-on-error` for CodeQL | absent |
| SARIF upload disabled | `security-events: write` retained; `analyze@v3` unmodified |
| security analysis deleted | **Proven from the current workflow, not from chronology.** At the terminal SHA — this one — `.github/workflows/codeql.yml` retains `security-events: write`, `github/codeql-action/init@v3` and `github/codeql-action/analyze@v3`, declares exactly one job (`analyze`) which is present and enabled, and carries **no** `upload: false`, **no** `continue-on-error`, **no** `if:` condition that could silently skip it, and no suppression of a CodeQL failure into a pass. The only change since the last independent verification is `git diff 2df3bbb..cfc5c97 -- .github/workflows/codeql.yml` = **+4 lines**: `timeout-minutes: 60` and a three-line comment — CI-hardening from the timeout series, semantically inert to CodeQL. *An earlier revision of this row argued the file was "last touched `b8a8417`, four days before the failure". That **became** false — it was accurate when written, and `09e17a0` and `ad5a7fb` touched the file afterwards — and a timestamp is the wrong instrument regardless: it cannot distinguish hardening from weakening.* |

When the org administrator enables code scanning, this workflow runs as-is. No change to it is
required or permitted to obtain a green result.

## Stated gaps — not omissions

1. **No browser-level verification of rendered output.** The Playwright smoke ran on the pre-fix
   tree and its artefact re-run is unmet: the Desk does not boot on this bench, reproduced on a
   **frappe-only control site** with `installed_apps == ['frappe']`, so the tree under test is
   not implicated. Wording is the security reviewer's: **not** "Desk UI unverified" — the role
   gates, state machine, confirm phrase and permission separation are all proven server-side.
   Mitigation: an AST check enumerating every JS interpolation and requiring `escape_html`,
   which **catches the one real unescaped interpolation this project shipped** — where the
   substring form of the same check returns TRUE against that same tree.
2. ~~**Only frappe v15.93.0 is exercised on this bench.**~~ **Closed at `2df3bbb`.** The push
   happened and CI ran the full `{v15.16.0, v15.93.0, version-15}` matrix plus the mandatory L3
   job, all green. The bench remains single-reference; the *gate* no longer is.
3. **Carried Lows** — **three** from security (`L-2`, `L-9`, `L-12` residue), all recorded, none blocking.

## How this release was tested, in one line

Every green here was checked by mutation as well as by reading: an assertion that cannot fail
is worse than no assertion. The failure taxonomy this delivery worked against, the eight-level
defect recursion the release audit traced through it, and the mid-attestation environment
collapse are all recorded in **`docs/DECISIONS.md`** — they describe how the work was done, not
whether this tree ships.

## The reconciliation a local runner will need

**The same tree measured without the object store reports 1346 tests and 5 skips**, and the two
reconcile exactly:

```
1346 + 19 real-S3 tests = 1365
   5 skips - 1          = 4     (the MinIO module's class-level skip stops firing)
```

Both figures are stated because a reader who sees only 1365 and then runs 1346 locally will
assume something broke. They are one tree measured two ways.

### L3 ecosystem regression — the real co-installation

`cfs-ecosystem.local`, carrying **erpnext 15.93.0 · hrms 15.49.2 · india_compliance 15.18.1 ·
payments 0.0.1** alongside this app:

```
migrate rc=0
run 1   Ran 1379 tests   OK (skipped=1)
run 2   Ran 1379 tests   OK (skipped=1)
```

1379 > 1365 because the ecosystem classes that skip on a bare site execute here, and skips fall
4 -> 1 for the same reason. This is the gate that would catch a `get_file_path` patch colliding
with India Compliance's import-bound copy.

## The one change with no separation of duties

**The G5 pass-ceiling fix at `8093fda` was implemented by the orchestrator and verified by the
orchestrator**, because both subagents died to API timeouts before it could be handed back.
That is exactly what this project's gates exist to prevent, so it is stated here rather than
buried. The independent release auditor subsequently re-verified it — and re-attacked it on a
vector the author had not used — and a test now pins the ceiling, proven by the mutation the
auditor showed had previously stayed green. Full history in `docs/DECISIONS.md`.

## F1–F7 acceptance gates — the definition of RELEASE_CANDIDATE

`docs/ACCEPTANCE_GATES.md:3`: the terminal state is reached **only when F1–F7 all PASS**. Stated
here as a table because an evidence document that never mentions the gates cannot be checked
against them.

> **CANDIDATE STATUS — `bf0d88a` is SUPERSEDED (2026-08-19).** It was pushed and final CI was run.
> Two mandatory matrix jobs stalled — 55.7 and 56.1 minutes against a legitimate 5.7-12.9 — and a
> re-run at the same SHA stalled another 37.9, all producing no output and no failure.
>
> **The mechanism could not be established from the retained logs, and the first diagnosis was
> wrong.** The stall was initially attributed to `apt-get install` blocking on its confirmation
> prompt for want of `-y`. The logs refute that: the stall is inside `apt update`, `apt-get
> install` **never executed**, and the logs contain neither a confirmation prompt ("Do you want to
> continue?") nor any lock-contention message ("Waiting for cache lock"). The last output is at
> 08:23:15 and the job was killed at 08:59:46 — thirty-six minutes of silence with no diagnostic.
> The mirror retries that looked suspicious appear in the passing jobs too — and note `Ign:` means
> apt gave up on an index and moved on, whereas a hang is the case where it did *not*, so that
> datum does not exclude a mirror stall. Recorded as **INCONCLUSIVE**: the two hypothesised
> mechanisms are excluded by the absence of their signatures, but **the candidate field is not
> exhausted** — at least one other class, a network or mirror stall inside `apt update`, remains
> unexcluded and is consistent with the evidence. No positive cause is claimed.
>
> The workflow now enforces **both** independent guards: deterministically non-interactive apt
> installation, which closes a real latent hang that did *not* cause this incident; and explicit
> **`timeout-minutes` on every job**, which bounds this stall and any future one of unknown cause.
> `ci.yml` is a tracked release file, so **no CI evidence from `bf0d88a` is reusable as final
> release evidence**, including the MinIO, L3 and F2 jobs that were green there — their job
> definitions changed too. F1-F6 are re-established on the new candidate below; F7 is not run until
> they are. The F3 local closure carries unchanged: no F3 subject was touched, and the F3 evidence
> never depended on CI.
>
> **CI evidence in this table was executed at `203d67e`** (run 32254745359), and every mandatory
> job is cited at its own at-candidate job id below. **CI executed at `203d67e`; this terminal
> docs-only SHA changes no product file, no test, no workflow or helper, no migration/patch
> implementation, and no schema or fixture.**
>
> **One qualification, because the blanket form of that sentence would be false here.**
> `"documentation-only"` does **not** imply `"cannot affect F1-F6"` in this repository:
> `cloud_file_storage/tests/test_documentation_references.py:34` scans
> `docs/DECISIONS.md` and `docs/PROGRESS.md` and fails if a cited `test_*` name does not resolve,
> and that suite runs inside all three ref jobs plus MinIO and L3. Both files are in the span since `203d67e` that the inheritance covers,
> so this delta *could* have turned F1/F2/F5 red. It does not: the guard was executed at this
> tree and passes (`test_documentation_references`, 6 tests OK). Separately
> `.github/helper/check_mission_matrix.sh:21` parses `docs/ACCEPTANCE_GATES.md`, which this delta
> does not touch. The inheritance is sound **because those inputs were checked**, not because a
> docs delta is inert — and the next auditor should re-check rather than infer.
>
> That inheritance is permitted by a **narrow owner ruling covering the terminal documentation
> phase only**, granted after the `203d67e` release audit showed the reconciliation could not
> otherwise terminate: a final documentation/evidence-correction commit may inherit the immediately
> preceding candidate's F1-F6 evidence when the delta is documentation-only and mechanically proven
> to change no product file, no test, no workflow or helper, no migration/patch implementation, and
> no configuration/schema/fixture input used by F1-F6 — with the parent SHA named and an
> independent reviewer confirming the delta cannot affect any F1-F6 subject.
>
> **This is not general carry-forward and must not be read as one.** The broad argument — that
> byte-identical inputs make a rerun unnecessary — was put to the owner earlier and **declined**
> (`DECISIONS.md`), and it stays declined. Any non-documentation change voids this exception and
> requires normal qualification.


| Gate | Its actual subject (per `ACCEPTANCE_GATES.md`) | Status | Basis |
|---|---|---|---|
| F1 | **Contract gate** — C1–C19 on **every supported frappe ref**, junit-asserted per ref | **PASS** | **Executed at `203d67e`** (run 32254745359). Earlier citations at `2df3bbb` and `71deb15` are superseded: this row names only jobs whose `head_sha` is the candidate. `contract completeness OK: C1..C19 all executed (19/19 accounted for)` on **all three** refs — v15.16.0 (job 96073731537, 1557 tests OK skipped=9), v15.93.0 (96073731861, 1557 OK skipped=9), version-15 (96073731838, 1557 OK skipped=9). **The floor ref is green for the first time in the delivery**, after six attempts each removing a real version-compatibility defect class. Gate mechanism re-verified by the release auditor at source: `check_contract_completeness.sh` parses junit XML (not grep), counts a skip as a failure, fails on an empty match, and is wired mandatorily into all three ref jobs with no `continue-on-error`. Contract suite carries 58 tests across C1–C19 with **zero skip decorators**. |
| F2 | **Mission test matrix** — 43 named T-tests at their layer, plus A25–A36 and thumbnail availability | **PASS** | `cloud_file_storage/mission_matrix.py` maps all 43 scenarios to exact test ids; `.github/helper/check_mission_matrix.sh` resolves them against junit XML across both layers. **Executed at `203d67e`** (gate job 96076444199, resolving junit from both layers — the L2/MinIO job 96073731922 and the L3 job 96073731835, which `ci.yml` declares as its `needs`): `43/43 scenarios + 12 A-series + thumbnail availability executed across 2 report(s), 0 missing, 0 skipped, 0 failed.** Nine failure modes proven to fire (absent, unexecuted, skipped, partially-skipped, string-duplicate, class/method-aliased, count-shrunk, missing A-series, clean baseline). Six scenarios had **no test at any layer** when the artefact was built and now have real ones; two independent review rounds found and closed a further eleven items. See `docs/evidence/f2-mission-matrix.md` and `docs/evidence/f2-independent-review.md`. |
| F3 | 100k migration rehearsal | **PASS** | **All nine criteria PASS** — reconciled row by row in `docs/evidence/f3-nine-criteria.md`, which states for each whether it is fresh or carried, its evidence SHA, and why the carry holds. **Fresh at the candidate:** *A1* (release scale, 100,020 objects — deltas 0.0833/0.1001/0.1332s against the owner's frozen `<= 0.250s` and `<= 2.0x`, 0 probe failures, 0 migration jobs on ERP queues); the *bandwidth throttle* (**249,925.8 B/s against a 250,000 ceiling = 99.97%**, 30,172,702 real transferred bytes, limiter asleep 115.84s of 120.73s elapsed (**sleep fraction 0.9595**, against a 0.25 floor — the ceiling bounded the run, not the corpus), 32.8x faster unthrottled, gated by the committed `throttle_preflight` stage, `THROTTLE_CRITERION=PASS` emitted by the harness on a mechanically preflighted clean site); *dedicated-queue execution* (0 migration jobs on ERP queues at release scale, plus a red control — with no `cloud_migration` queue the engine refuses to start rather than falling back); and the *delete-before-verify* lint (AST: zero deletion calls in UPLOAD/VERIFY, all deletions confined to `cleanup.py`). **Carried with reasons:** bounded resources, RQ job count 1.60/batch, crash/restart recovery, pause/resume, and convergence (**99,862 / 99,901 = 0.99961** on a denominator fixed in advance — the post-triage 1.0 is reported but explicitly not relied on, since triage moves objects into `Skipped` and `Skipped` leaves the denominator). |
| F4 | Migration capacity preflight | **PASS** | Measured and projected (below), with the byte distinction F7 requires. **Carry-forward basis, corrected — the earlier statement did not reproduce.** This row previously said `git diff d9ea898..<candidate>` over the five measured DocType JSONs is **empty**. It is not: `cloud_storage_audit_log.json` changed in `3d62336` (2026-08-16). Re-derived rather than re-asserted: the change adds exactly two **Select option values** (`backup_lifecycle_applied`, `backup_presign_issued`) to an existing field — no new field, no column-width change, no index change, leading `\n` preserved. **Why that leaves row width untouched, stated so it can be re-derived rather than trusted:** Frappe maps `Select` to **`varchar(140)`**, not to a MySQL `ENUM` (`frappe/database/mariadb/database.py:196`; the live column is `action varchar(140)`). An options change is therefore pure DocField metadata — no DDL, no `ALTER`, no column-width change, whatever values are added. **The one channel that could touch bytes/row is stored-value length**, since InnoDB charges a `varchar` its actual string length: the calibration was taken on rows carrying `file_link` (9 chars) and `local_quarantine` (16), while the new values are 24 and 21, so a row carrying one costs ~8-15 bytes more in that column — **≤2% of one table's row, for actions that are per-backup-operation rather than per-file** — and therefore outside the per-object scaling the projection is built on. F4 scales the audit log at **1.998 rows per object**; an action that is not per-file cannot participate in that scaling at all, so those rows are an **additive constant, not a multiplier**. A thousand backup operations is ~24 KB against a 10.61 GB projection, and a million objects does not make it more. This is a property of the projection's scaling model, not an assumption about how a given site behaves. Against `DISK_SAFETY_FACTOR = 2.0` on a 10.61 GB projection that is far inside the margin. The 770.56 B/row calibration therefore stands; but the *verification as previously written was false*, on the one gate whose purpose is to refuse a start on understated numbers. The other four JSONs are byte-identical. The refusal path is pinned by A29 (`test_migration_reconcile.TestCapacityPreflight`) — **19 tests, all PASS, 0 skipped, executed at `203d67e`**. |
| F5 | Ecosystem gate — L3 CI job green **and mandatory** | **PASS** | **Executed at `203d67e`:** the L3 CI job ran green (job 96073731835, 1590 tests OK skipped=5, `ecosystem completeness OK: 9 exit-criterion tests executed, 0 skipped`), including `Assert the ecosystem apps are actually installed` — which printed the real co-installed versions, erpnext 15.93.0 / hrms 15.49.2 / india_compliance 15.18.1 / cloud_file_storage 1.0.0 — — so the co-installation is proven, not assumed — and `Ecosystem completeness (T-RIV / T-ITEMIMG / T-IC-LIVE)`. Release auditor verified at source that `check_ecosystem_completeness.sh`'s required set is exactly F5's named scenarios: Repost Item Valuation, Item image, GST Return Log create/read/update/download, e-Waybill `save_file`, and upload-gstr-after-cleanup. The job carries no `continue-on-error`, which is what "mandatory" required. Bench evidence (1379×2 on the real co-installation) still stands beneath it. |
| F6 | Safety lint gates | **PASS** | **Re-derived at `2df3bbb` by the release auditor in round 4, not carried forward.** `test_migration_safety_lint` (19 tests) and `test_p1_package_contract` (39 tests) both executed green under the bench interpreter, covering all five F6 bullets: no deletion call in UPLOAD/VERIFY (AST-based, self-tested against deliberately-bad synthetic modules), no `ACL` key in any S3 call, archive storage classes rejected, every Select field with an explicit default or leading newline, and raw SQL against tabFile/CSO confined to the three sanctioned sites. An independent grep confirms no `ACL` key anywhere in product code. |
| F7 | Release — incl. **release-manager audit PASS** and the byte distinction | **PENDING EXTERNAL ATTESTATION** | Its satisfied parts: integration-merge retest green, real `bench migrate` legacy-site test green, security audit **PASS** (no Critical, no High; **three** Lows carried — `L-2`, `L-9`, `L-12` residue), supported-versions matrix published (`docs/supported-versions.md`, floor 15.16.0 — now actually exercised green in CI), and this document with the byte distinction and capacity numbers below. **Pending external attestation.** F1–F6 are all PASS. F7 names the **release-manager audit**, and its verdict is recorded **outside this tree**, keyed to the SHA it approves — because a commit that wrote its own F7 PASS would be a SHA no audit had examined. **Release-audit history, by candidate SHA — every round, not a selection:** `2df3bbb` NO-GO (round 4, blocked on A1 awaiting an owner threshold); `71deb150cf48271bd654b366606b69ba745d789d` NO-GO (release documentation); `203d67e7c44d136d36075ff476138f01178b0506` NO_GO (five EVIDENCE_DEFECTs); `cfc5c9768b2c875fedc3d21f3f7d8843502cc9d3` NO_GO (two EVIDENCE_DEFECTs, both closed by the commit that supersedes it). **None of these is the current verdict** — each was superseded by the candidate that followed it, and the verdict for the present terminal SHA is pending its own external attestation. In every round from `71deb15` onward the auditor found **no gate failing on its merits**; each blocked solely on release documentation. **CodeQL** also remains `BLOCKED_EXTERNAL_ADMIN_CONFIGURATION` and is not reported as PASS. |

**F1–F6 are PASS; F7 is NOT MET, so this tree is not a RELEASE_CANDIDATE under its own
definition.** F1 and F5's CI halves ran green first at `2df3bbb` and again, at the candidate, at `203d67e` (run 32254745359) — which is the evidence this document now cites.
F7 is unmet because the release-manager audit has not been run against this terminal SHA. Every
prior round is enumerated in the F7 row above, and each was superseded by the candidate that
followed it — none is the operative reason now. Naming round 4 as that reason was the same
summary-versus-table drift this document has recorded three times already.

This paragraph has twice contradicted the table above it — first reading "F1 NOT MET, F2 NOT
ASSESSED, F3 PARTIAL", then "F1 NOT MET ... F5 split" after CI had already closed F1 and F5. Both
were stale prose left behind by table edits; the gate statuses in the table are authoritative.
F2 was closed by the mission-matrix work (`aff7b37`, `3b9272e`, `5ebe22a`) and F3 by the
disk-I/O measurement (`f399fd4`); the table was updated and the summary beneath it was not.
Corrected in place and recorded here rather than silently, because a summary that disagrees with
the table it summarises is the exact drift this document warns about — and it is the **third**
time a defect has been found in the accounting added to prevent miscounting.

An earlier version of this table said "two gates are not met". That undercount had two causes,
both found by the release auditor: it **crossed the F1 and F2 labels**, filing the contract gate
under F2 and thereby never assessing F2's actual subject at all; and it marked **F7 PASS on the
byte distinction alone**, dropping the clause in F7's own text that requires the release-manager
audit to PASS. Recorded rather than silently corrected, because this document's stated rule is
that unmet items are stated as unmet — and the miscount was the same failure mode, inside the
table added to prevent it.

### F3 — the four P5 criteria, and A1

Four criteria were unmet at P5. **All four are now met**, and the two that had been declared
blocked turned out to be instrument faults rather than product or environment limits. The
fifth — **A1**, the ERP-queue criterion — blocked four consecutive audit rounds for want of a
numeric definition; the owner ruled it on 2026-08-18 and it is now measured and **PASS**. See
"What remains genuinely unmet" item 1 for the A1 figures and their caveats, and
`docs/evidence/f3-a1-erp-queue-latency.md` for the per-pair artifact.

1. **End-to-end bandwidth throttle — MET.** The harness had no way to *seed* a bandwidth-bound
   corpus (payload band was a module constant at 120-400 B, where a limit is unobservable).
   With `--min-bytes/--max-bytes`, on two comparable batches through the same `run_upload_batch`
   a worker calls:

   ```
   throttled   2 Mbps 100 objects 30,172,702 B 120.73s ->    249,925.8 B/s  (ceiling 250,000)
                                  slept 115.84s of 120.73s elapsed  (fraction 0.9595)
   unthrottled  none  100 objects 30,253,262 B   3.69s ->  8,208,384.3 B/s
   ```
   **99.97% of the configured ceiling; 32.8x faster unthrottled; the limiter slept for 96% of the
   throttled run** against a 25% floor — which is what distinguishes a ceiling that bound the run
   from a corpus that finished under it. Gated by the committed `throttle_preflight` stage (six
   checks, `PREFLIGHT=PASS`). Artifacts: `docs/evidence/f3-throttle-measurement.json`,
   `f3-throttle-preflight.json`, and `f3-throttle-preflight-refusal.json` (the same gate refusing
   a dirty environment).

   **Superseded figures, listed in full** — the complete disposition, with inputs, lives in
   `docs/evidence/f3-nine-criteria.md`; none is retracted as wrong on rate, and every displacement
   was forced by a change to the code that gates or judges the measurement:

   | figure | why it no longer stands |
   |---|---|
   | `249,315.7 B/s` on 60 objects (`CFS-CAMP-0001`) | the original P5-era run, superseded by the release-scale re-measurement |
   | `249,928.8 B/s` on 34,194,160 B | **withdrawn** — the harness never SELECTed `objects_failed`, and it carried no engagement witness |
   | `249,927.8 B/s` on 29,744,458 B | predates the engagement floor and the committed preflight |
   | `249,937.5 B/s` on 29,472,439 B | predates three corrections to `throttle_preflight` |
   | `249,906.8 B/s` on 29,977,246 B | predates the starting-state fix |
   | `249,825.5 B/s` on 30,927,079 B | predates the precondition being raised to two Pending UPLOAD batches |
   | `249,818.7 B/s` on 30,258,880 B | predates `pending_batches` gaining its `phase: UPLOAD` filter |

   **The repetition is the evidence, not a caveat on it.** Across the seven runs at this ceiling,
   seven corpora and a host whose speed measurably changed, the throttled rate spans **0.048%**
   (249,818.7-249,937.5 B/s) while the unthrottled comparison spans **46%** (5.63-8.21 MB/s): one
   number is pinned by the limiter, the other by the machine. Two things make that checkable
   rather than asserted: **a 0.048% spread leaves nothing to select between** — whichever run had
   shipped, the criterion reads the same to four significant figures — and **the schedule was not
   the author's to choose**, since every regeneration was forced by a review finding and the
   seventh ran against the reviewer's explicit advice that it was unnecessary. Both runs the
   harness rejected were unfavourable and both are recorded.

   The 99.97 -> 99.93 -> 99.97 utilisation excursion is a batch-finalisation tail moving between
   29ms and 88ms with host load, reproducing to four decimal places as `1 - tail/elapsed`; the
   sleeping portion tracks the ceiling to within milliseconds throughout. **The shipped run
   confirms that model rather than restating it:** the host recovered, the tail fell back to 36ms
   and utilisation returned to 99.97% - a prediction the tail account makes and a
   drifting-limiter account does not.

2. **Bounded RQ job count — MET.** Two real RQ workers, every executed job recorded with its
   queue: **8 jobs across 5 batches = 1.60 per batch** against a design bound of ~2, **0
   redelivered, 0 on ERP queues**. That run converged 270/270.

3. **Bounded disk I/O vs a recorded baseline — MET.** P5 called this an ENVIRONMENT_BLOCKER; it
   was four instrument defects, all fixed (`docs/evidence/f3-disk-io-scale.md`). An idle baseline
   now exists, and two comparable loaded points were measured:

   ```
   metric                  IDLE     4188      6278
   device write IOPS /s    8.14    81.9     273.9
   device write MB/s       0.14     1.06      3.37
   await ms                4.03     1.73      2.05
   utilisation %           0.93     8.9      30.3
   app RSS peak MB          0.0   (excluded) 577.4
   redis peak MB            1.67     1.85      2.41
   ```

   **Growth is sublinear, not proportional:** per-object device write **falls** from 296.7 to
   174.1 KB/object as the corpus grows. Within the 6278 run, application RSS is **flat**
   (577 -> 574 MB) while the whole corpus is consumed, write throughput **decreases**
   (4.28 -> 2.97 MB/s), and `await` **improves** under load (4.03 ms idle -> 2.05 ms) — the
   signature of an unsaturated device. Utilisation peaks at 30%.

4. **Crash/restart recovery at campaign scale — MET** (`docs/evidence/f3-crash-recovery.md`).
   Redis flush: 4 keys destroyed with 2 batches in flight, progress resumed in **20.1 s**,
   converged 1200/1200. Worker restart: both workers killed at 1728 objects, resumed in
   **10.1 s**, converged 2992/2992.

**A1 — the item that was an OWNER DECISION — is now ruled and measured.** The reading quoted
here historically (0.257 s idle -> 0.410 s loaded, +60%) carried **no verdict** because the
approved documents stated no tolerance for "unaffected". The owner ruled the tolerance on
2026-08-18; A1 is measured against it and **passes**. See "What remains genuinely unmet" item 1
and `docs/evidence/f3-a1-erp-queue-latency.md`. During the 6278 run, `cfs-fault.local` had
**0 jobs** on every ERP queue; the `default` queue's constant depth of 124 belongs entirely to
another site on the shared bench.

### F4 — the capacity numbers, and the byte distinction F7 requires

The gate requires this document to distinguish **cloud-managed permanent attachment bytes** from
**bounded temporary/local operational bytes**. They are different populations with different
lifecycles, and conflating them is how a capacity gate passes a start it should refuse:

- **Permanent, cloud-managed:** the attachment objects themselves, in the bucket. Growth is the
  business's; this app does not bound it. Local copies are removed only after independent
  remote verification and re-verification at deletion time (invariant 1).
- **Bounded, local/operational:** the MariaDB rows this app adds, the materialisation cache, and
  the quarantine trash tree. These are what the preflight measures and what a capacity refusal
  protects.

Measured post-CLEANUP on the 100k corpus, projected to ~1.2M rows:

```
Migration Object      3,388 B/row        Cloud Storage Object  2,223 B/row
File Ref              1,694 B/row        Audit Log               771 B/row @ 1.998 rows/object
Conflict              1,480 B/row
projection @ 1.2M  ->  10.61 GB total, of which 5.01 GB is index
free-space required at DISK_SAFETY_FACTOR 2.0  ->  21.23 GB
peak RSS 57 MB · Redis peak 1.60 MB
```

Against the design's 1.5–2.5 GB planning figure. **The first measurement of this was itself
understated by 2.1×** — it omitted `Cloud Storage Object` entirely and was taken before CLEANUP
grew the two tables it grows. Understating is the direction in which a capacity gate passes a
start it should refuse, which is why the corrected figure is stated with its own correction.

## Everything this release does NOT claim

`docs/release-report.md` §8 carries eight non-claims. All eight are repeated here, because an
evidence document that carries two of eight has chosen which to surface:

1. ~~**The three-ref CI matrix has not run.** Only `v15.93.0` is exercised.~~ **No longer true, and corrected here rather than deleted:** the matrix ran and at the release candidate `203d67e` all three refs are green — v15.16.0 (job 96073731537), v15.93.0 (96073731861), version-15 (96073731838), each 1557 tests OK with `C1..C19 all executed (19/19)`. `docs/release-report.md` §8 and `docs/supported-versions.md` state the same, at the same SHA and the same job ids. This item was carried verbatim after it stopped being accurate and survived into two release audits.
2. **No two-person rule.** Cleanup is two audited actions, but nothing forces two distinct actors.
3. **Postgres is unsupported**, despite branches in the code.
4. **1.2M rows were not migrated.** The 100k rehearsal is the evidence; production scale is projected, not run.
5. **`storage_health()` is not optimised** — ≈0.83s over 600k File rows, measured.
6. **No penetration test.** Controls are asserted by tests and reviewed, not attacked by a third party.
7. **Invariant 3's `content_hash` clause is NOT met for adopted fork rows** — a stated exception
   to a hard invariant. Fork-era rows on an adopted site carry no MD5; the invariant holds for
   every row this app writes, not for every row on every site.
8. **P8 was reviewed once and FAILED**, and shipped on a second submission. The round-1 HIGH —
   the bootstrap's entire write surface unreached by any test — is recorded rather than absorbed.

### Carried Lows — three, derived from the list rather than counted to a number

**Three** security Lows are carried, none blocking, all recorded in `docs/security/` and
DECISIONS:

1. **L-2** — audit-log actor granularity.
2. **L-9** — credential pairing on a secret-without-key site; narrowed and warned rather than
   switched, on the implementer's argument that "warn, then break the site" is worse than either
   alternative.
3. **L-12 residue** — falsy-value coercion at one remaining boundary.

An earlier version of this section claimed **seven**, reached by padding the list with items
that are not carried security Lows: **L-10 and L-11 were fixed** (described as fixed in the same
sentence that counted them), the **Error Log growth is a ruled Medium**, and
**browser verification is a documentation gap**. The release auditor's phrasing is the accurate
diagnosis and is kept: *the list was assembled to reach a number rather than the number derived
from the list.*

Separately and not Lows: the Error Log growth (24 rows/run on a pristine site — **Medium**,
non-blocking, because all five Error Log assertions in the suite are deltas rather than absolute
counts), and the browser-verification gap under "Stated gaps".

## What remains genuinely unmet

1. **F3's ERP-queue criterion (A1) — RESOLVED, now PASS.** The owner ruled the tolerance on
   2026-08-18, operationally defining "unaffected" as no material probe-job latency
   degradation: per queue, >=30 baseline and >=30 loaded probes at the same SHA/site/topology,
   p95, requiring **both** `loaded_p95 - baseline_p95 <= 250 ms` **and**
   `loaded_p95 / baseline_p95 <= 2.0`, plus zero probe failures, no sustained queue-depth
   accumulation, zero migration jobs on ERP queues, and depths recovering. One queue failing
   any condition fails A1. The threshold was frozen before measurement and **was never
   adjusted**.

   **SUPERSEDED — retained for history, not the measurement of record.** The figures below are
   the 1,020-object run at `1b2a94c`. A1 was later re-measured at **release scale (100,020
   objects, `CFS-CAMP-0008`)** after an independent reviewer showed that measuring on a
   1,020-object campaign biased toward passing a gate scoped to 100,000 files. **The measurement
   of record is the release-scale one** — deltas 0.0833 / 0.1001 / 0.1332 s, ratios <= 1.505x,
   0 probe failures, 0 migration jobs on ERP queues — recorded in
   `docs/evidence/f3-a1-erp-queue-latency.md` and cited in the F3 row above. The thin-headroom
   caveat below ("82% of the bound") belongs to the superseded run; the release-scale run sits at
   53% of the bound.

   Measured at `1b2a94c` (clean protocol — quiesced baseline, single run stage,
   one accounting worker per queue):

   | queue | baseline p95 | loaded p95 | delta | ratio | pass |
   |---|---|---|---|---|---|
   | short | 0.2556s | 0.4577s | 0.2021s | 1.791x | yes |
   | default | 0.3048s | 0.5100s | 0.2052s | 1.673x | yes |
   | long | 0.4082s | 0.5188s | 0.1106s | 1.271x | yes |

   `VALID_MEASUREMENT` (load witnessed: 323 objects progressed in-window), PROBE_FAILURES 0,
   MIGRATION_JOBS_ON_ERP_QUEUES 0, ERP depths recovered to 0. Four clean-protocol pairs were
   run and **all four pass**; worst delta across them 0.2052s.

   **Reported honestly rather than as a comfortable pass:** one earlier valid measurement,
   taken in an uncontrolled environment (two consumers per ERP queue, one unlogged, plus
   leftover pollers), **exceeded** the bound at `default` delta 0.2613s. It did not reproduce
   in five subsequent measurements, and it is recorded in `DECISIONS.md` rather than dropped.
   Headroom is thin — 0.2052s is 82% of the bound — and the effect scales with contention, so
   a production topology busier than this scratch bench could plausibly exceed it. That is a
   deployment-sizing consideration for the owner, not a gate failure. **F3_A1 = PASS.**
2. **Coverage — non-blocking quality debt, recorded as failing.** The combined gate ran to a
   verdict for the first time at `2df3bbb` and **failed: 76% combined against a 90% floor**
   (job 95747420442). Product code alone is **86%** (6584 statements, 905 missed); the rest of
   the gap is the test tree, including two harnesses that are 0% by design because
   `bench run-tests` never drives them (`tests/rehearsal.py`, `tests/playwright/smoke.py`).
   The owner ruled this outside F1–F7 on 2026-08-18 — verified by the release auditor against
   the frozen text, which defines **no coverage percentage as a release condition** (the only
   `%` in the whole document is F3's convergence ≥99.9%). Recorded as:

   ```
   COVERAGE_RELEASE_GATE = NOT_APPLICABLE_TO_F1_F7
   PRODUCT_COVERAGE      ~86%
   TARGET                90%   (unchanged — not lowered)
   STATUS                NON_BLOCKING_QUALITY_DEBT
   ```

   **This is not a coverage PASS and is not recorded as one.** The floor was not lowered, no
   omit rule hides product code, and the tooling is untouched and operational. The ~905
   uncovered product statements are post-release work prioritised by risk: security boundaries,
   deletion/GC, migration recovery, private serving, permission enforcement, backup/restore,
   compatibility paths.
3. **CodeQL — `BLOCKED_EXTERNAL_ADMIN_CONFIGURATION`, never PASS.** Code scanning is an
   org-admin repository setting the project owner cannot enable, so the analysis completes and
   cannot upload. Re-verified unweakened at `cfc5c97`: no `upload: false`, the `analyze` job
   intact, no CodeQL-only `continue-on-error`, `security-events: write` retained and
   `analyze@v3` unmodified. Greening it by disabling the upload would destroy its purpose.
4. **No browser-level verification of rendered output**, with the mitigation and its limits
   under "Stated gaps".

Items 3 and 4 are limits of what this environment could prove, and item 2 is a ruled-scope
decision. **Item 1 is now closed**: the owner ruled A1's tolerance on 2026-08-18, it was measured
against that frozen threshold at release scale, and **A1 is RESOLVED and PASS**. It is retained
here as history rather than deleted, because it blocked four consecutive audit rounds and the
record of how it closed is worth more than a tidy list.
