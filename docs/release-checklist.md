# Release checklist — cloud_file_storage v1.0.0

Executable, not descriptive: every line is a command or an observation with a recorded
outcome. The execution record for v1.0.0 is `docs/release-report.md`; this file is the
procedure, reusable for the next release.

`PYTHONPATH` must point at the checkout being released on every bench command, or the
commands silently exercise whatever `cloud_file_storage` is on the bench path instead.

## 0. Preconditions

- [ ] Working tree clean; on the release branch; `main` untouched.
- [ ] A scratch site. **Never an existing business site** — every command below writes.
- [ ] MinIO (or a real S3 endpoint) reachable, with a bucket dedicated to this run.
- [ ] `docs/PROGRESS.md` shows every phase up to this one at **PASS**.

## 1. Version and changelog

- [ ] `cloud_file_storage/__init__.py` → `__version__ = "<version>"` (no `-dev` suffix).
- [ ] `CHANGELOG.md` has a dated section for `<version>` with Added / Changed / Deprecated /
      Removed / Fixed / Security, and the compare links at the bottom resolve.
- [ ] `README.md` describes the release, not a phase baseline.
- [ ] `pyproject.toml` frappe floor matches `docs/supported-versions.md`.

## 2. Rename hygiene (T-RENAME)

```
git grep -n frappe_s3_attachment -- . ':!docs' ':!CHANGELOG.md' ':!license.txt'
```

- [ ] **Exactly 19 files hit, broken down by area so the total is checkable against the list
      below.** A bare total tells you *that* something moved; only the breakdown tells you
      *where*, and where is the difference between a benign change and a disqualifying one.

      | area | files |
      |---|---|
      | `hooks.py` — compat endpoint remap | 1 |
      | `api/compat.py` — compat module | 1 |
      | `legacy_install.py` — bootstrap | 1 |
      | patches — `v1_0_0` legacy-fork bootstrap 2 + `v0_2_0` backfill 1 | 3 |
      | legacy-adoption machinery (`commands.py`, `migration/adoption.py`, `migration/analyzer.py`) | 3 |
      | `README.md` | 1 |
      | tests (`tests/`, `tests/contract/`) | 9 |
      | **total** | **19** |

      20 with tests at 10 reads "a new test names the fork — look at it". 20 with product at 11
      reads "new coupling — fail". (Note `19` appears three times in this document with different
      referents: 19 files here, C1–C19 in §6, 19 real-S3 tests in §7.)

- [ ] Any change to that number is a fail until explained — a new hit
      means new coupling, a lost hit means a compat path was deleted. Check the count first: it
      is mechanical, whereas "is every hit on the list?" is a judgement call, and that judgement
      is what went stale here through the whole of P8 until the `71deb15` release audit.
- [ ] Every remaining hit is one of: the compat endpoint remap in `hooks.py`, the compat
      module `api/compat.py`, the legacy-install bootstrap and its patches/fixture, a
      **patch** — note all three patches are *live*, not historical: the two `v1_0_0` ones are the
      A21 legacy-fork adoption bootstrap that must find the fork's rows at install time (both
      confirmed EXECUTED in the Patch Log), and `v0_2_0/backfill_s3_object_key` backfills this
      app's own earlier schema — the **legacy-adoption machinery** (`commands.py`,
      `migration/adoption.py`, `migration/analyzer.py` — which must name the fork's app to find
      and adopt its rows), `README.md`, or a **test** that exercises compat/adoption
      (`tests/` and `tests/contract/`). A hit anywhere else is a fail.

      *(This list was stale until the `71deb15` release audit. It omitted the three adoption
      modules, README and every test that names the fork — so a reviewer running §2 verbatim
      against correct code would have recorded a false fail on thirteen files. The audit found
      the three modules; the rest surfaced on re-running the command while fixing it. Enumerated
      by area rather than widened to a wildcard, so a genuinely new hit outside these still
      fails.)*
- [ ] `git grep -n "S3 File Attachment\|S3 Ignored DocType Row" -- cloud_file_storage` returns
      only the bootstrap and its fixture.

## 3. A fresh install works

```
bench new-site <site> …
bench --site <site> set-config allow_tests true
bench --site <site> set-config workers '{"cloud_migration": {"timeout": 1500}}' --parse
PYTHONPATH=<checkout> bench --site <site> install-app cloud_file_storage
PYTHONPATH=<checkout> bench --site <site> migrate
```

- [ ] Both exit 0 with no traceback in the log.
- [ ] `bench --site <site> migrate` a second time is a clean no-op.

## 4. The suite, three consecutive times on that pristine install

```
PYTHONPATH=<checkout> bench --site <site> run-tests --app cloud_file_storage
```

- [ ] Three runs, all green, same test count.
- [ ] **Singleton state compared between runs 2 and 3, not 1 and 2.** Run 1 legitimately
      differs on a fresh site: it rewrites six `Cloud Storage Settings` fields from SQL NULL to
      empty string. A difference between 2 and 3 is a leak.
- [ ] Row counts for this app's doctypes compared before run 1 and after run 3; any growth is
      named and explained or fixed.

## 5. Skips are enumerated by name

- [ ] Every skipped test is listed, with its reason, and every one is an **expected loud
      skip** (a missing optional app, or an integration suite whose endpoint is not configured
      for that run). A skip nobody can name is a fail.

## 6. Gates

```
JUNIT_XML=<junit> bash .github/helper/check_contract_completeness.sh
JUNIT_XML=<junit> bash .github/helper/check_backup_completeness.sh
JUNIT_XML=<junit> bash .github/helper/check_minio_completeness.sh   # MinIO run only
```

- [ ] C1–C19 complete.
- [ ] Backup completeness green — including the F-2 walk, which fails if a test marked
      `@refusal_guard` is not named in the gate's `REQUIRED` map.
- [ ] MinIO completeness green **on a run with `RUN_MINIO_INTEGRATION_TESTS=1`**, and — as the
      control — red on a run without it. A gate that has never gone red has not been tested.

## 7. Real object store

```
RUN_MINIO_INTEGRATION_TESTS=1 \
CLOUD_FILE_STORAGE_MINIO_{ENDPOINT,ACCESS_KEY,SECRET_KEY,BUCKET,REGION}=… \
PYTHONPATH=<checkout> bench --site <site> run-tests --app cloud_file_storage
```

- [ ] All 19 real-S3 tests execute (not skip).
- [ ] The bucket contains no object after the run that the run did not intend to leave.

## 8. Lint and formatting

```
pre-commit run -a
```

- [ ] Green **with zero files modified**. A hook that reformatted a file has not passed; it
      has fixed something that should have been committed already.

## 9. Legacy-install bootstrap

- [ ] On a synthetic 0.2.x site: `bench migrate` fails **before** the bootstrap (the red
      control), the bootstrap runs, `bench migrate` then exits 0, and a second migrate is a
      no-op.
- [ ] Fork values carried over; fork rows untouched; `file_url` unchanged; adopted objects are
      `legacy_unverified` with no hash.
- [ ] Recorded in `docs/evidence/`.

## 10. Residue

- [ ] After the runs, count rows of every doctype this app owns on the test site, plus files
      under `sites/<site>/private/files`, `public/files` and the materialization cache.
- [ ] Report the numbers. Anything non-zero is named and explained.

## 11. Documentation

- [ ] `docs/PROGRESS.md` updated (the implementer leaves the phase `in_review`; the implementer
      does not approve their own work).
- [ ] `docs/DECISIONS.md` has an entry for every ruling made during the release, including any
      test whose direction changed and why.
- [ ] Threat model, source comparison, supported versions and release report present and
      consistent with each other.
- [ ] Every runbook command in `docs/runbooks/` names a function or CLI command that exists.

## 12. Hand-off

- [ ] Commit with a conventional-commit message; do **not** push without the owner's request.
- [ ] Report the SHA, the site and lock state, and — explicitly — what was **not** done and why.
