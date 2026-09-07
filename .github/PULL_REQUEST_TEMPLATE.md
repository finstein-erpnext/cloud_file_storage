<!--
Before you open this PR:

  1. Target branch — bug fix: `version-16-hotfix` (or `version-15-hotfix` if v15-only);
     feature: `develop`. NEVER `version-15` or `version-16` — a push to either releases.
     docs/BRANCHING.md §1 and §7.
  2. The PR title is a conventional commit. commitlint checks it, and semantic-release
     computes the released version from it. See CONTRIBUTING.md §5.
  3. `pre-commit run -a` green, and the suite green on a scratch site.
  4. `closes #1234` below auto-closes the issue this fixes.

Contribution guide: CONTRIBUTING.md   Invariants: docs/INVARIANTS.md   Gates: docs/ACCEPTANCE_GATES.md
Suspected vulnerability? Stop. Do not open a PR. See SECURITY.md.
-->

## What this changes, and why

<!-- The problem, then the change. Not a restatement of the diff — a reviewer can read the diff.
     If it changes behaviour an existing site depends on, say so explicitly. -->

closes #

## How it was tested

<!-- The commands you ran, on which line, and against what: mocked storage, MinIO, or a real
     endpoint. Name the new or updated tests. -->

```
bench --site <scratch-site> run-tests --app cloud_file_storage
```

- Version line tested: <!-- v15 / v16 / both -->
- Regression test (bug fixes only), by name: <!-- fails before the fix, passes after -->

## Rollback note

<!-- REQUIRED. One or two lines: how to undo this if it misbehaves in production.
     If it is irreversible — a data migration, a schema change, a deletion — say so and give
     the mitigation instead. "Revert the commit" is a valid answer when it is true. -->

## Backport

- [ ] No backport needed
- [ ] Backport to `version-15` — label the merged PR `backport version-15`
- [ ] Cannot be backported (say why: newer-frappe behaviour / schema change v15 cannot migrate to / it is a feature)

---

## Checklist

- [ ] **Target branch is correct** (docs/BRANCHING.md §7) and this is not aimed at a stable branch
- [ ] **PR title is a conventional commit** with the right type — `feat` bumps minor, `fix`/`perf` bump patch, everything else does not release (CONTRIBUTING.md §5)
- [ ] **Tests added or updated**; every bug fix ships a regression test that fails before and passes after
- [ ] **`pre-commit run -a` is green** locally
- [ ] **The full suite is green** on a scratch site — never on a site holding real data
- [ ] **No contract test was weakened.** C1–C19 (`tests/contract/`) still execute and pass; none skipped
- [ ] **Schema change?** Ships an idempotent patch and a test for it
- [ ] **Docs updated** where behaviour or an operator procedure changed (`README.md`, `docs/runbooks/`, `docs/supported-versions.md`)

## Invariants (docs/INVARIANTS.md — a violation rejects this PR regardless of test results)

- [ ] **Zero delete-before-verify.** No local file or thumbnail is deleted or quarantined before its remote object is independently verified, and re-verified at deletion time. No deletion calls added to UPLOAD/VERIFY modules
- [ ] **`file_url` stays canonical** — `/files/...` or `/private/files/...` only. No signed URL is stored; no healthy legacy URL is renamed
- [ ] **No `ACL` key** in any S3 call. No archive storage class on the live bucket. Nothing public-read
- [ ] **Physical S3 deletes only in deferred GC** (grace window, locking recount, tombstone)
- [ ] **No new raw SQL** against `tabFile` or Cloud Storage Object outside the three sanctioned audited sites
- [ ] **Timeout on every network call**; retries bounded, with backoff and jitter
- [ ] **No secrets** in code, config, fixtures, docs, logs, or this PR's description
- [ ] **Server-side authorization** re-checked at every new entry point — whitelisted method, hook, job, bench command. An ID from a request body is never trusted
- [ ] **Migration/repair background work targets the `cloud_migration` queue**
- [ ] **No TODO or stub** left behind and described as complete
