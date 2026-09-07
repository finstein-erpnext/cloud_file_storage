# Architecture Overview

Authoritative summary: `docs/PLAN.md` §A (binding). Detailed designs (source material
with binding reconciliation headers): `docs/design/{runtime-storage,migration-engine,
delivery-platform}.md`. Verified ground truth: `docs/research/` (six file:line-cited
reports). Precedence: PLAN.md > adr/ (incl. amendments-register) > design docs.

## Component map (target package layout)

```
cloud_file_storage/                    # python package (renamed in P1)
├── hooks.py                           # write_file/before_write_file/delete_file_data_content,
│                                      # override_doctype_class File, before/after_request,
│                                      # after_job, page_renderer, override_whitelisted_methods,
│                                      # scheduler_events (thin dispatchers only)
├── install.py                         # custom fields (cloud_storage_object, s3_object_key),
│                                      # indexes (tabFile.content_hash), role, seeds — idempotent
├── core_hooks.py                      # dual-convention write_file / delete hook / before_write_file
├── doc_events.py                      # File.after_insert ensure_cso_link
├── overrides/file.py                  # CloudFile(File): get_content(encodings=None),
│                                      # get_full_path, exists_on_disk, _resolve_cso chain, …
├── storage/                           # engine, client (timeouts+breaker), keys, hashing,
│                                      # objects (ensure_cso/adopt_references/set_cso_status),
│                                      # modes, exceptions
├── serving/                           # runtime_patches, private, public (renderer), disposition
├── cache/                             # materialize, writeback (own-commit), eviction
├── api/compat.py                      # legacy_generate_file, test_connection
├── migration/                         # api, analyzer, planner, engine, verify, cleanup,
│                                      # adoption, conflicts, aliases, reconcile, report, audit
├── backup/                            # tasks, lifecycle (generator+economics), restore
├── gc.py                              # deferred object GC, orphan sweep, repair dispatcher
├── commands.py                        # bench cfs-migrate-* CLI (same api as UI buttons)
└── cloud_file_storage/doctype/        # Cloud Storage Settings, Cloud Storage Ignored DocType,
                                       # Cloud Storage Object, Cloud Backup Settings,
                                       # Cloud Storage Backup Log, Cloud Migration Campaign/
                                       # Batch/Object/File Ref/Conflict, Cloud File URL Alias,
                                       # Cloud Reconcile Run, Cloud Storage Audit Log
```

## Critical flows (authoritative descriptions in PLAN.md §A)
1. **Upload**: core `save_file` → `write_file` hook → mode dispatch → content-addressed
   PUT (checksummed, SSE, no ACL) → `ensure_cso` → canonical `file_url`.
2. **Read**: `get_content`/`get_full_path` → `_resolve_cso` chain → local / cache /
   streamed S3 (typed errors; materialize-on-demand with write-back tracking).
3. **Serve private**: `/private/files/*` → patched `download_private_file` (post-auth) →
   permission gate + one access log → 302 presign (or local fallback / alias lookup).
4. **Serve public**: nginx/static hit → else renderer (DB-verified, is_private=0,
   no_cache) → CDN/presign; HTML/htm/svg/xml forced attachment (Policy B).
5. **Delete**: core refcount gate → `delete_file_data_content` hook → CSO decrement →
   deferred GC (grace, locking recount, tombstone).
6. **Migration**: analyze → plan → UPLOAD → VERIFY (guarded link) → operator-approved
   CLEANUP (fresh remote + local re-verify → quarantine) — dedicated queue, DB-driven.
7. **Backup**: scheduler dispatcher → forced-fresh dump → verified upload → Backup Log;
   lifecycle generator (scoped, merged) for the backup bucket only.

## erpnext repost sentinel note
`erpnext/stock/stock_ledger.py:416` tests `"/frappe_s3_attachment." in file_url` — our
canonical-URL invariant guarantees it never matches; correctness rests on materialized
`get_full_path` + write-back (contract #7/#8), proven by T-RIV in the ecosystem job.
