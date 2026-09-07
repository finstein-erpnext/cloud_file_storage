> **DISPOSITION (P0.5): ALL FINDINGS CLOSED.** Every confirmed finding in this report
> was bound as a fix in `docs/adr/amendments-register.md` (round-1 reports → A1–A24;
> final-round reports → A25–A36) and carries a named regression test and phase. Nothing
> in this file is an OPEN issue. Paths like `scratchpad/design|research/...` refer to
> the planning workspace — the same documents now live at `docs/design/` and
> `docs/research/`.

I have verified the plan and all three design docs against frappe v15.93.0, erpnext, india_compliance, the fork, and the mission requirements (`docs/REQUIREMENTS.md`). Report follows.

---

# Adversarial Review — Data Safety / Loss / Corruption Lens

## BLOCKERS

---

**[SEVERITY: BLOCKER] [CONFIRMED] `write_file` sets `cloud_storage_object`/`content_hash` only in memory; india_compliance's `save_file(overwrite=True)` never calls `.save()`, so the relink is never persisted — the new bytes become an unreferenced CSO that GC destroys while the File silently reverts to the old content.**

- Evidence:
  - `/home/user/v15/apps/frappe/frappe/core/doctype/file/file.py:711-715` — `save_file()` calls `write_file_method(self)` and **returns its dict to the caller**; it performs no DB write to `tabFile`.
  - `/home/user/v15/apps/frappe/frappe/core/doctype/file/file.py:807-808` — core's own caller does `self.save_file(...)` **then `self.save()`**.
  - `/home/user/v15/apps/india_compliance/india_compliance/gst_india/doctype/gst_return_log/gst_return_log.py:103-104` — `file.save_file(content=content, overwrite=True)` followed **only** by `self.db_set(file_field, file.file_url)`. **No `file.save()`.** This is the app the mission names as the strict compatibility target.
  - `design/runtime-storage.md:312` — "5. Set `file.cloud_storage_object = cso.name`." (in-memory only); `:434` — "New sha256 → new/different CSO → relink `cloud_storage_object`, decrement old CSO (→ `pending_delete` when last)".
- Why it breaks the plan: after `update_json_for()`, (a) the new CSO has **zero** `tabFile` references → the daily orphan sweep (`design/runtime-storage.md:460`, "derived-refcount-0 + age > grace → pending_delete") physically deletes the new GST JSON from S3; (b) `tabFile.cloud_storage_object` still points at the **old** CSO, so `get_json_for()` silently returns the previous period's data; (c) the old CSO was fast-tracked to `pending_delete`, and `serving/private.py`'s gate is `cso.status in ("uploaded","verified","legacy_unverified")` (`design/runtime-storage.md:371`) → `/private/files/...` now 404s for a file that exists; (d) `tabFile.content_hash` keeps the stale MD5, poisoning core's dedup (`file.py:687`) and the `_delete_file_on_disk` refcount gate (`file.py:511-519`). Contract #11/#15 are asserted as satisfied but are not.
- Suggested fix: `_write_file_doc` must persist its own mutations when `not file.is_new()` — `file.db_set({"cloud_storage_object":…, "content_hash":…, "file_size":…}, update_modified=False)` inside the hook — and must **never** decrement/schedule deletion of the old CSO until the new link is durable. Add a contract test that calls `save_file(overwrite=True)` **without** a following `.save()` and asserts `tabFile.cloud_storage_object` changed.

---

**[SEVERITY: BLOCKER] [CONFIRMED] Materialization write-back runs in `after_request`/`after_job`, both of which execute *after* the framework's final commit/rollback and are followed by connection teardown — every `db_set` the sync-back performs is discarded.**

- Evidence:
  - `/home/user/v15/apps/frappe/frappe/app.py:139-155` — `else: rollback = sync_database(rollback)` … `finally:` → `frappe.db.rollback()` (unsafe methods) → `run_after_request_hooks(request, response)`. No commit follows.
  - `/home/user/v15/apps/frappe/frappe/app.py:416-423` — `sync_database` commits (unsafe) or **rolls back** (safe/GET) *before* the `finally` block.
  - `/home/user/v15/apps/frappe/frappe/app.py:78-88` — `ClosingIterator(..., frappe.destroy)`; `frappe/__init__.py:423-428` — `destroy()` does `db.close()` (no commit).
  - `/home/user/v15/apps/frappe/frappe/utils/background_jobs.py:255-268` — `else: frappe.db.commit()` … `finally:` → `after_job` hooks → `frappe.destroy()`. Hooks run **after** the commit, and also run on the exception path after `frappe.db.rollback()`.
  - `design/runtime-storage.md:347` — "relink `File.cloud_storage_object`, update `content_hash`, `file_size` via `db_set(update_modified=False)`"; `design/runtime-storage.md:315` — "No `frappe.db.commit()` anywhere in the hook". No commit is specified anywhere in `writeback.py`.
- Why it breaks the plan: the new bytes are uploaded to a new CSO (irreversible side effect) but the File keeps pointing at the old CSO, and the sidecar is rewritten "clean". Reads succeed from cache until TTL/size eviction (`cache_ttl_hours` 72), then fall back to the **old** CSO → silent content reversion. For `stock_ledger.create_json_gz_file` this means repost valuation data reverts to a stale snapshot. The new CSO has 0 refs → orphan sweep destroys it. This defeats contract #8 and the plan's P2 acceptance criterion "bank_statement_import-shaped test → new CSO linked, `content_hash` updated" (`design/runtime-storage.md:483`).
- Suggested fix: `flush_request`/`flush_job` must open and commit their own transaction (`frappe.db.commit()` after the `db_set`, guarded by try/except so a failure cannot poison teardown), must **skip** entirely when the enclosing transaction was rolled back, and must be covered by a test that asserts the value after `frappe.db.rollback()` + re-read from a fresh connection.

---

**[SEVERITY: BLOCKER] [CONFIRMED] `get_content` cannot resolve bytes for a File row that has a `file_url` but no `cloud_storage_object`, which is exactly the shape core produces for amend/copy, `attach_files_to_document`, and `file_manager.save_file` — all break in S3-primary/S3-only modes.**

- Evidence:
  - `/home/user/v15/apps/frappe/frappe/core/doctype/file/file.py:107-113` — `before_insert` → `self.save_file(content=self.get_content())` for every non-remote File.
  - `/home/user/v15/apps/frappe/frappe/model/document.py:453-464` — `copy_attachments_from_amended_from` builds the doc with `file_url`/`file_name` only, then `_file.save()`.
  - `/home/user/v15/apps/frappe/frappe/core/doctype/file/utils.py:363-374` — `attach_files_to_document` builds `frappe.get_doc(doctype="File", file_url=value, …)` then `file.insert()` inside `try/except` → **silent** orphan on failure.
  - `/home/user/v15/apps/frappe/frappe/utils/file_manager.py:167-189` — after `write_file_method(fname, content, …)` (whose return is filtered to `["file_url","file_name"]`, `frappe/hooks.py:70`), the doc is built and `f.insert()`ed with no `content` key.
  - `design/runtime-storage.md:266` — `_read_bytes` order is: local path → cache → "**cloud-backed** → `engine.get_object_bytes(cso)`" → raise; `:265` — `_cso()` is "cached `frappe.get_doc` of `self.cloud_storage_object`". A brand-new doc has no link.
  - Contradicted claim: `design/runtime-storage.md:435` — "Amend/copy … our `get_content` works for cloud-only rows".
- Why it breaks the plan: T-AMEND, T-ATTACH and contract #12 are listed as P2 acceptance gates and cannot pass. `attach_files_to_document` swallows the exception → Attach-field values silently lose their File row (contract #9's stated failure mode). e-Waybill (`e_waybill.py:741`) and `import_supplier_invoice.py:104` fail at insert.
- Suggested fix: `_read_bytes` must add a resolution step *before* raising: resolve a CSO from `self.content_hash` + `is_private`, else from a sibling `File` row with the same `file_url` and a non-empty `cloud_storage_object`. Add contract tests that insert a File with `file_url` only (no content, no link) in S3_ONLY.

---

**[SEVERITY: BLOCKER] [CONFIRMED] In DUAL_WRITE (and S3_PRIMARY with a local fallback copy) `get_full_path()` returns the canonical local path and registers **no** write-back tracking — writes through that path never reach S3, and CLEANUP later quarantines the only correct copy.**

- Evidence:
  - `design/runtime-storage.md:272` — `get_full_path`: "If local canonical path exists → **return it** (core-identical result). Else if cloud-backed … → `cache.materialize(self)`".
  - `design/runtime-storage.md:344` — write-back tracking is appended **only inside `materialize()`**: "append `(file_name, path)` to `frappe.local.cfs_materialized`".
  - `/home/user/v15/apps/erpnext/erpnext/stock/stock_ledger.py:420-422` — `path = file_doc.get_full_path()` … `with open(path,"wb") as f: f.write(compressed_content)` — no `save()`, no hash update.
  - `design/migration-engine.md:434` — CLEANUP quarantines `disk_path` after only a **remote** `head_object` re-check.
- Why it breaks the plan: DUAL_WRITE is the mode the plan says the site runs in for the entire 1–2 day migration (plan line 372, 270). Every repost-item-valuation / bank-statement-import write during that window mutates the local file only. VERIFY already linked the File to the CSO holding the **pre-write** bytes. CLEANUP then removes the local copy → the site permanently serves stale content. This is precisely the erpnext sentinel path the plan claims is "moot by design" and proven safe by T-RIV (plan line 289, `design/delivery-platform.md:77`).
- Suggested fix: register write-back tracking for **every** path returned by `get_full_path()` (local canonical included) whenever the File is cloud-backed, with mtime/size recorded at hand-out time; or make CLEANUP re-stat/re-hash the local file against `cso.content_sha256` immediately before quarantine.

---

**[SEVERITY: BLOCKER] [CONFIRMED] Backup verification compares a full-object SHA256 against a multipart composite checksum and, on mismatch, **deletes the freshly uploaded artifact** — no backup can ever succeed on a real site.**

- Evidence:
  - `design/delivery-platform.md:214` — "for each artifact: stream SHA256 → `upload_file` with `ExtraArgs={ChecksumAlgorithm:'SHA256', StorageClass:'STANDARD', SSE...}` → `head_object` compare size + checksum". **No `Config=TransferConfig(...)` and no `ChecksumMode="ENABLED"`.**
  - Verified in this bench's venv: `boto3 1.34.162 / s3transfer 0.10.4`, `TransferConfig().multipart_threshold == 8388608` (8 MiB). Any artifact >8 MiB is uploaded multipart, so S3 returns a composite `ChecksumSHA256` of the form `<b64>-N`, never the full-object SHA256. `head_object` also omits checksum fields unless `ChecksumMode="ENABLED"` is passed.
  - `design/delivery-platform.md:256` — "Checksum mismatch on verify | corrupt remote artifact | **delete remote object**, raise `BackupVerificationError`, status Failed — never logged Success".
  - The sibling design already knows this: `design/migration-engine.md:19` (ADR-M7) — "composite `ChecksumSHA256` ≠ full-object SHA256 on boto3 1.34.162".
- Why it breaks the plan: a 1.2M-file site has a multi-hundred-MB DB dump. Every scheduled backup uploads it, fails verification, deletes it from S3, and logs Failed — an infinite loop that produces **zero** off-site backups while reporting a clean failure path. This violates R-60/R-63 and the mission's "verified upload" claim (B4).
- Suggested fix: pass an explicit `TransferConfig` with a threshold above the largest expected artifact **or** verify by `ContentLength` + a streamed re-GET SHA256 (the ADR-M7 strategy), always pass `ChecksumMode="ENABLED"` to `head_object`, and **never delete the remote artifact on verification failure** — retain it and mark the log Failed.

---

**[SEVERITY: BLOCKER] [CONFIRMED] The refcount safety story is not sound: the GC's "recount before physical delete" is a non-locking read under MariaDB REPEATABLE READ, and the CSO row lock does not serialize the new-reference side at all.**

- Evidence:
  - `design/runtime-storage.md:231` — "Physical `DeleteObject` happens only in `gc.run_deferred_object_gc`, which re-locks, re-counts live `tabFile` references (`frappe.db.count("File", {"cloud_storage_object": name})`), re-checks grace, then deletes".
  - `/home/user/v15/apps/frappe/frappe/database/database.py:1263-1278` — `db.count` is a plain `SELECT COUNT(*)`, non-locking. Frappe sets no isolation level for MariaDB (`grep -rn ISOLATION frappe/database/` returns only the Postgres path at `postgres/database.py:185`), so the server default REPEATABLE READ applies: subsequent non-locking reads in the same transaction serve the snapshot established by the transaction's first read.
  - `design/runtime-storage.md:236` — ADR-4's claim: "both `ensure_cso_link` (link/increment side) and the delete decrement acquire `FOR UPDATE` on the CSO row before reading/altering references, **serializing** concurrent 'last delete' vs 'new reference'". But `ensure_cso_link` is registered on `after_insert` (`design/runtime-storage.md:96`, `:333`) — the `tabFile` row is already inserted before any CSO lock is taken, and a row lock on `tabCloud Storage Object` cannot prevent inserts into `tabFile`.
- Why it breaks the plan: a GC batch job that processes many CSOs in one transaction recounts against a stale snapshot → `count == 0` for an object a concurrent transaction has already committed a reference to → `DeleteObject` on shared bytes. This is the exact failure ADR-4 and the mission's R-33/R-73 exist to prevent, and the design presents deferred-GC-with-recount as the last line of defence.
- Suggested fix: make the recount a **locking** read (`frappe.db.get_all("File", filters={"cloud_storage_object": name}, limit=1, for_update=True)`) and/or commit before each object so a fresh read view is established; and drop the ADR-4 serialization claim in favour of the honest statement that only the deferred-GC recount protects the delete-vs-link race.

---

## MAJOR

---

**[SEVERITY: MAJOR] [CONFIRMED] CLEANUP re-verifies only the *remote* object before deleting the local copy; a local mutation between VERIFY and CLEANUP is never detected, so the local file is deleted while its bytes differ from the "verified" object.**

- Evidence: `design/migration-engine.md:433-434` — "4. **Fresh independent re-verification at deletion time**: `head_object` size+checksum again (cheap HEAD; catches remote-side loss between verify and cleanup). 5. Then: quarantine `os.rename(disk_path, quarantine_path)`". `design/migration-engine.md:432` — the gate is object status, CSO status, and `linked=1`; nothing re-reads `disk_path`. The claimed mitigation `design/migration-engine.md:528` ("File mutated during migration → VERIFY hash mismatch → Conflict `checksum_unstable`") only covers the upload→verify window, not the verify→cleanup window, which the plan's own timeline (plan line 521 "conflict triage → approve+cleanup") makes days long.
- Why it breaks the plan: the mission rule the plan quotes as its safety spine ("no local file deleted until **its** remote object is independently verified", ADR-M16, R-53) is satisfied only against the object's own recorded hash, not against the file being deleted. Combined with the DUAL_WRITE write-through gap above, this is the mechanism that actually destroys the newer bytes.
- Suggested fix: at CLEANUP time, `os.stat` the local file and compare `size` + `mtime` against `disk_size`/`disk_mtime` captured at UPLOAD; on any drift, re-hash and refuse (`cleanup_blocked` Conflict). Store `sha256`+`size`+`mtime` at upload for exactly this comparison.

---

**[SEVERITY: MAJOR] [CONFIRMED] `handle_is_private_changed` relinks only `self`, but core's `update_existing_file_docs` flips `is_private` and `file_url` for **every** File row sharing the MD5 — leaving newly-private Files pointing at `pub/`-prefixed, CDN-exposed objects, and simultaneously breaking their downloads.**

- Evidence:
  - `/home/user/v15/apps/frappe/frappe/core/doctype/file/utils.py:301-310` — `update_existing_file_docs` updates `file_url` **and** `is_private` on all rows `WHERE content_hash = doc.content_hash AND name != doc.name`.
  - `design/runtime-storage.md:276` — "relink `self.cloud_storage_object`, decrement old CSO … then replicate core's URL rewrite + parent propagation + `update_existing_file_docs` sibling rewrite … propagation preserved **verbatim**"; and "if CDN configured and flip was public→private: … **fast-track old pub CSO to `pending_delete`**".
  - `design/runtime-storage.md:246` (ADR-7/§4) — visibility is encoded in the key prefix `pub/` vs `prv/`; `design/runtime-storage.md:389,391` — the CloudFront/OAC behavior is scoped to `pub/*`.
  - `design/runtime-storage.md:371` — private serving accepts only `status in ("uploaded","verified","legacy_unverified")`.
- Why it breaks the plan: (a) sibling rows are now `is_private=1` with `/private/files/...` URLs while their CSO is still `visibility=public` under `pub/` — a file the site believes is private remains reachable through the public CDN behavior with a long TTL, and since keys are immutable and content-addressed the exposure is permanent (R-70, R-73); (b) the fast-track to `pending_delete` puts the siblings' CSO into a status the private server rejects → those files 404 while their bytes exist; (c) `exists_on_disk` (`design/runtime-storage.md:273`) also returns False for `pending_delete`, silently killing core dedup for that content.
- Suggested fix: relink **all** File rows touched by `update_existing_file_docs` to the new-visibility CSO in the same transaction; never fast-track a CSO to `pending_delete` outside the derived-refcount path; and include `pending_delete` in the read/serve status allow-list (deletion is gated by GC's recount, not by status).

---

**[SEVERITY: MAJOR] [CONFIRMED] The runtime GC has no concept of migration-owned objects; by ADR-M10 a migrated object has **zero** `tabFile` references between UPLOAD and VERIFY, so a paused or conflicted campaign's uploaded objects are orphan-swept and physically deleted.**

- Evidence: `design/migration-engine.md:22` (ADR-M10) — "`File.cloud_storage_object` is linked **only inside the VERIFY success transaction**"; `design/migration-engine.md:405` — "no File mutation yet (ADR-M10)". `design/runtime-storage.md:224` — "orphaned: set by reconciliation sweep (object in bucket without CSO row, **or CSO with 0 refs** and no transition history) ──▶ pending_delete after review"; `design/runtime-storage.md:460` — "daily GC sweep: derived-refcount-0 + age > grace → `pending_delete`". `object_delete_grace_days` default 7 (`design/runtime-storage.md:163`). The CSO schema's only migration awareness is a passive `migration_batch` Data field (`design/runtime-storage.md:204`) that no GC rule consults.
- Why it breaks the plan: the plan's own timeline includes multi-day pauses ("conflict triage", "operator approval", `Paused` campaigns). Objects stuck in `Uploaded` (failed batch, unresolved conflict, paused campaign) age past grace, get marked `pending_delete`, and are deleted — silently discarding uploaded bytes and forcing a full re-upload, or worse, being deleted *after* a later VERIFY linked them.
- Suggested fix: exclude CSOs whose `migration_batch` belongs to a non-terminal campaign from both sweeps, or count `Cloud Migration Object.cloud_storage_object` as a live reference in `live_reference_count()`.

---

**[SEVERITY: MAJOR] [CONFIRMED] The migration engine contains no ignored-doctype exclusion — files the operator deliberately kept local (Data Import, Prepared Report, Package Import) are uploaded and their local copies quarantined; Package Import bypasses the File API entirely and has no fallback.**

- Evidence: `grep -n "ignore\|Ignored\|ignored" design/migration-engine.md` returns exactly one hit — `insert(ignore_permissions=True)` at `:226`. The campaign schema (`:88-108`) has only `include_public`/`include_private` scope; CLASSIFY (`:287-305`) and CLEANUP (`:427-435`) have no ignored-doctype filter. Meanwhile `design/runtime-storage.md:159` seeds `Data Import`, `Prepared Report`, `Package Import` as ignored and `:305` makes `write_file` return `save_file_on_filesystem()` for them, and `design/runtime-storage.md:159` cites `package_import.py:47-56` / contract §2 #16: **Package Import bypasses the File API entirely**.
- Why it breaks the plan: CLEANUP removes the local bytes for a doctype the runtime layer will never serve from S3 for that consumer, and Package Import reads the disk path directly — it cannot fall back to materialization. R-73 ("no deletion flow can remove data … outside its declared scope") is violated.
- Suggested fix: SCAN_DB must join `attached_to_doctype` against `Cloud Storage Settings.ignored_doctypes` and classify those rows `Skipped`; add a CLEANUP assertion that refuses any object whose refs include an ignored parent doctype.

---

**[SEVERITY: MAJOR] [CONFIRMED] Thumbnails get migration objects and are uploaded (contradicting ADR-M17), but thumbnails have no File row — so the object ends up with 0 references (GC-deleted), the public renderer cannot serve it, and `delete_thumbnails_on_cleanup=1` destroys the only copy.**

- Evidence:
  - `/home/user/v15/apps/frappe/frappe/core/doctype/file/file.py:459-467` — `make_thumbnail` does `image.save(path)` into `public/` and `self.db_set("thumbnail_url", thumbnail_url)`. **No File document is created.**
  - `design/migration-engine.md:267` — "7. If `thumbnail_url` set → **insert a thumbnail object row** (`is_thumbnail=1`, `parent_object` link) keyed by the thumbnail URL", vs `design/migration-engine.md:29` (ADR-M17) — "Thumbnails are **not uploaded** and not deleted by default".
  - `design/runtime-storage.md:389` — `PublicFileRenderer.can_render` requires `frappe.db.get_value("File", {"file_url": "/" + path}, …)` to return a cloud-backed row.
  - `design/migration-engine.md:424` — VERIFY's link write is `UPDATE tabFile JOIN tabCloud Migration File Ref` — a thumbnail object has no refs, so it no-ops.
- Why it breaks the plan: uploaded thumbnail objects have zero `tabFile` references → the runtime orphan sweep deletes them; the renderer cannot serve them because no File row carries that `file_url`; with `delete_thumbnails_on_cleanup=1` the local copy is quarantined and purged after 14 days → every Attach-Image thumbnail 404s permanently (T-ATTIMG / T-ITEMIMG regress).
- Suggested fix: honour ADR-M17 literally — do not create migration objects for `thumbnail_url` at all (record them only for `orphan_physical` accounting), and hard-refuse `delete_thumbnails_on_cleanup=1` unless a thumbnail serving path exists.

---

**[SEVERITY: MAJOR] [CONFIRMED] `ensure_cso` resurrects `pending_delete → verified` unconditionally, fabricating an "independently verified" state for objects that were never verified — defeating the S3_ONLY gate and the CLEANUP gate.**

- Evidence: `design/runtime-storage.md:432` — "found → return (**resurrect from `pending_delete` → `verified`** under the same lock)"; `design/runtime-storage.md:220-222` — `pending_delete` is reachable from `uploaded` (via decrement) as well as from `verified`. Gates that trust the status: `design/runtime-storage.md:423` — "`→ S3_ONLY` (hard gate): zero managed Files without a **`verified`** CSO"; `design/migration-engine.md:432` — "CSO status `Verified`".
- Why it breaks the plan: the mission's central invariant is "verified before delete". An object can now carry `verified` without a single HEAD/checksum having been performed, and both the mode gate and CLEANUP will then authorise removing local bytes.
- Suggested fix: resurrection must restore the object's **prior** status (`uploaded` or `verified`), recorded on the row when it entered `pending_delete`; never upgrade status as a side effect of resurrection.

---

**[SEVERITY: MAJOR] [CONFIRMED] Legacy public attachments (the fork's raw endpoint URLs) are `Skipped` by default while the plan mandates Block-Public-Access and forbids public-read ACLs — every legacy public file breaks, violating R-22.**

- Evidence:
  - `/home/user/v15/apps/cloud_file_storage/frappe_s3_attachment/controller.py:161-166` — public uploads use `ACL: "public-read"`; `:243-247` — public `file_url = f"{endpoint_url}/{bucket}/{key}"`.
  - `design/migration-engine.md:90` — `adopt_remote_https_rows` **default 0**; `:302` — "`remote_https` → `Skipped` unless `adopt_remote_https_rows`"; `:476` — "non-fork rows: `Skipped` by default".
  - `design/runtime-storage.md:391` — "**Explicitly NO public-read ACLs**"; `:444` — deployment checklist "Block Public Access ON".
  - `docs/REQUIREMENTS.md:24-25` (R-22) — "Existing stored `file_url` formats (local paths, `generate_file?key=...`, **raw endpoint URLs**) keep resolving **forever**."
- Why it breaks the plan: these URLs are stored in business fields (Item `image`, etc. — the fork wrote them via `frappe.db.set_value(parent, image_field, file_url)` at `controller.py:254-257`). Turning on the mandated bucket posture 403s them all, with no alias, no renderer coverage (they are not `/files/...`), and `is_remote_file=True` so `get_content` fails too.
- Suggested fix: default `adopt_remote_https_rows = 1` when the parsed host/bucket matches the configured bucket, adopt in place, canonicalise the URL with an alias + parent-field update, and make the bucket-posture runbook step a *post*-adoption gate rather than a prerequisite.

---

**[SEVERITY: MAJOR] [CONFIRMED] Legacy adoption backfills `tabFile.content_hash` from the S3 ETag using only a `'-' not in ETag` test — SSE-KMS/SSE-C single-part ETags are not MD5, so a bogus 32-hex value is written into the column core uses for dedup and for the delete refcount gate.**

- Evidence: `design/migration-engine.md:472` — "`md5 = ETag if '-' not in ETag else NULL` (single-part ETag is MD5; multipart unknown)"; `:473` — "backfill `content_hash` from md5 when known". The CSO schema itself records `sse_mode` (`design/runtime-storage.md:194`), so the information to make the check correct is available and unused. Consumers of that column: `/home/user/v15/apps/frappe/frappe/core/doctype/file/file.py:687-692` (upload dedup) and `:511-519` (`_delete_file_on_disk` refcount gate), plus `core/doctype/file/utils.py:308` (`update_existing_file_docs`).
- Why it breaks the plan: the plan's own contract #15 is "`content_hash` (MD5) must stay populated (core's refcounted delete depends on it)" — populating it with a *wrong* value is worse than leaving it NULL, because the delete gate then concludes "not shared" for content that is shared, and `update_existing_file_docs` groups the wrong rows.
- Suggested fix: only trust the ETag when `ServerSideEncryption` is absent or `AES256` **and** the ETag has no `-`; otherwise leave `content_hash` NULL and schedule the optional re-GET SHA256/MD5 backfill job the design already contemplates.

---

**[SEVERITY: MAJOR] [CONFIRMED] The external-install compat patch can never run: after the on-disk rename, `sites/apps.txt` and the `installed_apps` global still name `frappe_s3_attachment`, so `frappe.init` raises and `patch_handler` never sees `cloud_file_storage` — the upgrade bricks the site and the patch that would fix `installed_apps` is inside the unreachable patch.**

- Evidence:
  - `/home/user/v15/apps/frappe/frappe/__init__.py:1584-1595` — `_load_app_hooks` iterates `get_installed_apps(_ensure_on_bench=True)` and **re-raises** the `ImportError` for a missing app; `get_all_apps` reads `sites/apps.txt` (`__init__.py:1520-1524`), which still lists the old name. This is the exact break the plan documents on this bench (plan lines 96-99).
  - `/home/user/v15/apps/frappe/frappe/modules/patch_handler.py:89` — `for app in frappe.get_installed_apps():` — the raw DB global; `cloud_file_storage` is not in it, so `get_patches_from_app("cloud_file_storage")` is never called.
  - `design/delivery-platform.md:136` — step 5 of the patch: "`installed_apps` global: read … replace element … `set_global`" — the fix is *inside* the patch.
  - `design/delivery-platform.md:140` — the only documented operator prerequisite is "drain RQ queues".
- Why it breaks the plan: P8's deliverable is "external-install patch + synthetic legacy-site test" (plan line 378). The synthetic test would pass only because the test harness installs the app under the new name; the real upgrade path leaves the site unable to `frappe.init`, i.e. total outage requiring manual bench surgery.
- Suggested fix: ship a documented, **mandatory pre-migrate operator procedure** (edit `sites/apps.txt`, `pip uninstall`/`pip install -e`, and a `bench --site X execute` one-liner rewriting the `installed_apps` global) in `docs/runbooks/deployment.md`, and gate the patch's remaining steps on that state; alternatively ship a thin `frappe_s3_attachment` shim package that only re-exports `hooks.py` so `frappe.init` survives until the patch runs.

---

**[SEVERITY: MAJOR] [CONFIRMED] `apply_lifecycle_policy` uses `put_bucket_lifecycle_configuration`, which **replaces** the bucket's entire lifecycle configuration, and the generated expire/noncurrent/abort rules carry no prefix filter — violating R-61's "provably scoped to the backup prefix".**

- Evidence: `design/delivery-platform.md:233` — "`def apply_lifecycle_policy(confirm_phrase)` → `put_bucket_lifecycle_configuration`"; `:223-227` — only "Rule cfs-archive: **Filter.Prefix=backup_prefix**"; `cfs-expire`, `cfs-noncurrent`, `cfs-abort-mpu` are specified with no filter. `:235` — "`apply` diffs against `get_bucket_lifecycle_configuration` and shows the delta before write" — showing a delta is not preserving it. `docs/REQUIREMENTS.md:70` (R-61) — "pruning is **provably scoped** to the backup prefix."
- Why it breaks the plan: any pre-existing operator/compliance lifecycle rule on the backup bucket is silently destroyed on apply, and an unfiltered `Expiration.Days` rule will expire *every* object in that bucket, not just `backup_prefix/`. The two guards the design does have (`backup_bucket != attachment bucket`, `assert_synchronous_retrieval`) do not cover either failure.
- Suggested fix: put `Filter.Prefix = backup_prefix` on **every** generated rule; merge (not replace) with the existing configuration, preserving any rule whose `ID` does not start with `cfs-`; and make the type-to-confirm dialog list, verbatim, every existing rule that will be dropped.

---

**[SEVERITY: MAJOR] [CONFIRMED] The Desk button contract and the migration state machine disagree, and the "Delete Verified Local Copies" button has no mode gate — the plan's rulings reconcile doctype names but not statuses or the endpoint split.**

- Evidence: `design/delivery-platform.md:282` — the delivery design's campaign machine is `Draft → Analyzing → Planned → Running → Paused → Verifying → Verified → CleaningUp → Completed`; `design/migration-engine.md:85` — the actual `Cloud Migration Campaign.status` Select is `Draft\nAnalyzing\nAnalyzed\nPlanned\nRunning\nPaused\nStopping\nStopped\nCleanup Running\nCompleted\nFailed`. **`Verifying`, `Verified`, `CleaningUp` do not exist.** `design/delivery-platform.md:296` — "Delete Verified Local Copies … Valid states: **Verified**" with no operation-mode check, versus `design/migration-engine.md:28` (ADR-M16) — "CLEANUP allowed **only** in modes S3_PRIMARY_LOCAL_FALLBACK / S3_ONLY". Endpoint ownership also splits: `design/delivery-platform.md:56-58` fixes buttons at `cloud_file_storage.api.admin.*` while `design/migration-engine.md:39` and the plan (line 271) require the CLI and UI to share `migration/api.py`. Plan ruling #1 renames only `api/serve.py → api/compat.py`.
- Why it breaks the plan: the plan asserts a "single validation path" (line 273) and "every server-side role- and state-revalidated" (line 307), but P6/P7 (product-engineer) and P5 (migration-engineer) are built by different agents against two incompatible state vocabularies. The most destructive button in the product is the one whose valid-state check is written against a status that will never occur, and whose spec omits the mode gate that ADR-M16 calls the enforcement of the mission's safety rule.
- Suggested fix: add a binding ruling that `Cloud Migration Campaign.status` is the migration design's Select verbatim, that `api/admin.py` contains **only** thin `frappe.only_for` wrappers delegating to `migration/api.py`, and that `delete_verified_local_copies` is an alias of `start_cleanup` (approval + mode + per-object gates included).

---

**[SEVERITY: MAJOR] [CONFIRMED] The public renderer performs no permission check and no `is_private` check, and the analyzer deliberately routes `privacy_mismatch` rows (`is_private=1` with a `/files/` URL) into the CDN-exposed `pub/` prefix — turning a local, fixable inconsistency into a permanent public exposure.**

- Evidence: `design/runtime-storage.md:389` — `can_render` is `path.startswith("files/")` + a `frappe.db.get_value("File", {"file_url": …})` existence check (`frappe.db.get_value` bypasses permissions); `render()` 302s to CDN or a presigned GET with no `has_permission` and `make_access_log` only "when `audit_public_fallback`" (default 0, `:152`). `design/migration-engine.md:303` — "`privacy_mismatch=1` → Conflict (`ambiguous_privacy`, **Warning**) but object continues with **URL-derived** privacy" → `pub/` key. `design/runtime-storage.md:392` — "keys are content-addressed ⇒ objects immutable ⇒ long CDN TTLs are safe". `design/migration-engine.md:218` even defines an unused `Cloud File URL Alias.reason = privacy_fix`.
- Why it breaks the plan: today such a file is exposed only as long as it sits in the local `public/files`; after migration it is copied into a CDN-fronted prefix with an immutable key and long TTL, and after CLEANUP the local copy is gone but the S3 exposure remains — permanently, with no access log. R-70 ("no whitelisted endpoint exposes an object without … permission checks") and R-73 are stressed, and the `ambiguous_privacy` conflict is only a Warning, so it does not block.
- Suggested fix: make `ambiguous_privacy` a **Blocker** conflict (operator decides the true visibility before upload), and have `PublicFileRenderer.can_render` require `is_private = 0` on the resolved row, declining otherwise.

---

**[SEVERITY: MAJOR] [CONFIRMED] The frozen Settings schema still specifies `multipart_threshold_mb = 32 / chunksize 32`, contradicting binding ruling #5 and ADR-M8's 64 MB — at 32 MB, every 32–64 MB object is multipart while VERIFY still HEAD-compares, producing guaranteed false `checksum_mismatch` conflicts that cannot be retried without re-upload.**

- Evidence: `design/runtime-storage.md:161-163` — "`multipart_threshold_mb` Int default **32** | `multipart_chunksize_mb` Int default **32**"; plan line 350 (ruling #5) — "one shared pair — threshold **64MB**, chunk 16MB … runtime's 32/32 superseded"; `design/migration-engine.md:98` — `verify_strategy_cutover_mb` default **64**; `design/migration-engine.md:415` — "size < `verify_strategy_cutover_mb`: `head_object(…ChecksumMode="ENABLED")` → compare … `ChecksumSHA256 == b64(sha256)` (**single-part guaranteed by ADR-M8**)"; `:425` — "**no retry of verification without re-upload**". The plan itself says the merged schema is "frozen at P2 start … recorded in docs/design/runtime-storage.md" (plan line 347) — i.e. the document that still says 32.
- Why it breaks the plan: nothing validates `verify_strategy_cutover_mb <= multipart_threshold_mb`. An operator (or the shipped default) that breaks the coupling produces a stream of false corruption alarms on exactly the large files the operator most cares about, blocks CLEANUP for them, and burns re-uploads.
- Suggested fix: correct the schema defaults to 64/16, and add a hard validator in `CloudStorageSettings.validate()` and `Cloud Migration Campaign.validate()` asserting `verify_strategy_cutover_mb <= multipart_threshold_mb`.

---

**[SEVERITY: MAJOR] [CONFIRMED] Three stated mission requirements are contradicted by the plan without a recorded supersession.**

- Evidence:
  - `docs/REQUIREMENTS.md:13-15` (R-13) — "Object keys collision-safe and **customizable via the `s3_key_generator` hook (existing contract preserved)**" vs plan line 285 / `design/delivery-platform.md:73` (ADR R5) — "`s3_key_generator` hook is **not honored**; startup detector logs".
  - `docs/REQUIREMENTS.md:26-27` (R-23) — "**temporary** materialization with **guaranteed cleanup**; no temp-file leaks" vs `design/runtime-storage.md:156-157,351` — a persistent 5120 MB / 72 h LRU cache whose eviction **skips dirty entries** (unbounded when write-back fails, which per the BLOCKER above it always does).
  - `docs/REQUIREMENTS.md:50-51` (R-44) — "Frappe **>=15,<17**; MariaDB primary (**Postgres index path kept working**)" vs plan line 320 / `design/delivery-platform.md:91` — "`frappe-dependencies` tightened to `<16.0.0`", plan line 400 defers v16 entirely; and `design/migration-engine.md:264` uses `INSERT ... ON DUPLICATE KEY UPDATE` and `:419-423` uses `UPDATE tabFile f JOIN …` — both MariaDB-only syntax with no Postgres branch anywhere in the design.
- Why it breaks the plan: the plan's own governance ("DECISIONS.md append-only; anything architectural graduates to a numbered ADR") is not applied to requirement supersessions, so P8's release checklist and the acceptance-criteria doc will be evaluated against requirements the implementation deliberately does not meet.
- Suggested fix: add an explicit "Superseded requirements" section to `docs/REQUIREMENTS.md` (R-13 → ADR-0007, R-23 → cache ADR, R-44 → supported-versions ADR) with rationale, or narrow the scope claim; and either add Postgres query branches or state Postgres as unsupported in `docs/supported-versions.md`.

---

## MINOR

---

**[SEVERITY: MINOR] [CONFIRMED] `legacy_generate_file`'s "require permission on every File sharing the key" narrowing contradicts core's documented multi-parent sharing semantics and will 403 legitimate users.**

- Evidence: `design/runtime-storage.md:402` — "narrowed by resolving with `frappe.form_dict.fid` when present and otherwise requiring permission on **every** File sharing the key" vs `/home/user/v15/apps/frappe/frappe/core/doctype/file/utils.py:437-443` — "if the file is accessible from **any one** of those documents then it should be downloadable", which the plan itself calls "a supported core pattern our permission design must preserve" (plan line 68) and which the primary private path *does* preserve (`design/runtime-storage.md:379`).
- Why it breaks the plan: the same legacy URL behaves differently from the canonical URL for the same bytes; a user with read access via one parent gets 403. It also creates a logged-in existence oracle (distinct responses for unknown key vs. forbidden key).
- Suggested fix: use the identical `find_file_by_url`-equivalent any-one-readable gate, resolved over the CSO's File rows, and return an indistinguishable 403 for both "no such key" and "not permitted".

---

**[SEVERITY: MINOR] [CONFIRMED] `frappe.db.add_index` commits the caller's open transaction, so `ensure_analysis_indexes()` silently commits whatever the analyzer job had in flight.**

- Evidence: `/home/user/v15/apps/frappe/frappe/database/mariadb/database.py:420-425` — `if not self.has_index(...): self.commit(); self.sql("ALTER TABLE ... ADD INDEX ...")`. Called from `design/migration-engine.md:234-236` inside `start_analysis`.
- Why it breaks the plan: a hidden commit inside a transition function whose contract is "`SELECT … FOR UPDATE` on the campaign row → assert status → write → commit → audit" (`design/migration-engine.md:324`) releases the campaign row lock early, weakening the single-active-campaign CAS.
- Suggested fix: run `ensure_analysis_indexes()` as its own step before acquiring any campaign lock, and document the implicit commit.

---

**[SEVERITY: MINOR] [SUSPECTED] `ensure_cso_link` adopts a sibling's CSO by MD5 (`content_hash`), extending core's MD5-collision exposure into the object layer.**

- Evidence: `design/runtime-storage.md:335` — "find sibling `File` with same `content_hash` + `is_private` + non-empty link → **adopt link**"; `design/runtime-storage.md:317` — the legacy path's "linkage key = `content_hash` md5 → CSO lookup". CSO identity is SHA256 (`:431`) but the *adoption* path keys on MD5. Core already keys upload dedup on MD5 (`file.py:687-692`), so this is an amplification rather than a new class.
- Why it breaks the plan: a user who can upload two chosen-prefix MD5-colliding files can cause their own File row to be linked to another tenant's private object, which then serves through the permission-checked path as *their* file.
- Suggested fix: after MD5-based sibling discovery, confirm the candidate CSO's `file_size` matches and (cheaply) re-hash the local bytes to SHA256 before adopting; never adopt on MD5 alone.

---

**[SEVERITY: MINOR] [CONFIRMED] The `Cloud Storage Audit Log` "immutable" claim is a permissions convention, not an integrity guarantee, and the plan presents it as the audit trail for destructive actions.**

- Evidence: `design/migration-engine.md:226-227` — "no write/delete perms for anyone … Every destructive/mutating action writes one row **in the same transaction**". Frappe permissions do not bind Administrator, `frappe.db.sql`, or the same `ignore_permissions=True` path the writer uses; and the quarantine `os.rename` in `design/migration-engine.md:434` is a filesystem effect that cannot be transactional with the row.
- Why it breaks the plan: `docs/THREAT_MODEL.md` and the release checklist will claim an immutable audit trail that a System Manager can edit.
- Suggested fix: state the guarantee honestly (append-only by convention, no UI mutation) or add a hash-chain/`prev_hash` column so tampering is detectable.

---

## Verdict

**Not implementable as written.** The architecture is sound in its large decisions — content-addressed immutable keys, canonical `file_url`, the correct v15 hook seams, deferred GC, phase-separated migration with operator-gated cleanup — and the plan's citations of frappe v15.93 internals are accurate everywhere I checked them (`file.py:511-526`, `:687-715`, `:742-747`, `hooks.py:70`, `file_manager.py:163-166`, `app.py:105/126/210-211`, `middlewares.py:20-27`, `path_resolver.py:56-70`, `sync.py:143-177`, `override_whitelisted_method` at `__init__.py:2521`). But the design's data-durability spine has six independent breaks that each lose or corrupt bytes on a first-day production path, and they compound: the `write_file` hook never persists its own relink (so the app's single most important compatibility target, `gst_return_log.py:103`, silently reverts content and orphans the new object); the write-back mechanism writes into a transaction the framework has already committed and will never commit again; `get_content` cannot serve the three core flows that create a File from a `file_url` alone; `get_full_path` hands out an untracked local path in exactly the mode the migration runs in; the backup verifier compares incomparable checksums and then deletes the artifact; and the GC's "recount before delete" is a stale-snapshot read while ADR-4's serialization claim is simply not true of `after_insert`. Layered on top are scope gaps the migration design never addresses at all — no ignored-doctype exclusion, thumbnails without File rows, no CSO ownership by in-flight campaigns, remote-adoption defaults that break every legacy public URL under the mandated bucket posture — plus a compat patch that cannot bootstrap itself on the sites it targets and two designs that disagree on the campaign state machine that gates the most destructive button in the product. None of these are fatal to the architecture; all of them are fatal to a 1.2M-file/100GB cutover. The plan should be revised to (1) make persistence and commit boundaries explicit for every hook-side mutation, (2) resolve reads by `file_url`/`content_hash` and not solely by the doc's own link, (3) make every local-delete site re-validate the *local* bytes against the CSO hash immediately before deletion, (4) give the GC an "owned by an active campaign" exclusion and a locking recount, and (5) freeze one campaign state machine and one endpoint module before P5/P7 begin in parallel — after which the P0–P8 sequencing and the C1–C19 contract-gate strategy are a credible way to build it.