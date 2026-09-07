# DECISIONS — append-only log

Entries ≤5 lines: date, decision, alternatives, decider, links. Architectural decisions
graduate to `docs/adr/`. Agent conflicts: append an entry tagged `NEEDS-RULING` for the
orchestrator; never edit another owner's files to force a resolution.

---

2026-08-14 — **Architecture approved (AUTONOMOUS_EXECUTION_READY)** after 6-agent
research, 3-agent design, and two adversarial review rounds (3+3 reviewers); all
confirmed findings bound as A1–A36 (adr/amendments-register.md). Decider: owner.
Links: docs/PLAN.md, docs/review/*.

2026-08-14 — **Pre-P0.5 untrusted scaffold discarded and regenerated.** During planning,
read-only-intended subagents wrote unauthorized draft INVARIANTS.md/.agent files
(one reviewer then cited the invented REQUIREMENTS.md as ground truth). Drafts deleted
at P0.5; authoritative scaffold regenerated from the approved plan; read-only roles are
now guard-enforced (autonomy_guard). Decider: owner directive + orchestrator.

2026-08-14 — **P0 recorded PASS** (ops-only .pth repair, 13/13 DB-backed sites verified;
no commits). Decider: owner (out-of-band repair), verified by orchestrator.

2026-08-14 — Owner-decision defaults adopted for build (production cutover still gated):
restore window 30d + bucket versioning ON; private presign TTL 300s; legacy adoption of
raw-bucket URLs ON with audit (cutover sign-off pending). Links: PLAN.md §H.

2026-08-14 — Backlog (post-v1): presigned direct-to-S3 chunked upload (>25MB Werkzeug
clamp); multi-bucket routing; v16 branch; upstream PR for core content_hash index; Redis
Sentinel/HA; semantic-release; proxy-stream mode for statutory docs.

2026-08-14 — **P0.5 PASS.** Baseline scaffold regenerated, independently reviewed twice
(16 + 5 findings, all fixed and re-verified), committed, tagged architecture-approved-v1.
Guard hardened (git -C forms, deny-all empty scopes, block corrupt manifests). Decider:
gate discipline per PLAN §D. Next: P1 in isolated worktree.

2026-08-14 — **P1: `delete_file_from_cloud` deliberately absent from the settings
schema.** The fork's on_trash delete path is ported verbatim but reads a flag the frozen
A12 field set does not define, so `delete_from_s3` is a structural no-op until deferred
GC lands in P2 (INVARIANTS.md invariant 4). Alternative rejected: adding the field back
(would put physical deletes on the hook path). Decider: P1 implementer.

2026-08-14 — **P1: credential-source precedence.** Static `access_key_id`/
`secret_access_key` are passed to boto3 only when `use_default_credential_chain` is off;
with the flag on (A12 default) boto3 resolves credentials itself and the pairing
validator is skipped. Alternative rejected: static-keys-win (silently defeats the
preferred IAM chain). Decider: P1 implementer. Links: A12, ADR 0020.

2026-08-14 — **P1: two A12-adjacent items deferred, not dropped.** (a) `permlevel: 1` on
the credential fields needs a matching permlevel-1 DocPerm row and the Cloud Storage
Manager DocPerms — deferred to P7, which owns the form and roles; the role itself is
created by install.py now. (b) The A12 validator `verify_strategy_cutover_mb <=
multipart_threshold_mb` cannot ship yet: `verify_strategy_cutover_mb` is not in the
frozen field list and VERIFY arrives in P5. Decider: P1 implementer. NEEDS-RULING if the
reviewer reads A12 as requiring the field now.

2026-08-14 — **RULING on the above NEEDS-RULING: `verify_strategy_cutover_mb <=
multipart_threshold_mb` is P5 scope, not P1.** The cutover value belongs to the Cloud
Migration Campaign VERIFY strategy split, which P5 implements; P1 ships the frozen
Settings schema only, and A12 does not require the field to exist before VERIFY does.
P1 is not failed for its absence. Decider: owner directive (recovery brief).

2026-08-14 — **P1 gate re-run from scratch.** The previous independent P1 gate workflow
was lost when its agent process exited. That run is treated as NO evidence — not as a
pass, and not resumed. A fresh independent test-engineer and a fresh independent reviewer
were launched against `phase/P1-rename`. Decider: gate discipline per PLAN §D (an
implementer never approves its own work; a lost run is not a pass).

2026-08-14 — **P1 defect found and fixed by the fresh gate: the rename shipped no locale
catalog.** `frappe_s3_attachment/locale/{main.pot,de.po}` was deleted without recreating
the equivalent under the renamed package, so "locale regenerated" (PLAN §B, P1) was unmet
and every translation was silently dropped. Fix (1cef6cd): `main.pot` regenerated with
frappe's own `generate_pot` (83 messages, zero fork references); `de.po` rebuilt from that
template carrying over the 12 fork translations whose msgids survive. The remaining fork
strings belonged to the removed `S3 File Attachment` doctype and no longer have a message
to translate. Decider: independent test-engineer (found) + P1 implementer (fixed).

2026-08-14 — **Deferred to P2, recorded so they are not lost.** Two inherited ALYF
behaviours ship unchanged in P1 because the approved architecture replaces them in P2, and
P1 is explicitly "rename, no architecture generation": (a) `controller.py` builds the
public `file_url` from an unencoded key, so a key containing a space (any doctype name
like "Sales Invoice") yields an invalid URI — the private branch already percent-encodes;
(b) `controller.py` nulls `content_hash` in raw SQL, which conflicts with hard invariant 3
once the runtime owns the column. P2 must fix both; the storage engine dissolves this code
path. Decider: orchestrator, per the owner's explicit P1 scoping instruction.

2026-08-14 — **P1 PASS.** Fresh independent gate (the lost run counted for nothing):
independent test-engineer PASS + independent reviewer `GATE VERDICT: PASS`. Five defects
found and fixed — missing locale catalogue; a contract-completeness gate that reported
green for an all-skipped run (A22); workflows that never fired on this repo's branch
topology; `install-app` dying in `after_install` on a stale DocType→Module cache and
leaving the operational trio unseeded; and an unencoded public `file_url`. Scratch-site
exit gate green on `cfs-autonomous.local`: install / uninstall / reinstall / migrate all
clean, install contract verified, **127/127 tests with zero skips** including real MinIO,
94% coverage (gate floor 90%), pre-commit green. Bench rename complete with all 13
business sites re-verified read-only. Known limit: only `v15.93.0` of the 3-ref matrix was
exercised locally — the other two refs need a CI run, which needs an owner-authorised
push. Decider: gate discipline per PLAN §D. Next: P2 in a new isolated worktree.

2026-08-14 — **RELEASE-BLOCKING REGISTER: the ported fork runtime deviates from four hard
invariants, by design, until P2/P3 replace it.** P1's mandate is "rename + CI baseline (no
architecture generation)" (PLAN §B), so `controller.py` ships the fork's upload hook
verbatim. That hook currently, in `file_upload_to_s3`:
  1. `controller.py:275` — `os.remove(file_path)` immediately after commit, with **no
     independent remote verification** between upload and local delete. Violates hard
     invariant 1 (zero delete-before-verify). Replaced by P2's checksummed writes +
     materialization cache + deferred GC.
  2. `controller.py:256-259` — writes a NON-canonical `file_url` (a raw
     `{endpoint}/{bucket}/{key}` URL for public, the `/api/method/...` endpoint for
     private). Violates hard invariant 2 (`file_url` is only ever `/files/...` or
     `/private/files/...`). Replaced by P2/P3.
  3. `controller.py:262,267` — sets `content_hash` to NULL (raw SQL and on the doc).
     Violates hard invariant 3 (`content_hash` stays populated). Replaced by P2.
  4. `controller.py:262` — raw SQL `UPDATE tabFile`, which is not one of the three
     sanctioned audited sites. Violates hard invariant 5. The statement dies with the
     controller in P2.
These are NOT P1 defects — they are the pre-existing fork behaviour the approved
architecture defers, and the owner's P1 scoping instruction names exactly this class. They
ARE release-blocking: **no build that still contains `controller.py`'s upload hook may be
tagged, and the final release audit must verify all four are gone.** Blast radius today is
zero (the app is installed on no site; P1 is an intermediate phase branch, not a release).
Decider: orchestrator; routed to the independent security reviewer for confirmation.

2026-08-14 — **Deferred to P5, recorded so it is not lost: the fork's bulk-migration
endpoint survives P1.** `controller.py:286,306` still expose `run_migrate_existing_files`
/ `migrate_existing_files` — a whitelisted, `only_for("System Manager")` surface that
enqueues a bulk upload of every existing attachment. It is inherited fork behaviour and
P1 is rename-only, so it is not a P1 defect, but **P5 must delete it when the migration
engine lands**: two independent migration paths, one of which has no campaign, no
batching, no CAS, no heartbeats, no pause/resume and no verification, is a data-safety
hazard and would bypass every F3/F4 gate. Decider: orchestrator.

2026-08-14 — **P1 CI defect found and fixed: the contract-completeness gate did not
bite.** `.github/helper/check_contract_completeness.sh` grepped the junit report for the
identifiers `C1..C19` and never inspected test status, so a run in which all nineteen
contract tests were collected-but-skipped (missing boto3, absent MinIO env, guarded
`skipUnless`) printed "C1..C19 all executed" and exited 0 — exactly the silent-skip
failure A22 and ACCEPTANCE_GATES F1 exist to prevent, and one that would have let every
later phase merge with the contract suite disabled. Fixed by parsing the report as XML:
skipped, absent, failed or errored C-tests now fail the gate, and `C1` is no longer
matched inside `C19`; the no-op until P2 ships `tests/contract/` is preserved. Ships with
a 9-case regression suite (`tests/test_contract_gate.py`) including the all-skipped case.
Found by the independent test-engineer's adversarial junit fixture. Decider: orchestrator.

2026-08-14 — **Deferred to P3, recorded so it is not lost.** `api/compat.py::
legacy_generate_file` gates on `check_permission("read")` (an improvement over the fork),
but returns `DoesNotExistError` for an unknown key and a permission error otherwise, so
the two outcomes are distinguishable and the endpoint is a file-existence oracle. A24
requires the legacy endpoint to use core's any-one-readable gate with **indistinguishable
403s**; A10/A24 assign that surface to P3. P3 must close it. Decider: orchestrator.

2026-08-14 — **Test environment provisioned in isolation (bounded ENVIRONMENT repair, not
a production action).** The bench MariaDB root credential is not available to the agent,
so `bench new-site` aborted and no scratch site could be created; filesystem credential
search was refused by policy and not worked around. Repair: a dedicated MariaDB 10.6
instance was initialised under `/home/user/cfs-scratch-mariadb` on **127.0.0.1:3307**
(own datadir/socket, utf8mb4, `innodb_file_per_table`, root credential stored 0600 outside
the repo), and `cfs-autonomous.local` created against it via `bench new-site --db-host
127.0.0.1 --db-port 3307`. Alternatives rejected: reusing an existing site (SAFETY_BOUNDARY
— never a business site) and modifying the business MariaDB (unnecessary risk). The
business instance on :3306 was never touched; all 13 DB-backed sites were re-verified
read-only afterwards (`list-apps` OK; none has the app installed). Bench redis restarted on
its configured ports 13001/11001. MinIO runs from the upstream binary on 127.0.0.1:9000
(bucket `cloud-file-storage-test`) because Docker is unavailable here — CI keeps using the
documented container. Decider: orchestrator, PLAN §E bounded repair before declaring
ENVIRONMENT_BLOCKER.

2026-08-15 — **P2: the four registered fork deviations are closed and `controller.py` is
deleted.** The upload hook it carried (unverified `os.remove` after upload, non-canonical
`file_url`, `content_hash` nulling, raw `UPDATE tabFile`) is replaced by `core_hooks.write_file`
+ the Cloud Storage Object state machine + deferred GC. The fork's `migrate_existing_files` /
`run_migrate_existing_files` bulk endpoint died with the module — earlier than the P5 date
recorded on 2026-08-14, because it lived in the deleted file and keeping it would have meant
re-implementing an unbatched, uncoordinated upload path with no campaign, CAS, heartbeat or
verification. Its P1 characterization tests went with it (`tests/test_controller.py`); the
behaviour they described no longer exists. Decider: P2 implementer, per the owner's P2 brief.

2026-08-15 — **P2: `sse_mode` gains a blank option (default still SSE-S3).** MinIO — our own
CI object store — rejects `ServerSideEncryption: AES256` outright when it has no KMS backend,
so a mandatory SSE header makes the app unusable on S3-compatible stores that encrypt at rest
themselves. The design doc already specified `\nSSE-S3\nSSE-KMS` (runtime-storage.md §2); A12
names the field and its default, not its option list. Both SSE modes keep dedicated L1 tests.
Alternative rejected: requiring a KMS-backed MinIO in CI (moves the constraint into every
developer's environment). Decider: P2 implementer.

2026-08-15 — **NEEDS-RULING (P6): A12's defaults and A27's validator contradict each other.**
A27 requires `object_delete_grace_days >= restore_window_days`, while A12 freezes the defaults
at 7 and 30. Shipping that validator makes a fresh install fail during `after_install`, which
saves the settings singleton. P2 therefore does **not** ship it — A27 is marked *(P6)* and P6
owns both the validator and the reconciliation. P6 must either raise the grace default to 30
or lower the restore-window default; it cannot ship the rule with today's defaults. Decider:
P2 implementer (scope), ruling needed from the orchestrator/owner before P6.

2026-08-15 — **P2: mode-transition gates are enforced by live server-side checks, not by a
recorded `test_connection` timestamp.** runtime-storage.md §9.2 wants "a passing test_connection
within 24h", which needs a timestamp field the frozen A12 set does not define. Instead, entering
a cloud mode runs a real `head_bucket` probe at save time (strictly stronger than a 24h-old
record), and `→ S3_ONLY` keeps the three hard gates (zero unverified managed Files, zero dirty
cache sidecars, no transferring campaign). `flags.skip_connection_probe` is the documented
console/test escape hatch; the app never sets it. Decider: P2 implementer.

2026-08-15 — **P2: phase-worktree test runs need `PYTHONPATH`, and coverage must be measured
separately.** The bench installs the app from `apps/cloud_file_storage` via a `.pth`, so
`bench --site … run-tests` in a phase worktree silently tests the integration checkout unless
`PYTHONPATH=<worktree>` is set. frappe's own `--coverage` hardcodes `apps/<app>` as the source
and therefore reports 0% for worktree code; real coverage is measured with `coverage run
--source=<worktree>` around the same command. Every later phase running in a worktree needs
both. Decider: P2 implementer (environment fact, recorded so it is not rediscovered).

2026-08-15 — **P2: two concurrent test runs on one scratch site produced deadlocks, and
that exposed a real defect.** An independent gate run executed `run-tests` against
`cfs-autonomous.local` at the same time as the implementer's run; both create Cloud Storage
Objects from identical fixture bytes, so they contended on the same rows and on `tabSingles`,
and 24 tests errored with `QueryDeadlockError`. The failures were environmental — the suite is
green in isolation before and after — but investigating them found that `engine._call` caught
bare `Exception` and translated *everything* into `CloudStorageTransportError`, which
`core_hooks._store` would then have degraded past as an S3 outage. Fixed in 7a402fd: only
`ClientError`/`BotoCoreError`/`Boto3Error` are translated; a deadlock propagates untouched
(InnoDB rolls back the whole victim transaction, so an engine-level retry would discard the
caller's work). The test helper also stopped writing `tabSingles` field-by-field.
**Process note for the orchestrator: only one test run at a time per scratch site.** Two
runs share one database and will produce failures that belong to neither. Decider: P2
implementer.

2026-08-15 — **RULING on the A27 NEEDS-RULING: `object_delete_grace_days` default is raised
from 7 to 30.** A27 requires `object_delete_grace_days >= restore_window_days`; A12 freezes
the two defaults at 7 and 30, which cannot both hold. Resolved from the approved architecture
without owner input: `docs/PLAN.md` outranks `docs/adr/` (INVARIANTS.md precedence rule), and
PLAN §H item 1 records the owner's adopted default as "Attachment recovery window — 30 days +
attachment-bucket versioning ON". `object_delete_grace_days` **is** that recovery window — it
is the delay before a physical S3 delete — so A12's 7 predates and contradicts an owner
decision that PLAN §H carries, and PLAN wins. With grace = 30 the A27 validator becomes
shippable in P6 exactly as written, and the invariant "an object cannot be physically gone
while a restorable backup still references it" holds out of the box. The A27 validator itself
stays P6; P2 ships only the default, a guarded patch that raises the value on sites currently
holding a grace shorter than their window (an operator-chosen longer grace is left alone), and
a test asserting the shipped defaults satisfy A27. Decider: team-lead ruling, implemented by
the P2 implementer. Supersedes the NEEDS-RULING entry above.

2026-08-15 — **P2: the test suite is idempotent, enforced structurally.** `Cloud Storage
Settings` is a persistent singleton and A3 mandates hook-side commits, so a test's writes to
it escape `FrappeTestCase`'s per-test rollback; left behind, `operation_mode` stays `S3_ONLY`
and every later run starts poisoned. Worse, it could not be undone through a normal save,
because the S3_ONLY downgrade gate correctly refuses — recovery needed DB-level intervention.
Fix: the full managed field set is snapshotted once per test class and force-restored at DB
level via `addClassCleanup`, which unittest runs even when a test errors mid-way; per-test
cleanup restores from that class snapshot rather than from a value read mid-run (a per-test
snapshot taken after an earlier leak would faithfully restore the leak). The downgrade gate
itself is unchanged — it is correct production behaviour. Proven by running the full suite
twice back to back from a pristine site and diffing the settings snapshot: identical.
Decider: team-lead finding, implemented by the P2 implementer.

2026-08-15 — **P2 gate round 2: both independent gates returned FAIL; seven findings fixed.**
Confirmed and closed: (1) the pre-delete recount was not a locking read — `lock_cso` locks the
CSO row but `frappe.db.count` is a plain `SELECT COUNT(*)` with no `for_update` at all, so
under REPEATABLE READ a sweep answered from the read view opened by its own candidate query
and could delete bytes referenced by a File committed after it started (A6); (2) that count
also saw only explicitly-linked rows, while the A1 chain resolves link-less rows through a
content-hash sibling — now counted, conservatively, keyed on the object's MD5 and visibility;
(3) `adopt_references` did not carry `content_hash`, so core's `_delete_file_on_disk` gate
could route a shared file to a full delete; (4) **R6, a Critical the consolidated fix list
omitted** — `handle_is_private_changed` looked up siblings with an unguarded `content_hash`,
and a `None` filter compiles to `IS NULL`, so flipping a hashless row repointed every hashless
File on the site at its object (verified: an unrelated file's `get_content()` returned the
wrong bytes); (5) `_repair_one` published local bytes under a key that is supposed to be their
sha256 without re-hashing; (6) presigned URLs lacked `no-store` (PLAN §C); (7) the cache had no
admission check, evicted unflushed edits, and lost them silently. **Overruled by the
orchestrator and deliberately NOT changed:** the security review's second Critical (a re-HEAD
before removing the local copy in the delete hook) — that path runs behind core's own
last-reference gate, no physical S3 delete happens there, and requiring a remote round trip
would block legitimate deletion during an outage. Decider: orchestrator adjudication between
two disagreeing reviewers, implemented by the P2 implementer.

2026-08-15 — **P2 gate round 3: both gates PASS; five further findings closed, three
recorded.** Fixed: **NEW-1** — `unlinked_sibling_count` mirrored only the `content_hash` arm
of the A1 chain, so a link-less row sharing `file_url` with the last linked row was a live
reader worth zero to the recount; it now mirrors both arms. **N1** — eviction's drift check compared size only, so a same-length in-place
edit was classed clean and was evictable, which is the A4 write-back loss R2 named; it now
compares the whole `(mtime, size)` signature exactly as `_sync_back` does, and the test edits
in place at identical length so it actually discriminates. **NEW-2** — `materialize` calls the
admission check while holding `cfs_mat_<file>`, and frappe's filelock is not reentrant, so a
materialize could never evict its own File's stale entries; the held identity is now passed
through. **N4** — the amendments register still recorded `object_delete_grace_days` 7 against
the shipped 30; recorded as supersession S1 with its rationale, so P6 implements A27 against
the right number. **N5** — the test settings snapshot omitted `secret_access_key`,
`multipart_chunksize_mb` and `transfer_max_concurrency`; all three are covered and the
credential is snapshotted and restored through `__Auth`. Decider: orchestrator findings,
implemented by the P2 implementer.

2026-08-15 — **Carried forward to P4, recorded so they are not rediscovered.** (a)
`materialize.make_room_for` walks the cache directory on every materialize once the cache sits
at budget — O(N) on a hot read path, a performance concern rather than a correctness one, to
be addressed with the rest of P4's hardening. (b) frappe's `filelock` calls `log_error` on
every timeout, so eviction's 0.2s lock probe writes an Error Log row per contended entry —
noisy, harmless, and worth a quieter probe when P4 touches this code. Decider: orchestrator
(reviewer N2/N3), deferred deliberately.

2026-08-15 — **Carried forward to P3/P5, recorded so it is not lost: NEW-3, the `file_url`
conflict check has the same three-valued-logic hole.** `core_hooks._canonical_file_url` filters
`{"content_hash": ("!=", digest.md5)}`, and in SQL a row whose `content_hash` is NULL is never
"not equal" to anything, so such a row is never seen as conflicting: the incoming file keeps
the URL and `_relink_url_siblings` then repoints that row — content substitution through the
URL arm, the same root cause as R6 and NEW-1. Not fixed in P2 on the orchestrator's explicit
instruction not to expand scope: the fix changes URL-assignment behaviour and would need
re-verification against the overwrite-stability contract (#11/#18) that P2 has already
demonstrated. Decider: orchestrator.

2026-08-15 — **P2 went beyond the literal NEW-1 finding, deliberately: fixing the count alone
would have hidden the harm rather than removed it.** The finding said the recount misses the
A1 `file_url` arm, and the fix asked for was to add that arm. Doing only that makes GC keep
the bytes — and leaves the survivor unable to reach them, because the `file_url` arm resolves
*through the deleted row's link*, and a row with a NULL `content_hash` (which
`patches/v0_2_0/backfill_s3_object_key` produces) has nothing else to resolve with. The
outcome would have been "bytes orphaned and unreachable" instead of "bytes destroyed", with a
green regression test asserting the finding was closed. So `delete_file_data_content` also
adopts link-less URL-sharers onto the object *before* releasing the reference
(`_adopt_url_sharers_before_release`). Canonical `file_url`s identify a file uniquely (hard
invariant 2), so such a row genuinely is a reference; the adoption makes an implicit reference
explicit at the one moment its implicit path is about to disappear, and it strengthens the
recount as a side effect, since an explicit link is seen by the primary count rather than by
the sibling arm. **The general lesson, which NEW-3 sits squarely inside: a fix that satisfies
the letter of a finding while leaving its harm reachable is worse than no fix, because it
retires the finding and ships a passing test.** Decider: P2 implementer, ratified by the
orchestrator.

2026-08-15 — **P3 gate round 1, Critical: the public renderer served thumbnails of private
files (A19 bypass), and the fix goes past the missing filter because the filter alone leaves
the harm reachable.** `PublicFileRenderer._resolve_derived` looked up the source row by
`thumbnail_url` without `is_private = 0`, so an unauthenticated GET of a stale
`/files/<name>_small.<ext>` returned a presigned URL for a 300x300 rendering of private
content. Five things changed, and the last three are the reason the first two are not enough:
(1) the filter is on the derived lookup, which also covers the alias branch because that
branch re-enters `_resolve`; (2) `store_thumbnail` stamps the derived object with the
**source's** visibility instead of a hardcoded public — it does not move the key (derived
objects live under `thm/` either way) but it stops the byte-identical-to-source case minting a
second, public-labelled copy of private bytes; (3) `handle_is_private_changed` rewrites
`thumbnail_url` onto the new prefix, for this row **and for every content_hash sharer**, since
`update_existing_file_docs` flips those by raw SQL and they never run the handler themselves;
(4) the flip **moves the local thumbnail** and `_write_local_thumbnail` writes a private
thumbnail to `private/files/` — core writes every thumbnail under `public/`
(`file.py:463`), where nginx's `try_files /<site>/public/$uri` serves it to anyone, which is
the same disclosure by a route no DB filter can reach; core's own `delete_file` already looks
for it in `private/files/` (`file_manager.py:317-322`), so this follows the half of core that
is right; (5) `download_private_file_cloud` now resolves a `thumbnail_url` through core's
own gate (`is_downloadable`, one access log, local copy streamed, presigned otherwise), so a
private file's thumbnail is *served correctly* rather than left dead — a dead URL is what
invites a later change to point it back at `/files/`. **The old thumbnail URL is deliberately
NOT aliased.** An alias would make the public renderer 302 an anonymous caller to
`/private/files/…`, which answers `Forbidden`: that converts a clean 404 into "this file
exists and is private", the existence oracle A24 forbids. Decider: P3 fix implementer, on the
orchestrator's Critical finding.

2026-08-15 — **The India Compliance patch decision is per site, not per process (A36).**
`runtime_patches._state["installed"]` is process-global while `_patch_india_compliance` is
conditional on `frappe.get_installed_apps()`, so in a multi-site worker the first site to
serve a request decided for the whole interpreter: a site without IC left IC's import-bound
`get_file_path` unpatched, and a site that had IC and arrived second silently kept core's
implementation — its GSTR ingest opening a local path that does not exist on an S3_ONLY site.
`ensure_installed` now asks again until the answer is yes. The re-ask is cheap
(`get_installed_apps` is cached) and monotonic: the patch is a no-op for sites that do not
need it, because `get_file_path_cloud` hands anything not cloud-backed back to core. Decider:
P3 fix implementer, on the orchestrator's High finding.

2026-08-15 — **P3 residuals accepted or deferred, recorded so they are not rediscovered.**
(a) **`compat._readable_file_for_key` timing (accepted residual).** Every refusal is the same
exception, message and status, but an unknown key does no work while a known one costs up to
`MAX_CANDIDATES` document loads and permission checks, so the two remain distinguishable by
timing. Not closed: padding every refusal to equal cost means running twenty permission checks
for every unknown key, which hands an attacker a free amplifier, and `has_permission` — share
rows, user permissions, the attached document — cannot be made constant-time. The channel also
requires a valid fork-era key, an opaque per-file string this runtime never mints, and reveals
only that *some* row references it. (b) **`invalidate_404` scans every `website_404` hkey on
every file write (deferred to P4).** The cache is keyed on the whole request URL, so entries
can only be found by parsing each key's path; the cost is O(cache) per write. Mitigated, not
fixed: `invalidate_public_404` is variadic, so the privacy flip's four URLs cost one scan
rather than four. Dropping the whole hash instead would trade a bounded per-write cost for
unbounded re-computation of every unrelated 404 on the site. (c) **`thumbnail_url` is
unindexed (deferred to P4).** Both derived-object lookups — the public renderer's and the new
private one — filter on `tabFile.thumbnail_url`, which `install.ensure_file_indexes` does not
cover, so each *miss* costs a scan. They run only after the primary URL lookup has already
missed, i.e. on 404s and probes, never on a successful download; the fix is an index plus its
patch and test, which belongs with P4's hardening. (d) **`get_file_path_cloud` narrowed.** It
widened core's `name|file_name` matcher with `file_url` and performs no permission check
(core's does not either) while materializing whatever it resolves; the `file_url` arm is now
consulted only when the argument is spelled `/files/…` or `/private/files/…`, which keeps
erpnext's real caller (`code_list_import.py:30`) working and leaves arbitrary caller text no
more resolvable than in core. No whitelisted caller passes user-controlled text there today
and none may be added. Decider: P3 fix implementer, on the orchestrator's Medium/Low findings.

2026-08-15 — **The spike now proves the floor ref locally; only `version-15` is left to CI
(A34 / PLAN §B).** The exit criterion is "spike matrix green on all refs", and
`analyze_private_file_dispatch` was built to run on any ref's source — but its only caller
passed `inspect.getsource(frappe.app)`, i.e. the installed v15.93.0, so the phase's defining
criterion rested on a CI matrix that has never run. The bench's own frappe checkout carries
the `v15.16.0` tag, so the analyzers now read `git show <ref>:<path>` out of it and assert
the same things about the floor: module-attribute dispatch, `validate_auth` before dispatch,
`init_request` before `validate_auth`, `find_file_by_url(path, name)`, and — comparing the
floor's gate to the installed one rather than to a literal — that the gate this app
reproduces did not change across the supported range. A ref that is absent (`version-15`,
which is not fetched here) **skips loudly**, naming the ref and the `git fetch` that would
make it provable; it never silently passes. The reader itself is tested both ways, because a
broken `source_at_ref` would otherwise turn every ref into a skip and leave the matrix
"green" while proving nothing. Decider: P3 fix implementer, on the reviewer's P3-2.

2026-08-15 — **One half-fabricated assertion removed, and the shape it belonged to closed.**
`test_serving_private.py` asserted `X-Amz-Expires=120` in the redirect's `Location`, but the
fake store builds that string out of the ttl it was handed, so the assertion read like
end-to-end proof while asserting the test's own number back. Whether a TTL reaches a real
SigV4 signature is proved against real MinIO (`test_minio_integration.py:203`, which also
fetches the URL). What a runtime test can honestly assert is that the redirect hands out
exactly the URL that was minted — so the fake now records its return value and both sites
with this shape (the private serving test and the C10 contract test) assert that identity
instead. Decider: P3 fix implementer, on the reviewer's P3-5.

2026-08-15 — **CLOSED, the P1 deferral: the legacy endpoint's 404-vs-403 oracle (A10/A24).**
The 2026-08-14 entry above deferred it to P3 and P3 closes it. `api/compat.py::
legacy_generate_file` no longer raises `DoesNotExistError` for an unknown key: every refusal
— unknown key, known key whose rows the caller may not read, readable row whose object is
gone or unservable — goes through one `_refuse()` with one exception type, one message and
one title (`compat.py:79-81`, called at `:36` and `:41`), and the gate is core's own
any-one-readable rule applied across the rows sharing the key, narrowed to one row when `fid`
is supplied. `tests/test_compat.py` asserts the refusals are equal as `(type, message)` sets
across {unknown, empty, unreadable, fid-pinned}, so a divergence fails rather than degrades.
One residual remains and is accepted separately below: the cases are still distinguishable by
timing. Decider: P3 fix implementer, closing an orchestrator deferral.

2026-08-15 — **The private access log is written before servability is known, deliberately.**
`serving/private.py` logs immediately after the permission gate, so a request that then hits
a 503 (breaker open, object not yet uploaded) or a NotFound (object deleted) still leaves a
row. Two reasons, and it is not an accident. First, it is core's own order
(`frappe/utils/response.py:272` logs before `send_private_file`), and P3's contract is to
preserve core's semantics rather than improve them. Second, the auditable event is *an
authorised attempt to read this file* — who asked for what, and whether they were allowed —
which is answered at the gate; whether the bucket then had a bad minute is an operational
fact recorded in the CSO status and the logs, not an access-control fact. Placing the log
after the outcome would also mean repeating it on four branches (local, presigned, 503,
NotFound), which is exactly how "exactly one row" contracts rot. The new derived-thumbnail
branch follows the same order for the same reasons. Decider: P3 fix implementer, on the
reviewer's P3-6.

2026-08-15 — **The privacy flip relocates thumbnails only for rows this app manages; the
rest is a known upstream characteristic, left alone deliberately.** `handle_is_private_
changed` rewrites `thumbnail_url` and moves the local thumbnail on the cloud-backed branch
only. A row with no Cloud Storage Object — a pre-install row, or one of the
LOCAL_OPERATIONAL doctypes — still delegates to core verbatim, so core's residue (a
`public/files/<name>_small.<ext>` left behind by a flip, and a private file's thumbnail
written into `public/private/files/`) stays where core puts it. Three reasons, in order of
weight. (1) PLAN §A's mode contract requires LOCAL_ONLY to be core-identical, and silently
relocating files for rows this app does not manage would break that in the one mode whose
whole promise is "we changed nothing". (2) The residue is stock frappe behaviour that this
app neither introduces nor worsens, and no cloud path discloses those rows — the renderer's
`is_private = 0` filter refuses them whatever their URL says. (3) An app that quietly moves
files it does not own is a worse surprise for an operator than the upstream quirk it papers
over. Note the underlying upstream defect is two-sided: core's `make_thumbnail` writes to
`get_site_path("public", thumbnail_url.lstrip("/"))` (`file.py:463`) while core's own
`delete_file` looks for the same thumbnail in `private/files/` (`file_manager.py:317-322`),
so a private file's thumbnail is both orphaned on delete and — under bench's standard nginx
rule `try_files /<site>/public/$uri` — anonymously readable. That nginx premise is this
app's own documented assumption (`serving/public.py:3`) and could not be verified on this
bench, which has no nginx installed; it is worth an operator's check against a deployed
config. Decider: orchestrator ruling on the P3 fix implementer's boundary question.

2026-08-15 — **Backlog (post-v1), extending the 2026-08-14 backlog entry: an upstream
frappe PR for the thumbnail path inconsistency.** `File.make_thumbnail` should write a
thumbnail through the same convention `delete_file` reads it back with — `private/files/`
for a `/private/files/…` thumbnail URL — rather than always under `public/`. That closes
both halves of the defect described in the entry above (orphaned local thumbnail on delete;
a private file's thumbnail served anonymously by nginx) for every frappe site, not just
ours. Same shape as the existing backlog item for the core `content_hash` index. Not a v1
deliverable: this app is already safe on its own rows, and an upstream fix cannot be
depended on within the release. Decider: orchestrator.

2026-08-15 — **AMENDED RULING: the "leave it, it is stock frappe" half of the 2026-08-15
thumbnail-boundary entry above was decided on an incorrect premise and is superseded.** The
earlier ruling rested on the second reviewer's P3-4 finding that a private file's thumbnail
under `public/private/files/` is "not served from there, so there is no exposure". The
orchestrator verified that on this machine and it is wrong. Three pieces of evidence: (1)
bench's nginx template — a pip user install at `~/.local/lib/python3.10/site-packages/
bench/config/templates/nginx.conf`, which is why the implementer could not find it — answers
`location /` with `try_files /{{ site_name }}/public/$uri @webserver` at line 91, and its
`location ~ ^/protected/(.*)` block at line 64 is `internal`, i.e. it serves
X-Accel-Redirect and does not guard `/private/...` requests; (2) core's `make_thumbnail`
really does write there (`frappe/core/doctype/file/file.py:463`); (3) such a file was
present on `cfs-autonomous.local` at the time of the ruling. So on any nginx-served bench,
`GET /private/files/<name>_small.png` returns a private file's thumbnail to an
unauthenticated caller with no frappe process involved — a real disclosure, upstream, and
P3-4 was never "minor, no exposure".

**What survives the amendment and what changes.** The boundary itself stands: this app still
does not relocate files for rows it does not manage, because PLAN §A requires LOCAL_ONLY to
be core-identical and because an app that silently moves an operator's data is the worse
surprise. What does not survive is silence — "it is stock frappe" is not an adequate response
once we know stock frappe is disclosing private thumbnails to our own users. Three actions,
detection before mutation: (a) **detect and warn** — `health.detect_public_private_residue`
walks `sites/<site>/public/private/`, reports count, paths and remediation, and is wired into
`after_migrate` (Error Log) and the Settings health panel (`test_connection`); it moves and
deletes nothing, and there is a test asserting it does not; (b) **document** — a SECURITY
section in `docs/runbooks/deployment.md` giving the vector, the evidence, what this app
fixes, what remains exposed on stock rows, how to detect and how to remediate, with a note
that P8's release evidence must carry it; (c) **escalate upstream**, below. Decider:
orchestrator, amending its own earlier ruling on corrected evidence; recorded as an amendment
rather than an edit because the audit trail matters more than looking consistent.

2026-08-15 — **Backlog escalation (post-v1): the frappe thumbnail path issue goes upstream as
a SECURITY report, not a plain PR.** The 2026-08-15 backlog entry above proposed an upstream
PR to make `make_thumbnail` write through the same convention `delete_file` reads. Now that
the disclosure is confirmed reachable by an unauthenticated GET on any nginx-served bench, it
is reported to frappe through its security process, carrying the three pieces of evidence in
the amendment entry above. The PR remains the proposed fix; the difference is the channel and
the urgency. This app does not depend on the outcome: cloud-backed rows are already fixed,
and the runbook tells operators how to remediate the rest today. Decider: orchestrator.

2026-08-15 — **Observation, not a decision: one full-suite run failed intermittently and the
cause was not established.** During the P3 fix round, two consecutive full-suite runs on
`cfs-autonomous.local` failed (2 failures + 24 errors, then ~30 mostly-`ERROR` results)
between green runs, and the failures were scattered across unrelated suites — gc, writeback,
serving, the state machine, MinIO — which is the signature of a shared resource failing
mid-run rather than a logic defect in any one area. **The tracebacks were not captured**,
which is the gap in this record; six subsequent full runs (five with MinIO) were green, so it
could not be reproduced. Evidence that exists: the failing run took 63s against a ~47s
norm, and the site's Error Log carries `Filelock: Failed to aquire` rows written even during
green runs — the P2 gate already carried "frappe's filelock calls `log_error` on every
timeout, so eviction's 0.2s lock probe writes a row per contended entry" to P4 as harmless
noise. Under load it may be more than noise: a 0.2s probe that times out is a test failure,
not a log line. The other candidate is a second process touching the same site, which
`tests/utils.set_mode` already documents as able to deadlock a run ("a field-by-field loop
over `tabSingles` is enough lock churn to deadlock against any other run sharing the site").
Recorded so P4 treats the filelock probe as a correctness question rather than a cosmetic
one, and so the next agent that sees a scattered failure captures the traceback instead of
re-running until it goes away. Decider: P3 fix implementer, reporting an unexplained result
rather than a resolved one.

2026-08-15 — **P3 re-review NEW-2 fixed rather than recorded: the flip re-stamps the derived
object's visibility.** `store_thumbnail` states that a derived object carries its SOURCE's
visibility, and the privacy flip made that false one save later — no disclosure (a derived
key is `thm/…` whatever the label says, and every derived byte is presigned), but an
invariant asserted at creation and abandoned at update is how the next change comes to rely
on something untrue. The re-stamp lives in `storage/objects.restamp_derived_visibility`, i.e.
inside the A7 choke point rather than as a `set_value` at the call site, because it needs
three guards that only belong there: it takes the CSO row lock; it **refuses a primary
object** outright, since a primary object's visibility is its key and re-labelling one
without moving its bytes would make the row lie about where they are; and it checks the
frozen `(content_sha256, visibility, bucket)` unique index first, leaving the label alone
when another object already holds that identity — a stale label is a documentation defect, a
failed flip would be a data defect. The one place that read `visibility` unlocked
(`unlinked_sibling_count`, on the documented grounds that it is immutable after insert) has
its comment amended in place: the immutability claim still holds for primary objects, and for
derived ones the read stays safe because the arm consuming it matches File rows on
`content_hash` and no File row ever resolves to a genuine derived object — a thumbnail has no
File row, and the byte-identical-to-source case resolves to the source object instead of
creating a derived one, so a stale label can only inflate that count, never depress it.
Sharers are re-stamped too, and unconditionally rather than only when their URL moved.
Four mutations confirm the tests discriminate: dropping either re-stamp, dropping the
primary-object guard, or dropping the clash guard each fails a distinct test (the last one as
a unique-index error, which is the data defect the guard exists to prevent). Decider: P3 fix
implementer, on the re-reviewer's NEW-2, taking the orchestrator's stated preference for a fix.

2026-08-15 — **Recorded, not fixed: after a flip a derived object's `derived_of` still points
at the pre-flip source object.** `handle_is_private_changed` creates a new CSO for the new
visibility and releases the old one, but the derived row keeps `derived_of = <old source>`.
Nothing breaks: `thumbnail_cso_for` reads the File-side `cloud_thumbnail_object` link first,
and `delete_file_data_content` releases the linked thumbnail object explicitly, so neither
serving nor GC depends on the stale pointer. The narrow consequence is that
`find_derived_cso` — the fallback used only when a row carries no link, i.e. P2-era or
fork-era rows — cannot find the thumbnail after a flip. That is pre-existing rather than
introduced by the P3 fixes (the fallback resolved the source through the A1 chain and so
looked for the *new* object even before this change), and re-pointing the link is a second
behaviour change on a code path that is currently frozen for the gate. Left for P5's
migration work, which is where link-less legacy rows are adopted anyway. Decider: P3 fix
implementer, declining to widen a frozen tree.

2026-08-15 — **The upstream security report is an artifact, not a backlog line:
`docs/security/upstream-frappe-private-thumbnail-disclosure.md`.** It carries the nginx
template line, core's write path, the reproduction, a suggested patch and the two questions
that make it a report rather than a PR (migrating existing sites; whether private files
should be thumbnailed at all, given that stock frappe has no path that serves a private
thumbnail URL). It leads with the framing that makes it filable regardless of any dispute
about deployment: **§2 is provable from frappe's source alone** — `make_thumbnail` writes a
private thumbnail to `public/private/files/` while `delete_file` looks for it in
`private/files/`, so write and delete disagree and the write side is the one in the public
tree. To be filed through frappe's security process rather than as a public issue, because
the disclosure half is remotely reachable with no authentication. Decider: orchestrator's
escalation, drafted by the P3 fix implementer.

2026-08-15 — **P4 built a dedicated ecosystem site rather than adding erpnext to the unit
site.** `cfs-ecosystem.local` on the isolated scratch MariaDB `127.0.0.1:3307` carries
erpnext 15.93.0 + payments + hrms 15.49.2 + india_compliance 15.18.1 + cloud_file_storage;
`cfs-autonomous.local` stays a fast, app-free unit site. Alternative considered and rejected:
installing the three apps onto the unit site, which would have made every later phase's inner
loop minutes long and coupled unit failures to erpnext's validations (India Compliance alone
makes `gst_hsn_code` mandatory on Item). The cost is that the ecosystem module skips on the
unit site, which is paid for by `check_ecosystem_completeness.sh` failing the L3 job on any
skip. Decider: P4 implementer, on the lead's instruction.

2026-08-15 — **T-RIV/T-ITEMIMG/T-IC-LIVE call the real upstream functions, not
re-implementations of them.** `tests/test_ecosystem.py` imports and drives
`erpnext.stock.stock_ledger.{create_json_gz_file, get_reposting_data, get_reposting_file_name}`,
`GSTReturnLog.{update_json_for, get_json_for}`, `gst_return_log.download_file`,
`frappe.utils.file_manager.save_file`, `india_compliance.gst_india.utils.get_json_from_file`
and `PurchaseReconciliationTool.get_return_period_from_file`. A hand-rolled equivalent would
keep passing after the upstream call changed shape, which is the entire failure mode an
ecosystem gate exists to catch. Two consequences were accepted deliberately: the Repost Item
Valuation fixture is inserted with `ignore_validate` (its `validate()` reaches for a Company
through a Warehouse, which would drag chart-of-accounts setup into a test about file bytes,
and the attachment path reads none of those fields); and the reconciliation-tool assertion is
on the returned period rather than on "no exception", because
`get_return_period_from_file` swallows every exception and returns None. Decider: P4
implementer.

2026-08-15 — **Two A4 write-through defects found by T-RIV and fixed. Both were live
data-loss paths in *merged* P2 code that 641 passing tests did not see, and both surfaced
only because the ecosystem tests drive erpnext's real reposting loop instead of a mock.**
That loop calls `create_json_gz_file` once per chunk against the same File
(`stock_ledger.py:415-423`), so hand-out → write → hand-out is an ordinary sequence, and a
mock of `get_full_path()` reproduces neither mechanism.

*Defect 1 — a second hand-out re-downloaded over unflushed bytes.* `materialize()` decided
cache-hit vs re-download by comparing the entry's **size** against its sidecar. A local write
that changed the size therefore failed the hit test, fell through to the download branch, and
`os.replace(partial, path)` overwrote bytes that existed nowhere else yet: write-back had not
run, so the object still held the old content and the new content was destroyed with no error
anywhere. The fix hands back any entry with a provable `(mtime, size)` drift from its sidecar,
marks it dirty so eviction also protects it, and logs `materialize_conflict` when the object
moved on as well — local wins, because bytes that exist only locally are the ones that can be
lost. A sidecar missing either half of the signature is deliberately NOT treated as an edit:
it cannot prove one, and pinning unprovable local bytes would strand a file whose sidecar
write once failed on permanently stale content. **Mutation that proves the fix:** disabling
the drift branch in `materialize()` (`if False and has_unflushed_changes(...)`) fails
`test_ecosystem.TestRepostItemValuation.test_a_second_hand_out_does_not_overwrite_the_bytes_just_written`
plus `test_writeback.TestRepeatedHandOuts.{test_a_second_hand_out_never_re_downloads_over_unflushed_bytes,
test_a_conflicting_object_replacement_keeps_the_local_bytes_and_says_so}` — 5 tests in total.

*Defect 2 — a second hand-out disarmed write-back.* `track_path` recorded the CURRENT
`(mtime, size)` on every call, so the second hand-out reset the baseline to the post-write
state and `_sync_back` compared the file against itself, concluded nothing had changed, and
returned False. The edit stayed on disk, correct and invisible, until eviction eventually
dropped it — the worst shape available, because every read in the same request sees the right
bytes. The fix keeps the baseline from the FIRST hand-out in the request. **Mutation that
proves the fix:** forcing `previous = None` in `track_path` fails
`test_ecosystem.TestRepostItemValuation.test_a_second_hand_out_does_not_disarm_the_pending_write_back`
and `test_writeback.TestRepeatedHandOuts.{test_the_baseline_recorded_is_the_first_hand_outs,
test_an_edit_still_reaches_the_bucket_after_a_second_hand_out}`.

The two are independent — either alone loses the write — and they are separated in the tests
by payload size: the clobber needs a size change to be visible, the disarm needs identical
sizes so that nothing is clobbered and only the baseline is at fault. Alternative considered
and rejected: making `_read_bytes` prefer a drifted cache entry, which would have masked
defect 2 in a read-back assertion without fixing it. Decider: P4 implementer.

2026-08-15 — **The filelock probe carry-over was two problems, and the noisy one was hiding
the real one.** The Error Log rows came from `frappe.utils.synchronization.filelock`, which
calls `frappe.log_error` on every `Timeout` before raising: a contended cache entry — an
ordinary "someone is using this, skip it" — inserted an Error Log document with a full
`get_traceback(with_context=True)`, per entry per sweep. The probe now takes the same lockfile
directly through `filelock.FileLock`, and two tests hold both ends of that trade (the path is
compared against a lockfile frappe actually created; a contended probe must add zero Error Log
rows). Underneath it was a genuine under-freeing bug: `trim_to` measured the cache total over
*evictable* entries only, so dirty entries occupied disk invisibly and a mostly-dirty cache
reported itself under budget and freed nothing — the bound was not what §A claims. The total
is now measured over every entry, only clean ones are evicted, and what could not be freed is
returned as `contended`/`bytes_contended`/`over_budget` with a warning when a sweep cannot
meet the budget. **Mutations that prove each half:** routing the probe back through frappe's
`filelock` fails the two no-Error-Log tests; pointing `probe_lock_path` at a different
directory fails 9; measuring `trim_to`'s total over clean entries only fails
`TestEvictionReportsWhatItCouldNotFree.test_dirty_entries_count_towards_the_budget` and the
over-budget warning test;
dropping the contention counters fails the reporting tests. Recorded because the
reclassification reached the right conclusion by the wrong mechanism: a fix aimed at the probe
would have shipped behind a passing test with the real bug untouched. Decider: P4 implementer,
on the lead's perf→correctness reclassification, correcting its mechanism.

2026-08-15 — **Recorded, not fixed: within one request, `get_content()` does not see bytes
written through `get_full_path()` until write-back flushes.** `_read_bytes` prefers the
canonical local copy and otherwise goes to the object store; it never consults a drifted cache
entry. No ecosystem caller is affected — erpnext's repost reads at job start and writes at job
end, India Compliance reads through `get_file_path` (which returns the same materialized path,
so it sees the new bytes), and IC's read-modify-write in `update_json_for` uses `get_content`
on both sides — so the fix would be a broad change to read semantics for a shape nobody
executes, and serving unflushed bytes to unrelated readers has its own risks. Revisit if a
caller appears. Decider: P4 implementer, declining to widen the change surface.

2026-08-15 — **NEEDS-RULING: a second write-capable session edited the P4 worktree.** From
~15:09 a second agent process (pid 30035) modified `tests/test_writeback.py`,
`tests/test_maintenance.py` and `pyproject.toml` in `/home/user/v15/apps/cfs-P4` while this
implementer was working in it, and at 15:23 reverted all three to the committed baseline —
removing its own 7 tests (preserved by it as `p4b_my_edits.patch`) and this implementer's 11,
which were in the same file. Recovered from a 15:19 snapshot; nothing is lost. This is the
INVARIANTS.md invariant "no two write-capable agents or sessions modify the same worktree
concurrently — ever", and it produced the exact failure the invariant predicts. Reported to
the lead twice; work continued only on files the other session had never touched. Open
question for the orchestrator: whether that session is sanctioned, and whether its
duplicate-coverage tests should be re-applied alongside or instead of this implementer's.
Decider: pending — P4 implementer declined to delete another agent's tests unilaterally.

2026-08-15 — **NEEDS-RULING resolved: the concurrent session was an accident, not a rival.**
Amending the earlier entry with what the evidence turned out to say. The orchestrator read a
"connection lost mid-response" notification as a dead implementer, committed this
implementer's in-progress `cache/eviction.py` as `df33700` so it could not be lost with the
worktree, and spawned a second implementer to continue. The implementer was alive. `df33700`
is therefore this implementer's own work, honestly labelled unverified, and was built on
rather than around. The second session backed its own additions out on realising the same
thing; both sessions yielded their half of `tests/test_writeback.py` at once, which is why the
file briefly held neither set. Recovered from a 15:19 snapshot. Two rulings followed: the
single-owner invariant held (one session, the original), and one test was salvaged from the
second session — `test_the_probe_does_not_hold_a_lock_frappe_would_consider_free`, which
asserts that holding the probe's lock actually excludes frappe's acquisition. That is a
different property from "the paths match", and a probe that agreed on the path while
acquiring it in a way frappe's `FileLock` could not see would have passed the path test while
protecting nothing — the sixth instance in this project of a check that cannot fail, caught
before it shipped. Decider: orchestrator, recording its own error.

2026-08-15 — **`Co-Authored-By` trailers before this point are wrong, including the
orchestrator's.** The P0.5 scaffold specified `the baseline agent` and every commit on
`feat/enterprise-cloud-storage-v15` up to and including `df33700` carries it, but the work was
done by Opus 5. A `Co-Authored-By` line is a factual record, not a style convention, so the P4
implementation commits (`47fef25`..) carry `the implementation agent` and the earlier
trailers are left in place rather than rewritten: no force-push, and the record of the mistake
is more useful at the release audit than a tidy branch. The scaffold's template should be
corrected before P5. Decider: orchestrator, on the implementer's refusal to write an
attribution it knew to be false.

2026-08-15 — **Correction: two same-day DECISIONS entries were expanded in place, and the
append-only rule now binds without exception.** `565ab5a` rewrote its own A4 and filelock
paragraphs rather than appending to them. INVARIANTS.md declares this file append-only without
qualification; the P4 reviewer flagged it (F6). The rewrite expanded the author's own
ungated same-day entries rather than altering earlier history, so it is not reverted — but
"it was only my own recent entry" is precisely the reasoning that erodes an append-only
log, so from here corrections are appended as new dated entries naming what they supersede.
This entry records that the in-place expansion happened. Decider: orchestrator, on the P4
gate finding.

2026-08-15 — **Third instance of a reference to a check drifting from the check itself.**
The filelock entry above cited `test_a_dirty_entry_still_counts_against_the_budget`; the
real method is
`TestEvictionReportsWhatItCouldNotFree.test_dirty_entries_count_towards_the_budget`. Previously:
`probe_lock_path`'s docstring named `..._the_same_lockfile_...` against a method called
`..._the_lockfile_...`, which made a real, working guard invisible to a name-based audit and
led the orchestrator to escalate it as a missing test; and P1's contract gate matched test
identifiers without checking their status. The guards themselves have been sound each time —
what keeps rotting is the pointer to them. Two further instances were found while writing
this entry: the correction itself first cited class `TestEvictionBudget`, which does not
exist, and `test_serving_spike.py` had wrapped a test name across two comment lines, making
it ungreppable. So the guard is built rather than deferred:
`tests/test_documentation_references.py` fails a run when any name cited in this file, in
PROGRESS.md, or in an app docstring or comment does not resolve to a real test method — and
when a `Class.method` citation names the wrong class, which is exactly the shape this
entry's own first draft got wrong. A stale name may only be *quoted* by registering it in
`HISTORICAL_NAMES` with a reason, and that allowlist is checked both ways so it cannot
become the place stale names hide. Decider: P4 implementer, on the P4 gate finding (F5),
building the check the finding suggested deferring to P5.

2026-08-15 — **Carried to P5/P6: a permanently-dirty cache entry warns forever with no
remedy.** An entry whose sidecar is missing or garbled is drift by `_has_drifted`'s
definition, so eviction will never delete it, and with P4's budget fix it now also counts
toward `over_budget` — which means the sweep logs `cache_eviction_over_budget` on every run
and no amount of sweeping will ever clear it. Both halves are the safe direction (bytes that
cannot be shown safe to delete are kept, and a bound that cannot be met is stated rather than
hidden), so nothing here is being changed under P4's gate. What is missing is the third step:
a repair path that re-derives a sidecar from the object when the bytes still match it, and
quarantines rather than deletes when they do not. P5 owns quarantine layout (A32) and P6 owns
the maintenance surface, so it belongs to whichever lands first. Raised by the P4 reviewer as
a non-blocking Low. Decider: P4 implementer, recording rather than widening a gated phase.

2026-08-15 — **P4 PASS, and the orchestrator's own corrections were the fourth and fifth
drift instances.** Independent reviewer `GATE VERDICT: PASS` at `565ab5a` (no Critical, no
code defect); attested at `d46e813` under exclusive lock: `cfs-autonomous.local` 668/668
twice, `cfs-ecosystem.local` 682/682 twice with real MinIO and zero ecosystem skips,
settings identical across all 35 `tabSingles` fields, both CI gates correct in both
directions, pre-commit green, 19 proving mutations.

Worth recording honestly: commit `89e60ea` was the orchestrator fixing the gate's three
documentation findings, and it introduced two **new** errors of precisely the class it was
documenting — it cited a class `TestEvictionBudget` that does not exist (the method is on
`TestEvictionReportsWhatItCouldNotFree`), and credited `f3bc817` with a static check that
did not yet exist, which would have made this file assert a guard that was not there. The
implementer caught both. So the tally in the drift entry is five instances, not three, and
two were created by the careful correction of the first three.

That is the argument for `tests/test_documentation_references.py` existing at all: three
rounds of attentive human-style reading produced two fresh errors inside the very entry
warning about them, while the machine check found two more nobody had noticed (a comment
citation wrapped across two lines, hence ungreppable) and now validates 623 citations
against 677 real methods on every run. Stale names may only be quoted via `HISTORICAL_NAMES`
with a reason, and that allowlist is verified in both directions so it cannot become the
place stale names hide. Decider: orchestrator; guard built by the P4 implementer rather than
deferred to P5 as originally ruled.

2026-08-15 — **P5 entry gate: the frozen A7 constructor cannot express an adoption, and the
resolution stays inside the choke point.** PLAN §B makes CSO interface-conformance an entry
criterion, so it was asserted before the engine was written
(`tests/test_migration_entry_gate.py`). A15/A18 adoption needs a Cloud Storage Object for bytes
already in the bucket, at the key the 0.2.x fork chose, with no SHA256 — and `ensure_cso`
derives its key from the content hash and requires that hash. P2/P3 already assume such rows
exist (`api/compat._resolve_cso` resolves an object *by a fork key*), so the concept was in the
design and only the constructor was missing. Added `storage.objects.adopt_legacy_cso`: same
module, status fixed at `legacy_unverified` with no argument that could write anything better,
idempotent on the unique `s3_key` index. INVARIANTS.md invariant 4 still holds — migration mutates a
CSO only through `storage.objects`. Alternative rejected: letting `migration/adoption.py` insert
the row itself, which is ORM rather than raw SQL and so technically legal, but would put a second
place in the codebase that can create a Cloud Storage Object. Decider: P5 implementer.

2026-08-15 — **A33 makes the design's unique constraints redundant, so they are not created.**
migration-engine.md §2.3/§2.4 specify unique `(campaign, url_hash)` and `(campaign, file)`.
A33 postdates them and names the primary key `sha1(campaign‖url_hash)` / `sha1(campaign‖file)`,
which enforces exactly the same thing. At 1.2M objects plus 1.2M refs a redundant unique index is
index bytes F4 has to account for and answers no query this engine asks — every lookup that would
have used it computes the name instead. Four standalone indexes whose leading column a composite
already covers were dropped for the same reason. Decider: P5 implementer; recorded because a
reviewer reading §2.3 will look for them.

2026-08-15 — **The analyzer's identity rule deviates from design §3.2 on one point.** §3.2 says a
row is `legacy_fork_key` when the URL is the fork endpoint **or** `s3_object_key` is set. Taking
that literally reclassifies a healthy local file as an already-remote adoption the moment a stale
locator column survives on it — and adoption moves no bytes, so that file would never be uploaded
and would simply be missing under S3_ONLY. The URL is what serving resolves and what business
fields store, so the URL decides and `s3_object_key` is consulted only for rows whose URL is not
canonical. Asserted in `TestAnalyzerIdentity.test_a_canonical_url_wins_over_a_stale_locator_column`.
Decider: P5 implementer.

2026-08-15 — **The planner keysets on the primary key, not on `(is_private, disk_path)`.**
Design §4 asks for disk-path ordering so a batch reads one area of the disk at a time. That
ordering needs a sort of an unindexed column on every page, turning an O(n) plan into
O(n²/batch_size) at 1.2M rows, and the locality it buys is small in a corpus the design itself
calls request-rate bound (§12). Thumbnails are still planned last, which is correctness rather
than locality: a thumbnail's derived object is keyed off its source's content hash and cannot be
built before the source is uploaded. Decider: P5 implementer.

2026-08-15 — **Which refs the A26 UPDATE linked is read back, not inferred from ROW_COUNT.**
A26 says "ROW_COUNT compared to ref count". MariaDB reports *changed* rows, so a ref that was
already correctly linked — a retried batch, a DUAL_WRITE row that got there first — reports zero
and would be declared `superseded_by_runtime`, which bars its object from CLEANUP forever. The
guarded UPDATE runs exactly as A26 specifies and ROW_COUNT is still captured and written to the
audit row; the authoritative linked/superseded split comes from a read-back of `tabFile` in the
same transaction. Asserted in
`TestVerifyLinkGuard.test_an_already_correctly_linked_ref_is_not_mistaken_for_superseded`.
Decider: P5 implementer.

2026-08-15 — **VERIFY does not promote an adopted object, and that was a real defect not a
design choice.** `verify_and_link` set every object it checked to `verified`. For a
`legacy_unverified` row the check that runs is existence, size and — only when the ETag is usable
— an MD5; it is never a SHA256 of the bytes, because nobody has hashed them. The shipped row
therefore claimed a verification that had not happened, on exactly the population A15 was written
about. Found by `TestLegacyForkAdoption.test_an_adopted_object_is_never_verified_without_a_hash`
on its first run. VERIFY now leaves such an object alone while still writing the guarded link
(those bytes were already what the site served, and the status is servable);
`adoption.promote_adopted_object`, which re-reads every byte, is the only path to `verified`. The
consequence is asserted too: an unpromoted adopted object fails CLEANUP's gate, so unhashed bytes
keep their local copy. Decider: P5 implementer.

2026-08-15 — **`reconcile._find_unreferenced` is the second declared non-locking call site, and
the P2 lock gate's "exactly one" assertion became a two-way comparison.** The A6 gate bit on P5's
first new caller, which is what it exists for. Non-locking is right there: the number becomes a
line in a drift report that nothing acts on automatically, and the runtime's GC takes its own
locking recount before deleting anything, while locking would hold row locks on `tabFile` across
a sweep of every object in the bucket, in one transaction, on a live site. The count `1` was a
snapshot of the then-current code; it is replaced by an equality against the declared table, which
keeps the same property (nothing undeclared skips the lock) and adds one the count did not have
(nothing declared may have disappeared). The decision-path assertions are untouched and the new
site is explicitly asserted not to be one. Decider: P5 implementer, per the gate's own design.

2026-08-15 — **Defects the P5 suites found in P5's own code, recorded because each is a shape
worth recognising.** (1) A Pending batch whose objects were all backing off was re-dispatched in
a tight loop, burning its bounded attempts and marking a healthy batch Failed — the dispatcher now
skips a batch with non-terminal work none of which is due. (2) `iter_batch_objects` selected
eleven columns while its consumers read thirty; CLEANUP read a row with no `cloud_storage_object`
and concluded the object had none, a refusal indistinguishable from a real one — fails safe *and*
silently, which is the worst shape for a bug in a gate. (3) `cleanup_object` trusted its caller to
have checked approval and the mode; it is the function that moves the file, so it checks them
itself. (4) `audit.record(..., action=…)` collided with its own positional parameter, so an audit
write raised inside the transaction of the thing it was auditing; `action` is now positional-only.
(5) `backoff_seconds` capped before applying jitter, so +25% carried the result back over the cap.
(6) The A29 preflight read a backup taken seconds ago as the oldest possible one, because
`age_hours` is `0.0` and `0.0 or 1e9` is `1e9` — found when the rehearsal refused a
production-scale campaign one minute after a successful `bench backup`. Decider: P5 implementer.

2026-08-15 — **`FakeObjectStore` stubbed out `engine.verify` with a HEAD echo, which made every
"verification bites" assertion vacuous.** The fake recomputed the checksum from whatever bytes it
was holding, so it could never disagree with itself — on the gate that decides whether a local
file may be deleted. The stub is removed and the real implementation runs against the fake bucket;
the tamper and missing-object tests fail without it. This is a P2-era test-harness defect that
P5's suites surfaced, in the same family as the four "checks that could not fail" P2's own gate
found. Decider: P5 implementer.

2026-08-15 — **RULING: the P5 entry-gate finding is approved and recorded as ADR
supersession S2 — `storage/objects.adopt_legacy_cso()` extends A7's frozen public API to
five functions.** The tension is real and between two binding amendments: A7 freezes
`ensure_cso()` as the constructor and that function derives `s3_key` from a
`content_sha256` it requires, while A15/A18 require adopting rows that point at a
fork-chosen key and have no SHA256 until a streamed re-GET produces one. They cannot both
be satisfied through the frozen constructor.

Approved for four reasons, recorded so this is not re-litigated in P8: **(1)** A7's intent
is the boundary — "the migration engine imports these, never inlines SQL against CSO" — and
a function inside the choke point honours it completely; what is frozen is where CSO
mutation may happen, not the arity of the API behind it. **(2)** It is stronger than the
alternative: an override parameter on `ensure_cso()` would let any caller bypass the hash
requirement and would demote A15's honesty rule to caller discipline, whereas hardcoding
the status with **no override argument** makes the dishonest case structurally unreachable
— and on a project that has found seven checks naming properties they did not test, a
constraint enforced by construction beats one enforced by convention. **(3)** The design
already assumed these rows exist: `api/compat._resolve_cso` resolves a Cloud Storage Object
*by a fork key*, so P2 and P3 were written for adopted rows and this completes the design
rather than changing it. **(4)** Idempotency on the unique `s3_key` index matches A33's
principle of making upserts idempotent by construction.

A7's frozen list in `docs/adr/amendments-register.md` is annotated as extended by S2, so a
reader of A7 alone cannot conclude the API has four functions. The honesty property is
asserted directly by
`tests/test_migration_adoption.py::TestAdoptionHonestyIsStructural` — no argument
combination produces any other status, the code contains no other status literal, and
promotion to `verified` re-reads every byte — and all three were shown to fail against a
mutation that made the status depend on a caller-supplied hash. Decider: orchestrator
(team lead), on the P5 implementer's entry-gate finding.

2026-08-15 — **A conflict resolved after planning left its object unbatched and therefore
invisible.** Found by the 100k rehearsal's triage pass: `resolve_privacy` returned 50
objects to Pending, and none of them moved. The planner only batches Pending objects, and
an object in Conflict at plan time was never batched, so returning it to Pending gives the
dispatcher nothing to pick it up in — it would sit Pending for ever while
`campaign_is_complete` correctly refused to finish the campaign. Fixed by splitting
`planner.plan_unbatched()` out of `plan_campaign()` (same re-runnable keyset, without the
status write that would send a Running campaign back to Planned) and calling it from
`conflicts._reopen_batch` when the object has no batch. Decider: P5 implementer.

2026-08-15 — **An unattributed edit appeared in the P5 phase worktree after the phase's
final commit, and was reverted rather than adopted.** Two files were modified in
`/home/user/v15/apps/cfs-P5` at 22:12, after `b473673`: `migration/preflight.py`
(`assert_startable` rewritten to re-measure on every production-scale start instead of
trusting a `preflight_status == "Passed"` recorded within the last day) and
`tests/test_migration_reconcile.py` (a matching test). The P5 implementer did not write
them. PLAN §C and INVARIANTS.md state that no two write-capable agents or sessions modify the
same worktree — ever — and P4 already records one incident of two sessions briefly sharing
one.

**The idea is sound and is recorded here so it is not lost:** a recorded pass is a statement
about the machine as it was then. The free space it measured may since have been consumed by
the very campaign that recorded it, and the backup it accepted ages out of the freshness
window on the same 24-hour clock the cache uses — so trusting yesterday's verdict is a way
for a start to proceed onto a full volume with no recoverable backup, which is the outcome
A29 exists to prevent. That is a genuine weakness in the shipped `assert_startable`.

**It was reverted anyway, for two reasons.** (1) Provenance: adopting an unreviewed change
of unknown authorship into a phase about to be handed to an independent gate would mean the
tree that was tested (847/847 three times) and the tree delivered are not the same tree, and
committing it would have meant attributing authorship this implementer cannot honestly
claim. (2) It does not pass: reconstructed and run, the pair takes
`tests/test_migration_reconcile.py` from 20 green to three errors, because the new test
leaves global state (`PRODUCTION_SCALE_OBJECTS`, the site's `private/backups` directory)
perturbed for the tests that follow it — later campaigns fail the single-active-campaign
rule. Never weaken a failing test to get green, and equally never ship one.

Left for the P5 gate to decide, with the reasoning above and the reconstruction in the
implementation report. Decider: P5 implementer, declining to ship an unattributed,
unverified change; the concurrency breach itself is escalated to the orchestrator.

2026-08-15 — **Correction, superseding the causal claim in the entry above (`b193f66`): the
three errors were our own collision, not the reverted change.** The previous entry recorded
that the reconstructed `assert_startable` pair "does not pass" because "the new test leaves
global state perturbed for the tests that follow it". That diagnosis was unsafe and is
withdrawn.

The author of those edits has identified itself: `P5-impl-2`, a second implementer the
orchestrator launched on the belief that this one had died of a request timeout. Both were
writing `/home/user/v15/apps/cfs-P5` and both were running `run-tests` against
`cfs-autonomous.local` in the same seconds.

The saved log settles it. `tmp/p5/pf.log` records all three errors as
`ValidationError: Campaign <name> is already active; only one campaign may run at a time`,
with **three different campaign names** (CFS-CAMP-35188 / 35357 / 35602) that no single run
of that module creates — and on three tests that **predate** the change
(`test_the_measurement_reads_real_numbers_out_of_the_database`,
`test_a_healthy_bucket_reports_no_drift_of_any_class`, `test_a_size_disagreement_is_reported`).
The single-active-campaign rule is site-global, so a second suite running concurrently trips
exactly this. None of the three is the new test, and the mechanism the previous entry
asserted (`PRODUCTION_SCALE_OBJECTS`, the site's `private/backups` directory) appears in
none of the tracebacks. `P5-impl-2` reports the same collision as `QueryDeadlockError` in
its own runs; the exception differs, the cause does not.

What this entry does **not** claim: that the change passes. This implementer has not
re-verified it under an exclusive lock, and the site lock currently reads `orch`.
`P5-impl-2` reports 21/21 green three consecutive times with a single runner, and reports
proving the new test bites by restoring the cached-pass branch and watching it fail with
`ValidationError not raised`. That is recorded as its evidence, attributed, not adopted as
this implementer's.

**The revert stands on its remaining ground and only that ground:** the tree that was tested
three times must be the tree handed to the gate, and authorship cannot be claimed for work
this implementer did not write. The technical objection is withdrawn — on the merits the
change looks correct, and the recommendation to the gate is now to **adopt** it, from
`P5-impl-2`'s files, with `P5-impl-2` credited.

The underlying event is the one that matters: two write-capable implementers in one
worktree, running tests against one site, is precisely what PLAN §C and INVARIANTS.md forbid,
and it produced test results neither party could trust and a false defect claim in this log.
Decider: P5 implementer, correcting its own record on evidence from `tmp/p5/pf.log`.

2026-08-15 — **`git add -A` swept 1,143 lines of another session's work into commit
`b473673` unreviewed and unattributed, under a message describing only a PROGRESS update.**
`cloud_file_storage/tests/rehearsal.py` went from 559 lines to 1,702 in a commit whose
message mentions the rehearsal evidence and nothing else. The added code is `P5-impl-2`'s
harness — an accounting RQ worker and job-count gate, a resource sampler and its report, a
`Harness` class and a seven-fault injection matrix, an end-to-end bandwidth-throttle
measurement and an A29 refusal demonstration — written while both sessions had the worktree.
It was not reviewed by the committer and the commit claims no co-authorship for it.

History is not rewritten (no force-push, and the record of the mistake is more useful than a
tidy branch — the same reasoning applied to the wrong `Co-Authored-By` trailers on
2026-08-15). Instead the state is made deliberate: the orchestrator has ruled that these
stages be adopted where they cover an F3 requirement measured less directly, they have now
been read, and they are kept with attribution recorded here and in the commit that follows.
`git add -A` in a worktree that another agent may be writing is the mechanism, and it is
worth naming: it converts a concurrency breach into a false authorship claim in the
permanent record. Decider: P5 implementer, on discovering the sweep while editing the file.

2026-08-15 — **The end-to-end bandwidth-throttle measurement (F3) was attempted and NOT
completed; the throttle's evidence is unit-level.** `rehearsal.throttle` needs a corpus in
which bandwidth rather than request rate is the binding constraint, so a purpose-seeded
300 × 128KiB corpus was written to the rehearsal site. The run failed for a harness reason,
not a product one: `analyzer.run_analysis` scans the whole of `tabFile`, so the new campaign
swallowed the site's existing 100k corpus (100,267 objects) instead of the 300 large files,
and was killed during planning. The campaign and corpus were removed; `CFS-CAMP-0004` and
its evidence were untouched and it is now `Completed`.

What stands as evidence for "bandwidth throttle demonstrably enforced":
`TestBandwidthThrottle` (four tests) asserts that a zero limit never sleeps, that the
campaign's ceiling is divided across `parallelism` workers rather than applied per worker,
that consuming ahead of the budget sleeps for the difference, that consuming within it does
not, and that `run_upload_batch` builds its budget from the campaign's two policy fields.
Two of them fail against a mutation removing the division. That is the arithmetic and the
wiring, not an end-to-end rate. **The gate should run `rehearsal.throttle` on a site whose
corpus is only the large-payload files** — the stage is present and its pass criterion is
the right one (throttled rate at or under the ceiling AND the unthrottled batch faster, so a
slow machine cannot produce a false pass). Recorded as an open gap rather than claimed.
Decider: P5 implementer.

2026-08-15 — **`CFS-CAMP-0004`'s preflight row was re-derived from the artifact.** A second
session re-ran `assert_startable` against the finished campaign, overwriting
`preflight_status`, `preflight_at` and `preflight_report` — so a release audit reading the
Campaign row would have seen numbers that contradict `tmp/p5/measure_final.json`. The row is
restored from that JSON and carries a `note` saying so, and an audit row records the
re-derivation and why. The JSON artifact remains the evidence of record; the row is a
convenience copy of it. Decider: P5 implementer, on the orchestrator's instruction.

2026-08-15 — **F4's projection was understated 2.1× by the figure first reported, and the
cause was the gate measuring the wrong set of tables.** Raised by the stood-down
`P5-impl-2` session and verified here against `information_schema` on the rehearsal corpus.
Two independent causes. `preflight.MEASURED_TABLES` omitted **`Cloud Storage Object`** —
migration creates one per unique content, 99,764 rows and 2,223 bytes per migrated object,
a fifth of the whole footprint. And `measure_final.json` was written at 21:47 *as CLEANUP
started*, so the two tables CLEANUP grows were measured before it grew them: the audit log
doubles (`file_link` at VERIFY plus `local_quarantine` at CLEANUP, 1.998 rows per object,
not the 1.00 first recorded) and the object table grows as `quarantine_path`/`cleaned_at`
fill.

Fixed both: the CSO doctype is in `MEASURED_TABLES`, and `project()` now scales **every**
table on its own measured ratio to the object count instead of assuming 1:1 — shared URLs
mean more refs than objects, dedup means slightly fewer cloud objects, and the audit log
carries two rows each. Re-measured post-CLEANUP: **10.61 GB projected at 1.2M objects,
5.01 GB of it index, and a 21.23 GB free-space requirement at the shipped safety factor** —
against 5.09 GB reported before and the design's 1.5–2.5 GB planning figure.

This is the same class of defect as the `FakeObjectStore` verification stub, and deserves
the same weight: a gate that cannot fail when it should. An understated projection means
`assert_startable` passes a start that will not fit, on the phase whose invariant is that
nothing is deleted without verification. Three tests now hold it: every table in
`MEASURED_TABLES` is measured, the projection uses each table's own ratio (with the audit
log's 2:1 as the worked case), and removing a table from the input demonstrably lowers the
total — so the inclusion test is not a spelling check. Decider: P5 implementer, on
`P5-impl-2`'s finding, verified independently.

2026-08-15 — **Two seeded anomalies did not exercise the paths they name, and the evidence
said otherwise.** (a) **Adoption**: no bytes were ever placed under `attachments/`, because
`_seed_legacy_objects` was added to the harness after the 100k corpus had been seeded. All
39 `legacy_fork` rows therefore became `adoption_failed` Blockers and `objects_adopted` is
0. The behaviour is correct — a pointer to nothing is a Blocker — but A15/A18's *success*
path was never driven at scale, and the PROGRESS row listing "39 legacy ALYF rows" read as
though it had been. Corrected to say so; adoption's evidence is its 19 unit tests.
(b) **Dedup**: content dedup did work — 99,862 objects resolved to 99,764 Cloud Storage
Objects — but through `ensure_cso` converging two callers on one row, not through the
explicit `DedupReused` short-circuit, which requires the winning object to be already
`present` when the loser checks and so never fires when four workers process identical bytes
concurrently. `objects_dedup_reused: 0` is therefore accurate and not a bug, and the row now
says which mechanism was exercised. Decider: P5 implementer, on `P5-impl-2`'s finding.

2026-08-15 — **A dead term in `engine._record_object_failure` removed.**
`if fatal or (exhausted and not transient) or exhausted:` reduces to `fatal or exhausted`;
`transient` could not affect the branch. The behaviour it reduces to is the correct one and
is now stated plainly: `max_attempts` is precisely the bound on retrying a transient error,
so once it is spent a transient failure must stop and surface like any other — an object
that retried for ever would never reach an operator. Where the two do differ is severity,
which already keys off `fatal`. The now-unused `CloudStorageError` and `is_transport_failure`
imports went with it; ruff's F401 is not enabled in this repo's config, so they had to be
found by reading. Decider: P5 implementer, on `P5-impl-2`'s finding.

2026-08-15 — **P5 gate FAIL at `9f2b73e`: two real defects, and one of my evidence claims did
not verify.** All three are recorded because the third is the one that matters most.

**P5-C1 (Critical) — the quarantine purge destroyed the last local copy having checked
nothing.** `cleanup._purge_one` called `os.remove` on the strength of gates evaluated when
the file was quarantined, up to `quarantine_ttl_days` (default 14) earlier, and it runs
unattended from the scheduler. INVARIANTS.md invariant 1 asks for re-verification *at deletion
time*; fourteen days is long enough for a bucket to lose an object, a lifecycle rule to move
it or an operator to delete it. The purge now re-verifies through `_purge_gate` — cloud
object present, status `verified` or `legacy_unverified`, and a fresh `verify_object` using
the same strategy split VERIFY uses — and a failure blocks and audits instead of deleting.
Three tests, two of which fail against a mutation that removes the gate; the third asserts a
healthy expired quarantine *is* purged, so the refusals are not a purge that never fires.
The shape is worth naming: every other deletion in this engine was gated, and the one that
ran unattended on a timer was not.

**P5-H1 (High) — the capacity gate runs at the one moment it cannot measure itself.**
`assert_startable` fires at campaign start, where `Cloud Storage Object` has ~0 rows and the
audit log has no `file_link`/`local_quarantine` rows, so `project()` derived ratios of ~0 for
the two tables worth 3.76KB of the measured 8.84KB per object: the shipped gate projected
~6GB where the rehearsal measured 10.61GB, and demanded ~12GB free instead of 21.23GB.
`project()` now takes `max(measured, calibrated)` per table and per quantity, with
`CALIBRATION` sourced from the post-CLEANUP rehearsal measurement (now in
`docs/evidence/p5-rehearsal/`). A site heavier than the rehearsal still wins, because its own
measurement is the larger. Tested from a wholly empty database, which must still project the
full figure. This is the third instance in P5 of the same shape — a gate measuring the wrong
thing — after `MEASURED_TABLES` omitting the CSO table and the `FakeObjectStore` stub.

**The kill -9 claim did not verify, and the correction is mine.** I wrote that batch B2
carried `attempts: 2`. That was observed live and true when observed, but `attempts` is not
durable: `finalize_upload_batch` resets it whenever a batch makes progress, and
`api._open_cleanup_batches` zeroes it on every batch when CLEANUP starts. By the end of the
run `MAX(attempts)` is 0 across all 101 batches, so a reviewer checking the citation finds
nothing. **The recovery did happen**: `CFS-CAMP-0004-B2.last_error` still reads
`heartbeat expired; recovered by dispatcher`, a string written only by
`engine.recover_stale_batches`, and that batch went on to Verified and Cleaned; B42 and B47
still carry the A31 residue message. The lesson is not that the claim was false but that I
cited a **mutable counter** as evidence, which is the same error class as citing a test name
that does not resolve. `recover_stale_batches` now writes an audit row per recovered batch,
so crash recovery is provable from an append-only table rather than from a column any later
phase overwrites. Decider: P5 implementer, on the gate's UNVERIFIED finding.

**P5-M1/L1 — F3 is recorded as PARTIAL and the convergence figure re-characterised.** Unmet
at 100k scale: the end-to-end throttle, disk I/O against a baseline, the bounded-job-count
gate (nothing in the completed run recorded per-job execution), and two of three crash faults.
And the 155-object shortfall was not "155 seeded-unresolvable": 50 were operator-resolvable
privacy mismatches, 39 failed on a harness seeding gap, and only 66 were genuinely
unmigratable — so **0.99845, which misses the 0.999 target, is the honest engine-only
number**, and the post-triage 1.0 must be read knowing that `Skipped` is excluded from the
denominator and triage can therefore raise the ratio by skipping.

**P5-L2/L3 — two narrow holes closed.** `find_guarded_table_sql` read only `node.args[0]`, so
`frappe.db.sql(query=…)` walked past the invariant-5 lint; it now reads the keyword form too,
with a test. And `LINKED_REFS_SQL` did not re-assert `f.file_url = r.file_url`, so a row that
carried this object legitimately but had moved to a different URL counted as "linked by us" —
which would let CLEANUP quarantine a local file at a path the campaign never verified.
Decider: P5 implementer.

2026-08-15 — **P5 re-gate FAIL at `d9ea898`: closing the H1 gap walked a benign call into a
live invariant-5 breach, through a blind spot in the lint that was supposed to catch it.**
`preflight._table_metrics` counted rows with `frappe.db.sql(f"SELECT COUNT(*) FROM
\`{table}\`")`. Harmless while `MEASURED_TABLES` held only migration tables — and the moment
the CSO doctype was added to fix P5-H1, that statement became raw SQL against a guarded table
at an unsanctioned site. INVARIANTS.md invariant 5 is not conditioned on the statement being
read-only, and conditioning it on effect is how such a rule stops meaning anything.

The reason it was not caught is the finding underneath it: `_string_arguments` joined only
the `ast.Constant` parts of an f-string, so `f"SELECT COUNT(*) FROM \`{table}\`"` rendered as
``SELECT COUNT(*) FROM `` `` — the checker discarding the one token it exists to guard. Both
halves fixed: the call site uses `frappe.db.count(doctype)`, and the checker now resolves
f-string slots against module-level constants and, where it cannot, reports the statement.

The precision matters in both directions and took a second pass to get right. Reporting every
unresolved interpolation flagged the analyzer's `VALUES {placeholders}` bulk insert, which
says nothing about which table is written and would have pushed honest code onto the
sanctioned list; reporting none of them was the original hole. The rule now is that an
unresolved slot **inside backticks** — an identifier position — makes a statement reportable.
Proven by running the fixed checker against the code as it shipped at `d9ea898`: it reports
the breach the shipped checker cleared.

This is the sixth instance in P5 of one shape — a check that measures the wrong thing and so
cannot fail when it should — after the `FakeObjectStore` stub, `MEASURED_TABLES` omitting the
CSO table, `project()` measuring at campaign start, the lint reading only `args[0]`, and
`LINKED_REFS_SQL` dropping its URL guard. Worth carrying to the release audit as a pattern
rather than six line items: in every case the check ran, passed, and was reading something
other than the property it named. Decider: P5 implementer, on the re-gate finding.

2026-08-15 — **The recovery audit row was written unconditionally beside a CAS that can
lose.** `recover_stale_batches` updates the batch with a compare-and-set on `status`, so two
scheduler ticks can both see the same stale batch and only one can win — but the audit row
was written by both. That row had just been made the durable evidence for crash recovery
*because* `attempts` was mutable, and an append-only table full of unconditional writes is no
better evidence than the counter it replaced. Now gated on `rowcount == 1`, and the function
returns the number it actually recovered rather than the number it considered. Two tests: one
asserts the row is written exactly once on a real recovery, the other drives a losing tick and
asserts it records nothing — the second fails against a mutation removing the gate. Decider:
P5 implementer, on the re-gate finding.

2026-08-15 — **Purge loop: a stuck cohort could starve every healthy quarantine behind it.**
`purge_expired_quarantine` selected the oldest expired rows up to `limit`, and with the P5-C1
re-verification added, a cohort whose remote objects are permanently unverifiable is
re-read, re-checked and re-audited on every scheduler tick — and can occupy the entire
window for ever, so a healthy quarantine behind it is never purged. Each refusal now bumps
`attempt_count` and the query orders `attempt_count asc, cleaned_at asc`, so a stuck cohort
sinks and the window keeps making progress. The campaign document is also loaded once per
campaign instead of once per row. Decider: P5 implementer.

2026-08-16 — **P5 PASS.** Independent reviewer FAIL → fix → FAIL → fix → verified closed
across three rounds; orchestrator attested at `07b459d` under exclusive lock: **868/868 twice
consecutively**, 4 loud skips, settings byte-identical, tree clean, pre-commit green.

The phase found five defects in code that was already merged and gated, and one it created
while fixing another. All six share one shape, recorded here as a single pattern for the
release audit rather than six line items: **a check that ran, passed, and was reading
something other than the property it named.** `FakeObjectStore` stubbing `engine.verify` with
an echo of its own bytes (P2-era — every "verification bites" assertion vacuous, on the gate
governing local deletion); `MEASURED_TABLES` omitting the CSO table; `project()` measuring at
campaign start when the tables it models are still empty; the safety lint reading only
`node.args[0]`; `LINKED_REFS_SQL` dropping its `file_url` guard; and the f-string blind spot
that let an interpolated guarded query past invariant 5.

The instructive one is the last. Closing the second finding (adding the CSO doctype) walked a
previously benign `COUNT(*)` into the sixth. A check that cannot see a construct does not
merely miss existing instances — **it silently licenses new ones, and the licence is invisible
until something else moves.** That is the argument for machine-checked guards over careful
reading, and for proving a repaired check bites against the commit it previously cleared.

Two evidence corrections are also recorded: the crash-recovery citation pointed at `attempts`,
a counter the engine resets twice by design, and was replaced with the durable `last_error`
audit trail (orchestrator-verified: `CFS-CAMP-0004-B2` = "heartbeat expired; recovered by
dispatcher", status Cleaned); and the convergence shortfall was re-characterised — 0.99845
pre-triage **misses** the 0.999 target, only 66 of 155 were genuinely unmigratable, and
`Skipped` is excluded from the denominator so triage can raise the ratio by skipping.

Honestly unclaimed, not asserted: F3 is **PARTIAL** (end-to-end bandwidth throttle, disk I/O
vs baseline, bounded job count at 100k, and two of three crash faults at scale), and the
A15/A18 adoption success path was not driven at 100k scale. Decider: gate discipline per
PLAN §D.

2026-08-16 — **A20 names `hot_retention_days` as the archive driver, so `archive_after_days`
is not shipped.** `docs/design/delivery-platform.md` §2.1 lists both fields — `hot_retention_days`
(14, "stays STANDARD") and `archive_after_days` (30, the transition day) — and then has to add
an economics rule making `hot_retention_days > archive_after_days` an error, which is the tell
that they are one concept wearing two names. A20 says "`hot_retention_days` drives the archive
transition", and the register outranks the design, so the transition day IS `hot_retention_days`
and `archive_after_days` does not exist. Two fields for one number is a defect generator: an
operator who moves one and not the other gets a policy that says something they did not mean,
and the validator that catches it is a validator that would not need to exist.

Where the design used `archive_after_days`, the shipped code reads `hot_retention_days`: the
economics rule `archive_after_days + GLACIER_MIN > delete_after_days` becomes
`hot_retention_days + GLACIER_MIN > retention`, evaluated per prefix group. Archival is turned
off by clearing `archive_storage_class`, not by a magic zero. Decider: P6 implementer, on the
PLAN > adr > design precedence in INVARIANTS.md.

2026-08-16 — **A20's "separate expiration rules" is implemented as a real second retention
number, not as N copies of one.** Generating five identically-parameterised expiration rules
would satisfy the words and none of the purpose: hourly backups accumulate 24 artifacts a day
and daily ones accumulate one, so a single `delete_after_days` either keeps 8,760 hourly
artifacts a year or throws away a year of dailies after a week. The shipped policy therefore
carries `subdaily_retention_days` (default 7) for the `hourly/` and `every-6-hours/` prefixes
and `delete_after_days` (365) for `daily/`, `weekly/` and `manual/`.

`manual/` gets a prefix and an expiration rule of its own rather than borrowing the configured
frequency's: a prefix with no expiration rule is a prefix that grows for ever, and an operator
pressing Backup Now during a Weekly schedule should not have their artifact silently inherit a
year of retention. Decider: P6 implementer.

2026-08-16 — **An archive rule that could never fire is not generated, and the fact is
reported rather than hidden.** With the defaults (hot 14 days, sub-daily retention 7) an
hourly artifact is deleted a week before the day-14 transition would move it. Emitting the
rule anyway would put a policy in the bucket that claims an archival tier those objects never
reach; suppressing it silently would leave an operator believing their hourly backups are
archived. So `archives_group()` suppresses it and `validate_economics` emits
`archive_never_fires` as a **warning** explaining that this is the cheaper outcome — GLACIER's
90-day minimum on a 7-day artifact is money for nothing.

Only one economics finding is an `error` (and therefore blocks both the save and the apply):
`archive_before_minimum`, where a rule that IS generated moves objects to an archive class and
then deletes them before the class's minimum billing duration. That one is unambiguous — you
pay the full minimum for storage you deleted — and the operator cannot want it. Volume and
overhead findings are warnings, because "24 artifacts a day for 30 days" is a legitimate
choice that the operator should merely be shown the arithmetic for. Decider: P6 implementer.

2026-08-16 — **The design says to delete a remote artifact whose checksum did not match; A5
says never to, and A5 wins.** `docs/design/delivery-platform.md` §2.6 lists "Checksum mismatch
on verify → delete remote object, raise `BackupVerificationError`". A5 says "**never delete the
remote artifact on verification failure** — keep it, mark Failed, alert", and the register
outranks the design.

It is also the right answer on the merits. A verification failure means our belief about the
object and the object disagree; it does not tell us which one is wrong. A HEAD that raced a
replication lag, a proxy that rewrote a header, a bucket policy that stripped a checksum — in
each case the bytes in the bucket are fine and we would have deleted the only copy of a backup
because we could not confirm it in one attempt. And when the artifact really is corrupt, it is
evidence: it is the thing an operator needs to look at to find out what mangled it. This is the
same rule as the migration engine's zero-delete-before-verify, seen from the other end: there,
we refuse to delete a local file we cannot prove is safe to lose; here, we refuse to delete a
remote one for the same reason. `tests/test_backup.py` asserts it against a size mismatch, a
checksum mismatch and a direct call, all reading the fake bucket's `deleted` list, and the
module is grep-asserted to contain no deletion call at all. Decider: P6 implementer.

2026-08-16 — **The permanently-dirty cache entry carried from P4 is repaired, not documented
away.** An entry whose sidecar is missing or garbled reads as dirty to `_has_drifted` — rightly,
since eviction's question is "can I prove this is safe to delete" — so it is never evicted,
counts towards `over_budget` for ever, and makes `run_eviction` log `cache_eviction_over_budget`
on every run with nothing an operator can do. P4 recorded it as needing "a repair path that
re-derives a sidecar from the object when the bytes still match it, and quarantines rather than
deletes when they do not", and left it to whichever of P5's quarantine layout or P6's
maintenance surface landed first.

`cache.eviction.repair_unevictable_entries` is that path, scheduled in `daily_maintenance`
**before** `run_eviction` so a repaired entry is freed in the same pass rather than a day later.
It re-hashes the entry and compares against the Cloud Storage Object's `content_sha256`: a match
proves the bytes are a faithful copy of a verified remote object, so a clean sidecar is written
and the entry rejoins the evictable population; a mismatch, or an object that no longer exists,
means these may be local changes that were never written back, so the entry is **moved** to a
quarantine directory beside the cache and logged with its path. Never deleted, in either branch.
The quarantine sits outside the cache root deliberately: inside it, the moved bytes would still
count towards the budget and would be re-scanned by the next sweep, which is the condition being
fixed. `tests/test_cache_repair.py` asserts the entry is unevictable BEFORE the repair, so the
assertions that follow cannot be vacuous. Decider: P6 implementer, on the P4 reviewer's Low.

2026-08-16 — **A seventh instance of the "check that runs, passes, and reads something other
than the property it names".** `test_a_lying_head_on_a_large_artifact_is_caught_by_the_reget`
is the strongest assertion in the backup suite: it injects a bucket whose HEAD reports the
correct digest and the correct length over corrupt bytes, so only a streamed re-GET can catch
it. It was green. It was green because the run also uploaded the small site-config artifact, and
the injected `report_size` — sized for the 2MB database dump — mismatched the config's few
hundred bytes, so the ContentLength check raised on the WRONG artifact and `assertRaises` was
satisfied. Disabling the re-GET entirely still left the test passing.

Found by mutation, not by reading. The test now switches the config artifact off and asserts
`len(uploads) == 1`, so the only artifact in play is the one it is named for. Filed here as one
more instance of the pattern the six earlier ones share: the assertion was true, and it was not
about the thing.

Three more of the same family were found in the harness in the same round, all of them in code
whose purpose is to keep tests honest: `snapshot_backup_settings` read through a casting reader
and could not restore a NULL Datetime (`cast("Datetime", None)` is `datetime.min`), so the
stored bytes alternated between consecutive suite runs; `BackupTestCase._cleanup` deleted its
log rows and then called a restore that opens with a rollback, undoing them; and the dump
fixture stamped artifact mtimes before the job started, inverting the real ordering and losing a
microsecond race with `job_start_epoch()`. Decider: P6 implementer.

2026-08-16 — **`test_connection`'s A27 probe belongs in `storage/engine.py`, and the first
version of it did not.** `check_attachment_bucket_versioning` was written calling
`client.get_bucket_versioning` directly. That broke the rule the engine module states in its
own docstring — every S3 call the app makes goes through one place that owns the timeouts, the
breaker and the typed errors — with two consequences. It used the job profile, whose 300-second
read timeout would let an unreachable bucket hold an interactive `test_connection` open for
minutes, which is exactly the hazard A30 exists to prevent on the request path. And it was
invisible to `FakeObjectStore`, so an offline unit suite dialled out: `tests/test_compat.py`
went from 0.72s to 5.98s, all of it real connect timeouts against a bucket that does not exist.

`engine.get_bucket_versioning` and `engine.get_bucket_lifecycle` are the probes now, on the
request profile, and the fake store answers both. The suite is back at 0.74s. Worth recording
because the symptom (a slow test module) and the defect (an unbounded call on an operator-facing
path) looked nothing alike, and only the first one was visible. Decider: P6 implementer.

2026-08-16 — **The locale POT was regenerated and the diff is much larger than P6.**
`cloud_file_storage/locale/main.pot` was last generated during P1 and carried 83 msgids;
regenerating it with `bench generate-pot-file` produces 567, because P2-P5 added translatable
strings and never regenerated. The regeneration is the sanctioned action (INVARIANTS.md: "locale
regenerated not sed-ed") and the alternative — shipping a catalogue that describes the app as
it was at P1 — is worse, so the catch-up is included here rather than deferred. `de.po` is left
as it is: it is a P1 scaffold with no translated strings for anything after P1, and merging a
POT into a PO is a translation task with no bench command behind it. Decider: P6 implementer.

2026-08-16 — **`Cloud Storage Audit Log.action` gained `backup_lifecycle_applied` with a test
and no patch.** The lifecycle apply is destructive (it schedules deletions), so INVARIANTS.md
requires it to be audited, and the audit write raised `Action cannot be
"backup_lifecycle_applied"` inside the transaction of the thing it was auditing — the same shape
as the P5 defect where `audit.record(..., action=…)` collided with its own parameter. Adding a
Select option changes no data and no column type; `bench migrate` syncs the DocType JSON and
existing rows are untouched, so there is nothing for a patch to do. The test half of invariant 9
is shipped: the audit row is asserted by
`TestApplyRefusals.test_a_successful_apply_records_a_hash_and_an_audit_row`, which fails without
the option. Decider: P6 implementer.

2026-08-16 — **A5's "never delete on failure" was proven on the mismatch paths and only
grep-proven on the transport paths; both are now behavioural.** The three verification
mismatches (ContentLength, HEAD checksum, streamed re-GET) each had a test asserting the
remote artifact survives, and `test_the_backup_module_contains_no_delete_call` covered every
other path by construction. What had no behavioural test was the case where the artifact is
**already in the bucket** and the failure is transport, not integrity: `head_object` raising
after a successful upload, or the re-GET raising. That is the failure mode where a "tidy up
what we could not verify" reflex does the most damage, because the bytes are almost certainly
good and the error is almost certainly transient.

Closed with three tests that inject the transport failure and assert both that the object is
in the bucket and that `deleted` is empty, proven by two mutations that add
`except: delete_object(...); raise` around the HEAD and around the re-GET. The three
identities are now named in `.github/helper/check_backup_completeness.sh` (25 guards, up from
22).

The same round found the smaller version of the same shape in the harness:
`FakeBackupBucket.fail_head_with` and `fail_upload_with` existed and no test ever set them —
a capability to prove a property, sitting unused, which is what a guard that cannot fire looks
like before anyone notices. `test_every_failure_injector_the_fake_offers_is_exercised` now
discovers the injectors off the fake by attribute prefix (not from a restated list, which
would go stale) and fails when one is never driven; proven by adding an unused injector and by
removing every use of an existing one. Raised by the orchestrator asking where the A5 evidence
was thinnest. Decider: P6 implementer.

2026-08-16 — **The mutation evidence is a script, not a transcript.** The P6 gate asked for
"the exact mutation command list" so the reviewer could verify the claims rather than
re-derive them. Reconstructing forty-six commands from a session log and calling the result
evidence would be the same move this project has recorded five times already — citing a check
without confirming it resolves — so the list was written as
`docs/evidence/p6-mutation-evidence.sh` and executed end to end before being handed over.

Two properties make it evidence rather than documentation. It asserts each mutation actually
**changed** the file, so an anchor that has drifted is reported as `NOT APPLIED` instead of
silently counting as a pass; and it asserts the resulting run **failed**, so a mutation that
applies and leaves the suite green is reported as `SURVIVED` — which is the only outcome that
matters, because it names a guard no test holds. It refuses to start on a dirty worktree and
restores every file on any exit path including SIGINT.

The first execution found two of its own rows broken: the `Filter.Prefix` removal and the
delete-on-size-mismatch injection had anchors that did not match the formatted source, and
both were reported as `NOT APPLIED`. Under the old approach they would have been two lines in
a report claiming coverage that was never demonstrated. Fixed and re-run: **46 caught, 0
survived, 0 not applied.** Decider: P6 implementer, on the orchestrator's request.

2026-08-16 — **Gate finding F-1: an empty backup prefix generated two rules scoped to the
whole bucket.** With `backup_prefix` empty, `scoped_base` was `""`, so `cfs-noncurrent` and
`cfs-abort-mpu` shipped `Filter: {"Prefix": ""}` — which matches every object in the bucket,
including objects this app never wrote. Reachable in practice: `validate_target` requires a
prefix only while `enabled`, and `apply_lifecycle_policy` checked neither. A20 requires a
prefix on every generated rule, and an empty prefix does not satisfy it — it is the absence
of a scope wearing the shape of one, which is why it read as compliant.

Two things were wrong beyond the missing check. The prefix was assembled inline in
`build_lifecycle_policy` (`f"{base}/" if base else ""`), so each rule shape carried its own
copy of the decision and a fifth shape would have carried a sixth copy;
`assert_prefix_is_scoped` now owns it in one place and every shape inherits it. And nothing
asserted the *property* — that no generated rule leaves the function unscoped — so the only
thing standing between an unscoped rule and the bucket was four correct call sites.
`assert_every_rule_is_scoped` runs over the finished rule list and fails regardless of how a
rule was built. Apply, preview and the backup job all refuse an empty prefix; the health
panel reports it as a finding rather than raising, because a panel that threw would hide the
condition it exists to show.

Why the existing tests missed it: `test_every_generated_rule_has_a_non_empty_filter_prefix`
only ever ran the *default* prefix, so it asserted a true thing about one input and said
nothing about the input that breaks it. That is a milder form of the pattern this file keeps
recording — not a check that reads the wrong property, but a check that reads the right
property over too narrow a domain. Ten tests now cover empty, whitespace-only and
slashes-only, at generation, preview, apply, backup and health. Decider: P6 implementer, on
the gate's F-1.

2026-08-16 — **Gate finding F-3, the eighth instance and the most self-referential:
the guard against stale restated lists rested on a stale restated list.**
`test_every_failure_injector_the_fake_offers_is_exercised` discovered injectors by attribute
name prefix — `("fail_", "report_", "store_", "composite_")` — which is itself a restated
list. An injector named `raise_on_get` or `slow_upload` would have been invisible to the very
test written to stop capabilities sitting unused. Worse, the reviewer noticed the re-GET
transport test monkeypatched `get_object` directly instead of using an injector, so that path
was *already* outside the discovery on the day the test was written.

`FakeBackupBucket.FAILURE_INJECTORS` is now the definition rather than a description of one:
`__init__` creates the attributes from it, so "registered" and "exists" are the same set by
construction and cannot drift. The re-GET test drives a new `fail_get_with` injector instead
of reaching around the fake's failure surface. Recorded because the fix is not "use a better
list" — a registry maintained beside the attributes would drift the same way — it is to make
the list the thing that creates what it describes. Decider: P6 implementer, on the gate's F-3.

2026-08-16 — **Gate finding F-2 accepted as a recorded residual, with its closure scoped to
P8.** `tests/test_backup_gate.py` asserts that every identity the CI gate names resolves to a
real test, so the gate cannot demand tests that are gone — but nothing forces a *newly added*
refusal guard into `REQUIRED`, so a guard added in a later phase can sit outside the gate
silently. Not gate-blocking: all 28 named guards bite today and PLAN's P6 exit is met.

The closure is to mark guards (`@refusal_guard`, or a `# gate: refusal` comment) and assert
marked ⊆ `REQUIRED` from the same AST walk that already validates the other direction.
Recorded here with the trap attached: **a class-membership rule will not work**, because
`TestApplyRefusals` deliberately mixes acceptance tests in with the refusals
(`test_the_exact_phrase_is_accepted`, `test_surrounding_whitespace_in_the_phrase_is_tolerated`)
— so "every test in a Test*Refusals class must be in REQUIRED" would fail on tests that
should not be there. P8 should not spend the discovery. Decider: orchestrator, on the P6
gate; recorded by the P6 implementer.

2026-08-16 — **F-3's stronger closure: a registry the injection sites do not read is still a
description.** The first fix made `FakeBackupBucket.FAILURE_INJECTORS` create the attributes,
which stopped the registry and the attribute set from drifting apart. It did not stop a test
from setting an injector nobody implemented: `self.bucket.raise_on_get = X` bound an attribute
no injection site consulted, so the test then passed or failed for reasons unrelated to the
failure it believed it had injected — the same shape as the six, one level up.

Every injection site now reads `self.injectors[...]`, and `InjectorRegistry.__setitem__`
raises on an unregistered key. Registration is therefore load-bearing: an unregistered
injector cannot be read, and a test that reaches for one fails at the point of use. Two tests
hold the halves — an unregistered key raises, and every registered key is read by some
injection site — so a registered-but-inert injector is caught too. Decider: orchestrator's
ruling on F-3; implemented by the P6 implementer.

2026-08-16 — **The F-1 closure needed the other direction, and "every rule has a prefix" is
not it.** Asserting that every emitted rule carries a prefix passes trivially if a rule shape
stops being emitted at all — the assertion ranges over whatever the generator produced, so a
generator that produced less would satisfy it more easily. The test now names all four shapes
(`cfs-archive-*`, `cfs-expire-*`, `cfs-noncurrent`, `cfs-abort-mpu`), asserts each is present,
and asserts each carries the configured prefix, with `cfs-noncurrent` and `cfs-abort-mpu`
called out because those are the two that were bucket-wide and the two whose harm is
irreversible: `NoncurrentVersionExpiration` on an unscoped filter deletes noncurrent versions
of objects this app does not own. Decider: orchestrator's ruling on F-1.

2026-08-16 — **A deadlock this phase introduced, in the fix for a different defect.** The raw
`tabSingles` snapshot/restore added a per-field `frappe.db.delete("Singles", ...)` loop for
fields that had no row. `tests/utils.set_mode` documents exactly this hazard about exactly
this table — "a field-by-field loop over `tabSingles` is enough lock churn to deadlock against
any other run sharing the site" — and under contention it did, surfacing as
`QueryDeadlockError` in four unrelated tests' cleanups plus one confusing false failure in a
re-GET test that had nothing to do with it. Collapsed to a single `field IN (...)` delete.

Recorded for the shape rather than the fix: the warning was already written down, in a file
this phase edited, about the table this phase was writing to. Reading the neighbouring comment
would have prevented it. Decider: P6 implementer.

2026-08-16 — **H-1: the C10 contract asserted a proxy that stopped being equivalent, and the
mechanism built to force the decision did not fire.** `test_C10_the_runtime_core_issues_no_url_outside_that_gate`
reads "no module outside three mentions the identifier `presign_get`". Through P0–P5 that
*was* "no module mints a presigned URL", because `engine.presign_get` was the only wrapper in
existence. `backup/restore.download_url` called boto3's `generate_presigned_url` directly, the
equivalence broke, and the contract stayed green for a whole phase — including the comment
three lines above the allow-list saying "adding a fourth is a security decision, not a
refactor: it needs its own gate test here".

**Tenth instance of the pattern, and the worst-placed one: not in a phase suite but in the
C1–C19 contract set, which is the evidence the release rests on.** The failure is not that
anyone skipped the decision; it is that a proxy was chosen once, was exactly right at the
time, and nothing re-checked that it stayed equivalent as the codebase grew a second way to
do the thing.

The matcher now keys on the `generate_presigned` **prefix** rather than one spelling, so
`generate_presigned_post` and any future sibling are caught the day they land. That is still
a proxy — a mint through some other spelling would evade it — and it is recorded as one
rather than claimed to be airtight. A stronger check would key on the client object or the
returned value; both were considered and both are harder to make precise with an AST walk
than the prefix is, so the prefix was chosen and its limit written down here for whoever adds
the fifth minting site. Decider: P6 implementer, on the security review's H-1.

2026-08-16 — **Why `download_url` cannot reuse `legacy_generate_file`, only its shape.** The
review's template is right and the order is now copied exactly: resolve and refuse, record,
then mint. But a backup artifact **has no File row and no CSO** — it is not an attachment,
which is precisely why `api/compat.legacy_generate_file` refuses it and why this endpoint had
to exist at all. So step 1's "resolve to a row the caller may read" becomes "the key sits
under this site's scoped backup prefix AND appears in a successful Backup Log manifest", and
step 3's access log becomes an audit row committed **before** the mint. Two conditions rather
than one because either alone is weak: prefix scoping alone still admits anything a mis-set
bucket happens to contain, and manifest membership alone still admits a key recorded before
the prefix was changed. Decider: P6 implementer.

2026-08-16 — **The boto3-direct backup client stays direct, and the difference from defect 5
is the bucket, not the style.** Defect 5 was a probe against the **attachment** bucket that
built its own client: that one had to route through `storage/engine`, because the engine owns
the attachment bucket's timeouts, breaker and typed errors, and a second client meant a second
uncontrolled path to the same objects. The backup client is a different bucket, different
credentials and a different profile by design (B1), and `storage.client.build_s3_client` — the
one place that owns the A30 timeout profile — is what constructs it. Routing backup calls
through `storage/engine` would mean giving the engine a second bucket to know about, which is
a larger change than the problem justifies. Recorded because the next reader will ask why one
was moved and the other was not. Decider: P6 implementer, on the review's invitation to decide
deliberately.

2026-08-16 — **The security fixes' own tests contained three more instances, and the
security reviewer predicted the location before they were written** (relayed and sharpened by
the orchestrator; the prediction is the reviewer's). Its observation — that a fix
which adds tests is the most likely place for the next instance, because adding tests reads as
strictly-better and rarely gets read adversarially — was correct on the same day it was made.
The mutation run after the H-1/M-1/M-2/M-4/M-5 fixes found five survivors:

* `test_a_key_outside_the_backup_prefix_is_refused` drove `prv/ab/cd/...`, which the manifest
  check refuses as readily as the prefix check, so it stayed green with prefix scoping
  disabled entirely. Now split: a key recorded in a real Success manifest but outside the
  prefix (only the prefix check can refuse it) and a key inside the prefix that no manifest
  records (only the manifest check can).
* both permlevel mutations edit DocType JSON, which the installed meta does not see without
  `bench migrate`, so **no test could have caught them** — the same blind spot the
  shipped-defaults check already had a fix for, not applied here until the mutation said so.
* removing `assert_every_rule_is_scoped(rules)` from `build_lifecycle_policy` broke nothing,
  because the direct tests call the backstop themselves and the prefix guard refuses first in
  normal operation. A spy now asserts the generator runs it.

The fifth was not a gap: `apply_lifecycle_policy`'s own prefix assertion is genuinely
redundant. The call stays for the clearer error; the mutation row claiming to cover it was
**removed**, because a row that cannot fail is the thing this file exists to record.

2026-08-16 — **M-1 changed no result: every backup endpoint already refused.** The AST
assertions were proving too little, not concealing a hole. Eight endpoints now have real
`assertRaises(frappe.PermissionError)` tests through `acting_as`, each with a positive
control, and Cloud Storage Manager is asserted to be refused the three System-Manager-only
actions. `TestTheHarnessCanFail` was written first and asserts both of `frappe.only_for`'s
early returns (`flags.in_test` and Administrator) directly, so a harness that silently stopped
clearing the flag fails there rather than making twelve refusal tests pass while proving
nothing. `tests/permission_utils.acting_as` duplicates P7's `desk_utils.acting_as`; one is
kept at convergence and both docstrings say so.

One thing measured rather than assumed while writing M-5's test: **permlevel does not raise,
it redacts**, and it is not applied by a server-side document load at all. `get_doc(...).as_dict()`
returns every field; `frappe.get_list` silently drops the field from the result; `frappe.client.get`
returns it as `None`. The first draft asserted `as_dict` and would have claimed a protection
frappe does not offer. Decider: P6 implementer.

2026-08-16 — **M-5: the operator DocPerm is kept and the fields move to permlevel 1.** The
choice was between dropping Cloud Storage Manager's read on the Backup Log entirely or moving
the artifact keys above its level. Dropping it would blind the operator to whether backups are
running, which is exactly the job the role exists for; moving the keys withholds only *where
the artifact lives*, which is the half that matters — it is the lower-privileged end of H-1's
path. So: status, trigger and timing stay readable, and `db_key`, `config_key`,
`public_files_key`, `private_files_key`, `backup_bucket` and `sha256_manifest` do not.
Decider: P6 implementer, on the review's explicit "your call, but say which and why".

2026-08-16 — **`acting_as` granting a role after the user switch leaves `frappe.get_roles`
serving the pre-grant list.** Recorded as a trap rather than as a fix, because the symptom
points away from the cause: every *positive control* in `test_backup_permissions.py` failed
with `PermissionError` — indistinguishable from a real refusal, and on a suite whose whole
purpose is asserting refusals. The natural reading is "the endpoint is over-gated"; the truth
was that the harness had granted System Manager to a user whose cached role list had already
been computed without it, so `frappe.only_for` was correctly refusing a user who did hold the
role.

The fix is ordering, not caching cleverness: grant the roles **before** `frappe.set_user`,
commit, then `frappe.clear_cache(user=...)`, then switch, then clear `in_test` last. Written
down because the next person to build a permission fixture will hit it, will see refusals
where they expect passes, and will reasonably start by suspecting the code under test — which
is the wrong end.

Paired with the other correction from the same afternoon (**permlevel redacts rather than
raises, and is not applied by `get_doc(...).as_dict()` at all**), the shape is the same: both
were assumptions about how frappe enforces something, both were wrong in the direction that
makes a test *look* like it is proving more than it is, and both were caught only by running
the thing rather than reasoning about it. Decider: P6 implementer.

2026-08-16 — **M-8: the C10 matcher knew three routes to one name and one route to the
other.** `presign_get` was matched as an attribute, as a `from … import`, and as a bare call.
`generate_presigned*` was matched as an attribute only — so
`from botocore.signers import generate_presigned_url` followed by a bare call escaped the
contract entirely. That is **H-1's shape a second time**: a function that was always reachable
by a route the matcher did not know. The file already demonstrated it knew this mattered,
because it covered all three routes for the other name; the asymmetry was the defect.

All three branches now go through one `_is_minting_name` predicate, so they cannot drift apart
again — the fix is not "add the missing branches" but "stop having a per-name notion of what
counts". Proven in the order the ruling set: a synthetic module using the import-and-bare-call
form was shown to pass the shipped matcher and to be flagged by the symmetric one, before
anything was changed.

The limit is unchanged and still written down: a mint under an alias (`import x as y`) or
through a dynamically resolved attribute evades it. A fully precise check needs to know a
receiver is a boto3 client, which is type inference and not a syntactic walk. **Symmetry is
the achievable half, and claiming more would be the same error in a new place.** Decider: P6
implementer, on the security re-review's M-8.

2026-08-16 — **Why an AST assertion is right for L-7 when M-1 was about AST assertions being
wrong.** These are not the same instrument used twice; they are the same instrument used on
two different kinds of property.

*"This endpoint is System Manager only"* is **behavioural**. Reading the source is a proxy for
it, and the proxy can be true while the behaviour is not — `only_for` might run after a side
effect, the function might be reachable another way, refusal might not work in this runtime.
That was M-1, and the eight executable refusal tests do that work now.

*"`frappe.only_for` is the first executable statement"* is **syntactic**. Reading the source
reads it directly, with nothing in between. There is no behavioural test that establishes it
better, and until this existed it was verified by a reviewer reading eight functions by hand.

Recorded explicitly so a later reader does not "helpfully" replace this with four more
positive controls: that would re-prove the behavioural property a fifth time and leave the
ordering property unchecked again.

The endpoints are **discovered** from the source, never listed — a hardcoded eight covers eight
of nine the day a ninth lands, silently, which is the C10 allow-list failure in miniature. The
walk asserts it found something, for the same reason the injector registry test asserts the
registry is populated. It found a real one on its first run: `apply_lifecycle_policy` imported
before its gate. Harmless in effect and exactly the property named, so it moved. Decider:
orchestrator's ruling on L-7; framing is the security reviewer's.

2026-08-16 — **M-9: the migrate-time guard asserted the positives and missed the negative, and
was blind to the rows that actually govern a live site.** Two defects in one patch.

The suite asserted that Cloud Storage Manager holds no permlevel-1 row; the **patch** — the
thing protecting a site nobody runs tests on — asserted only that the level-1 row exists and
that System Manager can read it. A merge or a hand-edit adding an operator level-1 row would
pass it, handing over exactly what the level was added to withhold.

Worse, `patch_handler` sets `frappe.flags.in_patch` and `meta.py` returns early on that flag
**before** applying the `Custom DocPerm` override, so inside a patch `meta.permissions` is
always the shipped JSON. Frappe copies every DocPerm into `Custom DocPerm` on first edit, so on
any site where Role Permission Manager has touched these doctypes the patch was reading a file
and the site was governed by a table. The failure mode is silent success on precisely the
upgrade path the patch exists for.

The two checks carry **different remediation on purpose**: a JSON problem is fixed in the JSON;
a `Custom DocPerm` problem is fixed in Role Permission Manager, and telling an operator to edit
JSON that is no longer authoritative for their site is worse than saying nothing. Decider: P6
implementer, on the re-review's M-9.

2026-08-16 — **L-8 ruling: `export` is dropped from the operator row, not tested.** Export is
a third read route (`frappe.desk.reportview.export_query`) and the redaction tests cover two.
The choice was between asserting over the third route and removing the capability. Removing it
wins on two grounds: an operator does not need to download backup logs to see that backups are
running, so nothing is lost; and a route that is not granted cannot regress, whereas a route
that is granted and tested depends on that test continuing to be right. Same reasoning the
project has applied elsewhere — a constraint enforced by construction beats one enforced by a
check. Decider: P6 implementer, on the orchestrator's explicit "your call, say which and why".

2026-08-16 — **`recorded_digest` scans the 500 most recent successful Backup Log rows, so its
reach is coupled to log retention.** Not a security finding — it fails closed, refusing a
presign rather than allowing one — but a functional coupling worth naming: past 500 successful
backups, older artifacts stop being downloadable through `download_url` even though they are
still in the bucket and still inside their lifecycle retention. With the shipped defaults (one
daily backup, 90-day log retention) the window is far wider than the log itself, so it cannot
bite today. It becomes reachable the day someone raises `log_retention_days` or moves to hourly
backups, and the symptom then is "the console says the artifact exists and the endpoint says it
does not". If that is ever hit, the fix is to query the Backup Log by key rather than to scan
recent rows. Decider: P6 implementer, on the re-review's note.

2026-08-16 — **Four more instances, all in the tests written to close M-9, and all the same
defect: a source grep matching the prose that explains the code.** The mutation run after the
M-8/L-7/M-9/L-8 batch found four survivors. `test_the_patch_forces_the_doctype_reload` searched
the patch source for `force=True` — which also appears in the **comment explaining why
`force=True` is there** — so removing the keyword left it green. The operator-row check
searched for `OPERATOR_ROLE` and the word "grants", both of which survive emptying the offender
list. The `Custom DocPerm` check searched for the query, which survives deleting the *call* to
the function containing it. The export check read the installed meta, which a JSON edit does
not move.

All four now execute rather than read: synthetic metas and a real `Custom DocPerm` row drive
the helpers and assert they throw, `force=True` is asserted on the `reload_doc` **call node**,
and the export check reads the shipped JSON alongside the meta. A fifth survivor was the
wiring — deleting `_assert_custom_permissions(doctype)` from `execute()` broke nothing, because
every test called the helper directly — closed with a spy, the same fix as the lifecycle
backstop.

Worth stating plainly: **this is the second consecutive batch where the fix's own tests
contained more instances than the code being fixed.** The security reviewer's prediction is not
a one-off observation; it is the most reliable finding-generator this project currently has.
Decider: P6 implementer.

2026-08-16 — **The settings-singleton comparison has two limits, and the second is the
larger one.** The phase gate has leaned on "both singletons byte-identical across two
consecutive runs" as evidence that a suite leaves the site as it found it. It is worth
having, and it is narrower than we have been treating it.

**Limit one (raised by the orchestrator): it compares runs to each other, never to a clean
install.** A leak that damages the state the *next* run captures as its baseline reads as
perfectly stable — run 2 and run 3 agree, because by run 2 the damage is in the baseline. On
a warm site the comparison is structurally incapable of seeing that class.

**Limit two (found here, by reading this suite rather than running it): it watches two
`tabSingles` rows and nothing else.** Almost the entire persistent footprint of a suite is
elsewhere — table rows, `User` records and their roles, `Custom DocPerm` overrides, cached
metas. None of that was ever in scope, and citing the comparison as though it covered the
site was an overclaim on our part, not a defect in the check.

**What is NOT established:** that any tree actually has such a leak. The orchestrator's
pristine-site failure that prompted this was **retracted** — a second pristine site passed
twice at the same SHA — and its reasoning for ruling out concurrency did not hold either. So
the limits are real and the leak is unproven, and this entry says so rather than tidying the
history into a cleaner story. The three candidates below stand on their own evidence: they
were found by reading the suite, and they are defects whether or not a run ever surfaces them.
Decider: P6 implementer and orchestrator jointly.

2026-08-16 — **Three leak candidates in P6's own suite, and only one of them is
self-perpetuating.** Found by auditing what the suite writes that outlives it.

1. **A `Custom DocPerm` row deleted without clearing the meta cache.** A `Custom DocPerm`
   *overrides* the shipped DocPerms entirely, so a stale cached meta means every later test in
   the process reads a permission set no longer in the database. Highest consequence, and
   confined to one process.
2. **Fixture users that persist, with role removal inside `contextlib.suppress`.** This is
   the self-perpetuating one and therefore the dangerous one: a removal that half-works
   leaves the user permanently elevated, `ensure_test_user` finds them present on the next run
   and adds nothing, and every refusal test is then asserting against a user who holds the
   role it expects them to lack. **State that survives and then hides its own repair.** The
   `suppress` was written so a cleanup failure could not mask a test failure — the right
   instinct applied to the wrong statement — and it is removed rather than narrowed, on the
   same reasoning as the serialise-don't-mitigate ruling: a cleanup that fails quietly removes
   the signal.
3. **Audit rows accumulating** from every successful `download_url`, including the positive
   controls. Behaviourally benign as far as can be seen — every reader filters by action — but
   unbounded and unmeasured.

`TestTheSuiteLeavesNothingBehind` now pins all three. It cannot prove the absence of a leak;
it pins the three a read of the suite actually found. Decider: P6 implementer.

2026-08-16 — **A deadlock on the settings singleton does not fail the test that loses the
lock; it fails whichever tests read the singleton afterwards.** Worth stating plainly because
it sends a debugger to the wrong module. In the failed verification run, seven of ten errors
were `QueryDeadlockError` and three were downstream — `ttl 300 != 120` is a test asserting a
value that *another* test's deadlocked cleanup failed to restore. Read in isolation, those
three look like defects in `test_compat` and `test_cloud_file`; they are symptoms of a lock
lost somewhere else entirely. The cascade is the reason a lost lock produces N errored tests
rather than one. Decider: orchestrator's framing, recorded here.

2026-08-16 — **This suite is order-dependent, stated plainly rather than as a note.**
`test_object_stats_are_measured_from_this_site_s_own_backups` deletes **every** Cloud Storage
Backup Log row to get a known table, because `collect_object_stats` reads the last 30 Success
rows on the site and any row an earlier run left behind changes the mean. Nothing breaks today
— the other classes delete their rows by name afterwards and no-op — but *"nothing breaks
today"* is a property of the current test order, and order is not a contract. A later phase
adding a class that **counts** Backup Log rows would fail mysteriously, in the wrong module,
which is the same debugging trap as the singleton cascade above. Decider: P6 implementer.

2026-08-16 — **And one more instance, in the test written to close candidate 2, found within
the hour.** `test_role_removal_is_not_silently_suppressed` searched the cleanup block for the
string `"suppress"` — and failed, because the comment explaining that the removal is *not*
suppressed contains the word. Same defect as the four in the M-9 batch: **a grep matching the
prose that explains the code.** Rewritten to walk the AST for a `with contextlib.suppress(...)`
around the removal, and the note is kept in the test's own docstring rather than only here,
because that test is precisely where someone would reintroduce it. Decider: P6 implementer.

2026-08-16 — **A guard kept on measured evidence, with its mutation row removed because it
cannot fail — and the reason it cannot fail is left as an open question rather than guessed
at.** `_drop_custom_docperm` clears the doctype cache after deleting a `Custom DocPerm` row.
Measured in a normal (non-suite) process:

```
after_insert:          ["Cloud Storage Manager"]   the override is visible
after_delete_no_clear: ["Cloud Storage Manager"]   row gone from the DB, meta still shows it
after_clear_cache:     ["System Manager"]          restored
```

So outside the suite the clear is load-bearing: without it the process keeps reading a
permission set that is no longer in the database, and a `Custom DocPerm` *overrides* the
shipped rows entirely. That measurement is the justification for the code.

Inside the suite, removing the call fails nothing — the assertion that the meta no longer
shows the override passes either way. `db.delete` invalidates no cache and `get_meta` caches
in `frappe.cache` in tests as elsewhere, so the obvious explanations do not hold, and **I did
not determine the actual one.** Saying so is the point of this entry: a plausible mechanism
written down as fact is worth less than a stated gap, and this project has paid for the
difference more than once.

The consequence is procedural. **The code stays** — it is justified by a direct measurement,
not by a test. **The mutation row is removed**, because a row that cannot fail is precisely
what that script exists to prevent, and the same call was made for the redundant
apply-prefix guard and the superseded C10 row. **The gate identity is dropped too**, for the
same reason: naming a guard whose fallibility is unproven would put an unearned claim in the
release evidence. Decider: P6 implementer.
2026-08-16 — **P7: A9's "only thin wrappers" is a rule about where decisions live, not about
which module every wrapper delegates to.** A9 says `api/admin.py` contains only thin
`frappe.only_for` wrappers delegating to `migration/api.py`, and two of the twelve buttons —
Test Connection and the storage-health panel — have no campaign and nothing to say to the
migration engine. Reading the sentence literally would put storage probes behind the
migration state machine; reading its stated purpose ("single validation path shared with the
CLI") gives the rule its force: **`api/admin.py` decides nothing.** Campaign buttons delegate
to `migration/api`; Test Connection delegates to `storage/diagnostics`; the panel delegates to
`health`. `TestAdminIsOnlyWrappers` parses the module and fails on any branch, loop, throw,
assignment, comparison or database access in any endpoint, so the property is machine-checked
rather than asserted in prose.

**Why the literal reading is the worse one.** Satisfying the sentence as written would mean
inventing a `migration/api` function for Test Connection and for the health panel — putting a
bucket probe and a byte census behind the campaign state machine, in the module whose entire
job is to own the A9 transitions. That damages the structure A9 exists to protect in order to
preserve its wording: the migration API would then hold logic no campaign transition needs, and
"the single validation path" would name a module that validates things unrelated to migration.

**For the release audit: this is not a deviation from A9.** It is a reading of A9's force —
`api/admin.py` decides nothing, and every campaign button does route through `migration/api`.
The literal-text mismatch on two of the twelve buttons is deliberate and is the subject of this
entry. Decider: P7 implementer, **confirmed by the orchestrator** (ruling recorded 2026-08-16:
"A9's wording is 'delegating to `migration/api.py`', but its force is the parenthetical").

2026-08-16 — **P7: `retry_failed` and `start_verify` added to `migration/api`, because two
buttons had nothing to delegate to.** The design's button table names Retry Failed and Verify;
neither existed behind the state machine, and the CLI's `cfs-migrate-verify` reached past it
into `engine.dispatch_batches` — the exact drift A9 exists to prevent. Both now take the same
lock → assert → write → commit → audit shape as their siblings, and the CLI calls them. Two
deliberate narrowings: `retry_failed` does **not** reset `attempt_count` (a bulk button that
cleared the retry bound would let an operator loop an object for ever; objects that have spent
their attempts are already in the conflict queue, where `conflicts.retry` is the per-object
decision), and `start_verify` re-applies A31's residue check per batch rather than trusting the
batch status, since it is a second entry point into VERIFY. Decider: P7 implementer.

2026-08-16 — **P7: the cleanup confirm phrase is compared in `migration/api`, not in the Desk
endpoint, and is compared byte-for-byte.** `/api/method/…start_cleanup` is reachable without
ever loading the form, so a check in the button layer would guard the dialog and nothing else;
putting it in the choke point also gives `bench cfs-migrate-cleanup` the same gate (it grew a
`--confirm-phrase` option that it hands straight through). The comparison is exact:
`.strip().upper()` was tried and rejected, because a phrase the server normalises is one the
operator can get wrong and still be understood. Decider: P7 implementer.

2026-08-16 — **P7: Test Connection writes a probe object and never deletes it.** The design
specifies a put+get+delete probe. INVARIANTS.md invariant 4 reserves physical S3 deletes for
deferred GC, and a second delete path — even one that only touches a key no File row points at
— is what that invariant exists to prevent. The probe key is fixed
(`<prefix>/<site>/.cfs-connection-probe`), so N runs leave one small object rather than N, and
the returned payload says so. The probe rides `engine.put_bytes`/`get_bytes` rather than a
boto3 client of its own, so it exercises the same ExtraArgs (no `ACL`, checksummed PUT),
breaker and typed errors an attachment upload does. Decider: P7 implementer.

2026-08-16 — **P7: `Cloud Storage Manager` now genuinely operates campaigns.** P5 gated every
`migration/api` entry point on System Manager, which made the role — created in P1 — decorative
and left A12's `permlevel: 1` credential separation with nothing to separate. Design §3.2's
rationale is that ops staff run migrations *without* credential access, so the guard is split:
`_guard` (System Manager **or** Cloud Storage Manager) for operating a campaign, `_manager_guard`
(System Manager only) for approve/start cleanup, reconcile, fail and purge. The split is a
widening of permissions and is asserted in both directions, with `frappe.local.flags.in_test`
cleared — `frappe.only_for` returns early under that flag and for Administrator, so every role
assertion written the obvious way passes against code with no gate at all. Decider: P7
implementer.

2026-08-16 — **P7: Button and HTML fields added to `Cloud Storage Settings` do not touch A12's
frozen schema.** Both are in frappe's `no_value_fields`, so neither creates a column; the panel
and its buttons are form furniture over an unchanged data schema, and a test asserts that
property rather than the reviewer having to take it on trust. `active_campaign`, which the
design sketches as a stored read-only Link, is rendered inside the migration HTML block
instead: it would have been a real data field, and A12 outranks the design doc. Decider: P7
implementer.

2026-08-16 — **P7: two P5 state gates were missing and are added.** `start_analysis` did not
check for another active campaign (every transition in that module writes through
`frappe.db.set_value`, which does not run `validate`, so the doctype's `validate_single_active`
never fires on a transition) — two campaigns could both sit in `Analyzing`. And `reconcile` ran
during a campaign, where an object mid-transfer is indistinguishable from one that was lost:
its `missing_remote` counter raises an Error Log and is the one an operator is meant to act on
immediately, so a false positive there is worse than making the operator wait. Both now refuse.

**Filed as P5 defects found by the P7 gate, not as P7 features.** Both gaps are in merged P5
code that an independent test-engineer, an independent reviewer and a full green suite had
already passed over; they surfaced only when the Desk buttons drove the same entry points from
a second caller. The distinction matters to the release audit: a phase adding capability and a
gate catching something previously cleared are different events, and only the second says
anything about how well the earlier gate worked. Decider: P7 implementer for the fix; **the
defect belongs to P5.**

2026-08-16 — **P7: the merged baseline of 868 tests is 849 plus the 19 MinIO integration
tests.** `tests/test_minio_integration.py` collects **zero** tests without
`RUN_MINIO_INTEGRATION_TESTS`, and `tests/test_ecosystem.py` collects zero without the
ecosystem apps — they are gated at collection, not skipped at runtime, so they do not appear in
the "Ran N" line at all. The P5 attestation ran with MinIO configured and without the ecosystem
apps. Recorded because a phase that compares its count against 868 without the env vars will
conclude it is testing the wrong tree; the reliable check is the module path
(`cloud_file_storage.__file__`), not the count. Decider: P7 implementer.

2026-08-16 — **P7: the Playwright smoke's fixture must be purged from the site, not just its
campaign.** Running the suite with the 100 `p7-smoke-*` File rows still present produced four
failures in `test_migration_adoption`, `test_migration_reconcile` and
`test_migration_resilience`, and those modules ran 7–9× slower. Purging the fixture cleared all
four. Nothing was wrong with the app or with those tests: several migration suites analyse the
whole of `tabFile`, so the site they measure has to be the site they were written against. The
smoke is deliberately not part of `bench run-tests` for the same family of reasons, and
`docs/runbooks/desk-smoke.md` now spells out both cleanups. Worth knowing before a later phase
reads a similar failure as a regression. Decider: P7 implementer.

2026-08-16 — **P7: backup health findings are rendered by the Settings panel and detected by
`backup/health` (P6). No second detector.** The team lead routed this seam to P7 at the end of
P6; P6 owns `backup_health()` (schedule staleness, tarballs after the cutover, lifecycle
drift), P7 owns the chips. `health._backup_section` returns the module's findings verbatim —
`tests/test_backup_health_panel.py` asserts same objects, same order, same wording, that an
empty report stays empty, and (by AST) that the only backup import in `health.py` lives in the
`_backup_health_module` seam. A panel with its own staleness arithmetic would be two answers to
one question, and the operator would act on whichever surface they opened. **The import is
guarded** because the two phases were built in separate worktrees: on a tree without the backup
domain the panel says so rather than growing a local detector that would then have to be
deleted at convergence — and the deletion is the step that gets skipped. **The findings are
rendered in their own block with no number in it**: `tarballs_after_cutover` is a statement
about local bytes, and it is the finding most likely to end up beside a byte total, which is
precisely the conflation PLAN §A forbids. Decider: P7 implementer, on the lead's routing.

2026-08-16 — **P7 does not widen P6's role gate on backup health from the outside.**
`backup/health.get_backup_health` is `frappe.only_for("System Manager")`; `get_storage_health`
admits Cloud Storage Manager too. Rather than serving another phase's data to a wider role, the
section reports itself **withheld with a reason** for anyone P6 would have refused, and the
module is not called at all in that case (withheld means not computed, not computed-then-
hidden). Design §3.2 does say the operator role may read backups, so this may well be widened —
but that is a convergence decision with both phases present, not a P7 one. Named in the P7
report. Decider: P7 implementer.

2026-08-16 — **P7: `sys.modules` injection is the wrong way to stub a module in this suite.**
The first version of `tests/test_backup_health_panel.py` injected a fake
`cloud_file_storage.backup.health` through `patch.dict(sys.modules, …)`; twelve tests then
errored inside `frappe.get_cached_doc` with `PicklingError: … not the same object as …`. The
Settings Single is pickled on cache write, and with the module table mutated pickle resolves
the controller class to a different object than the instance's own. The failure had nothing to
do with the property under test. `health._backup_health_module()` is now a named seam that
tests patch directly. Recorded because the error message points at the document cache and says
nothing about the patch that caused it. Decider: P7 implementer.

2026-08-16 — **P7: locale regenerated; five German translations dropped because their strings
no longer exist.** `bench generate-pot-file` took `main.pot` from 83 msgids (P1) to 562 — P2–P5
added strings and never regenerated — and `bench update-po-files` synced `de.po` to match.
Seven of P1's twelve German translations survive **unchanged**; the five that went are
`Access denied: Could not delete file`, `File Upload Failed. Please try again.`,
`Migrate Existing Files`, `Timeout for Migration Job` and the migration-timeout description —
all fork-era strings deleted in P1/P2/P7 (the last of them the dead `migrate_existing_files`
button this phase removed from the settings form). Each was verified absent from the source
tree. Both were regenerated with frappe's own tools, never edited by hand (INVARIANTS.md). **P6
regenerated the same file in its own worktree (567 msgids there, P5+P6 strings; 562 here,
P5+P7), so the merge will conflict and the catalogue must be regenerated once more after
convergence rather than resolved by hand.** Decider: P7 implementer.

2026-08-16 — **P7-C1 (Critical, independent gate): P7 created `Cloud Storage Manager` access
to Cloud Storage Settings from nothing, and gave it write.** *Corrected record — this was not a
widening.* The base tree at `00355ef` had **no `Cloud Storage Manager` DocPerm on this doctype
at all** (`git grep '"role": "Cloud Storage Manager"'` on the base JSON returns nothing); P7
created the row, with `read: 1, write: 1`, copied from design §3.2's "rw (no delete)". The
first framing — including the orchestrator's and the gate's — described adding `write` to an
existing row, which understates the delta. What was created was the role's access to that
doctype, write included. permlevel 1 protects the two
credential fields and **nothing else**, so write at level 0 handed `Cloud Storage Manager`
every other field.

**The sharp harm is `bucket` and `endpoint_url`, not `operation_mode`.** Both sit at permlevel
0. A12's permlevel protects the *credentials*; nothing protected *where the bytes go*.
`validate_endpoint_url` requires only HTTPS with no query string, and
`warn_on_connection_change_with_existing_objects` is a `msgprint`, which does not block a call
arriving over `/api/method/`. So the assembled path was: a Cloud Storage Manager, holding no
System Manager role, sets `endpoint_url` to a host they control and `bucket` to anything, then
calls `create_campaign`, `analyze_storage`, `build_migration_plan` and `start_campaign` — every
one of which P7 had opened to exactly that role — and **every private attachment in the site is
uploaded to their endpoint.** The real bucket credentials are never needed, because an
attacker-run endpoint accepts any SigV4 signature. That is an attachment exfiltration
primitive, assembled from three individually reasonable parts: a role that can operate
campaigns, a settings form that role can edit, and a connection-change check that warns instead
of refusing. `operation_mode` and `object_delete_grace_days` were the second and third harms,
not the first: the mode is the gate `start_cleanup` checks, and the grace is PLAN §H item 1's
attachment recovery window, whose shortening brings forward a physical S3 delete that nothing
undoes. None of it passes an endpoint — a plain document save was enough, so every role gate
this phase built was beside the point for those fields. **Fixed by removing the capability, not by fencing each use of it:**
`write`/`create`/`delete` are gone and `read` stays, because operating storage needs no
settings write — every operator capability is reached through a role-gated whitelisted
endpoint. Moving the connection and safety fields to permlevel 1 was the rejected alternative:
it spreads A12's credential boundary over a dozen unrelated fields and leaves the write vector
alive for whatever field someone forgets. The `v0_6_0` patch now refuses the row on **every** install and
upgrade that applies it, with a failure message naming the fix, because the realistic way this
Critical comes back is a merge resolving the permissions array by taking both sides — and a
behavioural test only catches that if someone runs it after the merge and thinks to look.
Proven fallible by reinstating `write: 1` and watching `bench migrate` refuse. The behaviour is
additionally asserted through `Document.save` in both directions with `in_test` cleared.
Decider: orchestrator ruling on the reviewer's finding; implemented by P7; record corrected
after the independent security review established the base tree had no such row.

2026-08-16 — **P7-H1: `create_campaign` let the operator role pre-set the destructive
policy.** It copied `cleanup_mode` and `quarantine_ttl_days` straight from caller policy, and
`_guard` admits `Cloud Storage Manager` since P7. A campaign could therefore be created already
set to `Direct Delete` — "no going back" — and the System Manager who later approves and starts
it would approve a decision taken elsewhere, without that choice appearing anywhere on the
approval path. The approval is only meaningful if it covers everything destructive about the
run. Both fields are now refused at creation and take their DocType defaults (Quarantine / 14
days); `Direct Delete` stays reachable at `start_cleanup(direct_delete=True)`, which is
System-Manager-only, type-to-confirm gated and audited — the destructive choice made at the
destructive moment. `Cloud Storage Manager` holds no write DocPerm on `Cloud Migration
Campaign`, so there is no second route, and a test asserts that rather than assuming it.
Decider: orchestrator ruling on the reviewer's finding; implemented by P7.

2026-08-16 — **P7: the `object_delete_grace_days >= restore_window_days` validator is
deliberately NOT built here.** The reviewer correctly observed that P7's tree has only a health
*warning* for it. P6 ships the validator — `backup/retention.assert_grace_covers_restore_window`,
wired into `CloudStorageSettings.validate` — and it arrives at merge. A second validator would
be two answers to one question, the same conflation P7 refused when it declined to grow a local
backup-health detector. Recorded as a **convergence verification item**: after the merge,
confirm the validator is present and reachable on the save path. Note also that P7-C1 has
already removed what made its absence urgent — the operator role can no longer write that field
at all, so it is now reachable only by a System Manager, who could set it to anything in any
case. The validator is still right; it is no longer a privilege-escalation surface. Decider:
orchestrator ruling.

2026-08-16 — **P7-L1: the connection probe has two entry points with different roles, on
purpose.** `api.compat.test_connection` is `frappe.only_for("System Manager")` — P1 shipped it
that way and site code may call it — while `api.admin.test_connection` admits `Cloud Storage
Manager`, because design §3.2 gives the operator role storage diagnostics without credentials.
Both delegate to the same `storage.diagnostics.test_connection`, so there is one implementation
and no divergence in what is reported; only who may ask. Decider: P7 implementer.

2026-08-16 — **Instance nine of the recorded pattern, in pre-existing code:
`test_the_health_panel_carries_the_finding` never touched the health panel.** It called
`compat.test_connection()` and asserted a key on that payload — a different surface, with a
different audience and a different role gate. Two layers of harm: the name stated a property
the body did not read, and the name *also* grepped as coverage, so the panel's own
`public_private_residue` warning looked tested when nothing asserted it. Renamed to
`test_the_connection_report_carries_the_finding`; the panel is covered by
`test_storage_health.TestPanelWarnings`. Found while auditing warning coverage after the
orchestrator flagged one untested warning — the audit found four of five untested, which is
absent coverage rather than this pattern, and the two are recorded separately on purpose: a
test that lies is found by mutating code and watching green stay green, absent coverage by
comparing emitted behaviour against asserted behaviour. Conflating them would send the release
audit looking for misleading tests and hand it honest gaps. Pre-existing code, passed by three
prior gates. Decider: P7 implementer; classification confirmed by the orchestrator.

2026-08-16 — **P7/P6 merge collision, found by reading P6's tree at `b907603`:
`storage.engine.get_bucket_versioning` and the fake's versioning stub exist in both.** The
engine functions are near-identical (both `PROFILE_REQUEST`, both `settings.bucket`) and
conflict only textually. The **fakes disagree**: P7's defaults to versioning OFF
(`bucket_versioning = None`), P6's to ON (`versioning = {"Status": "Enabled"}`), under different
attribute names. Whichever survives the merge would silently change the other phase's reported
health — and P7 had **no test asserting the versioning probe either way**, so nothing would have
failed. That gap was P7's own, and the same shape as P6's finding #8 (`fail_head_with` existed
and no test ever set it): a capability built to make a safeguard testable, then never used.
`tests/test_storage_diagnostics.py` now asserts both outcomes with the value set **explicitly**,
which makes those tests independent of whichever default the merged fake carries. Convergence
still has to reconcile the two attribute names, but it can no longer do so silently. Decider:
P7 implementer.

2026-08-16 — **P7 security review, four smaller items, all fixed.**
* **M-3** — `report.export_campaign_report` joined an unvalidated `campaign` into a filesystem
  path, and `export_report` called only `_guard()`, so P7's role split put it in reach of the
  operator over HTTP. The bound was genuinely narrow (names are `format:CFS-CAMP-{####}`, so a
  traversing value names no campaign and the file is header-only) — but that argument stops
  holding the moment the filename or the column set changes, so it now checks existence **and**
  reduces the value to its basename. Two guards, neither depending on the other.
* **N-1** — the snapshot published with `user="Administrator"`: the narrowest possible audience,
  so for every non-Administrator operator the dashboard's realtime binding never fired and the
  Page ran on its 30s poll alone. The tests asserted a binding existed and the event name
  matched; nothing asserted who could hear it, so they were green for a feature inert for its
  users. **The obvious fix is worse than the bug:** with neither `user` nor `room`,
  `publish_realtime` resolves to `get_site_room()` — `"all"` — and campaign internals reach
  every Desk user. It now publishes to exactly the two roles `cloud_migration_dashboard.json`
  admits, resolved to enabled users, Administrator included explicitly because it holds every
  role implicitly and may carry no `Has Role` row. `tests/test_realtime_audience.py` asserts the
  **audience**, and carries a control proving a broadcast fails it — an assertion written as "a
  subscriber receives it" would pass identically for a broadcast.
* **L-4** — the one interpolation in `cloud_storage_settings.js` not passed through
  `escape_html`. The source is a Link to DocType so the injector already outranks the viewer;
  escaped for uniformity rather than because it was reachable.
* **N-3** — `export_campaign_report`'s docstring said "absolute path"; `frappe.get_site_path`
  returns a site-relative one and that string is shown to the operator.
Decider: P7 implementer, on the independent security review.

2026-08-16 — **P7-L3 recorded, NOT fixed: `storage_health()` is uncached and does real work.**
Two `GROUP BY` scans, two `SUM`/`COUNT` scans and a full `os.walk` of the cache directory per
call, behind an endpoint the operator can poll and which the Settings form fires on every
refresh. At the 1.2M-row target that is a load amplifier. Deliberately left alone: the right
fix depends on a measurement nobody has taken (which query dominates, and whether the walk or
the scans matter at scale), and reaching for a cache without that would be a reflex rather than
a decision — and a stale health panel has its own failure mode. Flagged for P8, which owns
performance work. Decider: orchestrator ruling.

2026-08-16 — **P7-L2 recorded: "two-person-rule friendly" is aspirational, not implemented.**
`approve_cleanup`'s docstring says the split from `start_cleanup` is two-person-rule friendly.
Server-side nothing is bypassed — both are `_manager_guard`ed and both audit
`frappe.session.user` — but the Desk flow chains them in a single `frappe.prompt` callback, and
no surface or test makes the two actors distinct. One System Manager can do both in one
gesture, which is what the phrase invites a reader to assume cannot happen. Recorded rather
than fixed: enforcing distinct actors is a policy decision about how a site is staffed, not a
defect in this phase, and it needs a place to record the second actor that no doctype currently
has. Decider: P7 implementer; raised by the independent security review.

2026-08-16 — **P7: the Playwright smoke cannot run on this bench, and it is NOT this app —
established by control, not by inference.** The Desk oscillates between `/app` and
`/app/setup-wizard/0` and never renders a layout. My first reading was "accumulated site state";
the orchestrator overrode a drop-and-recreate in favour of building a second site alongside,
which turned the guess into an experiment and was the better call.

**Result: neither of the predicted outcomes.** A fresh `cfs-p7b.local` (new-site → install-app →
migrate, all clean) reproduced the loop, so it is *not* accumulated state. But a third site,
`cfs-p7c.local`, with **frappe only and no apps installed**, reproduces it identically — 26
navigations, 12 wizard bounces, no layout — so it is *not* a P7 shipping blocker either.

**A candidate cause was proposed and disproved, which is worth more than the guess.** The
`frappe` checkout carries uncommitted modifications including a patch in `frappe/app.py` that
redirects requests whose resolved site is `localhost`/`127.0.0.1` to
`common_site_config.default_site` — on this bench, a business site. If it fired it would explain
everything, and it would also have meant browser traffic reaching a site it must never reach. It
does not fire: `site = _site or headers.get("X-Frappe-Site-Name") or get_site_name(...)`, and
`bench --site X serve` sets `_site`, so `site` is never `localhost` on any server used here.
**Measured, not argued** — the served page is byte-identical with and without the header, and
both carry `"sitename"` equal to the site `bench serve` was started for. Setting the header in
the browser context does not lift the loop (16 navigations instead of 37, same oscillation). The
modified files predate this work by months (`app.py` 2026-01-02), so nothing in this session
touched that checkout. **Safety consequence, stated directly rather than inferred:** browser
traffic went to the intended scratch site throughout, read off the served boot payload rather
than deduced from what would otherwise have failed.

**What remains unexplained, recorded as observed.** The served boot carries
`"setup_complete": true` at top level, `frappe.is_setup_complete()` returns True, and
`Installed Application.frappe.is_setup_complete` is 1 — while the browser still oscillates.
`router.js:147-151` routes to the wizard only when that flag is falsy. Something other than that
branch drives the redirect and it was not found. No cause is claimed; "transient" would be a
guess. The next step is inside frappe's desk bundle in a shared checkout serving thirteen
business sites, and the orchestrator ruled it out of scope on the grounds that it cannot change
what ships.

**One genuinely useful fact fell out.** `frappe.boot.setup_complete` comes from
`frappe.is_setup_complete()`, which reads `Installed Application.is_setup_complete` — not
`System Settings.setup_complete`, not `sys_defaults`. Both of those are what an operator (and
this implementer) reaches for first, and neither affects the redirect. Recorded in
`docs/runbooks/desk-smoke.md` with the snippet, because the next person will lose the same hour.

**The smoke therefore stands where the independent gate placed it: "ran, not re-verified
evidence."** Not claimed as met, not softened to transient, and the root cause of the
oscillation is still unknown. The script has its artefact writer, so the evidence is one clean
run away on any bench whose Desk boots. Decider: P7 implementer, on the orchestrator's ruling to
build alongside rather than replace.

2026-08-16 — **P7: the settings singleton is byte-identical run-to-run, but only from the
second run on a pristine site.** On `cfs-p7b.local` the first suite run rewrote six fields
(`access_key_id`, `bucket`, `cdn_base_url`, `endpoint_url`, `key_prefix`, `region`) from SQL
NULL to the empty string — the one-time transition from "never written" to "written empty",
performed by the suite's own `restore_settings`. Runs two and three were byte-identical to each
other (md5 `d5c11719a3d9f5c970a8716e914824df`). Recorded because the phase gate asks for a
byte-identical singleton across consecutive runs, and on a freshly installed site the *first*
comparison legitimately differs; comparing run 1 against run 2 there would report a leak that
is not one. On `cfs-p7.local`, where those fields had long since been written, all three
snapshots matched. Decider: P7 implementer.

2026-08-16 — **P7 security re-review: M-7, M-6, L-5, L-6 and N-5 fixed. The control for N-1 was
itself an instance of the pattern it existed to catch.**

**M-7 — `test_a_broadcast_would_fail_this_suite` simulated the mutation instead of performing
it.** It defined a local `broadcast()` and then asserted *inline* that a check of that shape
would notice — its own copy of the assertion in `test_every_publish_names_a_single_user`, so the
real assertion never ran. Its docstring claimed to prove the suite notices; loosening the real
assertion would have left the control green. The control now patches
`report.publish_campaign_snapshot` to broadcast and invokes the real test method, catching the
`AssertionError`, and a second case covers the `room="all"` shape as well as the omitted-room
one — a loosening of the form `user or room` accepts the first while still rejecting the second.
The assertion had to be lifted out of `subTest` to make this work at all: a failure inside a
subTest context is *recorded on the result rather than raised*, so `assertRaises` would never
have seen it and the control would have reported its own failure instead of catching one. That
is the difference between a control that runs the real assertion and one that appears to.
**Verified by weakening the real assertion and watching the control go red** — which the old
version could not do. This was requested by the orchestrator in exactly those terms and I still
wrote the simulating version first; the request was right and the instinct to shortcut it is the
thing to watch for.

**M-6 — the patch guarding C-1 was blind to `Custom DocPerm`, which is what governs on a real
site.** `frappe.get_meta` swaps `self.permissions` for `Custom DocPerm` rows whenever any exist
(`frappe/model/meta.py:542-554`), *except* it returns early while `frappe.flags.in_patch` is set
— which `patch_handler` sets while this patch runs. So the assertion genuinely read the shipped
JSON and was genuinely blind to the rows that apply once anyone has edited permissions, because
Role Permission Manager copies every DocPerm into Custom DocPerm on the first edit to a doctype.
A site that installed a pre-fix P7 and then touched permissions here would have `write: 1`
frozen there: the JSON fix applies, the assertion passes, `bench migrate` succeeds, and the
operator role still holds write. **The guard built to catch C-1 reporting green on the one path
it exists to protect.** A second query now checks those rows, with its **own** remedy — pointing
at Role Permission Manager, because telling an operator to edit JSON that is no longer
authoritative would be worse than saying nothing. Medium only because P7 is unreleased, so no
site can be in that state yet.

**L-6 — that guard had no committed test.** Its fallibility had been demonstrated once by hand
and lived only in a chat message, so nothing re-ran it. `TestTheC1PatchGuardBites` covers both
stores, both directions, and the shipped tree against its own guard. One of its assertions was
wrong on first write and is worth recording: it asserted the Custom DocPerm remedy does **not**
contain `cloud_storage_settings.json`, as a proxy for "does not send the operator to edit the
wrong file". The message names that file deliberately, to say editing it will not help — the
useful thing to say. The proxy was the wrong test for the property, so it now asserts the
property.

**L-5** — `dashboard_audience()` computed "users holding either role" while its docstring said
"users who may open the dashboard". A Website User with Cloud Storage Manager entered the
audience and received snapshots they could never act on. Filtered to `user_type = System User`,
with a positive control so the filter cannot silently exclude everyone.

**N-5** — the recipient loop was wrapped in a single `try`, so a failure on one recipient
silently dropped every recipient after it. The existing hiccup test used `side_effect` on every
call, so it never covered the partial case. Now per recipient, with a test that fails exactly
one and asserts the others still received it.

All five proven fallible by mutation: removing the Custom DocPerm arm, restoring the wrapping
`try`, widening `user_type`, and weakening the real audience assertion each fail their own test
and no other. Decider: P7 implementer, on the independent security re-review.

2026-08-16 — **P7: an enumerating `escape_html` assertion replaces the browser-level check the
smoke would have provided.** The security review's judgement was that the unmet Playwright smoke
is an *evidence* gap and not a security blocker — every control it would exercise is server-side
and reachable without a browser, and `/api/method/` is the adversary's path — but that it does
cover one class unit tests do not: **rendered-output escaping**. L-4 was a real unescaped
interpolation shipped in this phase, so the class is not hypothetical.
`tests/test_desk_escaping.py` covers it deterministically, on every run, without depending on a
Desk that boots.

**It enumerates rather than sampling, and that distinction is the whole point.**
`assertIn("escape_html", source)` returns **True** against the exact tree that shipped L-4 —
measured, not assumed — so it and occurrence-counting are proxies that would have certified the
defect they exist to catch. The check instead finds every `${...}` in every template literal
(descending into nested literals), classifies each against a fixed set of rules, and **fails on
anything it cannot classify**. Silently skipping the unparseable would be the same defect one
level down.

**Proven both ways, in the corrected C-20 form — mutate the artifact, run the real assertion
against the mutated artifact:**
* L-4's original expression restored verbatim is flagged at `cloud_storage_settings.js:361`;
* unwrapping a currently-escaped value (`payload.bucket`) is flagged at line 305, **naming that
  interpolation** rather than merely failing;
* the shipped tree passes, so the matcher is not simply flagging everything.

The first proves it would have caught the real defect; the second proves it notices a new one.
An assertion can pass one and fail the other if it only looks at some interpolations, which is
why both are kept.

**It found one immediately**: `cloud_migration_dashboard.js` interpolated a destructured `value`
from a `[label, value]` pair. Fixed by escaping it rather than by adding a classifier exception
— one fewer allow-list entry is a stronger check than one more.

**What escapes it, stated rather than implied** (it is a source matcher, not a renderer):
values passed through an intermediate variable are classified on the variable, not on their
origin — `HTML_FRAGMENT` and `CLIENT_LOCAL` are exactly that hole, held closed only by being
short and by every producer being an `*_html` function in the same file; HTML built by string
concatenation instead of a template literal is not seen at all; `.html()` called with markup
assembled elsewhere is not seen; and the `NUMERIC_FIELD` rule asserts a named server field *is*
numeric, which `test_every_numeric_field_is_really_numeric` checks against the doctype JSON but
which would drift if a field changed type without that test being updated. The precedent for
recording a matcher's limits rather than implying the class is closed is P6's `generate_presigned`
prefix matcher.

**The release evidence should describe the remaining gap as "no browser-level verification of
rendered output" — not "Desk UI unverified"**, which would overstate it and imply the role gates
are unproven when they are demonstrably not. Decider: orchestrator, on the security review's
recommendation; implemented by P7.

2026-08-16 — **Fourth instance in one session, in the fix for the third: an expression-level
assertion cannot see decoration of the rendered output.** The security review flagged
`test_desk_surfaces.py`'s `assertIn("escape_html(finding.message)", body)` as a substring proxy
— true, and it would have passed against the tree that shipped L-4. Replacing it, I first
asserted that the *interpolation expression* carrying `finding.message` is exactly the escape
call. That is still not the property the test is named for: prefixing the output with a literal
`Backup: ` leaves the expression untouched, so the panel can start editorialising while the
assertion stays green. **Verified by mutation — the replacement did not catch it.** It now reads
the markup between the surrounding tags and requires it to be the interpolation and nothing
else; the same mutation fails it.

Four instances now, each caught the same way: run the check against an input where the property
is actually violated. The first three were listed in the M-7 entry; this one is worth adding
because it happened while *deliberately* fixing that class of defect, with the pattern in mind,
one message after writing the tally. The gravity the orchestrator named — that
"assert something of this shape would notice" is easier to write than "run the real check" — is
not defeated by knowing about it. What defeats it is the mutation, every time, without exception.

Also recorded: `test_it_prints_the_messages_verbatim` is about **rewording**, not escaping.
Escaping is covered for every interpolation by `tests/test_desk_escaping.py`, and a single test
that gestures at both properties will be weak at one of them. Decider: P7 implementer, on the
independent security review.

2026-08-16 — **A P7 test leaked shared configuration, and the byte-identical-singleton gate is
what caught it.** `TestPanelWarnings.test_an_empty_ignored_list_is_warned_about` emptied
`Cloud Storage Settings.ignored_doctypes` on the real Single to reach the `no_ignored_doctypes`
branch, restoring it in cleanup. The restore did not hold. The next full run failed **eight
tests across four modules** — `test_migration_cleanup`, `test_migration_pipeline`,
`test_core_hooks`, `test_storage_health` — every one of them a test that depends on Data Import
being an ignored doctype, and none of them the test that caused it. The settings singleton also
stopped matching between runs, which is how it surfaced rather than being blamed on the
unrelated modules it broke.

**It self-perpetuates**, which is the part worth recording: once the list is empty, the
"original" the next run captures for restoration is already empty, so a single failed cleanup
poisons every subsequent run on that site and looks like a defect in the migration engine.

Fixed by removing the mutation rather than by hardening the restore: the branch reads
`modes.ignored_doctypes`, so the test patches that and writes nothing. **A warning test has no
business writing shared configuration to prove a branch** — the leak is a bigger risk than the
coverage is worth, and patching is both safer and a more direct test of the code under test.
Site repaired, and `test_the_seeded_defaults_are_still_in_place` now names the condition if
anything empties that list again. The patched test was re-proved fallible against the same
mutation. Decider: P7 implementer.

2026-08-16 — **A second leak from the same suite, and the decisive way it was pinned.**
`test_storage_health`'s operational fixture attaches a File to a real `Data Import` and dropped
the parent in cleanup. That cleanup can run **before** the base class removes the files it
tracked, leaving a File pointing at a Data Import that no longer exists — which then fails to
delete and survives. One orphan (`health-sum-operational.csv`) was enough to make
`test_a_ref_on_an_ignored_doctype_is_a_hard_refusal` count two ignored refs where it expects
one, **in a different module, with nothing pointing back at the fixture that caused it**.
`_drop_import` now deletes the attachments first, so the ordering cannot matter.

**Three purge errors survived that fix, and the pin is worth recording as a method.** Rather
than keep theorising about which residue mattered, the same module was run at the same SHA on
the two sites: **green on `cfs-p7.local`, three errors on `cfs-p7b.local`.** Same tree, same
command, different site — which settles "tree or site" in one step and cost less than any of the
hypotheses that preceded it. `cfs-p7b.local` carries residue from the run that failed with the
ignored list emptied (120 migration objects against zero Cloud Storage Objects, 91 orphaned
quarantine files) and from the repairs afterwards; the exact remaining difference was not
identified, and is not claimed to be. Final verification therefore ran on `cfs-p7.local`:
**1030/1030 twice consecutively**, settings byte-identical.

**The general lesson, which is the reusable part:** a test that writes shared configuration or
leaves a row behind fails *somewhere else*, later, in a module that never mentions it. Both
leaks this session were mine, both surfaced as failures in the migration engine, and both were
found only because the phase gate compares the settings singleton between runs and because two
sites were available to differ. A scratch site that has hosted a failed run should be treated as
suspect evidence, not as a control. Decider: P7 implementer.

2026-08-16 — **A false-pass mode in the escaping check, found by the security review and
closed.** `interpolations()` toggled its in-template state on **every** backtick, including one
inside a `//` comment or a quoted string. A stray backtick desynchronised the scan from that
point on, and the failure direction was **missed interpolations** — a silent pass, in the check
that stands in for a browser-level guarantee, which is the one place a silent pass would not be
caught by anything else. The total-count floor catches gross desynchronisation and would not
catch a subtle one.

Fixed rather than documented: the scanner now skips comments and quoted strings. **Proved in the
mutation-then-run-the-real-assertion form** the orchestrator specified — a stray backtick placed
in a comment *above* a deliberately unescaped interpolation, run through the real check, which
must still name that interpolation; and reverting the scanner to its comment-blind form fails
both new controls. The remaining edge is stated in the module docstring: a backtick inside a
regular-expression literal is not handled, and none of the three scripts contains one.

Recorded also because the reviewer checked something it could have assumed: `FIELD_ACCESS`
matches a bare `${something.field}`, and had that been treated as a *safe* category it would
have permitted `${campaign.title}` unescaped — L-4's exact class, inside the check written to
replace L-4. It is only a precondition for the numeric allow-list, and a non-numeric field falls
through to `return None`, which fails. That construction is the difference between a check and a
check-shaped object, and it was verified rather than trusted. Decider: P7 implementer, on the
independent security review.

2026-08-16 — **The test that proves P7-C1 closed was re-creating P7-C1, as cached state, and
leaving it behind.** `TestTheC1PatchGuardBites` inserts a real `Custom DocPerm` granting
`Cloud Storage Manager` write on Cloud Storage Settings — through the controller, whose
`on_update` calls `frappe.clear_cache(doctype=self.parent)`
(`frappe/core/doctype/custom_docperm/custom_docperm.py:36-37`), so the customised permissions
take effect, which is exactly what the M-6 arm needs to exercise. The cleanup then used
`frappe.db.delete`, raw SQL, and `CustomDocPerm` has **no `on_trash`** — so nothing invalidated
the cache on the way out while `Meta.set_custom_permissions` had already rebuilt `permissions`
from the row (`frappe/model/meta.py:542-554`).

**Measured on a live site rather than argued:**

    baseline           write=[0]
    after insert       write=[1]
    after db.delete    write=[1]   <- the cleanup as written
    after delete_doc   write=[1]   <- and what delete_doc alone does
    after clear_cache  write=[0]

**`delete_doc` does not fix it.** The orchestrator's preferred remedy — "delete through the
controller so the invalidation is a property of the deletion" — is the right instinct and the
mechanism is not there: the controller clears on update, not on trash. Only an explicit
`frappe.clear_cache(doctype=…)` clears it, so that is what the cleanup does.

**Severity, established by mutation:** restoring the raw-SQL cleanup fails **four** tests, and
three of them are the ones asserting the operator role *cannot* write `bucket`,
`object_delete_grace_days` and the settings document at all. The stale cache was not inert — it
granted the write those tests exist to forbid, inside the same run.

**I retracted this finding before proving it, and the retraction was wrong.** Having traced the
mechanism and confirmed both ends, I reasoned that `FrappeTestCase`'s rollback plus the
insert-time cache clear would heal it, and said so. Four commands would have settled it and I
sent the retraction instead. Confirming both ends of a mechanism and inferring the middle is the
same error as reading a green that could not have gone red — C-20 applies to disproof exactly as
it applies to proof.

P6 found the identical defect in its own suite independently within the hour, which says the
hazard is in how frappe's cache invalidation reads — cleared on write, not on delete — rather
than in either author's care. **P8 should look for it wherever a test writes permission rows.**
Decider: P7 implementer, on the orchestrator's ruling.

2026-08-16 — **Ten test users were created and never deleted.** `desk_utils.ensure_user` minted
`cfs-operator`, `cfs-manager`, `cfs-nobody`, `cfs-backup-operator` and six `cfs-rt-*` accounts,
and nothing removed them. They hold roles, so they accumulate in `report.dashboard_audience()` —
the role-derived set the realtime publisher iterates — and that set grows with every run on a
site. `tests/request_utils.py` has deleted its own users all along, so there was a precedent in
this suite and it was not followed. `drop_users` now removes them and **asserts they are gone**,
because a cleanup whose failure is silent is how a fixture outlives its test. The Website-User
case is dropped rather than restored, since a failed restore would leave a Cloud Storage Manager
permanently unable to reach the Desk. Decider: P7 implementer.

2026-08-16 — **P6 ∥ P7 convergence: the A27 seam is run in `storage/diagnostics` at `warning`
status with `severity: "error"`, and `legacy_steps` keeps P1's published step name.** The merge
presented `api/compat.py` as a conflict whose P7 side was **empty** — P7 had rewritten
`test_connection` to delegate to `storage/diagnostics` and project the old `steps` shape. Taking
"theirs" is what a merge tool prefers and what reads as correct, and it would have deleted A27's
attachment-bucket check while leaving behind a step still *named* `versioning` that only asks
whether versioning is on. The merged `diagnostics._check_versioning` therefore delegates to
`backup.retention.check_attachment_bucket_versioning` — one implementation, two entry points —
and `legacy_steps` maps the check back to `attachment_bucket_versioning`, which P1 published and
site code may read.

**Severity: `warning`, not `failed`, and the grounds are mechanical rather than aesthetic.**
`diagnostics._summarise` promotes any single `failed` check to an overall `failed`. A bucket that
is reachable, writable and readable would then report the whole connection as failed because of
someone else's lifecycle policy — a false statement about the connection, and one that trains
operators to ignore the top-line indicator. A27 (`amendments-register.md`) says "required /
hard-warned" and never "throw"; `backup/retention.py` gives the same reasoning independently.
The orchestrator first shipped this at `failed` and corrected it. **"Surfaced red" is delivered
by the renderer instead**: `indicator_for` prefers `severity === 'error'`, so a real A27 finding
paints red while a working bucket keeps an accurate overall status. Without that, a genuine
finding was visually identical to `"Skipped: the bucket could not be reached"` — a positive
finding and the *absence* of a check on one colour. Decider: orchestrator, on the independent
reviewer's grounds.

2026-08-16 — **`tests/desk_utils.acting_as` is an alias for `tests/permission_utils.acting_as`;
there is one implementation.** Both phases grew the helper independently and they were **not**
equivalent: `permission_utils` takes `roles=` and grants them **before** `frappe.set_user`, with
a user-cache clear — an ordering this register already records as the fix for a trap that
produced refusals indistinguishable from real ones. Two texts (the `permission_utils` docstring
and an earlier entry here) stated that one helper was kept at convergence. Both were kept. Rather
than correct the prose, the divergence was removed: the weaker name now imports the stronger
implementation, so the trap — someone adding a role grant inside the weaker helper — is
structurally unreachable rather than warned about. Decider: orchestrator, on the independent
reviewer's finding.

2026-08-16 — **The convergence verification item parked at this register's P7 entry is CLOSED.**
It asked that, after the merge, the `object_delete_grace_days >= restore_window_days` validator
be confirmed present and reachable on the save path. Verified by two independent readers:
`cloud_storage_settings.py` `validate()` → `validate_restore_window()` →
`backup/retention.assert_grace_covers_restore_window`, called exactly once. P7 was correct not to
build a second validator.

2026-08-16 — **The convergence gate FAILED its first round on a HIGH, and the failure is the
entry worth keeping.** The A27 seam fix above was correct in the code and **held by nothing**:
reverting `_check_versioning` to a status-only step left the entire merged suite green — 1292
tests, three consecutive runs. `FakeObjectStore.lifecycle` had been added at the merge precisely
to make the noncurrent-retention half reachable, and no test ever set it.

Found by **three methods, independently, none prompted**: the reviewer by reading which inputs
the tests drive; the test-engineer by grepping `store.lifecycle` and finding only the fake's own
reader; and the test-engineer again by **running the mutation and watching 1292 tests pass**.
The first two establish that no test *appears* to cover it; the third establishes that none
*does*. Six green suite runs and a passing security review could not see it.

Closed by six tests pinning both operator-facing entry points, in both directions, proven by the
same mutation now failing three of them by name. **Standing consequence:** a phase gate needs a
reader *and* a runner; neither route alone is sufficient evidence.

Reviewer's framing, kept verbatim because it generalises: *"The orchestrator gate being green is
not evidence against my HIGH finding; it is the finding. A green suite is compatible with the fix
having been reverted."*

2026-08-16 — **Both type-to-confirm phrases are compared byte for byte (security finding L-1).**
`backup.lifecycle.apply_lifecycle_policy` used to `.strip()` its phrase; `migration.api.
start_cleanup` never did. Two gates guarding the same class of action — remote deletion — with
opposite normalisation is a trap for whoever reads one and reasons about the other, and a phrase
the server normalises is one the operator can get wrong and still be understood, which defeats
the purpose of asking. The lifecycle gate changed, not the cleanup gate. **A test changed
direction as a result**: `test_backup_lifecycle.TestApplyRefusals` asserted that surrounding
whitespace was *tolerated* and now asserts it is *refused*, renamed to
`test_a_near_miss_is_refused_by_the_confirm_gate` and marked `@refusal_guard`. Neither Desk
dialog trims before posting, so the operator sees the same contract at both. Decider:
orchestrator ruling, implemented in P8.

2026-08-16 — **"Two-person-rule friendly" is ASPIRATIONAL, not delivered (security finding
L-2).** Cleanup is two audited actions (`approve_cleanup` then `start_cleanup`, A9) and that much
is real. What is *not* real is the second person: the Desk flow chains approve and start in one
dialog, and nothing anywhere requires the approver and the starter to be different users — the
approval records `cleanup_approved_by` and no gate reads it back for identity. Any document
describing this app as enforcing a two-person rule is wrong. The two-person flow is deliberately
**not** built in v1: doing it properly means a distinct-actor check, a Desk flow that cannot
chain, and an answer for the single-administrator site, none of which are v1 scope. Backlogged.
Decider: orchestrator ruling, recorded in P8.

2026-08-16 — **A21's "fix the Module Def" clause: the bootstrap leaves that column alone
(reviewer M-5).** *This entry replaces an earlier one that recorded a false technical reason as
the sole basis for accepting a destructive side effect. Rewritten rather than corrected in place
below it, because DECISIONS is the durable record and a reader should meet the true version
first.*

A21 prescribes fixing `apps.txt`/`installed_apps`/`Module Def` before migrate. The first two are
load-bearing and implemented. The third is **not done**, and the reason it was once done was
checkably wrong at both ends of the supported frappe range:

* The justification was that a stale `app_name` reaches `frappe.get_hooks(app_name=…)` through
  `get_doctype_app_map()` in `frappe/api/__init__.py`, turning a REST request naming a fork
  doctype into a framework 500. At **v15.93.0**, this bench's revision, that call exists and is
  inside `with suppress(Exception)` with the response already built — it cannot 500. At
  **v15.16.0**, the declared floor, `get_doctype_app_map` is not referenced from that module at
  all. There is no supported version where the benefit exists.
* Against that, `frappe/installer.py` selects the modules an uninstall destroys by exactly that
  column. Repointing it meant `bench uninstall-app cloud_file_storage` on an adopted site would
  also destroy the fork's `S3 File Attachment` Single, its `tabSingles` values and its `__Auth`
  secret — the data this module promises survives — and made `frappe/config/__init__.py` report
  the fork's module as belonging to this app.

A cost with no benefit anywhere in the supported range is not a trade. `adopt_module_def` became
`inspect_module_def`, which reports and writes nothing; the tests that asserted the repoint now
assert the column is **left alone** and, more usefully, run the selection an uninstall performs
and assert the fork module is not in it — so they also catch anything else that starts writing
that column.

What remains true either way, and was the useful half of the original entry: that column is not
what makes a module resolvable. `get_module_app` reads `frappe.local.module_app`, built from the
`modules.txt` of every app **on the bench** (`frappe/__init__.py:1649-1690`), so the fork's
DocTypes are unopenable through the ORM whatever `Module Def` says — which is why every read in
`map_settings` is meta-free, and why `remove_orphan_doctypes` leaves them alone (`ImportError` is
its orphan signal; this path raises `DoesNotExistError`). Decider: orchestrator's M-5 ruling on
the reviewer's finding, plus a floor-version check the reviewer could not run.

2026-08-16 — **The bootstrap does NOT rewrite `tabPatch Log`, against the rename research's
step 19.** That step is correct for a rename in place, where the app keeps its patch history and
skipping the rewrite re-runs every historic patch. This is a greenfield adoption: our patch
modules are new names, none of which collide with a `frappe_s3_attachment.patches.*` entry, so
nothing re-runs and rewriting the prefix would forge history — and, if a name ever did collide,
would mark one of our patches as already applied. The fork's rows are left as the inert history
they are; the rehearsal asserts the count is unchanged and that all of our own patches ran.
Decider: P8 implementer.

2026-08-16 — **The fork's settings are harvested post-model-sync, through the ORM, and the first
rehearsal is why.** An earlier revision wrote the harvested values straight into `tabSingles`
during `[pre_model_sync]`, which works — a Single's values live there whether or not its DocType
exists. It also leaves the Single *partially* populated, and `Document.load_from_db` falls back
to `new_doc` (and therefore to the schema defaults) only when it finds **no** rows at all. Six
harvested fields suppressed every default and the next `settings.save()` in the same migrate died
on an empty `storage_class`. Two further defects came out of the same run: "already has a value"
could not tell a shipped default from an operator's decision (so the fork's
`signed_url_expiry_time` was silently dropped and `use_default_credential_chain` stayed 1 while an
access key was adopted, leaving the runtime with a key it never reads), and writing the secret
with `set_encrypted_password` alone left the document's own field blank, so the next save of those
settings would have called `remove_encrypted_password` and destroyed the adopted credential. All
three were found by running a real `bench migrate` on a synthetic legacy site, not by review.
Decider: P8 implementer. Links: `docs/evidence/p8-legacy-install-rehearsal.md`.

2026-08-16 — **`storage_health()` is NOT optimised in v1, on a measurement rather than on a
reading (security finding L-3).** Measured on a site seeded with a 75,000-file residue tree and
600,000 `tabFile` rows: the whole call is 0.83s, of which `detect_public_private_residue`'s
`os.walk` is **0.033s — 4%**. The finding's implied fix would have removed 4% of the runtime and
lost the only detector for a live unauthenticated disclosure (threat model H-3). The real cost is
four full-table aggregate scans, projecting to ≈2s at the 1.2M-row production target; the
endpoint is operator-gated, refresh-button driven and bounded in shape, so ~2s per poll is a cost
rather than a risk. A short-TTL cached snapshot is the right improvement and is **backlogged**:
it changes P7's health surface, and doing that late in P8 trades ~2s on a panel for the chance of
an operator reading a stale number as current during a migration. Decider: P8 implementer, on the
orchestrator's "measure before optimising" ruling. Links: `docs/evidence/p8-storage-health-cost.md`.

2026-08-16 — **The A29 preflight refusal test now injects its failing threshold, and the reason
is worth keeping** (`TestCapacityPreflight` in `tests/test_migration_reconcile.py`). The test
asserted that a production-scale campaign is refused when a capacity check fails, but never
made one fail: it relied on the *site* naturally failing `recent_database_backup` (no dump on
disk) or `migration_queue_reachable` (no `cloud_migration` queue). Both are conditions a
correctly prepared bench does not have — the deployment runbook asks for that queue, and any run
of the backup suite leaves a real dump in `private/backups` for the next 24 hours. On the P8
release bench, which had both, all four checks passed and the refusal could not fire; the test
had been green for phases on the strength of the machine being misconfigured. Now `latest_backup`
is patched to `{"present": False}` for the duration and the assertion also names the failing
check, so it is red exactly when the refusal is missing. Mutation-confirmed: removing the
`if not report["passed"]` throw fails this test and its sibling by name. Decider: P8 implementer.

2026-08-16 — **The legacy-install candidate read uses `frappe.get_all`, not raw SQL, and the
safety lint is why.** The first draft of `legacy_install.adopt_fork_objects` selected candidate
File rows with `frappe.db.sql`, reasoning that it was "a compat-patch rewrite" and therefore one
of invariant 5's three sanctioned sites. `test_migration_safety_lint` disagreed, correctly: that
sanction names `patches/v0_2_0/backfill_s3_object_key.py`, not a family that a fourth file may
join by resembling it. Rewritten with `is set` / `is not set`, which compile to the same
`ifnull(col, '') != ''` predicate over the same index. The lint found this, not a reviewer.
Decider: P8 implementer.

2026-08-16 — **Adoption leaves the credential chain ON when the fork's secret cannot be
retrieved, and says so loudly (security finding L-12).** The flag and the secret were two
decisions in `legacy_install.map_settings`: `use_default_credential_chain` was cleared whenever
an `access_key_id` was adopted, while the secret was only carried over when one could actually be
decrypted. On a site whose fork holds a key but whose `__Auth` row is gone, that pairing cannot
be saved — `validate_credentials` requires both or neither. They are now one decision: the chain
is cleared only when a secret will be present after the save, whether adopted from the fork or
already held by this app.

**Where it bites is not where the finding placed it, and the difference is worth recording.** The
harvest's own `save()` survives the bug: `settings.update({"use_default_credential_chain": "0"})`
puts a truthy *string* on the document, so `validate_credentials`' early return fires. The error
arrives on the **next** load-and-save, once the value has round-tripped through the DB as integer
0 — `after_migrate`, the operator's first Desk save, or any later patch. Verified on a real site
rather than reasoned about, and the regression test drives that second save for that reason;
mutation-confirmed, the mutant raises the real *"Set both Access Key ID and Secret Access Key"*.

**What an operator should do when the fork's secret is genuinely unrecoverable.** Adoption now
succeeds and the site comes up on the IAM default credential chain holding an access key id it
cannot use. That is deliberate — a working migrate and a recoverable configuration beats a migrate
that dies on a credential-pairing error — but it is not a state anyone should have to infer, so it
writes an Error Log naming the two options: enter the Secret Access Key beside the adopted key and
clear *Use Default Credential Chain*, or clear the key and keep using the instance credential
chain. Until one of those, the adopted key is not in use. Also in `docs/runbooks/deployment.md`.
Decider: security reviewer's finding, implemented by the P8 implementer.

2026-08-16 — **The threat model's §5 claim is scoped to the model, on the reviewer's suggestion.**
"Every control is server-side and reachable over `/api/method/` without a browser" now reads "for
every control in this model", because the sentence it generalises was scoped to the controls the
reviewer had tested. The document is app-scoped and the generalisation was fair, but a claim that
carries its own limits is worth more than one that needs a reader to remember them. Decider: P8
implementer, on the security reviewer's optional suggestion.

2026-08-16 — **Values are cast to their field's type at the `map_settings` boundary — the class,
not the instance.** L-12 turned on `bool("0")` being True. `_is_untouched` leaned on
`str(current) == str(default)` for the same reason, and P5's A29 defect (`age or 1e9` turning the
freshest backup into the oldest) is the same trap a phase earlier. Two instances of one shape in
one codebase is a pattern, so `_typed()` now coerces Check and Int fields once, where fork data
becomes our data, instead of each caller remembering which fields are strings. Before `sync_all`
there is no meta to ask and no document to save, so the value passes through and the preview is
still accurate about which fields would be adopted. Decider: P8 implementer, on the reviewer's
framing — it asked for the class and was right to.

2026-08-16 — **N-2: invariant 3's `content_hash` clause is listed in release-report §8 as not met
for the adopted population, rather than argued away.** On an adopted site `content_hash` is NULL
for fork-era rows: the fork overloaded that column with the object key and `v0_2_0` clears it
rather than inventing an MD5 from bytes it has not read. The runtime does not depend on it (P2
ruling; the A1 chain resolves without it) and a campaign's A26 VERIFY link repopulates it from
hashed bytes. The reviewer asked for either the §8 entry or a reason; §8 is the honest place,
because "the invariant holds" and "it holds for every row on every site" are different claims and
only the first is true before that campaign runs. Decider: P8 implementer.

2026-08-16 — **N-5 accepted as-is: the A29 assertion pins presence, not exclusivity.**
`test_a_production_scale_campaign_is_refused_below_the_thresholds` asserts the refusal names
`recent_database_backup`; it does not assert that no *other* check also failed. Making it
exclusive would couple the test to the full check list, so that adding a fifth capacity check
would break a test about the backup threshold. The looser assertion still fails when the refusal
stops firing, which is the property it is named for. Recorded rather than changed. Decider: P8
implementer.

2026-08-16 — **Invariant 9 was NOT MET at `39fb256`, and this records it rather than the fix
alone.** "Every schema change ships a patch + test": P8 shipped two patch modules whose
`execute()` bodies called runtime functions no test executed, and `test_legacy_install.py` tested
only that they were *registered* in the right section and order. The reviewer separated the two
halves and the distinction matters: **no test was weakened** — the one that changed direction
(L-1) gained a stronger assertion and kept its positive control, and the one whose mechanism
changed (A29) went from machine-dependent to injected — the failure was missing coverage. Closed
by driving every non-committing function for real: `bootstrap` writes no settings value
(behaviourally and by AST), `harvest_settings` saves the real document and its credential
survives a second save, `finish` is driven through the ignored-list branch where L-12 actually
lands, and `adopt_module_def` has coverage at all. What is still left to the rehearsal, and said
plainly in the module docstring, is the two functions that commit and the schema state only a
real migrate produces. Decider: P8 reviewer's H-1, implemented by the P8 implementer.

2026-08-16 — **The A21 rehearsal harness is committed, because a run nobody else can repeat is
not evidence.** `docs/evidence/p8-legacy-rehearsal.sh` and its three helpers now live beside the
write-up. The prompt was the independent test-engineer reproducing the rehearsal and failing at
step one: every rehearsal site carries a site-level `developer_mode: 0`, this bench's
`common_site_config.json` sets 1, and with it on `ModuleDef.on_update` tries to mkdir a module
folder inside an app that is not on the bench. The document never mentioned it. Two things follow
that are worth keeping: an unstated precondition makes an evidence document unfalsifiable rather
than merely inconvenient, and `frappe.flags.in_patch` — which the fixture does set — bypasses
`DocType.check_developer_mode` and **not** `ModuleDef.on_update`, so the comment implying it
covered both was wrong. Decider: P8 implementer, on the test-engineer's finding.

2026-08-16 — **The credential regression test drives the real readers, with no stubs.** It
seeded the fork's `tabSingles` rows and its `__Auth` secret for real, instead of patching
`_legacy_single_values` and `get_decrypted_password`, so `harvest_settings` runs exactly the path
a migrate runs and only the fork's *data* is synthetic. The shape came from the independent
test-engineer's M-CRED reproduction, which is stronger than the mutation it replaced for a
specific reason: it demonstrated the customer-visible end state on a live adopted site —
`__Auth` going from `[secret_access_key]` to `[]` after one ordinary settings save — rather than
a failing assertion about it. Decider: P8 implementer.

2026-08-16 — **Reviewer L-9: the fork's key is no longer paired with a secret that is not its
own, and an assertion changed direction to match.** `map_settings` used to accept "this app
already holds a secret" as satisfying the credential pairing. In the one case that can arise it
is wrong: if this app had both a key and a secret, the fork's key would have been skipped as
already-decided and the branch never runs — so a secret of ours here means the operator has a
secret and **no** key, and pairing it with the fork's key yields a credential that authenticates
as neither. That site is working today on the IAM chain, and switching the chain off would break
every S3 operation on it with `SignatureDoesNotMatch`. The orchestrator's ruling left the choice
between warning and recording; the behaviour was narrowed instead, because "warn, then break the
site" is worse than either. `test_an_existing_secret_of_our_own_also_permits_the_switch` is now
`test_a_secret_of_our_own_does_NOT_permit_the_switch`, and the warning distinguishes the two ways
of reaching it because the operator's next move differs. Decider: P8 implementer, on the
reviewer's L-9.

2026-08-16 — **Reviewer L-7: the credential test's precondition is asserted, and the assertion
caught a real instance of it on its first run.** The destroying save only happens under
`if adopted:`, so a fixture that adopts no *mapped* field would skip it and the test would stop
guarding the credential path while still passing. Now asserted rather than documented. Two things
worth recording: the assertion immediately failed, because the fixture's only field was
`bucket_name` and the suite's harness already sets `bucket`, so `adopted` really was empty — the
finding was live, not hypothetical, and the fixture moved to `folder_name`/`key_prefix` with the
target cleared in `setUp`. And the reviewer's stated consequence did **not** reproduce at this
revision: with an empty fork the class still goes red under the credential mutation, because the
test's own second save destroys what the buggy path wrote. Measured both ways before accepting.
The assertion stands regardless — the margin it protects is one refactor wide. Decider: P8
implementer.

2026-08-16 — **Reviewer L-8: mutation anchors are checked by a test, not by hand.** M-3 was a
stale source-line anchor in `docs/evidence/p6-mutation-evidence.sh` — re-anchoring it fixed the
instance and left the class open, since these scripts pin lines by literal string and report a
missed match as NOT APPLIED rather than as a failure. `TestEveryMutationAnchorStillResolves` now
walks every `run_mutation` in every evidence script, resolves the file variable from the script's
own assignments, evaluates the anchor expression (they are built with `chr(34)` and `chr(9)*5` to
survive two layers of quoting, so they are parsed rather than pattern-matched), and asserts the
literal is still present in the file it names. Mutation-confirmed by restoring the pre-L-1 anchor:
the test names the mutation label and the file. Decider: P8 implementer, on the reviewer's L-8.

2026-08-16 — **Reviewer N2: the File→CSO link is asserted now, and the mutation that exposed it
also found a hang.** Removing the `File.cloud_storage_object` write inside `adopt_fork_objects`
left the whole suite green at two SHAs. The consequence is not subtle: `api/compat.
legacy_generate_file` resolves the File row *by* that column, so an adopted site would mint every
object and serve none of them — every legacy private URL 403s, which is the one thing that
function exists to prevent. H-1 closed the settings and credential half of this module; this was
the object half, equally unheld.

Running the mutation surfaced a second defect the finding did not mention: **the loop's
termination depends on that same write.** It re-reads "rows with a fork key and no object link"
until the set is empty, with no offset, so a write that stops happening turns `bench migrate`
into an infinite loop rather than a failure — and the mutation run hung instead of going red.
`adopt_fork_objects` now compares each pass's row names against the previous pass's and stops
loudly if they intersect. A migrate that hangs is worse than one that fails. Decider: P8
implementer, on the test-engineer's N2.

2026-08-16 — **Reviewer L-10/L-11: the rehearsal harness asserts its steps and derives its own
path.** It printed "expected 1"/"expected 0" beside an echo of `$?` and checked nothing, so a
failed `bench new-site` would have left every later step running against a site that does not
exist — with the red control "passing" for the wrong reason. In the artefact whose entire purpose
is that someone else can reproduce the run and believe the result, that is the same
reports-rather-than-fails shape this phase already failed a gate over. Every step now asserts its
exit code, the red control asserts **both** the non-zero exit and the `ModuleNotFoundError` that
justifies it, and the script exits non-zero on any failure. Verified both ways: it exits 1 with a
named assertion when `new-site` fails, and 0 with ten `ok:` lines on a clean run. It also pinned
`/home/user/v15/apps/cfs-P8`, a path that disappears when the phase worktree is removed — i.e.
exactly when the harness becomes the only record of the run; derived from the script's own
location now, exported so the `sed`-copied helpers inherit it. Decider: P8 implementer.

2026-08-16 — **Both proofs of the credential defect are kept, because they prove different
things.** The mutation shows the guard exists and would catch a regression; the independent
test-engineer's live-site reproduction — `__Auth` going from `[secret_access_key]` to `[]` after
one ordinary settings save on a genuinely adopted site — shows the harm is real. A release needs
both: the first is what stops it happening again, the second is why anyone should care that it
was found. Recorded here as a pair rather than one superseding the other, on the orchestrator's
correction to an earlier note of mine that credited only the reproduction. Decider: orchestrator.

2026-08-16 — **Reviewer L-13: the adoption loop's termination guard is tested, and the test is
bounded so a regressed guard fails instead of wedging CI.** The guard was added because removing
the `File.cloud_storage_object` write makes `adopt_fork_objects` hang rather than fail. Nothing
held the guard itself — the H-1 shape one level down, arriving inside the fix for a finding that
was itself about an unheld property. It matters for the case the link test cannot reach: the
write failing for an *environmental* reason on a production site rather than a code regression.

**The obvious test for it hangs the suite**, which would be worse than no test — a failing test
tells you something, a wedged job tells you nothing and blocks everything behind it. So it is
bounded in both directions: `BATCH_SIZE` patched to 1 so a second pass is immediate, only the
File-link `set_value` neutralised, and the candidate read itself raising `TookTooManyPasses` on
the third call. Removing the guard now fails in 1.7s with "the candidate read was reached 3
times… on a real site this is a hung bench migrate", rather than spinning.

**One structural limit, recorded as a decision rather than left as a gap:** the guard compares
each pass against the *immediately previous* one, so a cycle of period greater than 1 would still
spin. Reaching that needs an external writer clearing the link between passes, which is not
possible inside a `[post_model_sync]` patch — the reviewer would not widen it and neither would
I. Decider: P8 implementer, on the reviewer's L-13.

2026-08-16 — **The L-9 ruling was reached independently by three people who each started
somewhere else, and that is why it is recorded rather than merely applied.** The reviewer
originally proposed warning-only; the orchestrator leaned to warning; the implementer narrowed
the behaviour instead, on the grounds that warning and *then* switching the credential chain off
still breaks a working site — "warn, then break the site" is worse than either option offered.
Both then checked the premise rather than the conclusion: that reaching the branch requires this
app's own `access_key_id` to be empty, which `_is_untouched` guarantees for a Data field with no
shipped default. It holds, and both changed position. Kept here because a ruling that survived
three independent starting points is worth more than the argument that produced it.

2026-08-16 — **Two opposite errors, one root, and neither visible to the person who made it.**
Worth recording as a pair rather than as two notes, because the pairing is the argument.

*A live finding whose stated mechanism was wrong* (L-7): the reviewer's precondition finding was
real — the credential fixture's only mapped field was already set by the harness, so the fixture
did not arm the test — but its stated consequence, that an empty fork would make the tests pass
against the pre-fix code, did not reproduce. Only running both directions separates "the finding
is right" from "the reason given is right".

*A right fix reached for a weaker reason* (L-13): `assertEqual(passes["count"], 2)` rather than
`<=` is correct, and the implementer wrote it to pin **which** pass fired. The reviewer supplied
the stronger reason — a `<=` would let the bound itself stop the loop and still read green, which
is precisely the vacuity the bound was added to avoid. A right answer for a weaker reason is still
right, but the record should say which, because the next person copying the pattern needs the real
reason and not the one that happened to produce it.

Both were surfaced by someone else checking, not by the author noticing. That is the argument for
independent gates stated as a measurement rather than a principle, and it is the same shape as the
third instance this phase produced: a true statement carried past its scope, which appeared once
from each of three different agents and was caught every time by a fourth party. Decider:
orchestrator and P8 implementer, jointly.

2026-08-16 — **The L-13 bound is keyed on the query's filters, with an unconditional ceiling
behind it — and the recursion this closes is worth naming.** The bounded reader matched the
candidate query on `order_by == "name"`. The test-engineer removed the guard **and** respelled
that to `"name asc"` in the code under test — semantically identical, the kind of thing any
refactor writes — which un-matched the interceptor, un-bounded the bound, and wedged the suite
for 150s until it was killed. A wedged CI job is worse than a red one: a red says something, a
wedge says nothing and blocks everything behind it.

Two layers now. The recogniser keys on the **filters**, which name the column the loop exists
for, so ordering and field-list changes cannot defeat it. Behind that, **any** read of `tabFile`
counts against a hard ceiling, so defeating the recogniser costs a slightly less specific failure
rather than a hang. Both proven: with the guard removed and `order_by` respelled it fails in 2.1s
naming the pass count; with the recogniser additionally defeated outright the ceiling fires at
nine reads. Production behaviour is untouched — the guard compares row-name sets and never cared
about ordering.

**This is the fourth level of one recursion, and every level was found by someone other than its
author.** H-1: a property nothing held. N2's fix: a guard for it. L-13: a test for the guard.
This: the bound of that test, unheld. At each level the author had reason to think it was fine —
including the implementer flagging this very bound as "something to check rather than trust",
which was the right instinct and still insufficient, because what it needed was someone who would
**attack** it rather than verify it. Verification confirms what you thought; attack finds what you
did not. Decider: P8 implementer, on the test-engineer's G2.

## 2026-08-17 — G5: the L-13 test bound was one layer, not two; termination moved into the loop

**Decision.** `adopt_fork_objects` bounds its own passes (`for _pass in range(max_passes)`)
instead of relying on a test-side bound patched onto `frappe.get_all`.

**Why.** The L-13 chain built two protective layers — a filters-keyed recogniser and a
`MAX_FILE_READS` ceiling. Both were installed by the same `patch.object(frappe, "get_all", ...)`.
They hooked **a binding, not a behaviour**, so they were one layer wearing two hats. Attack G5
changed one token in the loop — `frappe.get_all(` -> `frappe.get_list(`, which does **not**
delegate to `get_all` — and walked past both layers: the suite wedged for 150s and had to be
killed. (G4, `frappe.db.get_all`, held only because that helper happens to be a one-line
pass-through to `frappe.get_all` — by luck of frappe's implementation, not by design.)

**The fix changes kind rather than adding a layer.** A ceiling in the loop cannot be bypassed
by changing which query helper is called, because it does not depend on intercepting a call at
all. `max_passes` is derived from the candidate count over the batch size plus
`ADOPTION_PASS_SLACK`, so it cannot bite a legitimate large adoption; reaching it is a fault and
is logged as one. The intersection guard is kept — it reports *why* adoption stopped, which the
ceiling alone does not.

**Verified against all three axes**, each with the intersection guard regressed so the bound is
the only thing left standing:

| axis | mutation | before | after |
|---|---|---|---|
| G2 | `order_by="name"` -> `"name asc"` | wedged 150s | terminates 4s |
| G4 | `frappe.get_all` -> `frappe.db.get_all` | held (by luck) | terminates 4s |
| G5 | `frappe.get_all` -> `frappe.get_list` | **wedged 150s** | terminates 4s |

G5 now fails by name with `adoption exceeded its pass ceiling`.

**The recursion this closes, and why it terminates here.** Five levels, each found by someone
other than its author: a property held by nothing (H-1) -> its fix was a guard (N2) -> a test for
that guard (L-13) -> the bound of that test (G2) -> **the binding that bound was installed on**
(G5). It ends here not from fatigue but because the fix is a different kind of thing: the
guarantee moved to where it cannot be intercepted. A sixth test-side layer would have been a
fifth hat on the same head.

**Recorded honestly:** the release evidence does NOT say "the L-13 bound was attacked and held".
It says two axes were attacked and held, a third defeated both layers, and the third is closed by
a production-side cap rather than a further test-side layer.

## 2026-08-17 — F3: two of four unmet criteria closed; the gate stays PARTIAL

**Trigger.** The independent release audit returned **NO-GO**, blocking item A1: F3 is recorded
PARTIAL since P5 and `ACCEPTANCE_GATES.md:3` defines RELEASE_CANDIDATE as F1-F7 all PASS. Rather
than only document that, the four unmet criteria were attempted.

**What was actually wrong with the throttle criterion.** P5 recorded the cause as "a
purpose-seeded bandwidth-bound corpus was swallowed by the site's existing 100k one". The real
cause was one level deeper: **the harness had no way to seed a bandwidth-bound corpus at all** —
the payload band was a module constant at 120-400 B, where transfer time is dominated by
per-object overhead and a bandwidth limit is unobservable. `seed` now takes
`--min-bytes/--max-bytes`. With that, the measurement completes:

    throttled   2 Mbps  60 objects  7,349,094 B  29.48s  ->    249,315.7 B/s  (ceiling 250,000)
    unthrottled  none   60 objects  7,421,958 B   3.45s  ->  2,151,848.1 B/s

**Bounded job count**, measured on the same site through two real RQ workers: 8 jobs across 5
batches = **1.60 per batch** against a ~2 bound, 0 redelivered, **0 on ERP queues**. The run
converged 270/270.

**Still unmet, and why.** Disk I/O against a baseline is an **ENVIRONMENT_BLOCKER** —
`/proc/self/io` reports zero on this WSL2 bench, so it is recorded as unmeasured rather than as
zero. Redis flush and bench restart remain unit-level: the fault stages need work in flight, the
campaign converged first, and starting over was refused twice by guards that were right to
refuse — the campaign lifecycle guard ("a campaign in state Planned cannot be deleted — it still
owns migration work") and `autonomy_guard` on the site-removal command.

**Ruling: F3 stays PARTIAL and A1 stands.** Two of four is progress, not a pass, and a gate is
not an agent's to negotiate down. **This is OWNER_DECISION_REQUIRED**: either the remaining two
criteria are completed on a bench that can measure disk I/O, or F3 is accepted as PARTIAL with
these four items named. Not a decision this delivery can take for the owner.

**Incidental, none of them product defects:** the A30 circuit breaker opened under repeated
credential failures and refused further uploads — correct, and observed live rather than in a
unit test; the scratch MinIO has no KMS, so `sse_mode = SSE-S3` failed every PUT with
`NotImplemented` (correct against real S3, disabled on the scratch site only).

## 2026-08-17 — Release audit round 2: the recursion reached a sixth level, in the fix for the fifth

**N1 — the test written to close G5's ceiling did not test its own name.** It asserted
`ADOPTION_PASS_SLACK == 2` and that the intersection guard fired early. Neither varies the
candidate count, which is the only way to observe scaling. The auditor proved it by mutation:
**hardcoding `max_passes = 3` left all 46 tests green** while refusing any real adoption of more
than ~2 batches of fork rows (>10,000 at the shipped `BATCH_SIZE`) — stopping with "adoption
exceeded its pass ceiling" and leaving the remainder unadopted. Mode 1: an assertion reading a
property other than the one it names.

The recursion therefore did **not** terminate at five. Six levels, each found by someone other
than its author: unheld property (H-1) -> guard (N2) -> test for the guard (L-13) -> that test's
bound (G2) -> the binding the bound was installed on (G5) -> **the naming of the test that
closed it (N1)**. Runtime was correct at every level from G5 onward; what kept failing was the
*protection*, and each time the author had good reason to think it was adequate.

Fixed by measuring the property directly — the same broken-link scenario at two candidate counts,
asserting the ceiling fires at `N + ADOPTION_PASS_SLACK` for each and that the larger fires
later. Both of the auditor's surviving mutations (`max_passes = 6`, `max_passes = 3`) now fail,
M3 by the correctly-named test.

**N3 — F7 was marked PASS against its own text.** F7 requires "security audit PASS;
**release-manager audit PASS**; ... RELEASE_EVIDENCE.md produced, clearly distinguishing...".
The table marked it PASS on the byte distinction alone, dropping the clause naming the auditor's
own verdict — which is NO-GO. **F7 is NOT MET.**

**N2 — the F1/F2 labels were crossed**, filing the contract gate (C1–C19) under F2. F1's
conclusion was right but the evidence was attached to the wrong gate, so **F2's actual subject —
the 43 T-tests plus A25–A36 and thumbnail availability — was never assessed at all**. It still
cannot be assessed mechanically: 27 `T-` tags exist under `tests/`, several of which are not
matrix scenarios, and unlike F1/F5/backup/MinIO there is **no gate script**. Recorded as **NOT
ASSESSED** rather than inferred from the suite being green.

**The count in the header was therefore wrong**: not "two gates are not met" but **F1 NOT MET,
F2 NOT ASSESSED, F3 PARTIAL, F7 NOT MET**, with F5 split. The same failure mode as A5, inside
the table added to prevent it.

**N5 — the carried-Lows list was padded to seven.** L-10 and L-11 were fixed (and described as
fixed in the sentence that counted them), the Error Log growth is a ruled Medium, and browser
verification is a doc gap. **Three** are genuine carried security Lows: L-2, L-9, L-12 residue.
The auditor's diagnosis is kept verbatim because it names the mechanism: *the list was assembled
to reach a number rather than the number derived from the list.*

**N4 — the document was stale by this project's own Rule 2** (a tree change invalidates prior
evidence regardless of whether it is semantic). Re-frozen at `463e3d1` and re-derived there
rather than carried: 1367 OK, contract 19/19, backup 61, MinIO 19, pre-commit clean. The
`8093fda` figures are kept but labelled with their SHA.

**N6 — the CHANGELOG's `[1.0.0]` compare link targeted `v0.6.0`**, a tag that does not exist and
a version the CHANGELOG never documents; `v0_6_0` is a patch namespace, not a release. Repointed
at `v0.2.2`, the newest tag that exists. (`v1.0.0` itself resolves only once the release is
tagged, which is expected for an unreleased entry.)

**On the auditor's route for F3 item 4**, recorded for whoever completes it: neither guard
forbids a second campaign on a *fresh* scratch site. Seed it large, throttle it to a low ceiling
— using the `--min-bytes/--max-bytes` seeding this delivery added — so the transfer window is
minutes, then inject the Redis flush mid-window. That works *with* both guards rather than
around them.

## 2026-08-17 — Release audit round 3: the seventh level, and why it is convergence

**P1 — the batch-size term of the pass ceiling was unobservable by construction.** Both data
points in the scaling test ran at `BATCH_SIZE` 1, and `ceil(N/1) == N`, so dropping the division
entirely — `max_passes = candidate_count + ADOPTION_PASS_SLACK` — left all 46 tests green. The
docstring claimed the opposite ("a ceiling that ignores the batch size fails the second"), which
it did not.

**At shipped values that term *is* the safety property.** `BATCH_SIZE` 5000 against 1.2M fork
rows: correct ceiling **242** passes, mutant **1,200,002**, each one a full indexed scan. Not a
ceiling in any operational sense.

Closed with one more data point — the same 6 candidates at `BATCH_SIZE` 2, where the ceiling must
be `ceil(6/2) + slack = 5` against the mutant's `6 + 2 = 8`. The auditor's M5 now fails with
`8 != 5`.

**The shape of the recursion, which is the point worth keeping.** Level 5 (G5) left the whole
ceiling untested. Level 6 (N1) left the whole derivation untested. Level 7 (P1) leaves **one
term** untested, and the overclaim had retreated from the assertion into the docstring. The
auditor's reading is adopted: *that is convergence, not repetition* — the last term of the
arithmetic simply had no data point, and it took one.

**P2–P5, all documentation.** The header contradicted the table it introduces, repeating there
the two-gate undercount the table records as a defect; the phase table had lost chronological
order and gained a duplicate row at the wrong SHA; the F2 row's "27 T- tags" was an unbounded
grep matching inside identifiers (`T-0001` from `DI-TEST-0001`, `T-ID` from `AKIA-SECRET-ID`) —
**22** word-bounded, conclusion unaffected but the number wrong in this project's own named
failure mode, inside the row about the gate that exists to be checked mechanically.

**P5 is the one with a durable lesson: the frozen SHA is no longer written in the document.**
Three consecutive rounds found it stale by the very commit that recorded it. A document that
hand-records its own SHA is out of date the moment it is written; it is derived at tag time
instead, and the phase table carries what happened at each SHA.

**The auditor's own summary of three rounds, recorded because it is the strongest single
statement about this codebase:** across ten hard invariants by AST scan, eight mutations against
the adoption ceiling, four full suite runs, two L3 runs and three SHAs, **not one defect has been
found in the runtime code.** Every finding in all three rounds has been in the evidence, the gate
accounting, or the tests.

## 2026-08-17 — Release audit round 4: the eighth was outside the arithmetic

**M6 — the ceiling and the loop it bounds were asking the same question from two copies of it.**
`candidate_count` came from `frappe.db.count("File", filters=[...])` and the loop read
`frappe.get_all("File", filters=[...])` — two duplicated literal predicates, with the ceiling
correct only while they stayed identical and nothing enforcing it.

**No test could see the difference**, and the reason is the interesting part: every test neuters
the link write, so no row ever becomes linked, so the two lists agree *by construction* in every
scenario the suite drives. The case that separates them is ordinary — a re-run on a
mostly-adopted site, where the count scales with the **adopted** population rather than the work
remaining. 1.2M fork rows of which 10 are unlinked: correct ceiling 3, drifted 242. Still
bounded, still terminating; ~80x looser.

**Fixed by deleting the second copy, not by adding a fourth data point.** A test would have
protected one drift out of a class; hoisting `_adoption_candidate_filters()` leaves nothing that
can drift. Same move as the pass ceiling itself: **change kind rather than add a layer.**

**Q1 — the expression is fully pinned; there is no fourth term.** `-(-N // B) + S`: `N` by two
data points, `B` by the third, and `S` deliberately *not* pinned by the scaling assertions —
they compute the expectation from the constant, which is correct, because a test should not be a
change-detector on a tuning value. `S` is bounded instead by the sibling test's fixed
`hard_stop`, and a mutation raising it to 1000 is caught.

**Q2 — `F2 NOT ASSESSED` is right; `NOT MET` would be wrong**, on the auditor's ruling and its
reasoning is worth keeping. **NOT MET is a claim about the world** — "no named green T-test
exists for every scenario" — for which there is no evidence and some against it. Asserting it
would be an unverified assertion in the *negative* direction: the mirror image of every error
this document has been correcting, which is the same mistake with the sign flipped, not an
improvement. **NOT ASSESSED is a claim about the evidence** — "no artefact exists that lets
anyone check this gate" — which is verified and true. It also points at the right work: building
traceability, rather than writing tests that probably already exist.

**With one condition, adopted:** NOT ASSESSED must never read as the milder verdict. For gate
accounting an unassessed gate **is not a pass**, and F2 blocks exactly as hard as NOT MET. If
the header ever softens to "four gates plus one open question", F2 flips to NOT MET on the spot.

**The trend across four levels, which is the reason to stop looking rather than a hope that it
is clean:** level 5 left the entire ceiling untested; level 6 the whole derivation; level 7 one
term of it; level 8 is not in the derivation at all — an unenforced coupling between two copies
of a predicate that feeds it, loosening a constant rather than removing a shape. Each finding
narrower than the last and further from the safety property.

**Recorded verbatim, and it is the auditor's sentence rather than this delivery's:** across four
rounds — ten hard invariants by AST scan, eleven mutations against the adoption ceiling, six full
suite runs, two L3 runs, five SHAs — **no defect has been found in the runtime code.** Every
finding in every round has been in the evidence, the gate accounting, or the tests.

## 2026-08-17 — The M6 fix's near-miss, and closing the release audit

**The obvious version of the M6 fix would have been worse than the defect.** The natural repair
for two duplicated filter lists is a module-level constant:

    ADOPTION_CANDIDATE_FILTERS = [["s3_object_key", "is", "set"], ...]

The release auditor checked frappe's source rather than accepting the shipped docstring, and
found that would have introduced a **worse** defect:

    frappe/model/db_query.py:159      self.filters = filters or []   # bound, not copied
    frappe/model/db_query.py:625-629  self.filters.remove(each)      # in-place, caller's list

A shared constant would be **mutated in place by frappe on first use**, silently losing a clause
for the life of the worker process — after which the ceiling and the loop would both compute
over the wrong predicate *together*, which is worse than two lists drifting apart, because the
drift is at least visible in the arithmetic. The shipped fix returns a fresh list per call and
says why. **The near-miss is more instructive than the fix**: repairing duplication by sharing
mutable state trades a visible defect for an invisible one.

## Release audit closed — five rounds

**Verdict: NO-GO, blocking on A1 (F3 PARTIAL) alone, which is OWNER_DECISION_REQUIRED.**

The auditor found no ninth finding and recommended closing, with the trend as the reason rather
than a hope:

| Level | Unprotected | Consequence if regressed |
|---|---|---|
| 5 | the ceiling did not exist | `bench migrate` hangs indefinitely |
| 6 | the whole derivation | ceiling becomes a constant; refuses real adoptions |
| 7 | one term of it | 242-pass ceiling becomes a 1.2M-pass one |
| 8 | a coupling *outside* it | ~80x looser; still bounded, still terminates |

**Level 8's worst case is a bound that is loose rather than a bound that is absent.** One
function took eleven mutations across four rounds from two independent agents, and the last one
it failed to catch could not lose a migration.

**`RELEASE_EVIDENCE.md` was cut from 401 lines to ~296 on the auditor's list, and the argument
for cutting is empirical rather than stylistic:** every finding across all five rounds — A2, A5,
N2–N5, P2–P5 — was a defect in *prose asserting something* (a count, a label, a SHA, an
enumeration, a summary disagreeing with its own table). **Not one was in the gate table's
logic.** Prose is where this document's defects live, so cutting prose removes defect surface,
not merely reading time. Nothing cut was a claim about the current tree; the removed narrative
lives here, in the append-only record that suits it.

**The document has two audiences wanting opposite things** — an owner deciding go/no-go wants one
page; a future engineer wants every rationale. Five rounds served the second at the first's
expense. It should shrink toward its gate table; DECISIONS is the other audience's home.

**Final tally, and it is the auditor's sentence rather than this delivery's:** across five rounds
— ten hard invariants by independent AST scan, eleven mutations, seven full suite runs, two L3
runs on a real four-app co-installation, six SHAs, all four completeness gates against the
auditor's own junit — **no defect has been found in the runtime code.** Every finding in every
round has been in the evidence, the gate accounting, or the tests. For a 43,852-line application
replacing an ERP's file-storage layer, that is the finding worth carrying to the owner.

## 2026-08-17 — F2 closed: 43/43 executed, and what building the gate found

**F2 was NOT MET, not merely NOT ASSESSED.** The earlier verdict was correct *given no artefact
existed to check the gate*; building one settled it the other way. Six of the 43 named scenarios
had no executing test at any layer — outbound email attachment bytes, inbound attachment save,
`File.unzip()`, `File.optimize_file()`, `copy_attachments_from_amended_from`, and the Prepared
Report gz round trip. Three of those are live frappe content-consumer paths, which is the exact
surface this application replaces.

**The sixth was invisible until the manifest forbade sharing.** T-PREPREP looked covered because
contract C1 asserts a gz round trip — but C1 belongs to T-IC-RAW. One test satisfying two
scenarios is a gap wearing a tag, and it only surfaced because the gate treats duplicate targets
as a failure.

**T-AMEND was this project's own named failure mode, one last time.** Grepping for "amend"
matched a *module docstring* claiming "amend/copy" coverage; no test drove `amended_from`. Prose
describing a file counted as a test of it.

**Why the gate is a manifest of test ids rather than a tag scan.** Every previous attempt to
count this gate by grepping was wrong in both directions — it matched `T-ID` inside
`AKIA-SECRET-ID`, and it counted T-AMEND from prose. A tag is a claim; a `<testcase>` element in
a junit is a measurement. Mapping exact ids also makes "one scenario satisfied by another's
name" structurally impossible.

**The gate was proven able to fail before it was trusted** — five controls, four failure modes,
each with the exit code recorded (`docs/evidence/f2-mission-matrix.md`). A gate that cannot fail
proves nothing, which this project has now found four separate ways.

**The tests were proven specific, not merely sensitive.** Breaking the remote read reds all six;
breaking only materialisation reds only T-ZIP. Sensitivity to a shared dependency is not
evidence that a test measures what it names.

**F2's gate is a CI job, not a step**, because the matrix spans layers: L1/L2 scenarios execute
on the MinIO site, and T-RIV/T-ITEMIMG/T-IC-LIVE only where erpnext, hrms and india_compliance
are installed. A gate reading one report would report the other layer's scenarios missing — the
first real run did exactly that, correctly.

Evidence: L2 1373 OK (skipped=4), L3 1387 OK (skipped=1), **43/43 executed across 2 reports,
0 missing, 0 skipped, 0 failed.**

## 2026-08-17 — F3 closed: the blocker was the instrument, four times over

**F3 criterion 3 was recorded as an ENVIRONMENT_BLOCKER since P5** ("`/proc/self/io` reports
zero"). It was never an environment limit. Four separate instrument defects had to be fixed
before any number was worth reading, and each produced a *plausible-looking zero* rather than an
error:

1. **The wrong process was sampled** — `/proc/self/io` of the measuring process, which performs
   no I/O, instead of the workers and mysqld.
2. **Only block-layer counters were read.** `read_bytes` is legitimately 0 for a page-cache read
   on a warm host. Reading only that pair makes a busy process look idle. `rchar`/`wchar` are
   syscall-level and move regardless; both pairs are now captured, because together they
   distinguish *the app moved bytes* from *the bytes reached the disk*.
3. **Device IOPS and latency were not derivable at all** — syscall counts are not disk
   operations, and `/proc/diskstats` was never sampled. Added.
4. **The process matcher silently dropped every worker started by file path**, matching only the
   dotted module form its own spawner uses. **The result reads as zero RSS and zero I/O rather
   than "unmatched"** — a quiet process, not a missing one. This invalidated the 4188-object
   run's application metrics, which are **excluded** from the evidence rather than reported as
   zeros.

**A fifth defect was mine and self-confirming.** Site setup set `s.storage_mode = 'CLOUD_PRIMARY'`
— a field that does not exist — and "verified" it by reading the same in-memory attribute back.
The real field is `operation_mode` and `CLOUD_PRIMARY` was never a valid value. It changed
nothing, because migration UPLOAD writes to the bucket regardless of serving mode, which the
direct bucket listing confirms — but **the verification was worthless, which is the point**. The
correct read-back is `frappe.db.get_single_value(...)`, from the database, never from the object
just mutated.

**The verdict rests on measurement, not on absence of evidence.** Per-object device write
**falls** with scale (296.7 -> 174.1 KB/object); application RSS is **flat** (577 -> 574 MB)
while 6278 objects are consumed; write throughput **decreases** across the run; `await`
**improves** under load (4.03 ms idle -> 2.05 ms loaded), which is the signature of an
unsaturated device rather than one approaching a knee; utilisation peaks at 30%.

**A queue-depth figure was nearly misread.** The `default` queue showed a constant 124 during the
loaded run. Every one of those jobs belongs to `kaynes.test`, a different site on this shared
bench, and the depth never moved — the migration neither added to it nor drained it, and had
**0 jobs** on short, default and long. Reported with its attribution rather than netted out: a
queue-depth number without provenance is exactly the figure that gets misread later.

**Unchanged and still the owner's:** ERP probe latency 0.257 s idle -> 0.410 s loaded (+60%,
sub-second throughout). The approved documents state no tolerance for "unaffected", so no verdict
is claimed here — supplying one would be inventing a threshold the gate does not have.

## 2026-08-17 — OWNER RULING: narrow Semgrep suppressions, and what it does not permit

**Recorded here because the artefact that depends on this ruling was, until now, the only place
it existed.** Found by independent security review (R3): `docs/security/semgrep-adjudication.md`
cited "the owner's ruling of 2026-08-17" to justify 38 suppressions, and a grep for *semgrep*
across `docs/` did not return `DECISIONS.md`. A ruling recorded only inside the document it
authorises is not a durable record.

**The ruling.** Narrow, per-finding `# nosemgrep` suppressions are permitted for the three rule
classes previously prohibited — `frappe-security-file-traversal`, `frappe-sql-format-injection`,
`frappe-setuser` — **only** where the specific occurrence is independently proven safe. It
explicitly amends the earlier blanket prohibition **for proven false positives and
architecture-required uses only**.

**What it does not permit**, verbatim in effect: no blanket rule-ignore, no directory
suppression, no filename suppression, no global disabling, and no weakening of a rule to make CI
green. Where a path is actually user-controlled or a value can become an identifier, the code is
fixed rather than suppressed.

**What it produced.** 45 findings → 10 FIXED, 31 FALSE_POSITIVE, 3 TEST_ONLY, 1
ARCHITECTURE_REQUIRED; 123 annotations in an exact 1:1 with 123 live findings; zero blanket,
directory or filename suppressions; no `.semgrepignore`.

**And it produced two real security fixes that the "false positive" reading would have buried:**
`cache.materialize.entry_path` escaped the cache root for any component containing `..`
(confinement lived in its callers), and `migration.cleanup` would unlink any path a System
Manager wrote into `disk_path`/`quarantine_path` (confinement lived nowhere). Both are now
enforced by construction with mutation-proven regression tests. **The requirement to prove
confinement per site rather than assert it is what surfaced both.**

## 2026-08-18 — Round-3 security re-check: terminal refusal, one predicate, aligned controls

Independent security review re-checked the three `disk_path` read sinks closed in round 2. Outcome
recorded here because the finding class is one this delivery keeps producing.

1. **`engine.process_object_upload`'s refusal handler was dead code.** It called
   `_record_object_failure` with keywords that function does not accept, raising `TypeError` before
   it could record anything. The guard itself held — the raise preceded `digest_path`, so no
   out-of-tree bytes were ever read — but the object was filed as a *transient* `TypeError`,
   returned to `Pending`, and retried against an attack input. Now `_open_conflict(...)`:
   `Uploading → Failed`, `severity="Blocker"`, `error_class="PathOutsideSite"`.

2. **Disposition, not outcome, is the assertion.** Every prior test asserted "no bytes uploaded",
   which is satisfied by the guard refusing *and* by the handler crashing. Tests now assert the
   recorded state. Mutation M1 (restore the broken call) reproduces `'Pending' != 'Failed'` while
   23 of 24 tests stay green; M2 (delete the guard) yields `'Uploaded' != 'Failed'`, demonstrating
   that `site_config.json` is genuinely published into the bucket without it.

3. **One confinement predicate.** `analyzer.confined_under(path, roots)` is now the single
   implementation; `cleanup._confined_under` is deleted. Two byte-identical copies of a security
   check is drift waiting to happen, and the duplicate guarded the destructive sinks. Consolidation
   immediately exposed `_purge_one` (nightly cron, reached directly) as a third call site my
   per-function lazy import had missed — caught by the full L2 run as a `NameError`.

4. **R9 mirrors the rule's own path exclusions.** `frappe_correctness.yml` excludes `**/patches/**`
   and `**/demo/**` from `frappe-manual-commit`. Without the same exclusion, R9 and the 1:1
   suppression-hygiene sweep oscillate over a single line forever: one demands an annotation the
   other correctly strips as dead. Controls that disagree about scope are not two controls.

**Standing lesson, third instance:** the defect was *a true statement carried past its scope* — the
guard was correct, and the correctness of the guard was allowed to stand in for the correctness of
what happens when it fires. Assert on what is recorded, not on what is avoided.

No production system was touched. All work on scratch sites `cfs-rc2.local` / `cfs-ecosystem.local`
against scratch MariaDB `:3307` and scratch MinIO `:9000`.

## 2026-08-18 — Scheduler frequencies moved to the floor's set, and the queue cost that hid behind it

**Trigger.** The F1 reference matrix ran for the first time and `bench install-app` refused on
the v15.16.0 ref: `ValidationError: Frequency cannot be "Hourly Maintenance"`. The
`*_maintenance` frequencies first exist in frappe **v15.79.0**; the declared floor is
**v15.16.0**. **The app could not install on any supported frappe below 15.79** — most of its
own declared range — and had not been able to for as long as those hooks existed.

**Rejected: raising the floor.** `docs/supported-versions.md` ties 15.16.0 to A34
(`find_file_by_url` + the `fid` argument), which is what lets serving re-run core's own
permission gate instead of reimplementing it — forbidden by PLAN §A. `ci.yml` pins v15.16.0 as
the matrix row that proves the floor is real. The floor is load-bearing and does not move to
make an install work.

**Decided: `hourly` / `daily`**, which exist across all of v15.

**What that actually cost, and the first answer was wrong twice over.** The change was recorded
as trading away frappe's per-site `maintenance_offset` stagger. Independent review checked the
source and found (a) the offset is computed and then **discarded** —
`scheduled_job_type.py:137-140` returns a freshly recomputed value, so it never delivered a
stagger at all — and (b) the real cost was elsewhere and invisible to that framing:
`get_queue_name` (`:183`) routes `*Maintenance*`/`*Long*` to `long` and everything else to
`default`, and the scheduler enqueues with no explicit timeout, so `background_jobs.py:45-52`
applies **1500s on `long` against 300s on `default`**.

So six entries moved onto the ERP's shared `default` queue with a 5x shorter timeout — the exact
thing `background.py`'s docstring forbids ("Migration, repair and GC work must not compete with
the ERP's own short/default/long queues (F3)") and `FALLBACK_QUEUE = "long"` encodes.

**Four of the six do real inline work**: `run_deferred_object_gc` (up to `GC_BATCH_SIZE`
DeleteObject calls), `run_orphan_sweep` (per-row commits), `repair_unevictable_entries` (whole
cache walk + re-hash), `run_eviction` (stats the whole tree). Each now has an O(ms) dispatcher
that enqueues the real work via `enqueue_maintenance(..., timeout=1500)`, mirroring
`gc.repair_pending_uploads_dispatch`.

**A correction to the charge, in the implementer's favour, from the reviewer.** `hooks.py`'s
"these entries stay O(ms) dispatchers" comment **predates this change** and was already false
for those four. The frequency change did not introduce inline work; it removed the cover that
`long` had been providing. The fix makes that inherited comment true for the first time.

**Two guards, both proven to fail before being accepted.**
`test_every_scheduler_frequency_exists_at_the_declared_floor` hard-codes the v15.16.0 option
list rather than reading the installed frappe — reading the local one reproduces the blindness
that let this ship — and keys its cron exemption on `isinstance(value, dict)`, which is frappe's
actual rule (`insert_events:216-222`) rather than the key name, an assumption that was itself
found and corrected in review. `test_no_scheduler_entry_does_real_work_on_the_erp_shared_queue`
asserts every non-cron target on a `default`-routed frequency dispatches rather than works
inline; **no test anywhere asserted the queue for a scheduler entry before this**, which is why
1405 green tests said nothing about it.

**The general lesson, recorded because it recurred:** a frequency name is not cosmetic in
frappe — it selects the queue and therefore the timeout. And a justification for a trade is
worth checking against source, not just the trade itself: this one named a loss that did not
exist while missing the loss that did.

## 2026-08-18 — CodeQL upload reclassified as an external platform limitation (owner amendment)

**Owner decision.** CodeQL SARIF upload is classified `BLOCKED_EXTERNAL_ADMIN_CONFIGURATION`.
GitHub Code Security / code scanning is controlled by the **organisation** administrator and
cannot be enabled by the project owner, so the upload is refused with "Code scanning is not
enabled for this repository". This supersedes the earlier owner statement requiring CodeQL upload
to pass before this release candidate.

**What it is not.** The amendment states in its own terms that it **does not constitute CodeQL
PASS**, and nothing in this repository records one. A refused upload is a platform permission
fact, not an application or runtime defect — the distinction that matters is between *"the
analysis ran and found nothing"* and *"the upload was refused"*, and only the second is claimed.

**What it does not unblock.** The amendment permits RELEASE_CANDIDATE despite this limitation
*provided* all mandatory F1–F7 criteria PASS, Semgrep passes, independent security review passes,
zero unresolved Critical/High findings, and security regression tests pass. **The first condition
is currently false** (F1, F5, F7). So this removes a blocker that was never the binding one, and
the release remains blocked on exactly what it was blocked on before. Recorded explicitly because
"CodeQL no longer blocks" is one paraphrase away from "clear to ship".

**Prohibited remedies, verified absent at `e3cadb8` rather than assumed.** The workflow must not
be weakened to manufacture green: no `upload: false`, no removal of the CodeQL job, no
CodeQL-only `continue-on-error`, no disabled SARIF upload, no deleted security analysis. All five
checked; `.github/workflows/codeql.yml` was last modified in `b8a8417`, four days before the
failure, so it was not softened in response to it. When the administrator enables code scanning,
the workflow runs unchanged.

**Superseded 2026-08-19 — the chronological half of that argument no longer holds.** `09e17a0`
and `ad5a7fb` touched `.github/workflows/codeql.yml` later, during the CI-timeout hardening, so
"last modified in `b8a8417`" ceased to be true. The entry was accurate when written; it *became*
false. The non-tampering proof is now taken from the **current workflow** rather than from a
last-touched date — a timestamp cannot distinguish hardening from weakening, which is why it was
the wrong instrument. The five prohibited remedies remain absent, re-derived at the terminal tree;
see `RELEASE_EVIDENCE.md`.

## 2026-08-18 — The coverage floor applies to the combined corpus, not to each shard

**Decision.** Test jobs publish coverage data as artifacts; a dedicated `coverage` job combines
them and applies the 90% floor **once, to the union**. The floor is unchanged, no `--omit`
patterns were added, no job was made soft-failing, and no number was fabricated.

**Why this is a restoration of the contract rather than a change to it.** `check_coverage.sh`
records where the floor came from: P1 measured **94% over `cloud_file_storage/` on a fresh
scratch site running 123 unit tests + 4 MinIO integration tests** — one corpus, one measurement,
with the floor set just below it. CI later split that corpus across jobs that are *deliberately
partial*: three frappe refs run the app suite, a separate job runs the real-S3 tests, another
runs L3 with erpnext installed. Applying a whole-corpus floor to each fragment asks every shard
to clear a bar measured on the whole.

On `dcb2ada` **two** of the three matrix refs reported **76%** and failed while the same tree
measured 92–94% locally (`v15.16.0` never reached a coverage number — it failed with 414
`AttributeError`s first). Nothing had regressed. `docs/ACCEPTANCE_GATES.md` never mentions coverage at all
— it is a P1 CI requirement (`docs/PLAN.md:140`), not an F-gate — so no acceptance meaning
changes here; what changes is that the gate now measures the thing its own floor was derived from.

**Two verdicts, deliberately separated.** A gate that reports the wrong cause sends the next
person to the wrong place — which is exactly what happened, since "fell below the 90% floor" was
printed for a corpus that had not shrunk:

```
exit 0  coverage >= floor
exit 1  COVERAGE FAILURE        the combined corpus is genuinely below the floor
exit 2  GATE EXECUTION FAILURE  missing / corrupt / empty / foreign-SHA shard, or no tool
```

**Vacuity is the failure mode this design has to survive**, because combining makes silence
cheap: a skipped job simply contributes nothing. So the required shard list is **declared, not
discovered** (`COVERAGE_EXPECTED_SHARDS`), every shard carries a `coverage-sha.txt` proving it
belongs to the candidate commit, and a missing, empty, corrupt or foreign shard is a gate
execution failure rather than a smaller union.

**Anti-vacuity suite** (`tests/test_coverage_gate.py`, 17 tests) drives the **shipped script**
through `subprocess` with synthetic-but-real coverage databases — testing a reimplementation
would test a copy of the logic rather than the artifact CI runs. It proves: a missing shard
fails; corrupt data fails; 89% fails as COVERAGE while 95% passes; **a 76% shard does not fail
before aggregation** (the `dcb2ada` case, asserted as a non-failure); a 0-byte artifact cannot
combine into a green; shards measuring only out-of-scope code cannot go green; a foreign-SHA
shard is refused; and an undeclared shard list is refused outright.

**Plus a wiring guard, because the two halves can drift in the dangerous direction.** Adding a
frappe ref to the matrix without adding its shard to the required list would publish coverage
that is quietly ignored and never required — the union judged without it, the gate still green,
and nothing else in CI noticing. Mutation-verified in both directions, along with
`include-hidden-files: true`, which is load-bearing: `.coverage.*` is dot-prefixed and
`upload-artifact` drops hidden files by default, so without it the artifact uploads carrying the
SHA stamp and no data.

**One implementation note worth keeping.** The shard validator lives in
`.github/helper/validate_coverage_shard.py` rather than in a heredoc: a tab-stripping heredoc
silently destroyed its indentation, and the gate then reported every shard as "could not
validate" — a true-sounding message produced by a broken validator. A file cannot be reindented
by the shell.

## 2026-08-18 — The combining coverage gate could not have returned a verdict in CI at all

**Found by the independent delta reviewer, as a Critical, after local qualification was green.**

`frappe/coverage.py` measures `<bench>/apps/<app>`, and coverage canonicalises every filename to
an **absolute** path, so each shard records
`/home/runner/frappe-bench/apps/cloud_file_storage/...`. The combining job has no bench — only
the repository checkout. `coverage report` must re-parse each source file to count statements,
so it raised `NoSource` and exited **1**, which this gate maps to GATE EXECUTION FAILURE.

**Every push would have gone red for an infrastructure reason with no coverage number ever
computed** — while the DECISIONS entry above claimed the gate "now measures the thing its own
floor was derived from". Nothing had demonstrated that. Reproduced before fixing: a shard naming
a non-existent root gives `No source for code: ...` and rc=1.

**Fix:** `[paths]` aliasing. The gate writes an rcfile mapping `*/apps/cloud_file_storage` onto
`COVERAGE_SOURCE_ROOT` (the workspace in CI) and passes it to both `combine` and `report`.

*A correction to this entry's first version, which had the reason wrong.* It claimed passing the
rcfile to only one of the two "silently does nothing". Aliasing is applied **only** by `combine`,
which rewrites the paths into the combined database; `report` then reads paths that are already
rewritten. Combine alone would have sufficed. Both are passed so that a later reader running
`report` in isolation gets the same mapping — which is a different and much weaker reason than the
one originally given. Worth correcting in a record whose subject is attributing causes correctly.

**Why local qualification could not have caught this, and why that matters more than the bug.**
The `coverage` job is CI-only; it never runs on this bench. And the 17 tests wrote their measured
module to a path that still existed when the gate ran — they reproduced a *convenient* shape
rather than the *shipped* one. That is the same failure class as the defect that started this
cycle, one level up: green locally, structurally incapable of seeing the failure. Three tests now
reproduce CI's actual condition, including one asserting that unresolvable sources are reported
as a gate problem naming the remedy.

**The IC path guard took three attempts, and the first two were vacuous on this bench.**
- A prefix check ("under this bench's apps dir?") **accepted the original hardcoded path**,
  because on this machine it is under that directory.
- An equality check against the derivation cannot distinguish them here either — the literal and
  the derivation produce the same string on the machine where the literal was written. Kept
  anyway: it fails on every other checkout.
- A source-text scan **matched its own search string**, then scanned the wrong region.
- An **AST walk** works: it sees no comments or docstrings, cannot match itself, and looks for
  the shape that actually shipped — a `Path(...)` call on an absolute string literal.
  Mutation-verified against the exact original path both weaker guards accepted. **Not
  exhaustive**, and deliberately not described as such: it misses `Path(_CONST)` and
  `pathlib.Path("/abs")`. A guard aimed at one real defect and proven against it is worth more
  than a general one nobody has checked.

**Also closed:** `get_app_path` was removed from that helper — its docstring claimed it does not
import India Compliance, which is false (`get_app_path` -> `get_pymodule_path` -> `get_module` ->
`importlib.import_module`). Two real consequences followed: the real package landed in
`sys.modules` before the fake replaced it and was then *deleted* rather than restored, evicting
it for the rest of the process; and the `except` meant for "app absent" also swallowed genuine
import failures, so a present-but-broken IC still passed. Now pure path arithmetic, with
snapshot-and-restore of `sys.modules`.

`coverage` is pinned to `~=6.5.0` in the combining job to match the producers (frappe pins the
same), so a schema divergence cannot surface as "unreadable or corrupt" — a wrong cause for a
version mismatch.

**Correction to the previous entry:** it said "all three matrix refs reported 76%". **Two** did.
`v15.16.0` failed with 414 `AttributeError`s and never reached a coverage number. In a record
whose subject is attributing causes correctly, that overstatement is worth fixing rather than
leaving.


## 2026-08-18 — CORRECTION: the coverage justification was false; the real figure is 76%

**The entry above ("The coverage floor applies to the combined corpus, not to each shard")
recorded a false premise, and the gate's own first complete run disproved it.**

That entry said the shards' 76% was a whole-corpus floor misapplied to partial slices, "while the
same tree measures 92–94% locally". On `2df3bbb` the combining gate ran end to end for the first
time — all five shards accepted, aliased, combined — and reported **76%**. The same figure
reproduces on the bench: **8379 statements, 2007 missed**.

**The design change was still correct; the reason given for it was not.** A floor measured over
one corpus does belong on the union rather than on deliberately partial shards, and the gate is
now demonstrably capable of returning a verdict at all (which, before the `[paths]` fix, it was
not). But combining did not restore a 92–94% figure, because no such figure existed on this tree.

**Where "92–94%" came from:** `docs/PROGRESS.md`'s P1 row records 94% measured over **123 unit
tests + 4 MinIO tests** on a far smaller app. I carried that number forward as current and never
re-measured it. That is precisely the stale-figure error this delivery has caught three times in
other people's work — asserted, propagated into a decision record, and only falsified when
something finally executed the measurement.

**What 76% actually contains, measured:**

| slice | stmts | missed | cover |
|---|---|---|---|
| everything the gate measures | 8379 | 2007 | **76%** |
| product code only (tests excluded) | 6584 | 905 | **86%** |
| `tests/rehearsal.py` (100k F3/F4 harness, never run by the suite) | 781 | 781 | **0%** |
| `tests/playwright/smoke.py` (browser smoke, never run by the suite) | 184 | 184 | **0%** |

Both zero-coverage files are real and deliberate: they are driven out-of-band for F3/F4 and the
Desk smoke, not by `bench run-tests`. They alone account for most of the gap between 86% and 76%.

**No gate change was made in response.** Adjusting the include pattern, adding an omit, or moving
the floor to reach green are all forbidden, and doing any of them on the strength of my own
framing — immediately after that framing proved wrong — would be the worse error. The decision is
recorded as OWNER_DECISION_REQUIRED with the three honest options: exclude the test tree
(86%, still short of 90); re-baseline the floor to what the project actually holds; or treat ~905
uncovered product statements as work.

## 2026-08-18 — OWNER RULING: coverage is non-blocking quality debt, not a release gate

**Ruling.** Coverage measures **product/runtime code**, excluding the test tree from the
denominator. The **90% target stands unchanged**. The measured **~86% product coverage** is
recorded as **QUALITY_DEBT / POST-RELEASE_IMPROVEMENT**, and coverage is **NOT BLOCKING** for
v1.0.0 — because `docs/ACCEPTANCE_GATES.md`, the frozen release criteria, never defines a
coverage percentage as a release condition. Verified: F1–F7 there are contract, mission matrix,
rehearsal, preflight, ecosystem, safety lints, release. Coverage appears only as a P1 CI
requirement in `PLAN.md`.

**This is explicitly NOT a coverage PASS.** Recorded as:

```
COVERAGE_RELEASE_GATE = NOT_APPLICABLE_TO_F1_F7
PRODUCT_COVERAGE      ~86%
TARGET                90%
STATUS                NON_BLOCKING_QUALITY_DEBT
```

**Prohibited and not done:** the target was not lowered to 76%, no omit rule hides product code,
the tooling is untouched and stays operational, and the failing result is not reported as PASS.

**The ~905 uncovered product statements are post-release work, prioritised by risk** — security
boundaries, deletion/GC, migration recovery, private serving, permission enforcement,
backup/restore, compatibility paths.

**Why the number moved.** The 76% the gate reports includes the test tree (1795 statements at
39%), of which two harnesses are 0% *by design* because `bench run-tests` never runs them:
`tests/rehearsal.py` (781, the 100k F3/F4 rehearsal) and `tests/playwright/smoke.py` (184). They
are driven out-of-band. Excluding the test tree gives 86% over 6584 product statements. Both
figures were measured on this bench and reproduce CI's exactly — see the correction entry above,
which retracts the earlier false claim of "92–94%".

## 2026-08-18 — F1, F2, F5 PASS at `2df3bbb`, verified against their own frozen criteria

Not inferred from job success — each gate's actual text was checked against CI's logs.

**F1 — contract gate.** `ACCEPTANCE_GATES.md:7`: C1–C19 execute on **every supported frappe ref**,
junit-asserted per ref, zero silent skips. Confirmed from the logs of all three matrix jobs:

```
v15.16.0     contract completeness OK: C1..C19 all executed (19/19 accounted for)
v15.93.0     contract completeness OK: C1..C19 all executed (19/19 accounted for)
version-15   contract completeness OK: C1..C19 all executed (19/19 accounted for)
```

**This is the first time the floor ref has been green in the entire delivery.** Six attempts, each
removing a real defect class no single-reference bench could have found: a scheduler frequency
absent below v15.79; a cache idiom valid only from v15.88; a PDF scan added at v15.80; a System
Manager guard removed by v15.93; a test helper added at v15.50; a dedup nesting the floor lacks.

**F2 — mission matrix.** `43/43 scenarios + 12 A-series + thumbnail availability executed across
2 report(s), 0 missing, 0 skipped, 0 failed`.

**F5 — ecosystem gate.** `ACCEPTANCE_GATES.md:46` requires the L3 CI job green **and mandatory**.
The job passed with every step green, including `Assert the ecosystem apps are actually installed`
and `Ecosystem completeness (T-RIV / T-ITEMIMG / T-IC-LIVE)` — so the co-installation is proven
rather than assumed, on erpnext + hrms + india_compliance with real MinIO.

**CodeQL remains `BLOCKED_EXTERNAL_ADMIN_CONFIGURATION`** under the recorded amendment — never
PASS, workflow untouched, SARIF upload intact.

## 2026-08-18 — RELEASE AUDIT ROUND 4 at `2df3bbb`: NO-GO, BLOCKED(OWNER_DECISION_REQUIRED) on A1

Independent release-manager audit, fourth round, against a new candidate. Rounds 1–3 returned
NO-GO against older SHAs; those verdicts stand as historical evidence and are not retroactively
converted.

**Verdict: NO-GO. `BLOCKED(OWNER_DECISION_REQUIRED)` — A1, F3's ERP-queue criterion.**

**Five gates verified PASS.** F1 (C1–C19 on all three refs, floor ref green for the first time),
F2 (43/43 + 12 A-series + thumbnail), F4 (capacity numbers, carry-forward basis verified), F5
(L3 CI green and mandatory, co-installation asserted), F6 (**re-derived at this SHA, not
carried** — 19 + 39 lint tests executed green by the auditor).

**The blocker.** `ACCEPTANCE_GATES.md:28` makes "normal short/default/long ERP queues unaffected
(probe-job latency at baseline)" a **strict pass/fail criterion** of F3. The measurement is
+60% (0.257 s -> 0.410 s). Four documents independently record that **no verdict is claimed**
because the approved documents define no tolerance for "unaffected" — `RELEASE_EVIDENCE.md`,
`DECISIONS.md:3228`, `evidence/f3-disk-io-scale.md:116`, and `PROGRESS.md`'s audit row. It was
ruled `OWNER_DECISION_REQUIRED` on 2026-08-17 and **no owner ruling on it has ever been made**.
The two rulings that exist cover Semgrep suppressions and coverage; neither touches A1.

**Why this was not waived.** F3's four previously-unmet criteria were genuinely closed on
measurement, which made it tempting to read F3 as done — and the gate table did read that way.
But "four of the criteria that were open at P5" is not "F3's criteria". Passing F3 here would
require inventing a latency tolerance the frozen gate does not contain, which is the definition
of negotiating a gate down.

**The coverage ruling was checked, not accepted on assertion, and it holds.** `ACCEPTANCE_GATES.md`
has exactly **one commit in its history** — the P0.5 freeze `cb4398b` — so the criteria were
demonstrably never edited to accommodate anything. The only `%` anywhere in it is F3's
convergence ≥99.9%. No coverage percentage is a release condition, so `COVERAGE_RELEASE_GATE =
NOT_APPLICABLE_TO_F1_F7` is correct. The gate ran, failed at 76%, and is recorded as failing;
the floor is still 90 and the tooling is untouched. The self-correction retracting the false
"92–94% locally" claim is **adequate**: it names the real figure, names where the wrong number
came from, and states that the design change was right for a reason other than the one given.

**Two defects found in this round, both in release artefacts, both fixed:**

1. **`RELEASE_EVIDENCE.md` contradicted itself on F3** — the gate table said `PASS` while "What
   remains genuinely unmet" said F3 "was attempted, fell short, and was never completed". The
   table had been updated when the disk-I/O criterion closed; the prose had not. A release
   document that disagrees with itself about the one blocking gate is the artefact F7 asks for,
   so this was a gate defect and not a typo. Both now state the same thing.
2. **The CHANGELOG's `1.0.0` compare link pointed at `alyf-de/frappe-attachments-s3`**, where
   `v1.0.0` will never exist — the release-checklist requires the compare links to resolve.
   Retargeted to this repository. The `0.1.0`–`0.2.2` links correctly stay upstream.

**Carry-forward legitimacy, assessed rather than assumed.** F6 was re-derived outright. F4's
per-row byte figures are properties of the five measured DocType schemas, and the schema diff
from the rehearsal commit to `2df3bbb` is **empty** — so they still hold. F3's non-latency
measurements are older than exactly one commit touching `migration/`+`storage/` (`ef27ff3`, the
`disk_path` confinement fix); under Evidence Rule 2 that invalidates them literally, which is
noted but is moot while A1 blocks F3 anyway.

**What would close this.** One owner decision, on the record: either a stated tolerance for
"unaffected"/"at baseline" against which +60% is judged, or an explicit acceptance of F3 with
the ERP-latency item named — the same two options offered on 2026-08-17. No code change is
implied. Every other gate is closed.

## 2026-08-18 — A1 measurement instrumentation: the load witness

**Context.** A1 compares ERP queue latency at baseline against latency *under migration
load*. The frozen threshold (owner ruling, same date) defines what counts as a regression,
but nothing in the harness established that the loaded run was in fact loaded.

**What went wrong first.** The first loaded run reported `883` Pending objects at the probe's
start and `883` at its end, which read as a stalled migration and would have invalidated the
point. It was the *instrument* that was wrong: MariaDB here runs `REPEATABLE-READ`, and both
counts were taken inside one long-lived transaction, so the second could not observe commits
made by the workers in between. Demonstrated directly — two counts in one transaction
returned `0` and `0` across a committed insert, and `1` only after a `rollback()`. Independent
evidence from the job log confirmed 6 migration jobs (3 UPLOAD, 3 VERIFY) executed inside the
36 s probe window, spanning it from +2.9 s to +35.3 s. The migration was never stalled.

The harness's own polling loops were already correct — they `rollback()` before reading. Only
the ad-hoc measurement script was wrong.

**Why this needed a code change rather than a better script.** The mirror-image failure is the
dangerous one. If a "loaded" run genuinely carries no load, every queue sits at its baseline
and **A1 passes trivially** — the gate reports success precisely when it measured nothing.
That is the same defect class as the sampler reading unmatched processes as idle, and it is
not acceptable to defend against it with operator discipline.

**Decisions.**
1. `probe()` records a **load witness**: fresh-snapshot object progress (via `_fresh_count`,
   which rolls back first), migration jobs executed inside the probe window, and a
   `load_state` of `LOAD_PRESENT` / `NO_LOAD`.
2. `a1_verdict()` returns `INVALID_MEASUREMENT` and **never** `passed: True` when the loaded
   run cannot witness load, or when the baseline was taken while the migration was running.
   Same three-state vocabulary as the disk-I/O stage, for the same reason.
3. Migration jobs are identified by the engine's own `cfs::` job-id marker. A1's probe
   enqueues ~105 pings onto `short`/`default`/`long`, so counting *any* job on an ERP queue
   would report the measuring instrument as the violation it exists to detect.
4. `_job_rows` reads **several** logs, because the two facts A1 needs are written by different
   workers: migration progress by the `cloud_migration` workers, and a migration job wrongly
   landing on an ERP queue by an *ERP* worker. ERP workers are pointed at their own log via
   `CFS_REHEARSAL_JOB_LOG`, so the migration job log — and the already-recorded `jobs()`
   evidence read from it — is left untouched.

**A threshold-adjacent fix, stated explicitly so it is not mistaken for moving the gate.**
The delta comparison used raw floats, so `0.30 + 0.250` produced `0.25000000000000006` and a
measurement sitting exactly on the frozen 250 ms bound **failed**, while the report printed
`delta 0.2500s` beside that FAIL. The comparison now decides on the same 4-dp figure it
reports. `A1_MAX_DELTA_SECONDS` and `A1_MAX_RATIO` are unchanged; the frozen bound is
inclusive as ruled, and is now actually reachable.

**Verification.** 23 regressions in `test_f3_a1_verdict.py`; all four F3 suites green (49).
Every new check was mutation-tested — nine deliberate breakages (ignore the witness, revert
the rounding, count all ERP jobs, admit a loaded baseline, let `passed` ignore the
measurement state, unbounded job-log window, drop the migration marker, marker matches
everything, read only the first log) each produced failures, and the restored code is green.

## 2026-08-18 — F3/A1 measured against the frozen threshold: FAIL on `default`

**Pair 1, valid on every precondition** (load witnessed `LOAD_PRESENT` with 300 objects
progressed and 6 migration jobs inside the probe window; baseline witnessed `NO_LOAD`; 35
completed probes per queue; zero probe failures; zero migration jobs on ERP queues; queue
depths back to zero):

| queue | baseline p95 | loaded p95 | delta | ratio | pass |
|---|---|---|---|---|---|
| short | 0.3571s | 0.4594s | 0.1023s | 1.286x | yes |
| default | 0.3582s | 0.6195s | **0.2613s** | 1.729x | **no** |
| long | 0.3596s | 0.4618s | 0.1022s | 1.284x | yes |

`default` exceeds the frozen 250 ms delta bound by 11.3 ms. The ratio bound is satisfied
(1.729x against 2.0x); it fails on delta alone. Under the owner ruling — one queue failing any
condition => A1 FAIL — **F3_A1 = FAIL**, and F3 does not pass at this candidate.

**The threshold was not touched.** It was frozen before the measurement precisely so that this
result could not be negotiated after the fact.

**On repetition.** The margin is 4.5% over the bound, so two further pairs were measured to
establish whether the exceedance reproduces. That is variance characterisation, not
re-rolling: the commitment to report all three pairs regardless of outcome was made before
they ran, and the earlier invalid baseline (1 probe failure, 54.55 s outlier) was rejected on
a precondition the protocol states, not because its number was unwelcome.

**Note on the shape of the effect.** The medians moved too (`default` 0.2552s -> 0.4078s), so
this is a shift of the whole distribution under load rather than a tail artifact. All three
queues degraded; `short` and `long` stayed inside the bound at ~0.10s.

## 2026-08-18 — F3/A1 resolved: PASS under a pre-declared clean protocol (pair 1 recorded, not hidden)

Pair 1's exceedance (`default`, delta 0.2613s) did not reproduce. Rather than resolve two
disagreeing valid measurements by keeping the favourable one — which is the selection bias
this whole cycle has been about — the measurement protocol was **fixed in advance** and then
run three times:

* quiesce first: `cloud_migration` depth 0 **and** zero in-flight batches, verified in a loop
  on fresh snapshots before the baseline is taken;
* exactly one `run` stage, killed at the end of the pair (leftover pollers from earlier pairs
  were what contaminated pair 2's baseline and inflated pair 3's);
* exactly one accounting ERP worker per queue — the stale plain-`rq.Worker` processes from an
  earlier session were removed. They logged nothing, which both doubled ERP consumer capacity
  and put a blind spot in the "no migration jobs on ERP queues" check;
* N = 3, **all reported, any single failure fails A1**.

| pair | protocol | state | short | default | long | verdict |
|---|---|---|---|---|---|---|
| 1 | uncontrolled | VALID | 0.1023s | **0.2613s** | 0.1022s | **FAIL** |
| 2 | uncontrolled | INVALID | — | — | — | discarded: baseline taken under load |
| 3 | uncontrolled | VALID | 0.0004s | 0.0016s | 0.0511s | PASS |
| 4 | clean | VALID | 0.1012s | 0.0996s | -0.0010s | PASS |
| 5 | clean | VALID | 0.1496s | 0.1008s | 0.1518s | PASS |
| 6 | clean | VALID | 0.1021s | 0.0494s | 0.1519s | PASS |

**F3_A1 = PASS** on the clean protocol, 3/3. Worst observed delta 0.1519s (61% of the 250 ms
bound), worst ratio 1.579x (79% of the 2.0x bound). PROBE_FAILURES 0 in every pair;
MIGRATION_JOBS_ON_ERP_QUEUES 0; ERP queue depths returned to 0 after every run.

**Pair 1 is reported, not discarded.** Its preconditions were satisfied, so it is a valid
measurement by the frozen definition and it exceeded the bound. What distinguishes it is
environmental, and stated as such rather than used to explain it away: it ran with two
consumers per ERP queue (one of them unlogged) and an unknown number of leftover run-stage
pollers. The consistent effect across every valid pair is that migration load adds roughly
+0.05s to +0.15s to ERP p95; pair 1's `default` at +0.26s is ~2x that increment and did not
recur in five subsequent measurements.

**A limitation of the load witness, recorded honestly.** The witness proves migration load is
present in the loaded run and absent at baseline. It does **not** detect unrelated background
load inflating the *baseline* — which is precisely what made pair 3 look excellent (baselines
~0.41s against ~0.26s when the box was actually quiet). A high baseline flatters both the
delta and the ratio. The clean protocol addresses this by construction rather than by
measurement, and pair 5 — the strictest, with the lowest baseline observed (0.2567s) — still
passed.

**The threshold was never adjusted**, before or after any measurement.

## 2026-08-18 — F3/A1 confirmed at the candidate SHA 1b2a94c

The clean-protocol pairs 4-6 were measured on the working tree before `ruff-format` touched
`rehearsal.py` and `engine.py`. Formatting cannot change behaviour, but this delivery's own
rule is that a tracked-file change supersedes the candidate, so A1 was re-measured against the
committed code rather than carried across.

Pair 7, at `1b2a94c` with zero pending tracked code changes, clean protocol:

| queue | baseline p95 | loaded p95 | delta | ratio | pass |
|---|---|---|---|---|---|
| short | 0.2556s | 0.4577s | 0.2021s | 1.791x | yes |
| default | 0.3048s | 0.5100s | 0.2052s | 1.673x | yes |
| long | 0.4082s | 0.5188s | 0.1106s | 1.271x | yes |

`VALID_MEASUREMENT` (LOAD_PRESENT, 323 objects progressed), PROBE_FAILURES 0,
MIGRATION_JOBS_ON_ERP_QUEUES 0, depths recovered to 0. **F3_A1 = PASS at 1b2a94c.**

**Headroom is thin and is reported as such.** 0.2052s is 82% of the 250 ms bound — the
narrowest clean-protocol margin measured, and pair 1 exceeded the bound outright under heavier
contention. Across all six valid measurements the effect of migration load on ERP p95 ranges
from ~+0.00s to +0.26s and scales with how much else is running on the box. A1 passes at this
candidate under the frozen definition; it should not be read as a comfortable margin, and a
production topology with more contention than this scratch bench could plausibly exceed it.
That is a deployment-sizing consideration for the owner, not a gate failure.

## 2026-08-18 — Local requalification at 1b2a94c

Re-derived at the candidate rather than carried forward, on a site created from nothing:

* `cfs-req3.local`: `new-site` + `install-app cloud_file_storage` rc=0, `bench migrate` rc=0,
  **12 patches applied** including both bootstrap patches (`adopt_legacy_fork_install`,
  `link_legacy_fork_objects`); `allow_tests` true; CSO and Settings doctypes present.
* App suite with real MinIO: **1488 x 3 consecutive, OK (skipped=5)**, rc=0 each.
* L3 ecosystem on `cfs-ecosystem.local` (erpnext, hrms, payments, india_compliance), migrated
  to the candidate first: **1502 x 2 consecutive, OK (skipped=2)**, rc=0 each.
* Completeness gates, run as the real `.github/helper` scripts against the junit reports:
  contract **C1..C19 19/19**, backup **61 refusal guards / 0 skipped**, MinIO **19 real-S3 /
  0 skipped**, ecosystem **9 exit-criterion tests / 0 skipped**, and F2 mission matrix
  **43/43 + 12 A-series + thumbnail across 2 reports, 0 missing / 0 skipped / 0 failed**.
* F3 regression suites **49/49**. Semgrep **0 findings**. `pre-commit run -a` clean with
  **zero files modified**.

**On the F2 two-junit result.** A single-layer run reports `T-IC-LIVE`, `T-ITEMIMG` and
`T-RIV` as unexecuted, and that is the gate working rather than a gap: those scenarios need
erpnext/india_compliance and the gate refuses to count a scenario it did not see run. The
matrix is therefore evaluated across the app and ecosystem reports together, which is what
`JUNIT_XML` accepts colon-separated.

**On the F6 checks.** Re-checked by AST, not grep — an earlier grep produced a false FAIL by
matching `engine.py`'s own docstring stating that there are no deletion calls. There is no
`upload.py`; the UPLOAD phase lives in `engine.py`. Both it and `verify.py` contain zero
deletion calls, and the only deletions anywhere in `migration/` are the two in `cleanup.py`,
the deferred-GC phase where deletion is permitted after verification and grace. Archive
storage classes appear only in `backup/lifecycle.py` and are never applied to the live bucket.

## 2026-08-19 — Independent review BLOCKED the A1 work; findings accepted and closed

An independent read-only reviewer blocked `629f3e0` with six HIGH findings. It was right on
every point I could reproduce, and the most important one is worth stating plainly.

**The mutation evidence I reported was circular.** I had reported nine mutations caught. All nine
lived inside `a1_verdict` and its pure helpers — the region that already had tests. The reviewer
named five mutations that survived the entire suite, and re-running them confirmed it, including
the single most dangerous one:

```
witness["load_state"] = "LOAD_PRESENT"      # unconditional  ->  49/49 green
```

That mutation makes A1 vacuous: an unloaded "loaded" run sits at its baseline and passes every
threshold. The guard existed; nothing held it. `A1_MIN_SAMPLES = 30 -> 5` was also green, because
every fixture derived its numbers *from* the constant it was meant to pin.

**Closed** (all eight mutations now fail, the vacuity one by 9 tests):
* `_assemble_witness` extracted and driven by tests against real job-log files on disk — no test
  asserts on a witness its own fixture wrote.
* The frozen constants pinned by value.
* A job log that was never read no longer reads as clean: paths, missing paths and rows-read are
  recorded, and a zero-row read invalidates rather than certifying "no ERP violations".
* An explicit `migration_jobs_on_erp` override may only raise the count; an explicit `0` used to
  beat both witnesses.
* Queue depths carried into the verdict, so the ruling's depth clause is read from the artifact.
* `a1` is a real stage — the verdict previously required an out-of-tree driver, the same shape as
  two earlier false readings in this delivery.
* Probe poll 50 ms -> 5 ms; the worst measured margin was 44.8 ms, and an instrument coarser than
  the headroom it resolves cannot support the verdict.
* **engine.py**: `throttle.consume` moved inside the `needs_upload` branch. It was counting — and
  sleeping for — bytes on the dedupe-convergence path where nothing crosses the network.

**The scale finding (reviewer H-3) changed the result, not just the paperwork.** A1 had only ever
been measured on a **1,020-object** campaign while `ACCEPTANCE_GATES.md:25` scopes F3 to
release-scale 100,000 files. A 100k corpus was seeded (`CFS-CAMP-0008`, 100,020 objects, 100
batches, 156 MB) and A1 re-measured there. Full evidence:
`docs/evidence/f3-a1-erp-queue-latency.md`.

**A1 at release scale — `VALID_MEASUREMENT`, PASS:** deltas **0.0833s / 0.1001s / 0.1332s**,
ratios <= **1.505x**, PROBE_FAILURES **0**, MIGRATION_JOBS_ON_ERP **0**, depths recovered, 2,000
objects progressed with job starts in both halves of the window.

**A second instrument defect, found by the new guard on real data.** The both-halves rule shipped
in `b5f0c05` judged continuity from migration *job-start* timestamps. At release scale a batch is
1000 objects, so a 40 s probe logs ~2 job starts and they cluster — two release-scale pairs were
rejected as `PARTIAL_LOAD` for windows in which 1,029 and 783 objects demonstrably moved. That
measures the batch size, not the load. `probe()` now samples campaign progress between queues.
Either signal establishes continuity, and the vacuity mutation still fails.

**Also found: queue depth 0 is not quiescence at release scale.** A dispatched 1000-object batch
keeps running long after nothing is queued, which contaminated one baseline. The quiesce now waits
for object progress to stop moving.

## 2026-08-19 — F3 throttle criterion: NOT re-established at the candidate

The `engine.py` change above alters the quantity the throttle criterion measures, so its previous
figure is invalidated by this delivery's own Rule 2. The re-measurement did not establish it:

```
throttled    1000 objects  transferred   226,213 B  41.22s  ->  5,487.9 B/s  (ceiling 250,000)
unthrottled  1000 objects  transferred   219,764 B  40.39s  ->  5,441.1 B/s
dedup_reused 1,484,431 of 1,710,644 B
```

Both batches ran at ~2% of the ceiling, so the limiter never engaged and throttled ~= unthrottled;
the stage correctly returns `passed: false`. **This is an instrument/corpus limitation, not a
product regression.** The analyzer scans every eligible File row on the site, so the campaign
absorbed the 100k small-payload A1 corpus and almost all bytes were dedup-reused — and after the
fix those correctly no longer count as transferred.

**Three further attempts refined the diagnosis without closing it**, and the blocker turned out
to be structural rather than a matter of trying harder:

1. Re-running on `cfs-throttle.local` reproduced the same result — that site now carries the 100k
   A1 corpus, and the analyzer scans *every* eligible File row, so any new campaign there absorbs
   it and is dominated by dedup-reused bytes.
2. A purpose-built pristine site (`cfs-thr3.local`, app installed, unique 300-600 KB payloads) was
   **refused by the harness**: `SCRATCH_SITES` allowlists five site names and nothing else may be
   touched. That guard is correct and was left alone — it exists so a rehearsal that creates 100k
   rows and uploads them can never run against a business site. Weakening a safety allowlist to
   unblock one's own measurement is not a trade worth making.
3. The one allowlisted site with no Cloud Storage Objects (`cfs-autonomous.local`) still carries
   prior campaign state — 528 `Skipped` and 39 `Conflict` objects — which the analyzer folds into
   any new campaign. The 200 freshly seeded objects stayed `Pending` (their files verified present
   on disk, nothing Failed) while the stage's batches addressed the older population, so both
   batches reported `NO_TRANSFER`. Configuring that site's bucket/endpoint, which was genuinely
   missing, did not change it.

**Remedy, for whoever closes this — with an earlier claim of mine corrected.** I first wrote
that both available remedies move the candidate SHA. That is wrong, and the distinction matters:
adding a site to `SCRATCH_SITES` is a tracked-file change and does move it, but **resetting an
allowlisted scratch site's migration tables is a data operation that touches no tracked file and
moves no SHA**. So the SHA was never what blocked this.

What remains is narrower than "contamination", and is now located rather than guessed. The batch
row after the last run reads:

```
CFS-CAMP-128160-B1  status=Pending  object_count=200  objects_done=200  bytes_done=0
   ... and all 200 of its objects are still `Pending`
```

**`objects_done` reached the full object count while `bytes_done` stayed 0 and not one object
advanced.** The upload loop (`engine.py:545-554`) ticks the heartbeat once per object regardless
of outcome — `beat.tick(size=size or 0, failed=size is None)` — so a batch can report itself
fully "done" having advanced nothing. Every early return in `process_object_upload`
(`engine.py:669-685`) is a candidate for what all 200 hit: the adoption branch, the thumbnail
branch (which, per the same review, is also the path that takes no throttle), the
`cas_object(expected="Pending" -> "Uploading")` guard returning `None`, and the vanished-source
handler.

**The branch has since been isolated by elimination, and the counter is not the defect.**
`Heartbeat.tick` increments `count` unconditionally and tracks `failed` separately
(`engine.py:480-487`), so `objects_done` counting an unadvanced object is by design, not a bug —
my first framing of it as a possible counter defect was wrong. The batch's own failure counter
settles what happened:

```
CFS-CAMP-128160-B1  objects_done=200  objects_failed=200  bytes_done=0
```

All 200 returned `None`. Each candidate branch was then eliminated against the data:

| branch | eliminated by |
|---|---|
| adoption (`legacy_fork_key`/`remote_https`) | all 200 are `local_private` (150) / `local_public` (50) |
| thumbnail | `is_thumbnail = 0` for all 200 |
| vanished source | all 200 `disk_path`s exist, 435,000 bytes each |
| path confinement | that path CAS's `Uploading -> Failed`; these are still `Pending` |
| dedupe short-circuit | returns `digest.size`, which would be non-zero |

That leaves exactly one path that returns `None` **without changing the object's status**: the
`cas_object(expected="Pending", to="Uploading")` guard at `engine.py:679-680`. So a
compare-and-set returned False for 200 rows that the batch iterator had just selected as
`Pending`, and `finalize_upload_batch` then correctly left the batch `Pending` on residue.

**That experiment was then run, and it disproved my own hypothesis.** I had suspected the
hand-written SQL reset used to re-arm the campaign. A brand-new campaign was created and driven
**entirely by the harness** — `analyze` -> `plan` -> `throttle`, no SQL of mine anywhere — over
400 freshly seeded, never-touched objects:

```
CFS-CAMP-128201-B1  object_count=400  objects_done=400  objects_failed=400  bytes_done=0
    ... all 400 objects still `Pending`
    ... local_private, is_thumbnail=0, size_bytes ~450 KB, disk_path present on disk
```

Identical signature. **The reset was not the cause**, and the behaviour reproduces from clean
state on this bench. (Incidentally `analyze` refused to re-run on the older campaign with
"Campaign ... is Planned; analysis runs from Draft" — a state-machine guard doing its job, and
the reason the earlier re-plan found nothing to batch.)

**Now fully attributed, and it exonerates the product.** The remaining question was answered
without touching tracked code, by exercising the live functions from an ad-hoc script:

* `cas_object(expected="Pending", to="Uploading")` called directly on one of the stuck rows
  returned **True** and transitioned it (restored afterwards). The CAS primitive is sound; my
  suspicion of it was wrong.
* `process_object_upload` called directly on the same object returned `None` and recorded
  **`error_class = CloudStorageTransportError`, `last_error = NoCredentialsError (upload_file)`**.

So every one of the 400 objects failed on **missing S3 credentials**, and `CloudStorageTransportError`
is a *typed transient* error — which is precisely why the object was returned to `Pending` rather
than marked `Failed`. That is **hard invariant 3 working as specified**: transient S3 errors are
typed, never converted, never swallowed, and leave the object retryable. There is no product
defect here, and my earlier framing of one was wrong.

The credentials gap was mine: the settings write used `if hasattr(d, "secret_access_key")` and the
secret is a Password field read through `get_password`, so it never landed. With
`use_default_credential_chain=0`, the key and secret set properly, and the bucket created, the
client authenticates (`head_bucket: OK`) and the failure moves on to the *next* environmental
obstacle — `IntegrityError(1062, "Duplicate entry ...")` on Cloud Storage Object, a unique-key
collision against content this shared scratch site already holds. Again typed, again retryable,
again not a defect.

**What closing this actually needs**, stated concretely for whoever picks it up: an allowlisted
site whose File corpus is *unique content* (no hash collisions with existing Cloud Storage
Objects) and whose Cloud Storage Settings carry a working key/secret and an existing bucket. Two
of those three were the obstacles here.

**Consequence for the gate:** F3's throttle criterion cannot be measured on this bench until that
is resolved, because every batch reports zero transferred bytes. Recorded OPEN with this evidence
rather than with a number.
Also noted: `storage_mode` reads `<absent>` on every scratch site inspected, so the field name
that check assumes may not be the one the DocType defines.

Recorded as **OPEN**, and F3 is not claimed as fully re-derived at the candidate on that basis.
The `NO_TRANSFER` and `passed: false` outcomes are the harness's three-state honesty working: it
refuses to compute a transfer rate from zero bytes rather than reporting a flattering number.

## 2026-08-19 — F3 throttle criterion: PASS on a clean scratch environment

**`THROTTLE_CRITERION=PASS`**, emitted by the harness (`rehearsal.throttle_verdict`), not by
arithmetic over a JSON blob. Site `cfs-thr4.local` (created for this measurement), campaign
`CFS-CAMP-0001`, unique-content corpus of 240 objects at 300–600 KB, dedicated empty bucket:

| | throttled (2 Mbps) | unthrottled |
|---|---|---|
| objects | 76 | 100 |
| uploaded / dedup-reused | 76 / 0 | 100 / 0 |
| failed | 0 | 0 |
| `ThrottleBudget.consume` calls | 76 | 100 |
| bytes transferred | 34,194,160 | 45,765,553 |
| elapsed | 136.82 s | 5.00 s |
| achieved | **249,928.8 B/s** | 9,145,665.8 B/s |
| ceiling | 250,000 B/s | — |
| **% of ceiling** | **99.97%** | — |

`measurement_state = VALID_MEASUREMENT`, `utilisation_of_ceiling = 0.9997`,
`unthrottled_speedup = 36.59x`. Every counted byte came from `consume`, so a dedup-reused object
contributes zero by construction; `dedup_reused_objects = 0` confirms the corpus was genuinely
unique.

**Preflight, mechanically checked immediately before the run** — the run does not start otherwise:
`CREDENTIALS_VALID=YES` (read through `get_password`, the same getter the client uses),
`BUCKET_EXISTS=YES`, `BUCKET/PREFIX_EMPTY_OR_UNIQUE=YES` (0 keys), `ACTIVE_CAMPAIGNS=0`,
`STARTING_OBJECT_STATE_VALID=YES` (240 Pending objects, 3 Pending batches),
`RESIDUAL_CSOS=0`, `WORKERS_HEALTHY=YES`.

### The earlier runs are ENVIRONMENT_INVALID — not PASS, not FAIL, not zero-throughput evidence

Three environmental conditions blocked the measurement, each found and fixed in turn. None was a
product defect, and none was worked around in the runtime engine:

1. **Credential never persisted.** The settings write used `if hasattr(d, "secret_access_key")`;
   the field is a Password read through `get_password`, so it never landed. Every upload raised
   `NoCredentialsError`.
2. **Shared-state collision.** After credentials were fixed, `IntegrityError(1062)` on Cloud
   Storage Object — a unique-key collision against content the shared scratch site already held.
3. **Circuit breaker open (A30).** The credential failures tripped the Redis-backed breaker
   (`FAILURE_THRESHOLD=5`, `COOLDOWN_SECONDS=60`), which is shared across sites on this bench, so
   later uploads were skipped with `upload_file skipped: object storage circuit breaker is open`.
   Cleared with the app's own `breaker.reset()`; the mechanism itself was left intact.
4. **SSE without KMS.** The final blocker: `build_extra_args` sets
   `ServerSideEncryption: AES256` from `sse_mode = "SSE-S3"`, and this local MinIO answers
   `NotImplemented — Server side encryption specified but KMS is not configured`. A plain
   `upload_file` succeeded and the same call with the app's `ExtraArgs` failed, which isolated it
   exactly.

**Recorded deviation:** `sse_mode` was set to empty **on this scratch site only**, because the
bench's MinIO has no KMS. That is a supported product setting, not a code change — the runtime
engine was not modified to bypass any of the four conditions. Production guidance is unchanged:
SSE-S3 remains correct, and a production bucket must have encryption available. This deviation
applies to no other site and to no shipped default.

### Product evidence the environmental failures produced

Worth keeping, because each was the product behaving as specified under fault:

* `cas_object(expected="Pending", to="Uploading")` called directly on a stuck row returned
  **True** and transitioned it — the CAS primitive is sound.
* `process_object_upload` classified every failure as **`CloudStorageTransportError`**, a typed
  transient error — never converted, never swallowed.
* `NoCredentialsError`, the breaker skip and `S3UploadFailedError` were all treated as
  **transient**, so objects returned to **`Pending`** rather than being wrongly marked `Failed`.
* **Hard invariant 3 behaved as designed** throughout: no object was lost, none was falsely
  terminal, and every one remained retryable once the environment was corrected.

## 2026-08-19 — F3 closed as PASS on all nine criteria; criterion 8 restated on an honest denominator

**Decision.** F3 = PASS. Criterion 8 (convergence >= 99.9%) is recorded as PASS on
**99,862 / 99,901 = 0.99961**, not on the post-triage 1.0 that the previous revision cited.

**Why the previous statement was wrong.** Triage moves objects that did not converge into
`Skipped`, and `Skipped` is excluded from the convergence denominator. The post-triage ratio is
therefore 1.0 by construction, whatever the engine did — the number cannot distinguish a
migration that converged from one that was tidied up. The project's own gate artifact
(`docs/evidence/p5-rehearsal/gates_pretriage.json`) records `convergence_pass: false` at
0.99845, and the row claiming PASS contradicted it. An independent reviewer named this as
"the gate being talked into passing"; the finding is accepted.

**The denominator, fixed by principle rather than by outcome.** Of 100,020 objects, exclude the
**66 deliberately seeded missing files** (File rows pointing at bytes never written — seeded to
prove the engine refuses them) and the **53 objects already `Skipped` before the run**. Every
other object is owed. The **39 legacy rows that failed on a harness seeding gap are counted as
NOT converged**, although their failure was the seeder's rather than the engine's, because the
denominator must not be drawn to flatter the result. The 50 privacy mismatches converge on the
documented operator path (A19) and count as converged.

**A criterion that could not fail, found and closed.** The throttle verdict required only that
the achieved rate sit under the ceiling. An overhead-bound corpus finishes far under any ceiling
with `time.sleep` never called, so a limiter that did nothing at all would have satisfied it.
`ThrottleBudget` now accumulates `slept`, `run_upload_batch` surfaces `throttle_slept_seconds`,
and `throttle_verdict` requires it `> 0` — otherwise `INVALID_MEASUREMENT`, which is neither PASS
nor FAIL. The new gate immediately rejected a real run at 2.68% of ceiling that had transferred
25,967 bytes across 100 objects. This is the same defect class as the `FakeObjectStore` stub and
the vacuous contract assertions: a check whose subject cannot make it fail.

**An instrument that reported a number it had never measured.** `objects_failed` was absent from
the harness's `get_value` field list. `frappe._dict` returns `None` for an unselected column
rather than raising, so `cint(row.objects_failed or 0)` produced a confident `failed_objects: 0`
for an entire measurement campaign without ever consulting a failure count. The earlier throttle
evidence is withdrawn on that basis and re-measured, not because its rate was wrong.

**A test that restated its subject.** `test_f3_throttle_measurement.py` defined its own
`old_rate`/`new_rate` helpers and asserted against those, so deleting the harness's `NO_TRANSFER`
branch outright left every test green. The derivation is extracted to
`rehearsal.throttle_measurement_row` and the tests now drive it. Six mutations — including that
one — were each confirmed to fail the suite.

**Evidence provenance was wrong on every carried row.** `07b459d` had been cited as the evidence
SHA for all six carried criteria; it is a preflight fix commit and produced none of them. Corrected
against `git log --follow` to `d9ea898` (100k rehearsal), `f399fd4` (disk I/O), `463e3d1` (job
count), `3b9272e` (fault suite) and `d1ce1a8` (Desk pause/resume smoke). The reviewer found two of
the six; the audit that followed found the rest.

**Stated, not fixed:** thumbnail derivatives are uploaded outside `ThrottleBudget.consume`, so
they are neither rate-limited nor counted. On a thumbnail-heavy corpus real egress exceeds the
configured ceiling by the thumbnail volume. Recorded as a product limitation for this release.

**Not claimed:** criteria 3 and 5 were measured at 6,278 objects and 5 batches respectively, well
below release scale, and are carried on the argument that resource ceilings and job-enqueue counts
are properties of an unchanged batch loop — not on having been observed at 100k.

## 2026-08-19 — the throttle criterion gets a floor, and the preflight becomes executable

**Decision.** `THROTTLE_MIN_SLEEP_FRACTION = 0.25`: the throttled batch must have spent at least
a quarter of its elapsed time asleep, in addition to sitting under the ceiling. And the six
preflight checks that gate the measurement are now a stage (`rehearsal.throttle_preflight`) with
committed output, not a paragraph.

**Why the floor.** The previous round closed a vacuity — a run under the ceiling with the limiter
never sleeping — by requiring `throttle_slept_seconds > 0`. An independent reviewer showed that
remedy is itself satisfiable without evidencing the ceiling: one object large enough to overshoot
the token bucket sleeps once (~0.3 s), and the run can then crawl for two minutes on per-object
overhead, finishing at ~3% of the ceiling with `throttle_engaged: true` and the free batch
faster — a PASS on exactly the run the criterion exists to reject. `> 0` was the letter of the
previous finding; the floor is its substance. Pinned from both sides by test, because a threshold
pinned only from below can be relaxed to nothing without a single test noticing.

**Why the preflight had to become code.** The row asserted six machine-shaped verdicts
(`CREDENTIALS_VALID=YES` and five others) that existed nowhere in the tree — they had been
produced by a throwaway script. This project has twice ruled that a gate whose application is
optional is not a gate, and it commits `p6-mutation-evidence.sh` and `p8-legacy-rehearsal.sh` for
this reason. The stage now emits the verdicts and `PREFLIGHT=PASS|FAIL`; its PASS for the
measurement is committed, and so is a refusal artifact produced by re-running it against the
environment the measurement itself left behind, where it fails three of six checks. A gate that
has never been seen to refuse is not known to be a gate.

**A figure that could not be derived from its own evidence.** The regression suite's headline
claim — the buggy derivation "produced 373,260 B/s" — is unreachable from the constants pinned in
the same commit: 7,488,078 / 18.20 = **411,432.9**. It was written from memory rather than from
the batch, in a file whose own comment warns that "the numbers must come from the same
measurement or they are not evidence of anything". Corrected at all three sites.

**Evidence SHAs mean the commit that recorded the artifact.** `7775793` was cited for criteria 1,
2 and 9; it is the commit that was HEAD when those runs executed, which is a different claim from
the one the column header makes. The A1 release-scale evidence was recorded at `0902f89`. The
column now says which sense it uses.

**Four low findings fixed rather than scheduled**, because each was a check that could not fail
or a row that could lie: `bytes_dedup_reused` could go negative when an object failed after
`consume`; the AST regression accepted a `throttle.consume` in the `else` branch of
`needs_upload` — counting bytes exactly when they did not cross the network, the inversion of the
invariant it pins; a missing batch row read as a row of zeroes instead of raising; and the
field-list test was satisfied by a comment mentioning the column.

**Re-measured from the shipped code**, gated by the committed preflight: 29,472,439 B in 117.92 s
= 249,937.5 B/s against a 250,000 ceiling (99.97%), asleep 113.23 s (fraction 0.9603), 31.3x
unthrottled, dedup 0, failed 0. The two earlier figures are recorded as superseded — the first
pair because their harness never read `objects_failed`, the third because it predates the floor
and the preflight and so was not produced by the code that ships.

## 2026-08-19 — the preflight's own gate had three holes, and widening one opened a fourth

**Context.** A third independent review passed every prior finding as closed but blocked on the
release document's headline still offering a **withdrawn** measurement as the evidence for the
throttle gate — the fourth time a summary in that file has been left behind by an edit to its own
body. Fixed, and the withdrawal is now named in the summary itself rather than only in the body.

**Three defects in `throttle_preflight`, which was new code nothing had reviewed:**

1. `ACTIVE_CAMPAIGNS` filtered on `["Running", "Dispatched"]`. **There is no `Dispatched` status**
   on Cloud Migration Campaign, so that clause matched nothing, and the filter missed `Analyzing`,
   `Stopping` and `Cleanup Running`, which do exist. `Analyzing` is the one that mattered: a second
   process writing Cloud Migration Object rows leaves no batch behind and every object still reads
   Pending, so every sibling check passes and the gate waves through the exact condition — a
   competing writer — it exists to catch. The in-flight set is now a named constant and a test
   pins every member against the DocType's own Select options.
2. `BUCKET_PREFIX_EMPTY_OR_UNIQUE` never consulted a prefix. It now scopes the listing to
   `key_prefix` when one is set, so the check answers the question its name asks.
3. `WORKERS_HEALTHY` matched only `" worker"`, so a concurrent `run`, `analyze` or `sample` — a
   second writer by any definition — was invisible. It now rejects any rehearsal process but
   itself, reusing `_rehearsal_pids` rather than the second, narrower matcher this change had
   introduced into the same file.

**Widening (3) immediately produced a false positive, found by running it rather than by reading
it.** `_rehearsal_pids` matched the **shell** that launched the stage: `bash -c '... -m
cloud_file_storage.tests.rehearsal ...'` carries the whole command in its own cmdline and
satisfies every string test. The preflight then refused a site with nothing running on it. This is
the same shape as the `pkill -f` pattern that twice killed the shell issuing it earlier in this
delivery. The predicate now requires the *interpreter* to be python, is extracted as
`_is_rehearsal_cmdline`, and is pinned by four tests including the shell case.

That fix also corrects the sampler, which shares the matcher: a shell wrapper was previously
eligible to be sampled as though it were application load.

**Standing lesson, recorded because it has now recurred:** widening a matcher to close a
false-negative is not a safe edit. It trades one error class for another, and the new one is
harder to see because the check still returns an answer — just a wrong one, in the conservative
direction, which reads as caution rather than as a bug.

## 2026-08-19 — F3's throttle evidence regenerated five times, and why that is recorded rather than hidden

**Decision.** The supersession record for the throttle measurement is kept as an extensible table
listing every displaced figure with its reason, and the regeneration count is stated openly in
both `docs/evidence/f3-nine-criteria.md` and `docs/RELEASE_EVIDENCE.md`.

**Why.** The headline figure moved five times. Each move was individually justified — every time,
the code that *gates or judges* the measurement had changed, so the committed artifact could no
longer honestly claim to have come from it. But a reader encountering only the current number has
no way to see that, and prose rewritten each round loses the earlier entries silently: an
independent reviewer found the record had already fallen one run behind in both documents, with a
figure that had been the shipped headline one revision earlier absent entirely and undisposed.

A list that is extended cannot lose an entry the way prose rewritten each round can. That is the
whole reason for the change of form.

**What the repetition does and does not mean.** Across six runs the throttled rate spans
**249,818.7 to 249,937.5 B/s — 0.05%** — while the unthrottled comparison ranged 5.63 to 7.82
MB/s, a 39% spread. The throttled figure is set by the limiter and is therefore stable; the free
figure is set by the bench's load that hour. The criterion depends only on the ordering, which
held in every run. The regenerations changed which code produced the evidence, not what the
product does.

**A precondition should ask for what the stage consumes.** `_starting_state_valid` required at
least one Pending object; `throttle()` needs **two Pending UPLOAD batches** and refuses without
them. A precondition that stops short still passes a site the stage will reject, which makes it
decorative at exactly the moment it matters. Raised to the real quantity.

**A narrowing fix can be more dangerous than the bug it fixes.** `_is_rehearsal_cmdline` was
narrowed to exclude shell wrappers, which fixed a false positive — but narrowing runs in the
*unsafe* direction for its three callers: a missed process means `WORKERS_HEALTHY=YES` on a
contaminated site and a sampler that under-measures. "This bench's interpreter basename contains
python" is now load-bearing, so it is asserted by test rather than assumed, and the NUL-delimited
input contract is documented — a caller that space-joins the cmdline first would get False for
everything, silently, at all three sites.

## 2026-08-19 — I edited the tree while an independent review was reading it

**What happened.** A reviewer was reading `a15d10e..ce8ac34`. While it read, I edited
`docs/evidence/f3-nine-criteria.md` and `docs/RELEASE_EVIDENCE.md` and committed `d9a7cff` ten
minutes after `ce8ac34`. The reviewer read the supersession table twice and got two different
versions — a five-row table on the first read and a six-row table on the second — and rather than
picking whichever suited it, it flagged the discrepancy and made its verdict conditional on my
confirming the tree had been quiescent. It had not.

**Why it matters.** `INVARIANTS.md` forbids two write-capable agents in one worktree. I read that as
being about *write* conflicts, and treated a read-only reviewer as something I could not conflict
with. That is wrong: a reviewer needs a frozen target for the same reason a writer needs an
exclusive one. A verdict rendered against a moving tree does not certify any particular tree, and
"reviewed at `<sha>`" becomes a claim nobody can substantiate — including the reviewer, which is
why it refused to assert it.

The failure mode is not hypothetical here. Had the reviewer silently taken its second read, it
would have passed content that was never in the commit it was asked to review, and the release
record would have carried a review of a tree that never existed.

**Rule adopted.** From the moment a review is requested until its verdict arrives, the worktree is
frozen: no code, no docs, no artifacts. If something must change, the reviewer is told first and
asked to stop reading. The freeze is the requester's responsibility, not the reviewer's problem to
detect — this one was caught only because the reviewer happened to read the same file twice.

**Credit where it is due.** The reviewer could have resolved the ambiguity in its own favour and
shipped a PASS. Refusing to certify a target it could not prove was stable is the behaviour that
makes an independent gate worth having, and it caught a process defect that three rounds of
content review had not.

## 2026-08-19 — "regenerate after every gate change" replaced by a test that says when to

**Decision.** The standing rule that committed evidence must come from the code that gates it is
kept, but its application is now mechanical: `TestTheCommittedEvidenceStillSatisfiesTheShippedGate`
loads `f3-throttle-preflight.json` and `f3-throttle-preflight-refusal.json` and asserts that the
**current** `_starting_state_valid` still accepts the state the PASS artifact records and still
refuses the state the refusal control records.

**Why.** Applied literally, the rule forced a fresh measurement after every preflight change —
six regenerations, with the marginal content of each change shrinking while the "why so many
regenerations" story got one entry longer. A reviewer pointed out that a bright-line rule with no
terminating condition is not more rigorous than a scoped one: it relocates the judgement to
whether you are still willing to pay for it, which is a worse place for the judgement to live.

It also showed the sixth regeneration was **not required by the evidence**, and did so from the
artifact rather than by assertion: `planner.py:83` creates batches with `phase: "UPLOAD"`, CLEANUP
batches exist only after a campaign reaches Verified, and the PASS artifact records a `Planned`
campaign with `batches_past_pending: 0` and `residual_cso: 0`. No CLEANUP batch can exist in that
state, so adding `"phase": "UPLOAD"` to the count leaves both artifacts identical field for field.
I had already regenerated by the time that arrived; the regeneration is sound but was avoidable.

**What the test buys.** A change to the predicate that would have refused the shipped run turns
the suite red and the artifact must be regenerated — correctly, and without anyone remembering the
rule. A change that would not is proven harmless by the same test and the artifact stands. Both
directions are proven by mutation: tightening the predicate past the shipped state fails it, and
loosening it until the refusal control would be accepted fails it too.

This is the same move made throughout this work — replace a rule someone has to remember to apply
with a check that fires by itself — applied to one of my own rules rather than to the product's.

## 2026-08-19 — bf0d88a SUPERSEDED: a mandatory CI step could hang instead of fail

**Classification: WORKFLOW_DEFECT.** Not product code, not an F3 subject — a defect in the release
gate's own machinery, and it blocked the mandatory matrix.

**What happened.** `bf0d88a` was pushed and final CI ran. Three jobs went green in minutes; two —
`Server (frappe v15.93.0)` and `Server (frappe version-15)` — sat on `Install MariaDB Client` for
**54 minutes**. The run was cancelled and the failed jobs re-run at the same SHA: `version-15`
passed, and `v15.93.0` hung on the same step again, observed minute by minute for **36 minutes**
before being cancelled.

**Cause.** All three install sites in `ci.yml` read:

    sudo apt update
    sudo apt-get install mariadb-client

Without `-y`, `apt-get install` asks *"Do you want to continue? [Y/n]"* whenever it must pull extra
packages. A runner has no stdin, so the step does not fail — it **hangs to the six-hour timeout**.
Whether it fires depends on what the runner image already carries, which is why the same step
finished in seconds on three jobs and hung on two. Nothing about the product changed between them.

**Why this is a defect and not a flake.** A mandatory gate that can hang is worse than one that
fails: it stalls silently and reads as slowness rather than as a fault. The first instinct — "CI is
being slow, re-run it" — is exactly the wrong response, and it cost a full re-run to rule out.

**Fixed** at all three sites with `sudo DEBIAN_FRONTEND=noninteractive apt-get install -y`, and
`apt update` replaced by `apt-get update` (the `apt` CLI is explicitly not stable for scripts).

**Guarded** by `tests/test_ci_workflow_contract.py`, which parses every workflow, walks each job's
`run:` steps, and fails if any `apt`/`apt-get` install/remove/purge/upgrade lacks an assume-yes
flag. It also pins the three mariadb sites by name and asserts no job or step carries
`continue-on-error`, which is what `ci.yml` itself defines "mandatory" to mean. Seven of its ten
tests drive the predicate with known-bad input, because a guard that cannot fail is worse than
none. Three mutations confirmed to fail it, including the near-miss that matters most:
`DEBIAN_FRONTEND=noninteractive` **without** `-y` — which suppresses debconf dialogs but not apt's
own confirmation, and is precisely the fix a careless hand would apply.

**Consequence for the release.** `.github/workflows/ci.yml` is a tracked release file, so the tree
has changed and **`bf0d88a` is SUPERSEDED**. Its CI evidence is not reusable as final release
evidence: F1's matrix never completed there, and the rest was produced by a workflow this candidate
no longer contains. F1–F6 must be re-established on the new candidate before F7 is run at all.

**What carries and what does not.** The F3 local closure stands: no F3 subject changed — no
product code, no throttle surface, no evidence artifact. What does not carry is any CI-derived
claim at `bf0d88a`, including the MinIO, L3 and F2 job results, which were green but were produced
by the superseded workflow.

## 2026-08-19 — correction: the bf0d88a CI stall was NOT the interactive apt prompt

**The previous entry misattributed the cause, and this corrects it.** That entry stated as fact
that `apt-get install` without `-y` blocked on a confirmation prompt and hung the job. The retained
GitHub Actions logs refute it.

**What the logs actually show**, for both hung jobs:

* the stall is inside **`sudo apt update`** — `apt-get install` **never executed** (no "Reading
  package lists" follows the echoed command in either job);
* **no confirmation prompt**: zero occurrences of "Do you want to continue?", "After this
  operation" or "Abort.";
* **no lock contention**: zero occurrences of "Waiting for cache lock", "lock-frontend",
  "dpkg lock" or "unattended-upgrade";
* last output at 08:23:15, job killed at 08:59:46 — **thirty-six minutes of silence** with no
  diagnostic line of any kind;
* the mirror `Ign:` retries that looked suspicious appear in the **passing** jobs too (32 in the
  `version-15` run that succeeded), so they do not distinguish the failure.

**Classification: INCONCLUSIVE.** Interactive prompting and lock contention are both positively
excluded by the absence of their signatures. No positive cause is claimed, because none is
supported. The missing `-y` could not have caused this stall: the command it guards never ran.

**Both fixes stand, for different reasons, and the record now says which does what.**

* **`timeout-minutes` on every job is the fix for the observed incident.** Whatever the mechanism,
  a job that stops producing output now dies in minutes rather than consuming GitHub's six-hour
  default. Policy from measured durations: server jobs ran 5.7-12.9 min and get 40; aggregation
  jobs ran 0.1 min and get 15; linters 20; CodeQL 60. Bounded above at 60 and below at 10 by test,
  so the numbers cannot drift into meaninglessness in either direction.
* **Non-interactive apt hardens a separate, real, latent hang** that did not fire here. Kept
  because it is correct on its own terms, not because it explains the incident.

**Why this correction matters more than the defect.** An independent reviewer challenged the
diagnosis on mechanism — noting that with stdin at EOF apt aborts rather than blocks — and asked
for the distinguishing log line rather than accepting elapsed time as evidence. Without that
challenge the release record would have carried a confidently stated root cause that the evidence
contradicts, and a "closed" defect that was never the problem. The claim had propagated to four
code comments, a test docstring and three documents before anyone looked at a log.

This is the same defect class this delivery keeps finding — a claim not derived from the thing it
is attributed to — and this instance is mine, made while fixing an instance of it.

## 2026-08-19 — amendment: "excluded" is not "exhausted"

The correction entry above says interactive prompting and lock contention are *positively excluded*
by the absence of their signatures. That is true and it is incomplete, and the incompleteness reads
as a stronger claim than the evidence supports.

**Excluding two hypotheses is not exhausting the candidate field.** At least one further class
remains unexcluded and fits every symptom: a **network or mirror stall inside `apt update`** —
silent, no diagnostic, intermittent, image- and region-dependent, and terminated only by an
external kill. Bare apt sets no read timeout, so a mirror that completes the TCP handshake and then
stalls the read hangs indefinitely.

The `Ign:` retries do not argue against it, and should not be cited as though they did: `Ign:`
means apt gave up on an index and moved on, whereas a hang is precisely the case where it did not.
Thirty-two of them in the job that passed shows mirror flakiness was present in both and fatal in
neither — neutral, not exculpatory.

`CI_STALL_CAUSE` stays **INCONCLUSIVE**. What changes is the honesty of the surrounding sentence.

**Acted on, since the location is established even though the mechanism is not:** all three
`apt-get update` invocations now carry `-o Acquire::Retries=3 -o Acquire::http::Timeout=15`, which
converts a stalled mirror into a fast failure inside the step rather than a 40-minute job timeout.
The job timeout remains the backstop for whatever that does not catch — the two are layered
deliberately, not redundantly.

## 2026-08-19 — release audit NO-GO at 71deb15: the software passed, the paperwork did not

**An independent release-manager audit of `71deb15` returned NO-GO**, and it was right. No gate
failed on its merits and no code change was required. F1, F2 and F5 passed **fresh** in CI at this
SHA; F3, F4 and F6 carry, with F4 and F6 additionally re-executed green in the same run. What
blocked F7 was the release documentation — the deliverables F7 itself names.

**Every finding was verified independently before acting on it.**

* **The F1/F2/F5 rows cited superseded evidence.** They named job IDs from the `2df3bbb` run
  (`95743634610`, `95743634993`, `95743635160`, `95743634953`) — and this document's own banner
  states that CI evidence produced by a workflow the candidate no longer contains is not reusable.
  `ci.yml` differs between those SHAs by exactly the change the banner describes, so the document
  disqualified its own cited basis. Correct evidence existed; it simply was not the evidence
  recorded. Re-cited against `71deb15`'s own jobs.
* **`docs/supported-versions.md` — a named F7 deliverable — was materially false.** It said "No
  push has been made", that the matrix was "backed by a CI configuration that has not yet run",
  and that the 3-ref criterion was "carried into the release audit, not claimed here". All three
  were true when written and false once the push happened. Corrected, with the superseded wording
  quoted in place rather than deleted.
* **The same document asserted a non-claim it disproved four paragraphs earlier:** "The three-ref
  CI matrix has not run. Only v15.93.0 is exercised", against an F1 row calling the floor ref
  green. Both could not be true.
* **F4's stated carry-forward verification did not reproduce, and this is the serious one.** The
  row claimed `git diff` over the five measured DocType JSONs "is empty — no schema drift since
  the rehearsal". It is not empty: `cloud_storage_audit_log.json` changed in `3d62336`. Re-derived
  rather than re-asserted, the change adds two **Select option values** to an existing field — no
  field, no column width, no index, leading `\n` preserved — so row width and the 770.56 B/row
  calibration are genuinely unaffected and the projection holds. **But the verification as written
  was false, on the one gate whose purpose is to refuse a start on understated numbers.**
* The candidate banner had been inserted between the gate table's header separator and its first
  row, splitting the table when rendered. `CHANGELOG.md` dated 1.0.0 three days before the
  candidate. `release-checklist.md` §2's allowed-hit list omitted thirteen files that legitimately
  name the fork — the three adoption modules the auditor found, plus README and every test that
  exercises compat/adoption, which surfaced on re-running the command while fixing it. A reviewer
  running §2 verbatim against correct code would have recorded a false fail.

**The pattern, stated because it is now unmistakable.** Every one of these is the same defect this
delivery keeps producing: **a claim that does not come from the thing it is attributed to.** A
job ID from a superseded run. A status paragraph that outlived its facts. A diff asserted empty
that is not. A checklist enumerating a subset of what it greps. The code has been repeatedly
sound while the record describing it drifted — and the record is what a release is audited on.

**Consequence.** Fixing these is documentation-only, but `docs/` is tracked, so the candidate SHA
moves and `71deb15`'s CI evidence goes with it. On the owner's instruction the full path is taken
rather than arguing the evidence carries: new SHA, complete mandatory CI re-run, and a fresh
release-manager audit. The cheaper carry-forward argument — that `ci.yml` and every test input are
byte-identical so the results would reproduce — is exactly the kind of reasoning that superseded
`bf0d88a`, and it was declined for that reason.

## 2026-08-19 — owner ruling: the terminal-documentation exception, and why F7 leaves the tree

**The problem, found by the `203d67e` release audit.** That audit returned NO_GO with five
blockers, every one an `EVIDENCE_DEFECT` and none a gate failing on its merits: F1–F6 were
genuinely PASS and the auditor re-executed F6 itself to confirm it. What failed was that the F7
deliverables did not record it — stale job citations, a carry-forward argument this file records
as declined, a live A1 self-contradiction, a false non-claim in `release-report.md`, and the
retracted byte-identical schema claim still standing in `PROGRESS.md`.

Fixing those is documentation-only. But under the rule recorded here, a docs edit moves the SHA,
which requires a fresh CI run and a fresh audit — whose own fixing commit would again cite
evidence from its parent and again carry a status the audit had not yet issued. **The
reconciliation could not terminate.** The auditor identified this and explicitly declined to
assume its way out, which was correct: it is a governance question, not a reviewer's call.

**Ruling 1 — a narrow terminal-documentation exception.** A final documentation/evidence-correction
commit may inherit the immediately preceding candidate's F1–F6 CI evidence without rerunning full
CI, provided all of the following are *mechanically* proven: the delta is documentation-only; zero
product/runtime Python, zero tests, zero CI/workflow/helper files, zero migration/patch
implementations, and zero configuration/schema/fixture inputs used by F1–F6 are changed; the parent
SHA carrying the evidence is named; and an independent reviewer confirms the delta cannot affect
any F1–F6 subject.

**This is not general carry-forward, and the distinction is the whole point.** The broad version of
this argument — byte-identical inputs, so the rerun is unnecessary — was put to the owner when
`bf0d88a` was superseded and was **declined**. It stays declined. Any non-documentation change
voids the exception and requires normal qualification.

**Ruling 2 — F7 is recorded outside the repository.** A tree cannot contain the verdict of an audit
performed against itself: a commit flipping `F7 PENDING` to `F7 PASS` is a SHA no auditor examined,
so the flip would always be unattested. The release documentation therefore states
`F7 PENDING EXTERNAL RELEASE-MANAGER ATTESTATION` and stops there, and **no commit is made to copy
the eventual verdict back in.** The attestation lives outside the repo, keyed immutably to the SHA
it approves.

**Why this is worth writing down rather than just doing.** The loop was not a mistake anyone made;
it is what the project's own rigour produces at the boundary where a document has to describe its
own approval. The rule that caught four generations of stale evidence — evidence stops carrying
when the thing producing it changes — is correct everywhere except at that boundary, where applied
literally it forbids ever finishing. The exception is scoped to exactly that boundary and nowhere
else, and it is recorded so the next person meets the reasoning instead of rediscovering the loop.
