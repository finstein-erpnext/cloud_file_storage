# Source comparison — what this app took, what it left, and why

`cloud_file_storage` is a rebuild of a fork chain, not a new project, and the licence
requires that to be legible. This document says exactly what came from where.

## Lineage and licence

| Ancestor | Repository | Relationship |
|---|---|---|
| `frappe-attachments-s3` | `github.com/zerodha/frappe-attachments-s3` | Original. MIT. |
| `frappe_s3_attachment` (ALYF fork) | `github.com/alyf-de/frappe-attachments-s3` | Fork of the above; the 0.1.0–0.2.2 line this app continues from. MIT. |
| **`cloud_file_storage`** | this repository | Renamed and rebuilt from ALYF 0.2.2. MIT. |

`license.txt` carries all three copyright lines — zerodha contributors (2018), ALYF GmbH,
Finstein — and the MIT terms are unchanged. `pyproject.toml` names ALYF GmbH and Finstein as
authors. `CHANGELOG.md` keeps the 0.1.0–0.2.2 history with links pointing at the ALYF
repository, because those releases were published there and not here.

## What was taken

**The idea and the seams.** That a Frappe attachment can live in S3 while the File row stays
canonical; that `write_file` / `before_write_file` / `delete_file_data_content` are the hooks
to do it through; that private files must be served by a permission-checked signed redirect;
that some parent doctypes must be excluded from upload. All of that is the fork's design and
this app keeps it.

**Specific code and behaviour, carried deliberately:**

| Taken | Where it lives now |
|---|---|
| The `override_whitelisted_methods` remap keeping `frappe_s3_attachment.controller.generate_file` URLs alive | `hooks.py`, `api/compat.legacy_generate_file` |
| The `File.s3_object_key` custom field, and its index | `install.py` — kept, documented deprecated (ADR 0009 / A13) |
| The ignored-parent-doctype child table | `Cloud Storage Ignored DocType`, seeded with Data Import / Prepared Report / Package Import |
| `filetype`-based MIME detection (ALYF's replacement for `python-magic`) | `storage/` |
| The German locale | `locale/de.po`, regenerated |
| RFC 5987 non-ASCII filename handling in Content-Disposition | `serving/disposition.py` |
| The fork's bug reports as requirements | `content_hash` misuse (#10), duplicate detection (#12) — both fixed by the identity model |

## What was deliberately not taken

| Left behind | Why |
|---|---|
| **Per-object `public-read` ACLs** | A per-object ACL is a permission model no one can audit and many buckets now refuse outright (Block Public Access). One private bucket with `pub/`/`prv/` prefixes, and a bucket policy or CloudFront-OAC scoped to `pub/*`, is auditable in one place. **Breaking change** for installs that relied on direct public bucket URLs. |
| **Filename-derived object keys** (`{prefix}/{Y}/{M}/{D}/{doctype}/{rand}_{filename}`) | The key is the object's identity. Deriving it from a filename makes two identical files two objects, makes dedup impossible, and makes reference counting — and therefore safe deletion — unimplementable. Keys are now content-addressed SHA256. |
| **The `s3_key_generator` hook** | Same reason, from the other side: a site-supplied key breaks dedup, refcounting and GC at once. Running the hook and discarding its result would be worse than not running it, so it is **not honoured** and a detector reports loudly if a site still defines one (ADR 0007 / A24). |
| **Storing the object key in `File.content_hash`** | It is core's content-identity field; overloading it broke core's duplicate detection and crashed on private files (alyf-de#10, #12). `content_hash` is now always a real MD5, and a patch moves legacy values into `s3_object_key`. |
| **`run_migrate_existing_files`** — the fork's bulk upload | An unbatched, uncoordinated loop with no campaign, no compare-and-swap, no heartbeat, no verification and no way to pause. At 1.2M rows it is not a migration, it is an outage. Replaced by the migration engine: campaigns, batches of 1,000, per-object CAS, independent VERIFY, and an operator-approved CLEANUP. The two settings that belonged to it are dropped by a patch. |
| **Deleting the local file straight after upload** | The invariant this app is built around is that nothing local is deleted until its remote object has been independently verified — and re-verified at deletion time. The fork's `delete_file_from_cloud` switch has no equivalent here, and its value is reported as unmapped during adoption. |
| **Immediate physical S3 deletes on File delete** | Content-addressed objects are shared between File rows. A delete is now a reference-count decrement; the object goes to `pending_delete`, waits out a grace window, and is removed by GC only after a **locking** recount. |
| **The fork's doctypes** (`S3 File Attachment`, `S3 Ignored DocType Row`) | Renaming doctypes in place is the highest-risk operation in a rename (it can lose `__Auth` secrets and child rows). Fresh doctypes with the full frozen schema ship instead, and the bootstrap copies the fork's values across without touching the fork's rows (ADR 0008 / A21). |
| **`ping()`, `config/desktop.py`, `config/docs.py`, `tox.ini`, the QUnit stub** | Dead code. |

## What was added that has no ancestor

Content-addressed identity and the `Cloud Storage Object` doctype; deferred refcount-safe GC
with tombstone revival; the four operation modes with server-validated transitions; the
materialization cache and write-back; the cloud-aware private-serving interception and public
renderer; URL aliases; the migration engine end to end; backup, restore helpers and the
lifecycle generator with its economics validator; the Desk surfaces and health panel; the
C1–C19 compatibility contract; and the legacy-install bootstrap.

## Compatibility promises kept from the fork

Old `/api/method/frappe_s3_attachment.controller.generate_file?key=…` URLs resolve forever —
they are not rewritten in `tabFile.file_url`, and they also live in business fields, emails and
bookmarks where no patch could reach them (A10). `File.s3_object_key` still exists and is still
created on every install. A 0.2.x site adopts in place: see `docs/runbooks/deployment.md` and
`docs/evidence/p8-legacy-install-rehearsal.md`.
