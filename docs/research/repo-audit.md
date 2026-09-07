> **STATUS (P0.5): HISTORICAL EVIDENCE.** Captured 2026-08-14 during planning; verified
> file:line ground truth at capture time. Where later decisions supersede content here,
> `docs/PLAN.md` / `docs/adr/` win. Known supersessions: P0 is COMPLETE (the "bench is
> currently broken" state described in rename-and-bench-reality §0 was RESOLVED by the
> ops-only .pth repair — see PROGRESS.md); the CI matrix/floor is {v15.16.0, v15.93.0,
> version-15} with `frappe>=15.16.0,<16.0.0` (A34), not the v15.0.0-based examples.

I have everything needed. Writing the report.

# cloud_file_storage (fork of alyf-de/frappe-attachments-s3 @ `frappe_s3_attachment` v0.2.2) — Architecture Recon

**Repo state:** `HEAD = 51b41e6` (`chore(release): bump version to 0.2.2 and update changelog`). `HEAD == version-15 == origin/feat/enterprise-cloud-storage-v15 == upstream-alyf/version-15` — `git diff --stat version-15 HEAD` is empty, working tree clean. **There are zero fork-specific commits yet**; the only "fork" so far is the on-disk directory rename to `cloud_file_storage`. Origin is `https://github.com/RamachandranMD/cloud_file_storage.git`, upstream `alyf-de/frappe-attachments-s3`. Tags: `v0.1.0 … v0.2.2`.

**Operationally broken right now:** `/home/user/v15/env/lib/python3.10/site-packages/frappe_s3_attachment.pth` contains `/home/user/v15/apps/frappe_s3_attachment` — a path that no longer exists (dir is `cloud_file_storage`). `/home/user/v15/sites/apps.txt` line 15 and `/home/user/v15/sites/apps.json:321` still say `frappe_s3_attachment`.

---

## 1. INVENTORY

| Path (all under `/home/user/v15/apps/cloud_file_storage/`) | Purpose |
|---|---|
| `frappe_s3_attachment/__init__.py` | `__version__ = "0.2.2"` (1 line) |
| `frappe_s3_attachment/hooks.py` | App metadata + the only 3 live hook points (`after_install`, `after_migrate`, `doc_events["File"]`); 90% commented boilerplate |
| `frappe_s3_attachment/controller.py` | **Entire runtime**: `S3Operations` client/key/upload/delete/presign, `file_upload_to_s3`, `generate_file`, `delete_from_cloud`, `migrate_existing_files` + `run_migrate_existing_files`, `ping` |
| `frappe_s3_attachment/install.py` | `after_install`/`after_migrate`: seeds `Data Import` ignored row, creates `File.s3_object_key` Custom Field, adds prefix index |
| `frappe_s3_attachment/modules.txt` | Single line `Frappe S3 Attachment` (no trailing newline) |
| `frappe_s3_attachment/patches.txt` | 2 patch dotted paths |
| `frappe_s3_attachment/patches/v0_2_0/backfill_s3_object_key.py` | Copies legacy `content_hash` → `s3_object_key`, nulls `content_hash` |
| `frappe_s3_attachment/patches/v0_2_1/ensure_data_import_ignored_doctype.py` | Idempotently re-seeds the `Data Import` ignore row. **No `__init__.py` in `v0_2_1/`** (relies on PEP-420 namespace packages; `v0_2_0/` does have one) |
| `.../doctype/s3_file_attachment/s3_file_attachment.json` | Singleton settings schema (14 fields) |
| `.../s3_file_attachment.py` | `validate()` → credential pairing + HTTPS-only endpoint validation |
| `.../s3_file_attachment.js` | One client handler: the *Migrate Existing Files* button → `frappe.call` |
| `.../test_s3_file_attachment.py` | 5 unit tests of the two validators (calls unbound methods on `frappe._dict` fakes) |
| `.../test_s3_file_attachment.js` | Dead upstream QUnit stub (`{key: 'value'}`), never activated |
| `.../doctype/s3_ignored_doctype_row/s3_ignored_doctype_row.json` | Child table, one `Link → DocType` field `doctype_name`, `unique: 1` |
| `.../s3_ignored_doctype_row.py` | Empty `Document` subclass |
| `frappe_s3_attachment/config/desktop.py` / `docs.py` | Legacy v12-era desk module card + docs brand string (dead on v15) |
| `frappe_s3_attachment/templates/{,pages/}__init__.py` | Empty; **no www/ or portal pages exist** |
| `frappe_s3_attachment/locale/main.pot`, `de.po` | Babel catalogs; ~59/60 hits of the old app/doctype names |
| `frappe_s3_attachment/tests/test_controller.py` | 717 lines, 5 test classes — the bulk of coverage |
| `frappe_s3_attachment/tests/test_install.py` | 99 lines, pure-mock tests of install helpers (plain `unittest.TestCase`) |
| `frappe_s3_attachment/tests/test_minio_integration.py` | 178 lines, opt-in live MinIO round-trip (3 tests) |
| `.github/workflows/ci.yml` | 2 jobs: `tests` (mocked) + `tests-minio` |
| `.github/workflows/linter.yml` | commitlint / Semgrep(frappe-rules) / pip-audit / pre-commit |
| `.github/workflows/codeql.yml` | CodeQL python, weekly cron |
| `.github/dependabot.yml` | weekly pip + github-actions |
| `.pre-commit-config.yaml`, `commitlint.config.mjs`, `.editorconfig`, `tox.ini` | Tooling. `tox.ini` is 2 lines of **dead flake8 config** (`max-line-length = 90`, contradicts ruff's 110 in `pyproject.toml:29`); there is no tox env — tests run via `bench run-tests` |
| `pyproject.toml` | flit build, name `frappe_s3_attachment`, deps `filetype/boto3/botocore/s3transfer/six`, `frappe >=15,<17` |
| `README.md`, `CHANGELOG.md`, `license.txt` | Docs (fork-delta table lives in README) |

---

## 2. HOOKS — `frappe_s3_attachment/hooks.py` (127 lines, only 5 live statements)

```python
hooks.py:3    app_name = "frappe_s3_attachment"
hooks.py:4    app_title = "Frappe S3 Attachment"
hooks.py:56   after_install = "frappe_s3_attachment.install.after_install"
hooks.py:57   after_migrate = "frappe_s3_attachment.install.after_migrate"
hooks.py:89-94
doc_events = {
	"File": {
		"after_insert": "frappe_s3_attachment.controller.file_upload_to_s3",
		"on_trash":     "frappe_s3_attachment.controller.delete_from_cloud",
	}
}
```

**Hook points NOT used (all commented out or absent):**
- `override_doctype_class` — absent entirely (`hooks.py:122-127` only shows commented `override_whitelisted_methods`). Compare `dfp_external_storage/hooks.py:100` which *does* use it. **⇒ no `File` class override, no `get_content` override, no file renderer.**
- `write_file` / `read_file` / `delete_file` — Frappe's storage-abstraction hooks are **not** used anywhere in the repo (grep-verified). The app hijacks `after_insert` *after* core has already written to disk.
- `permission_query_conditions` / `has_permission` — commented `hooks.py:69-75`.
- `scheduler_events` — commented `hooks.py:99-115`; **no scheduled jobs at all**.
- `website_route_rules`, `boot_session`, `fixtures`, `app_include_js`, `doctype_js`, `doctype_list_js` (deliberately removed in `7b0e5a5`).
- `before_tests` — commented `hooks.py:120`.

**Whitelisted methods** (only 3, all in `controller.py`):
- `generate_file` — `controller.py:265-266`, no `allow_guest` ⇒ session required.
- `migrate_existing_files` — `controller.py:313-316`, gated by `frappe.only_for("System Manager")` at `:316`.
- `ping` — `controller.py:364-369`, returns `"pong"`, unauthenticated-session-only, dead debug surface.

---

## 3. WRITE PATH (upload → S3)

Trigger: `File.after_insert` → `file_upload_to_s3(doc, _method)` (`controller.py:215-262`). **Runs synchronously inside the HTTP request.**

1. `S3Operations()` (`controller.py:23-51`) loads the singleton via `frappe.get_doc("S3 File Attachment", ...)` **on every single upload** (no caching), builds a boto3 client with `signature_version="s3v4"`; if `endpoint_url` set → `addressing_style: "path"` (`:35-39`); creds from settings else boto3 default chain (`:43-46`).
2. Skip check: `is_ignored_doctype(parent_doctype)` (`controller.py:53-58`) against the `S3 Ignored DocType Row` child table. `parent_doctype = doc.attached_to_doctype or "File"` (`:231`).
3. Local path resolution (`:234-237`): `site_path + "/public" + doc.file_url` (public) or `site_path + doc.file_url` (private) — **string concatenation, not `get_full_path()`**.
4. **Key generation** — `key_generator(file_name, parent_doctype, parent_name)` (`controller.py:68-129`):
   - Optional `s3_key_generator` hook, first dotted path only (`:95-97`), result `frappe.cstr(...).strip("/")` (`:103`); errors log with `exc_info` and fall back (`:110-114`); empty/slash-only logs warning and falls back (`:106-109`).
   - **Default layout (`:116-128`):** `{folder_name}/{YYYY}/{MM}/{DD}/{parent_doctype}/{8-char A-Z0-9 random}_{sanitised_file_name}`. Randomness is `random.choice` (`:118`) — **not `uuid`, not cryptographic, not content-derived**. `strip_special_chars` (`:60-66`) strips everything outside `[0-9a-zA-Z._-]`, so non-ASCII filenames collapse (`Pflanzenrückgabe.pdf` → `Pflanzenrckgabe.pdf`); the *doctype segment keeps raw spaces* (`Sales Invoice/`, asserted in `test_controller.py:80`).
   - **Identity is filename+date+random. There is no content hash anywhere in the key.**
5. **Upload** — `upload_files_to_s3_with_key` (`controller.py:131-171`): MIME via `filetype.guess(file_path)` → `mimetypes.guess_type(file_name)` → `application/octet-stream` (`:137-139`). Private: `ContentType` + `Metadata{ContentType, file_name=<NFKD-ASCII-folded>}` (`:142-154`). Public: same **plus `ACL: "public-read"`** (`:155-167`). No SSE, no KMS, no `StorageClass`, no `ChecksumAlgorithm`, no tags, no multipart tuning. Only `S3UploadFailedError` is caught → `frappe.throw` (`:169-170`); any `ClientError`/network error propagates and aborts the File insert.
6. **DB write** — raw SQL bypassing hooks (`controller.py:249-250`):
   ```sql
   UPDATE `tabFile` SET file_url=%s, s3_object_key=%s, content_hash=NULL WHERE name=%s
   ```
   - `file_url` private → `/api/method/frappe_s3_attachment.controller.generate_file?key={key}&file_name={doc.file_name}` (`:243-244`) — **key and filename are un-URL-encoded, injected raw into a query string**.
   - `file_url` public → `f"{s3_upload.S3_CLIENT.meta.endpoint_url}/{s3_upload.BUCKET}/{key}"` (`:246`) — direct bucket URL, **no CDN/custom-domain support**.
   - **`content_hash` is deliberately NULLed** (`:249`, `:254`) to defeat core dedup (issue #12 workaround; `README.md:39`, `CHANGELOG.md:60`). Consequence: Frappe's own dedup is off for all S3 rows and no content identity survives.
   - `folder`/`old_parent` deliberately *not* touched (fork delta `3e4f4dc`, `README.md:44`).
7. **Parent image_field side effect** (`controller.py:256-259`): if the parent DocType has `image_field` in Meta, it is unconditionally overwritten with the new `file_url` — **any** attachment to such a doctype clobbers its image.
8. `frappe.db.commit()` (`controller.py:261`) — **commits the caller's whole transaction from inside `after_insert`**.
9. `os.remove(file_path)` (`controller.py:262`) — **local file deleted immediately, with no HEAD/verify of the remote object**, and outside any try/except.

Custom fields written: only **`File.s3_object_key`** (Data(255), read_only, no_copy, print_hide, `insert_after: content_hash`, module `Frappe S3 Attachment`) created by `install.py:33-62`, indexed by `install.py:65-79` (`s3_object_key(191)` prefix on MariaDB, plain on Postgres).

---

## 4. READ / SERVE PATH

- **Public files:** no app code involved at all. `file_url` is the raw bucket URL and the object carries `ACL: public-read` (`controller.py:162`) ⇒ **permanently world-readable to anyone with the URL, forever**. No signing, no expiry, no permission check possible.
- **Private files:** `generate_file(key, file_name)` (`controller.py:265-284`):
  - `controller.py:271-272` — empty key → sets `response["body"] = "Key not found."` and returns (odd 200 response, not an error).
  - `controller.py:274-276` — `frappe.db.get_value("File", {"s3_object_key": key}, "name")`; missing → `frappe.DoesNotExistError`.
  - `controller.py:278` — `frappe.get_doc("File", name).check_permission("read")` ← the only authorization gate (fork addition, commit `06435bd`).
  - `controller.py:280-284` — `get_url()` presigns `get_object` and issues `response["type"]="redirect"`, `response["location"]=signed_url`.
  - `get_url` (`controller.py:187-212`): expiry = `signed_url_expiry_time` else **hard-coded 120 s** (`:194-197`) — note the schema default is **300** (`s3_file_attachment.json:63`), so the code fallback and the schema disagree. Adds RFC-5987 `ResponseContentDisposition: attachment; filename*=UTF-8''<quoted>` when `file_name` given (`:202-204`) ⇒ **every private download is forced as an attachment; inline preview/embedding of private images/PDFs is impossible**.
- **No streaming/proxy path.** `read_file_from_s3` (`controller.py:181-185`) exists but **is never called** by any code in the repo (grep-verified) — dead.
- **`File.get_content()` is broken for every S3-backed row.** No override exists (§2). Core `frappe/core/doctype/file/file.py:566-591` calls `self.get_full_path()`, which at `file.py:615` passes URLs through untouched (`URL_PREFIXES = ("http://", "https://", "/api/method/")`, `file.py:40`), then does `open(file_path, "rb")` ⇒ **`FileNotFoundError`** for both the public `https://…` and the private `/api/method/…` shapes. This is exactly the India-Compliance blast radius: `india_compliance/gst_india/doctype/gst_return_log/gst_return_log.py:55,98` call `file.get_content()` and `:188` calls `file.get_content(encodings=[])` — and installed core `frappe 15.93.0` defines `def get_content(self) -> bytes:` with **no `encodings` parameter** (`file.py:566`), the same signature DFP shipped in its override (`dfp_external_storage.py:577`). Any override the new app writes must accept `encodings=...` **and** return raw bytes.

---

## 5. DELETE PATH

`File.on_trash` → `delete_from_cloud(doc, method)` (`controller.py:356-361`): no-op if `s3_object_key` empty (`:358-359`), else `S3Operations().delete_from_s3(key)`.
`delete_from_s3` (`controller.py:173-179`) is **gated on the `delete_file_from_cloud` checkbox, default `0`** (`s3_file_attachment.json:33-38`). `ClientError` → `frappe.throw(_("Access denied: Could not delete file"))` (`:178-179`) — **a bucket permission problem hard-blocks the File deletion**; `NoSuchKey` is not distinguished.

**Shared-object hazards (all present today):**
- **No refcount, no object table.** Deletion is 1 File row → unconditional `delete_object` on its key.
- If a site's `s3_key_generator` hook returns a deterministic key (the documented use case, `README.md:116-129`), N File rows share one object; trashing any one row deletes the bytes out from under the other N-1, which keep pointing at a 404 URL.
- The same sharing also breaks authorization: `generate_file` resolves the key with `frappe.db.get_value("File", {"s3_object_key": key}, "name")` (`controller.py:274`) which returns **one arbitrary row**. With a shared key, a user permitted on *any* one of those File rows gets a presigned URL for the shared object — the permission check may run against a different document than the one that owns the request.
- With `delete_file_from_cloud = 0` (default) every deleted File **orphans its S3 object permanently** — no GC, no lifecycle, no reconciliation job.
- No `File.on_update` / `is_private` handler: core's `handle_is_private_changed` early-returns for remote files (`frappe/core/doctype/file/file.py:227`), so flipping public↔private **never re-ACLs the object** — a file made "private" in Desk keeps its `public-read` ACL.

---

## 6. MIGRATION TOOLING

**Entry** `migrate_existing_files()` (`controller.py:313-353`, whitelisted, `frappe.only_for("System Manager")` at `:316`), invoked from the settings button (`s3_file_attachment.js:8-14`).

- **Queue:** `queue="long"` (`controller.py:324`) — **shared with every other long job on the bench**; no dedicated `cloud_migration` queue anywhere.
- **Job identity:** `job_id = "frappe_s3_attachment.migrate_existing_files"` (`controller.py:19`), `deduplicate=True` (`:327`), namespaced via `create_job_id` → `"{site}::{job_id}"` (`frappe/utils/background_jobs.py:559-565`). `enqueue` returning `None` ⇒ already queued/running (`controller.py:341-351`). Returns `{"job_id": str, "queued": bool}`.
- **Timeout:** `S3 File Attachment.timeout_for_migration_job` → `cint(...) or 1500` (`controller.py:319-320`); schema default `1500` (`s3_file_attachment.json:120-125`). Coincidentally identical to frappe's `long` queue default (`background_jobs.py:52`).
- **Worker** `run_migrate_existing_files()` (`controller.py:294-310`):
  - **Enumeration:** one unbounded `frappe.get_all("File", fields=["name","file_url"], filters=[["file_url","is","set"],["s3_object_key","is","not set"]])` (`:296-303`) — **no `limit`, no `order_by`, no keyset paging**. At 1.2 M rows this materialises the entire result set in the worker's memory in a single query.
  - **Filtering:** `_s3_file_regex_match` (`controller.py:287-291`) skips `http:`/`https:` and the app's own `generate_file` path (`:305-307`); then `frappe.get_doc("File", name)` per row (`:308`) — **one full doc load per file**, 1.2 M docs; then `doc.exists_on_disk()` (`:309`).
  - **Work:** calls the *same* `file_upload_to_s3(doc, "migrate_existing_files")` (`:310`), so every migrated file gets its own `frappe.db.commit()` (`:261`) and its own settings-doc load + boto3 client construction (`:228`) — **1.2 M singleton reads and 1.2 M boto3 clients**.
- **Batching:** none. **Phases:** none — one monolithic loop; no separate UPLOAD / VERIFY / CLEANUP.
- **Verification:** none. No `head_object`, no size/ETag/checksum compare. `os.remove(file_path)` (`:262`) fires immediately after the commit — **local bytes are destroyed with zero independent remote verification**, exactly the anti-pattern the mission forbids.
- **Resumability:** only implicit — the `s3_object_key is not set` filter (added in `c8c7806`) means a re-run skips already-migrated rows. There is **no checkpoint/cursor doctype, no run record, no progress counter, no per-file state machine, no phase marker**.
- **Failure handling:** none. The loop has **no `try/except`** — one bad file (`ClientError`, `EndpointConnectionError`, `FileNotFoundError` from `os.remove`, `frappe.throw` from `upload_files_to_s3_with_key`) aborts the entire job. Nothing is recorded; the operator sees only an RQ failure. On timeout the job is killed mid-loop and the only user-facing guidance is "re-run it" (`s3_file_attachment.json:121`, `README.md:111`).
- **Observability:** a single `frappe.msgprint` with a link to the `RQ Job` form (`controller.py:330-337`). No progress %, no ETA, no per-file log, no error log doctype, no dashboard, no `frappe.publish_progress`.
- **No CLI/bench command**, no `frappe.get_all` streaming, no throttle/rate-limit, no bandwidth cap, no concurrency.

---

## 7. SETTINGS

**`S3 File Attachment`** — singleton (`issingle: 1`, `s3_file_attachment.json:128`), module `Frappe S3 Attachment` (`:132`), perms: **System Manager only** (`:135-146`), `track_changes: 1` (`:152`), `quick_entry: 1` (`:147`). `field_order` at `:7-25`.

| Field | Type | Default | Notes (line) |
|---|---|---|---|
| `general_configuration` | Section Break | — | `:28-31` |
| `delete_file_from_cloud` | Check | `0` | Whether trashing a File deletes the S3 object (`:32-38`); label has a trailing space `"Delete file from cloud "` |
| `column_break_bngr` | Column Break | — | `:81-84` |
| `signed_url_expiry_time` | Int (non_negative) | `300` | seconds; code falls back to **120** when unset (`controller.py:197`) (`:62-69`) |
| `credentials_section` | Section Break (bold) | — | `:101-106` |
| `bucket_name` | Data | — | **reqd: 1** (`:39-45`) |
| `access_key` | Data | — | plaintext (`:85-89`) |
| `secret_key` | **Password** | — | read via `get_password(..., raise_exception=False)` (`:96-100`) |
| `section_break_7` | *Column* Break (misnamed) | — | `:46-49` |
| `folder_name` | Data | — | key prefix inside bucket (`:57-61`) |
| `region_name` | Data | — | **reqd: 1** (`:50-56`) |
| `endpoint_url` | Data | — | label typo **"S3 Endoint URL"**; HTTPS-only validation (`:90-95`) |
| `ignored_doctypes_section` | Section Break (bold) | — | `:107-112` |
| `ignored_doctypes` | Table → `S3 Ignored DocType Row` | — | `:113-118` |
| `section_break_10` | Section Break "Migration" (bold) | — | `:70-75` |
| `timeout_for_migration_job` | Int (non_negative) | `1500` | seconds (`:119-126`) |
| `migrate_existing_files` | Button | — | `:76-80` |

Validation (`s3_file_attachment.py:12-44`): `validate_credentials` — access_key and secret_key must both be set or both empty (`:16-24`); `validate_endpoint_url` — must be `https` with netloc (`:26-37`) and must carry no params/query/fragment (`:39-44`). **Not validated:** bucket reachability, credential correctness, region/endpoint consistency, folder_name shape. **No "test connection" button.**

**`S3 Ignored DocType Row`** (`s3_ignored_doctype_row.json`) — `istable: 1` (`:24`), `allow_rename: 1` (`:3`), module `Frappe S3 Attachment` (`:28`), **no permissions block** (`:31`). Single field `doctype_name`: `Link → DocType`, `reqd: 1`, **`unique: 1`** (`:12-20`) — note `unique` on a child-table field is enforced globally, not per-parent.

**Absent from settings entirely:** operation mode, dual-write toggle, fallback policy, storage class / lifecycle / Glacier, SSE/KMS, CDN base URL, per-doctype routing, multi-bucket/multi-tenant, retry/backoff, migration batch size, dedup toggle, verification policy, backup targets.

---

## 8. PATCHES (`patches.txt` — both post-model-sync, no `[pre_model_sync]`/`[post_model_sync]` section markers at all)

1. **`frappe_s3_attachment.patches.v0_2_0.backfill_s3_object_key`** (`backfill_s3_object_key.py:22-52`) — calls `ensure_s3_object_key_custom_field()` + `ensure_s3_object_key_index()` (`:23-24`), then a raw SQL `SELECT name, content_hash FROM tabFile WHERE content_hash IS NOT NULL AND content_hash != '' AND (s3_object_key IS NULL OR s3_object_key = '') AND (file_url LIKE 'https://%' OR file_url LIKE '/api/method/frappe_s3_attachment.controller.generate_file%')` (`:26-38`) and per-row `frappe.db.set_value(... s3_object_key=content_hash, content_hash=None, update_modified=False)` (`:41-49`), one `commit` at the end (`:51-52`). **Unbounded / non-batched / non-resumable**, and the `file_url LIKE 'https://%'` predicate will sweep in *any* externally-hosted File row that happens to have a `content_hash`. The dotted path `frappe_s3_attachment.controller.generate_file` is **baked into the SQL literal** — a rename must ship a follow-up patch or this predicate silently stops matching.
2. **`frappe_s3_attachment.patches.v0_2_1.ensure_data_import_ignored_doctype`** (`ensure_data_import_ignored_doctype.py:13-14`) — delegates to `install.ensure_default_ignored_doctype()` (`install.py:22-30`), which appends `{"doctype_name": "Data Import"}` with `flags.ignore_mandatory = True` and `save(ignore_permissions=True)`. Idempotent. Package dir has **no `__init__.py`**.

---

## 9. TESTS

**`tests/test_controller.py` (717 lines, 5 classes)** — everything is `unittest.mock`-based against a `frappe._dict` fake settings object (`:18-39`); `boto3.client` and `frappe.get_doc` are patched wholesale (`:48-66`).
- `TestControllerCharacterization` (`:42`): `strip_special_chars`; key layout with/without folder and with spaces (`:75-98`); the full `s3_key_generator` hook matrix — happy path, bytes return, exception+`exc_info` fallback, slash-only fallback, empty fallback (`:100-178`); `is_ignored_doctype` (`:180-193`); migration — empty result, not-on-disk skip, http/https skip + exact `get_all` filter assertion (`:195-239`); enqueue matrix — default 1500, settings override 3600, falsy→1500, already-queued→`queued: False` (`:241-278`); upload ACL public vs private (`:280-313`); MIME via `filetype` and via filename fallback (`:315-345`); `delete_from_s3` flag on/off (`:347-357`); ignored-doctype skip in `file_upload_to_s3` (`:359-379`); `image_field` write + SQL shape (`:381-415`); `delete_from_cloud` with/without key (`:417-429`); `generate_file` filters on `s3_object_key` (`:431-448`); `_s3_file_regex_match` (`:450-458`).
- `TestEndpointUrl` (`:461`): endpoint threading + `addressing_style: path` present/absent (`:480-496`).
- `TestNonAsciiFilenames` (`:499`): ASCII-folded metadata `file_name` (`:524-540`); RFC-5987 content-disposition round-trip (`:542-554`).
- `TestGenerateFilePermissions` (`:560`): **the only real-DB tests** — deny for unauthorized user (`:594-609`), redirect for owner (`:611-630`), `DoesNotExistError` for unknown key (`:632-636`). Requires a real File insert with the upload hook patched out (`:576-578`).
- `TestPrivateDuplicateContentHashRegression` (`:639`): issue-#12 marker — two identical-content private uploads must not crash (`:677-717`). Its `tearDown` **bypasses `on_trash` with raw `frappe.db.delete`** because the doc_event resolves through `frappe.get_attr` and escapes `mock.patch` (`:663-675`) — an explicit acknowledgement that doc_event hooks are untestable via mocks.

**`tests/test_install.py` (99 lines)** — plain `unittest.TestCase` (not `FrappeTestCase`), 100% mocked: ignored-doctype append/no-op (`:17-34`), custom-field payload assertions (`:38-53`), MariaDB prefix vs Postgres plain index (`:57-79`), `after_install`/`after_migrate` chaining (`:83-99`).

**`tests/test_minio_integration.py` (178 lines)** — opt-in via `RUN_MINIO_INTEGRATION_TESTS=1`, else `SkipTest` (`:23-24`). Config from `FRAPPE_S3_ATTACHMENT_MINIO_{ENDPOINT,ACCESS_KEY,SECRET_KEY,BUCKET,REGION}` env vars (`:26-30`), client built with `s3v4` + path addressing (`:31-38`), connectivity probe → `RuntimeError` if unreachable (`:39-46`), bucket auto-created (`:79-83`). Settings are a `frappe._dict` injected by patching `frappe.get_doc` selectively (`:107-110`) — the real singleton is never saved (README explains the HTTPS-validation conflict, `README.md:70`). `tearDown` force-deletes File rows and objects because `file_upload_to_s3` commits mid-test and escapes `FrappeTestCase` rollback (`:61-77`). Three tests: public upload → object exists + local file gone + `content_hash` falsy (`:129-144`); private upload → `generate_file` redirect containing `X-Amz-Expires=300` (`:146-164`); delete-from-cloud removes the object (`:166-178`).

**MinIO provisioning:** `docker run quay.io/minio/minio:latest server /data --address ":9000"` on port 9000 with `minioadmin/minioadmin`, health-polled 30× on `/minio/health/live` (`ci.yml:166-182`); bucket `frappe-s3-attachment-test` created by an inline python heredoc (`ci.yml:190-204`). Local instructions duplicated in `README.md:76-96`.

**How tests run:** `bench --site test_site set-config allow_tests true && bench --site test_site run-tests --app frappe_s3_attachment` (`ci.yml:118-120`, `ci.yml:219-221`). **`tox.ini` is vestigial** (2 lines of flake8 config, no `[tox]` section, no env) — nothing invokes tox.

**Coverage gaps:** zero tests for `install.after_*` against a real DB, zero for the patches, zero for `read_file_from_s3`, zero for concurrency/idempotency, zero for `get_content`/India-Compliance compatibility, zero for large-scale migration, zero JS tests (the QUnit file is the dead upstream stub), no coverage measurement/threshold in CI.

---

## 10. CI / TOOLING

- **`ci.yml`** — triggers: `workflow_dispatch`, push to `develop`/`version-15`, PRs (skips `**.md/.html/.csv/.po/.pot` + linter.yml) (`:3-22`); concurrency group `develop-frappe_s3_attachment-…` (`:24-26`).
  - Job **`tests`** (`:29-122`): services redis-cache `13000:6379`, redis-queue `11000:6379`, mariadb `11.8` with health-cmd (`:35-50`); **Python 3.14 / Node 24** (`:61-69`) — note the app's own `requires-python = ">=3.10"` and the bench env here is py3.10; `bench init --skip-redis-config-generation --skip-assets`, utf8mb4 globals set via SQL (`:98-103`); `bench get-app --resolve-deps frappe_s3_attachment $GITHUB_WORKSPACE` (`:108`) — **hard-coded app name**; `bench new-site … test_site`, `install-app`, `bench build`, then `run-tests` (`:105-123`). **No frappe branch pin** — `bench init` takes whatever `develop`/latest resolves to.
  - Job **`tests-minio`** (`:124-229`): same stack + MinIO container, runs the *same* full suite with `RUN_MINIO_INTEGRATION_TESTS: "1"` and the five `FRAPPE_S3_ATTACHMENT_MINIO_*` env vars (`:222-229`).
  - `Find tests` step is a `grep -rn "def test"` smoke check (`:56-59`).
- **`linter.yml`** — `commit-lint` (commitlint from PR base→head SHA, `:26-44`), `linter` (clones `frappe/semgrep-rules`, runs `semgrep ci --config ./frappe-semgrep-rules/rules --config r/python.lang.correctness`, `:46-64`), `deps-vulnerable-check` (`pip-audit --desc on .`, `:66-90`), `precommit` (`pre-commit/action@v3.0.1`, `:92-102`). `permissions: contents: read` (`:17-18`).
- **`codeql.yml`** — python only, push/PR on `develop`+`version-15`, weekly `0 3 * * 1` (`:1-44`).
- **`dependabot.yml`** — weekly pip (`chore(deps)`) and github-actions (`chore(ci)`).
- **`.pre-commit-config.yaml`** — `pre-commit-hooks v6.0.0` (trailing-whitespace scoped to `files: "frappe_s3_attachment.*"` `:11`, check-yaml/json/toml/ast/merge-conflict/debug-statements), `ruff-pre-commit v0.14.13` (`ruff --fix` + `ruff-format`), `commitlint-pre-commit-hook v9.24.0` on `commit-msg`. `default_install_hook_types: [pre-commit, commit-msg]`.
- **`commitlint.config.mjs`** — `@commitlint/config-conventional`.
- **Ruff** config in `pyproject.toml:27-59`: line-length 110, target py310, select `F,E,W,I,UP,B`, tab indent, double quotes.
- **Missing:** no release automation on this branch (`3f99db7 feat(ci): add semantic-release strategy for branch channels` exists only on `upstream-alyf/ci-semantic-release`), no coverage gate, no matrix over frappe versions, no ERPNext/India-Compliance integration job.

---

## 11. DOCS

- **`README.md` (136 lines)** — the richest artifact. Feature list (`:8-15`) documents the key layout `{folder}/{year}/{month}/{day}/{doctype}/{random}_{filename}` (`:14`). Install instructions still point at `alyf-de/frappe-attachments-s3 --branch version-15` and `bench install-app frappe_s3_attachment` (`:19-20`). Branch policy `develop`/`version-15`/`version-16` (`:24-28`). **"Changes vs upstream" table (`:34-47`)** — the authoritative fork-delta doc: endpoint URL + path-style, ASCII/RFC-5987 filenames, `filetype` over `python-magic`, `s3_object_key` over `content_hash`, permission-checked `generate_file`, ignored-doctype child table, un-whitelisted upload hook, background migration on `long`, preserved folder tree, Password secret, CI stack, hardened `key_generator`. Git anchors for rebases: `2274ea1` (CI baseline) and `b595155` (format pass) (`:49-56`). **"Known limitations" (`:60-66`)** explicitly states duplicate handling is **out of scope** and would require core File changes. MinIO test instructions incl. the HTTPS-validation caveat (`:68-98`). Hetzner setup incl. the warning that public files depend on per-object ACLs (`:100-105`). Desk config walkthrough (`:107-112`). Full `s3_key_generator` hook contract with example code (`:114-132`).
- **`CHANGELOG.md` (113 lines)** — Keep-a-Changelog, `[Unreleased]` empty; 0.2.2 (migration timeout field, long-queue enqueue, `{"job_id","queued"}` return shape, System-Manager restriction, `:10-26`), 0.2.1 (Data Import patch, hook normalisation, `doctype_list_js` removal, `:28-47`), 0.2.0 (`s3_object_key` field + index + backfill, `filetype`, content_hash clearing, `:49-65`), 0.1.1 (MinIO, `:67-76`), 0.1.0 (fork baseline, `:78-107`). All release links point at `alyf-de/frappe-attachments-s3`.
- **`license.txt`** — MIT, dual copyright zerodha (2018) + ALYF GmbH (2026), attribution to upstream.
- **`.github/issue_template.md`** — 4-heading stub.
- **Absent:** no `docs/`, no ADRs, no runbook, no upgrade/migration guide, no security model doc, no architecture diagram. `config/docs.py` is dead v12 scaffolding.

---

## 12. GAP LIST (mission requirement → evidence of absence)

**Object model / identity**
- **No "Cloud Storage Object" abstraction.** Only two doctypes exist (`s3_file_attachment`, `s3_ignored_doctype_row`); the entire object reference is one Data column `File.s3_object_key` (`install.py:33-62`). No object table, no size/etag/storage-class/bucket columns, no per-object state.
- **No content-hash dedup — dedup is actively destroyed.** The upload path NULLs `content_hash` on purpose (`controller.py:249`, `:254`), and README declares dedup out of scope (`README.md:64`). Every re-upload of identical bytes creates a new object.
- **Identity is filename+date+8-char `random.choice`** (`controller.py:116-128`) — non-deterministic, non-content-derived, PII-leaking (original filenames and parent doctype names are in the key path), and `strip_special_chars` (`:60-66`) mangles all non-ASCII names so two different files can collide on the sanitised tail (only the 8-char random prefix separates them).
- **No refcount, no safe delete.** `delete_from_cloud` (`controller.py:356-361`) deletes the object for the single File row being trashed, with no check for other referrers (see §5 for the shared-key hazards).
- **Orphan objects by default** — `delete_file_from_cloud` defaults to `0` (`s3_file_attachment.json:33`) and there is no reconciliation/GC job (`hooks.py:99-115` scheduler_events commented out).

**Operation modes**
- **No modes at all.** No LOCAL_ONLY / DUAL_WRITE / S3_PRIMARY_LOCAL_FALLBACK / S3_ONLY concept anywhere. Behaviour is hard-wired "upload then `os.remove` the local copy" (`controller.py:262`) = permanent S3_ONLY, with the only escape hatch being the coarse per-parent-doctype ignore list (`controller.py:53-58`).
- **No local fallback on S3 read failure.** Private reads redirect to a presigned URL (`controller.py:282-283`); if S3 is down the user gets an S3 error page. No dual-write, no read-through cache, no circuit breaker.
- **No per-doctype/per-file routing** beyond the on/off ignore table.

**Serving / security**
- **Public files are unprotected forever** via `ACL: public-read` (`controller.py:162`), which the README even flags as requiring bucket ACL support (`README.md:105`). No signed-URL option for public, no CDN, no cache headers.
- **Presigned URL lifetime is a single global int**, defaulting to 300 in schema / 120 in code (`s3_file_attachment.json:63` vs `controller.py:194-197`). No per-doctype or per-sensitivity policy, no one-time tokens, no audit trail of who fetched what.
- **Private downloads are forced as attachments** (`controller.py:202-204`) — inline rendering of private images/PDFs in Desk is impossible.
- **`is_private` toggles are ignored** — core early-returns for remote files (`frappe/core/doctype/file/file.py:227`) and the app registers no `on_update` handler, so ACLs never converge.
- **`ping()` is a live whitelisted endpoint** (`controller.py:364-369`) with no purpose.
- **No SSE/KMS/bucket-policy enforcement, no object versioning, no legal hold** — `upload_file` ExtraArgs carry only ContentType/ACL/Metadata (`controller.py:143-167`).

**`File.get_content` compatibility (mission-critical)**
- **`File.get_content()` raises `FileNotFoundError` on every S3-backed row today.** No `override_doctype_class` (`hooks.py` has none), so core `file.py:566-591` + `:594-621` `open()`s the URL string. India Compliance calls it at `gst_return_log.py:55,98` and calls **`get_content(encodings=[])`** at `:188`, which core `frappe 15.93.0` (`file.py:566`) does not even accept — the exact DFP failure shape (`dfp_external_storage.py:577` defines `get_content(self)`).
- **No raw-bytes guarantee anywhere** — core `get_content` `.decode()`s text (`file.py:583-588`); any new override must preserve bytes for GST JSON/ZIP payloads.
- **No renderer / no `File.get_full_path` compatibility layer**, so anything in the ecosystem doing `exists_on_disk()`, `get_full_path()`, `validate_file_on_disk()`, or `frappe.utils.file_manager` reads against an S3-backed row silently mis-behaves.

**Migration engine**
- Enumerates **all 1.2 M rows in one unbounded `frappe.get_all`** with no ordering or paging (`controller.py:296-303`).
- Runs as **one RQ job on the shared `long` queue** (`controller.py:324`) with a 1500 s default (`:319-320`) — for ~100 GB this cannot complete; the documented remedy is "re-run it" (`s3_file_attachment.json:121`).
- **No dedicated `cloud_migration` queue**, no worker config, no `workers` entry documented.
- **No batching, no phases** — UPLOAD/VERIFY/CLEANUP are fused into one call (`controller.py:310` → `:238-262`).
- **No verification before local delete** — `os.remove` at `controller.py:262` with no `head_object`/checksum.
- **No per-file error isolation** — no try/except in the loop (`controller.py:304-310`); one failure kills the run.
- **No progress/state model** — no run doctype, no counters, no `publish_progress`, no failure ledger; resumability is only the `s3_object_key is not set` filter (`:301`).
- **No idempotency key per file**, no dry-run, no throttling, no bandwidth/parallelism control, no bench CLI entry point.
- **Per-file `frappe.db.commit()` inside `after_insert`** (`controller.py:261`) — during normal uploads this commits the caller's transaction, a correctness hazard well beyond migration.

**Backup / lifecycle / economics**
- **Nothing.** No storage-class selection, no lifecycle-rule management, no Glacier/Deep-Archive transition, no restore workflow, no cost model, no retention policy, no backup-to-S3 integration, no `scheduler_events` at all (`hooks.py:99-115`).

**Observability / operations**
- **No dashboards, workspace, number cards, or reports** — the app ships no workspace JSON, no `www/` pages (`templates/pages/` is empty).
- **No health/connection test** in settings (`s3_file_attachment.py:12-44` validates only string shapes).
- **No structured logging or Error Log integration** — two `frappe.logger("frappe_s3_attachment")` calls in `key_generator` (`controller.py:107,111`) are the only logging in the app.
- **No metrics** (upload count, bytes, latency, failure rate), no alerting.

**Correctness / robustness**
- Client + singleton re-instantiated per file (`controller.py:23-51`, called at `:228` and `:360`) — no connection pooling, no `lru_cache`.
- Private `file_url` interpolates key and filename into a query string **without URL-encoding** (`controller.py:243-244`) — keys containing `&`/`#`/spaces (achievable via a hook, or via the raw-space doctype segment like `Sales Invoice`) produce a broken/ambiguous URL and a failing lookup.
- `image_field` on the parent is unconditionally overwritten by any attachment (`controller.py:256-259`).
- Local path built by string concat rather than `get_full_path()` (`controller.py:234-237`), diverging from core's path semantics.
- Only `S3UploadFailedError` is handled (`controller.py:169`); all other boto exceptions escape and abort the File insert.
- `delete_from_s3` turns a bucket-permission `ClientError` into `frappe.throw`, **blocking the document deletion** (`controller.py:178-179`).
- `patches/v0_2_1/` has no `__init__.py` (namespace-package reliance).
- `tox.ini` flake8 `max-line-length = 90` contradicts ruff's 110 (`pyproject.toml:29`) — dead config.
- No multi-tenant/multi-bucket support; one singleton, one bucket, one credential pair.

---

## 13. RENAME SURFACE (`frappe_s3_attachment` → `cloud_file_storage`; "S3 File Attachment" / "Frappe S3 Attachment" / "S3 Ignored DocType Row" → new names)

**A. Filesystem / package layout**
- App root already renamed to `/home/user/v15/apps/cloud_file_storage` — but the **inner python package is still `frappe_s3_attachment/`**, and the module dir is the doubly-nested `frappe_s3_attachment/frappe_s3_attachment/` (which must become `cloud_file_storage/cloud_file_storage/`).
- DocType dirs: `.../doctype/s3_file_attachment/` (5 files, all prefixed `s3_file_attachment.*`), `.../doctype/s3_ignored_doctype_row/` (3 files).
- Patch dirs `patches/v0_2_0/`, `patches/v0_2_1/`.

**B. Packaging / env / bench artifacts (currently broken)**
- `pyproject.toml:2` — `name = "frappe_s3_attachment"` (plus `description`, `authors`, `license`, `[tool.bench.frappe-dependencies]`).
- `/home/user/v15/env/lib/python3.10/site-packages/frappe_s3_attachment.pth` → contains `/home/user/v15/apps/frappe_s3_attachment` (**stale, path does not exist**); `.../frappe_s3_attachment-0.2.2.dist-info/` must be reinstalled.
- `/home/user/v15/sites/apps.txt` line 15 = `frappe_s3_attachment`.
- `/home/user/v15/sites/apps.json:321` = `"frappe_s3_attachment": {…, "branch": "version-15", "commit_hash": "51b41e6…"}`.
- Per-site DB: `tabInstalled Application.app_name`, `tabModule Def` (`Frappe S3 Attachment`), `tabDocType.module` for both doctypes, `tabPatch Log` rows for the two dotted patch paths, `tabCustom Field` `File-s3_object_key` (its `module` = `Frappe S3 Attachment`, `install.py:58`), the `s3_object_key_index` index name (`install.py:5`), the `tabSingles` rows keyed on doctype `S3 File Attachment`, and `tabRQ Job` / redis job ids namespaced `{site}::frappe_s3_attachment.migrate_existing_files`.

**C. Dotted paths in code / config (must be rewritten atomically with a migration patch)**
- `hooks.py:3` `app_name`, `:4` `app_title`, `:5-9` publisher/email/description, `:56` `after_install`, `:57` `after_migrate`, `:91` `after_insert` handler, `:92` `on_trash` handler (+ commented boilerplate at `:16,17,20,21,44,55,63,101,104,107,110,113,120,126`).
- `patches.txt:1-2` — both dotted paths.
- `patches/v0_2_0/backfill_s3_object_key.py:19` (import) and **`:34` — the dotted path is embedded in a SQL `LIKE` literal** (`'/api/method/frappe_s3_attachment.controller.generate_file%'`); `patches/v0_2_1/ensure_data_import_ignored_doctype.py:10` (import).
- `controller.py:19` job-id constant, `:243` `generate_method` string (**this string is baked into every existing private `File.file_url` in the DB → a data-migration patch is required, not just a code rename**), `:291` the `_s3_file_regex_match` regex, `:323` the enqueue dotted path, `:107` & `:111` logger names, `:28-29` `frappe.get_doc("S3 File Attachment", "S3 File Attachment")`, `:319` `get_single_value("S3 File Attachment", …)`.
- `install.py:23` `frappe.get_single("S3 File Attachment")`, `:55` description string, `:58` `"module": "Frappe S3 Attachment"`.
- `s3_file_attachment.js:4` `frappe.ui.form.on('S3 File Attachment', …)`, `:10` `method: "frappe_s3_attachment.controller.migrate_existing_files"`.
- `s3_file_attachment.json:117` `"options": "S3 Ignored DocType Row"`, `:132` `"module"`, `:133` `"name"`; `s3_ignored_doctype_row.json:28` `"module"`, `:29` `"name"`.
- `modules.txt:1` `Frappe S3 Attachment` (no trailing newline).
- `config/desktop.py:7,11`; `config/docs.py:5,6,12`.
- Public hook contract name **`s3_key_generator`** (`controller.py:95`, documented `README.md:116-132`) — renaming it breaks downstream site apps; needs an alias/deprecation shim.
- Field name **`s3_object_key`** (`install.py:4`) and index name **`s3_object_key_index`** (`install.py:5`) — renaming requires a DB migration; keeping them is the lower-risk option.

**D. Tests / CI / tooling / docs**
- `tests/test_controller.py` (71 identifier hits, incl. every `patch("frappe_s3_attachment.controller…")` target and the literal `_PRIVATE_API_URL` at `:650-653`), `tests/test_install.py` (13), `tests/test_minio_integration.py` (5, incl. the `FRAPPE_S3_ATTACHMENT_MINIO_*` env-var names at `:26-30`), `tests/__init__.py:1`, `doctype/s3_file_attachment/test_s3_file_attachment.py:8` (import path), `test_s3_file_attachment.js:5,12,13`.
- `.github/workflows/ci.yml` — `:25` concurrency group, `:108`/`:209` `bench get-app … frappe_s3_attachment`, `:111`/`:212` `install-app`, `:120`/`:221` `run-tests --app`, `:202-203` + `:228` bucket name `frappe-s3-attachment-test`, `:225-229` the five env-var names.
- `.pre-commit-config.yaml:11` `files: "frappe_s3_attachment.*"`.
- `.gitignore:6` `frappe_s3_attachment/docs/current`.
- `locale/main.pot` (59 hits: header `Project-Id-Version`, `Report-Msgid-Bugs-To`, and every `#:` source reference) and `locale/de.po` (60 hits) — must be regenerated, not sed-ed, since msgids include the doctype label `S3 File Attachment`.
- `README.md` (14 hits: title `:1`, install commands `:19-20`, delta table `:34-47`, Desk walkthrough `:107-112`, MinIO env vars `:89-95`) and `CHANGELOG.md` (6 hits + five release links to `alyf-de/frappe-attachments-s3`); `license.txt` attribution; git remote `origin` already points at `cloud_file_storage.git` while README still instructs `bench get-app https://github.com/alyf-de/frappe-attachments-s3`.