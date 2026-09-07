# Architecture Decision Records — Index (FROZEN at P0.5)

Binding order of precedence: `docs/PLAN.md` > `docs/adr/amendments-register.md` (A1–A36)
> numbered ADRs below > `docs/design/*` (source material with reconciliation headers).
New ADRs are appended during phases (never renumbered); decisions graduate here from
`docs/DECISIONS.md`.

| # | Decision | Owner domain |
|---|---|---|
| 0001 | Integration seams: `write_file`/`before_write_file`/`delete_file_data_content` hooks + minimal `CloudFile(File)` via `override_doctype_class` — never doc_events-after-write, never wholesale File reimplementation | runtime |
| 0002 | Cloud Storage Object = single source of physical identity; content-addressed key `<prefix>/<site>/<pub|prv>/<sha2>/<sha2>/<sha256>`; filename never identity (FD-4) | runtime |
| 0003 | `get_content(self, encodings=None)` superset semantics (no-arg = exact v15; `[]` = raw bytes; list = v16 semantics); typed errors; FileNotFoundError only on true absence | runtime |
| 0004 | `file_url` stays canonical `/files`/`/private/files`; signing only at serve time; private = cloud-aware `download_private_file` interception post-`validate_auth` (P3 spike-proven); public = static fast path + DB-verifying renderer | runtime |
| 0005 | Derived refcount (query-at-delete) + deferred GC: `pending_delete` → grace → locking recount → delete + tombstone; tombstones revive on re-upload (A25) | runtime |
| 0006 | Materialization cache with write-back: every handed-out path tracked, own-transaction commits (A3/A4), bounded LRU/TTL | runtime |
| 0007 | `s3_key_generator` hook NOT honored; loud deprecation detector (FD-9) | product |
| 0008 | Greenfield rename to cloud_file_storage; fresh doctypes; guarded `[pre_model_sync]` compat patch + out-of-band bootstrap for external installs (A21) | product |
| 0009 | Keep legacy `s3_object_key` field (deprecated, install-created); authority is the `cloud_storage_object` Link (A13) | product |
| 0010 | Dedicated `cloud_migration` queue; batched jobs (default 1000); MariaDB source of truth; Redis = dispatch hint | migration |
| 0011 | UPLOAD/VERIFY/CLEANUP phase separation; independent verification before any local delete; CLEANUP re-verifies remote AND local (A4) | migration |
| 0012 | Conflict queue for legacy anomalies; healthy URLs never renamed (FD-3); alias(302) + parent-field consistency + audit for unavoidable changes | migration |
| 0013 | Separate `Cloud Backup Settings` Single + isolated backup bucket/prefix | product |
| 0014 | Lifecycle generator with economics validator; prefix-scoped, merge-not-replace (A20); live bucket sync-retrieval only (FD-5) | product |
| 0015 | Custom Page + throttled publish_realtime for the migration dashboard; Workspace Number Cards for slow counts | product |
| 0016 | Frappe floor v15.16.0; 3-ref matrix {v15.16.0, v15.93.0, version-15} (A34, FD-8); v15-only, MariaDB-only for v1 | product |
| 0017 | Cloud Storage Manager role + permlevel-guarded credentials; destructive actions SM-only + type-to-confirm | product |
| 0018 | C1–C19 contract module as merge gate on every supported ref (A22) | product |
| 0019 | Legacy `/api/method/frappe_s3_attachment...` URLs served via `override_whitelisted_methods` remap to `api.compat.legacy_generate_file` (A10); 302 aliases (A11) | product |
| 0020 | IAM default credential chain preferred; SSE-S3 default / SSE-KMS optional; bounded request-path timeouts + circuit breaker (A30) | runtime |
| 0021 | Thumbnails = cloud-managed derived objects of the source CSO; never deleted before source verified + availability guaranteed; no permanent local thumbnail persistence post-S3_ONLY (PLAN.md §A) | runtime |
| 0022 | Data Import / Prepared Report / Package Import = LOCAL_OPERATIONAL / TEMPORARY_LOCAL, mode-exempt, reported distinctly from cloud-managed bytes (PLAN.md §A) | product |
| 0023 | Public HTML edge = Policy B: `.html/.htm/.svg/.xml` always forced-attachment (FD-7) | runtime |
| 0024 | Autonomous execution contract + hard-stop taxonomy (PLAN.md §D/§E); one write-capable agent per worktree; genuinely read-only reviewers (guard-enforced) | orchestration |

Amendments A1–A36: see `amendments-register.md` (binding).
