# F2 — independent review, and what it found

An independent reviewer (read-only, source inspection only, no test runs) reviewed the F2
commit `aff7b37`, which the orchestrator wrote and nobody had reviewed.

**Verdict: FAIL — five blocking items.** All five were confirmed from source and fixed. The
review is the reason F2's 43/43 claim is now true rather than merely counted.

## What it found, and why each mattered

**B1 / B5 — two mappings pointed at tests the repo labels as OTHER scenarios.** T-OUTAGE was
mapped to `TestPrivateServingResolutionChain`, whose own docstring reads "A35 — serving resolves
through the A1 chain": no endpoint refusal, no mode-specific failure, no read fallback. T-BACKUP
was mapped to `TestBackupFreshness`, docstring "A28 — the dump uploaded is the dump this run
produced". Both A-items are separately required by F2, so each mapping counted one test twice
under two different clauses. **The no-sharing rule had forced a filler mapping rather than an
admitted overlap — the inversion of what it was written to prevent.**

**B2 / B3 — two scenarios showed green with no covering test at any layer.**

- **T-API**: defined as `/api/method/upload_file` (guest + session) **plus** legacy
  `file_manager.save_file`. Only the second half was mapped. A repo-wide grep confirmed
  **nothing in the suite called frappe's upload endpoint.**
- **T-UNI**: mapped to a presign test whose only unicode contact was
  `assertIn("Pflanzenr", disposition)` — **satisfied by the ASCII prefix alone.** Mangling
  `ückgabe` left it green, and it exercised a mocked client rather than a round trip.

This is the exact condition that made F2 NOT MET for the original six. Both now have real tests:
the endpoint driven through a genuine multipart request including the Guest refusal branch, and
a filename carrying an umlaut, a CJK character and an emoji, asserted on the whole non-ASCII
portion rather than a prefix.

**B4 — "F2 PASS" was declared against one of the gate's three clauses.** F2 requires the 43
scenarios **plus** the A25–A36 regression tests **plus** thumbnail availability. The manifest
held only the 43 and the doc cross-check harvests only `T-*` tokens, so the A-series clause —
lowercase prose — was invisible to both. **As shipped, deleting the tombstone re-upload test left
the gate green.** All three clauses are now enforced.

## The fifth failure mode five controls missed

**F3 — the scenario count was pinned nowhere.** The gate computed `named - set(matrix)` and
printed `len(matrix)/len(matrix)`. Deleting a scenario from **both** the gate document and the
manifest left them agreeing with each other: `42/42 ... OK`, rc=0. **A gate that shrinks and
reports success.**

Every control run had mutated *one side* of a comparison, so none could see it. The count is now
asserted against a constant that must be changed deliberately.

**F4 — class/method aliasing defeated the no-sharing check.** The duplicate test compared target
*strings* while resolution expands a class target to every method in it, so mapping one scenario
to `Foo` and another to `Foo.test_bar` produced two distinct strings satisfied by overlapping
tests. Most entries are class-level, so this is how a future scenario would naturally collide.

## Controls — every failure mode proven to fire, on clean copies

| Broken condition | Result |
|---|---|
| scenario absent from manifest | rc=1 |
| mapped test never executed | rc=1 |
| mapped test skipped | rc=1 |
| two scenarios share a test (string) | rc=1 |
| **scenario deleted from BOTH files** | **rc=1** — "count changed: manifest has 42 ... both must be 43" |
| **class/method aliasing** | **rc=1** — "scenarios overlap on class ..." |
| **required A-series test skipped** | **rc=1** — "A25: required test did not execute green" |
| clean baseline | rc=0 — 43/43 + 8 A-series + thumbnail |

**One control was run twice.** The first aliasing attempt failed on the *previous* mutation
because the reset had not restored the copy — the same "control failed for the wrong reason"
error this project has hit before. Re-run on a fresh copy, where it failed for its own reason.

## Corrections to the prose, made because the reviewer was right

- "Every test drives the real frappe call, never a re-implementation" was **not true of all six**:
  T-EMAIL-IN reproduces the document shape from `receive.py` rather than calling
  `save_attachments_in_doc`, and T-PREPREP does its own gzip round trip rather than calling
  `PreparedReport.get_prepared_data`. The per-row table was honest; the summary sentence was not.
- T-PREPREP runs `LOCAL_ONLY` and is the only one of the six that does not assert a CSO exists,
  so the claim that "breaking the remote read reds all six" cannot mean what it says for that
  test. Recorded rather than restated.

## Standing findings, carried not fixed

F1/F2/F8/F9/F10 (partial-definition mappings, layer not recorded or enforced, minor gate dead
code) are recorded in the review and remain open. They are coverage-depth findings, not
false-green ones: each names a test that genuinely exercises part of its scenario.

---

# Round 2 — PASS WITH FINDINGS, and the three conditions

The reviewer re-read `3b9272e` and confirmed every round-1 blocking item closed, with the
distinction stated precisely: round 1's artefact asserted things that were **false about the
tree**; round 2's were true about the tree but overstated its **durability**. Three conditions,
all now fixed.

**N1 — "all three clauses enforced" was 8 of 12.** `A_SERIES` held eight amendments; F2 names
twelve. Deleting the quarantine-collision test still left the gate green. Added A30 (breaker
under injected outage), A32 (quarantine collision), A33 (deterministic-name idempotency) and
A34 (floor-ref private serving). The reviewer's warning is worth keeping verbatim: shipping the
claim while clause 2 was two-thirds *"reverts to FAIL on identical grounds to round 1's B4 — and
your own audit history is that every finding lived in prose asserting a count."*

**N2 — the gate's own list contained the shape its new check rejects.** `A_SERIES["A36"]` was a
method inside `TestIndiaComplianceLive`, which was T-IC-LIVE's class-level target: two entries
satisfied by overlapping executed tests, exactly what the aliasing check forbids. It did not fire
because the check iterated the scenario map only. Both checks now span
`matrix | A_SERIES | {thumbnail}`, and T-IC-LIVE takes its own method.

**N3 — the eighth hole, live in a test written an hour earlier.** The skip check fired only when
*every* method in a target was skipped. 34 of 43 targets are class-level, so a class could lose
all but one method silently. It was already happening: the new T-API guest test called
`skipTest` when the site allowed guest uploads, and its sibling kept the class non-empty. **F1
demands "zero silent skips"; F2's gate permitted them.** Now any skip inside a mapped target
fails, and the guest test drives *both* branches with the setting flipped and restored — which
also covers what a refusal alone cannot, that an allowed guest upload actually stores.

**N4** — the count pin was blind for five hard-coded ids; expansion is now table-driven off the
document text. **N5** — the weak `assertIn("Pflanzenr", disposition)`, the assertion that started
this thread, now asserts the full percent-encoded `Pflanzenr%C3%BCckgabe.pdf`. **N6** — a
docstring claiming a serve limb the class never reached, scoped to what it asserts.

## Controls for the new checks

| Broken condition | Result |
|---|---|
| newly-required A32 quarantine-collision test skipped | rc=1 — "A32: required test did not execute green" |
| **one method of three skipped inside a class-level target** | **rc=1** — "T-PAUSE: 1 test(s) inside ... were SKIPPED, so this scenario's coverage shrank silently" |
| clean baseline | rc=0 — 43/43 + **12** A-series + thumbnail |

**The first partial-skip control was invalid and was re-run.** It targeted
`TestPublicRendererOutage`, which has exactly one test method — so skipping it exercised the
"every test skipped" branch, not the partial one, and the gate's own message said so. Re-run
against a three-method class where two stayed green. *A control that fires for the wrong reason
is not a control*, and the gate's error text is what exposed it.

## Carried, not fixed

F7 (layer neither recorded nor enforced) is the most consequential: the T-UNI fix moved that
scenario from mocked-boto3 to fake-store — both L1 — where its definition names L2. Bounded by
the MinIO gate's 19 real-S3 tests and F5, so not blocking, but "green at its layer" is assumed
rather than audited. F2 (T-DATAIMP is a two-line predicate), F8 (three half-mapped scenarios),
F9, F10 remain coverage-depth findings.

**Neither review round could diff a commit** — the reviewer had no Bash. "No runtime behaviour
changed" for `aff7b37` and `3b9272e` is therefore confirmed by reading the files' current state,
not by inspecting the diffs, and that limit is the reviewer's own disclosure.
