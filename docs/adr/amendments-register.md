# Binding Amendments Register (A1–A36) — FROZEN at P0.5

Every amendment below is BINDING and overrides any conflicting text in `docs/design/*`.
Origin: two adversarial review rounds (round 1: `docs/review/{frappe-reality,safety,
completeness}.md`; final round: `docs/review/final-*.md`) — every finding was confirmed
against real code before being bound here. Each amendment names its implementing phase
and its regression test.

## Round 1 — durability spine (A1–A8)

- **A1 URL-only File creation** *(P2)* — Cloud resolution must not depend solely on the
  `cloud_storage_object` link: core creates Files from `file_url` alone (amend/copy
  `document.py:446-464`, `attach_files_to_document` `file/utils.py:363-374`,
  `file_manager.save_file` `file_manager.py:161-190`) before any link exists, and
  `before_insert` re-reads bytes. `CloudFile._resolve_cso()` fallback chain: own link →
  sibling File with same `file_url`+link → sibling with same `content_hash`+`is_private`
  → `frappe.local` stash written by `_write_file_legacy` keyed by content_hash. Used by
  `_is_cloud_backed`, `_read_bytes`, `exists_on_disk`, `validate_file_on_disk`,
  `get_full_path` (and serving — see A35). S3_ONLY tests for all three call sites are in
  the P2 exit gate; P2 implements all four modes at the storage layer.
- **A2 write_file persistence** *(P2)* — When `not file.is_new()` (overwrite/optimize —
  gst_return_log.py:103 never calls `.save()` after `save_file`), the hook itself
  persists `cloud_storage_object`/`content_hash`/`file_size` via
  `db_set(update_modified=False)`; the old CSO is never decremented before the new link
  is durable. Test: `save_file(content=…, overwrite=True)` with NO following `.save()`
  must repoint the DB row. Extension: URL-siblings sharing the mutated `file_url` are
  relinked in the same transaction.
- **A3 write-back commit discipline** *(P2)* — `after_request`/`after_job` run AFTER the
  framework's final commit/rollback and the request path swallows hook exceptions.
  `flush_request`/`flush_job` wrap their body in try/except + `frappe.log_error` and end
  with an explicit `frappe.db.commit()`; skip when the enclosing transaction rolled
  back. Test asserts persistence from a fresh connection.
- **A4 write-through tracking + local re-check** *(P2/P5)* — `get_full_path()` registers
  write-back tracking for EVERY path handed out on a cloud-backed File (canonical local
  paths included, mtime/size recorded at hand-out); migration CLEANUP re-stats (on drift
  re-hashes) the LOCAL file against the CSO hash immediately before quarantine — drift ⇒
  `cleanup_blocked` conflict.
- **A5 backup verification** *(P6)* — Explicit `TransferConfig`;
  `head_object(ChecksumMode="ENABLED")`; artifacts above the multipart threshold verified
  by ContentLength + streamed re-GET SHA256; **never delete the remote artifact on
  verification failure** — keep it, mark Failed, alert.
- **A6 GC locking recount** *(P2)* — The pre-delete recount is a **locking** read
  (`for_update`) with a commit per object (MariaDB REPEATABLE READ serves stale
  non-locking counts). ADR-4's "serialized by row lock" over-claim corrected: deferred GC
  + locking recount is the actual protection. GC excludes CSOs referenced by migration
  objects of non-terminal campaigns (see A14).
- **A7 frozen CSO interface** *(P2↔P5 contract)* — Statuses (lowercase):
  `pending_upload uploaded verified failed orphaned pending_delete deleted
  legacy_unverified`. Columns: `content_sha256 / content_hash_md5 / file_size / s3_key`.
  Public API: `ensure_cso()`, `adopt_references()`, `set_cso_status()`,
  `engine.verify()` **— extended to five by S2 with `adopt_legacy_cso()`; read S2 before
  concluding this API has four functions.** The migration engine imports these — never
  inlines SQL against CSO. P5 entry gate = interface-conformance test +
  migration-created-CSO-servable test.
- **A8 website_404 negative cache** *(P3)* — Production-only, invisible under
  developer_mode. `PublicFileRenderer` sets `frappe.local.no_cache = True` for
  `files/*`; `write_file`, `handle_is_private_changed`, and the VERIFY link transaction
  invalidate the `website_404` entry; regression test runs with website cache forced on.

## Round 1 — major fixes (A9–A24)

- **A9 campaign state machine frozen** *(P5/P7)* — `Cloud Migration Campaign.status` =
  `Draft / Analyzing / Analyzed / Planned / Running / Paused / Stopping / Stopped /
  Cleanup Running / Completed / Failed` (migration design §5.1 verbatim). Desk buttons validate against it;
  **Delete Verified Local Copies = approve_cleanup + start_cleanup** (two audited
  actions, mode-gated, per-object server recompute); `api/admin.py` contains only thin
  `frappe.only_for` wrappers delegating to `migration/api.py` (single validation path
  shared with the CLI).
- **A10 one legacy endpoint** *(P3/P8)* —
  `cloud_file_storage.api.compat.legacy_generate_file` everywhere; the external-install
  compat patch does NOT mass-rewrite `tabFile.file_url`; the adoption campaign is the
  canonicalization layer.
  **Amended (16.0.x / 15.0.x):** the `override_whitelisted_methods` remap that kept fork-era
  URLs resolving is no longer shipped -- the Frappe Cloud marketplace audit rejects any
  override of another app's whitelisted method, and a published listing was judged worth more
  than an automatic remap. The endpoint is unchanged and still whitelisted; an operator can
  reinstate the remap from their own app. The consequence is that on an adopted site those
  URLs 404 until the campaign canonicalises them, so the campaign is now load-bearing rather
  than merely tidy. Enforced by `test_compat.test_this_app_registers_no_whitelisted_method_override`.
- **A11 aliases consumed** *(P3)* — `Cloud File URL Alias` ships in P3;
  `serving/private.py` consults it at the miss (before the Forbidden/NotFound outcome,
  re-running the permission gate on the resolved target) and `PublicFileRenderer` before
  declining; redirects are **302** (never 301/307); `old_url` length 500 + `url_hash`
  Data(40) unique index. Test T-ALIAS.
- **A12 settings schema frozen** *(P2)* — Fields: `bucket`, `region`, `endpoint_url`,
  `addressing_style`, `key_prefix`, `use_default_credential_chain`(1), `access_key_id`,
  `secret_access_key`(Password), `sse_mode`(default SSE-S3), `kms_key_id`,
  `storage_class`(sync-retrieval only, hard assert), `operation_mode`,
  `fail_insert_on_s3_error`, `cdn_base_url`, `public_presign_ttl` 3600,
  `private_presign_ttl` 300, `inline_mimetype_prefixes`, `audit_public_fallback`,
  `cache_max_size_mb` 5120, `cache_ttl_hours` 72, `ignored_doctypes`,
  `multipart_threshold_mb` 64, `multipart_chunksize_mb` 16, `transfer_max_concurrency` 4,
  `object_delete_grace_days` 7 **[SUPERSEDED → 30, see S1]**, `restore_window_days` 30 (A27),
  `migration_batch_size` 1000, `migration_parallelism` 2,
  `migration_bandwidth_limit_mbps` 0. NO `public_bucket` (single-bucket invariant).
  Validator: `verify_strategy_cutover_mb <= multipart_threshold_mb`.
- **A13 s3_object_key** *(P1/P5)* — The custom field IS created by install.py on every
  install (read-only, documented deprecated); analyzer/adoption depend on it and guard
  with `has_column`.
- **A14 migration-owned CSOs** *(P2/P5)* — CSOs linked from migration objects of
  non-terminal campaigns count as live references in GC/orphan sweeps. Supporting index
  on `Cloud Migration Object.cloud_storage_object`.
- **A15 adoption honesty** *(P5)* — Adoption writes `legacy_unverified` (never `verified`
  without a hash check); `content_sha256`/`content_hash_md5` non-reqd with validate()
  requiring them unless `legacy_unverified`; ETag→content_hash backfill only when no `-`
  AND SSE absent/AES256; a backfill job promotes to `verified` after streamed re-GET
  hash. `ensure_cso` resurrection restores the PRIOR status (stored on entering
  pending_delete) — never upgrades.
- **A16 is_private flip completeness** *(P2)* — Relink ALL content-hash siblings (the
  rows core's `update_existing_file_docs` flips) to the new-visibility CSO in the same
  transaction; never fast-track the old CSO to `pending_delete` outside the
  derived-refcount path; the serving allow-list includes `pending_delete`.
- **A17 migration scope** *(P5)* — SCAN_DB joins `attached_to_doctype` against the
  ignored-doctypes list → `Skipped`; CLEANUP hard-refuses objects whose refs include an
  ignored parent (LOCAL_OPERATIONAL: Data Import / Prepared Report / Package Import).
  *Thumbnail clause superseded:* thumbnails are handled per the derived-object policy
  (PLAN.md §A) — migrated/regenerated as derived objects after source verification;
  never deleted before availability is guaranteed.
- **A18 legacy public URLs** *(P5)* — `adopt_remote_https_rows` defaults ON for URLs
  whose host/bucket matches the configured bucket; adoption canonicalizes + aliases +
  updates parent fields; Block-Public-Access posture is a **post-adoption** gate.
  (Owner sign-off required at production cutover — PLAN.md §H.)
- **A19 privacy_mismatch = Blocker** *(P5/P3)* — Operator decides true visibility before
  upload; `PublicFileRenderer.can_render` requires `is_private = 0`.
- **A20 lifecycle policy** *(P6)* — `Filter.Prefix` on EVERY generated rule;
  merge-don't-replace (preserve rules whose ID doesn't start `cfs-`); confirm dialog
  lists dropped rules; per-frequency prefixes (hourly/, daily/) with separate expiration
  rules; `hot_retention_days` drives the archive transition; economics validator covers
  hourly-frequency × delete_after_days.
- **A21 compat-patch bootstrap** *(P8)* — External 0.2.x sites cannot run migrate after
  the rename (installed_apps names the vanished module). Mandatory pre-migrate operator
  procedure + `bench cfs-adopt-legacy-install` out-of-band command fix
  apps.txt/installed_apps/Module Def first; the `[pre_model_sync]` patch idempotently
  finishes. Acceptance = a REAL `bench migrate` on a synthetic legacy site.
- **A22 contract tests on all refs** *(P1/P2)* — C1–C19 run storage-mocked in the 3-ref
  matrix job AND live in the MinIO job; junit completeness asserted per ref.
- **A23 test-matrix additions** — T-RECON, T-RELINK, T-ADOPT, T-MODEGATE, T-PRIVFLIP,
  T-CLI, T-ALIAS; T-DEL2 = last-reference delete ⇒ `pending_delete` + GC-forced physical
  delete.
- **A24 sundries** — Single private bucket (`public_bucket` dropped); renderer resolves
  via `frappe.local.request.path` + `/{endpoint}.html` (see FD-7 Policy B for the HTML
  edge); dev StaticDataMiddleware shim patches the INSTANCE's exports loader (returns
  not-found → werkzeug falls through) or documented dev-404 degradation — P3 spike
  decides; private local-copy path calls `send_private_file` directly (no double
  access-log/scan); byte counters Long Int in ALL doctypes; every Select has an explicit
  default or leading `\n` (lint); `tabFile.content_hash` index created outside locked
  transitions (add_index commits; property-setter quirk documented); analyzer
  `ref_count` derived by GROUP BY in CLASSIFY (not incremental upsert); MD5 sibling
  adoption confirmed by size + SHA256 re-hash; audit log honestly append-only-by-
  convention (optional hash chain); scheduler tick 60s; batch 1000 / cache 5120MB/72h /
  abort-MPU 7d / rehearsal 100k unified; `docs/runbooks/deployment.md` is the single
  home for the queue prerequisite; `s3_key_generator` deprecation detector in P3;
  `legacy_generate_file` uses core's any-one-readable gate with indistinguishable 403s.

## Final round — open blockers bound (A25–A36)

- **A25 tombstone revival** *(P2)* — `ensure_cso` upload gate is positive: upload unless
  status ∈ {uploaded, verified, legacy_unverified}; `deleted`/`orphaned` rows reset to
  `pending_upload` under lock (clear deletion_scheduled_at, keep audit fields). Test:
  delete → force GC → re-upload identical bytes → object present and servable.
- **A26 guarded VERIFY link** *(P5)* — The link UPDATE-JOIN adds
  `AND (f.cloud_storage_object IS NULL OR = %(cso)s) AND f.file_url = r.file_url AND
  (f.content_hash IS NULL OR = %(md5)s)`; ROW_COUNT compared to ref count; non-matching
  refs → `Skipped(superseded_by_runtime)` (never CleanupEligible) + audit row.
- **A27 restore-window coupling** *(P6)* — `restore_window_days` field;
  `object_delete_grace_days >= restore_window_days` validated; attachment-bucket
  versioning required/hard-warned with `NoncurrentVersionExpiration >= window`
  (test_connection checks); restore runbook verifies artifact SHA256 vs Backup Log
  before `bench restore`. Default window 30 days (owner-overridable).
- **A28 backup freshness** *(P6)* — `new_backup(..., force=True)`; assert returned dump
  mtime ≥ job start before logging Success.
- **A29 capacity preflight** *(P5)* — See ACCEPTANCE_GATES.md F4. Measured during the
  100k rehearsal; projection to 1.2M rows; production campaign start refused below
  thresholds; numbers recorded on the Campaign; plus `innodb_file_per_table` check and
  a verified recent DB backup requirement.
- **A30 S3 timeouts + breaker** *(P2)* — Request-path client:
  `Config(connect_timeout=3, read_timeout=15, retries={"max_attempts": 2, "mode":
  "standard"})`; separate long-timeout client for jobs; Redis-cached circuit breaker
  consulted by `before_write_file` and serving; after N consecutive transport failures
  DUAL_WRITE/S3_PRIMARY go straight to local and S3_ONLY fails fast typed.
- **A31 batch-finalize residue** *(P5)* — `_finalize_upload_batch` is a CAS
  `Uploading→Uploaded` firing only when COUNT(objects in {Pending, Uploading}) == 0;
  otherwise batch → Pending (attempts+1, bounded) for re-dispatch; VERIFY refuses
  batches with non-terminal upload objects; heartbeat during long transfers via boto
  progress callback.
- **A32 quarantine layout** *(P5)* — Quarantine path =
  `<trash>/<campaign>/<migration_object_name>` (collision-free); refuse rename if target
  exists; purge selects on `Cloud Migration Object.status='Quarantined' AND cleaned_at <
  now - ttl` (DB-keyed, never filesystem mtime).
- **A33 deterministic migration names** *(P5)* — `Cloud Migration Object.name =
  sha1(campaign‖url_hash)`; `File Ref.name = sha1(campaign‖file)` — removes
  autoname-hash PK collisions and makes upserts idempotent by construction.
- **A34 serving floor v15.16.0** *(P1/P3)* — `find_file_by_url` + `fid` first exist at
  v15.16.0; declared floor raised to v15.16.0; CI matrix `{v15.16.0, v15.93.0,
  version-15}`; `frappe-dependencies >=15.16.0,<16.0.0`; floor-ref private-serving test.
- **A35 serving uses the resolution chain** *(P3)* — `serving/private.py` and
  `PublicFileRenderer` resolve the object via the A1 chain across URL-sharing rows (not
  own-link only); public additionally requires `is_private=0` (A19).
- **A36 get_file_path bypass** *(P3/P4)* — Patch `frappe.utils.file_manager.get_file_path`
  AND (guarded on IC installed) `india_compliance.gst_india.utils.get_file_path`
  (import-bound copy) to resolve the File row and return the materialized
  `get_full_path()`; also covers erpnext EDI readers. Test: upload_gstr after cleanup.

## Post-freeze supersessions (S1–)

Entries here override a frozen amendment where a higher-precedence document contradicts it.
Precedence is `docs/PLAN.md` > `docs/adr/` (INVARIANTS.md). Each records what changed and why, so
a later phase implementing the amendment uses the right value.

- **S1 supersedes A12's `object_delete_grace_days` default: 7 → 30** *(ruled during P2,
  implemented in P2, consumed by P6)* — A27 requires
  `object_delete_grace_days >= restore_window_days`, and A12 froze the pair at 7 and 30, which
  cannot both hold: A27's validator was unshippable and a fresh install started in the state
  A27 forbids. PLAN.md §H item 1 records the owner's adopted default as "Attachment recovery
  window — 30 days + attachment-bucket versioning ON", and `object_delete_grace_days` **is**
  that recovery window — it is the delay before a physical S3 delete. A12's 7 predates and
  contradicts an owner decision that PLAN §H carries, and PLAN outranks this register, so the
  shipped default is 30. **P6 implements A27's validator against 30, not 7.** Every other A12
  value is unchanged. Shipped with a guarded patch that raises the value only on sites whose
  grace is currently shorter than their own restore window, so an operator-chosen longer grace
  is left alone.

- **S2 extends A7's frozen public API with `adopt_legacy_cso()`** *(found by the P5 entry
  gate, ruled and approved by the orchestrator, implemented in P5)* — A7 freezes
  `ensure_cso()` as the constructor, and `ensure_cso()` derives `s3_key` from a
  `content_sha256` it requires. A15/A18 adoption needs a Cloud Storage Object for bytes that
  are **already in the bucket, at the key the 0.2.x fork chose**, and that have **no SHA256
  until a streamed re-GET produces one** — A15's whole point is that hashing those bytes is
  deferred. The two cannot both hold through the frozen constructor: a content-addressed key
  would point at nothing, and requiring the hash would mean re-reading 100GB before an
  adoption that is defined as moving no bytes.

  **Resolution:** `storage/objects.adopt_legacy_cso()` — a fifth function in the same
  choke-point module, taking an explicit `s3_key`, idempotent on that column's existing
  unique index.

  **Why this and not the alternatives.** (a) A7's intent is the *boundary* — "the migration
  engine imports these, never inlines SQL against CSO" — and a function inside the choke
  point honours that completely; what A7 freezes is where CSO mutation may happen, not how
  many functions live behind it. (b) Adding an override parameter to `ensure_cso()` was
  rejected: it would let any caller bypass the hash requirement and would demote A15's
  "adoption writes `legacy_unverified`, never `verified` without a hash check" to caller
  discipline. `adopt_legacy_cso` hardcodes the status and takes **no argument that could
  change it**, so the dishonest case is unreachable by construction rather than discouraged
  by convention. (c) Letting `migration/adoption.py` insert the row itself is ORM rather than
  raw SQL and so technically legal, but would create a second place in the codebase that can
  mint a Cloud Storage Object.

  **What is preserved:** INVARIANTS.md invariant 4 (migration mutates a CSO only through
  `storage.objects`); A7's status set, columns and the four original functions, all
  unchanged; A15's honesty rule, now structural. P2/P3 already assumed adopted rows exist —
  `api/compat._resolve_cso` resolves a Cloud Storage Object *by a fork key* — so this
  completes the design rather than altering it. Idempotency on the unique `s3_key` index
  follows A33's principle of making upserts idempotent by construction.

  **Tests:** `tests/test_migration_entry_gate.py::TestAdoptionCannotBeExpressedByEnsureCso`
  asserts the gap and the resolution;
  `tests/test_migration_adoption.py::TestAdoptionHonestyIsStructural` asserts that no call
  can produce any other status and that `verified` requires a real hash.

## Recorded residual risks (non-blocking, tracked)
CLEANUP pre-delete HEAD strategy honors the 64MB cutover (size-split like VERIFY);
attachment-bucket AbortIncompleteMultipartUpload is a deployment-checklist item +
reconcile monitors orphaned MPUs; backup catch-up after >24h S3 outage re-uploads the
last failed artifact when connectivity returns; legacy NULL-content_hash rows rely on
reconcile for orphan cost control; URL-only File insert costs two S3 GETs on amend
(accepted, documented); GC/number-card queries paginated/cached; email inline images +
`pdf_to_base64` + wkhtmltopdf public images degrade under S3_ONLY (documented exception
list — S3_PRIMARY_LOCAL_FALLBACK is the recommended production mode).
