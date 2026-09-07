# Contributing to cloud_file_storage

Thanks for taking the time. This file covers the mechanics: which branch to target, how to get
a bench running, how to run the tests and linters, and what a pull request has to carry.

Two documents outrank this one and are worth reading first:

| Document | What it decides |
|---|---|
| [`docs/BRANCHING.md`](docs/BRANCHING.md) | The branch map, the versioning policy, the release process, the backport policy |
| [`docs/INVARIANTS.md`](docs/INVARIANTS.md) | The hard invariants. A PR that violates one is rejected regardless of test results |

Security vulnerabilities do **not** go through this process — see [`SECURITY.md`](SECURITY.md).

---

## 1. Which branch do I target?

`docs/BRANCHING.md` §1 is the authoritative map. The short form:

| You are | Target branch |
|---|---|
| Fixing a bug that affects v16 | `version-16-hotfix` |
| Fixing a bug that affects only v15 | `version-15-hotfix` |
| Adding a feature | `develop` |
| Changing docs only | The line the docs describe; `develop` if it applies to all |

Two rules that catch people out:

- **Never open a PR against `version-15` or `version-16`.** A push to either triggers
  `create-release.yml`, which tags a release. Stable branches only ever receive the weekly
  automated `version-NN-hotfix → version-NN` pull request.
- **Fixes land on the newest supported line first**, then flow downwards by backport. A fix
  written only against v15 has not been shown to work on v16.

If a merged fix should also ship on v15, label the merged PR `backport version-15` and
`.github/workflows/backport.yml` opens the downward PR for you. If it does not apply cleanly the
action says so; redo the merge by hand and explain on the PR why the code differs.

---

## 2. Setting up a bench

**The two lines cannot share a bench.** Frappe v15 declares `requires-python = ">=3.10,<3.14"`
and Frappe v16 declares `">=3.14,<3.15"`. The ranges are disjoint — no interpreter satisfies
both — so a bench is per version line, and so is the CI matrix (`.github/workflows/ci.yml` uses
`include:` rather than a bare ref list for exactly this reason).

### v15 line (Python 3.10–3.13)

```bash
bench init --frappe-branch version-15 --python python3.11 frappe-bench-15
cd frappe-bench-15
bench get-app https://github.com/finstein-erpnext/cloud_file_storage --branch version-15
bench new-site cfs.localhost
bench --site cfs.localhost install-app cloud_file_storage
bench --site cfs.localhost set-config allow_tests true
```

### v16 line (Python 3.14)

```bash
bench init --frappe-branch version-16 --python python3.14 frappe-bench-16
cd frappe-bench-16
bench get-app https://github.com/finstein-erpnext/cloud_file_storage --branch version-16
bench new-site cfs.localhost
bench --site cfs.localhost install-app cloud_file_storage
bench --site cfs.localhost set-config allow_tests true
```

`allow_tests` is required — without it `run-tests` refuses, and the settings validator rejects a
plain-HTTP endpoint URL, which is what makes local MinIO usable (see §3.2).

**Use a scratch site.** Every test run writes. Never point these commands at a site that holds
real data.

The version window each line declares is in `pyproject.toml` under
`[tool.bench.frappe-dependencies]`; `docs/supported-versions.md` explains why the floor is where
it is. `tests/test_p1_package_contract.py` derives the expected window from `__version__`, so a
branch whose metadata drifts fails its own suite.

### Install the git hooks

```bash
cd apps/cloud_file_storage
pre-commit install
```

`.pre-commit-config.yaml` sets `default_install_hook_types: [pre-commit, commit-msg]`, so this
one command installs both the formatting hooks and the commitlint hook that checks your commit
message.

---

## 3. Running the tests

### 3.1 The standard suite

```bash
bench --site cfs.localhost run-tests --app cloud_file_storage
```

A single module or test while iterating:

```bash
bench --site cfs.localhost run-tests --module cloud_file_storage.tests.test_cloud_file
```

Storage is mocked by default, so this suite needs no bucket and no credentials.

### 3.2 MinIO integration tests (opt-in)

Real S3 round trips. Skipped unless `RUN_MINIO_INTEGRATION_TESTS=1`.

```bash
docker run --rm -d --name cfs-minio -p 9000:9000 \
  -e MINIO_ROOT_USER=minioadmin -e MINIO_ROOT_PASSWORD=minioadmin \
  quay.io/minio/minio:latest server /data --address ":9000"

export RUN_MINIO_INTEGRATION_TESTS=1
export CLOUD_FILE_STORAGE_MINIO_ENDPOINT="http://127.0.0.1:9000"
export CLOUD_FILE_STORAGE_MINIO_ACCESS_KEY="minioadmin"
export CLOUD_FILE_STORAGE_MINIO_SECRET_KEY="minioadmin"
export CLOUD_FILE_STORAGE_MINIO_BUCKET="cloud-file-storage-test"
export CLOUD_FILE_STORAGE_MINIO_REGION="us-east-1"

bench --site cfs.localhost run-tests --app cloud_file_storage
```

Use a bucket dedicated to the run. The suite writes and deletes objects in it.

### 3.3 What CI runs

`.github/workflows/ci.yml` runs the server suite against each supported frappe ref on the
interpreter that ref can install on, plus a MinIO job and an L3 ecosystem job (erpnext + hrms +
india_compliance). Coverage shards are combined and a floor applied once, to the union.
`docs/ACCEPTANCE_GATES.md` (F1–F7) is what those jobs are enforcing.

---

## 4. Linting

```bash
pre-commit run -a
```

Must be green before you open the PR; CI runs the same hooks (`linter.yml`, job `Pre-Commit`).
That workflow also runs Semgrep with `frappe/semgrep-rules`, `pip-audit`, and commitlint over
every commit in the PR.

Formatting is `ruff` and `ruff-format` configured in `pyproject.toml`: tabs, double quotes,
line length 110. Do not reformat code you did not otherwise touch — a diff that is 90 %
whitespace cannot be reviewed.

---

## 5. Commit messages

Conventional commits, enforced by commitlint (`commitlint.config.mjs`, extending
`@commitlint/config-conventional`) at commit time via `pre-commit` and again in CI over the
whole PR range.

```
<type>(<scope>): <subject>

<body>

<footer>
```

**The release version is computed from these commits** by semantic-release (`.releaserc`,
angular preset), so the type you choose decides the version number that ships:

| Type | Version effect | Use for |
|---|---|---|
| `feat` | **minor** | A new capability |
| `fix` | **patch** | A bug fix |
| `perf` | **patch** | A performance fix |
| `revert` | **patch** | Reverting a previously released commit |
| `docs` | none | Documentation only |
| `test` | none | Tests only |
| `refactor` | none | Behaviour-preserving restructuring |
| `style` | none | Formatting only |
| `build` | none | Packaging, dependencies |
| `ci` | none | Workflows, helper scripts |
| `chore` | none | Everything else |

**A breaking change does not bump the major.** `.releaserc` sets
`{"breaking": true, "release": false}` because the major is reserved for the Frappe major this
line targets — `version-15` ships `15.x.x`, `version-16` ships `16.x.x`. A change that cannot be
made compatibly belongs on the next line, not in a major bump here. Describe it in a
`BREAKING CHANGE:` footer anyway so it appears in the release notes and the reviewer sees it.

The scope is optional and not enum-enforced, but pick one already in use rather than inventing
a synonym — mostly the package's own module names: `storage`, `serving`, `migration`, `backup`,
`cache`, `gc`, `api`, `desk`, `settings`, `security`, `tests`, `ci`, `deps`, `readme`.
`git log --pretty=%s` is the reference.

---

## 6. Every bug fix ships a regression test

Not a suggestion. The test must **fail before your fix and pass after it** — check both
directions, don't assume:

```bash
git stash          # remove the fix, keep the test
bench --site cfs.localhost run-tests --module <your test module>   # expect FAIL
git stash pop
bench --site cfs.localhost run-tests --module <your test module>   # expect PASS
```

Name the test in the PR description. `tests/test_documentation_references.py` checks that test
names cited in `docs/DECISIONS.md` and `docs/PROGRESS.md` resolve to real methods, so a pointer
that goes stale is caught — but only for names it can see.

Tests live in `cloud_file_storage/tests/`. The compatibility contract C1–C19 lives in
`cloud_file_storage/tests/contract/` and is the set of behaviours a Frappe site is entitled to
expect from `File` regardless of what this app does underneath. **Never weaken a contract test
to get a green run** — if C1–C19 goes red, the change is wrong, not the test.

---

## 7. The hard invariants

`docs/INVARIANTS.md` carries the full list and is the authority. A PR is rejected on any of these
regardless of what the tests say. The ones an outside contributor is most likely to trip:

1. **Nothing is deleted before it is verified.** No local file or thumbnail is removed until its
   remote object has been independently verified, and re-verified at deletion time. The UPLOAD
   and VERIFY migration modules contain no deletion calls at all — a grep gate enforces this.
2. **`file_url` is only ever `/files/...` or `/private/files/...`.** Signed URLs are issued at
   serve time and never stored. Healthy legacy URLs are never mass-renamed.
3. **No `ACL` key in any S3 call**, and no archive storage class on the live bucket.
4. **Physical S3 deletes happen only in deferred GC** — grace window, locking recount, tombstone.
5. **Raw SQL against `tabFile` or Cloud Storage Object** exists at exactly three sanctioned,
   audited sites. Adding a fourth fails the gate.
6. **Every schema change ships a patch and a test**, and patches are idempotent.
7. **Secrets are never logged or committed.**

`docs/ACCEPTANCE_GATES.md` F6 lists the lint gates that enforce several of these mechanically.

---

## 8. Opening the pull request

`.github/PULL_REQUEST_TEMPLATE.md` is the checklist and it is not decorative. In particular:

- **The PR title is a conventional commit** — it is what commitlint checks and, on a squash
  merge, what semantic-release reads.
- **A rollback note is required.** One or two lines: how to undo this if it misbehaves in
  production. If it is irreversible — a data migration, a schema change — say what the
  mitigation is instead.
- **Say whether a backport is needed** and add the `backport version-NN` label after merge.
- `closes #1234` in the description auto-closes the issue.

Reviews look for correctness and security first, then data integrity and reliability, then
maintainability. A change that is faster but makes attachment state inconsistent does not merge.

---

## 9. Reporting bugs and requesting features

Use the issue forms — `.github/ISSUE_TEMPLATE/`. The bug form asks for the version line, the
frappe and Python versions, the database, the operation mode
(`LOCAL_ONLY` / `DUAL_WRITE` / `S3_PRIMARY_LOCAL_FALLBACK` / `S3_ONLY`) and whether the problem
reproduces on a scratch site. Those six answers are what makes a report reproducible; a report
without them will be asked for them before anything else happens.

**Never paste credentials, presigned URLs, bucket names you consider sensitive, or `site_config.json`
into an issue.** A presigned URL is a bearer token for the object it points at.

Suspected vulnerabilities go to [`SECURITY.md`](SECURITY.md), never to the issue tracker.

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).
