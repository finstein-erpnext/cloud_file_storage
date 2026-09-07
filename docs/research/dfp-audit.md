> **STATUS (P0.5): HISTORICAL EVIDENCE.** Captured 2026-08-14 during planning; verified
> file:line ground truth at capture time. Where later decisions supersede content here,
> `docs/PLAN.md` / `docs/adr/` win. Known supersessions: P0 is COMPLETE (the "bench is
> currently broken" state described in rename-and-bench-reality §0 was RESOLVED by the
> ops-only .pth repair — see PROGRESS.md); the CI matrix/floor is {v15.16.0, v15.93.0,
> version-15} with `frappe>=15.16.0,<16.0.0` (A34), not the v15.0.0-based examples.

I have everything needed. Producing the report.

# DFP External Storage — Reference App Teardown

App root: `/home/user/v15/apps/dfp_external_storage_reference` — version `1.1.3` (`dfp_external_storage/__init__.py:2`), single runtime dep `minio` (`pyproject.toml:10-12`). Total Python: **1059 lines**, of which the entire storage engine is **one 757-line module**.

Bench context used for all core comparisons: `/home/user/v15/apps/frappe` @ **v15.93.0** (`frappe/__init__.py:54`).

---

## 1. INVENTORY

| Path (under `/home/user/v15/apps/dfp_external_storage_reference/`) | Purpose |
|---|---|
| `dfp_external_storage/hooks.py` | All wiring: `override_doctype_class` File, `page_renderer`, `doc_events` File before_save/after_delete, desk bundles. 212 lines, ~85% commented boilerplate. |
| `dfp_external_storage/dfp_external_storage/doctype/dfp_external_storage/dfp_external_storage.py` | **The whole app**: settings DocType controller, Minio wrapper, `File` subclass, S3 file proxy, save/delete hooks, page renderer, HTTP serve function (757 lines). |
| `.../dfp_external_storage/dfp_external_storage.json` | "DFP External Storage" DocType schema (connection + tuning settings). |
| `.../dfp_external_storage/dfp_external_storage.js` | Form script: folder link filters, "List files in bucket" button, defaults ignored-doctypes to Data Import / Prepared Report. |
| `.../dfp_external_storage/dfp_external_storage_list.js` | Hides name column in list view (4 lines). |
| `.../dfp_external_storage/test_dfp_external_storage.py` | **Empty stub** — 5 `# TODO` lines, zero tests (`test_dfp_external_storage.py:8-14`). |
| `.../dfp_external_storage_by_folder/*` | Child table (Table MultiSelect) mapping a Frappe folder → storage. |
| `.../dfp_external_storage_ignored_doctype/*` | Child table of DocTypes excluded from offload. |
| `dfp_external_storage/dfp_external_storage/custom/file.json` | Custom fields injected into core `File` + `field_order` property setter. |
| `.../page/dfp_s3_bucket_list/*.py|.js|.json|.html|.css` | Admin-only page listing raw bucket objects (orphan discovery); `roles: [Administrator]`. |
| `.../workspace/dfp_s3_storage/dfp_s3_storage.json` | Workspace with links to File / DFP External Storage / Error Log. |
| `dfp_external_storage/public/js/app.js` | Desk overrides: S3 cloud icon in FileView list/grid + File form. |
| `dfp_external_storage/public/scss/app.scss` | Icon styling; hides "New File" button inside the storage form. |
| `dfp_external_storage/patches.txt` | **Empty** — no migration patches at all. |
| `dfp_external_storage/change_log/**` | Human-readable release notes (v0.9.2 → v1.1.1). |
| `dfp_external_storage/config/desktop.py`, `config/docs.py` | Legacy module icon/docs config. |

No `templates/`, `www/`, `api/`, `tasks.py`, `utils.py`, `install.py`, no scheduler events, no bench commands.

---

## 2. DATA MODEL

### 2.1 `DFP External Storage` (connection; `dfp_external_storage.json`)
Autoname `format:DFP.ES.{bucket_name}.{YY}{MM}{DD}.{##}`, `track_changes: 1`, permissions: **System Manager only** (r/w/c/d).

| Field | Type | Notes |
|---|---|---|
| `type` | Select | Only value `S3 Compatible`, reqd |
| `title` | Data | reqd, descriptive name |
| `endpoint` | Data | `host:port` only (Minio style), reqd |
| `secure` | Check | TLS toggle, default `0` |
| `region` | Data | default `auto`, reqd |
| `bucket_name` | Data | reqd |
| `access_key` | Data | optional → env fallback `AWS_ACCESS_KEY_ID` / `MINIO_ACCESS_KEY` / `MINIO_ROOT_USER` (`dfp_external_storage.py:132-135`) |
| `secret_key` | Password | optional → env fallback `AWS_SECRET_ACCESS_KEY` / `MINIO_SECRET_KEY` / `MINIO_ROOT_PASSWORD` (`:145-148`) |
| `enabled` | Check | "**Write** enabled" — read still works when off (`:384-385`, `:450-453`) |
| `folders` | Table MultiSelect → `DFP External Storage by Folder` | folder→bucket assignment |
| `doctypes_ignored` | Table MultiSelect → `DFP External Storage Ignored Doctype` | opt-out per attached DocType |
| `files_within` | Int | display-only; recomputed live by `frappe.db.count("File", {"dfp_external_storage": name})` (`:117-119`) |
| `presigned_urls` | Check | default 0 |
| `presigned_mimetypes_starting` | Small Text | default `video/`, newline list |
| `presigned_url_expiration` | Int | default `10800` s (3 h) |
| `stream_buffer_size` | Int | default `10000000` (10 MB), floored to 8192 (`:81-83`, `:98-100`) |
| `cache_files_smaller_than` | Int | default `5000000` |
| `cache_expiration_secs` | Int | default `86400` |
| `remote_size_enabled` | Check | use S3 `stat_object().size` instead of File.file_size (INT(11) > 2 GB problem) (`:353-363`) |

### 2.2 `DFP External Storage by Folder` (child, `istable:1`)
Single field `folder` — **Link to `File`** (reqd). Uniqueness of folder→storage is enforced only client-side in `dfp_external_storage.js:31-49` (query filter excludes already-assigned folders); no server-side unique constraint.

### 2.3 `DFP External Storage Ignored Doctype` (child, `istable:1`)
Single field `doctype_to_ignore` — Link to `DocType` (reqd).

### 2.4 Custom fields on core `File` (`custom/file.json`, `sync_on_migrate: 1`)
* `section_break_dfp_a` (Section Break, after `uploaded_to_google_drive`)
* `dfp_external_storage` — **Link → DFP External Storage**, editable (this is the user-facing "move this file" control)
* `column_break_dfp_a_a`
* `dfp_external_storage_s3_key` — **Data, read_only 1** (the object key)
* Property setters: `File-main-field_order`, `in_list_view` on `dfp_external_storage`/`folder`/`file_size`/`file_name`, `folder.hidden = 0`.

**There is no file-tracking / object-registry DocType.** State lives entirely in two custom columns on `File`. No content-hash→object table, no refcount table, no migration-job table, no verification/audit trail.

---

## 3. FILE INTEGRATION MECHANISM

Mechanism = **`override_doctype_class`** (a real subclass, no monkeypatching) + **two `doc_events`** + a **`page_renderer`**:

* `hooks.py:100-102` → `override_doctype_class = {"File": "...DFPExternalStorageFile"}`
* `hooks.py:104-106` → `page_renderer = ["...DFPExternalStorageFileRenderer"]`
* `hooks.py:121-128` → `doc_events["File"] = {"before_save": hook_file_before_save, "after_delete": hook_file_after_delete}`

> Critical constraint for `cloud_file_storage`: `frappe/model/base_document.py:89-93` resolves `override_doctype_class[doctype][-1]` — **last app installed silently wins**. DFP + cloud_file_storage cannot coexist on `File`; there is no chaining/MRO merge.

### Overridden core methods (side-by-side)

| DFP (`dfp_external_storage.py`) | frappe v15.93 core (`frappe/core/doctype/file/file.py`) | Drift |
|---|---|---|
| `__init__(self, *args, **kwargs)` :312-313 | `Document.__init__` / `File.__init__` :~70-78 | pure pass-through, no drift |
| `@property is_remote_file` :315-317 → `True if self.dfp_external_storage_s3_key else super()` | `@property is_remote_file` :80-84 → `file_url.startswith(URL_PREFIXES)` else `not self.content` | signature same, **semantics inverted for S3 files**; core then skips `validate_file_path` (:204-206), `validate_file_url` (:215-217), `handle_is_private_changed` (:226-228), `generate_content_hash` (:424-426), `write_file` (:629-632), `save_file` (:647-655) |
| `validate_file_on_disk(self)` :540-541 → `True` if S3 else `super()` | :358-366 → throws `IOError` if path missing, returns `None` | returns `True` vs `None` (harmless), but bypasses existence validation |
| `exists_on_disk(self)` :543-544 → `False` if S3 else `super()` | :563-564 → `os.path.exists(self.get_full_path())` | signature same; **returning `False` disables core's dedup reuse** (:410-412 and :694-702) |
| `@frappe.whitelist() optimize_file(self)` :546-550 → raises `NotImplementedError` for S3 | :786-787, also whitelisted | same signature; behavior narrowed |
| `get_content(self) -> bytes` :577-587 | :566-592 `get_content(self) -> bytes` | **see §8** |
| `_remote_file_local_path_get(self)` :552-553 | **not a core method** | new |

### Not overridden but semantically required (silent breakage surface)
`get_full_path()` (core :594-627) — for an offloaded file `file_url` is `/file/<name>/<file_name>`, so `get_full_path()` resolves to a bogus **local** path; `make_thumbnail()` (core :435-474) falls into `get_web_image(self.file_url)` on a relative URL and silently returns `None` (thumbnails dead for S3 images); `_delete_file_on_disk()` (core :511-526) → `delete_file_data_content()` → `delete_file("/file/<name>/<fname>")` (`frappe/core/doctype/file/utils.py:153-170`) which splits the path and `os.remove()`s `site/private/files/<fname>` — an unrelated local file with the same basename can be deleted; `unzip()` (core :528+, uses `get_content()`), `save_file_on_filesystem()`, `write_file()`, `check_content()` (PDF-JS scan is skipped entirely for offloaded content).

---

## 4. WRITE PATH & KEY SCHEME

Flow for a new upload: core `before_insert` writes the bytes to **local disk** (`frappe/core/doctype/file/file.py:108-111` → `save_file`), then DFP's `before_save` hook uploads and deletes the local copy.

* Entry point `hook_file_before_save` (`:606-666`) → `dfp_external_storage_upload_file()` (`:376-435`).
* Storage resolution order (`dfp_external_storage_doc`, `:319-344`): 1) explicit `File.dfp_external_storage`; 2) storage whose `folders` child contains `File.folder`; 3) storage assigned to `Home`.
* Guards: ignored DocType (`:370-374`), storage disabled, `is_folder`, already has key, remote http(s) URL → `raise NotImplementedError` (`:391-393`).

**Key scheme** (`:399-400`):
```python
base, extension = os.path.splitext(self.file_name)
key = f"{frappe.local.site}/{base}-{self.name}{extension}"
```
→ `mysite.localhost/invoice-abc1234567.pdf`. Properties: site-prefixed, **filename-embedded, File-docname-embedded, unique per File row**, **no** content hash, **no** public/private separation, **no** folder path, **no** date sharding (one flat prefix per site — hot-prefix listing pain at 1.2M objects). Object metadata is deliberately **not** written (`:415-416`, `:649-650`: "Meta removed because same s3 file can be used within different File docs"), so there is no reverse object→File mapping in S3.

Upload is a single `put_object(data=open(f,'rb'), length=os.path.getsize(...))` (`:409-417`) — Minio auto-multiparts, but there is no explicit part-size/concurrency control, no checksum verification, no retry.

Local file is deleted **immediately after** the PUT with `os.remove(local_file)` (`:422`) — inside the same `before_save`, i.e. **before the DB commit**. A later rollback leaves the row pointing at a local path whose bytes are gone (core registers `on_rollback` handlers at :113/:643 that then cannot restore).

**Dedup:** effectively **none for S3**. Core's two dedup branches (`file.py:408-412` and `:694-702`) both gate on `duplicate_file_doc.exists_on_disk()`, which DFP forces to `False` for remote files (`:543-544`), so identical content is re-uploaded under a new key. The only sharing path is `dfp_file_url_is_s3_location_check_if_s3_data_is_not_defined()` (`:555-575`), which — when a File is *copied* with an existing `/file/...` URL (e.g. `copy_attachments_from_amended_from`) — parses the URL, copies `dfp_external_storage`, `dfp_external_storage_s3_key`, `content_hash`, `file_size` from the source File and sets `flags.ignore_duplicate_entry_error = True`. Note this helper is invoked **only from `get_content()`** (`:578`), never during `before_save`, so the sharing is opportunistic and lazy.

**`file_url` after offload** (`:421`, `:553`): `/file/{File.name}/{File.file_name}` — identical for public and private files. Consequence: public files stop being served by nginx and are proxied by Python forever.

**Storage changes** (`hook_file_before_save`, `:618-661`): local→remote (upload), remote→local (`download_to_local_and_remove_remote`, `:522-538` — full read into `self._content`, i.e. whole file in RAM), remote→remote (streamed copy via `S3FileProxy` then `remove_object` on the source, `:641-657`). On upload failure: new files silently fall back to local (`:427-432`); existing files `frappe.throw` (`:434-435`).

---

## 5. SERVE PATH

* **Route interception**: `DFPExternalStorageFileRenderer` (`:679-701`), registered via `page_renderer`. `PathResolver` puts hook renderers **first, ahead of `StaticPage`** (`frappe/website/path_resolver.py:56-70`). `can_render()` (`:692-696`) returns `True` purely on regex `file\/(.+)\/(.+\.\w+)$` — it never checks that the File exists or is remote, so any matching URL is swallowed and 404s instead of falling through.
* **Handler** `file(name, file)` (`:704-757`):
  1. Cache lookup `external_storage_public_file:{File.name}` **before** loading the doc — a cache hit returns the response with **no permission check and no filename check** (`:708-711`). Only public files are ever cached (`dfp_is_cacheable`, `:350-351`), so this is tolerable but fragile.
  2. `frappe.get_doc("File", name)`; `doc.file_name != file` → 404 (`:717-718`).
  3. **Permission gate**: `if not doc.is_downloadable(): raise frappe.PermissionError` (`:720-721`) → core `File.is_downloadable()` → `has_permission(self,"read")` (`frappe/core/doctype/file/file.py:757-758`, `:843-875`) — public files short-circuit to allowed (`:852-853`), private files delegate to the attached document's read permission.
  4. **Presigned redirect** (`:727-730`): `dfp_presigned_url_get()` (`:595-603`) issues a Minio presigned GET if `presigned_urls` is on and (optionally) the guessed mime type matches a `presigned_mimetypes_starting` prefix; then `frappe.flags.redirect_location = url; raise frappe.Redirect`, caught by `frappe/website/serve.py:28-29`. Applies to **private files too** — the resulting URL is bearer-capability, valid for `presigned_url_expiration` (default 3 h) to anyone who obtains it.
  5. Otherwise **stream through the server**: bytes if cacheable or smaller than the stream buffer (`:732-733`), else `wrap_file(...)` over `S3FileProxy` in `stream_buffer_size` chunks (`:517-520`, `:33-64`) with a manual `Content-Length` header (`:736`). No `Accept-Ranges`, no `Content-Disposition`, no ETag/Last-Modified, no 304 handling — HTTP range/seek only works via the presigned path.
  6. mimetype from `mimetypes.guess_type(file_name)` (`:589-593`, `:745-746`); `status = 200`; public+small responses cached in Redis for `cache_expiration_secs` (`:749-752`).
* **Cache invalidation**: only in `hook_file_before_save` (`:663-666`), i.e. on File save; not on delete.
* **Bug**: line `:749` `doc.dfp_is_cacheable()` sits **outside** the `try/except` that ends at `:740`; if `dfp_external_storage_doc` is `None` (storage row deleted/renamed) this raises `AttributeError` → 500 instead of 404.
* Second endpoint: `dfp_s3_bucket_list.get_info(storage, template, file_type)` (`page/.../dfp_s3_bucket_list.py:7-35`) — whitelisted, lists **every** object in the bucket via `list_objects(recursive=True)` with no pagination; page role-gated to Administrator, but the whitelisted method itself does not re-check the role.

---

## 6. DELETE SEMANTICS

Hook: `hook_file_after_delete` (`:674-676`, wired at `hooks.py:126`) → `dfp_external_storage_delete_file()` (`:437-460`):

```python
files_using_s3_key = frappe.get_all("File", filters={
    "dfp_external_storage_s3_key": self.dfp_external_storage_s3_key,
    "dfp_external_storage": self.dfp_external_storage
})
if len(files_using_s3_key):
    return
```

So: **yes, multiple File rows can point at one object** (only via the amend/copy path of `:555-575`), and the delete **is refcount-safe** — the guard runs on `after_delete`, so the row being deleted is already gone from the table and is not counted. Caveats:

* The count is an unbounded `get_all` (no `limit=1`) — full scan of matching rows; at scale, `dfp_external_storage_s3_key` has no index (plain Data custom field).
* Refcount is derived by **query at delete time**, not stored — no protection against a concurrent insert racing between the check and `remove_object` (no locking, no `FOR UPDATE`).
* If the storage is disabled, delete **throws** and blocks the File deletion (`:450-453`) — and if `dfp_external_storage_doc` is `None`, `self.dfp_external_storage_doc.title` on line `:452` raises `AttributeError` before the intended `frappe.throw`.
* Remote removal happens in `after_delete`, i.e. **before commit** — a rollback afterwards leaves a DB row whose object is gone. There is no tombstone/deferred-GC queue.
* Independently, core `on_trash` still runs `_delete_file_on_disk()` (`frappe/core/doctype/file/file.py:154-159, 511-526`) against `file_url = /file/<name>/<fname>` — see §3 note about collateral `os.remove` on `site/private/files/<fname>`.

---

## 7. MIGRATION TOOLING

**There is none.** Concretely:

* `patches.txt` is empty; no `install.py`, no `after_migrate`, no scheduler events (`hooks.py:130-149` fully commented), no bench command, **zero `frappe.enqueue` calls anywhere in the app** (grep across app: only a commented reference in `dfp_s3_bucket_list.js:113`).
* The documented bulk method (`README.md:55`) is Frappe's **list-view Bulk Edit**: select Files, set the `dfp_external_storage` field, which fires `before_save` per doc. That path is `frappe/desk/doctype/bulk_update/bulk_update.py:51-70`: <20 docs run **inline in the web request**; 20–500 docs are enqueued as **one** job on the `short` queue with `timeout=1000`; **>500 docs → `frappe.throw("Bulk operations only support up to 500 documents.")`**. For 1.2 M rows / 100 GB that is ~2400 manual batches on the shared `short` queue, each doing synchronous 10 MB-chunk network I/O.
* No enumeration/cursor, no batch/state tracking, **no resumability**, no idempotency key, no UPLOAD/VERIFY/CLEANUP phase separation, no verification of the remote object at all (no `stat_object`/ETag/hash comparison after PUT) — `os.remove(local_file)` at `:422` happens **in the same try block, immediately after the PUT succeeds**, which is exactly the "delete local before independent verification" pattern the mission forbids.
* Failure handling: `frappe.log_error` + fall back to local for new files, `frappe.throw` for existing (`:423-435`). Failures are only discoverable through Error Log; nothing is retried.
* Reverse migration (S3 → local) is `download_to_local_and_remove_remote()` (`:522-538`), which loads the **entire object into memory** (`self._content = response.read()`), then `save_file_on_filesystem()`, then deletes remote — again no verification between write and remote delete.
* The only reconciliation aid is the read-only `dfp-s3-bucket-list` page (unpaginated `list_objects`) for spotting orphan objects.

---

## 8. THE `get_content` FAILURE

**The code** — `dfp_external_storage.py:577-587`:
```python
def get_content(self) -> bytes:
    self.dfp_file_url_is_s3_location_check_if_s3_data_is_not_defined()
    if not self.dfp_is_s3_remote_file():
        return super(DFPExternalStorageFile, self).get_content()
    try:
        if not self.is_downloadable():
            raise Exception("File not available")
        return self.dfp_external_storage_download_file()
    except Exception:
        # ...do not give any information, so just raise a 404 error
        raise frappe.PageDoesNotExistError()
```

**Signature drift.** The override hard-freezes the arity at `get_content(self)`. Callers that pass `encodings` get `TypeError: get_content() got an unexpected keyword argument 'encodings'`. The concrete caller in this bench:

* `/home/user/v15/apps/india_compliance/india_compliance/gst_india/doctype/gst_return_log/gst_return_log.py:188` → `frappe.response["filecontent"] = file.get_content(encodings=[])` inside the whitelisted `download_file()` (`:180-190`) — the GST return JSON download. `encodings=[]` is the *idiom for "give me raw bytes, do not attempt any decode"*, which matters because the payload is gzip-compressed (`get_decompressed_data(file.get_content())` at `:55` and `:98`).

**Where the contract actually lives.** In frappe **v15.93.0** core the signature is `def get_content(self) -> bytes:` (`frappe/core/doctype/file/file.py:566`) — **no `encodings`**. The kwarg version `def get_content(self, encodings=None) -> bytes | str` was introduced in commit `45eabd32cd` ("feat(Data Import): custom delimiters", 2024-04-30) which is **not an ancestor of the v15.93.0 tag** — `git tag --contains 45eabd32cd` → `v16.0.0-beta.1/-beta.2/-rc.1` only. So the India Compliance call is written against the develop/v16 signature and is **already a latent TypeError on stock v15**; any app that owns `File` via `override_doctype_class` is the only place that can absorb it. DFP's override is the app that *should* have made it work and instead makes the incompatibility permanent and unfixable-from-outside (only one app can own the File class, `frappe/model/base_document.py:89-93`).

**Second, subtler break — the return-type contract.** Core decodes text payloads: `self._content.decode()` inside `try/except UnicodeDecodeError` (`file.py:583-590`) → **`str` for text, `bytes` for binary**. DFP's remote branch returns whatever `dfp_external_storage_download_file()` read from S3 — **always `bytes`** (`:501-515`). The same File therefore yields `str` while local and `bytes` after offload. Affected core callers that branch on type: `frappe/utils/csvutils.py:31-32` → `read_csv_content(fcontent)` (`:39-40` explicitly `if not isinstance(fcontent, str)`), `frappe/core/doctype/data_import/importer.py:435` and `:598`, `frappe/recorder.py:409` (`json.loads`), `frappe/core/doctype/prepared_report/prepared_report.py:96-97` (`gzip.decompress` — needs bytes, gets str locally), `frappe/email/email_body.py:251`, `frappe/email/doctype/email_queue/email_queue.py:393`, `frappe/utils/pdf.py:291` (`base64.b64encode`), `frappe/handler.py:286` (`download_file`), `frappe/core/doctype/file/file.py:658/663/801/833`.

**Third — exception swallowing.** Every failure (S3 timeout, missing object, credential error, *and* permission denial) is converted to `frappe.PageDoesNotExistError` (`:585-587`). For a browser this is a 404; for `csvutils.get_csv_content_from_attached_file` it becomes "Invalid CSV Format" (`csvutils.py:33-36`); for `email_queue` / PDF generation / Prepared Report it becomes an opaque 404 raised from a background worker with the real cause never logged. Diagnosability is destroyed.

**Fourth — permission check in a data path.** `is_downloadable()` → `has_permission(doc,"read")` is invoked on *every* `get_content()` (`:582`). Core `get_content()` has **no** permission check (it is a data accessor; the gate lives in the HTTP layer). Any server-side caller running as a user who cannot read the attached document — PDF rendering, email attachment assembly (`email_body.py:251`), data import, `unzip()` — now silently 404s where core would return bytes.

**Fifth — in-memory content is ignored.** Core returns `self.get("content")` first (`file.py:570-576`); DFP jumps straight to S3 whenever a key exists, so freshly assigned `.content` is discarded and stale remote bytes are returned.

### Every other overridden method vs frappe v15.93 core

| Method | DFP | Core | Drift verdict |
|---|---|---|---|
| `is_remote_file` (property) | `:315-317` | `:80-84` | signature identical; **semantic drift** — flips ~7 core branches (`:108`, `:205`, `:216`, `:227`, `:425`, `:631`, `:654`) into "remote" mode, disabling content-hash generation, private/public move handling, and local write |
| `validate_file_on_disk` | `:540-541` | `:358-366` | signature identical; returns `True` instead of core's `None`; skips `IOError` guard |
| `exists_on_disk` | `:543-544` | `:563-564` | signature identical; forced `False` **breaks core dedup** (`:410-412`, `:694-702`) |
| `optimize_file` | `:546-550` (`@frappe.whitelist()`) | `:786-787` (`@frappe.whitelist()`) | signature identical; raises `NotImplementedError` for remote — but note core also raises `NotImplementedError` for non-images, so callers see the same exception type |
| `__init__` | `:312-313` | inherited | none |
| `get_content` | `:577` | `:566` | **breaking** (above) |

Methods DFP leaves inherited but whose core implementations are wrong for offloaded files (silent, not signature drift): `get_full_path` (:594), `make_thumbnail` (:435), `_delete_file_on_disk` (:511), `delete_file_data_content` (:742), `unzip` (:528), `check_content` (:380), `save_file_on_filesystem` (:717).

---

## 9. CONFIG SURFACE

All configuration is **per connection document**, resolved through `@cached_property` accessors with hard-coded fallbacks:

| Knob | Field / code | Default | Fallback logic |
|---|---|---|---|
| Presign on/off | `presigned_urls` | 0 | `:596` |
| Presign mime allowlist | `presigned_mimetypes_starting` (newline list, prefix match) | `video/` | `:598-602`, mime guessed from **filename only** (`mimetypes.guess_type`, `:589-593`) — never from stored `content_type` or S3 |
| Presign expiry | `presigned_url_expiration` | 10800 s | `setting_presigned_url_expiration` `:112-115`, `<=0` → 3 h; int→`timedelta` coercion at `:267-268` |
| Stream chunk | `stream_buffer_size` | 10 MB | `:98-100` + validate floor 8192 (`:81-83`) |
| Redis cache size cap | `cache_files_smaller_than` | 5 MB | `:102-105`; applies to **public files only** (`:350-351`) |
| Redis cache TTL | `cache_expiration_secs` | 86400 s | `:107-110` |
| Remote size lookup | `remote_size_enabled` | 0 | `:353-363`, `stat_object` per request, exception → falls back to `file_size` |
| Write enable | `enabled` | 0 | blocks upload (`:384`) and delete (`:450`) but not reads |
| Folder routing | `folders` child table | — | `:329-341` (folder → parent → Home) |
| DocType opt-out | `doctypes_ignored` child table | JS seeds `Data Import`, `Prepared Report` (`dfp_external_storage.js:9-13`) | `:370-374` |
| Credentials | fields or env vars | — | `:132-148` |
| URL segment | `DFP_EXTERNAL_STORAGE_URL_SEGMENT_FOR_FILE_LOAD = "file"` | constant | `:24` — **not configurable** |
| Cache key prefix | `external_storage_public_file:` | constant | `:20` |

**Absent knobs:** no multipart part-size / concurrency (Minio defaults), no retry/backoff, no timeouts, no SSE/KMS, no storage class / lifecycle / Glacier settings, no ACL control, no per-file or per-doctype presign policy, no operation-mode switch (LOCAL_ONLY / DUAL_WRITE / …) — DFP is strictly S3-ONLY-per-folder with an accidental local fallback on error, no `site_config` surface at all.

---

## 10. ADOPT vs AVOID

### ADOPT
1. **`override_doctype_class` (real subclass) over monkeypatching** — `hooks.py:100-102`; keeps `super()` reachable and survives core upgrades better. But own it exclusively (`frappe/model/base_document.py:89-93`).
2. **Storage resolution chain with `Home` as global default** — `:319-344`; a clean, user-legible way to express "everything" vs "just this folder". Worth generalising to rule precedence in cloud_file_storage.
3. **Per-DocType opt-out list, pre-seeded with `Data Import` and `Prepared Report`** — `:370-374`, `dfp_external_storage.js:9-13`. These two DocTypes genuinely must stay local; hard-won knowledge.
4. **Seekable S3 proxy object** — `S3FileProxy` (`:33-64`) + `dfp_external_storage_file_proxy()` (`:483-499`) enables `zipfile`/partial reads without downloading 100 GB, and powers zero-RAM bucket→bucket copy (`:641-657`). Directly reusable.
5. **`werkzeug.wsgi.wrap_file` chunked streaming with configurable buffer** — `:517-520`.
6. **Presign gated by mime-type prefix** — `:595-603`: proxy small/sensitive files, redirect only heavy streaming media. Good hybrid; extend it with per-doctype/per-sensitivity rules.
7. **Redis response cache for small public objects with TTL** — `:708-711`, `:749-752`; cheap win, and correctly restricted to non-private (`:350-351`).
8. **Refcount guard before remote delete** — `:437-447`: exactly the shape cloud_file_storage needs, but promoted to a real `Cloud Storage Object.refcount` column with row locking.
9. **`remote_size_enabled`** — `:353-363`: acknowledges `File.file_size` is `INT(11)` and overflows past 2 GB. Design the object table with `bigint` from day one.
10. **Guard against deleting a connection that still has files** — `on_trash` `:93-96` + live `files_within` count (`:117-119`).
11. **Bucket reachability validated on save when connection fields change** — `:85-91`, `:121-123`, plus a loud warning when critical fields change on a bucket that already holds files (`:88-89`).
12. **Raw-bucket listing page for orphan detection** — `page/dfp_s3_bucket_list/*`; make it paginated and reconcile against the object table.
13. **Credential fallback to environment variables** — `:132-148`; useful for container/IRSA deployments (but keep decryption via `get_decrypted_password`, `:141`).

### AVOID
1. **`get_content(self)` with a frozen signature and no `**kwargs` passthrough** — `:577`. Must be `get_content(self, encodings=None, *args, **kwargs)` forwarding to core semantics, returning **raw bytes when `encodings=[]`** (India Compliance `gst_return_log.py:188`) and mirroring core's decode behavior otherwise (`frappe/core/doctype/file/file.py:583-590`).
2. **Swallowing all exceptions into `PageDoesNotExistError`** — `:585-587`. Destroys diagnosability for CSV import, email, PDF, prepared reports. Log the real error; raise typed exceptions; keep 404 masking strictly in the HTTP layer.
3. **Permission check inside the data accessor** — `:582`. `get_content()` must stay permission-free (core has none); enforce in the serve endpoint only, or background/system callers break.
4. **Deleting the local copy in the same breath as the PUT** — `:422`, and before the DB transaction commits. No `stat_object`/ETag/hash verification, no phase separation. This is the single most dangerous pattern for a 100 GB migration.
5. **Bulk Edit as the migration engine** — `README.md:55` → `frappe/desk/doctype/bulk_update/bulk_update.py:51-70`: 500-doc hard cap, `short` queue, `timeout=1000`, no resumability, no idempotency, no progress/state. Unusable at 1.2 M rows; build a dedicated batch-cursor engine on the `cloud_migration` queue.
6. **`exists_on_disk() → False` for remote files** — `:543-544`: silently disables core content-hash dedup (`file.py:410-412`, `:694-702`), guaranteeing duplicate objects. A `Cloud Storage Object` table must answer "does this content already exist remotely?" instead.
7. **Key scheme embedding `File.name` + original filename, flat under the site prefix** — `:399-400`: guarantees 1 object per File row (no dedup), leaks user filenames into the bucket, and produces one giant unshardable prefix. Prefer `<site>/<hash[0:2]>/<hash[2:4]>/<sha256>` with the display name only in `Content-Disposition`.
8. **Refusing to write object metadata** — `:415-416`: leaves zero reverse mapping object→File; orphan reconciliation becomes a full-bucket list. Store `content_hash`/object-id metadata.
9. **Rewriting *public* file URLs to `/file/<name>/<fname>`** — `:421`: every public asset now traverses Python + Redis instead of nginx. Public objects should keep a directly cacheable/CDN-able URL.
10. **`page_renderer` with a bare regex `can_render()`** — `:686-696`: intercepts any path containing `file/<x>/<y.ext>` ahead of `StaticPage` (`frappe/website/path_resolver.py:56-70`) and 404s it, whether or not the File exists. Use `website_route_rules`/an explicit prefix, and make `can_render()` verify the record.
11. **Cache hit served before the permission and filename checks** — `:708-721`: safe only because caching is public-only; one config slip makes it a private-file leak. Never key a response cache ahead of authorization.
12. **Presigned URLs for private objects with a 3-hour default** — `:595-603` + `presigned_url_expiration` default `10800`: a shareable, unrevocable capability URL. Use short TTLs (seconds–minutes), per-request issuance, and response-header pinning.
13. **Whole-file `read()` into memory** — `:501-515` (`download_to_local_and_remove_remote` at `:530-531` too): OOM on large objects; the `S3FileProxy` already exists, use it everywhere.
14. **Unbounded `frappe.get_all` for the refcount** — `:442-447` (no `limit=1`) on an unindexed Data field, plus check-then-act with no locking.
15. **Not overriding the delete/thumbnail/full-path family** — core `_delete_file_on_disk` (`file.py:511-526`) → `delete_file()` (`utils.py:153-170`) will `os.remove(site/private/files/<basename>)` for an offloaded file; `make_thumbnail` (`file.py:435-454`) silently no-ops. Override the complete surface, not just the read path.
16. **`AttributeError` landmines when the storage doc is missing** — `:452` (`self.dfp_external_storage_doc.title` after checking it may be falsy), `:749` (`dfp_is_cacheable()` outside the `try`), `:351` (same deref). Every `dfp_external_storage_doc` access needs a null guard.
17. **Zero tests and zero patches** — `test_dfp_external_storage.py:8-14` is a TODO stub; `patches.txt` is empty. For a production migration app, both are mandatory (the fork at `/home/user/v15/apps/cloud_file_storage` already does better: it ships `tests/test_minio_integration.py` and `patches/v0_2_0/backfill_s3_object_key.py`).
18. **Silent fallback to local storage on upload error for new files** — `:427-432`: the File looks fine, the mode contract is violated, and nothing surfaces except an Error Log row. In DUAL_WRITE/S3_PRIMARY modes this must be an explicit, queryable state on the object record.