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

All research verified. Composing the design document.

# Cloud File Storage — Productization Design
**Domain:** rename execution, backup/lifecycle, Desk UI, testing/CI, docs, phased delivery.
**Ground truth:** six research reports in scratchpad `research/`; frappe @ v15.93.0 (`/home/user/v15/apps/frappe`); fork @ `51b41e6` (`/home/user/v15/apps/cloud_file_storage`, inner package still `frappe_s3_attachment`). Sibling designs own: File subclass/CSO/serving (runtime) and the migration engine; this document defines their product shell, shared schemas it needs, and every integration seam.

---

## 0. Package Layout & Naming Contract (shared with sibling designs)

```
/home/user/v15/apps/cloud_file_storage/
├── pyproject.toml                     # name = "cloud_file_storage"
├── README.md, CHANGELOG.md, license.txt (MIT, three-party attribution)
├── .github/workflows/{ci.yml, linter.yml, codeql.yml}  + .github/helper/install.sh
├── .pre-commit-config.yaml, commitlint.config.mjs, .editorconfig, dependabot.yml
├── docs/                              # §6
├── delivery role definitions:                     # §6.4
├── INVARIANTS.md                          # §6.3
└── cloud_file_storage/                # python package (was frappe_s3_attachment/)
    ├── __init__.py                    # __version__ = "1.0.0-dev"
    ├── hooks.py                       # app_name = "cloud_file_storage", app_title = "Cloud File Storage"
    ├── modules.txt                    # single line: Cloud File Storage
    ├── patches.txt                    # [pre_model_sync] + [post_model_sync] sections
    ├── install.py                     # after_install/after_migrate: role, custom fields, indexes, seeds
    ├── cloud_file_storage/            # module dir (Frappe module "Cloud File Storage")
    │   ├── doctype/
    │   │   ├── cloud_storage_settings/            # Single (§3.1)
    │   │   ├── cloud_storage_ignored_doctype/     # child table (§3.1)
    │   │   ├── cloud_backup_settings/             # Single (§2.1)
    │   │   ├── cloud_storage_backup_log/          # log doctype (§2.4)
    │   │   ├── cloud_storage_object/              # SIBLING (runtime design)
    │   │   ├── cloud_migration_campaign/          # SIBLING (migration design)
    │   │   ├── cloud_migration_batch/             # SIBLING
    │   │   ├── cloud_migration_object/  (A9: 'Object', not 'Item')              # SIBLING
    │   │   └── cloud_migration_conflict/          # SIBLING
    │   ├── page/cloud_migration_dashboard/        # custom Page (§3.4)
    │   └── workspace/cloud_storage/               # Workspace (§3.4)
    ├── core/                          # SIBLING runtime: file_override.py, write_hooks.py, storage.py, serving.py, materialize.py
    ├── migration/                     # SIBLING migration engine
    ├── api/
    │   ├── admin.py                   # all Desk button endpoints (§3.3)
    │   └── compat.py                  # legacy_generate_file + signed-URL issuance + adopt_installed_apps + detectors (single compat home, A10)
    ├── backup/
    │   ├── tasks.py                   # scheduled backup job (§2.3)
    │   ├── lifecycle.py               # policy generator + economics validator (§2.5)
    │   └── restore.py                 # archive restore helpers (§2.6)
    ├── compat/
    │   └── (merged into api/compat.py — single compat home)
    ├── patches/{v0_2_0, v0_2_1, v1_0}/            # all with __init__.py
    ├── config/                        # desktop.py deleted; keep empty pkg only if needed
    ├── locale/                        # regenerated main.pot + de.po
    └── tests/                         # §4 layout
```

Fixed dotted-path contract (referenced by JS, patches, and docs — sibling designs must not deviate):
- Private-file server endpoint: `cloud_file_storage.api.compat.legacy_generate_file`
- Button endpoints: `cloud_file_storage.api.admin.<name>` (§3.3 table)
- Backup task: `cloud_file_storage.backup.tasks.run_scheduled_backup`
- Migration enqueue targets live under `cloud_file_storage.migration.*`; queue name `cloud_migration` (requires `common_site_config.json` `workers` entry + process restart — frappe-jobs-backups.md §1.2, `background_jobs.py:41-56`; deployment prerequisite documented in runbook, never committed).

---

## 1. Rename Execution Plan

### 1.0 ADR summary (rename)
| # | Decision | Rationale (one line) |
|---|---|---|
| R1 | Immediate unbreak via `.pth` edit before any code change | `.pth` adds the app root to `sys.path`; the inner package is still `frappe_s3_attachment`, so pointing the stale `.pth` at the new dir restores `import frappe_s3_attachment` for all 14 sites in minutes (rename-and-bench-reality §0: crash chain `frappe/__init__.py:258→1670→1513`). |
| R2 | Greenfield rename: ship **new** DocTypes, never `rename_doc` in place | App installed on ZERO sites (rename §1, verified per-DB); rename patches are pure liability here (reaper risk, `frappe/model/sync.py:143-177`). |
| R3 | New names: `Cloud Storage Settings`, child `Cloud Storage Ignored DocType` | Matches app title; drops the "Row" suffix anti-pattern; migration doctypes named per sibling design. |
| R4 | **Keep** `s3_object_key` custom field + `s3_object_key_index`, deprecate in docs | Renaming needs ALTER TABLE + Custom Field rename + 3 code sites (rename §6.23) for zero user value; new authoritative pointer is the `cloud_storage_object` Link field (runtime design); `s3_object_key` stays as legacy-compat lookup for external 0.2.x installs. |
| R5 | `s3_key_generator` hook is **not honored**; startup detector logs + surfaces a Settings warning | Mission forbids filename-derived identity; arbitrary site-generated keys break CSO identity/refcount; a shim that silently ignores results is worse than a loud deprecation. |
| R6 | Old legacy patches kept, dotted-paths rewritten, made fully idempotent, `__init__.py` added to `v0_2_1/` | External 0.2.x installs need the backfill; on fresh installs both no-op (repo-audit §8: `v0_2_1` has no `__init__.py`; backfill SQL embeds old dotted path at `backfill_s3_object_key.py:34`). |
| R7 | Compat patch for external installs: single `pre_model_sync` patch, guarded, opt-in-by-presence | The doctype reaper fires in the same migrate if run post-sync (rename §6.14, `frappe/migrate.py:148`); guards make it a no-op on all 13 live sites here. |
| R8 | **SUPERSEDED BY A10**: NO `tabFile.file_url` mass-rewrite — the `override_whitelisted_methods` remap (consulted before any import, both API routers) keeps every old URL resolving; a `before_request` 302 alias is defense-in-depth; the adoption campaign canonicalizes | A10; rename-and-bench-reality §3 evidence stands but the remedy changed. |
| R9 | erpnext `stock_ledger.py:416` sentinel is moot by design; regression-tested, documented | Our `file_url` stays `/files/...`／`/private/files/...` (contract #9), so `"/frappe_s3_attachment." in file_url` is always False and erpnext falls through to `get_full_path()+open(path,"wb")` — which works because the runtime materializes + write-backs (contract #7/#8). Test T-RIV (§4) proves it. |
| R10 | Bench-level fixes (apps.txt/apps.json/.pth/reinstall) are operational runbook steps, never committed | They are per-bench state; documented in `docs/runbooks/deployment.md`. |

### 1.1 Phase 0 — Unbreak the bench (operational, ~10 minutes, no commits)
1. Edit `/home/user/v15/env/lib/python3.10/site-packages/frappe_s3_attachment.pth`: change the single line to `/home/user/v15/apps/cloud_file_storage`. (`sites/apps.txt` line 15 still says `frappe_s3_attachment` — that is now *correct again* because the module imports.)
2. Verify: `bench --site finstein.erp list-apps` succeeds; spot-check one more site. Skip `skc.local` (dead DB) in any loop; remember `kaynes.site`/`kaynes.test` share one DB (rename §1).
3. Do not touch `apps.json` yet (its `frappe_s3_attachment` key is consistent with apps.txt at this point).

**Acceptance:** every live site initializes; scheduler/web unaffected.

### 1.2 Phase 1 — Code-side rename (one atomic commit series; safe: 0 installs)
Ordered file/identifier list (each bullet = one reviewable commit or squashed group):

1. **Package dirs:** `git mv frappe_s3_attachment cloud_file_storage`; inside it `git mv frappe_s3_attachment cloud_file_storage` (module dir). Doctype dirs: `git rm -r` old `s3_file_attachment/`, `s3_ignored_doctype_row/` (replaced by new doctypes in Phase 2 — do not carry old JSON forward).
2. **pyproject.toml:** `[project] name = "cloud_file_storage"`, description "Enterprise cloud storage for Frappe attachments", tighten `[tool.bench.frappe-dependencies] frappe = ">=15.16.0,<16.0.0"` (A34/FD-8) (current `<17` lets CI test v16 — repo-audit §10, rename §5). Keep `requires-python = ">=3.10"`.
3. **hooks.py:** `app_name`/`app_title`/publisher lines 3-9; delete the legacy `doc_events["File"] = {after_insert, on_trash}` block (`hooks.py:89-94`) — replaced by runtime design's `write_file`/`delete_file_data_content`/`override_doctype_class`/`before_request` hooks; rewrite `after_install`/`after_migrate` dotted paths; strip all commented boilerplate (it is 90% of the file — repo-audit §2).
4. **modules.txt:** single line `Cloud File Storage` (with trailing newline — old file lacked one).
5. **patches.txt:** add section markers; rewrite both legacy entries to `cloud_file_storage.patches.v0_2_0.backfill_s3_object_key` / `...v0_2_1.ensure_data_import_ignored_doctype`; add `[pre_model_sync] cloud_file_storage.patches.v1_0.rename_from_frappe_s3_attachment` (§1.4).
6. **Legacy patch bodies:** `v0_2_0/backfill_s3_object_key.py` — rewrite import at `:19`; keep the SQL `LIKE` predicate at `:34` matching the OLD prefix only, `'/api/method/frappe_s3_attachment.controller.generate_file%'` (A10 — no new-path URLs are ever written); add batching (`LIMIT 5000` loop) and early-return when zero candidate rows. `v0_2_1/`: add `__init__.py`, rewrite import.
7. **install.py:** rewrite `"module": "Frappe S3 Attachment"` → `"Cloud File Storage"` (`install.py:58`), singleton reference → `Cloud Storage Settings`; keep field constant `s3_object_key` and `s3_object_key_index` (ADR R4); add creation of the `cloud_storage_object` Link custom field (schema owned by runtime design) and role `Cloud Storage Manager` (§3.2), all idempotent.
8. **controller.py:** dissolved. Its live surfaces move: `generate_file` semantics → `api/compat.py::legacy_generate_file` (A10); `migrate_existing_files`/`run_migrate_existing_files` → deleted (superseded by migration engine); `ping` → deleted (dead surface, repo-audit §12); job-id constant / regex / logger names die with it. Grep gate: `git grep -n "frappe_s3_attachment"` must return only `compat/`, `patches/`, docs, and CHANGELOG history after this phase.
9. **Tests:** rewrite the 71+13+5 identifier hits (repo-audit §13.D); rename MinIO env vars `FRAPPE_S3_ATTACHMENT_MINIO_*` → `CLOUD_FILE_STORAGE_MINIO_*` (`tests/test_minio_integration.py:26-30`); bucket `frappe-s3-attachment-test` → `cloud-file-storage-test`. Delete the dead QUnit stub `test_s3_file_attachment.js`.
10. **.pre-commit-config.yaml:11:** `files: "cloud_file_storage.*"` (otherwise pre-commit silently checks nothing — rename §6.7). **.gitignore:6** likewise. Delete dead `tox.ini` (2-line flake8 stub contradicting ruff — repo-audit §1).
11. **CI:** full rewrite per §5 (all 7 old-name hits in `ci.yml`, concurrency group, get-app/install-app/run-tests args, MinIO env, frappe pinning).
12. **config/desktop.py + config/docs.py:** delete (dead v12 scaffolding); workspace JSON replaces desk presence.
13. **Locale:** delete stale `locale/main.pot` and `de.po` contents; regenerate after doctypes land: `bench generate-pot-file --app cloud_file_storage`, `bench update-po-files --app cloud_file_storage`, `bench compile-po-to-mo --app cloud_file_storage` (commands verified in `frappe/commands/gettext.py:7-72`). Do **not** sed the catalogs (msgids embed old doctype labels — repo-audit §13.D).
14. **README.md:** new title, install instructions pointing at `RamachandranMD/cloud_file_storage`, keep the fork-delta table as historical "Origins" section; **CHANGELOG.md:** add `[1.0.0]` section "Renamed from frappe_s3_attachment; full rewrite", keep 0.x history with old release links intact. **license.txt:** keep MIT with all three attributions (zerodha 2018, ALYF GmbH, + new copyright line) — MIT requires preserving the notice.
15. **Bench-level (operational, after merge):** `sed` `sites/apps.txt` line 15 → `cloud_file_storage`; update `sites/apps.json:321` key + branch; `env/bin/pip uninstall frappe_s3_attachment && env/bin/pip install -e apps/cloud_file_storage` (removes stale `.pth`+`dist-info`, creates `cloud_file_storage.pth`); `bench build` no-op (no bundles yet); verify `bench --site finstein.erp list-apps`.

**Acceptance (Phase 1):** `bench --site test_site install-app cloud_file_storage` succeeds on a fresh site; `bench run-tests --app cloud_file_storage` green; `git grep frappe_s3_attachment` clean outside compat/patches/docs; pre-commit runs on changed files.

### 1.3 New DocType names (final)
| Old | New | Kind |
|---|---|---|
| S3 File Attachment | **Cloud Storage Settings** | Single |
| S3 Ignored DocType Row | **Cloud Storage Ignored DocType** | child table |
| — | Cloud Backup Settings | Single (§2.1) |
| — | Cloud Storage Backup Log | log (§2.4) |
| — | Cloud Storage Object | sibling (runtime) |
| — | Cloud Migration Campaign / Batch / Object / File Ref / Conflict | sibling (migration) |

### 1.4 Optional guarded compat patch (external frappe_s3_attachment 0.2.x installs)
File: `patches/v1_0/rename_from_frappe_s3_attachment.py`, registered under `[pre_model_sync]` (**mandatory** — post-sync placement lets `remove_orphan_doctypes()` reap `S3 File Attachment`, destroying `__Auth` secret_key and orphaning `tabSingles` — rename §6.14, `frappe/migrate.py:148`, `delete_doc.py:64/207-210`).

```python
def execute():
    # Guard 1: successor state already present → no-op (erpnext lending-split pattern,
    # erpnext/patches/v15_0/remove_loan_management_module.py:5-6)
    if frappe.db.exists("DocType", "Cloud Storage Settings"):
        return
    # Guard 2: no legacy state → fresh install → no-op (all 13 live sites here)
    if not frappe.db.exists("Module Def", "Frappe S3 Attachment"):
        return
```
Then, in order (each step idempotent, re-runnable):
1. `frappe.rename_doc("Module Def", "Frappe S3 Attachment", "Cloud File Storage", force=True, ignore_permissions=True)` — cascades to `tabDocType.module`, `tabCustom Field.module`, `tabProperty Setter.module` (rename §3, `rename_doc.py:182-183`). Note: under `developer_mode=1`, `ModuleDef.on_update` will mkdir + append `modules.txt` (`module_def.py:29-57`) — the appended name equals the shipped line, so it is a harmless duplicate; patch strips duplicates from `modules.txt` if `frappe.conf.developer_mode`.
2. `frappe.db.set_value("Module Def", "Cloud File Storage", "app_name", "cloud_file_storage")` — not covered by rename_doc, required by `get_doctype_app_map()` (`frappe/modules/utils.py:284-294`).
3. Rename doctypes **parent first, then child** (child `parenttype` rewrite filters on `{"parent": new}` — `rename_doc.py:623-649`): `frappe.rename_doc("DocType", "S3 File Attachment", "Cloud Storage Settings", force=True)`, then `S3 Ignored DocType Row` → `Cloud Storage Ignored DocType`. Singles data survives (`doctype.py:649-662`); `__Auth` secret survives (`rename_doc.py:209-210`); disk moves suppressed by `frappe.flags.in_patch` (`doctype.py:665-667`).
4. Rewrite child fieldname mismatches: old child field `doctype_name` is retained in the new schema (decision: new child table keeps `doctype_name` fieldname for this exact reason).
5. `installed_apps` global: read `frappe.db.get_global("installed_apps")`, replace element, `frappe.db.set_global(...)`, then `frappe.defaults._clear_cache("__global")`-equivalent per `installer.py:347/363`.
6. `tabPatch Log`: `UPDATE `tabPatch Log` SET patch = REPLACE(patch, 'frappe_s3_attachment.patches.', 'cloud_file_storage.patches.')` — literal-string matching in `patch_handler.py:56/75`; skipping re-runs every historic patch.
7. ~~`tabFile.file_url` rewrite~~ — **DELETED per A10**: the compat patch does NOT mass-rewrite `tabFile.file_url`; the `override_whitelisted_methods` remap keeps every old `/api/method/frappe_s3_attachment.controller.generate_file?...` URL resolving, and the adoption campaign is the canonicalization layer.
8. **Permanent alias:** `api/compat.py::redirect_legacy_api_urls()` registered in `before_request` (runs before dispatch — `app.py:105/210-211`; HTTPException return path `app.py:134-135`): if `request.path.startswith("/api/method/frappe_s3_attachment.controller.generate_file")` → 302 to the new path preserving the query string (A11: 302, never 301). This covers stored copies of old URLs in business fields, emails, and bookmarks forever, at ~zero cost.
9. Drain note (runbook, not patch): operators must drain RQ queues before migrating an external install (in-flight jobs serialize old dotted paths — rename §6.21).

**Acceptance:** patch is a verified no-op on a fresh site (unit test asserts both guards); a synthetic "legacy site" integration test (create old-named Module Def/DocType rows + fake old-URL File rows in a scratch site, run patch, assert all rewrites + alias redirect 302).

### 1.5 stock_ledger sentinel (documentation deliverable)
`docs/architecture.md` gets a subsection "erpnext repost sentinel": `erpnext/stock/stock_ledger.py:416` tests `"/frappe_s3_attachment." in file_doc.file_url` and, when true, deletes+recreates the File; when false it writes through `get_full_path()` + `open(path,"wb")` (`:420-422`). Our canonical-URL invariant (contract #9) guarantees the sentinel never matches; correctness then rests on invariants #7 (materialization returns an openable path) and #8 (writes through the path are flushed back). Test T-RIV (§4) is the executable proof; the erpnext+IC CI job runs it.

### 1.6 `s3_key_generator` disposition
`api/compat.py::detect_deprecated_hooks()` called from `after_migrate` and from Test Connection: if `frappe.get_hooks("s3_key_generator")` is non-empty, create/refresh a red Note on the Settings health panel and `frappe.log_error` once per migrate: "s3_key_generator is not supported by Cloud File Storage; object keys are content/UUID-derived (see docs/adr/0007)." No shim executes site code (ADR R5).

---

## 2. Backup & Lifecycle Management

### 2.0 ADR summary (backup)
| # | Decision | Rationale |
|---|---|---|
| B1 | Separate Single **Cloud Backup Settings** (not a section of Cloud Storage Settings) | Different bucket/credentials/retention lifecycle and audience; mirrors core `S3 Backup Settings` precedent (frappe-jobs-backups §6); keeps the runtime Settings form focused. |
| B2 | Our scheduled job always calls `new_backup(ignore_files=True)` for DB+config; file tarballs are separate opt-in checks | `ignore_files` is caller-level (`backups.py:164/196-200`); after cutover attachment bytes live in S3 and the CSO table rides in the DB dump — tarring 100 GB nightly is waste. |
| B3 | One `scheduler_events` `hourly` dispatcher that self-gates on frequency, not N frequency-specific entries | Scheduler can't target custom queues (`scheduled_job_type.py:181-182`); a thin O(ms) gate then enqueues real work; renaming-safe (one Scheduled Job Type row). |
| B4 | Backup upload verified by SHA256 computed locally + `head_object` size/checksum match before success is logged | Core S3 backup uploads with zero verification (frappe-jobs-backups §6 shortcomings); mission requires independent verification patterns everywhere. |
| B5 | Lifecycle JSON is generated, previewed with economics math, and applied only to the **backup** bucket; archive classes are hard-refused for the attachment bucket | Mission hard guard: live attachments stay synchronous-retrieval; Glacier minimums (90d/180d) and 40KB overhead make naive policies cost-negative. |
| B6 | Bench-crontab full backups remain the operator's DR baseline; we document coexistence rather than replace them | `bench setup backups` crontab is outside app control (frappe-jobs-backups §5); duplicating DB dumps hourly is cheap, fighting the crontab is not. |

### 2.1 DocType: `Cloud Backup Settings` (Single, module Cloud File Storage, perms: System Manager rwc, Cloud Storage Manager r)
| fieldname | fieldtype | options / default | notes |
|---|---|---|---|
| enabled | Check | 0 | master switch |
| sb_target | Section Break | "Backup Target" | |
| backup_bucket | Data | reqd if enabled | separate bucket from attachments (validated ≠ attachment bucket) |
| backup_prefix | Data | default `{site}/backups` | `{site}` token expanded |
| use_storage_credentials | Check | 1 | reuse Cloud Storage Settings creds/endpoint/region |
| region_name | Data | depends: !use_storage_credentials | |
| endpoint_url | Data | 〃 | HTTPS-only validation reused |
| access_key | Data | 〃 | empty ⇒ default credential chain |
| secret_key | Password | 〃 | |
| sse_type | Select | `Inherit\nSSE-S3\nSSE-KMS`, default Inherit | |
| kms_key_id | Data | depends: sse_type==SSE-KMS | |
| sb_policy | Section Break | "Backup Policy" | |
| frequency | Select | `Hourly\nEvery 6 Hours\nDaily\nWeekly`, default Daily | |
| backup_hour | Int | default 1 | 0-23; used for Daily/Weekly |
| include_site_config | Check | 1 | |
| include_public_files | Check | 0 | tarball; description warns "leave off after cloud cutover" |
| include_private_files | Check | 0 | 〃 |
| compress_files | Check | 1 | maps to `compress_files` kwarg |
| sb_retention | Section Break | "Retention & Archival" | |
| hot_retention_days | Int | default 14 | stays STANDARD |
| archive_after_days | Int | default 30 | 0 = never archive |
| archive_storage_class | Select | `GLACIER_IR\nGLACIER\nDEEP_ARCHIVE`, default GLACIER | backup bucket only (B5) |
| delete_after_days | Int | default 365 | total retention; validated > archive_after_days + class minimum |
| noncurrent_retention_days | Int | default 7 | NoncurrentVersionExpiration |
| abort_multipart_days | Int | default 7 | AbortIncompleteMultipartUpload |
| lifecycle_preview | Button | "Preview Lifecycle Policy" | |
| apply_lifecycle | Button | "Apply Lifecycle Policy" | |
| lifecycle_applied_hash | Data | read_only | SHA256 of last applied policy JSON |
| sb_notify | Section Break | "Notifications & Status" | |
| notify_email | Data | | failure notifications |
| email_on_success | Check | 0 | |
| last_backup_on | Datetime | read_only | |
| last_backup_status | Select | `\nSuccess\nFailed`, read_only | |
| backup_now | Button | "Backup Now" | |

Controller `cloud_backup_settings.py`: `validate()` → bucket-pair check, retention math check (`delete_after_days >= archive_after_days + minimum_storage_days(class)`), `head_bucket` preflight when enabled (pattern from `s3_backup_settings.py:45-76`).

### 2.2 Signatures (backup/tasks.py)
```python
def run_scheduled_backup() -> None:            # scheduler_events hourly dispatcher; O(ms) gate
def is_backup_due(settings, now) -> bool
def take_cloud_backup(trigger: str = "scheduled") -> str   # returns Cloud Storage Backup Log name;
    # new_backup(ignore_files=not (pub or priv), compress_files=..., force=True), assert dump mtime >= job start (A28), then upload_and_verify per artifact
def upload_and_verify(client, bucket, key, local_path, sse_args) -> dict  # {sha256, size, etag}; raises BackupVerificationError
@frappe.whitelist()  # roles: System Manager, Cloud Storage Manager
def backup_now() -> str                        # enqueue(take_cloud_backup, queue="long", timeout=3600, job_id="cfs::backup", deduplicate=True)
```
Sequence (critical path): dispatcher (scheduler `hourly`, runs on `default` per core convention) → gate → `frappe.enqueue(take_cloud_backup, queue="long", ...)` (backups are single-file DB dumps; `long` is appropriate and pre-exists — no dependency on the `cloud_migration` queue) → `new_backup(...)` (`frappe/utils/backups.py:587-628`) → for each artifact: stream SHA256 → `upload_file` with `ExtraArgs={ChecksumAlgorithm:'SHA256', StorageClass:'STANDARD', SSE...}` → `head_object` compare size + checksum → write Backup Log row → JobTimeoutException self-re-enqueue up to 2 (pattern `s3_backup_settings.py:109-126`) → failure: log status=Failed + email `notify_email`.

Coexistence + enforcement of "attachments excluded after cutover" (B2/B6): (a) our job never tars files unless the checkboxes are on; (b) `Cloud Storage Settings` health panel (§3.1) shows a warning chip when `operation_mode in (S3_PRIMARY_LOCAL_FALLBACK, S3_ONLY)` and either include_*_files checkbox is on, or when local `public/files` still holds > threshold bytes post-CLEANUP; (c) `docs/runbooks/backup-restore.md` documents that bench-crontab `bench backup-all-sites` (no `--with-files` by default) remains untouched and why `--with-files` cron entries should be removed after cutover.

### 2.3 Lifecycle policy generator (backup/lifecycle.py)
```python
GLACIER_MIN_DAYS = {"GLACIER_IR": 90, "GLACIER": 90, "DEEP_ARCHIVE": 180}
SYNC_RETRIEVAL_CLASSES = {"STANDARD", "STANDARD_IA", "INTELLIGENT_TIERING", "ONEZONE_IA"}

def build_lifecycle_policy(settings) -> dict            # → S3 LifecycleConfiguration JSON:
    # Rule cfs-archive: Filter.Prefix=backup_prefix, Transitions=[{Days: archive_after_days, StorageClass: ...}]
    # Rule cfs-expire: Expiration.Days=delete_after_days
    # Rule cfs-noncurrent: NoncurrentVersionExpiration.NoncurrentDays=...
    # Rule cfs-abort-mpu: AbortIncompleteMultipartUpload.DaysAfterInitiation=...
def validate_economics(policy, object_stats) -> list[dict]   # [{level: warn|error, code, message, math}]
def assert_synchronous_retrieval(storage_class: str, context: str) -> None  # frappe.throw outside SYNC_RETRIEVAL_CLASSES
@frappe.whitelist()  # System Manager
def preview_lifecycle_policy() -> dict   # {policy_json, warnings, economics_table, current_bucket_policy}
@frappe.whitelist()  # System Manager; requires confirm_phrase == "APPLY LIFECYCLE"
def apply_lifecycle_policy(confirm_phrase: str) -> dict  # put_bucket_lifecycle_configuration; stores lifecycle_applied_hash
```
Economics validator rules (math rendered in the preview dialog): (1) `archive_after_days + GLACIER_MIN_DAYS[class] > delete_after_days` ⇒ **error** "objects deleted before minimum storage duration — you pay the full minimum anyway"; (2) mean object size < 128 KB ⇒ warn about the 40 KB metadata overhead (8 KB STANDARD-billed + 32 KB archive-billed per object) with `%overhead = 40KB/mean_size`; (3) DEEP_ARCHIVE ⇒ warn "retrieval 9–48 h; standard retrieval ≈ $0.02/GB + requests — restoring a full 100 GB backup set ≈ $2 + wait"; (4) `hot_retention_days > archive_after_days` ⇒ error (contradictory). `apply` diffs against `get_bucket_lifecycle_configuration` and shows the delta before write (dry-run = preview). Hard guard: `apply_lifecycle_policy` refuses if `settings.backup_bucket == Cloud Storage Settings.bucket`; `assert_synchronous_retrieval` is also called by `Cloud Storage Settings.validate()` on its `storage_class` field (attachment bucket may only be STANDARD/STANDARD_IA/INTELLIGENT_TIERING).

### 2.4 DocType: `Cloud Storage Backup Log` (autoname `format:CB-{YYYY}-{#####}`, no amend/submit, perms r: System Manager, Cloud Storage Manager; in_create off)
Fields: `trigger` Select(`scheduled\nmanual`), `status` Select(`Running\nSuccess\nFailed`), `started_at` Datetime, `ended_at` Datetime, `db_key` Data, `config_key` Data, `public_files_key` Data, `private_files_key` Data, `total_bytes` (Data — string-typed to dodge INT(11); dfp-audit ADOPT #9), `sha256_manifest` Code(JSON), `error` Small Text, `frappe_backup_path` Data. Retained 90 days via a weekly `clear_old_logs`-style scheduler entry.

### 2.5 Restore-from-archive hooks (backup/restore.py)
```python
@frappe.whitelist()  # System Manager
def list_backups(limit=30) -> list[dict]           # from Backup Log + list_objects_v2 reconcile
@frappe.whitelist()  # System Manager
def initiate_restore(key: str, days: int = 3, tier: str = "Standard") -> dict  # restore_object; returns ongoing-request state
@frappe.whitelist()
def restore_status(key: str) -> dict               # head_object Restore header parse
```
`docs/runbooks/backup-restore.md` walks: pick log row → initiate_restore → poll → download via presigned URL → `bench restore`. No automatic DB restore from Desk (deliberate — destructive, CLI-only).

### 2.6 Failure modes (backup domain)
| Failure | Effect | Mitigation |
|---|---|---|
| Upload dies mid-multipart | orphan MPU billed forever | `cfs-abort-mpu` lifecycle rule + `abort_multipart_upload` in `finally` of `upload_and_verify` |
| `new_backup` succeeds, upload fails | local dump exists, log=Failed | artifact kept in `private/backups` (core retention `keep_backups_for_hours` cleans it); retry on next tick; email |
| Checksum mismatch on verify | corrupt remote artifact | delete remote object, raise `BackupVerificationError`, status Failed — never logged Success (B4) |
| Job killed at timeout (SIGALRM, `rq/worker.py:1426`) | partial state | JobTimeoutException re-enqueue (≤2), idempotent keys `{prefix}/{ts}/...` mean re-run overwrites cleanly |
| Lifecycle applied to wrong bucket | live attachments archived → 403 InvalidObjectState on GET | bucket-pair guard + `assert_synchronous_retrieval` + type-to-confirm (B5) |
| Scheduler paused (`pause_scheduler`) | silent no backups | health panel shows `last_backup_on` age with red chip > 2×frequency |

---

## 3. Desk UI

### 3.1 `Cloud Storage Settings` form (Single)

**Schema ownership: the A12 frozen field set (docs/adr/amendments-register.md) and
runtime-storage.md §2 own the schema — this section only arranges the FORM.** Field
names per A12: `bucket`, `region`, `endpoint_url`, `addressing_style`, `key_prefix`,
`use_default_credential_chain`, `access_key_id`, `secret_access_key` (Password),
`sse_mode` (default SSE-S3), `kms_key_id`, `storage_class` (sync-retrieval only),
`operation_mode`, `fail_insert_on_s3_error`, `cdn_base_url`, `public_presign_ttl` 3600,
`private_presign_ttl` 300, `inline_mimetype_prefixes`, `audit_public_fallback`,
`cache_max_size_mb` 5120, `cache_ttl_hours` 72, `ignored_doctypes`,
`multipart_threshold_mb` 64, `multipart_chunksize_mb` 16, `transfer_max_concurrency` 4,
`object_delete_grace_days` 7, `restore_window_days` 30, `migration_batch_size` 1000,
`migration_parallelism` 2, `migration_bandwidth_limit_mbps` 0.
Form layout: Connection section (+ `test_connection` Button + `connection_status_html`),
Operation Mode & Serving, Local Cache (+ `cache_stats_html`), Rules (`ignored_doctypes`
child table — per-parent uniqueness in validate()), Storage Health
(`storage_health_html` + `analyze_storage` + `reconcile` Buttons), Migration
(`active_campaign` read-only + `migration_html` + JS-injected buttons §3.3).
Permissions: System Manager rwc; Cloud Storage Manager rw (no delete); credential
fields permlevel 1. `quick_entry: 0`, `track_changes: 1`.

### 3.2 Roles model
- New role **Cloud Storage Manager** (desk role, created idempotently in `install.after_install`): operate settings, campaigns, backups (read), dashboards. 
- **System Manager** required for: credential fields (via permlevel 1 on the credential section), Delete Verified Local Copies, Apply Lifecycle, Reconcile-with-fixes, compat-patch tools. Rationale: separation lets ops staff run migrations without credential access; permlevel is the native Frappe mechanism.
- All whitelisted endpoints re-check roles server-side via `frappe.only_for(("System Manager", "Cloud Storage Manager"))` or `frappe.only_for("System Manager")` — never trust the form.

### 3.3 Button set — full contract
All endpoints in `cloud_file_storage/api/admin.py`; every endpoint validates state server-side (the table's Valid-states column) and raises `frappe.ValidationError` otherwise; campaign-state source of truth is `Cloud Migration Campaign.status` — FROZEN machine (A9, migration-engine.md §5.1 verbatim): `Draft / Analyzing / Analyzed / Planned / Running / Paused / Stopping / Stopped / Cleanup Running / Completed / Failed`.

| Button | Endpoint (whitelisted) | Role | Valid states (server-checked) | Client JS behavior | Confirmation |
|---|---|---|---|---|---|
| Test Connection | `test_connection()` | CSM/SM | always | `frappe.call` with freeze; renders result into `connection_status_html` (bucket HEAD, put+get+delete probe object `._cfs_probe`, SSE echo, clock-skew check, deprecated-hook detector §1.6) | none |
| Analyze Storage | `analyze_storage()` | CSM/SM | no campaign in Running/Cleanup Running | enqueues analysis on `cloud_migration` queue (falls back to `long` + warning if queue unconfigured — frappe-jobs-backups §1.2); progress via realtime; result renders counts (dupes, shared URLs, shared hashes, missing files, orphans, remote URLs, corrupt metadata) | none |
| Build Migration Plan | `build_migration_plan(config: dict)` | CSM/SM | analysis complete; no active campaign | dialog (batch size default 1000, parallelism, bandwidth cap MB/s, scope filters) → creates Campaign(Draft)+Batches → route to campaign | none |
| Start | `start_campaign(name)` | CSM/SM | Planned | `frappe.confirm` summary (N items, M GB, mode) → call → live progress | soft confirm |
| Pause | `pause_campaign(name)` | CSM/SM | Running | sets Paused; in-flight batch finishes (cooperative check per item-chunk) | none |
| Resume | `resume_campaign(name)` | CSM/SM | Paused, Failed | re-derives work from DB state (Redis is a hint only — frappe-jobs-backups §8) | none |
| Stop After Current Batch | `stop_after_current_batch(name)` | CSM/SM | Running | sets Stopping; coordinator exits after batch commit | soft confirm |
| Retry Failed | `retry_failed(name)` | CSM/SM | Running, Paused, Stopped, Failed | re-queues Failed items (attempt++ ≤ max); shows count first | soft confirm |
| Verify | `start_verify(name)` | CSM/SM | Running, Paused, Stopped, Failed | launches VERIFY phase (independent head_object + checksum per item) | none |
| Reconcile | `reconcile()` | SM | no Running campaign | bucket↔CSO↔File three-way diff job; report doc + orphan list (paginated, unlike DFP's unpaginated page — dfp-audit AVOID) | none |
| Delete Verified Local Copies | = `approve_cleanup(name)` then `start_cleanup(name, confirm_phrase)` (two audited actions, A9) | **SM only** | Running w/ all batches Verified AND mode ∈ {S3_PRIMARY_LOCAL_FALLBACK, S3_ONLY} (ADR-M16); server recomputes `COUNT(objects NOT IN ('Verified','DedupReused','Skipped')) == 0`, never trusts counters | `frappe.prompt` type-to-confirm | **type `DELETE LOCAL FILES`**; server compares literal phrase; logs actor+time on campaign |
| Export Migration Report | `export_migration_report(name)` | CSM/SM | any terminal/paused | server builds CSV/XLSX of items+conflicts+timings → private File → download | none |
| Preview/Apply Lifecycle | §2.3 | SM | — | on Cloud Backup Settings | type `APPLY LIFECYCLE` |
| Backup Now | §2.2 | CSM/SM | enabled | freeze + toast with log link | none |

Client JS lives in `cloud_storage_settings.js` (refresh handler adds custom buttons grouped under "Migration" / "Storage"; binds `frappe.realtime.on("cfs_campaign_progress", ...)` to update `migration_html`).

### 3.4 Dashboards — decision
**Workspace "Cloud Storage"** (static entry): Number Cards (CSO by status: Live/Uploading/Failed/Orphaned; Files migrated %; Local bytes remaining; Cache size; Last backup age) + shortcuts (Settings, Campaigns, Conflicts, Backup Log, Migration Dashboard). Number Cards are `document_type`-count based — fine for slow-moving counts.
**Custom Page `cloud-migration-dashboard`** for live migration ops. Rationale (ADR): Number Cards/Charts poll and cannot render a batch stream or per-phase progress; `publish_realtime` shares the queue Redis so updates must be throttled per-batch (frappe-jobs-backups §8.2) and a Page is the only surface that can subscribe to those events; DFP precedent (`dfp_s3_bucket_list` page) confirms the pattern. Page contents: campaign selector, phase pipeline (UPLOAD → VERIFY → CLEANUP with counts), throughput sparkline (items/min, MB/min from batch completion events), failed-item table with retry action, conflict-queue table, ETA. Events: coordinator publishes `cfs_campaign_progress` **once per batch completion** with `{campaign, phase, done, total, failed, bytes_done, rate}` — never per file.
Storage Health (embedded in Settings `storage_health_html`, refreshed by `get_storage_health()` endpoint): mode chip, CSO status counts, orphan count from last reconcile + its age, cache stats (entries/bytes/hit-rate), last backup age, config warnings (queue unconfigured, deprecated hook present, file backups still on in S3 modes).

### 3.5 UI failure modes
| Failure | Mitigation |
|---|---|
| Realtime lost (socketio down) | Page also polls `get_campaign_snapshot(name)` every 30 s; realtime is enhancement only |
| Two operators click Start concurrently | server-side `filelock("cfs_campaign_control")` (states per the frozen A9 machine) (`frappe/utils/synchronization.py:17-49`) around state transitions + status re-read inside lock |
| Button state drift (stale form) | every endpoint re-validates state; JS refreshes doc on `ValidationError` |
| Delete-local clicked with partial verify | server recomputes `COUNT(objects NOT IN ('Verified','DedupReused','Skipped')) == 0` in SQL inside the lock; counter fields are display-only |

---

## 4. Testing

### 4.0 Layer definitions
- **L1 unit** (`tests/unit/`): pure `unittest.mock`, no DB writes beyond FrappeTestCase rollback; runs in the plain CI job.
- **L2 MinIO integration** (`tests/integration/`): bench site + live MinIO (docker), gated by `RUN_MINIO_INTEGRATION_TESTS=1` + `CLOUD_FILE_STORAGE_MINIO_*` env (preserving the fork's opt-in pattern, repo-audit §9); exercises real byte round-trips.
- **L3 ecosystem** (`tests/ecosystem/`): bench site with erpnext+hrms+india_compliance installed (+ MinIO); exercises real consumer code paths.
- **Contract module** (`tests/contract/test_file_compat_contract.py`): the 19 invariants of content-consumers-contract §5 as tests **C1–C19**, each docstring citing the strictest caller file:line. Runs at L2 (needs real File rows + storage) — this module is the release gate; a PR that changes `core/` cannot merge with any C-test skipped.

### 4.1 Full matrix (mission item → test)
| ID | Scenario | File / class | Layer |
|---|---|---|---|
| T-PUB | public upload → serve via CDN URL / correct object | `integration/test_upload_paths.py::TestPublicUpload` | L2 |
| T-PRIV | private upload → `/private/files/...` → perm check → 302 signed URL; Guest 403 | `integration/test_private_serving.py::TestSignedRedirect` | L2 |
| T-PERM | private permission enforcement incl. attached-doc read cascade (`file.py:843-875`) and shared-URL `fid` resolution | `integration/test_private_serving.py::TestPermissionCascade` | L2 |
| T-DUP1 | same name + different bytes ⇒ 2 CSOs, distinct keys | `integration/test_identity.py::TestSameNameDiffBytes` | L2 |
| T-DUP2 | same name + same bytes ⇒ 1 CSO, 2 File rows, refcount 2 | `integration/test_identity.py::TestSameNameSameBytes` | L2 |
| T-DUP3 | different name + same bytes ⇒ 1 CSO reused | `integration/test_identity.py::TestDiffNameSameBytes` | L2 |
| T-DEL1 | delete one of two sharing Files ⇒ object survives, refcount 1 | `integration/test_delete.py::TestSharedObjectDelete` | L2 |
| T-DEL2 | delete last reference ⇒ object deleted (or tombstoned per runtime design) | `integration/test_delete.py::TestLastReferenceDelete` | L2 |
| T-AMEND | duplicate/amend/copy doc → `copy_attachments_from_amended_from` (`document.py:446-464`) re-reads via get_content, dedups, no new object | `integration/test_frappe_flows.py::TestAmendCopy` | L2 |
| T-ATTACH | Attach field set → `attach_files_to_document` links File (contract #9: url canonical) | `integration/test_frappe_flows.py::TestAttachField` | L2 |
| T-ATTIMG | Attach Image + thumbnail generation (`make_thumbnail`, thumbnails land public — frappe-file-core §8) | `integration/test_frappe_flows.py::TestAttachImageThumbnail` | L2 |
| T-ITEMIMG | erpnext Item image + website thumbnail | `ecosystem/test_erpnext_flows.py::TestItemImage` | L3 |
| T-EMAIL-OUT | outbound email attachment bytes (`email_body.py:251`) | `integration/test_frappe_flows.py::TestEmailAttachment` | L2 |
| T-EMAIL-IN | inbound email attachment save (`receive.py:581-598`, path (a) private) | `integration/test_frappe_flows.py::TestInboundEmail` | L2 |
| T-DATAIMP | Data Import stays local (ignored doctype seed) + import reads xlsx/csv | `integration/test_frappe_flows.py::TestDataImport` | L2 |
| T-PREPREP | Prepared Report gz write + `gzip.decompress(get_content())` (`prepared_report.py:96-97`) | `integration/test_frappe_flows.py::TestPreparedReport` | L2 |
| T-RIV | Repost Item Valuation: `create_json_gz_file` write-through-path + sentinel mootness (`stock_ledger.py:407-424,416`) | `ecosystem/test_erpnext_flows.py::TestRepostItemValuation` | L3 |
| T-ZIP | `File.unzip()` on S3-backed zip via materialized path (`file.py:533`) | `integration/test_materialize.py::TestUnzip` | L2 |
| T-IMG | image serve + optimize_file overwrite-in-place (contract #11) | `integration/test_frappe_flows.py::TestOptimizeOverwrite` | L2 |
| T-UNI | Unicode filename (`Pflanzenrückgabe.pdf`) round-trip, RFC-5987 disposition | `integration/test_upload_paths.py::TestUnicodeFilenames` | L2 |
| T-LARGE | 120 MB file multipart upload + streamed serve (no full-RAM read) | `integration/test_large_files.py` | L2 |
| T-API | `/api/method/upload_file` (guest + session) + legacy `file_manager.save_file` path (b) signature polymorphism (contract #12) | `integration/test_upload_paths.py::TestApiUpload` | L2 |
| T-IC-RAW | `get_content(encodings=[])` returns identical raw gz bytes; no-arg returns str-for-UTF8/bytes-otherwise; `encodings=['utf-8']` v16 semantics | contract C1–C4 in `tests/contract/` | L2 |
| T-IC-FLOW | replicate `gst_return_log` semantics without IC: private gz File attached via `attached_to_field`, `save_file(content=..., overwrite=True)` + `db_set(file_field, file_url)`, then read back + decompress; FileNotFoundError only when object truly absent (C5) | `unit/test_ic_semantics.py` (mock storage) + contract C5/C11 | L1+L2 |
| T-IC-LIVE | with IC installed: import `gst_return_log`, drive `create/attach/get_json_for/update_json_for` against S3-backed files; e-Waybill path-(b) `save_file` | `ecosystem/test_india_compliance.py` | L3 |
| T-OUTAGE | S3 outage (endpoint refused): upload fails loudly per mode (DUAL_WRITE degrades per runtime spec, S3_ONLY errors typed, never `PageDoesNotExistError`-swallowed — dfp-audit AVOID #2), read falls back per mode | `integration/test_failure_modes.py::TestS3Outage` (MinIO stopped/port firewall via wrong-endpoint client injection) | L2 |
| T-RESTART | Redis flush + worker kill mid-campaign ⇒ resume re-derives from DB (frappe-jobs-backups §8 conclusions) | `integration/test_migration_resume.py::TestRedisLoss` | L2 |
| T-INTR | kill batch job mid-UPLOAD ⇒ idempotent re-run, no dup objects, no lost items | `integration/test_migration_resume.py::TestInterrupt` | L2 |
| T-RETRY | failed item retry increments attempt, honors max, lands in conflict queue after | `unit/test_migration_states.py` + `integration/test_migration_resume.py` | L1+L2 |
| T-CHKSUM | corrupt remote object (overwrite object bytes out-of-band) ⇒ VERIFY fails item, CLEANUP blocked | `integration/test_verify_gate.py::TestChecksumFailure` | L2 |
| T-PAUSE | pause/resume mid-batch: no item processed twice, counters exact | `integration/test_migration_resume.py::TestPauseResume` | L2 |
| T-GATE | cleanup gate: local delete refused unless 100% verified (server recompute) | `integration/test_verify_gate.py::TestCleanupGate` | L2 |
| T-LEGACY | legacy `/files/...` URLs untouched by migration (no rename of healthy URLs); old `/api/method/frappe_s3_attachment...` URL 302-aliased | `integration/test_legacy_urls.py` + `unit/test_compat_patch.py` | L1+L2 |
| T-BACKUP | backup task: upload verified, checksum-mismatch ⇒ Failed, lifecycle economics validator table-driven cases | `unit/test_lifecycle.py` (pure math) + `integration/test_backup.py` | L1+L2 |
| T-RENAME | compat patch guards (fresh site no-op) + synthetic legacy-state full run | `unit/test_compat_patch.py` + `integration/test_compat_patch_legacy_site.py` | L1+L2 |

### 4.2 Testing the 19 invariants
`tests/contract/test_file_compat_contract.py` — one test per invariant, IDs C1..C19 mapped 1:1 to content-consumers-contract §5 (C1 encodings kwarg accepted; C2 `encodings=[]` raw bytes; C3 bytes for PNG/PDF/gz/xlsx/zip; C4 str for UTF-8 text; C5 FileNotFoundError only on true absence — transient S3 error raises `CloudStorageTransientError`; C6 save/validate of remote-only File; C7 get_full_path openable by `open/zipfile/Image/load_workbook/csv`; C8 write-back through materialized path; C9 canonical file_url shape asserted after every operation in the module; C10 permission gate + `make_access_log` fires (assert via Access Log row); C11 overwrite=True stable URL; C12 both write_file conventions; C13 delete via hook, refcount-safe; C14 exists_on_disk True for remote twin; C15 MD5 content_hash preserved; C16 is_private toggle without shutil.move + propagation; C17 stock_ledger sentinel fallthrough safe (unit-simulated at L2; real at T-RIV L3); C18 file_url stored in business fields resolvable; C19 marked not-in-scope with an assertion documenting why). CI marks this module `--failfast`-eligible and required.

### 4.3 India Compliance without IC installed
L1/L2 tests never import IC. They encode IC's *call semantics* extracted from the contract report: exact kwargs (`encodings=[]`), exact exception filter (`except FileNotFoundError` at `gst_return_log.py:57` — hence C5's transient-error rule), gzip payloads, `overwrite=True` + `db_set` URL persistence. The L3 job (feasible per rename-and-bench-reality §5: hrms/IC `install.sh` pattern — clone frappe pinned, `bench init --frappe-path`, `bench get-app` erpnext/hrms/india_compliance at matching branches) imports IC modules directly and exercises them against S3-backed Files. L3 runs on one frappe ref only (version-15) to bound CI time.

### 4.4 Local test runbook (preserved + extended from the fork, README.md:76-96)
`docs/runbooks/local-testing.md`: `docker run -d -p 9000:9000 quay.io/minio/minio:latest server /data --address ":9000"` (minioadmin/minioadmin); create bucket `cloud-file-storage-test`; export `RUN_MINIO_INTEGRATION_TESTS=1` + 5 `CLOUD_FILE_STORAGE_MINIO_*` vars; `bench --site test_site set-config allow_tests true`; `bench --site test_site run-tests --app cloud_file_storage`. Document the HTTPS-validation caveat successor: Settings `validate()` allows `http://` endpoints only when `frappe.conf.allow_tests` (replacing the fork's inject-settings workaround, repo-audit §9).

---

## 5. CI/CD

### 5.0 Version floor — decision
**Minimum supported: frappe v15.16.0** (A34: `find_file_by_url`/`fid` first exist at v15.16.0; the earlier v15.0.0 claim was refuted). Verified directly against tags: `get_hook_method("write_file")` (modern File-instance convention) present since commit `b87f31a94e` (contained in **v14.0.0**); `delete_file_data_content` hook ancient; at tag `v15.0.0`: both hooks present (`file.py:660,690`), `download_private_file` dispatch + `before_request` hooks present (`app.py:116,190-191`), `enqueue(deduplicate=)` present (`background_jobs.py:71,91`), `frappe/utils/synchronization.py` filelock present. Matrix: `{v15.16.0 (floor, A34), v15.93.0 (bench rev), version-15 (tip)}`. `supported-versions.md` documents the floor + the verification method so future bumps re-run it.

### 5.1 `.github/workflows/ci.yml` (rewrite)
Shared `.github/helper/install.sh` (hrms pattern, `hrms/.github/helper/install.sh:13-14`): `git clone https://github.com/frappe/frappe --branch "$FRAPPE_REF" --depth 1 ~/frappe` → `bench init --skip-assets --frappe-path ~/frappe --python "$(which python)" ~/frappe-bench` → services mariadb 11.8 + redis (ports as today) → `bench get-app cloud_file_storage $GITHUB_WORKSPACE` → `bench new-site test_site` → `install-app cloud_file_storage` → `set-config allow_tests true`. Python 3.10 + 3.12 on the floor/tip cells respectively (drop the current py3.14 mismatch, repo-audit §10); Node 20.

Jobs:
1. **unit-and-integration** — `strategy.matrix.frappe_ref: [v15.16.0, v15.93.0, version-15]` (A34), `fail-fast: false`; runs `bench run-tests --app cloud_file_storage --coverage` excluding `tests/integration tests/ecosystem` (marker/env-gated as today); uploads coverage.
2. **minio** — single ref `version-15`; MinIO container + bucket bootstrap (port/health-poll steps carried over from current `ci.yml:166-204`, renamed envs); runs full suite incl. `tests/integration` + contract module with `RUN_MINIO_INTEGRATION_TESTS=1`.
3. **ecosystem** — single ref `version-15`; `install.sh` additionally `bench get-app erpnext --branch version-15`, `hrms`, `payments`, `india_compliance` (branch mapping per IC's own workflow: frappe branch tracks target branch, `server-tests.yml:32/107-109`); installs all; runs `tests/ecosystem` + contract module. `continue-on-error: false` — this is the IC-mandatory gate.
4. **lint** (from linter.yml, kept): commitlint, `semgrep ci --config frappe-semgrep-rules --config r/python.lang.correctness`, `pip-audit --desc on .`, pre-commit. **codeql.yml** kept as-is with branch names updated.
5. **coverage-gate** — needs 1+2; combines, fails under **80% line coverage on `cloud_file_storage/` package** and under **100% of contract-module tests executed** (no silent skips: assert junit contains C1..C19).
Concurrency group `ci-cloud_file_storage-${{ github.ref }}`. Sharding: not initially (≈150–250 tests); documented trigger — if wall-clock > 25 min, adopt `run-parallel-tests --total-builds N` with `strategy.matrix.container` (erpnext pattern, rename §5).

### 5.2 Supported-version matrix document (`docs/supported-versions.md`)
Table format: rows = app releases; columns = frappe floor / max tested / bench-verified rev / erpnext / india_compliance / python; plus "surfaces relied on" appendix listing each hooked/overridden frappe symbol with file:line at floor and tip (generated once per release from the contract module's docstrings).

### 5.3 Release checklist automation
`docs/release-checklist.md` + a `workflow_dispatch` release workflow stub: (1) version bump in `__init__.py` + CHANGELOG section; (2) full matrix green incl. ecosystem; (3) contract module green; (4) `bench generate-pot-file` regenerated; (5) tag `vX.Y.Z`; (6) supported-versions.md refreshed. Semantic-release (upstream branch `ci-semantic-release`, repo-audit §10) deferred — manual keep-a-changelog is lower risk during 1.0 development (ADR).

---

## 6. Docs & Deliverables

### 6.1 `docs/` tree
```
docs/
├── architecture.md              # component map, mode matrix, serving flows, CSO model, sentinel note (§1.5)
├── source-comparison.md         # ALYF fork vs DFP vs cloud_file_storage; rows: integration seam (doc_events vs
│                                #   override+hooks), identity/key scheme, dedup/refcount, serving, get_content
│                                #   signature, migration tooling, verification, modes, backup, tests — cells cite
│                                #   repo-audit/dfp-audit file:lines
├── threat-model.md              # assets (bytes, creds, signed URLs), actors, STRIDE per surface: upload,
│                                #   signed-URL issuance (expiry/scope), before_request alias (open-redirect guard:
│                                #   only exact legacy prefix, same-origin target), bucket policy (no public ACLs),
│                                #   SSE/KMS, IAM least-privilege policy JSON, audit trail (Access Log + Backup Log)
├── performance-testing.md       # 1.2M-row synthetic dataset generator, throughput targets, queue-Redis memory
│                                #   budget (no maxmemory! frappe-jobs-backups §8), content_hash index requirement
├── release-checklist.md
├── supported-versions.md
├── runbooks/
│   ├── deployment.md            # workers.cloud_migration prerequisite + restart requirement (lru_cache,
│   │                            #   background_jobs.py:41), supervisor/Procfile lines, nginx notes (X-Accel,
│   │                            #   /files CDN cutover), bench-level rename steps (§1.2.15), apps.txt/apps.json/.pth
│   ├── migration.md             # campaign lifecycle, pause/stop semantics, conflict triage, cleanup gates
│   ├── backup-restore.md        # §2.5/§2.6 + crontab coexistence
│   ├── rollback.md              # per-phase rollback: mode downgrade path S3_ONLY→FALLBACK→DUAL_WRITE→LOCAL,
│   │                            #   re-materialization procedure, compat-patch reversal notes
│   └── local-testing.md         # §4.4 MinIO runbook
├── adr/                         # §6.2
├── PROGRESS.md                  # per-phase status table (phase, owner-agent, state, PR, gate sign-off)
└── DECISIONS.md                 # append-only log: date, decision, alternatives, deciding agent, links to ADR
```

### 6.2 Initial ADR set (numbered; owners in brackets)
0001 Use `write_file`/`delete_file_data_content` hooks + minimal `override_doctype_class` File subclass, not doc_events [runtime] · 0002 Cloud Storage Object as single source of physical identity; key = immutable UUID-bearing, content-hash-indexed, filename never identity [runtime] · 0003 `get_content(self, encodings=None)` superset semantics [runtime] · 0004 file_url stays canonical `/files|/private/files`; signing at serve time via before_request interception [runtime] · 0005 refcount-or-equivalent with row-locked delete gate [runtime] · 0006 materialization cache with write-back detection [runtime] · 0007 s3_key_generator not honored (R5) [product] · 0008 greenfield rename, fresh doctypes, guarded pre_model_sync compat patch (R2/R7) [product] · 0009 keep `s3_object_key` field, add `cloud_storage_object` link (R4) [product] · 0010 dedicated `cloud_migration` queue, batched jobs, DB-backed state, Redis as dispatch hint [migration] · 0011 UPLOAD/VERIFY/CLEANUP phase separation with independent verification before any local delete [migration] · 0012 conflict queue instead of in-place fixes for legacy anomalies; healthy URLs never renamed [migration] · 0013 separate Cloud Backup Settings Single (B1) [product] · 0014 lifecycle generator with economics validator + sync-retrieval hard guard (B5) [product] · 0015 custom Page + throttled publish_realtime for migration dashboard (§3.4) [product] · 0016 frappe floor v15.16.0 (A34), 3-point matrix (§5.0) [product] · 0017 Cloud Storage Manager role + permlevel-guarded credentials (§3.2) [product] · 0018 contract test module as merge gate (§4.2) [product] · 0019 override_whitelisted_methods remap (302; A10/A11) for legacy /api/method URLs [product] · 0020 IAM default credential chain preferred; static keys optional; SSE-S3 default [runtime].

### 6.3 Repo `INVARIANTS.md` (content outline)
Build/test: `bench --site test_site run-tests --app cloud_file_storage`; MinIO env block; lint `pre-commit run -a`. Architecture map: 5-line component diagram + "read docs/architecture.md first". **Invariants agents must not break** (verbatim list): the 19 contract invariants by ID; "never write `file_url` outside canonical shapes"; "never delete local bytes without independent remote verification"; "no raw SQL on tabFile/CSO outside `migration/sql.py`"; "all new queue work targets `cloud_migration`"; "credentials never logged"; "every schema change ships a patch + test". Pointers: DECISIONS.md conventions, phase gates ("a coding agent cannot approve its own work").

### 6.4 the delivery role definitions definitions
| File | Role | Tools | When |
|---|---|---|---|
| `runtime-engineer.md` | File subclass/CSO/serving impl | Read/Edit/Bash(bench,pytest) | phases 2–4 |
| `migration-engineer.md` | campaign engine impl | same | phases 5 |
| `product-engineer.md` | settings/UI/backup/CI impl | same + browser (playwright MCP) for Desk verification | phases 1,6,7 |
| `test-engineer.md` | writes/extends matrix tests, owns contract module; may not modify `core/` | Read/Edit(tests,docs)/Bash | every phase gate |
| `reviewer.md` | independent review gate; read-only + ReportFindings; blocks merge on contract/matrix gaps | Read/Bash(ro)/ReportFindings | every phase gate |
| `release-manager.md` | changelog, versioning, supported-versions refresh | Read/Edit(docs)/Bash(git,gh) | phase ends |

### 6.5 PROGRESS/DECISIONS conventions
`PROGRESS.md`: one table row per phase — `Phase | Scope ref | Status (todo/in-progress/in-review/done) | Impl agent | Review agent | Test evidence (CI run link) | Date`. Updated only by the agent completing the transition. `DECISIONS.md`: append-only; entries ≤5 lines; anything architectural graduates to a numbered ADR file; conflicts between agents escalate by writing a DECISIONS entry tagged `NEEDS-RULING` for the orchestrator.

---

## 7. Phase Plan & Backlog

Dependency graph: SUPERSEDED by PLAN.md §B — `P0 → P0.5 → P1 → P2 → P3 → P4 → P5 → {P6 ∥ P7} → integration merge/retest → P8 → FINAL RELEASE AUDIT`; P6 and P7 run in parallel (disjoint ownership: `backup/` vs `api/admin.py`+UI); parts of P7 (dashboard page) can start once P5's state machine lands. Every phase ends committable + green; gate = test-engineer runs the phase's verification suite + reviewer signs PROGRESS.md (implementer never self-approves).

| Phase | Scope & deliverables | Acceptance criteria | Verification | Parallel? |
|---|---|---|---|---|
| **P0 Unbreak bench** (ops, no commit) | §1.1 `.pth` fix | all live sites `list-apps` OK; documented in runbook draft | manual bench loop (skip skc.local) | — |
| **P1 Rename + CI baseline** | §1.2 items 1–15; §5 ci.yml + install.sh; pre-commit/gitignore; README/CHANGELOG/license; delete controller-era dead code; legacy patches rewritten | fresh-site install works; grep-gate clean; matrix job green on 3 frappe refs with the (temporarily trimmed) existing test set; lint green | CI run; `unit/test_compat_patch.py` guards | single agent (product-engineer) |
| **P2 Runtime core** (sibling spec) | CSO doctype, File subclass, write/delete hooks, LOCAL_ONLY+DUAL_WRITE, materialization cache | contract C1–C9, C11–C16 pass at L2; T-DUP*, T-AMEND, T-ATTACH pass | minio CI job | runtime-engineer; test-engineer writes contract module concurrently against the spec |
| **P3 Serving + modes** | post-validate_auth `download_private_file` interception (ruling 1; before_request = fallback only), signed URLs, CDN public, S3_PRIMARY_LOCAL_FALLBACK, S3_ONLY, legacy-URL alias | C10, C17(sim), T-PRIV/T-PERM/T-OUTAGE/T-LEGACY pass; Access Log verified | minio CI | runtime-engineer |
| **P4 Ecosystem hardening** | fixes surfaced by L3; ecosystem CI job added | T-RIV, T-ITEMIMG, T-IC-LIVE green; ecosystem job required in branch protection | ecosystem CI | runtime + test engineers |
| **P5 Migration engine** (sibling spec) | campaign doctypes, analyzer, planner, phased executor on `cloud_migration`, conflict queue | T-INTR/T-RESTART/T-RETRY/T-CHKSUM/T-PAUSE/T-GATE green; 100,000-file synthetic rehearsal in perf harness completes with kill/resume (FD-2) | minio CI + perf script | migration-engineer ∥ P7-dashboard stub by product-engineer |
| **P6 Backup & lifecycle** | §2 complete: settings, tasks, log, lifecycle generator+validator, restore hooks | T-BACKUP suite green; economics validator table-driven cases; apply-guard tests (wrong bucket, archive-class-on-live refused) | unit + minio CI | product-engineer (disjoint from P7 files) |
| **P7 Desk UI** | §3 complete: settings form+JS, buttons wired with state validation, role, workspace, dashboard page, health panel | every button's server state-validation has a unit test; type-to-confirm server checks tested; playwright smoke: settings loads, test-connection renders, campaign start→pause on 100-file fixture | minio CI + playwright script | product-engineer |
| **P8 Compat patch, docs, release** | §1.4 patch + synthetic legacy-site test; full docs/ tree; threat model; runbooks; supported-versions; v1.0.0 tag | T-RENAME green; docs build/links checked; release checklist executed; CHANGELOG 1.0.0 | full CI matrix + release workflow | release-manager + reviewer |

Backlog (post-1.0, recorded in DECISIONS.md): presigned direct-to-S3 chunked upload endpoint (bypasses 25 MB Werkzeug clamp, frappe-jobs-backups §7); multi-bucket routing rules; content_hash DB index upstream PR (frappe-file-core §5 full-scan risk); Sentinel/HA queue Redis; semantic-release.

---

### Critical Files for Implementation
- /home/user/v15/apps/cloud_file_storage/frappe_s3_attachment/hooks.py (rename ground zero: app_name/doc_events to be replaced)
- /home/user/v15/apps/cloud_file_storage/frappe_s3_attachment/controller.py (every legacy surface being dissolved/relocated)
- /home/user/v15/apps/cloud_file_storage/.github/workflows/ci.yml (full rewrite target incl. frappe pinning/matrix)
- /home/user/v15/apps/cloud_file_storage/frappe_s3_attachment/install.py (custom field/index/role/seed idempotency carried forward)
- /home/user/v15/apps/frappe/frappe/utils/backups.py (new_backup/ignore_files contract the backup module wraps)
- /home/user/v15/apps/frappe/frappe/model/sync.py (remove_orphan_doctypes — the constraint that shapes the compat patch)