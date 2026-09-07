> **BINDING RECONCILIATION HEADER (P0.5)** — This design document is source material.
> Where it conflicts with `docs/PLAN.md`, `docs/adr/` (incl. the amendments register
> A1–A36), or this header, THOSE WIN. Known supersessions applied to this file inline
> where safe; the authoritative frozen values are:
> multipart threshold/chunk/concurrency = 64MB/16MB/4; single private bucket (no
> `public_bucket`); default `migration_batch_size` = 1000; alias redirects are 302;
> scheduler tick = 60s; CSO statuses are the lowercase set `pending_upload uploaded
> verified failed orphaned pending_delete deleted legacy_unverified`; CSO columns
> `content_sha256 / content_hash_md5 / file_size / s3_key`; CSO public API = `ensure_cso()
> / adopt_references() / set_cso_status() / engine.verify()`; campaign state machine =
> migration-engine.md §5.1 verbatim; legacy endpoint =
> `cloud_file_storage.api.compat.legacy_generate_file`; frappe floor = v15.16.0; release
> rehearsal = 100,000 files; byte counters = Long Int everywhere; deterministic
> sha1-derived names for Migration Object/File Ref; thumbnails = cloud-managed derived
> objects of the source CSO (supersedes ADR-M17/A17 thumbnail clauses); Data Import /
> Prepared Report / Package Import = LOCAL_OPERATIONAL; HTML/htm/svg/xml = forced
> attachment (Policy B); `new_backup(force=True)`; lifecycle rules prefix-scoped +
> merge-not-replace; external compat patch does NOT mass-rewrite file_url (A10).

All research absorbed and framework details verified (`{app}.commands` CLI discovery at `frappe/utils/bench_helper.py:72-86`, `frappe.db.add_index` at `database.py:1342`, s3transfer 0.10/boto3 1.34.162 supports `ChecksumAlgorithm` in upload ExtraArgs). Here is the design document.

---

# Cloud File Storage — Existing-Data Analyzer & Migration Engine Design

Scope: analyzer, grouping model, migration state machines, execution engine, phases, conflict queue, legacy adoption, observability, reconcile, CLI, runtime math. Assumes the sibling runtime layer provides: `Cloud Storage Object` (CSO) DocType (content-addressed key, refcount, provider/bucket/key/visibility/md5/sha256/size/mime/etag/version_id/status), `File.cloud_storage_object` Link custom field (indexed), operation-mode singleton `Cloud Storage Settings` with modes LOCAL_ONLY / DUAL_WRITE / S3_PRIMARY_LOCAL_FALLBACK / S3_ONLY, an S3 client provider, and serving-layer redirect that keeps `file_url` canonical (contract #9, content-consumers-contract.md §5).

## 0. ADR Summary (each decision + one-line rationale)

| # | Decision | Rationale |
|---|---|---|
| ADR-M1 | Analysis snapshot **is** the migration plan: analyzer writes `Cloud Migration Object` + `Cloud Migration File Ref` rows directly; no separate "analysis item" table | Avoids a second 1.2M-row copy; the classified object row is exactly what UPLOAD needs (repo-audit.md §6 shows the cost of re-deriving work). |
| ADR-M2 | Flat linked DocTypes, **no child tables** for batches/objects/refs | Child rows load wholesale with the parent (`get_doc`), can't carry independent composite indexes or CAS updates; 1.2M child rows under one parent is unusable. |
| ADR-M3 | Migration unit = **physical object** (identity: normalized `file_url` for local files, `s3_object_key` for legacy fork rows), N File rows attached via ref table | Shared `file_url` is a supported core pattern (frappe-file-core.md §5: dedup reuses `file_url`, `find_file_by_url` returns first readable of many, `utils.py:430-443`). |
| ADR-M4 | MariaDB rows are the only source of truth; RQ `job_id` dedup is merely an anti-double-enqueue hint; every job re-derives work from DB with CAS claims | Redis queue has no persistence and no auto-requeue (frappe-jobs-backups.md §2.5, §8: killed jobs are never retried by RQ; frappe never sets `retries_left`). |
| ADR-M5 | Hybrid dispatch: scheduler cron thin-dispatcher (default queue, O(ms)) + batch-job self-chaining | Scheduler cannot target a custom queue (frappe-jobs-backups.md §4: `get_queue_name()` returns only long/default, `scheduled_job_type.py:181-182`); self-chaining gives continuity between ≥240s scheduler ticks; the tick is the crash-recovery watchdog. |
| ADR-M6 | Commit cadence: one `frappe.db.commit()` per object state transition (2/object in UPLOAD) | At ~15-25 obj/s/worker that is ≤50 commits/s — trivial for MariaDB — and bounds crash re-work to exactly one object; the fork's per-file commit was only a problem because it hijacked the *caller's* transaction inside `after_insert` (repo-audit.md §3 step 8); worker jobs own their transaction. |
| ADR-M7 | Verification = S3 additional checksum `ChecksumSHA256` compare for single-part uploads; streamed re-GET SHA256 for files ≥ multipart threshold | Multipart ETag ≠ MD5 and composite `ChecksumSHA256` ≠ full-object SHA256 on boto3 1.34.162 (no CRC64NVME full-object mode); with avg 83KB the ≥64MB tail is a few hundred files, so re-GET is cheap and is the strongest *independent* check. |
| ADR-M8 | `multipart_threshold = 64MB` in `TransferConfig` | Makes ~99.9% of the corpus single-part ⇒ `ChecksumSHA256` is the exact full-object SHA256 and HEAD-verify is one request. |
| ADR-M9 | Cleanup = **quarantine by `os.rename`** into `private/cloud_migration_trash/<campaign>/`, TTL purge (default 14 days) | Same-filesystem rename is atomic, instantly reversible, costs zero extra space; space is reclaimed at purge; direct unlink is offered but not default. |
| ADR-M10 | `File.cloud_storage_object` is linked only inside the VERIFY success transaction, via one sanctioned raw `UPDATE tabFile` (`update_modified=False`, no doc events), audit-logged | Linking flips serving to S3; doing it pre-verification would serve unverified objects. Raw SQL is safe here because the column is additive and touches no core invariant; going through `File.save()` would trigger `validate_file_on_disk` churn ×1.2M (frappe-file-core.md risk 1). |
| ADR-M11 | Analyzer never hashes file content; hashing (SHA256+MD5) happens once, streamed, during UPLOAD | Hashing 100GB twice is waste; classification needs only DB `content_hash` comparison, existence, and size; VERIFY compares the upload-time hash against S3 independently. |
| ADR-M12 | Healthy legacy URLs are **never rewritten**; URL change only for (a) legacy `/api/method/frappe_s3_attachment...` rows (they 404 after rename anyway — rename-and-bench-reality.md §3 table, `tabFile.file_url` ⚠ row) and (b) conflict relinks; both go through the alias store + parent-field updater | Contract #9/#18: stored `file_url` values live in business fields (`gst_return_log.py:87,104`, `stock_ledger.py:308-318`). |
| ADR-M13 | Campaign counters are atomic `SET x = x + 1` updates in the same transaction as the state change, reconciled per batch completion by one indexed `GROUP BY` | No `COUNT(*)` per render; drift self-heals at batch boundaries. |
| ADR-M14 | Add permanent index `tabFile.content_hash(32)` before any scan | Core queries content_hash on every insert and every delete with no index (frappe-file-core.md §5, risk 8: `file.py:688`, `:513`); the migration multiplies these lookups. |
| ADR-M15 | Campaign-scoped snapshot rows with an explicit purge command | ~1.2M object + ~1.2M ref rows ≈ 1.5–2.5GB InnoDB incl. indexes — a one-time, purgeable cost that buys full resumability and audit. |
| ADR-M16 | CLEANUP allowed only in modes S3_PRIMARY_LOCAL_FALLBACK / S3_ONLY, and only after explicit operator approval + per-object re-verification at deletion time | Deleting local copies under DUAL_WRITE violates the mode contract; "no local file deleted until its remote object is INDEPENDENTLY verified" is enforced twice. |
| ADR-M17 | **SUPERSEDED by ADR 0021** (thumbnails = derived objects, migrated/regenerated after source verification; `delete_thumbnails_on_cleanup` REMOVED for v1) | Thumbnails are derivable, always land in `public/` even for private files (frappe-file-core.md §8, `file.py:463`), and remote thumbnail URLs break `make_thumbnail`/`delete_file` (risk 11). |

## 1. Module / File Layout (inside renamed package)

```
cloud_file_storage/                              # app root
  cloud_file_storage/                            # python package (app_name)
    commands.py                                  # bench CLI (discovered via {app}.commands — bench_helper.py:75)
    migration/
      __init__.py
      api.py            # whitelisted endpoints + shared server-side validation (UI buttons & CLI call these)
      analyzer.py       # DB scan, FS walk, classification
      planner.py        # batch assignment
      engine.py         # dispatch_tick, run_upload_batch, run_verify_batch, claims, heartbeat, throttle
      verify.py         # per-object verification strategies
      cleanup.py        # quarantine/delete, TTL purge, thumbnail handling
      adoption.py       # legacy fork / remote-URL adoption into CSO
      conflicts.py      # conflict creation + resolution actions
      aliases.py        # URL alias store writer + parent Attach/Attach Image/image_field updater
      reconcile.py      # bucket ⇄ CSO ⇄ File three-way diff
      report.py         # progress snapshots, realtime publishing, CSV/JSONL export
      audit.py          # destructive-action audit writer
    cloud_file_storage/doctype/
      cloud_migration_campaign/
      cloud_migration_batch/
      cloud_migration_object/
      cloud_migration_file_ref/
      cloud_migration_conflict/
      cloud_file_url_alias/
      cloud_reconcile_run/
      cloud_storage_audit_log/
  docs/migration-deploy.md                       # deploy prerequisites (queue, workers, S3 lifecycle)
```

`hooks.py` additions (migration domain only):

```python
scheduler_events = {
    "cron": {
        "* * * * *": ["cloud_file_storage.migration.engine.dispatch_tick"],      # watchdog + dispatcher; O(ms) idle
        "0 3 * * *": ["cloud_file_storage.migration.cleanup.purge_expired_quarantine"],
        "30 2 * * 0": ["cloud_file_storage.migration.reconcile.scheduled_reconcile"],  # weekly drift check (opt-in via settings)
    }
}
```
`dispatch_tick` lands on the `default` queue (scheduler constraint, frappe-jobs-backups.md §4) and must return in milliseconds when no campaign is Running (one indexed SELECT).

## 2. DocType Schemas

All DocTypes: module `Cloud File Storage`, permissions **System Manager only** (read/write/create; no delete except Campaign after Completed), `track_changes: 0` on high-volume tables (Object/File Ref) to avoid 1.2M Version rows, `in_create: 1` hidden from quick entry. Byte counters use **Float** (DOUBLE — exact ≤2^53) because Frappe `Int` is INT(11) and overflows >2GB (dfp-audit.md §2.1 `remote_size_enabled` note).

### 2.1 `Cloud Migration Campaign` (autoname `format:CFS-CAMP-{####}`)

| fieldname | fieldtype | options / default | notes |
|---|---|---|---|
| title | Data | reqd | |
| status | Select | `Draft\nAnalyzing\nAnalyzed\nPlanned\nRunning\nPaused\nStopping\nStopped\nCleanup Running\nCompleted\nFailed` default `Draft`, search_index | |
| active_phase | Select | `\nSCAN_DB\nSCAN_FS\nCLASSIFY\nUPLOAD\nVERIFY\nCLEANUP` read_only | |
| control_flag | Select | `\nPAUSE\nSTOP` | polled per object by workers |
| include_public / include_private | Check | default 1 / 1 | scope |
| adopt_legacy_fork_rows | Check | default 1 | §8 |
| adopt_remote_https_rows | Check | default 1 (A18) | ON for URLs whose host/bucket matches the configured bucket; non-matching remote rows stay Skipped |
| dedupe_by_content_hash | Check | default 1 | reuse one CSO for identical bytes |
| batch_size | Int | default 1000 | objects per batch |
| parallelism | Int | default 2 | batches in flight = worker affinity count |
| bandwidth_limit_mbps | Int | default 0 (=off) | token-budget throttle |
| max_attempts | Int | default 5 | |
| retry_backoff_base_secs | Int | default 60 | delay = base × 2^(attempt-1), ±25% jitter, cap 3600 |
| multipart_threshold_mb | Int | default 64 | ADR-M8 |
| verify_strategy_cutover_mb | Int | default 64 | below: HEAD checksum; at/above: re-GET hash (ADR-M7) |
| cleanup_mode | Select | `Quarantine\nDirect Delete` default `Quarantine` | ADR-M9 |
| quarantine_ttl_days | Int | default 14 | |
| ~~delete_thumbnails_on_cleanup~~ | — | — | REMOVED (ADR 0021 supersedes ADR-M17) |
| cleanup_approved_by | Link | User, read_only | set only by `approve_cleanup` |
| cleanup_approved_at | Datetime | read_only | |
| scan_cursor_file | Data | read_only | keyset cursor (last `tabFile.name`) |
| fs_entries_seen | Int | read_only | progress only; FS walk restarts from zero (idempotent) |
| classify_step | Data | read_only | resumable classification step marker |
| total_files / total_objects | Int | read_only | |
| objects_pending / uploading / uploaded / verified / cleaned / failed / conflict / skipped / adopted / dedup_reused | Int | read_only, default 0 | counter family (ADR-M13) |
| bytes_total / bytes_uploaded / bytes_verified | Float | read_only | |
| started_at / completed_at | Datetime | read_only | |
| last_error | Small Text | read_only | |

Server rule: at most one campaign in {Analyzing, Running, Paused, Stopping, Cleanup Running} (validated in `before_save` + CAS on start).

### 2.2 `Cloud Migration Batch` (autoname `format:{campaign}-B{batch_no}`)

| fieldname | fieldtype | options / default |
|---|---|---|
| campaign | Link Cloud Migration Campaign, reqd, search_index |
| batch_no | Int, reqd |
| status | Select `Pending\nUploadDispatched\nUploading\nUploaded\nVerifyDispatched\nVerifying\nVerified\nFailed\nStalled` default `Pending`, search_index |
| object_count | Int |
| objects_done / objects_failed | Int default 0 |
| bytes_total / bytes_done | Float |
| attempts | Int default 0 |
| heartbeat_at | Datetime |
| worker_host | Data |
| rq_job_id | Data |
| last_error | Small Text |

`on_doctype_update`: `add_index("Cloud Migration Batch", ["campaign", "status"])`, `add_index(..., ["campaign", "batch_no"])`.

### 2.3 `Cloud Migration Object` (autoname `hash`; ~1.2M rows)

| fieldname | fieldtype | options / default |
|---|---|---|
| campaign | Link Cloud Migration Campaign, reqd, search_index |
| batch | Link Cloud Migration Batch, search_index |
| url_hash | Data(40), reqd — sha1 hex of identity key |
| identity_kind | Select `local_public\nlocal_private\nlegacy_fork_key\nremote_https\norphan_disk` |
| file_url | Small Text — normalized shared URL (or remote URL / legacy key display) |
| classification | Select `healthy_unique\nshared_url\nshared_content\nconflicting_url\nmissing_physical\norphan_physical\nremote_url\nlegacy_fork\ncorrupt_metadata` search_index |
| dup_filename | Check default 0 — informational duplicate-filename flag |
| status | Select `Pending\nUploading\nUploaded\nVerifying\nVerified\nDedupReused\nCleanupEligible\nQuarantined\nCleanedUp\nAdopted\nFailed\nConflict\nSkipped` default `Pending`, search_index |
| ref_count | Int default 0 |
| primary_file | Link File |
| disk_path | Small Text — absolute path at scan time |
| on_disk | Check default 0 |
| disk_size | Float |
| disk_mtime | Datetime |
| db_content_hash | Data(32) — consensus MD5 from File rows (null if absent/inconsistent) |
| sha256 | Data(64) — computed during UPLOAD |
| md5 | Data(32) — computed during UPLOAD |
| size_bytes | Float |
| is_private | Check |
| privacy_mismatch | Check default 0 — `is_private` column vs URL prefix disagreement |
| is_thumbnail | Check default 0 |
| parent_object | Link Cloud Migration Object — set on thumbnail rows |
| thumbnail_disk_path | Small Text — parent's expected thumbnail path |
| cloud_storage_object | Link Cloud Storage Object |
| attempt_count | Int default 0 |
| next_retry_at | Datetime |
| error_class | Data — e.g. `S3ClientError`, `FileVanished`, `ChecksumMismatch` |
| last_error | Small Text |
| uploaded_at / verified_at / cleaned_at | Datetime |
| conflict | Link Cloud Migration Conflict |
| legacy_bucket / legacy_key | Data / Small Text — for adoption rows |

`on_doctype_update`:
```python
frappe.db.add_unique("Cloud Migration Object", ["campaign", "url_hash"], constraint_name="uq_cmo_campaign_urlhash")
frappe.db.add_index("Cloud Migration Object", ["campaign", "status"])
frappe.db.add_index("Cloud Migration Object", ["batch", "status"])
frappe.db.add_index("Cloud Migration Object", ["campaign", "classification"])
frappe.db.add_index("Cloud Migration Object", ["sha256(64)"])
```

### 2.4 `Cloud Migration File Ref` (autoname `hash`; ~1.2M rows)

| fieldname | fieldtype |
|---|---|
| campaign | Link Cloud Migration Campaign, reqd |
| migration_object | Link Cloud Migration Object, reqd, search_index |
| file | Link File, reqd |
| file_name | Data |
| content_hash | Data(32) |
| is_private | Check |
| attached_to_doctype / attached_to_name / attached_to_field | Data |
| linked | Check default 0 — `File.cloud_storage_object` written |
| url_rewritten | Check default 0 |

`on_doctype_update`: unique `(campaign, file)`; index `(migration_object)`.

Why a ref table instead of JSON on the object: linking at VERIFY is one indexed `UPDATE tabFile JOIN` per batch, conflict resolution needs per-File actions, and `GROUP BY migration_object` powers ref_count reconciliation — none of which JSON supports.

### 2.5 `Cloud Migration Conflict` (autoname `format:CFS-CONF-{#####}`)

| fieldname | fieldtype |
|---|---|
| campaign | Link, reqd, search_index |
| migration_object | Link Cloud Migration Object |
| conflict_type | Select `conflicting_url\nchecksum_mismatch\nchecksum_unstable\nmissing_physical\ncorrupt_metadata\nambiguous_privacy\nupload_failed\nverify_failed\nadoption_failed\ncleanup_blocked` search_index |
| status | Select `Open\nRetrying\nSkipped\nResolved` default `Open`, search_index |
| severity | Select `Blocker\nWarning` |
| details | Code (JSON: file rows, hashes, errors, sizes) |
| resolution_action | Data — last action taken |
| resolved_by | Link User |
| resolved_at | Datetime |

### 2.6 `Cloud File URL Alias` (autoname `hash`)

| fieldname | fieldtype |
|---|---|
| old_url | Data, reqd, unique (index `old_url(191)`) |
| new_url | Data, reqd |
| file | Link File |
| campaign | Link Cloud Migration Campaign |
| reason | Select `legacy_adoption\nconflict_relink\nprivacy_fix` |
| active | Check default 1 |

Consumed by the runtime serving layer: on `/private/files/*` miss and on the private-serving interceptor, look up `old_url` and 302-redirect (A11: 302, never 301/307) to `new_url` (public `/files/*` under nginx cannot reach Python — frappe-file-core.md §4 — so a public rename additionally leaves a filesystem **symlink** old→new until quarantine purge; the aliases writer creates it).

### 2.7 `Cloud Reconcile Run` (autoname `format:CFS-RECON-{####}`)
`status` (Select Running\nCompleted\nFailed), `bucket`, `prefix`, counters: `remote_objects`, `cso_rows`, `orphan_remote`, `missing_remote`, `size_mismatch`, `unreferenced_cso`, `report_file` (Attach — private JSONL report), `started_at`, `completed_at`. Drift detail lives in the JSONL file, not rows (unbounded volume).

### 2.8 `Cloud Storage Audit Log` (autoname `hash`, no write/delete perms for anyone; created via `frappe.get_doc(...).insert(ignore_permissions=True)` only from `audit.py`)
`action` (Select `local_quarantine\nlocal_delete\nquarantine_purge\nurl_rewrite\nfile_link\nremote_adopt\nconflict_action\ncampaign_transition`), `actor` (Data — session user or `scheduler`), `campaign`, `migration_object`, `file`, `details` (Code JSON), plus standard `creation`. Index `(campaign, action)`. Every destructive/mutating action writes one row in the same transaction.

## 3. Analyzer

### 3.1 Preflight: indexes (run in `start_analysis`, idempotent)

```python
def ensure_analysis_indexes():
    frappe.db.add_index("File", ["content_hash(32)"], index_name="content_hash_index")  # permanent — fixes frappe-file-core.md risk 8
    # file_url(100) already exists (file.py:838-840); prefix index serves equality with row re-check — sufficient.
    # s3_object_key index exists from the fork (install.py:65-79).
```
`add_index` is `ALGORITHM=INPLACE` on MariaDB — no table lock; still documented as "run in a quiet window" (one-time ~1.2M-row build).

### 3.2 Phase SCAN_DB — keyset scan of `tabFile`

Runs as one job on `cloud_migration` queue (`job_id=f"cfs::{campaign}::analyze"`). Raw SQL, no `get_doc`:

```sql
SELECT name, file_name, file_url, file_size, is_private, is_folder, folder,
       content_hash, s3_object_key, attached_to_doctype, attached_to_name,
       attached_to_field, thumbnail_url
FROM `tabFile`
WHERE name > %(cursor)s
ORDER BY name
LIMIT %(page_size)s          -- 2000
```

Per page (all in one transaction, then `frappe.db.commit()` + cursor update — the checkpoint):
1. Skip `is_folder=1`.
2. Normalize `file_url`: strip own-site absolute prefix, unquote, trim. Derive `identity_kind`:
   - starts `/files/` → `local_public`; `/private/files/` → `local_private`
   - starts `/api/method/frappe_s3_attachment.` (or new dotted path) **or** `s3_object_key` set → `legacy_fork_key`, identity = `s3legacy::{s3_object_key}` (multiple URL spellings share one key)
   - starts `http(s)://` → `remote_https`
   - empty / any other shape → `corrupt_metadata` (Conflict, no object work)
3. Identity string: `f"{identity_kind}::{normalized_url_or_key}"` → `url_hash = sha1(identity)`.
4. Group rows in Python per page; upsert objects with
   `INSERT ... ON DUPLICATE KEY UPDATE ref_count = ref_count + VALUES(ref_count)` against unique `(campaign, url_hash)`; bulk `INSERT IGNORE` file refs against unique `(campaign, file)` (re-run safe).
5. `db_content_hash` merge rule: set on first ref; on later ref with a different non-null hash → set `db_content_hash=NULL` and flag pending `conflicting_url` (finalized in CLASSIFY).
6. `privacy_mismatch = (is_private column) != (URL startswith /private)` (core derives is_private from URL — `file.py:782`).
7. If `thumbnail_url` set → insert a thumbnail object row (`is_thumbnail=1`, `parent_object` link) keyed by the thumbnail URL.
8. Update `campaign.scan_cursor_file = last name`; atomic counter bumps.

Resume after any crash: `WHERE name > scan_cursor_file` — pages are idempotent (`INSERT IGNORE`/upsert). ~600 pages of 2000; minutes of wall clock.

### 3.3 Phase SCAN_FS — streaming walk

`os.scandir` recursive over `sites/<site>/public/files` and `sites/<site>/private/files` (excluding the quarantine dir). For each regular file: compute expected URL (`/files/<rel>` or `/private/files/<rel>`), `url_hash`, collect `(url_hash, size, mtime, path)` in a 1000-entry buffer, then:

```sql
UPDATE `tabCloud Migration Object`
SET on_disk=1, disk_size=%s, disk_mtime=%s, disk_path=%s
WHERE campaign=%s AND url_hash=%s
```
(executemany; point updates on the unique index). Hashes with zero matched rows → insert `orphan_disk` object rows (`classification=orphan_physical`, `status=Skipped` — never auto-processed, reported only). Commit per buffer.

**Resumability decision (ADR)**: FS walk restarts from zero on crash — it is pure idempotent upsert and costs only ~1.2M `stat` calls (2–5 min); a sortable cursor over an unordered million-entry flat dir is not worth the memory. `fs_entries_seen` is progress display only.

**Hashing**: none here (ADR-M11).

### 3.4 Phase CLASSIFY — set-based SQL, resumable via `classify_step`

Ordered steps, each one UPDATE (or small set), each committed and recorded in `campaign.classify_step`:

1. `missing_physical`: `identity_kind IN (local_public, local_private) AND on_disk=0` → classification + Conflict (`missing_physical`, Blocker), `status=Conflict`.
2. `conflicting_url`: objects with ≥2 distinct non-null `content_hash` among refs:
   ```sql
   SELECT migration_object FROM `tabCloud Migration File Ref`
   WHERE campaign=%s GROUP BY migration_object
   HAVING COUNT(DISTINCT content_hash) > 1
   ```
   → `classification=conflicting_url`, `status=Conflict`, Conflict row (`conflicting_url`, Blocker) with all ref hashes in details. (Same URL, different byte expectations — cannot pick bytes automatically.)
3. `shared_url`: `ref_count>1` and not conflicting → supported pattern, proceeds normally.
4. `shared_content`: objects sharing the same non-null `db_content_hash` + equal `disk_size` (self-join on the `(campaign, db_content_hash)` — add temp index if needed via `sha256` index later) → mark `classification=shared_content`; UPLOAD-time dedup handles reuse.
5. `dup_filename`: informational — `GROUP BY file_name HAVING COUNT(*)>1` over refs → set flag; never blocks (filename is not identity — mission invariant).
6. `remote_url` / `legacy_fork`: by identity_kind; legacy rows go to `status=Pending` with adoption path if `adopt_legacy_fork_rows`, else `Skipped`. `remote_https` → `Skipped` unless `adopt_remote_https_rows`.
7. `privacy_mismatch=1` → Conflict (`ambiguous_privacy`, **Blocker** per A19): the object is HALTED (`status=Conflict`) pending an operator decision on true visibility — it is never uploaded under URL-derived privacy.
8. Everything remaining local+on_disk+ref_count=1 → `healthy_unique`.
9. Final counter reconciliation `GROUP BY status/classification`; campaign → `Analyzed`; publish summary.

### 3.5 Snapshot storage cost
1.2M objects (~0.7KB row+index) + 1.2M refs (~0.35KB) + conflicts ≈ **1.5–2.5GB** InnoDB. Accepted (ADR-M15); `bench ... cfs-migrate-purge` deletes Object/Ref/Batch rows of Completed campaigns older than a retention window (default 90d), keeping Campaign, Conflicts, Aliases, Audit.

## 4. Planning

`plan_campaign(campaign)` (job on cloud_migration queue): assign `batch` to all `status=Pending` objects ordered by `(is_private, disk_path)` (locality for page cache) in chunks of `batch_size` via one keyset UPDATE loop; create Batch rows with `object_count`, `bytes_total`. Campaign → `Planned`. Re-runnable: only touches objects with `batch IS NULL`.

## 5. State Machines

### 5.1 Campaign
```
Draft ──start_analysis──▶ Analyzing ──▶ Analyzed ──plan──▶ Planned ──start──▶ Running
Running ⇄ Paused (control_flag=PAUSE / resume)
Running ──control_flag=STOP──▶ Stopping ──(workers drain current object)──▶ Stopped ──start──▶ Running
Running ──all batches Verified──▶ (stays Running w/ active_phase=VERIFY done) ──approve_cleanup + start_cleanup──▶ Cleanup Running ──▶ Completed
any ──fatal──▶ Failed (operator may re-start → re-derives from DB)
```
Transitions are whitelisted methods in `api.py`, each: `frappe.only_for("System Manager")` → `SELECT ... FOR UPDATE` on the campaign row → assert expected current status → write → `frappe.db.commit()` → audit row. CLI runs as Administrator through the same functions. `approve_cleanup` is a **separate** action from `start_cleanup` (both audited). *Reconciliation note, P8:* the parenthetical here originally read "two-person-rule friendly"; it is aspirational and is not delivered in v1 — nothing makes the two actors distinct. See DECISIONS 2026-08-16, security finding L-2.

### 5.2 Batch (CAS-driven, DB-transactional)
```
Pending ──dispatcher enqueues──▶ UploadDispatched ──worker claim CAS──▶ Uploading ──▶ Uploaded
Uploaded ──dispatcher──▶ VerifyDispatched ──claim──▶ Verifying ──▶ Verified
Uploading/Verifying ──heartbeat stale (>10 min)──▶ Stalled ──dispatcher resets──▶ Pending/Uploaded (attempts+1)
attempts > max ──▶ Failed (objects keep their own states; operator retry re-opens)
```
Claim: `UPDATE tabCloud Migration Batch SET status='Uploading', worker_host=%s, rq_job_id=%s, heartbeat_at=NOW() WHERE name=%s AND status='UploadDispatched'` — `affected_rows==1` or the job exits silently (defeats double execution even if RQ dedup fails — ADR-M4).

### 5.3 Object
```
Pending ─▶ Uploading ─▶ Uploaded ─▶ Verifying ─▶ Verified ─▶ CleanupEligible ─▶ Quarantined ─▶ CleanedUp
Pending ─▶ DedupReused (existing verified CSO with same sha256+size) ─▶ CleanupEligible ...
Pending ─▶ Adopted (legacy path, §8) ─▶ Verified
any transient error ─▶ (status unchanged or back to Pending) attempt_count+1, next_retry_at set
attempt_count ≥ max_attempts ─▶ Failed ─▶ Conflict(upload_failed/verify_failed)
classification conflicts ─▶ Conflict;  operator ─▶ Skipped
```
Every object transition = single-row CAS UPDATE + counter bump + commit. Who may trigger: workers (engine functions only), operators via conflict actions (`api.py`, role-checked), never client-side `set_value` (all fields read_only in UI; DocType `in_create`).

## 6. Execution Engine

### 6.1 Queue prerequisite & graceful degradation

`cloud_migration` queue requires (frappe-jobs-backups.md §1.2, `background_jobs.py:41-56` lru_cache):

```jsonc
// common_site_config.json
"workers": { "cloud_migration": { "timeout": 3600, "background_workers": 2 } }
```
plus a dedicated worker process (Procfile: `worker_cloud_migration: bench worker --queue cloud_migration`; supervisor: regenerate via `bench setup supervisor`) and **restart of all processes** (lru_cache). `docs/migration-deploy.md` ships: the JSON, Procfile/supervisor snippets, restart instructions, the note that `stopwaitsecs == timeout` (keep batch wall-time ≪ 3600s), and the S3 bucket lifecycle rule `AbortIncompleteMultipartUpload: DaysAfterInitiation: 7`.

Guard:
```python
def migration_queue_available() -> bool:
    from frappe.utils.background_jobs import get_queues_timeout
    return "cloud_migration" in get_queues_timeout()
```
`start_migration` refuses to start when unavailable (`frappe.throw` with link to the deploy doc) unless `force_fallback_queue=1` is passed explicitly, in which case jobs go to `long` with: Error Log entry, realtime banner, and a red campaign-form warning on every render. Never silent.

### 6.2 Dispatcher (`engine.dispatch_tick`) — the coordinator (ADR-M5)

```python
def dispatch_tick():
    camp = frappe.db.get_value("Cloud Migration Campaign",
        {"status": ("in", ("Running", "Cleanup Running"))}, ["name", ...], as_dict=True)
    if not camp: return                       # O(ms) idle path
    _recover_stale_batches(camp)              # heartbeat_at < NOW()-10min → Stalled → reset, attempts+1
    inflight = frappe.db.count("Cloud Migration Batch",
        {"campaign": camp.name, "status": ("in", ("UploadDispatched","Uploading","VerifyDispatched","Verifying"))})
    for batch in _next_batches(camp, limit=camp.parallelism - inflight):
        _enqueue_batch(camp, batch)           # verify-priority: Uploaded batches before Pending ones
    report.publish_campaign_snapshot(camp.name)
```
`_enqueue_batch` uses `frappe.enqueue("cloud_file_storage.migration.engine.run_upload_batch", queue="cloud_migration", timeout=3600, job_id=f"cfs::{camp}::up::{batch}", deduplicate=True, campaign=..., batch=...)`. RQ dedup covers only QUEUED/STARTED (§2.1) — the batch-status CAS is the real guard. Scheduler tick is 60s by default (`scheduler_tick_interval`, frappe/utils/scheduler.py:247-248) — acceptable because of self-chaining: at the end of `run_upload_batch`/`run_verify_batch`, the job calls `_chain_next(campaign)` which enqueues the next eligible batch directly (same dedup discipline). The tick exists for crash recovery and cold start.

### 6.3 Batch upload job

```python
def run_upload_batch(campaign: str, batch: str):
    if not _claim_batch(batch, expected="UploadDispatched", to="Uploading"): return
    camp = _load_campaign_policy(campaign)
    s3 = get_s3_client()                        # runtime layer; one client per job (not per object — repo-audit.md §3 anti-pattern)
    throttle = ThrottleBudget(camp.bandwidth_limit_mbps, camp.parallelism)
    for obj in _iter_batch_objects(batch):      # keyset over (batch,status) index; statuses Pending or retry-due Failed-transient
        flag = frappe.db.get_value("Cloud Migration Campaign", campaign, "control_flag")  # PK lookup, per object
        if flag in ("PAUSE", "STOP"): break
        _process_object_upload(obj, camp, s3, throttle)   # own commits
        _heartbeat_maybe(batch)                 # every 25 objects or 15s
    _finalize_upload_batch(batch)               # all done → Uploaded; failures remain → Uploaded (partial) with objects_failed>0
    _chain_next(campaign)
```

`_process_object_upload(obj, camp, s3, throttle)`:
1. CAS `Pending→Uploading`; commit.
2. Re-stat `disk_path`; on `FileNotFoundError` → `_refresh_object_from_db(obj)` (re-read File rows: URL changed → update identity/disk_path and retry once; File rows gone → `Skipped(reason=deleted_during_migration)`).
3. Stream-read file once computing SHA256+MD5 while uploading:
   - dedup check first: `SELECT name FROM tabCloud Storage Object WHERE sha256=%s AND size=%s AND status='Verified'` — requires hashing before upload; for files < 8MB hash in-memory pass then upload the same buffer; for larger, hash pass then `upload_file` (two reads; page cache makes pass 2 cheap). If hit and `dedupe_by_content_hash` → attach refs to existing CSO (refcount += ref_count via runtime API `adopt_references(cso, n)`), object → `DedupReused`, commit, return.
   - else `s3.upload_file(disk_path, bucket, key, ExtraArgs={"ChecksumAlgorithm": "SHA256", "ContentType": mime, **sse_args}, Config=TransferConfig(multipart_threshold=64MB, multipart_chunksize=16MB, max_concurrency=4))`. Key comes from the runtime layer's content-addressed generator (`sha256`-derived — never filename, mission invariant). On any exception: if a multipart upload id was created, `abort_multipart_upload` in `finally` (belt) + lifecycle rule (suspenders).
4. Create/lookup CSO row via runtime API `ensure_cso(...)` (frozen A7 interface; status set via `set_cso_status`, lowercase `uploaded`) — **no File mutation yet** (ADR-M10).
5. Object CAS `Uploading→Uploaded` (+ `sha256/md5/size_bytes/uploaded_at`, `cloud_storage_object`), counters, commit.
6. `throttle.consume(size)` → sleeps as needed (per-process budget = limit/parallelism; simple elapsed-vs-bytes token bucket).
7. On transient error (S3 5xx, timeout, throttling): CAS back to `Pending`, `attempt_count+1`, `next_retry_at = now + backoff`, `error_class/last_error`; commit; continue with next object. `attempt_count ≥ max_attempts` → `Failed` + Conflict(`upload_failed`).

### 6.4 Batch verify job (`run_verify_batch`) — separate pass, separate job

Per object in `Uploaded`:
1. CAS `Uploaded→Verifying`.
2. `verify.verify_object(obj, cso, s3, policy)`:
   - size < `verify_strategy_cutover_mb`: `head_object(Bucket, Key, ChecksumMode="ENABLED")` → compare `ContentLength == size_bytes` AND `ChecksumSHA256 == b64(sha256)` (single-part guaranteed by ADR-M8). ETag is NOT trusted (SSE-KMS/multipart make it non-MD5).
   - size ≥ cutover: streamed `get_object` re-hash SHA256 + size compare (ADR-M7).
3. Match → in ONE transaction: CSO status → `Verified` (runtime API), object → `Verified`, and the sanctioned link write:
   ```sql
   UPDATE `tabFile` f
   JOIN `tabCloud Migration File Ref` r ON r.file = f.name
   SET f.cloud_storage_object = %(cso)s
   WHERE r.migration_object = %(obj)s
   ```
   (`update_modified` semantics: plain UPDATE without touching `modified` — deliberate; audit row `file_link`), refs `linked=1`, backfill `tabFile.content_hash = md5` where NULL (restores core dedup/refcount — contract #15), counters; commit.
4. Mismatch → object `Failed`, Conflict(`checksum_mismatch`, Blocker) with both hashes/sizes; CSO stays `Uploaded` (never served); **no retry of verification without re-upload** — retry action re-runs upload (bytes may have changed under us → re-hash catches drift; two successive mismatches with different local hashes ⇒ Conflict `checksum_unstable`, meaning the file is being actively rewritten, e.g. `save_file(overwrite=True)` flows, contract #11 — operator migrates it later or runtime DUAL_WRITE has already superseded it).

### 6.5 CLEANUP (operator-triggered only)

Gates, all revalidated server-side inside the job (not just at button time):
1. Campaign has `cleanup_approved_by/at` set via `approve_cleanup` (role-checked, audited).
2. Operation mode ∈ {S3_PRIMARY_LOCAL_FALLBACK, S3_ONLY} (ADR-M16).
3. Per object: status ∈ {Verified, DedupReused}, CSO status `Verified`, all refs `linked=1`.
4. **Fresh independent re-verification at deletion time**: `head_object` size+checksum again (cheap HEAD; catches remote-side loss between verify and cleanup).
5. Then: quarantine `os.rename(disk_path, quarantine_path)` (or `os.remove` in Direct Delete mode) + thumbnail handling per ADR-M17 + object → `Quarantined`/`CleanedUp` + audit row (`local_quarantine` with full path/size/sha256) + commit. Runs as batched jobs (`run_cleanup_batch`) through the same dispatcher.
`purge_expired_quarantine`: daily; deletes quarantine files older than TTL, object `Quarantined→CleanedUp`, audit `quarantine_purge`. No deletion ever occurs in UPLOAD or VERIFY code paths — those modules contain no `os.remove`/`os.rename` calls at all (lintable invariant).

### 6.6 Idempotency / resumability proofs

| Event | UPLOAD | VERIFY | CLEANUP |
|---|---|---|---|
| Worker SIGKILL mid-object | Object stuck `Uploading`; batch heartbeat goes stale → dispatcher resets batch, `Uploading` objects CAS back to `Pending`; re-upload PUTs the same deterministic content-addressed key (overwrite-idempotent); partial multipart aborted by lifecycle rule + next attempt starts fresh | Stuck `Verifying` → reset to `Uploaded`; HEAD/GET is read-only idempotent | Stuck object: rename either happened (target exists → treat as done, verify by `os.path.exists`) or not (source exists → redo); rename is atomic, no partial state |
| Redis flush / restart | Queue contents lost; DB rows unchanged; next `dispatch_tick` re-enqueues from batch statuses (ADR-M4) | same | same |
| bench restart (warm SIGTERM) | rq finishes current job or supervisor kills at `stopwaitsecs`; both collapse to the SIGKILL row above | same | same |
| S3 5xx / throttling | caught per object → backoff retry, attempt counter, batch continues (per-object error isolation — the fork's single-try loop is explicitly rejected, repo-audit.md §6) | retry via backoff; persistent → `verify_failed` Conflict | HEAD failure blocks deletion (fail-safe: nothing deleted), Conflict `cleanup_blocked` |
| Checksum mismatch | n/a (hash computed from source) | object Failed + Conflict; local file untouched | pre-delete HEAD mismatch → abort object, Conflict, local file untouched |
| Partial batch completion | batch `Uploaded` with `objects_failed>0`; failed objects carry retry state; `retry` action or next dispatch window re-processes only non-terminal objects (`_iter_batch_objects` filters by status) | same pattern | same pattern |
| Duplicate job execution (RQ dedup gap) | second job fails `_claim_batch` CAS and exits | same | same |

## 7. Conflict Queue & Aliases

**What lands there**: `conflicting_url`, `checksum_mismatch`, `checksum_unstable`, `missing_physical`, `corrupt_metadata`, `ambiguous_privacy`, `upload_failed`, `verify_failed`, `adoption_failed`, `cleanup_blocked` (§3.4, §6).

**Resolution UX**: Conflict list view filtered `status=Open`, sorted Blocker-first; form shows object, refs, details JSON rendered; actions (whitelisted in `conflicts.py`, all `frappe.only_for("System Manager")`, all audited):
- `retry(conflict)` — reset object to `Pending`, `attempt_count=0`, batch re-opened; conflict → `Retrying`.
- `skip(conflict)` — object → `Skipped`; conflict → `Skipped` (file stays local forever; report flags it).
- `relink(conflict, keep_file, new_urls_map)` — for `conflicting_url`: the chosen File keeps the original URL; each other File row gets a fresh physical copy: bytes are **copied** (never moved) to a new canonical name (`generate_file_name`-style hash-suffix), its `file_url` updated via `aliases.rewrite_file_url(...)`, then each becomes its own new Migration Object (`Pending`). This is the only flow that renames a local URL.
- `mark_resolved(conflict, note)`.

**`aliases.rewrite_file_url(file_name, old_url, new_url, reason, campaign)`** — the consistency updater:
1. Insert `Cloud File URL Alias` row; for public URLs also create filesystem symlink `public/files/<old> → <new>` (nginx keeps serving old links — frappe-file-core.md §4: `/files/*` never reaches Python).
2. `UPDATE tabFile SET file_url=%s WHERE name=%s` (+ `thumbnail_url` if renamed).
3. Parent consistency: if ref has `attached_to_doctype/name/field` → validate via Meta that the field exists and `fieldtype in ("Attach","Attach Image")` → `UPDATE tab<dt> SET <field>=%s WHERE name=%s AND <field>=%s` (guarded equality); also update the parent Meta `image_field` if it equals old_url; also sweep sibling File rows sharing the old URL only when explicitly part of the same relink decision. (Contract #18: business fields store `file_url` — `gst_return_log.py:87,104`, `stock_ledger.py:308-318`.)
4. Audit row `url_rewrite` with before/after.
Never invoked for healthy files (ADR-M12).

## 8. Legacy Remote Rows (old fork adoption)

`adoption.py`, objects with `identity_kind=legacy_fork_key` (fork wrote `/api/method/frappe_s3_attachment.controller.generate_file?...` private URLs and raw bucket URLs, key in `File.s3_object_key` — repo-audit.md §3 step 6):

`adopt_object(obj)` per object inside adoption batches (same engine):
1. `head_object(legacy_bucket, s3_object_key)` — exists? capture size, ETag, ContentType.
2. Create CSO: provider/bucket/key = legacy location (CSO supports non-content-addressed `source=legacy` keys), `md5 = ETag if '-' not in ETag else NULL` (single-part ETag is MD5; multipart unknown), `sha256=NULL` (optional later backfill via re-GET job), status `Verified` (existence+size verified; hash-verification deferred — these bytes were already the serving truth).
3. Link File rows (same sanctioned UPDATE as §6.4), backfill `content_hash` from md5 when known.
4. `canonicalize_legacy_api_urls` (default ON — these URLs 404 after rename regardless, rename-and-bench-reality.md §3 ⚠ row): rewrite `file_url` to canonical `/private/files/<file_name>` (or `/files/...`) via `aliases.rewrite_file_url(reason=legacy_adoption)`; serving layer then redirects to signed URLs. A thin whitelisted shim `cloud_file_storage.api.compat.legacy_generate_file` (+ optional old dotted alias for external installs) resolves stragglers for one release.
5. HEAD 404 → Conflict(`adoption_failed`, Blocker) — pointer to nothing; operator decides (restore from backup / skip).
No re-upload, no byte movement. `remote_https` non-fork rows: `Skipped` by default; opt-in adoption uses the same flow against the parsed bucket/key when the URL matches the configured bucket, else stays `remote_url` untouched.

## 9. Observability

- **Counters**: campaign fields (ADR-M13); Desk dashboard = Number Cards bound to campaign fields + a progress block on the Campaign form (`cloud_migration_campaign.js` renders %, MB/s, ETA).
- **Rates/ETA**: `report.publish_campaign_snapshot` computes rolling rate from `(bytes_uploaded, objects_*)` deltas cached in `frappe.cache` (best-effort only — cache is LRU-evictable, frappe-jobs-backups.md §8) and `ETA = remaining_objects / obj_rate` (request-bound corpus → object rate dominates).
- **publish_realtime cadence**: batch jobs publish event `cfs_migration_progress` at most every 2s (module-level monotonic timestamp guard); dispatcher publishes once per tick. Rationale: realtime rides the queue Redis instance — flooding it degrades all desk realtime (frappe-jobs-backups.md §8.2).
- **Migration Report export**: `report.export_campaign_report(campaign, fmt="csv"|"jsonl")` → private File containing one row per object: identity, classification, status, sizes, hashes, CSO key, attempts, error, timestamps, conflict link + a summary header (counts by class/status, bytes, durations). Streamed via server-side cursor (no 1.2M-row materialization).
- **Audit**: every destructive/mutating action (§2.8) — quarantine, purge, delete, URL rewrite, File link, adoption, conflict actions, campaign transitions — one immutable row each, written in-transaction.

## 10. Reconcile (three-way diff)

`reconcile.run_reconcile(bucket=None, prefix=None)` — job on `cloud_migration` queue, `job_id="cfs::reconcile"`:
1. Paginated `list_objects_v2` (1000/page, ContinuationToken persisted on the `Cloud Reconcile Run` row per page → resumable), streaming each page's keys against CSO via `WHERE object_key IN (...)` (indexed).
2. Classifies: **orphan remote** (bucket key, no CSO), **missing remote** (CSO Verified, no bucket key — CRITICAL alert), **size mismatch**, **unreferenced CSO** (CSO with refcount 0 / no File link — GC candidates for the runtime layer's lifecycle, never auto-deleted here).
3. Writes JSONL drift report file + counters; publishes summary; missing-remote > 0 raises an Error Log + realtime alert.

## 11. bench CLI (`cloud_file_storage/commands.py`)

Click commands (discovered via `{app}.commands` — `bench_helper.py:72-86`), each: `frappe.init(site) → frappe.connect() → frappe.set_user("Administrator")` → call the **same** `migration.api` functions the UI buttons use (single validation path):

```
bench --site S cfs-migrate-analyze   [--new "title"] [--campaign NAME]
bench --site S cfs-migrate-plan      --campaign NAME [--batch-size N]
bench --site S cfs-migrate-start     --campaign NAME [--parallelism N] [--bandwidth-mbps N] [--force-fallback-queue]
bench --site S cfs-migrate-pause     --campaign NAME
bench --site S cfs-migrate-resume    --campaign NAME
bench --site S cfs-migrate-stop      --campaign NAME          # stop-after-current-object/batch
bench --site S cfs-migrate-status    --campaign NAME [--watch] # renders counter snapshot table
bench --site S cfs-migrate-verify    --campaign NAME           # (re)dispatch verify batches
bench --site S cfs-migrate-approve-cleanup --campaign NAME
bench --site S cfs-migrate-cleanup   --campaign NAME [--direct-delete]
bench --site S cfs-migrate-report    --campaign NAME [--format csv|jsonl]
bench --site S cfs-migrate-reconcile [--prefix P]
bench --site S cfs-migrate-purge     --campaign NAME [--older-than-days 90]
```

## 12. Runtime Math & Recommended Defaults

Corpus: 1.2M files / 100GB ⇒ avg 83KB ⇒ **request-rate bound**, not bandwidth bound:
- UPLOAD per object: 1 stat + 1 hash pass (83KB ≈ sub-ms) + 1 single-part PUT (~50–90ms to same-region S3) + 2 commits (~2–5ms) ≈ 70–110ms → **~10–14 obj/s per batch worker**.
- 2 workers (default): ~22 obj/s → 1.2M ÷ 22 ≈ **15h upload**; 4 workers: ~7.5h. Aggregate bandwidth at 4 workers ≈ 100GB/7.5h ≈ **3.8MB/s** — confirms the network is idle-bound; throttle default off.
- VERIFY: 1 HEAD (~25–40ms) + link UPDATE ≈ 30–50 obj/s/worker → **~4–8h** at defaults (overlapped with upload via verify-priority dispatch).
- CLEANUP: rename ≈ 500–1000 obj/s single worker → **< 1h** + HEAD re-check dominating (~10h at 30/s single worker; run at parallelism 2 → ~5h).
- **Recommended defaults**: `batch_size=1000` (⇒ 1200 batches ≈ 1200 upload + 1200 verify jobs total — Redis queue depth never exceeds `parallelism` because the dispatcher enqueues lazily; vs. the forbidden 1.2M-job flood that would OOM the persistence-less queue Redis, frappe-jobs-backups.md §8.1), `parallelism=2` on this bench (1 vCPU-ish worker box; raise to 4 with `workers.cloud_migration.background_workers=4` on stronger hardware), `timeout=3600` (batch wall-time at 1000 objects ≈ 100s upload / 40s verify — 30× headroom under `stopwaitsecs`), backoff base 60s, max_attempts 5.
- End-to-end operator timeline: analyze (~1h) → plan (minutes) → upload+verify (~1–2 days at parallelism 2, site fully live, default/long workers untouched) → conflict triage → approve+cleanup (~half day) → 14-day quarantine TTL → purge.

## 13. Failure-Mode Analysis (top risks + mitigations)

| Failure | Impact | Mitigation |
|---|---|---|
| Queue unconfigured / worker absent | jobs never run or land on `long`, starving normal jobs | start-time hard guard + explicit `--force-fallback-queue` with loud persistent warnings (§6.1) |
| File mutated during migration (`overwrite=True` flows, contract #11) | uploaded bytes ≠ local bytes | VERIFY hash mismatch → Conflict `checksum_unstable`; runtime DUAL_WRITE supersedes; retry re-hashes |
| File deleted mid-flight | dangling ref | `_refresh_object_from_db` → `Skipped(deleted_during_migration)`; link UPDATE JOIN no-ops on missing rows |
| is_private flip mid-flight (`shutil.move`, `file.py:265`) | disk_path invalid | FileNotFoundError → refresh → identity recomputed |
| Redis loss | queued dispatch lost | DB-derived re-dispatch (ADR-M4) |
| Multipart orphans | silent storage cost | `abort_multipart_upload` in `finally` + bucket lifecycle rule (deploy doc) |
| Backfilling `content_hash` re-awakens core dedup on rows the fork had nulled | behavior change on future inserts | intended (contract #15); done only post-verification with the true MD5; content_hash index added first (ADR-M14) |
| Counter drift from crashed transactions | wrong dashboard | per-batch `GROUP BY` reconciliation (ADR-M13) |
| Operator deletes campaign rows mid-run | engine loses plan | Campaign deletable only in Draft/Completed; Objects/Refs have no delete perm |
| Two campaigns racing | double-processing | single-active-campaign CAS rule (§2.1) |
| kaynes.site/kaynes.test shared DB | double execution across "sites" | documented in deploy doc; campaign is per-DB so the single-active rule already covers it (rename-and-bench-reality.md §1) |

## 14. Ordered Implementation Steps (with acceptance criteria)

1. **DocTypes + indexes** (all §2 schemas, `on_doctype_update` indexes, perms). *AC*: `bench migrate` creates tables; `SHOW INDEX` shows composite/unique indexes; non-System-Manager gets PermissionError on all.
2. **`ensure_analysis_indexes` + audit writer**. *AC*: idempotent double-run; `tabFile.content_hash` index present; audit rows immutable (no write perms).
3. **Analyzer SCAN_DB** (keyset scan, identity/upsert, cursor checkpoint). *AC*: on a seeded site (incl. shared-URL, remote, legacy, corrupt fixtures) object/ref counts match hand-computed truth; kill -9 mid-scan + rerun yields identical final counts.
4. **Analyzer SCAN_FS + CLASSIFY**. *AC*: missing/orphan/conflicting/duplicate fixtures classified exactly per §3.4 table; rerun idempotent; Conflicts created once (no duplicates).
5. **Planner**. *AC*: every Pending object has a batch; batch sizes = batch_size (last partial); rerun no-ops.
6. **Queue guard + dispatcher + heartbeat/stale recovery**. *AC*: with queue unconfigured `start` throws with doc link; with queue configured, dispatcher keeps exactly `parallelism` batches in flight; killing a worker mid-batch → batch back to Pending within one tick, `Uploading` objects reset.
7. **UPLOAD phase** (stream hash, TransferConfig+ChecksumAlgorithm, dedup reuse, CSO creation, retry/backoff, throttle, control_flag). *AC*: MinIO integration test migrates fixture corpus; no `tabFile` mutation occurs (assert via checksum of File table before/after); SIGKILL mid-upload leaves no local deletion and rerun converges; identical-content pair yields one CSO with refcount 2.
8. **VERIFY phase** (HEAD-checksum + re-GET path, link transaction, content_hash backfill, mismatch conflict). *AC*: tampering an uploaded object (overwrite in bucket) produces `checksum_mismatch` Conflict and NO File link; healthy path sets `File.cloud_storage_object` for all refs atomically; `get_content()` via runtime layer returns byte-identical content post-link.
9. **Conflicts + aliases + parent updater**. *AC*: relink of a conflicting_url fixture leaves original URL serving old bytes, new File serving new bytes, Attach field of parent updated, alias row + symlink present, all audited.
10. **CLEANUP + quarantine + TTL purge + thumbnail policy**. *AC*: cleanup refuses without approval/mode; pre-delete HEAD tamper aborts with `cleanup_blocked`; quarantined file restorable by rename; purge after TTL only; grep proves `os.remove|os.rename` absent from engine/verify modules.
11. **Adoption (legacy fork)**. *AC*: fixture with `/api/method/frappe_s3_attachment...` URL + seeded MinIO object gets CSO w/o re-upload, canonical URL + alias, still downloads via serving layer.
12. **Observability + report export**. *AC*: realtime events ≤1 per 2s under load; exported CSV row count = object count; ETA within ±30% on fixture run.
13. **Reconcile**. *AC*: seeded orphan-remote + deleted-remote objects appear in drift JSONL with correct classes; missing-remote raises alert.
14. **CLI commands**. *AC*: every command drives the same api functions (spy test); `cfs-migrate-status --watch` renders; commands fail cleanly on wrong campaign state.
15. **Deploy doc + purge command + scale rehearsal**. *AC*: `docs/migration-deploy.md` contains queue JSON/supervisor/lifecycle-rule; synthetic 100k-file rehearsal on a staging site completes UPLOAD+VERIFY unattended with ≥99.9% Verified and zero local deletions.

### Critical Files for Implementation
- /home/user/v15/apps/cloud_file_storage/cloud_file_storage/migration/engine.py (dispatcher, batch jobs, claims, heartbeat, throttle — new)
- /home/user/v15/apps/cloud_file_storage/cloud_file_storage/migration/analyzer.py (scan/walk/classify — new)
- /home/user/v15/apps/cloud_file_storage/cloud_file_storage/cloud_file_storage/doctype/cloud_migration_object/cloud_migration_object.json (core snapshot/state schema — new)
- /home/user/v15/apps/cloud_file_storage/cloud_file_storage/migration/api.py (whitelisted state transitions shared by UI + CLI — new)
- /home/user/v15/apps/frappe/frappe/utils/background_jobs.py (queue registration/enqueue/dedup ground truth the engine must conform to — reference)