# Requirements (FROZEN at P0.5)

Source of truth: the owner's project directive (mission) + the approved plan
(`docs/PLAN.md`). IDs are stable — never renumber; mark superseded items instead.
Acceptance mapping: `docs/ACCEPTANCE_GATES.md`. Anything not traceable to the mission or
the approved plan is NOT a requirement (the pre-P0.5 draft REQUIREMENTS.md containing
invented items was discarded).

## CFS-1 Runtime storage
- CFS-1.1 S3-backed public and private attachments; private S3 bucket only.
- CFS-1.2 Public serving via CloudFront-OAC/private S3 or controlled presigned serving —
  never a public-read ACL dependency.
- CFS-1.3 Private serving only after Frappe permission validation, then short-lived
  signed URL.
- CFS-1.4 Server-side `get_content` works for S3-backed Files.
- CFS-1.5 Temporary, bounded local materialization/cache for flows that need real
  filesystem paths — with write-back safety.
- CFS-1.6 Local fallback when configured.
- CFS-1.7 Operation modes: LOCAL_ONLY, DUAL_WRITE, S3_PRIMARY_LOCAL_FALLBACK, S3_ONLY —
  server-validated transitions; explicit queryable failure states, no silent fallbacks.

## CFS-2 File identity & collision handling
- CFS-2.1 Filename is never storage identity; original filename preserved separately
  (`File.file_name`, applied at serve time via RFC-5987 disposition).
- CFS-2.2 Physical key contains an immutable unique identifier:
  `<prefix>/<site>/<pub|prv>/<sha256[:2]>/<sha256[2:4]>/<sha256>` (FROZEN).
- CFS-2.3 Same filename + different bytes ⇒ separate objects.
- CFS-2.4 Same content may reuse one Cloud Storage Object; multiple File rows may
  reference one object.
- CFS-2.5 Deleting one File never deletes a shared object still referenced by another
  (derived refcount + deferred GC with grace, locking recount, tombstones).

## CFS-3 Existing-data migration
- CFS-3.1 Analyze File rows AND the physical public/private filesystem.
- CFS-3.2 Group by physical file_url/object identity, not blindly by File row.
- CFS-3.3 Detect: duplicate filenames, shared file_urls, shared content hashes,
  conflicting file_urls, missing physical files, orphan physical files, remote URLs,
  corrupt/inconsistent metadata (9 classes incl. healthy).
- CFS-3.4 Healthy canonical legacy URLs are NEVER mass-renamed (FROZEN).
- CFS-3.5 Conflicts enter a conflict queue; never destructively auto-changed.
- CFS-3.6 Historical references keep working; unavoidable URL changes require
  `Cloud File URL Alias` (302) + exact parent Attach/Attach Image/image_field
  consistency + audit row.
- CFS-3.7 ALYF legacy fork rows adopted in place (no re-upload), `legacy_unverified`
  until hash-verified.

## CFS-4 Migration engine
- CFS-4.1 Migration Campaign / Batch / Object / File Ref / Conflict doctypes.
- CFS-4.2 Resumable, idempotent, pause/resume, stop-after-batch, bounded retry,
  throttled (bandwidth token bucket).
- CFS-4.3 Checksum/size verification; separate UPLOAD, VERIFY, CLEANUP phases; UPLOAD
  and VERIFY contain no local deletion path (lintable).
- CFS-4.4 Dedicated `cloud_migration` RQ queue; normal ERP workers unaffected.
- CFS-4.5 Batched jobs — default `migration_batch_size` = 1000 (campaign-overridable);
  never ~1.2M individual RQ jobs.
- CFS-4.6 MariaDB rows are the only source of truth; Redis is a dispatch hint.
- CFS-4.7 Cleanup only after hard verification gates (operator approval + mode gate +
  per-object fresh remote verification + local re-stat/re-hash) → quarantine with
  DB-keyed TTL purge.
- CFS-4.8 Migration Capacity Preflight (measured during the 100k rehearsal, projected to
  1.2M rows) must pass before a production campaign may start.

## CFS-5 Storage model (Cloud Storage Object owns)
provider, bucket, object key, visibility, content hash (MD5), SHA256, size (Long Int),
MIME, ETag, version ID, source path, storage status, timestamps, retry/error info,
refcount-equivalent (derived). Statuses (FROZEN, lowercase): pending_upload, uploaded,
verified, failed, orphaned, pending_delete, deleted, legacy_unverified. Public API
(FROZEN): `ensure_cso()`, `adopt_references()`, `set_cso_status()`, `engine.verify()`.

## CFS-6 Frappe integration
- CFS-6.1 Native hooks preferred: `write_file` (BOTH call conventions),
  `before_write_file`, `delete_file_data_content`.
- CFS-6.2 Minimal `CloudFile(File)` subclass only for what hooks cannot cover; every
  overridden method signature-compatible with supported v15 revisions.
- CFS-6.3 India Compliance compatibility mandatory, incl. `get_content(encodings=[])`
  raw bytes and FileNotFoundError-only-on-true-absence.
- CFS-6.4 The DFP `get_content` failure class must not recur (superset signature, no
  exception swallowing, no permission checks in data accessors).
- CFS-6.5 `file_url` stays canonical `/files/...` | `/private/files/...`;
  `content_hash` stays populated.

## CFS-7 Backup management
- CFS-7.1 Live attachment storage and backup storage are separate concerns; separate
  backup bucket/prefix; live files stay immediately retrievable.
- CFS-7.2 Post-cutover, regular Frappe backups exclude public/private attachment
  archives by default (DB+config only; file tarballs opt-in).
- CFS-7.3 Configurable DB/config backup policy from Desk; hourly/daily frequencies with
  per-frequency prefixes and retention.
- CFS-7.4 Retention controls: hot retention days, total retention days, archive storage
  class, delete age, noncurrent-version retention.
- CFS-7.5 Glacier economics validated before generating lifecycle policies (90d/180d
  minimums, per-object overhead, retrieval cost/latency warnings).
- CFS-7.6 Live ERP files never silently placed in asynchronous-retrieval classes
  (structural assert: STANDARD/STANDARD_IA/INTELLIGENT_TIERING only).
- CFS-7.7 Backup uploads verified (SHA256) before Success; artifacts never deleted on
  verification failure; dumps forced fresh (`force=True` + mtime assert).
- CFS-7.8 `restore_window_days` ≥ coupling with object grace + attachment-bucket
  versioning so DB restore points always have matching object bytes.

## CFS-8 UI
- CFS-8.1 Cloud Storage Settings singleton; Migration dashboard; Storage health
  dashboard (cloud-managed vs operational bytes); Backup/lifecycle UI.
- CFS-8.2 The 12 mission buttons: Test Connection, Analyze Storage, Build Migration
  Plan, Start Migration, Pause, Resume, Stop After Current Batch, Retry Failed, Verify,
  Reconcile, Delete Verified Local Copies, Export Migration Report — all thin wrappers
  over `migration/api.py`, server-side role- and state-revalidated against the frozen
  campaign state machine.
- CFS-8.3 Destructive actions: hard server-side validation, System Manager +
  type-to-confirm literal phrases, audited.

## CFS-9 Safety
No deletion before verification; no hidden raw SQL bypassing invariants (three
sanctioned audited sites only); no public-read ACL requirement; IAM role/default chain
preferred; SSE/KMS support; audit trail; no secrets committed; idempotent retries;
crash/reboot-recoverable migration; no production deployment or production data
manipulation from this development environment.

## CFS-10 Testing
The full mission test matrix (public/private, same/diff name×bytes, shared-object
delete, last-reference delete, duplicate/amend/copy, Attach, Attach Image, Item image,
email attachment, Data Import, Prepared Report, Repost Item Valuation, ZIP,
image/thumbnail, Unicode, large files, API upload, India Compliance raw-bytes contract,
S3 outage, Redis/worker restart, migration interruption, retry, checksum failure,
pause/resume, cleanup gate, legacy URL, private permission enforcement) mapped 1:1 to
named tests (T-*) plus the C1–C19 contract module as a merge gate. Release-scale
rehearsal = 100,000 synthetic files.

## CFS-11 Compatibility
Strict version-15 branch for v1; compatibility matrix {floor v15.16.0, bench v15.93.0,
version-15 tip}; no v16 claims until a separate branch + tests exist; MariaDB-only for
v1 (documented).

## CFS-12 Deliverables
Production code, migrations/patches, tests, CI, architecture doc, ADRs, threat model,
migration/backup-restore/rollback/deployment runbooks, performance-testing procedure,
release checklist, supported-version matrix, ALYF-vs-DFP-vs-final source comparison,
RELEASE_EVIDENCE.md.

## Frozen decisions (FD)
- FD-1 P0 = PASS (ops-only `.pth` repair; evidence in PLAN.md §A).
- FD-2 Release rehearsal scale = 100,000 files; default batch size 1000; production
  target ~1.2M rows/~100GB.
- FD-3 Healthy canonical URLs never mass-renamed.
- FD-4 Content-addressed physical identity (CFS-2.2 key scheme).
- FD-5 Live objects synchronously retrievable; Glacier only for backup/archive.
- FD-6 Single private live bucket, `pub/`/`prv/` prefixes, no public-read ACLs.
- FD-7 Public HTML edge = Policy B (forced attachment) — deterministic, tested.
- FD-8 Frappe floor v15.16.0.
- FD-9 `s3_key_generator` hook NOT honored (loud deprecation detector); content-addressed
  keys are non-negotiable. (Supersedes the ALYF hook contract.)

## Superseded / rejected draft items
The pre-P0.5 draft requirement "object keys customizable via s3_key_generator (contract
preserved)" is REJECTED (conflicts with CFS-2.2/FD-9; it was never a mission
requirement). Postgres support is NOT a v1 requirement (CFS-11).
