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

# Cloud File Storage — Runtime Storage Architecture & Frappe Integration Design

Target: `cloud_file_storage` app (rename of `frappe_s3_attachment` 0.2.2 fork) on frappe v15.93.0, bench `/home/user/v15`. All contract references `#N` are the 19 invariants in `content-consumers-contract.md §5`. Research citations use the shorthand `[repo]`, `[dfp]`, `[core]`, `[jobs]`, `[contract]`, `[rename]` for the six reports.

---

## 0. Architecture Decision Records (summary)

| # | Decision | Rationale (one line) |
|---|---|---|
| ADR-1 | `file_url` stays canonical `/files/...` / `/private/files/...` forever; signed URLs are issued only at serve time | Contract #9: `attach_files_to_document` (`file/utils.py:326`), `set_is_private` (`file.py:784`), `find_file_by_url` exact-match (`file/utils.py:430-445`) all break on any other shape. |
| ADR-2 | Integration seams = `write_file` + `before_write_file` + `delete_file_data_content` hooks (the correct v15 seams, `file.py:711-714`, `:743-747`) + minimal `override_doctype_class` subclass for read-side methods hooks cannot cover | [core §2] proves these are the only three File-lifecycle hooks; the fork's `after_insert` seam writes-then-uploads and cannot express modes [core risk #15]. |
| ADR-3 | S3 keys are content-addressed: `{site}/{pub\|prv}/{sha256[0:2]}/{sha256[2:4]}/{sha256}` — no filename, no doctype, no PII | Immutable identifier = sha256; same name+different bytes separate; same bytes converge to one object; objects are never mutated in place, so DB rollback restores pointers for free (fixes core `on_rollback` disk-writes, `file.py:163-195`). |
| ADR-4 | Refcount = **query-at-delete over `tabFile.cloud_storage_object`, serialized by `SELECT ... FOR UPDATE` on the CSO row, with deferred GC** (never inline S3 delete) | Derived counts cannot drift (raw `frappe.db.delete` calls bypass hooks); the row lock closes the check-then-act race DFP has (`dfp-audit §6`); deferred GC converts residual races into recoverable states. |
| ADR-5 | Private serving primary = module-attribute patch of `frappe.utils.response.download_private_file`; `before_request`-raise kept as documented fallback only | The patch executes at dispatch time (`app.py:126`), i.e. **after** `validate_auth()` (`app.py:107`), so token-auth API clients are authenticated; `before_request` runs inside `init_request` (`app.py:105`, `:210-211`) *before* `validate_auth`, so token users would still be Guest there. Verified in `app.py:100-135, 195-211`. |
| ADR-6 | Public serving = local-hit fast path (nginx/`StaticDataMiddleware`) + miss fall-through to a `page_renderer` that 302s to CDN/presign | Verified: bench nginx template `location / { try_files /{{site}}/public/$uri @webserver; }` (`bench/config/templates/nginx.conf:80-92`) — a public miss **does** reach Python in production; dev needs a guarded middleware patch (see §8.2). |
| ADR-7 | One **private** bucket; visibility encoded in the key prefix (`pub/` vs `prv/`); CloudFront OAC behavior restricted to `pub/*` — **no `public_bucket` override (A24/FD-6: single private bucket)** | No public-read ACLs anywhere (fork's `ACL: public-read` at `controller.py:162` is the #1 security bug [repo §4]); prefix split means CSO dedup granularity = (sha256, visibility), which exactly matches core's `(content_hash, is_private)` dedup (`file.py:686-692`). |
| ADR-8 | `get_content(self, encodings=None)` superset semantics; typed exceptions; `FileNotFoundError` only on true absence | Contract #1-#5; DFP anti-patterns #1-#5 (`dfp-audit §8`) explicitly avoided. |
| ADR-9 | Legacy `File.s3_object_key` field is **not created** on this bench (0 installs, [rename §1]); a guarded compat patch converts it to CSO rows for external 0.2.x installs | Greenfield rename; `override_whitelisted_methods` remap (verified live at `handler.py:67`, `api/v2.py:36`, `frappe/__init__.py:2521`) keeps old `/api/method/frappe_s3_attachment.controller.generate_file` URLs alive without shipping the old package. |
| ADR-10 | Materialization cache lives at `sites/{site}/cloud_storage_cache/`, per-**File** entries, sidecar metadata, filelock-guarded, write-back at `after_request`/`after_job` | Outside `public/`,`private/files` so it is never web-served and never tarred by `BackupGenerator.backup_files` (which only tars `{public,private}/files`, `backups.py:349-373` [jobs §5]). |
| ADR-11 | **SUPERSEDED by ADR 0021 (PLAN.md §A thumbnail policy)**: thumbnails are cloud-managed DERIVED OBJECTS of the source CSO (`thm/` prefix, `derived_of` link) — migrated/regenerated after source verification; a local thumbnail is never deleted until the source object is verified AND thumbnail availability is guaranteed; no permanent local thumbnail persistence post-S3_ONLY; `make_thumbnail` renders via bounded materialization then uploads the derived object. |
| ADR-12 | CSO named by UUID (controller `autoname`), identity enforced by unique index on `s3_key` + `(content_sha256, visibility, bucket)` | v15 has no `autoname: UUID` (verified `naming.py:275-280`); identity must live in unique constraints, not names; name stays opaque/immutable. |
| ADR-13 | `file_size` on CSO = **Long Int** | MariaDB `Int` = `int(11)` (verified `frappe/database/mariadb/database.py:177`); >2 GB objects overflow (DFP lesson, `remote_size_enabled`). |

---

## 1. Package Layout

```
/home/user/v15/apps/cloud_file_storage/
├── pyproject.toml                      # name = "cloud_file_storage"
├── license.txt                         # MIT, keep zerodha + ALYF attribution, add Finstein
└── cloud_file_storage/                 # python package (renamed from frappe_s3_attachment/)
    ├── __init__.py                     # __version__ = "1.0.0"
    ├── hooks.py                        # see §1.1
    ├── modules.txt                     # "Cloud File Storage"
    ├── patches.txt
    ├── install.py                      # after_install/after_migrate: indexes, seeds, queue check
    ├── core_hooks.py                   # write_file / before_write_file / delete_file_data_content
    ├── doc_events.py                   # File.after_insert ensure_cso_link etc.
    ├── overrides/
    │   └── file.py                     # class CloudFile(File)
    ├── storage/
    │   ├── engine.py                   # StorageEngine facade (put/get/head/verify/presign/copy/delete)
    │   ├── client.py                   # cached boto3 session/client, TransferConfig, ExtraArgs builder
    │   ├── keys.py                     # build_object_key(), parse_object_key()
    │   ├── hashing.py                  # streaming md5+sha256 in one pass
    │   ├── objects.py                  # ensure_cso(), lock_cso(), live_reference_count(), state transitions
    │   ├── modes.py                    # OperationMode enum + get_mode() + transition validation
    │   └── exceptions.py               # typed exception hierarchy
    ├── serving/
    │   ├── runtime_patches.py          # install/uninstall the two monkeypatches (idempotent)
    │   ├── private.py                  # patched download_private_file + presign policy
    │   ├── public.py                   # PublicFileRenderer (page_renderer)
    │   └── disposition.py              # inline-vs-attachment policy, RFC-5987 helpers
    ├── cache/
    │   ├── materialize.py              # materialize(file_doc) -> path; sidecar; filelock
    │   ├── writeback.py                # flush_request/flush_job sync-back
    │   └── eviction.py                 # scheduled LRU/TTL eviction
    ├── api/
    │   ├── __init__.py
    │   └── compat.py                   # legacy_generate_file (alias target), test_connection
    ├── migration/                      # migration-domain agent owns internals; layout reserved:
    │   ├── engine.py, scanner.py, verify.py, cleanup.py, conflicts.py
    ├── gc.py                           # deferred object GC + orphan sweep (scheduler)
    ├── cloud_file_storage/             # module dir (scrub of "Cloud File Storage")
    │   └── doctype/
    │       ├── cloud_storage_settings/         # Single (successor of S3 File Attachment)
    │       ├── cloud_storage_ignored_doctype/  # child table
    │       ├── cloud_storage_object/           # the CSO
    │       ├── cloud_migration_campaign/       # migration domain
    │       ├── cloud_migration_batch/
    │       └── cloud_migration_conflict/
    ├── patches/
    │   └── v1_0/
    │       ├── __init__.py
    │       └── compat_rename_from_frappe_s3_attachment.py   # guarded no-op here, [pre_model_sync]
    └── tests/  (ported + new: contract tests, minio integration)
```

### 1.1 hooks.py (live entries only)

```python
app_name = "cloud_file_storage"
app_title = "Cloud File Storage"

after_install = "cloud_file_storage.install.after_install"
after_migrate = "cloud_file_storage.install.after_migrate"

override_doctype_class = {"File": "cloud_file_storage.overrides.file.CloudFile"}

write_file = "cloud_file_storage.core_hooks.write_file"
before_write_file = "cloud_file_storage.core_hooks.before_write_file"
delete_file_data_content = "cloud_file_storage.core_hooks.delete_file_data_content"

doc_events = {"File": {"after_insert": "cloud_file_storage.doc_events.file_after_insert"}}

before_request = ["cloud_file_storage.serving.runtime_patches.ensure_installed"]
after_request  = ["cloud_file_storage.cache.writeback.flush_request"]
after_job      = ["cloud_file_storage.cache.writeback.flush_job"]

page_renderer = ["cloud_file_storage.serving.public.PublicFileRenderer"]

override_whitelisted_methods = {
    "frappe_s3_attachment.controller.generate_file":
        "cloud_file_storage.api.compat.legacy_generate_file",
}

scheduler_events = {
    "hourly_maintenance": ["cloud_file_storage.gc.repair_pending_uploads_dispatch"],
    "daily (dispatched)": [
        "cloud_file_storage.gc.run_deferred_object_gc",
        "cloud_file_storage.cache.eviction.run_eviction",
    ],
}
```

Notes: `write_file`/`delete_file_data_content` are single-owner hooks (`get_hook_method` takes index `[0]`, `frappe/utils/__init__.py:624-631`) and `override_doctype_class` is last-wins (`base_document.py:91`) — document loudly that DFP External Storage and this app **cannot coexist** on a site. Scheduler entries use `*_maintenance` frequencies so they land on `long`/`default` only as O(ms) dispatchers ([jobs §4]: `scheduler_events` cannot target custom queues; `scheduled_job_type.py:181-182`).

---

## 2. `Cloud Storage Settings` (Single)

`issingle: 1`, module "Cloud File Storage", permissions System Manager RW only, `track_changes: 1`, no `quick_entry`. Full field list (fieldname / fieldtype / options / default):

**Section: Operation**
| fieldname | fieldtype | options / default | notes |
|---|---|---|---|
| `operation_mode` | Select | `LOCAL_ONLY\nDUAL_WRITE\nS3_PRIMARY_LOCAL_FALLBACK\nS3_ONLY`, default `LOCAL_ONLY`, reqd | transitions validated in `validate()` (§9.2) |
| `fail_insert_on_s3_error` | Check | default 0 | DUAL_WRITE only: if 0, S3 failure degrades to local + repair queue; if 1, insert aborts |
| `mode_changed_on` | Datetime | read_only | audit |

**Section: Connection**
| `provider` | Select | `AWS S3\nS3 Compatible`, default `AWS S3` |
| `bucket` | Data | reqd when mode != LOCAL_ONLY | the private bucket |
| ~~`public_bucket`~~ | — | **DROPPED (A24: single-bucket invariant)** |
| `region` | Data | reqd for AWS |
| `endpoint_url` | Data | optional; HTTPS-only validation (port fork's validator) |
| `addressing_style` | Select | `auto\npath\nvirtual`, default `auto` |
| `key_prefix` | Data | optional extra prefix, sanitized, immutable once objects exist |
| `access_key_id` | Data | optional | **empty ⇒ boto3 default chain (IAM role / env / profile) — preferred** |
| `secret_access_key` | Password | optional | must be paired with access_key_id (port fork validator, `s3_file_attachment.py:16-24`) |
| `sse_mode` | Select | `\nSSE-S3\nSSE-KMS`, default `SSE-S3` (A12) |
| `kms_key_id` | Data | depends_on `sse_mode=='SSE-KMS'` |
| `test_connection` | Button | | → `cloud_file_storage.api.compat.test_connection` |

**Section: Serving**
| `cdn_base_url` | Data | optional | CloudFront w/ OAC over `pub/*`; empty ⇒ presigned GET for public misses |
| `public_presign_ttl` | Int | default 3600 (seconds) |
| `private_presign_ttl` | Int | default 300 (seconds) | single source of truth — no 120-vs-300 code/schema split as in fork (`controller.py:194-197` vs schema:63) |
| `inline_mimetype_prefixes` | Small Text | default `image/\napplication/pdf\nvideo/\naudio/\ntext/plain` | inline disposition allowlist; svg/html/htm/xml always forced attachment (mirror `response.py:299-301`) |
| `audit_public_fallback` | Check | default 0 | also `make_access_log` public fallback redirects |

**Section: Materialization Cache**
| `cache_max_size_mb` | Int | default 5120 |
| `cache_ttl_hours` | Int | default 72 |

**Section: Behaviour**
| `ignored_doctypes` | Table | `Cloud Storage Ignored DocType` | seeded: `Data Import`, `Prepared Report`, `Package Import` (Package Import bypasses File API entirely — `package_import.py:47-56`, contract §2 #16) |
| `multipart_threshold_mb` | Int | default 64 |
| `multipart_chunksize_mb` | Int | default 16 |
| `transfer_max_concurrency` | Int | default 4 |
| `object_delete_grace_days` | Int | default 7 | GC grace for `pending_delete` |

**Section: Migration (tuning refs — engine is separate domain)**
| `migration_batch_size` | Int | default 1000 |
| `migration_parallelism` | Int | default 2 |
| `migration_bandwidth_limit_mbps` | Int | default 0 |
| `open_campaigns` | Button | client-side route to campaign list |

`Cloud Storage Ignored DocType` (child, `istable: 1`): single field `doctype_name` Link → DocType, reqd, **no `unique:1`** (child-table unique is enforced globally, a latent fork bug [repo §7]); per-parent uniqueness enforced in `CloudStorageSettings.validate()`.

`validate()` responsibilities: credential pairing; HTTPS endpoint; mode-transition gates (§9.2); ignored-doctype dedup; on connection-field change with existing CSOs → loud warning (DFP ADOPT #11). `on_update()`: `frappe.cache` flush of engine client cache. `test_connection` (whitelisted, `frappe.only_for("System Manager")`): `head_bucket` → PUT probe `{site}/probe/{uuid}` with configured SSE → `head_object` → presign GET → `delete_object`; returns per-step pass/fail (upgrade of s3_backup_settings' `head_bucket`-only check [jobs §6]).

---

## 3. `Cloud Storage Object` (CSO)

DocType flags: `track_changes: 0` (high volume), `in_create: 1`, System Manager read/write only, no delete via UI. Controller `autoname(self): self.name = uuid.uuid4().hex` (ADR-12).

| fieldname | fieldtype | options/default | notes |
|---|---|---|---|
| `content_sha256` | Data (length 64) | reqd, read_only | primary identity |
| `content_hash_md5` | Data (length 32) | reqd, read_only | core-compat hash (contract #15; MD5 of bytes, `file/utils.py:187-190`) |
| `file_size` | **Long Int** | reqd, read_only | ADR-13 |
| `mime_type` | Data | read_only |
| `visibility` | Select | `public\nprivate`, reqd, read_only | encoded in key prefix |
| `provider` | Select | `AWS S3\nS3 Compatible` | snapshot at upload |
| `bucket` | Data | reqd, read_only |
| `s3_key` | Data (length 500) | reqd, read_only | full object key |
| `etag` | Data | read_only |
| `checksum_sha256_s3` | Data | read_only | S3 `ChecksumSHA256` returned value |
| `version_id` | Data | read_only | when bucket versioning on |
| `sse_mode` | Select | `\nSSE-S3\nSSE-KMS` | snapshot |
| `kms_key_id` | Data | read_only |
| `status` | Select | `pending_upload\nuploaded\nverified\nfailed\norphaned\npending_delete\ndeleted\nlegacy_unverified`, default `pending_upload` | state machine §3.1 |
| `source_path` | Data | read_only | original local relative path (audit/migration provenance) |
| `uploaded_at` | Datetime | read_only |
| `verified_at` | Datetime | read_only |
| `deletion_scheduled_at` | Datetime | read_only |
| `reference_count` | Int | read_only, default 0 | **denormalized, UI-only, never authoritative** (ADR-4) |
| `retry_count` | Int | default 0 |
| `last_error` | Small Text | read_only |
| `migration_batch` | Data | read_only | provenance link for migration engine |

**Indexes** (created in `on_doctype_update()` + `install.after_migrate`):
- UNIQUE `(s3_key)` — physical identity, insert-race backstop.
- UNIQUE `(content_sha256, visibility, bucket)` — dedup identity.
- `(content_hash_md5)` — join with `tabFile.content_hash`.
- `(status, deletion_scheduled_at)` — GC scan.
- On `tabFile`: index `cloud_storage_object`, index `content_hash(32)` (core queries it unindexed on every insert/delete — `file.py:688`, `:513`; risk note [core §11.8]).

**File linkage**: one app-owned Custom Field on File — `cloud_storage_object` (Link → Cloud Storage Object, read_only, `no_copy: 0`, hidden 0, insert_after `content_hash`, module "Cloud File Storage"). Legacy `s3_object_key`: **not created here** (ADR-9); the external-install compat patch converts `s3_object_key` values into `legacy_unverified` CSOs, links them, and hides (not drops) the old field.

### 3.1 State machine

```
pending_upload ──put ok──▶ uploaded ──independent HEAD+checksum ok──▶ verified
      │ ▲                      │                                          │
      │ └─ retry (retry_count) └──verify mismatch──▶ failed               │ last ref removed (locked count == 0)
      └──put failed──▶ failed ──manual/repair──▶ pending_upload           ▼
                                                                   pending_delete ──grace elapsed AND recount==0──▶ S3 delete ──▶ deleted (tombstone)
verified ──new File ref while pending_delete──▶ verified   (resurrection: allowed any time before physical delete)
orphaned: set by reconciliation sweep (object in bucket without CSO row, or CSO with 0 refs and no transition history) ──▶ pending_delete after review
legacy_unverified ──migration VERIFY phase──▶ verified
```

Transition rules enforced in `storage/objects.py` (single choke-point functions; **no raw SQL against CSO outside this module**):
- Every transition takes the row lock first: `frappe.db.get_value("Cloud Storage Object", name, "status", for_update=True)`.
- `pending_delete` is set only by `delete_file_data_content` decrement or GC orphan sweep, never inline S3 delete (ADR-4).
- Physical `DeleteObject` happens only in `gc.run_deferred_object_gc`, which re-locks, re-counts live `tabFile` references (`frappe.db.count("File", {"cloud_storage_object": name})`), re-checks grace, then deletes and marks `deleted`.

### 3.2 Refcount decision (ADR-4, expanded)

Stored counters drift: `frappe.db.delete("File", ...)` (used in tests and patches, e.g. the fork's own teardown `test_controller.py:663-675` [repo §9]) bypasses `on_trash` entirely; crashes between S3 op and commit desync counters permanently. Derived counting is self-healing: the source of truth is the set of live `tabFile.cloud_storage_object` values, which is transactional with File row lifecycle. Races are closed in two layers:
1. **Row lock**: both `ensure_cso_link` (link/increment side) and the delete decrement acquire `FOR UPDATE` on the CSO row before reading/altering references, serializing concurrent "last delete" vs "new reference" within InnoDB transactions.
2. **Deferred GC with re-verification**: even if a locking bug slips through, `pending_delete` + grace period + recount-before-physical-delete means the failure mode is "object lives a few days longer", never "shared bytes destroyed" (mission's safety direction).

`reference_count` field is refreshed opportunistically (best-effort `db_set(update_modified=False)`) for list views/dashboards only.

---

## 4. S3 Key Scheme & Upload Settings

```
key = f"{key_prefix + '/' if key_prefix else ''}{site}/{'pub' if visibility=='public' else 'prv'}/{sha256[:2]}/{sha256[2:4]}/{sha256}"
# e.g. finstein.erp/prv/9f/3a/9f3ab8...e41c
```

- Immutable unique identifier = full sha256 (ADR-3). No filename, no doctype, no date, no randomness — the fork's `random.choice`+filename key (`controller.py:116-128`) leaked PII and had no content identity [repo §3.4]; DFP's `{site}/{filename}-{docname}{ext}` leaked filenames and prevented dedup (`dfp-audit AVOID #7`).
- Sharding: two 2-hex-char levels = 65k prefixes; no hot prefix at 1.2M objects.
- **Original filename preservation**: lives only in `File.file_name` (DB). At serve time it is applied via `ResponseContentDisposition: inline|attachment; filename*=UTF-8''<rfc5987-quoted>` on presigned GETs (reuse the fork's proven RFC-5987 encoder, `controller.py:202-204` + tests `test_controller.py:542-554`). Object metadata carries only `Metadata={"sha256": ..., "app": "cloud_file_storage"}` — enough for orphan reconciliation (fixes DFP AVOID #8) without PII.
- `ContentType` set from sniffed MIME (`filetype.guess` → `mimetypes.guess_type` → `application/octet-stream`, keep fork logic `controller.py:137-139`).
- **Transfer settings** (`storage/client.py`): `boto3.s3.transfer.TransferConfig(multipart_threshold=mb(settings.multipart_threshold_mb), multipart_chunksize=mb(settings.multipart_chunksize_mb), max_concurrency=settings.transfer_max_concurrency, use_threads=True)` (params verified against installed boto3 1.34.162 / s3transfer 0.10.4).
- **ExtraArgs builder**: always `{"ContentType": mime, "ChecksumAlgorithm": "SHA256", "Metadata": {...}}`; plus `{"ServerSideEncryption": "AES256"}` for SSE-S3 or `{"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": kms_key_id}` for SSE-KMS. **Never any `ACL` key** (ADR-7).
- Client: one cached boto3 client per (site, settings.modified) — fixes the fork's per-upload client construction (`controller.py:23-51`, called per file [repo §12]). `signature_version="s3v4"`; `addressing_style` per settings.
- Multipart hygiene: uploads wrapped so that on any exception the engine calls `abort_multipart_upload` in `finally` when an upload id exists, and `install.py` documents the bucket lifecycle rule `AbortIncompleteMultipartUpload: 7 days` (A24) (RQ SIGALRM can kill a worker mid-part, [jobs §2.4]).

Independent verification (`engine.verify`): fresh `head_object` (bypassing any client-side state) comparing `ContentLength == cso.file_size` and, when present, `ChecksumSHA256 == base64(sha256)`; fallback ETag-vs-md5 only for single-part uploads. Used by the migration VERIFY phase and by DUAL_WRITE repair before any local delete.

---

## 5. `CloudFile(File)` — the override class

`cloud_file_storage/overrides/file.py`. Every signature identical to v15.93 core ([core §1] table). Internal helpers (new, underscore-prefixed): `_cso() -> Document|None` (cached `frappe.get_doc` of `self.cloud_storage_object`), `_is_cloud_backed() -> bool`, `_canonical_local_path() -> str` (pure path math, **never** touches network — extracted from core `get_full_path` logic minus safety throws), `_read_bytes() -> bytes`.

`_read_bytes` resolution order: (1) canonical local path exists → read it (DUAL_WRITE fast path); (2) materialization cache hit with clean sidecar → read; (3) cloud-backed → `engine.get_object_bytes(cso)`; (4) neither → raise. Error mapping (ADR-8): `CloudObjectNotFound(CloudStorageError, FileNotFoundError)` **only** when S3 returns 404/NoSuchKey or no CSO exists and no local copy — this is what lets `gst_return_log.py:53-59` (which catches only `FileNotFoundError` and then **nulls the pointer**) behave correctly (contract #5). All transport/credential/throttle errors raise `CloudStorageTransportError` / `CloudStoragePermissionError` — never `FileNotFoundError`, never swallowed (DFP AVOID #2/#3: no `PageDoesNotExistError` blanket, no permission checks inside data accessors).

| Override | Signature | Behavior per mode | Contract |
|---|---|---|---|
| `get_content` | `def get_content(self, encodings=None) -> bytes \| str` | All modes: if `self.get("content")` set → core in-memory path verbatim (`file.py:570-576`; fixes DFP fifth failure). Else `raw = self._read_bytes()`. Decode policy: `encodings is None` → try `raw.decode()` except `UnicodeDecodeError` → bytes (exact v15 behavior, `file.py:583-590`); `encodings == []` → return raw bytes untouched; explicit list → try each encoding in order (`UnicodeDecodeError`/`LookupError` → next), bytes if all fail (develop/v16 semantics). Sets `self._content`. No permission check. LOCAL_ONLY: identical to core. | #1 #2 #3 #4 #5 |
| `get_full_path` | `def get_full_path(self)` | If local canonical path exists → return it (core-identical result). Else if cloud-backed and mode != LOCAL_ONLY → `cache.materialize(self)` → cache path (registers write-back tracking, §7). Else → `super().get_full_path()` (core throws/URL-passthrough semantics preserved for genuinely-remote `http(s)://` Files). | #7, and transitively #17 (stock_ledger fallthrough `open(path,"wb")` becomes safe), unzip (`file.py:533`), xlsx (`xlsxutils.py:106`) |
| `exists_on_disk` | `def exists_on_disk(self)` | `os.path.exists(local) or (self._is_cloud_backed() and cso.status in ("uploaded","verified"))`. No network HEAD in the hot path (status is trusted; GC/verify keep it honest). Restores core dedup that DFP destroyed (`dfp-audit AVOID #6`). | #14 |
| `validate_file_on_disk` | `def validate_file_on_disk(self)` | If cloud-backed → return `True` (no disk check, **no materialization**). Else `super()`. | #6 |
| `validate_file_path` | `def validate_file_path(self)` | If cloud-backed → validate `self._canonical_local_path()` realpath containment (same rule as core `file.py:204-213`) without requiring existence; else `super()`. Never triggers a download on `save()`. | #6 |
| `handle_is_private_changed` | `def handle_is_private_changed(self)` | Cloud-backed: compute new visibility; `engine.copy_visibility` (server-side `CopyObject` to the `pub/`↔`prv/` key), `ensure_cso` for the target (dedup-aware), relink `self.cloud_storage_object`, decrement old CSO (→ possibly `pending_delete`); then replicate core's URL rewrite + parent `attached_to_field` propagation + `update_existing_file_docs` sibling rewrite (`file.py:270-292`, `file/utils.py:301-310`) — propagation preserved verbatim. If a local copy also exists (DUAL_WRITE) call the local `shutil.move` part via core logic too. If CDN configured and flip was public→private: enqueue CloudFront invalidation for the old pub key (best-effort) and fast-track old pub CSO to `pending_delete`. | #16, #18; fixes fork gap [repo §5] |
| `on_rollback` | `def on_rollback(self)` | If the file had a local copy → `super()` (core disk restore). Pure-cloud: no filesystem restore needed — objects are immutable and content-addressed, DB rollback already restored `cloud_storage_object`/`content_hash` pointers (ADR-3); just reset `self.file_url` flags like core's bookkeeping. Any S3 object uploaded in the rolled-back transaction becomes an unreferenced CSO... which was also rolled back — the bare S3 object is swept by GC orphan reconciliation or reused by the next identical upload. | core risk #7 neutralized |
| `make_thumbnail` | `def make_thumbnail(self, set_as_thumbnail=True, width=300, height=300, suffix="small", crop=False)` | If `file_url` canonical and source missing locally and cloud-backed → materialize, open via PIL from cache path, then proceed with core's resize flow, save locally AND upload the derived object (`thm/` key, `derived_of`=source CSO), `db_set thumbnail_url` (canonical URL; renderer serves derived-object misses). Per ADR 0021. Errors logged (`frappe.log_error`) before returning None — no silent `OSError` swallow beyond core's own contract. | fixes [contract §2 #5/#6] thumbnail death |
| `generate_content_hash` | `def generate_content_hash(self)` | If cloud-backed: `self.content_hash = cso.content_hash_md5` (no disk read, no throw). Else `super()`. | #15 |

**Deliberately NOT overridden** (each is an ADR-grade choice):
- `is_remote_file` — stays core. Canonical URLs ⇒ `False` ⇒ core's validate/save machinery runs; DFP's inversion (`dfp_external_storage.py:315-317`) flipped 7 core branches and caused most of its breakage (`dfp-audit §8` table).
- `unzip`, `optimize_file`, `zip_files` — fully served by the `get_full_path`/`get_content` overrides (`file.py:533` uses the path; `:801` uses content + `save_file(overwrite=True)` which lands in our write hook).
- `save_file`, `write_file`, `save_file_on_filesystem`, `_delete_file_on_disk`, `validate_duplicate_entry` — core logic is correct once `exists_on_disk`/hooks are cloud-aware; overriding them is unnecessary drift surface. Note: core `save_file` still pre-loads `flags.original_content = self.get_content()` on updates (`file.py:657-658`) ⇒ one object GET per content-update save (overwrite/optimize flows only) — accepted cost, documented.
- `validate_file_url`, `set_is_private` — canonical URLs satisfy them (ADR-1).

---

## 6. Core hooks (`core_hooks.py`)

### 6.1 `write_file` — dual-convention dispatcher

```python
def write_file(*args, **kwargs):
    if len(args) == 1 and not kwargs and hasattr(args[0], "doctype"):
        return _write_file_doc(args[0])                     # file.py:712-714 convention
    return _write_file_legacy(*args, **kwargs)              # file_manager.py:165-167 convention

def _write_file_doc(file: "File") -> dict: ...
def _write_file_legacy(fname: str, content: bytes | str, content_type: str | None = None,
                       is_private: bool = False) -> dict:   # must return write_file_keys dict
```

`_write_file_doc(file)` (state available per [core §2.2]: `_content`, `file_name` already hash-suffix-renamed unless overwrite, `content_hash`, `file_size`, `content_type`, `is_private`; `file_url` unset):
1. Resolve mode + ignored-doctype (`file.attached_to_doctype`); if LOCAL_ONLY or ignored → `return file.save_file_on_filesystem()` (exact core behavior).
2. Compute `sha256` + reuse `content_hash` md5 in one pass over `_content` (`hashing.py`).
3. Set canonical `file_url` exactly as core would: `re.sub(r"[/\\%?#]", "_", file.file_name)` + `/private/files/` or `/files/` prefix (`file.py:717-722`). **DB-based collision guard**: since `generate_file_name`'s `os.path.exists` check degenerates without local files ([core §3], `utils.py:198-208`), query `frappe.db.exists("File", {"file_url": candidate, "name": ("!=", file.name or "")})` with a different `content_hash` → apply the same hash-suffix rename core would; identical hash ⇒ dedup/overwrite, keep URL.
4. `cso = ensure_cso(sha256, md5, size, mime, visibility, ...)` (§10) — get-or-create under lock; if new or `pending_upload`/`failed` → upload now:
   - DUAL_WRITE: local write first (`file.write_file()` core method), then `engine.put_object_from_path(local_path, ...)`; on S3 failure: if `fail_insert_on_s3_error` → raise typed error (aborts insert); else CSO stays `pending_upload` with `last_error`, repair job retries — the file is durable locally either way.
   - S3_PRIMARY_LOCAL_FALLBACK: `engine.put_object_from_bytes`; on failure → local write via core + CSO `pending_upload` + repair.
   - S3_ONLY: `engine.put_object_from_bytes`; on failure → raise `CloudStorageTransportError` (insert aborts; **explicit** — no DFP silent-local fallback, `dfp-audit AVOID #18`).
5. Set `file.cloud_storage_object = cso.name`. Do **not** touch `content_hash` (kept! — reverses the fork's NULLing at `controller.py:249` which destroyed core dedup [repo §3.6]).
6. Return `{"file_name": file.file_name, "file_url": file.file_url}`.

No `frappe.db.commit()` anywhere in the hook (fork's mid-transaction commit at `controller.py:261` was a correctness hazard [repo §12]). No `os.remove` of anything ever in the write path.

`_write_file_legacy(fname, content, content_type=None, is_private=False)`: normalize `content` to bytes (encode str), run the same steps 1-5 against a transient context, and return `{"file_url", "file_name"}` (filtered by `write_file_keys`, `frappe/hooks.py:70`). The created CSO is linked later by `doc_events.file_after_insert` when the File doc is inserted by `file_manager.save_file` (it copies `file_url` into the doc, `file_manager.py:169-189`); linkage key = `content_hash` md5 → CSO lookup. Live callers proving this path matters: `e_waybill.py:741`, `import_supplier_invoice.py:104` (contract #12).

`before_write_file(file_size=None, **kwargs)`: fail-fast guard only — if mode requires S3 and settings unconfigured/unreachable-flagged → `frappe.throw` with actionable message. Return value discarded by core (`file.py:711`).

### 6.2 `delete_file_data_content(file_doc, only_thumbnail=False)`

Reached only through core's `_delete_file_on_disk` refcount gate (`file.py:511-526`, contract #13):
1. Thumbnails per ADR 0021: decrement/GC the DERIVED object like any CSO; delete the local thumbnail copy only when the derived object is verified or the source is being fully deleted. If `only_thumbnail` → handle the derived object only, then return.
2. Local bytes: if the canonical local file exists → unlink it (core already established no other File shares this `content_hash`).
3. Cloud decrement: if `file_doc.cloud_storage_object`: `lock_cso(name)`; `others = frappe.db.count("File", {"cloud_storage_object": name, "name": ("!=", file_doc.name)})`; if `others == 0` → transition `pending_delete`, `deletion_scheduled_at = now + grace`; else refresh `reference_count`. **No inline S3 delete.**
4. Also drop the file's materialization cache entry (+sidecar), tolerating absence.

Known core quirk handled: core's gate ignores `is_private` when counting hash-sharers (`file.py:518-519`), so the last deleter of a `prv/` CSO can be routed to `only_thumbnail=True` because a *public* twin shares the md5. The daily GC orphan sweep (CSOs with derived refcount 0, `modified < now - grace`) closes this leak — decrement is eventually-exact, never unsafe.

Legacy path note: `file_manager.delete_file` mangles non-local paths into bogus private paths (`file_manager.py:309-326`, contract #13) — irrelevant here because our hook is authoritative and our URLs are canonical.

### 6.3 `doc_events.file_after_insert(doc, method)` — `ensure_cso_link`

Covers every flow where `write_file` never fires: core dedup adoption (`file.py:687-702` skips hooks), amend/copy (`document.py:446-464` re-reads bytes and dedups [core §5]), legacy path (b) inserts, and `attach_files_to_document` auto-created rows. Logic: if `doc.cloud_storage_object` empty, not folder, not remote-`http(s)`, mode != LOCAL_ONLY: find sibling `File` with same `content_hash` + `is_private` + non-empty link → adopt link under CSO lock (refresh `reference_count`); else if mode is S3-primary/only and no local file → log repair item. Cheap (2 indexed queries), silent no-op in LOCAL_ONLY.

---

## 7. Materialization Cache

- **Location**: `sites/{site}/cloud_storage_cache/` (ADR-10). Justification: outside `public/` (never served by nginx `try_files` or `StaticDataMiddleware`, which map only `public/files` — `middlewares.py:20-27`), outside `private/files` (not tarred by backups, `backups.py:349-373`; not swept by core `delete_file`), per-site isolation, survives nothing it shouldn't.
- **Naming**: `{file.name}__{cso.name}{ext}` + sidecar `{same}.meta.json` holding `{sha256, size, mtime, materialized_at, last_access, dirty}`. Per-File (not per-CSO) so two File rows sharing an object can't corrupt each other via concurrent write-back; the mutation of one relinks only that File (§10).
- **Locking**: `frappe.utils.synchronization.filelock(f"cfs_mat_{file.name}", timeout=120)` ([jobs §3]) around materialize and sync-back. Download to `path + ".part"`, fsync, verify sha256 against CSO, atomic `os.rename`. Single-host limitation of filelock stated: multi-server benches must pin materialization-heavy work to one node or replace with a DB lock (documented, out of scope v1).
- **`materialize(file_doc) -> str`**: lock → cache hit + sidecar-clean → touch `last_access`, return; else download+verify+rename; append `(file_name, path)` to `frappe.local.cfs_materialized` (created lazily) for write-back tracking; return path.
- **Write-back detection (contract #7/#8)** — concrete mechanism:
  1. On materialize, sidecar records `sha256`, `size`, `mtime`.
  2. `flush_request` (`after_request` hook, `app.py:163-168`) and `flush_job` (`after_job` hook, [jobs §2.6]) iterate `frappe.local.cfs_materialized`: if `mtime/size` changed → re-hash; if sha changed → **sync-back**: `ensure_cso` for new bytes → upload → relink `File.cloud_storage_object`, update `content_hash`, `file_size` via `db_set(update_modified=False)` → decrement old CSO → rewrite sidecar. This covers the two known writers: `bank_statement_import.py:202-212` and `stock_ledger.py:420-422` — both mutate within one request/job.
  3. Opportunistic re-check: every subsequent `materialize`/`_read_bytes` cache hit re-validates `mtime/size` vs sidecar; drift → sync-back before serving (recovers from a crash between write and flush).
  4. Weekly consistency scan (part of eviction job): full-hash any entry whose `mtime` postdates `materialized_at` and sidecar not refreshed.
  - **Residual risks, honestly**: (a) a writer that mutates the path *after* `after_request/after_job` fired (background thread, subprocess) is only caught at next touch or weekly scan — window of DB-vs-bytes divergence; (b) process killed (SIGKILL) between file write and flush → same, recovered on next touch; (c) two concurrent jobs materializing the same File and both writing → last flush wins (per-File filelock serializes the flushes but not the intent); (d) a caller that derives a *different* path from the returned one is invisible. These are inherent to path-level interop; the design bounds them to "temporary divergence, later converged", never data loss of the original object (old CSO survives until GC grace).
- **Eviction** (`eviction.run_eviction`, daily (dispatched)): scan sidecars; delete entries with `last_access < now - cache_ttl_hours` OR (over `cache_max_size_mb`, evict oldest-access first) — skipping `dirty` entries and entries whose filelock is held. Never evicts the canonical `files/` copies (those belong to mode semantics, not the cache).

---

## 8. Serving Architecture

### 8.1 PRIVATE — `/private/files/...` preserved

Primary (ADR-5): `serving/runtime_patches.ensure_installed()` (registered as `before_request`, idempotent, thread-safe via module flag) replaces `frappe.utils.response.download_private_file` with `serving.private.download_private_file_cloud`, keeping a reference to the original. Works because `app.py:126` performs attribute lookup at call time ([core §4] point 1) and executes **after** `validate_auth()` (`app.py:107`) — token-auth API clients are fully authenticated. The patched function is **site-aware**: first statement checks "app installed on this site AND mode != LOCAL_ONLY", else delegates to the original unchanged (safe in multi-site worker processes).

```python
def download_private_file_cloud(path: str) -> Response:
    if not _active_for_site(): return _original(path)
    if frappe.session.user == "Guest": raise Forbidden(...)          # response.py:271-272 preserved
    file = find_file_by_url(path, name=frappe.form_dict.fid)          # permission gate preserved
    if not file: raise Forbidden(...)                                 # is_downloadable→has_permission
    make_access_log(doctype="File", document=file.name, file_type=ext)  # response.py:279 preserved (contract #10)
    if os.path.exists(file._canonical_local_path()):                  # DUAL_WRITE / not-yet-migrated
        return _original(path)                                        # keeps X-Accel + send_file behavior
    cso = file._cso()
    if cso and cso.status in ("uploaded", "verified", "legacy_unverified"):
        url = engine.presign_get(cso, ttl=settings.private_presign_ttl,
                                 disposition=disposition_for(file), filename=file.file_name,
                                 content_type=cso.mime_type)
        return redirect(url, code=302)                                # Cache-Control: no-store
    raise NotFound                                                    # truly absent
```

- The full `find_file_by_url → is_downloadable → has_permission` gate and `make_access_log` are byte-for-byte preserved (contract #10).
- Disposition policy (`serving/disposition.py`): `inline` iff mime prefix ∈ `inline_mimetype_prefixes` AND extension ∉ {svg,html,htm,xml} (mirror of `response.py:299-301`), else `attachment`; applied via `ResponseContentDisposition` with RFC-5987 filename — fixes the fork's "everything is an attachment" limitation (`controller.py:202-204` [repo §4]).
- Token-auth API clients: they hit the same code path (302 + `Location`); `frappe-client`/requests follow redirects by default. Documented caveat: the presigned URL itself is unauthenticated-bearer for `private_presign_ttl` seconds (default 300) — threat notes §11.
- X-Accel/nginx setups: unaffected — local-copy requests still delegate to `_original`, which honors `X-Use-X-Accel-Redirect` (`response.py:286-292`, header set by the nginx template).
- Fallback interception (shipped dark, config flag `cfs_use_before_request_interception`): a `before_request` hook that, for `/private/files/*` on session-cookie users, raises a werkzeug `HTTPException` redirect (caught at `app.py:134-135`). Not default because of the pre-`validate_auth` token problem (ADR-5) — it would misclassify token clients as Guest.
- `frappe/handler.py:271-287 download_file` (API download) needs nothing: it uses `get_content()`.

### 8.2 PUBLIC — `/files/...` preserved

- **Local hit fast path**: unchanged — nginx serves `public/files` directly; dev `StaticDataMiddleware` serves it with zero Python (`app.py:531`, `middlewares.py:13-29`).
- **Miss path, production** (verified): `location / { try_files /{{site}}/public/$uri @webserver; }` (`bench nginx template:80-92`) — a missing public file proxies to Frappe with the original path → `app.py:128-129 get_response()` → `PathResolver` → custom `page_renderer` hooks run **first** (`path_resolver.py:56-70`). `PublicFileRenderer.can_render(path)`: `path.startswith("files/")` AND app active AND `frappe.db.get_value("File", {"file_url": "/" + path}, ["name", "cloud_storage_object"])` returns a cloud-backed row (DB-verified — fixes DFP's swallow-everything regex, `dfp-audit AVOID #10`). `render()`: 302 to `cdn_base_url + "/" + quote(cso.s3_key)` when CDN configured and disposition-irrelevant mime, else presigned GET (`public_presign_ttl`, RCD filename for download types); `Cache-Control: private, max-age=min(300, ttl-60)`; optional `make_access_log` when `audit_public_fallback`. `.htm/.html/.svg/.xml` always presigned-attachment (the nginx template forces attachment for these locally; we mirror it).
- **Miss path, dev** (`bench serve`): verified `StaticDataMiddleware.get_directory_loader` **raises `NotFound`** on a miss (`middlewares.py:22-27`) instead of falling through to the wrapped app — the renderer would never run. Mitigation: `runtime_patches` also wraps `StaticDataMiddleware`'s loader (dev-only, gated on `frappe.local.dev_server`) to return "not handled" → falls through to the app. Guarded, 6-line patch; without it, dev benches simply 404 S3-only public files (documented degradation).
- **Explicitly NO public-read ACLs**; CloudFront uses Origin Access Control against the private bucket, distribution behavior limited to `pub/*` (deployment doc + `test_connection` warns if `cdn_base_url` set while objects lack `pub/` prefix).
- CDN cache-correctness: keys are content-addressed ⇒ objects immutable ⇒ long CDN TTLs are safe; content changes produce new keys/URLs (no stale-cache class of bugs). Privacy flips trigger best-effort CloudFront invalidation (§5 `handle_is_private_changed`).

### 8.3 LEGACY ALIAS

`override_whitelisted_methods = {"frappe_s3_attachment.controller.generate_file": "cloud_file_storage.api.compat.legacy_generate_file"}` — resolution confirmed at `handler.py:67` / `api/v2.py:36` / `frappe/__init__.py:2521-2523`, so old external URLs `/api/method/frappe_s3_attachment.controller.generate_file?key=...&file_name=...` keep working with **no old package shipped**.

```python
@frappe.whitelist()   # session required — no allow_guest (parity with fork controller.py:265)
def legacy_generate_file(key: str = None, file_name: str = None):
```
Behavior: empty key → `frappe.throw(404-style)` (fixes the fork's odd 200 "Key not found." body); resolve File by `s3_object_key == key` **or** CSO by `s3_key == key`; then run the exact §8.1 gate (`is_downloadable`/`has_permission` on the *resolved specific File*, `make_access_log`) → 302 presign. The fork's shared-key authorization hole ("any one readable row grants access", `controller.py:274` [repo §5]) is narrowed by resolving with `frappe.form_dict.fid` when present and otherwise requiring permission on **every** File sharing the key.

---

## 9. Mode Semantics Matrix

`storage/modes.py`: `class OperationMode(str, Enum): LOCAL_ONLY, DUAL_WRITE, S3_PRIMARY_LOCAL_FALLBACK, S3_ONLY`. `get_mode()` = cached settings read; per-doctype ignore list forces LOCAL_ONLY behavior for that File regardless of mode.

| Operation | LOCAL_ONLY | DUAL_WRITE | S3_PRIMARY_LOCAL_FALLBACK | S3_ONLY |
|---|---|---|---|---|
| **Upload (write_file)** | `save_file_on_filesystem()` verbatim | local write, then S3 put + CSO(verified-after-verify). S3 fail: `fail_insert_on_s3_error`? abort : local-durable + CSO `pending_upload` + hourly repair | S3 put + CSO; **no** local canonical copy. S3 fail: local write + CSO `pending_upload` + repair (explicit, queryable state — not DFP's silent fallback) | S3 put + CSO. S3 fail: typed raise, insert aborts |
| **Serve private** | core original | local copy exists → original (X-Accel capable); else presign 302 | presign 302; if S3 down and local fallback copy exists → original | presign 302; S3 down → 503 with Retry-After (never 404) |
| **Serve public** | nginx/static only | local hit via nginx (fast); renderer only for rows missing locally | renderer → CDN/presign | renderer → CDN/presign |
| **get_content / get_full_path** | core behavior | local read (fast path 1) | cache → S3; transport error + local fallback copy → local | cache → S3; transport error → `CloudStorageTransportError` (not FileNotFoundError) |
| **Delete (File on_trash)** | core local delete via hook step 2 | local delete + CSO decrement→`pending_delete` when last | CSO decrement; local fallback copy removed if present | CSO decrement |
| **is_private flip** | core `shutil.move` via super() | S3 visibility copy + relink + local move + URL/parent propagation | S3 visibility copy + relink + propagation | same |
| **S3 outage behavior** | unaffected | fully operational (reads local); uploads queue repairs | reads of migrated files fail unless local fallback exists; uploads degrade to local+repair | reads/uploads fail loudly with typed errors; serving 503 |

### 9.2 Mode-transition validation (in `CloudStorageSettings.validate()`)

- `LOCAL_ONLY → DUAL_WRITE | S3_PRIMARY`: requires a passing `test_connection` result recorded within 24h.
- `→ S3_ONLY` (hard gate): (a) zero managed Files without a `verified` CSO — query: File rows with canonical `file_url`, `is_folder=0`, parent doctype not ignored, and (`cloud_storage_object` empty OR linked CSO status != `verified`); (b) zero `dirty` cache sidecars; (c) no migration campaign in UPLOAD/VERIFY phase. Violation → `frappe.throw` listing counts + link to campaign UI.
- `S3_ONLY → S3_PRIMARY_LOCAL_FALLBACK`: always allowed. `→ DUAL_WRITE/LOCAL_ONLY`: blocked unless a reverse-materialization campaign completed or `flags.force_mode_downgrade` set by a System Manager via console (bytes aren't local anymore; flipping the label doesn't make them so).
- Every transition writes a Comment on the Singles doc + `mode_changed_on`.

---

## 10. Dedup Design

- **Identity**: `(content_sha256, visibility, bucket)` unique on CSO (ADR-7 gives natural congruence with core's `(content_hash md5, is_private)` dedup at `file.py:686-692`). Size is stored and verified but not part of the key (sha256 collision-with-different-size is not a practical concern; size mismatch at verify → `failed`).
- **`ensure_cso(sha256, md5, size, mime, visibility) -> cso`**: `SELECT ... FOR UPDATE`-style get (`frappe.db.get_value(..., for_update=True)`) by identity; found → return (resurrect from `pending_delete` → `verified` under the same lock); not found → insert; on `frappe.db.DuplicateEntry` (concurrent creator hit the unique index) → re-fetch. Upload step is idempotent: same content ⇒ same key ⇒ `put_object` overwrite with identical bytes is harmless.
- **Interplay with core dedup (now KEPT)**: `content_hash` is populated again (reversing the fork). Core's insert-time dedup reuses `duplicate.file_url` when `exists_on_disk()` — our override makes that true for cloud twins (contract #14), so the second File row adopts the URL, skips `write_file`, and `file_after_insert.ensure_cso_link` adopts the sibling's CSO link. Result: N File rows → 1 CSO, exactly the mission's sharing model, riding core's own mechanism instead of fighting it.
- **`overwrite=True` in-place mutation (contract #11)** — `gst_return_log.py:103`, `optimize_file` (`file.py:807`): core skips the hash-suffix rename, keeps `file_url` stable, and calls our `write_file` with new `_content`. New sha256 → new/different CSO → relink `cloud_storage_object`, decrement old CSO (→ `pending_delete` when last). `file_url` unchanged ⇒ the business-field pointers stored by `gst_return_log.py:87,104` and the `file_url + attached_to_field` lookups in `stock_ledger.py:308-318` keep resolving (contract #18). The S3 object is never mutated — rollback safety preserved.
- **Amend/copy (`document.py:446-464`)**: new File row, same `file_url`, no content → `before_insert` → `save_file(content=self.get_content())` — our `get_content` works for cloud-only rows ([core §5] requirement), hash matches, core dedup path fires, `ensure_cso_link` adopts. No new object, refcount via link count.
- **Migration-time grouping** (interface contract for the migration engine): existing-data dedup groups by physical identity (file_url → resolved path → sha256), not by File row; the engine calls the same `ensure_cso` so historical duplicates converge onto single objects, and conflicts (same file_url, different bytes; missing physical file; remote URLs; corrupt rows) go to `Cloud Migration Conflict` — healthy legacy URLs are **never renamed**.

---

## 11. Security & Audit

- **Credentials**: empty key fields ⇒ boto3 default chain (IAM instance role / IRSA / env / shared profile) — stated as preferred in field description; explicit keys stored as `Password` (encrypted `__Auth`), read via `get_password(raise_exception=False)`. No secrets in code, fixtures, or CI files; MinIO CI creds via env vars only.
- **Encryption**: `sse_mode` → ExtraArgs (§4); SSE-KMS supported with customer CMK; `test_connection` PUTs the probe with the configured SSE so KMS key permissions fail loudly at config time, not first upload.
- **Bucket posture** (deployment checklist shipped in README): Block Public Access ON; bucket policy grants only the app principal + CloudFront OAC (scoped to `pub/*`); no ListBucket for the CDN principal; lifecycle rules `AbortIncompleteMultipartUpload=7d` (A24); versioning optional (CSO stores `version_id`).
- **Presign threat notes**: presigned GETs are bearer capabilities — mitigations: short TTLs (private default 300s, settings-enforced max 3600), per-request issuance only after the full Frappe permission gate, `ResponseContentDisposition`/`ResponseContentType` pinned into the signature (prevents content-type–confusion reuse), `Cache-Control: no-store` on the 302 so the Location isn't cached, and no query-string logging of signed URLs in app logs. Residual: a recipient can share the URL for TTL seconds; not IP-bound (S3 presign limitation) — documented, with the private TTL as the control knob.
- **Audit**: every private serve keeps `make_access_log` (contract #10); `legacy_generate_file` logs identically; optional public-fallback logging; every CSO state transition appends `last_error`/timestamps; upload/delete/GC actions `frappe.logger("cloud_file_storage")` structured lines + `frappe.log_error` on failures (never swallowed — DFP AVOID #2). GC physical deletes leave `deleted` tombstone rows with `s3_key`, timestamps.
- **Endpoint surface**: fork's dead `ping` endpoint removed; `test_connection` and campaign controls `frappe.only_for("System Manager")`; `legacy_generate_file` session-required.

---

## 12. Failure-Mode Analysis

| Failure | Effect | Mitigation |
|---|---|---|
| S3 outage during upload | per-mode: DUAL_WRITE/S3_PRIMARY degrade to local + `pending_upload`; S3_ONLY aborts insert with typed error | hourly repair job re-uploads `pending_upload`/`failed` CSOs (batched, `cloud_migration` queue when configured, else `long`), retry_count + backoff |
| S3 outage during read | `CloudStorageTransportError` (never FileNotFoundError → GST log pointer never wiped, contract #5) | local fallback path when copy exists; serving returns 503 not 404 |
| Worker SIGKILL mid-multipart | orphan multipart upload | `abort_multipart_upload` in `finally` + bucket lifecycle rule ([jobs §2.4]) |
| DB rollback after S3 put | CSO row rolled back, S3 object unreferenced | content-addressed key reused by next identical upload; GC orphan sweep reconciles bucket-vs-CSO (with `Metadata.app` marker) |
| Concurrent last-delete vs new-reference | race on shared object | CSO row lock serializes; `pending_delete` + grace + recount before physical delete (ADR-4) — worst case object outlives by grace days |
| Core `only_thumbnail` routing hides last cloud deref (`file.py:518-526`, is_private ignored) | stale `verified` CSO with 0 refs | daily GC sweep: derived-refcount-0 + age > grace → `pending_delete` |
| Cache disk full | materialization fails | typed error; eviction job; `cache_max_size_mb` enforcement; monitoring note |
| Write-through mutation lost (crash before flush) | DB/bytes divergence on one File | opportunistic re-check on next touch + weekly scan (§7 residual risks stated) |
| Presigned URL expiry mid-download | browser retry hits `/private/files/...` again → fresh gate + fresh URL | stateless re-issue; TTL ≥ 300s covers normal downloads |
| `cloud_migration` queue not configured | migration/repair enqueues would throw (`background_jobs.py:456-467`) | dispatcher checks `get_queue_list()`, falls back to `long` with a System-Health warning ([jobs §1.2]) |
| DFP installed alongside | one app silently loses `File` override (`base_document.py:91`) | `after_install`/`after_migrate` check `frappe.get_hooks("override_doctype_class")["File"]` and `write_file` ownership; throw/log loudly |
| stock_ledger old-app sentinel (`stock_ledger.py:416`) | branch never fires post-rename | intentional: fallthrough `get_full_path()+open("wb")` is now safe via materialize + write-back (contract #17) |
| v16/develop upgrade drift (`get_content(encodings=...)` becomes core) | our signature is the v16 superset already | signature-compat test matrix in CI against pinned frappe revisions ([rename §5]) |

---

## 13. Ordered Implementation Steps

Prereq (other domain, listed for sequencing): bench unbreak + package rename per [rename §6 Phase A/B]; compat rename patch `[pre_model_sync]`, guarded no-op on this bench.

1. **Skeleton + rename completion**: package layout §1, hooks.py with only `after_install/after_migrate`, modules.txt, empty doctype dirs. *Accept*: `bench --site finstein.erp list-apps` works; app installs on a scratch site; zero behavior change.
2. **Typed exceptions + hashing + keys + modes** (`storage/exceptions.py`, `hashing.py`, `keys.py`, `modes.py`). *Accept*: unit tests — key format golden tests (no filename/PII, shard prefixes), one-pass md5+sha256 equals hashlib reference, `CloudObjectNotFound` isinstance `FileNotFoundError`, transport error is not.
3. **Cloud Storage Settings + Ignored DocType doctypes** with validators + `test_connection`. *Accept*: settings save validates pairing/HTTPS/mode gates; `test_connection` full round-trip green against MinIO CI service; mode transition to S3_ONLY blocked with actionable message on a site with unmanaged Files.
4. **Cloud Storage Object doctype + `storage/objects.py`** (ensure_cso, lock, transitions, derived count) + indexes in `install.after_migrate`. *Accept*: concurrency test — two threads `ensure_cso` same identity → one row; delete-vs-link race test leaves object undeleted; unique indexes present (`SHOW INDEX`).
5. **StorageEngine + client** (TransferConfig, ExtraArgs, presign, verify, copy_visibility, abort-multipart-in-finally). *Accept*: MinIO integration — put/head/verify/presign(disposition pinned)/copy/delete round-trip; SSE args asserted in mocked AWS test; no `ACL` key ever present (regression test).
6. **`core_hooks.write_file` dual-convention + `before_write_file` + `doc_events.file_after_insert`**. *Accept*: contract tests — File-doc insert via `upload_file` shape sets canonical `file_url` + linked CSO in every mode; legacy `frappe.utils.file_manager.save_file(fname, content, dt, dn, is_private=1)` (e-waybill shape) returns dict and links after insert (#12); dedup insert of identical bytes reuses CSO with 2 links; DB-collision rename test (two same-named different-bytes files get distinct URLs); no `frappe.db.commit` inside hook (assert via patched commit).
7. **CloudFile override** — all §5 methods + registration. *Accept*: the 19-invariant suite: `get_content()` str/bytes matrix (PNG, PDF, gz, xlsx, zip, UTF-8 text), `get_content(encodings=[])` byte-identical (#1-#4), absent object → `FileNotFoundError` subclass, S3 500 → not (#5); `File.save()` of cloud-only row passes validate (#6); `unzip`, `read_xlsx_file_from_attached_file`, amend-copy, `optimize_file(overwrite)` green with zero local canonical copy (#7 #11); `exists_on_disk` dedup (#14); is_private flip moves pub↔prv key + propagates parent field (#16 #18).
8. **`delete_file_data_content` + GC** (`gc.py`: deferred delete, orphan sweep, repair dispatcher). *Accept*: trash last File → CSO `pending_delete`, object still in bucket; GC after grace deletes + tombstones; trash one-of-two sharers → object untouched; hash-shared-across-privacy edge covered by sweep test.
9. **Materialization cache + write-back** (`cache/*`, `after_request`/`after_job` hooks). *Accept*: bank_statement_import-shaped test (open `get_full_path()` "w", write, end request) → new CSO linked, `content_hash` updated; stock_ledger gz-shaped test in an RQ job; concurrent materialize under filelock produces one download; eviction respects dirty entries.
10. **Private serving patch** (`runtime_patches` + `serving/private.py`). *Accept*: session user w/o permission → 403 (gate preserved); permitted → 302 with `X-Amz-Expires=<private_presign_ttl>` and pinned disposition; Access Log row written; token-auth API request follows redirect to bytes; DUAL_WRITE local copy → served by original path (X-Accel header honored); site without app → original behavior byte-identical.
11. **Public renderer + dev-statics shim** (`serving/public.py`). *Accept*: with nginx-simulated fallthrough (direct `get_response()` test), missing public S3-backed file 302s to CDN URL when configured, presign otherwise; non-File `/files/...` miss falls through to 404 (renderer declines); html/svg forced attachment; dev `bench serve` miss serves via shim.
12. **Legacy alias** (`api/compat.py` + `override_whitelisted_methods`). *Accept*: GET `/api/method/frappe_s3_attachment.controller.generate_file?key=...` resolves through the remap (test via `frappe.override_whitelisted_method`), enforces per-File permission, 302s; guest → 403.
13. **External-install compat patch** (`patches/v1_0/...`, `[pre_model_sync]`, double-guarded per [rename §6.13-14]): Module Def rename, Patch Log rewrite, `s3_object_key` → `legacy_unverified` CSOs + link, batched `tabFile.file_url` rewrite for old `/api/method/frappe_s3_attachment...` private URLs. *Accept*: no-op in <1s on all 13 live site DBs; on a synthetic 0.2.2 fixture DB, private URLs resolve post-patch via both old alias and new serving.
14. **Docs + CI**: README (deployment: `common_site_config.workers.cloud_migration` + supervisor worker + restart requirement per [jobs §1.2]; CloudFront OAC; IAM policy sample; bucket lifecycle), frappe-pinned CI matrix per [rename §5], MinIO job renamed env vars. *Accept*: CI green on `version-15` pin; pre-commit `files:` filter updated (silent-noop trap, [rename §6.7]).

Steps 1-5 have no user-visible behavior; the app is inert in LOCAL_ONLY until step 6+, and every step leaves the site operational (mission constraint). The migration engine (separate design) plugs into `ensure_cso`, `engine.verify`, `Cloud Migration *` doctypes, and the `cloud_migration` queue defined here.

### Critical Files for Implementation
- /home/user/v15/apps/cloud_file_storage/cloud_file_storage/overrides/file.py — the CloudFile subclass carrying all 19 contract invariants
- /home/user/v15/apps/cloud_file_storage/cloud_file_storage/core_hooks.py — dual-convention write_file / delete_file_data_content refcount seam
- /home/user/v15/apps/cloud_file_storage/cloud_file_storage/storage/engine.py — S3 engine (put/verify/presign/copy/GC-delete, SSE, TransferConfig)
- /home/user/v15/apps/cloud_file_storage/cloud_file_storage/serving/private.py — patched download_private_file + presign policy (with runtime_patches.py)
- /home/user/v15/apps/cloud_file_storage/cloud_file_storage/cloud_file_storage/doctype/cloud_storage_object/cloud_storage_object.py — CSO state machine + locking/refcount choke-point