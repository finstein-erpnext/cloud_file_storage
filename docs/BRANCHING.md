# Branching and releases

This app follows the branch and release model used across the Frappe ecosystem. Every claim
below about "how Frappe does it" is checkable in the clones this repo is developed against:
`frappe/.releaserc`, `frappe/.github/workflows/{create-release,initiate_release,backport}.yml`,
and the same files in `erpnext`, `india_compliance` and `hrms`.

---

## 1. Branch map

| Branch | Purpose | What lands here | Releases |
|---|---|---|---|
| `develop` | Next major line | Features targeting the next Frappe major | Nothing |
| `version-16` | Stable v16 | Merges from `version-16-hotfix` only | `v16.x.x` |
| `version-16-hotfix` | v16 fixes | Bug fixes and small improvements for v16 | Nothing directly |
| `version-15` | Stable v15 | Merges from `version-15-hotfix` only | `v15.x.x` |
| `version-15-hotfix` | v15 fixes | Backported fixes for v15 | Nothing directly |

```
   feature / fix PR
          │
          ├─────────────► develop                    (next major, no releases)
          │
          └─────────────► version-16-hotfix
                                │  weekly automated PR (initiate_release.yml)
                                ▼
                          version-16 ──► semantic-release ──► tag v16.x.x
                                │
                                │  backport label on the merged PR
                                ▼
                          version-15-hotfix
                                │  weekly automated PR
                                ▼
                          version-15 ──► semantic-release ──► tag v15.x.x
```

**Never commit directly to `version-15` or `version-16`.** A push to either triggers a release.

---

## 2. Versioning policy

**The app major tracks the Frappe major.** `version-15` ships `15.x.x` and runs on Frappe v15;
`version-16` ships `16.x.x` and runs on Frappe v16. This is the ecosystem convention, not a
local invention — erpnext `15.93.0`, india_compliance `15.18.1` and hrms `15.49.2` all do the
same, and it means an installed app states which Frappe it supports without the user consulting
a table.

| Part | Meaning |
|---|---|
| major | The Frappe major this line targets. Never bumped by a commit |
| minor | A `feat:` commit landed on the line |
| patch | A `fix:` or `perf:` commit landed on the line |

A **breaking change does not bump the major** — `.releaserc` sets
`{"breaking": true, "release": false}`, because the major is reserved for the Frappe major.
A change that cannot be made compatibly belongs on the next line, not in a major bump here.

**The same source may ship as both 15.x and 16.x.** The two lines are currently identical
apart from `__version__`, the declared `frappe` window, `.releaserc`, and the CI matrix.
Divergence is expected eventually; it is not required now, and identical content is not a bug.

`tests/test_p1_package_contract.py` derives the expected `frappe` window from `__version__`, so
a line whose metadata drifts from the branch it lives on fails its own test suite.

---

## 3. Release process

Automated. A maintainer merges; nothing is tagged by hand.

1. Fixes accumulate on `version-NN-hotfix`.
2. `initiate_release.yml` opens a `version-NN-hotfix → version-NN` PR every Tuesday 09:30 UTC
   (or on demand via **Run workflow**).
3. **Do not merge that PR until the full CI matrix is green on the head branch.**
4. Merging pushes `version-NN`, which triggers `create-release.yml` → `npx semantic-release`.
5. semantic-release reads the conventional commits since the last tag, computes the next
   version, rewrites `__version__` in `cloud_file_storage/__init__.py`, commits
   `chore(release): bumped to version X.Y.Z`, tags `vX.Y.Z`, and publishes GitHub release notes.

`create-release.yml` is one file shared by every line, triggered by a push to any stable
branch. It cannot release the wrong line: `.releaserc` names exactly one branch and differs per
line, and semantic-release refuses to release from a branch its own config does not name.

**Prerequisite:** a `RELEASE_TOKEN` repository secret with `contents: write` and `pull_requests:
write`. Without it these workflows fail rather than releasing incorrectly.

### Commit types

Enforced by commitlint (`commitlint.config.mjs`) and `pre-commit`.

| Type | Effect | Use for |
|---|---|---|
| `feat` | minor | New capability |
| `fix` | patch | Bug fix |
| `perf` | patch | Performance fix |
| `docs`, `test`, `ci`, `chore`, `refactor`, `style`, `build` | none | Everything else |

---

## 4. Backport policy

Fixes land on the **newest supported line first**, then flow downwards. Never the reverse: a fix
written only against v15 has not been shown to work on v16.

Label the merged PR `backport version-15` and `backport.yml` opens the PR against
`version-15-hotfix`. If it does not apply cleanly the action says so; do the merge by hand and
say in the PR why the code differs.

**Do not backport** a change that: depends on behaviour only present in the newer Frappe;
changes a DocType schema in a way an existing v15 site cannot migrate to; or is a feature
rather than a fix. Say so on the PR instead of silently dropping it.

---

## 5. Support policy

| Line | Status | Receives |
|---|---|---|
| `16.x` | Current | Features, fixes, security fixes |
| `15.x` | Maintained | Fixes and security fixes; features only if they backport cleanly |
| `0.2.x` and earlier | End of life | Nothing. Published as `frappe_s3_attachment` by ALYF GmbH |

A line is supported for as long as the Frappe major it targets is supported upstream.

---

## 6. Hotfix procedure

For an urgent production fix:

```bash
git switch version-16-hotfix && git pull
git switch -c fix/<short-description>
# fix, plus the regression test that fails before and passes after
bench --site <scratch-site> run-tests --app cloud_file_storage
pre-commit run -a
git commit -m "fix(<scope>): <what changed>"
```

Open the PR against `version-16-hotfix`. Once merged, label it `backport version-15` if v15 is
affected. To ship immediately rather than waiting for Tuesday, run **initiate_release.yml**
manually and merge the release PR after CI is green.

---

## 7. Contributor quickstart

| You are | Target branch |
|---|---|
| Fixing a bug affecting v16 | `version-16-hotfix` |
| Fixing a bug affecting only v15 | `version-15-hotfix` |
| Adding a feature | `develop` |
| Changing docs only | The line the docs describe; `develop` if it applies to all |

Every bug fix ships a regression test that fails before and passes after. `pre-commit run -a`
must be green. Commit messages are conventional commits — the release version is computed from
them, so an incorrectly typed commit ships the wrong version number.

---

## 8. What is unusual about this app

Three things a contributor coming from another Frappe app will not expect.

1. **One source tree currently serves both majors.** The compatibility work was done as
   version-agnostic code — behaviour is *probed* rather than keyed to a version number (see
   `migration_job_marker()`, `only_for_honours_in_test()`). Prefer that style over
   `if frappe.__version__` when the branches must differ.
2. **The Python ranges are disjoint.** Frappe v15 requires Python 3.10–3.13; v16 requires 3.14.
   No interpreter satisfies both, so each line's CI pins its own, and one bench cannot serve
   both lines. This is why the matrix uses `include:` rather than a bare ref list.
3. **The hard invariants in `INVARIANTS.md` gate every merge**, on every branch — zero
   delete-before-verify, canonical `file_url` only, no `ACL` key in any S3 call, raw SQL only at
   the sanctioned audited sites. They are not style preferences and a PR that violates one is
   rejected regardless of test results. `docs/ACCEPTANCE_GATES.md` carries the gates.

---

## 9. Bootstrap — what the owner must do before any of this releases

The branches, `.releaserc` files, workflows and baseline tags exist locally. Four things are
outside a contributor's reach and must be done once, by the repository owner.

| # | Action | Why it fails without it |
|---|---|---|
| 1 | **Push the branches and the baseline tags** — `version-15`, `version-16`, their `-hotfix` pairs, `develop`, and tags `v15.0.0` / `v16.0.0` | Nothing exists on the remote to release from |
| 2 | **Create a `RELEASE_TOKEN` repository secret** with `contents: write` and `pull_requests: write` | `GITHUB_TOKEN` cannot push to a protected branch, and step 5 of §3 writes `chore(release):` back to the stable branch. All three release workflows fail closed without it |
| 3 | **Protect `version-15` and `version-16`** — require a PR, require CI, forbid direct pushes; exempt the release bot so it can land its own `chore(release):` commit | Anyone could push straight to a stable branch and trigger an unreviewed release |
| 4 | **Create the labels** — `bash scripts/create-labels.sh` (needs `gh auth login` with repo admin) | A label that does not exist is not an error on GitHub: the action applies nothing. Missing labels make `labeler.yml`, `release.yml`, `stale.yml` and `backport.yml` silently do nothing rather than fail |
| 5 | **Install the `Stale` GitHub App** if `.github/stale.yml` is to have any effect | That file is probot-stale configuration, not a workflow. Without the app it is inert. Delete it instead if you would rather not run a stale bot |
| 6 | **Set the default branch** — `version-16` if the newest stable should be what visitors see; `develop` if active development should be | The repo currently has no `main` and no default set |

### Why the baseline tags matter

semantic-release computes the next version from the **last semver tag reachable from the
branch**. Both lines descend from the inherited ALYF history, whose newest tag is `v0.2.2`.
Without a baseline, the first automated release on `version-15` would compute **`0.3.0`**, not
`15.1.0` — the version-locking policy in §2 would be silently undone by the tooling on its
first run.

`v15.0.0` and `v16.0.0` are therefore seeded at the current tips. They are markers, not
releases: they carry no release notes and nothing was published from them. Every subsequent
version on each line is computed by semantic-release from the conventional commits since.

### The stale `version-15`

`version-15` previously sat at `51b41e6` — the ALYF `0.2.2` lineage, when the package was still
named `frappe_s3_attachment`. It was **fast-forwarded, not replaced**: `51b41e6` was verified to
be a true ancestor of the new tip first, so no commit was orphaned and the `v0.x` tags remain
reachable. `git branch -f version-15 51b41e6` restores the previous tip if that is ever wanted.
