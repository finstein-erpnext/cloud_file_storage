# Release report — cloud_file_storage v1.0.0

What was executed for this release, on this bench, on 2026-08-16. Numbers are measured, not
projected, unless a line says otherwise. The procedure this follows is
`docs/release-checklist.md`; the phase-by-phase record is `docs/PROGRESS.md`.

---

## 1. Storage accounting: three quantities, never one

PLAN §A requires this report to distinguish **cloud-managed permanent attachment bytes** from
**bounded temporary/local operational bytes**, and never to sum them. `health.storage_health()`
already models exactly that, in three buckets, and this report uses its split verbatim:

| Bucket | What it counts | Why it is separate |
|---|---|---|
| **Cloud-managed permanent attachment bytes** | Bytes held in the object store as `Cloud Storage Object` rows — split into source objects and derived (thumbnail) objects, with a per-status breakdown and a verified-bytes figure | These are the bytes the app is responsible for and the only ones a "migrated" number may count |
| **Bounded temporary / local operational bytes** | Attachments of the LOCAL_OPERATIONAL doctypes (Data Import, Prepared Report, Package Import), the materialization cache against its budget, and files quarantined by a cleanup pass | These stay local **by design** and are a documented mode exemption under `S3_ONLY`. Counting them as unmigrated attachments would report a permanent failure that is not one |
| **Local attachment bytes not yet cloud-managed** | Non-folder File rows with no object backing them and a parent that is not an ignored doctype | Neither of the above. Folding them into the operational number would excuse them; folding them into the cloud number would claim them |

**These three are never added together, anywhere** — not in the panel, not in the CLI status,
and not in this report. A single "storage used" figure would have to choose one of the three
lies above.

Measured on the release site `cfs-p8-rc2.local` after the full suite:

```
mode: LOCAL_ONLY
cloud_managed:       0 objects, 0 bytes (0 verified) · 0 derived objects · 0 linked File rows
local_operational:   0 files, 0 bytes · cache 0 / 5,368,709,120 byte budget · 0 quarantined
local_unmigrated:    0 files, 0 bytes
warnings:            none
```

Zeros are the correct reading for a site whose suite cleans up after itself; the shape is what
matters here, and a populated example is in `docs/runbooks/migration.md`. The measurement of
what this call *costs* at production scale is in `docs/evidence/p8-storage-health-cost.md`.

## 2. The test runs

These are the **orchestrator's attestation runs**, not the implementer's: taken independently on
a separate pristine install (`cfs-p8fin2.local`, built for the attestation — `bench new-site` →
`install-app` → `migrate`, all rc=0, nothing else), with both the site and the tree locks held
and the tree re-checked between every run. This report points at that evidence rather than
claiming to be it.

Three consecutive full-suite runs, **all three with `RUN_MINIO_INTEGRATION_TESTS=1` against the
real object store**:

| Run | Tests | Result | Skips |
|---|---|---|---|
| 1 | 1365 | **OK** | 4 |
| 2 | 1365 | **OK** | 4 |
| 3 | 1365 | **OK** | 4 |

**The same tree measured without the object store reports 1346 tests and 5 skips, and the two
figures reconcile exactly:**

```
1346 + 19 real-S3 tests   = 1365
   5 skips −  1           =    4     (the MinIO module's class-level skip stops firing)
```

Both are stated because a reader who sees only 1365 and then runs 1346 locally will assume
something broke. They are one tree measured two ways, not two measurements to choose between.

Both `v1_0_0` patches were confirmed **executed** in the Patch Log — not merely registered in
`patches.txt`, which is the distinction that check exists to make.

### Singleton state, compared between runs 2 and 3

`Cloud Storage Settings` and `Cloud Backup Settings` differ between runs 2 and 3 **only** in
`modified`. Run 1 is excluded from the comparison because it legitimately differs on a fresh
site — and it did, in exactly the documented way: six fields moved from SQL `NULL` to the empty
string (`access_key_id`, `bucket`, `cdn_base_url`, `endpoint_url`, `key_prefix`, `region`).

### Row counts across the three runs

Every doctype this app owns held the **same number of rows after run 3 as after run 1**:

```
Cloud Storage Object 0/0/0      Cloud Migration Campaign 0/0/0    Cloud Migration Batch 0/0/0
Cloud Migration Object 0/0/0    Cloud Migration File Ref 0/0/0    Cloud Migration Conflict 0/0/0
Cloud Storage Audit Log 0/0/0   Cloud Storage Backup Log 0/0/0    Cloud File URL Alias 0/0/0
Cloud Reconcile Run 0/0/0       Cloud Storage Ignored DocType 3/3/3 (the seeded trio)
File 2/2/2 (the two core folders)  User 4/4/4   Role 32/32/32   Custom DocPerm 0/0/0
```

The campaign-row leak carried out of P7 is closed: `TestAdminEndpointRoles` now deletes the
campaign it creates **and asserts the site's campaign count is unchanged**, which is the part
that will catch the next endpoint someone adds to that list.

### Skips, enumerated by name

Five per run, every one an expected loud skip:

| Skipped | Reason |
|---|---|
| `test_ecosystem.TestRepostItemValuation` (class) | needs erpnext + india_compliance — runs on the L3 job |
| `test_ecosystem.TestItemImage` (class) | same |
| `test_ecosystem.TestIndiaComplianceLive` (class) | same |
| `test_minio_integration.TestMinioIntegration` (class) | `RUN_MINIO_INTEGRATION_TESTS` not set — **absent in run 4** |
| `test_serving_spike…test_every_declared_ref_dispatches_through_the_module_attribute` (`ref='version-15'`) | that frappe ref is not checked out on this bench, so `frappe/app.py` cannot be analysed for it |

## 3. Gates

| Gate | Run | Result |
|---|---|---|
| Contract completeness (C1–C19) | attestation run 3 | **PASS** — 19/19 executed |
| Backup completeness | attestation run 3 | **PASS** — **61** refusal guards executed, 0 skipped |
| MinIO completeness | attestation run 3 | **PASS** — 19/19 real-S3 tests executed, 0 skipped |
| MinIO completeness | a report with the variable unset (**red control**) | **FAIL, exit 1**, as it must — "the whole MinIO class was SKIPPED" |

That last row is the point of the new gate. Before it, a run with all nineteen real-S3 tests
skipped passed every gate in the repository: the backup gate's module list does not include
that module and the contract gate only knows C1–C19. A typo in one of six env vars in `ci.yml`
would have shipped a green `tests-minio` job that never touched S3. The gate is now driven by
the job rather than by the variable, and it has been **observed red** on a real report with the
variable unset.

The backup gate also grew the other direction (finding F-2): it walks the test tree for
`@refusal_guard` and fails if a marked test is absent from its `REQUIRED` map, so a newly
written refusal guard cannot be written, pass, and be watched by nothing. Both failure modes —
a marked-but-unlisted guard, and a tree whose guards have vanished — are driven against the
real script in `test_backup_gate.py`.

## 4. Lint

`pre-commit run -a`: all nine hooks **Passed**, with **zero files modified**. Verified by
comparing `git status --porcelain` and the hash of `git diff HEAD` across the run, not by
reading the hook output alone — the run covers the newly added files, which were staged first
so `pre-commit run -a` could see them.

## 5. The legacy-install bootstrap

`bench cfs-adopt-legacy-install` and the two `v1_0_0` patches, proven by a real `bench migrate`
on a synthetic `frappe_s3_attachment` 0.2.x site, including the red control (migrate fails
before the bootstrap) and idempotency (a second migrate is a clean no-op). Full record:
`docs/evidence/p8-legacy-install-rehearsal.md`. Three defects were found by running it that a
code read had passed; they are listed there and in `docs/DECISIONS.md`.

## 6. Rename hygiene (T-RENAME)

`git grep -n frappe_s3_attachment -- . ':!docs' ':!CHANGELOG.md' ':!license.txt'` was executed.
Every hit is a place where the fork's name is load-bearing, and there are no others:

| Where | Why the name must appear |
|---|---|
| `hooks.py` | the `override_whitelisted_methods` remap keeping 0.2.x URLs alive (A10) |
| `api/compat.py` | the endpoint that remap points at |
| `migration/analyzer.py` | the classifier predicate that recognises a fork URL |
| `migration/adoption.py` | the docstring describing the two fork populations |
| `patches/v0_2_0/backfill_s3_object_key.py` | the historical `file_url` predicate |
| `legacy_install.py`, `patches/v1_0_0/*`, `commands.py` | the bootstrap, which exists to adopt that app |
| `tests/legacy_fixture.py` and eight test modules | fixtures and assertions about the above |
| `README.md` | prose about the lineage |

Nine runtime files, all of them compat surfaces. No module that implements this app's own
behaviour mentions the fork.

## 7. Residue, counted and named

After three suite runs on the pristine site, per run:

| Residue | Per run | After 3 runs | What it is |
|---|---|---|---|
| Files in `private/files` | **10** (109,680 bytes) | 30 (329,040 bytes) | test fixtures and export artifacts whose File **rows** were rolled back by the test transaction while the bytes had already been written to disk. 3 × `CFS-CAMP-*-migration-report.csv`, 1 × `CFS-RECON-*-drift.jsonl`, 6 × fixture attachments |
| `Error Log` rows | **23** | 69 | deliberate `frappe.log_error` calls the suite exercises — the residue detector, the deprecated-hook detector, the write-back error path, and the adoption warnings. **This figure moved twice during the phase** (17 at `39fb256`, 21 at `4d10128`, 23 at HEAD) as coverage was added; the current number is recorded rather than the first one, because a figure that moved and was written down once reads later as an error. Ruled Medium and non-blocking: all five Error Log assertions in the suite are **deltas**, not absolute counts, so none is fragile to it. The teardown-hygiene fix goes to the release audit |
| Rows in this app's doctypes | **0** | 0 | — |
| Materialization cache | 0 files | 0 | the eviction path leaves nothing behind |
| `public/private` residue | 0 files | 0 | — |
| Objects left in the MinIO bucket | 0 | 0 | the real-S3 suite removes every object it creates in `tearDown` |

The file residue is a Frappe test-harness artifact rather than an app defect — `save_file`
writes to disk before the DB commit, and a rollback does not unlink — but it is real, it is
linear, and it is recorded here rather than rounded to zero. It is worth an owner decision
whether the suite should unlink what it writes; nothing in the product depends on it.

## 8. What this release does NOT claim

1. ~~**The three-ref CI matrix has not run.** Only `v15.93.0` is exercised on this bench.~~
   **No longer true — corrected in place rather than deleted.** The matrix has run, and at the
   release candidate `203d67e` all three refs are green: v15.16.0 (job 96073731537), v15.93.0
   (96073731861) and version-15 (96073731838), each 1557 tests OK with
   `C1..C19 all executed (19/19)`. `docs/supported-versions.md` states the same.

   This item was carried verbatim after it stopped being accurate, and it survived into two
   release audits before being caught — the second of which NO-GO'd partly because this file and
   `supported-versions.md` had drifted into stating opposite things. It is kept struck rather
   than removed so the drift is legible.

   **Still true, and not superseded:** this *bench* runs a single frappe checkout at v15.93.0,
   so every local test run and rehearsal recorded in `docs/PROGRESS.md` was executed against that
   ref. The other two refs are exercised in CI only.
2. **No two-person rule.** Cleanup is two audited actions, but nothing makes the two actors
   distinct (DECISIONS 2026-08-16, finding L-2). Any statement that this app enforces a
   two-person rule would be false.
3. **Postgres is unsupported**, despite the branches in the code.
4. **1.2M rows were not migrated.** The 100k rehearsal is P5's evidence; the production-scale
   numbers in the capacity preflight are projections from measured per-row costs.
5. **`storage_health()` is not optimised.** Measured at ≈0.83s over 600k File rows and
   projected to ≈2s at 1.2M; a cached snapshot is backlogged, with the reasoning and the limits
   in `docs/evidence/p8-storage-health-cost.md`.
6. **No penetration test.** The security controls are asserted by tests and reviewed at each
   phase gate, which is not the same as an adversary having tried them
   (`docs/security/threat-model.md` §6).
7. **Invariant 3's `content_hash` clause is not met for adopted fork rows**, and this is the
   right place to say so. The invariant reads "`content_hash` (MD5) stays populated on File
   rows". On a site adopted from `frappe_s3_attachment` it is `NULL` for the fork-era
   population: the fork overloaded that column with the object key, and
   `patches/v0_2_0/backfill_s3_object_key` moves the key to `s3_object_key` and clears it
   rather than inventing an MD5 it has no bytes to compute. The runtime is built around that —
   ruled in P2, and the A1 resolution chain does not depend on `content_hash` — and the value
   is repopulated for real when a campaign's guarded VERIFY link (A26) runs against hashed
   bytes. It is listed here because "the invariant holds" and "the invariant holds for every
   row on every site" are different claims, and until that campaign runs only the first is
   true.
8. **P8 has been reviewed once and FAILED, and this is the second submission.** The phase is
   left `in_review` in `docs/PROGRESS.md`; the implementer does not approve their own work.

## 9. What the first review round found, and what round two changed

The reviewer returned **FAIL on one HIGH**: not one line of `legacy_install.py` that mutates a
site was executed by the committed suite. The credential fix that stops an adopted S3 secret
being destroyed on the next save sat on a line no unit test reached — proven by the rehearsal,
which is an artefact, and by nothing re-runnable. **Invariant 9 was not met**: both `v1_0_0`
patches called runtime functions no test executed, and only their registration was tested.

The reviewer's own distinction is worth repeating, because it is the one this project has been
careful about: **no test was weakened.** The test that changed direction (L-1) gained a stronger
assertion and kept its positive control; the one whose mechanism changed (A29) went from
machine-dependent to injected. The failure was missing coverage.

What made it survive every earlier gate was a *true-sounding reason applied past its scope*: the
module docstring justified the gap with "the bootstrap commits its own transaction by design",
which is true of `bootstrap()` and `finish()` and false of the five functions that do not commit
— including the one holding the credential fix. The coverage gate could not see it either: a
package-wide 90% floor cannot express "a new module's write paths must be covered", and an
entirely uncovered 200-statement module barely moves the aggregate.

Round two drives every non-committing function for real, each mutation-confirmed:

| Closed | Mutation that proves it |
|---|---|
| `bootstrap()` writes no settings value — behaviourally, and by AST | move `harvest_settings()` back into `bootstrap()` → both halves fail |
| `harvest_settings()` saves the real document, and the adopted credential survives the **next** save | write the secret with `set_encrypted_password` instead → `None != 'fork-secret-value'`, "the next save deleted the adopted credential" |
| `finish()` through the ignored-list branch, where L-12 actually lands | revert the flag fix → the real `ValidationError: Set both Access Key ID and Secret Access Key…` |
| `inspect_module_def()`, which had no coverage at all — and which, after reviewer M-5, asserts the fork's `Module Def` is **left alone** and would not be selected by an uninstall | — |
| Typed values at the `map_settings` boundary, closing the `bool("0")` class | drop the cast → `'900' != 900` |

Also in that round: security L-12; reviewer M-1 to M-4 (two docstrings corrected against
frappe's source, the uninstall consequence of repointing `app_name` documented, the P6 mutation
script re-anchored after L-1 moved the line it pinned, and the two runbooks made to agree that
promotion is manual); and L-1 to L-5 (a backup step before a procedure that rewrites
`installed_apps`, the stronger deletion checker borrowed rather than re-implemented, the
two-person-rule claims qualified at all three sites, and the fixture made both safe and
reachable).

The scanner extension that came with M-3 found a second stale pointer in the same evidence
script on its first run.

### Rounds three and four

Two further rounds followed, closing M-5 and L-6 through L-13 plus the test-engineer's N2 and N3.
The full record is in `RELEASE_EVIDENCE.md`; two results from them are worth carrying here,
because they are what a future reader needs rather than inventory.

**N2 — the File→CSO link was executed by no test that could tell whether it worked.** Removing
the write left the entire suite green at two separate SHAs. On an adopted site that means every
object is minted and none is served: `api/compat.legacy_generate_file` resolves the File row *by*
that column, so every legacy private attachment would 403. It is the same shape as H-1 — the
settings half of that module was covered, the object half was not.

**And running N2's mutation found what the finding did not predict: the link write is also the
loop's termination condition.** `adopt_fork_objects` re-reads "rows with a fork key and no object
link" with no offset, so removing the write does not produce a wrong result — it produces a
**hanging `bench migrate`**, which is worse, because an operator cannot distinguish it from a slow
one. The loop now compares each pass's rows with the previous pass's and stops loudly. No amount
of reading that function finds this; running it for ten minutes does. If this delivery carries one
argument for mutating over reviewing, that is the cleanest instance it produced.
