# Changelog

All notable changes to this app are documented in this file. Releases up to and
including 0.2.2 were published as `frappe_s3_attachment` by ALYF GmbH; their links point
at that repository.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Removed
- The `override_whitelisted_methods` remap pointing
  `frappe_s3_attachment.controller.generate_file` at this app's handler. The Frappe Cloud
  marketplace audit rejects overriding another app's whitelisted method.

  **Operator impact.** On a site adopted from the 0.2.x fork, fork-era
  `/api/method/frappe_s3_attachment...` URLs now return 404 until the migration campaign
  canonicalises `file_url` -- which the compat patch deliberately does not do (A10). Run the
  campaign before retiring the fork. `cloud_file_storage.api.compat.legacy_generate_file` is
  unchanged and still whitelisted, so the remap can be reinstated from your own app or site
  if you need those URLs resolving in the meantime; see README and hooks.py.

### Changed
- The File controller keeps `override_doctype_class` on this line: frappe v15 has no
  `extend_doctype_class` hook. The 16.x line composes the class instead. Which is used is
  probed from the framework, so the same source works on both.

## [15.0.0] - 2026-09-07

Renumbering of the 1.0.0 release onto the **v15 line**. No functional change from 1.0.0
beyond the compatibility work listed below; the major now tracks the frappe major, as it does
across this ecosystem (erpnext 15.x -> frappe v15), so that an installed app's metadata states
which frappe it supports.

### Added
- Release automation the repo previously lacked -- semantic-release per version line, weekly
  hotfix->stable PRs, and label-driven backports.
- `docs/BRANCHING.md`: the branch map, versioning policy, release, backport and support
  policy for both lines.

### Fixed
- Portability fixes shared with the 16.x line: SQL functions passed as strings in `fields=`
  replaced with the query builder; the migration job-id marker derived from `create_job_id`
  rather than hardcoded; the A34 `fid` floor read from the code object; `install.py` testing
  for MariaDB explicitly rather than "not postgres". All are no-ops on v15 and were verified
  not to regress it: 1557 tests, no new failure.

### Changed
- `frappe` window narrowed to `>=15.16.0,<16.0.0` for this line; `requires-python` is
  `>=3.10,<3.14`, matching what frappe v15 accepts.

### Note
- The `frappe` window in `[tool.bench.frappe-dependencies]` is **advisory**: bench warns and
  installs anyway (`bench/app.py:244`, `throw=False`). Choose the branch deliberately; run
  `bench validate-dependencies` if you want the check to fail.

## [1.0.0] - 2026-08-19

### Fixed (post-merge)

- **Legacy-adoption loop bounds its own passes.** `adopt_fork_objects` previously relied on an
  intersection guard alone; if the `File.cloud_storage_object` link write stopped happening for
  an environmental reason the loop could still spin. Termination is now a property of the loop
  itself — a pass ceiling derived from the candidate count — so it cannot be defeated by
  changing which query helper issues the read. A `bench migrate` that hangs is worse than one
  that fails, and this is the difference.

The first release under the name **cloud_file_storage**, and a rebuild rather than an
upgrade: object identity, deletion, serving, migration, backup and the Desk surfaces are all
new. Read **Removed** and **Changed** before upgrading — two of the removals are breaking.

See `docs/source-comparison.md` for what was taken from `frappe_s3_attachment` and what was
deliberately left behind, `docs/supported-versions.md` for the support matrix, and
`docs/security/threat-model.md` for the security model.

### Added

- **Content-addressed object identity.** `Cloud Storage Object` is the single source of
  physical identity: UUID name, unique `(content_sha256, visibility, bucket)`, unique
  `s3_key`, Long Int byte counters. Keys are
  `<prefix>/<site>/<pub|prv>/<sha2>/<sha2>/<sha256>` — the filename is never the uniqueness
  mechanism, so identical bytes are one object and reference counting is possible at all.
- **Refcount-safe deferred garbage collection.** Deleting the last File referencing an object
  schedules it (`pending_delete`), not deletes it: a grace window (30 days, coupled to the
  restore window), then a **locking** reference recount, then the physical delete and a
  tombstone. Tombstones revive to `pending_upload` when the same bytes are uploaded again.
  Objects owned by a non-terminal migration campaign are excluded from every sweep.
- **Four operation modes** — `LOCAL_ONLY`, `DUAL_WRITE`, `S3_PRIMARY_LOCAL_FALLBACK`,
  `S3_ONLY` — with server-validated transitions. Failures are explicit queryable states, never
  silent fallbacks.
- **Cloud-aware serving.** Private files are intercepted after `validate_auth`, keep core's
  `find_file_by_url` / `is_downloadable` semantics, write exactly one access-log row, and
  redirect to a short-TTL presigned GET with **disposition pinned into the signature**. Public
  files use the nginx static fast path with a DB-verifying renderer behind it. `.html`,
  `.htm`, `.svg` and `.xml` are always served as attachments (Policy B).
- **`Cloud File URL Alias`** — 302 redirects, consulted by both serving paths with the
  permission gate re-run on the resolved target, so the rare unavoidable URL change does not
  break stored links.
- **Bounded materialization cache with write-back**, at
  `sites/<site>/cloud_storage_cache/`, filelocked, with hash sidecars; every path handed out
  by `get_full_path()` is tracked, and write-back commits its own transaction.
- **Thumbnails as derived cloud objects**, linked `derived_of` to their source. A local
  thumbnail is never deleted until the source object is verified **and** thumbnail
  availability is guaranteed.
- **The migration engine**: Campaign / Batch / Object / File Ref / Conflict, a keyset analyzer
  with a nine-class classifier, a dedicated `cloud_migration` queue, batches of 1,000,
  per-object compare-and-swap with commits, heartbeats, stale recovery, pause / resume /
  stop-after-batch, bounded retry, a bandwidth throttle, an independent VERIFY phase, an
  operator-approved CLEANUP that re-verifies remote **and** local immediately before
  quarantining, collision-safe quarantine paths, a three-way reconcile, and a capacity
  preflight. **UPLOAD and VERIFY contain no local deletion path, and that is lint-enforced.**
- **ALYF legacy adoption**: fork rows are adopted in place as `legacy_unverified` and promoted
  to `verified` only after a streamed re-GET has produced a real hash.
- **Backup**: a separate `Cloud Backup Settings` Single and an isolated backup bucket/prefix;
  dumps taken with `force=True` and asserted fresh; SHA256-verified uploads; per-frequency
  prefixes and retention; a lifecycle-policy generator that is prefix-scoped, merges rather
  than replaces, and refuses policies whose economics destroy value; restore helpers and a
  runbook that verifies an artifact's digest before `bench restore`.
- **Desk surfaces**: the Settings form with a storage-health panel that reports cloud-managed
  and operational bytes **separately**, a migration dashboard Page, a Workspace, twelve
  mission buttons that are thin wrappers over `migration/api.py`, and a **Cloud Storage
  Manager** role that can operate storage without holding credentials.
- **`bench cfs-*` CLI** for every mission action, calling the same functions the Desk calls.
- **`bench cfs-adopt-legacy-install`** plus two guarded patches, for adopting a site that is
  still running `frappe_s3_attachment` 0.2.x.
- **C1–C19 compatibility contract**, run on every supported frappe ref and live against MinIO,
  with junit completeness gates that fail when a named test silently stops executing.
- **`File.get_content(self, encodings=None)`** — a superset of v15's signature: no argument is
  byte-identical to core, `encodings=[]` returns raw bytes. This is the India Compliance
  compatibility vanilla v15 lacks.

### Changed

- **Renamed from `frappe_s3_attachment` to `cloud_file_storage`** (title **Cloud File
  Storage**, publisher Finstein). Package, module directory, `modules.txt`, hooks, patches,
  locale and CI all follow.
- **New settings doctypes.** **Cloud Storage Settings** (Single) and **Cloud Storage Ignored
  DocType** (child) replace **S3 File Attachment** and **S3 Ignored DocType Row**, with the
  full frozen configuration schema. The fork's doctypes are neither renamed nor deleted; the
  bootstrap copies their values across.
- **Credential fields are at permlevel 1**, with a level-1 DocPerm for System Manager and none
  for the operator role. A migrate-time assertion checks both the shipped permissions and
  `Custom DocPerm`.
- **`file_url` is only ever canonical** `/files/…` or `/private/files/…`. Signed URLs are
  issued at serve time and never stored. Healthy legacy URLs are never mass-renamed.
- **`content_hash` is always a real MD5 again** and stays populated on File rows.
- Private `file_url` query values are URL-encoded, fixing keys and filenames containing spaces
  or `&`.
- A single presigned-URL TTL (`private_presign_ttl`, default 300s) replaces the fork's 120/300
  split; public serving has its own (`public_presign_ttl`, 3600s).
- Endpoint URLs must be HTTPS except on sites running with `allow_tests`.
- Request-path S3 calls are bounded (`connect_timeout=3`, `read_timeout=15`, 2 attempts) and
  sit behind a circuit breaker, so an S3 outage degrades file operations per mode rather than
  the ERP.
- Both type-to-confirm phrases (`DELETE LOCAL FILES`, `APPLY LIFECYCLE`) are compared **byte
  for byte** on the server. Neither is normalised.
- Minimum supported frappe raised to **15.16.0** (`<16`); CI runs {v15.16.0, v15.93.0,
  version-15} plus MinIO and L3 ecosystem jobs.
- `after_install` seeds **Data Import**, **Prepared Report** and **Package Import** as ignored
  DocTypes and creates the **Cloud Storage Manager** role, idempotently.

### Deprecated

- **`File.s3_object_key`** is superseded by the `File.cloud_storage_object` link. It is still
  created on every install and still resolved, because legacy lookups and the adoption
  analyzer depend on it.

### Removed

- **Per-object ACLs. No `ACL` key is sent on any S3 call, ever.** The bucket stays private and
  public direct-URL serving now requires a bucket policy or CloudFront-OAC scoped to your key
  prefix. **Breaking** for installs that relied on `public-read`.
- **The `s3_key_generator` hook is no longer honoured.** Object keys are content-addressed and
  are the object's identity; a site-supplied key would break dedup, reference counting and GC
  at once. A detector reports loudly on every migrate if a site still defines one. **Breaking**
  for installs that used it.
- **`run_migrate_existing_files`** and the two settings that belonged to it
  (`timeout_for_migration_job`, `migrate_existing_files`), dropped by a patch. The migration
  engine is its supervised replacement.
- `ping()`, `config/desktop.py`, `config/docs.py`, `tox.ini` and the dead QUnit stub.

### Fixed

- Object keys are no longer stored in `File.content_hash`
  ([alyf-de#10](https://github.com/alyf-de/frappe-attachments-s3/issues/10)); a patch moves
  legacy values into `s3_object_key`, so core's duplicate detection works again
  ([alyf-de#12](https://github.com/alyf-de/frappe-attachments-s3/issues/12)).
- Transient S3 failures raise typed errors and are never converted into `FileNotFoundError`,
  which used to make an outage indistinguishable from data loss.
- Overwrite flows that never call `.save()` now persist their own relink, so an optimised or
  replaced attachment does not lose its object.
- `frappe.utils.file_manager.get_file_path` — and India Compliance's import-bound copy — are
  patched to resolve the File row and return a materialized path, so readers that bypass the
  File API keep working under `S3_ONLY`.

### Security

- Private bytes are served only after the full permission gate, with exactly one access-log
  row, through a short-TTL, disposition-pinned presigned URL. Presigned URLs are minted at
  exactly four enumerated sites.
- Every destructive action is System Manager gated, type-to-confirm gated where it deletes,
  and audited. `Cloud Storage Audit Log` grants write and delete to nobody.
- The legacy endpoint's refusals are indistinguishable — unknown key, unreadable row and
  missing object all return the same error — closing the file-existence oracle the fork had.
- A backup artifact that fails verification is **never** deleted; it is kept and the log is
  marked Failed.
- Live objects are constrained to synchronously retrievable storage classes; archive classes
  are refused for the attachment bucket.
- Private-file thumbnails written by core into the public site tree are detected and reported
  on every migrate — an upstream disclosure this app surfaces rather than causes
  (`docs/security/upstream-frappe-private-thumbnail-disclosure.md`).

## [0.2.2] - 2026-05-27

### Added

- **S3 File Attachment** field _Timeout for Migration Job_ (`timeout_for_migration_job`, default 1500 seconds) for the background migration RQ job.
- German translations for background-migration Desk strings and the new migration settings fields.

### Changed

- **`migrate_existing_files`** enqueues **`run_migrate_existing_files`** on the **long** queue with deduplication and Desk messages linking to **RQ Job**; the whitelisted API returns `{"job_id": str, "queued": bool}` instead of `True`.
- Migration scans **File** rows with `file_url` set and `s3_object_key` not set (skips already-migrated rows earlier on large sites).
- **`file_upload_to_s3`** updates only `file_url`, `s3_object_key`, and `content_hash` (preserves `folder` / `old_parent`); removes the local file only after a successful DB commit.
- README _Changes vs upstream_ documents background migration and preserved **File** folder placement.

### Security

- **`migrate_existing_files`** is restricted to **System Manager** via `frappe.only_for`.

## [0.2.1] - 2026-05-12

### Added

- Patch `v0_2_1.ensure_data_import_ignored_doctype` appends **Data Import** to **S3 File Attachment** *Ignored DocTypes* when missing, so upgraded sites match fresh installs without controller-side forcing.

### Changed

- Ignored parent DocTypes for S3 upload are read only from the **S3 File Attachment** child table (administrators may remove **Data Import** to allow those files on S3).
- **`migrate_existing_files`** loads **File** candidates with non-empty **`file_url`**, skips rows whose URL already looks off-local (``http:``/``https:`` URLs or this app’s **`generate_file`** API path), checks **`File.exists_on_disk()`** before upload, and calls **`file_upload_to_s3`** so migration matches the insert hook (ignore list, ``attached_to_doctype`` fallback, parent **`image_field`**).
- **`s3_key_generator`** hook integration normalises return values (**`frappe.cstr`**, strip leading slashes), logs hook failures with stack traces instead of a bare ``except``, warns and falls back when the hook yields no usable key, and drops the unused legacy **`doc_path`** call shape; README documents the hook contract (#17).
- README distinguishes production **Endpoint URL** values using **`https://`** from local MinIO tests served over **`http://`** (#14).

### Fixed

- Private **File** rows migrated to S3 use the same **`generate_file`** query string as new uploads (including **`file_name`**) so presigned download filenames stay consistent.

### Removed

- Stale **`doctype_list_js`** hook registration (wrong DocType name and asset path); **S3 File Attachment** Desk behaviour is unchanged because the co-located client script already loads (#13).

## [0.2.0] - 2026-05-08

### Added

- Dedicated **File** custom field `s3_object_key` (Data, length 255, read-only and visible in Desk for audit) ensured automatically on install and migrate, with an indexed lookup (prefix 191 on MariaDB, plain on Postgres) for fast presigned-URL resolution.
- Backfill patch (`v0_2_0.backfill_s3_object_key`) that copies legacy `content_hash` values into `s3_object_key` for File rows whose `file_url` was managed by this app, then clears `content_hash` on app-managed S3 rows so existing data matches the upload-hook behaviour below.

### Changed

- S3 upload MIME detection uses **`filetype`** (`filetype.guess`) with a filename-based fallback instead of **`python-magic`**, removing the **`libmagic`** system dependency.
- Upload, presigned URL generation, and cloud-delete flows now read and write `s3_object_key` instead of overloading the core `content_hash` field.
- After each successful S3 upload, **File** `content_hash` is cleared so Frappe core does not treat subsequent uploads as duplicates of S3-backed rows (same intent as dedupe bypass for remote URLs; avoids the private-file crash in [issue #12](https://github.com/alyf-de/frappe-attachments-s3/issues/12)).
- `delete_from_cloud` is a no-op for File rows without an `s3_object_key`, so unrelated File deletions do not call out to S3.

### Fixed

- Stores the S3 object key in `s3_object_key` instead of the core `content_hash` column, avoiding column-length and misuse issues when the key was previously written into `content_hash` (see [issue #10](https://github.com/alyf-de/frappe-attachments-s3/issues/10)).

## [0.1.1] - 2026-05-08

### Added

- MinIO-backed integration tests for public/private upload, signed URL generation, and delete-from-cloud behavior.

### Changed

- CI workflow now includes a dedicated MinIO integration job and treats MinIO test failures as blocking.
- README documents local MinIO setup and environment variables for running integration tests.

## [0.1.0] - 2026-05-07

Initial ALYF fork release on `version-15` and `develop`, including all changes since forking from upstream `zerodha/frappe-attachments-s3`.

### Added

- Support for custom S3-compatible endpoint URLs via **S3 File Attachment** _Endpoint URL_.
- Characterization tests for upstream controller behavior and dedicated TDD slices for fork features.
- Configurable ignored DocTypes for S3 upload behavior via child table instead of `site_config.json`
- German locale support and updated translation templates.
- CI additions on top of baseline linting (server test workflow, Dependabot, CodeQL, vulnerability check job).

### Changed

- Repository tooling aligned with ALYF baseline (Ruff, pre-commit, Semgrep, commitlint, modern `pyproject.toml` layout).
- Improved **S3 File Attachment** field descriptions and validation behavior.
- `README.md` expanded with upstream delta documentation and branch guidance.
- Package version advanced from pre-release line (`0.0.x`) to stable `0.1.0`.

### Fixed

- Non-ASCII filename handling for metadata and download content disposition (RFC 5987).
- Signed URL generation now enforces **File** read permission checks.
- Internal upload hook is no longer exposed as a whitelisted API endpoint.
- Credential retrieval uses robust password access handling.
- Custom endpoint client setup now uses path-style S3 addressing for compatibility with endpoint-based providers.

### Security

- Closed a privilege-escalation path in signed URL generation by checking **File** permissions before redirecting.

[0.1.0]: https://github.com/alyf-de/frappe-attachments-s3/releases/tag/v0.1.0
[0.1.1]: https://github.com/alyf-de/frappe-attachments-s3/releases/tag/v0.1.1
[0.2.0]: https://github.com/alyf-de/frappe-attachments-s3/releases/tag/v0.2.0
[0.2.1]: https://github.com/alyf-de/frappe-attachments-s3/releases/tag/v0.2.1
[0.2.2]: https://github.com/alyf-de/frappe-attachments-s3/releases/tag/v0.2.2
[1.0.0]: https://github.com/finstein-erpnext/cloud_file_storage/releases/tag/v1.0.0
