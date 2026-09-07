<div align="center">

<h1>Cloud File Storage</h1>

S3-compatible object storage for Frappe attachments — uploads, downloads, serving,
migration and lifecycle, for Frappe **v15 and v16**.

[![CI](https://github.com/RamachandranMD/cloud_file_storage/actions/workflows/ci.yml/badge.svg)](https://github.com/RamachandranMD/cloud_file_storage/actions/workflows/ci.yml)
[![CodeQL](https://github.com/RamachandranMD/cloud_file_storage/actions/workflows/codeql.yml/badge.svg)](https://github.com/RamachandranMD/cloud_file_storage/actions/workflows/codeql.yml)
[![Linters](https://github.com/RamachandranMD/cloud_file_storage/actions/workflows/linter.yml/badge.svg)](https://github.com/RamachandranMD/cloud_file_storage/actions/workflows/linter.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-success.svg)](license.txt)
[![frappe](https://img.shields.io/badge/frappe-v15%20%7C%20v16-success.svg)](docs/supported-versions.md)
[![python](https://img.shields.io/badge/python-3.10--3.13%20%7C%203.14-success.svg)](docs/supported-versions.md)

</div>

One source tree serves both Frappe majors. **The Python requirements of those majors do not
overlap** — read [Compatibility](#compatibility) before installing anything.

---

## Contents

* [Compatibility](#compatibility)
* [Installation](#installation)
* [Configuration](#configuration)
* [Operation modes](#operation-modes)
* [Migrating existing attachments](#migrating-existing-attachments)
* [Security note — private thumbnails in the public tree](#security-note--private-thumbnails-in-the-public-tree)
* [What it does](#what-it-does)
* [Public objects require a bucket policy](#public-objects-require-a-bucket-policy)
* [Legacy URLs](#legacy-urls)
* [MinIO integration tests](#minio-integration-tests)
* [Origins](#origins)
* [Documentation](#documentation)
* [Contributing](#contributing)
* [License](#license)

---

## Compatibility

The app major tracks the Frappe major, the way the rest of the Frappe ecosystem does: one
branch per Frappe version line, built from the same source tree.

| App line | Ships | Frappe | Python | Database | Status |
| -------- | ----- | ------ | ------ | -------- | ------ |
| `version-15` | `15.x.x` | v15, floor **15.16.0** | **3.10 – 3.13** | MariaDB | Supported |
| `version-16` | `16.x.x` | v16 | **3.14 only** | MariaDB | Supported |
| `develop` | next major | Frappe `develop` | follows `develop` | MariaDB | Unstable — do not deploy |

> ### The Python ranges are disjoint
>
> Frappe v15 requires **Python 3.10–3.13**. Frappe v16 requires **Python 3.14**. No version of
> Python satisfies both.
>
> **One bench cannot serve both Frappe majors.** Moving a site from v15 to v16 is a bench
> rebuild on a new Python, not a `bench switch-to-branch`. Plan it as such, and pick the app
> branch that matches the bench you are installing into.

**Why the v15 floor is 15.16.0.** The private-serving path depends on
`frappe.core.doctype.file.utils.find_file_by_url` and on the `fid` query argument that
`download_private_file` accepts. Both first exist at 15.16.0; below that floor the app cannot
re-run core's own permission gate and would have to reimplement it. The floor is *declared* in
`pyproject.toml`, not merely documented.

**MariaDB only.** Postgres branches exist in a few places (`install.py` picks a plain index
instead of a prefix index, for instance) because they are correct and cheap — but they are
**not tested**: no CI job runs Postgres, the 100k-row migration rehearsal was MariaDB, and the
GC locking recount relies on `SELECT … FOR UPDATE` semantics that were measured on MariaDB.
Frappe v16's SQLite backend is likewise unsupported. Treat both as unsupported code that
happens to exist.

**Object store.** S3, or an S3-compatible store. Verified against AWS S3 semantics and MinIO.

**Parity evidence.** The same 1557 tests are discovered and run on both lines, with identical
results. CI runs the server suite against three Frappe refs (`fail-fast: false`) — the declared
floor, the bench-verified revision and the branch tip — plus a MinIO job doing real S3 round
trips, and an L3 job that installs `erpnext`, `hrms` and `india_compliance` together. Per-ref
run identities, and the honest status of each claim, are in
[docs/supported-versions.md](docs/supported-versions.md).

Releases up to and including `1.0.0` predate this scheme and are Frappe v15 only.

---

## Installation

Pick the branch that matches your bench's Frappe major. **Do this deliberately** — see the
warning below.

**Frappe v15 bench** (Python 3.10–3.13):

```bash
bench get-app --branch version-15 https://github.com/RamachandranMD/cloud_file_storage
bench --site <site> install-app cloud_file_storage
```

**Frappe v16 bench** (Python 3.14):

```bash
bench get-app --branch version-16 https://github.com/RamachandranMD/cloud_file_storage
bench --site <site> install-app cloud_file_storage
```

> **The Frappe dependency declaration is advisory.** The app declares its supported Frappe
> range under `[tool.bench.frappe-dependencies]` in `pyproject.toml`, but bench treats a
> mismatch as a **warning and installs anyway**. Nothing stops you putting the `version-15` app
> on a v16 bench; you get a warning in the install output and a broken app afterwards. Choose
> the branch yourself — do not rely on the tool to choose it for you.

After installation the app sits in `LOCAL_ONLY` mode, which is byte-identical to core Frappe
behaviour. Nothing is sent anywhere until you configure a bucket and change the mode.

**Deployment prerequisites** — the dedicated `cloud_migration` RQ queue, bucket and IAM
settings, and the procedure for adopting an existing `frappe_s3_attachment` 0.2.x install — are
in [docs/runbooks/deployment.md](docs/runbooks/deployment.md).

---

## Configuration

Open **Cloud Storage Settings** (single DocType, System Manager).

| Field | Notes |
| ----- | ----- |
| **Bucket** | Required. One private bucket for both public and private files; `pub/` and `prv/` are logical prefixes, not separate buckets. |
| **Region** | Required. |
| **Endpoint URL** | Optional; for S3-compatible providers. Validated as **HTTPS-only**, except on sites running with `allow_tests` (which is how local MinIO over `http://` is supported). |
| **Addressing Style** | `auto` (default), `path`, `virtual`. Path-style is what most non-AWS providers want. |
| **Key Prefix** | Optional prefix ahead of the site segment in every object key. |
| **Use Default Credential Chain** | **On by default** — boto3 resolves the instance-role / environment / profile chain. Leave it on wherever you can. |
| **Access Key ID** / **Secret Access Key** | Static credentials. **Hidden until `Use Default Credential Chain` is unticked**, and both sit at **permlevel 1**, which the shipped permission rules grant to **System Manager** only. Set both or neither. The secret is a `Password` field, read through `get_password`. |
| **Server Side Encryption** | `SSE-S3` (default) or `SSE-KMS`; **KMS Key ID** appears for the latter. |
| **Storage Class** | `STANDARD` (default), `STANDARD_IA`, `INTELLIGENT_TIERING` — **only** these. Live attachments must stay synchronously retrievable; archive classes belong on the backup bucket, not this one. |
| **Operation Mode** | See [Operation modes](#operation-modes). Defaults to `LOCAL_ONLY`. |
| **Public / Private Presign TTL** | Signed-URL lifetimes; 3600s and 300s by default. Signed URLs are issued at serve time and never stored. |
| **Ignored DocTypes** | Parent DocTypes whose attachments stay local. **Data Import**, **Prepared Report** and **Package Import** are seeded by default: their attachments are short-lived operational bytes consumed through local paths. The health panel reports them separately from cloud-managed bytes — the two are never added together. |
| **Object Delete Grace (Days)** / **Restore Window (Days)** | 30 and 30. The grace window must not be shorter than the restore window; the health panel warns if it is. |

Then press **Test Connection**. It does a real round trip against the bucket and returns the
health panel: reachability, credential resolution, bucket and encryption settings, and a set of
named warnings — including `public_private_residue` (see the [security
note](#security-note--private-thumbnails-in-the-public-tree)) and `deprecated_hook`.

**Credentials, in order of preference:** instance role or the boto3 default chain, then static
keys in the permlevel-1 fields. Secrets are never logged and never leave the settings row.

---

## Operation modes

Transitions are validated server-side. There are no silent fallbacks: where a mode says S3 is
authoritative, a failure surfaces as a failure.

| Mode | Behaviour | Use it for |
| ---- | --------- | ---------- |
| `LOCAL_ONLY` | **Default.** Byte-identical to core Frappe. Nothing is written to or read from the bucket. | Installing without committing; the safe resting state. |
| `DUAL_WRITE` | Writes to both local disk and the bucket; reads local. | Building bucket coverage before you trust it. |
| `S3_PRIMARY_LOCAL_FALLBACK` | **Recommended for production.** Writes to the bucket; reads the bucket, falling back to a local copy where one exists. | Steady-state production. |
| `S3_ONLY` | The bucket is the only copy. | Sites that have completed a migration and verified it. |

`Fail Insert on S3 Error` (off by default) decides whether an upload failure fails the document
insert or is retried out of band.

---

## Migrating existing attachments

Moving a site's existing files into the bucket is a **supervised campaign**, not a switch. Run
it from the **Cloud Migration Campaign** form, the migration dashboard, or the CLI:

```bash
bench --site <site> cfs-migrate-analyze   --new "Attachment migration 2026-09"
bench --site <site> cfs-migrate-plan      --campaign CFS-CAMP-0001
bench --site <site> cfs-migrate-preflight --campaign CFS-CAMP-0001
bench --site <site> cfs-migrate-start     --campaign CFS-CAMP-0001
bench --site <site> cfs-migrate-status    --campaign CFS-CAMP-0001 --watch
```

Lifecycle: `Draft → Analyzing → Analyzed → Planned → Running` (⇄ `Paused`, → `Stopping` →
`Stopped`), and `Running → Cleanup Running → Completed` only after an explicit cleanup
approval. One campaign may be active at a time, enforced under a row lock rather than in the
form. Batches are resumable; a crash resumes from the scan cursor.

**What the phases may and may not do:**

* **ANALYZE** writes only migration rows. It never touches `tabFile`.
* **UPLOAD** hashes and PUTs. It has **no deletion path at all** — the local file is not
  touched.
* **VERIFY** independently re-checks the bucket copy (a HEAD checksum below the multipart
  threshold, a streamed re-GET and re-hash at or above it) and then makes the engine's only
  write to `tabFile`: the guarded link. It also has **no deletion path**.
* **CLEANUP** is the only phase that moves a local file, only after a typed operator approval,
  and only after re-checking both copies. **It quarantines by default** — the file is renamed
  into quarantine and is restorable
  (`cloud_file_storage.migration.cleanup.restore_from_quarantine`), purged later by a daily job
  keyed on when the quarantine happened, never on the age of the attachment. `--direct-delete`
  skips quarantine, runs the same gates, and is not reversible.

Full procedure, conflict triage and the rehearsal protocol:
**[docs/runbooks/migration.md](docs/runbooks/migration.md)**.

---

## Security note — private thumbnails in the public tree

**Check this before go-live on every site, including sites that never install this app.**

Frappe core's `File.make_thumbnail` writes *every* thumbnail into the **public** site tree,
including the thumbnails of **private** files, at
`sites/<site>/public/private/files/<name>_<suffix>.<ext>`. bench's stock nginx template answers
`location /` with `try_files /<site>/public/$uri @webserver`, so a `GET` of
`/private/files/<name>_small.<ext>` is served **off disk to an unauthenticated caller** — no
session, no permission check, no access-log row, before any Frappe process runs. Core's own
`delete_file` looks for that thumbnail somewhere else, so it is also orphaned on disk when the
File is deleted.

**This is an upstream Frappe defect, not one this app introduces** — a stock site with no apps
installed has it — and **it is fixed in neither v15 nor v16**.

**What this app does.** Rows it manages are fixed: a private thumbnail is written to
`private/files/`, a privacy flip *moves* an existing one out of the public tree (moved, never
deleted), and the private serving path serves it behind core's permission gate. Rows it does
not manage — pre-install rows, ignored DocTypes, anything on a `LOCAL_ONLY` site — keep core's
behaviour, because `LOCAL_ONLY` is contractually byte-identical to core and because relocating
files it does not own is not the app's call.

**What it does instead is detect and warn.**
`cloud_file_storage.health.detect_public_private_residue` reports every file under
`sites/<site>/public/private/` on each `bench migrate` (as an Error Log row) and in the settings
health panel (`Test Connection` → `public_private_residue`). It moves nothing.

**Remediation is the operator's job, and the app will not do it for you.** Check by hand:

```bash
find sites/<site>/public/private -type f
```

The full procedure — which residue can simply be deleted, which must be moved to
`sites/<site>/private/files/`, and the belt-and-braces `location ^~ /private/ { internal; }`
nginx guard — is in **[docs/runbooks/deployment.md](docs/runbooks/deployment.md)**, section
*"SECURITY — private files served off disk from the PUBLIC site tree"*. Evidence, reproduction
and a suggested upstream patch are in
[docs/security/upstream-frappe-private-thumbnail-disclosure.md](docs/security/upstream-frappe-private-thumbnail-disclosure.md).

**Do not wait for an upstream fix before running the remediation.**

---

## What it does

1. Uploads public and private attachments to a **single private** bucket, with logical `pub/`
   and `prv/` prefixes. No object ACLs are ever set.
2. **Content-addressed object identity.** An object's key derives from the SHA256 of its bytes
   (`<prefix>/<site>/<pub|prv>/<sha2>/<sha2>/<sha256>`), never from the filename — so identical
   bytes are one object, and references to it can be counted.
3. **Nothing is deleted before it is verified.** Deleting the last File referencing an object
   schedules it, waits out a grace window, re-counts references under lock, and only then
   removes it — leaving a tombstone that revives if the same bytes come back.
4. Private files are served through the full permission gate, with exactly one access-log row
   and a short-TTL presigned redirect whose `Content-Disposition` is pinned into the signature.
   Public files use the nginx static path with a DB-verifying renderer behind it.
5. **Four operation modes**, with server-validated transitions and no silent fallbacks.
6. A **migration engine** that moves an existing site into the bucket as supervised campaigns:
   batched, resumable, pausable, independently verified, with an operator-approved cleanup that
   re-checks both copies before it quarantines anything.
7. **Backup** to an isolated bucket, with SHA256-verified uploads, freshness-asserted dumps,
   restore helpers, and a lifecycle-policy generator that is prefix-scoped and merges rather
   than replaces.
8. A **local materialization cache** with write-back, size and TTL bounds and a daily eviction
   pass, so `get_full_path()` and code that reads files off disk keep working.

`file_url` is only ever the canonical `/files/...` or `/private/files/...`. Signed URLs are
issued at serve time and never stored; healthy legacy URLs are never mass-renamed.

---

## Public objects require a bucket policy

Uploads send **no `ACL` key at all** — not even for public files. The bucket stays private, and
public delivery is a bucket-policy / CloudFront-OAC concern rather than a per-object ACL. If
you serve public attachments by direct bucket URL, grant read access with a bucket policy scoped
to your key prefix. Per-object `public-read` — the behaviour of `frappe_s3_attachment` 0.2.x —
is gone and will not come back.

---

## Legacy URLs

Every `/api/method/frappe_s3_attachment.controller.generate_file?...` URL written by 0.2.x keeps
working: `override_whitelisted_methods` remaps it to
`cloud_file_storage.api.compat.legacy_generate_file`. Stored `file_url` values are never
mass-rewritten.

---

## MinIO integration tests

**Cloud Storage Settings** validates *Endpoint URL* as HTTPS-only except on sites running with
`allow_tests` — which is exactly how local MinIO over `http://127.0.0.1:9000` is supported
without an inject-settings workaround.

The suite in `cloud_file_storage/tests/test_minio_integration.py` is opt-in and runs only when
`RUN_MINIO_INTEGRATION_TESTS=1`. Start a local MinIO:

```bash
docker run --rm -d \
  --name cloud-file-storage-minio \
  -p 9000:9000 \
  -e MINIO_ROOT_USER=minioadmin \
  -e MINIO_ROOT_PASSWORD=minioadmin \
  quay.io/minio/minio:latest \
  server /data --address ":9000"
```

Then:

```bash
export RUN_MINIO_INTEGRATION_TESTS=1
export CLOUD_FILE_STORAGE_MINIO_ENDPOINT="http://127.0.0.1:9000"
export CLOUD_FILE_STORAGE_MINIO_ACCESS_KEY="minioadmin"
export CLOUD_FILE_STORAGE_MINIO_SECRET_KEY="minioadmin"
export CLOUD_FILE_STORAGE_MINIO_BUCKET="cloud-file-storage-test"
export CLOUD_FILE_STORAGE_MINIO_REGION="us-east-1"
bench --site <site> set-config allow_tests true
bench --site <site> run-tests --app cloud_file_storage
```

With `RUN_MINIO_INTEGRATION_TESTS` unset (or not `"1"`) these are skipped and the mocked suite
still runs.

---

## Origins

`cloud_file_storage` is a rename and rewrite of
[alyf-de/frappe-attachments-s3](https://github.com/alyf-de/frappe-attachments-s3)
(`frappe_s3_attachment` 0.2.2), itself a fork of
[zerodha/frappe-attachments-s3](https://github.com/zerodha/frappe-attachments-s3). Both
copyrights are preserved in [license.txt](license.txt), as MIT requires.

Three changes on top of that fork will alter behaviour on an existing install:

* **No object ACLs anywhere.** Public delivery now needs a bucket policy or CloudFront-OAC
  scoped to your key prefix.
* **The `s3_key_generator` hook is not honoured.** Keys are content-addressed and *are* the
  object's identity; a site-supplied key would break dedup, reference counting and garbage
  collection at once. A detector reports loudly on every migrate if a site still defines one.
* **`run_migrate_existing_files` and its two settings are gone**, replaced by the migration
  engine.

The rest is in [docs/source-comparison.md](docs/source-comparison.md).

<details>
<summary><strong>History: what the ALYF fork changed relative to zerodha upstream</strong></summary>

<br>

Kept so the provenance of each behaviour stays traceable.

| Topic | zerodha upstream | ALYF fork (`frappe_s3_attachment` 0.2.x) |
| ----- | ---------------- | --------------------------------------- |
| **S3-compatible endpoint** | Uses default AWS endpoints only. | Added an endpoint URL field passed to `boto3.client(..., endpoint_url=...)` with path-style addressing so providers such as Hetzner Object Storage work reliably. |
| **Non-ASCII filenames** | Raw names in S3 metadata / content-disposition can break uploads or downloads for some characters. | Metadata `file_name` ASCII-normalized; presigned responses use RFC 5987 `filename*` so Unicode names round-trip in browsers. |
| **MIME / content type** | `python-magic` (`magic.from_file`). | `filetype` (`filetype.guess`) with a `mimetypes.guess_type` fallback — no `libmagic` dependency. |
| **Object key storage** | Stored the object key in the core **File** `content_hash` field. | Dedicated **File** custom field `s3_object_key` (Data 255, read-only, indexed with prefix 191 on MariaDB) ensured on install/migrate, with an idempotent backfill patch. |
| **`generate_file` (signed URL)** | Any authenticated caller could request a presigned URL if they knew the object key. | Resolves the **File** row by `s3_object_key`, runs `check_permission('read')`, then redirects; a missing row raises Does Not Exist. |
| **Ignored DocTypes** | Effectively a fixed skip list. | Child table on the singleton, **Data Import** seeded by default. |
| **Upload hook exposure** | `file_upload_to_s3` was whitelisted like other helpers. | Hook is not whitelisted. |
| **Migrate existing files** | Ran synchronously in the HTTP request. | Queued on the **long** RQ worker with deduplication, an **RQ Job** link and a configurable timeout; local disk files removed only after a successful DB commit. |
| **File folder after upload** | Post-upload SQL reset **File** `folder` / `old_parent` to `"Home/Attachments"`. | Updates only `file_url`, `s3_object_key`, `content_hash`, preserving the folder tree. |
| **Credentials** | Plain **Data** secrets. | *Secret Key* as **Password**, read via `get_password`. |
| **Quality / CI** | Minimal tooling. | Ruff, pre-commit, Semgrep, commitlint, GitHub Actions server tests, pip-audit, CodeQL, Dependabot. |

</details>

---

## Documentation

| Document | What it covers |
| -------- | -------------- |
| [docs/PLAN.md](docs/PLAN.md) | Approved architecture and delivery plan (binding) |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Module boundaries and data flow |
| [docs/supported-versions.md](docs/supported-versions.md) | The support matrix, honestly — including what CI has and has not executed |
| [docs/BRANCHING.md](docs/BRANCHING.md) | Branch model, version-line policy, backport rules |
| [docs/runbooks/deployment.md](docs/runbooks/deployment.md) | Deployment prerequisites; adopting an existing `frappe_s3_attachment` 0.2.x install |
| [docs/runbooks/migration.md](docs/runbooks/migration.md) | Running a migration campaign |
| [docs/runbooks/backup-restore.md](docs/runbooks/backup-restore.md) | Backup and restore |
| [docs/runbooks/desk-smoke.md](docs/runbooks/desk-smoke.md) | Post-deploy Desk smoke test |
| [docs/security/threat-model.md](docs/security/threat-model.md) | Assets, actors, controls |
| [docs/security/upstream-frappe-private-thumbnail-disclosure.md](docs/security/upstream-frappe-private-thumbnail-disclosure.md) | The upstream defect above, with evidence and a suggested patch |
| [docs/adr/](docs/adr/) | Decision records and the binding amendments register |
| [docs/source-comparison.md](docs/source-comparison.md) | What came from the fork, and what did not |
| [CHANGELOG.md](CHANGELOG.md) | Release history, including the 0.x fork line |

---

## Contributing

* **Target the hotfix branch, not the stable one.** A bug fix for v16 goes to
  `version-16-hotfix`; a feature goes to `develop`. Pushing to `version-15` or `version-16`
  triggers a release. The full model is in [docs/BRANCHING.md](docs/BRANCHING.md).
* **Conventional commits** are enforced by commitlint — the release version is computed from
  them, so a wrongly typed commit ships the wrong version number.
* `pre-commit run -a` (ruff lint + format) must be clean.
* Tests: `bench --site <site> run-tests --app cloud_file_storage`
  (the site needs `bench --site <site> set-config allow_tests true`).
* Every bug fix ships a regression test that fails before and passes after.
* Every schema change ships a patch.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full workflow.

**Reporting a vulnerability:** see [SECURITY.md](SECURITY.md). Report privately — never open a
public issue for a vulnerability.

---

## License

MIT — see [license.txt](license.txt). Copyrights from both upstream forks are preserved there,
as MIT requires.
