> **STATUS (P0.5): HISTORICAL EVIDENCE.** Captured 2026-08-14 during planning; verified
> file:line ground truth at capture time. Where later decisions supersede content here,
> `docs/PLAN.md` / `docs/adr/` win. Known supersessions: P0 is COMPLETE (the "bench is
> currently broken" state described in rename-and-bench-reality §0 was RESOLVED by the
> ops-only .pth repair — see PROGRESS.md); the CI matrix/floor is {v15.16.0, v15.93.0,
> version-15} with `frappe>=15.16.0,<16.0.0` (A34), not the v15.0.0-based examples.

# COMPATIBILITY CONTRACT — File API consumers in frappe / erpnext / india_compliance / hrms

`india_compliance` HEAD: `6173e59d9c784eff8d57b7735d67d9869049e6ac chore(release): Bumped to Version 15.18.1`
`frappe` HEAD: `405aa71fc738a33a63930d0b71ca7f21fe41527c` — `__version__ = "15.93.0"` (`/home/user/v15/apps/frappe/frappe/__init__.py:54`), branch `version-15.93`
`hrms`: no `.get_content(` / `get_full_path(` / `get_file_path(` / `save_file(` / `write_file(` call sites at all (grep over `hrms/**/*.py` returns nothing); only File-doc *inserts* (see §4).

## 0. Baseline: what `get_content()` actually is today

`/home/user/v15/apps/frappe/frappe/core/doctype/file/file.py:566`
```python
def get_content(self) -> bytes:
```
- **No parameters.** `grep -rn "encodings" /home/user/v15/apps/frappe/` returns **zero hits** anywhere in the frappe app.
- Behaviour (`file.py:566-592`): if `self.content` is set → returns it as-is (b64-decoded via `decode_file_content` at `file/utils.py:422-428` when `self.decode`); otherwise `validate_file_url()` (`file.py:215`) → `open(self.get_full_path(), "rb")` → `f.read()` → **tries `.decode()` and returns `str` on success, `bytes` on `UnicodeDecodeError`** (`file.py:583-589`). **The `-> bytes` annotation is a lie.**
- Identical dual-type logic is duplicated in `frappe/utils/file_manager.py:329-343` (`get_file`).

**The india_compliance trap, verbatim** — `/home/user/v15/apps/india_compliance/india_compliance/gst_india/doctype/gst_return_log/gst_return_log.py:188`:
```python
    frappe.response["filecontent"] = file.get_content(encodings=[])
```
Against this exact frappe (15.93.0) that is a `TypeError: get_content() got an unexpected keyword argument 'encodings'`. IC 15.18.1 is written against a `File.get_content(self, encodings=...)` signature that this frappe does not have. **Our subclass must accept `encodings` as an optional keyword and treat `encodings=[]` as "return raw bytes, attempt no decoding".** Callers reached from `gst_return_log.js:27` (`cmd: ...gst_return_log.download_file`).

---

## 1. `get_content` CONSUMERS

| Caller (file:line) | What it passes | What it expects back | Notes |
|---|---|---|---|
| `india_compliance/.../gst_return_log/gst_return_log.py:188` | **`get_content(encodings=[])`** — kwarg | **raw `bytes`**, no decode attempt, streamed to `frappe.response["filecontent"]` → `response.py:115-129 as_raw()` | **Hard signature requirement.** Breaks with `TypeError` on any override lacking `encodings`. This is the exact DFP failure. |
| `india_compliance/.../gst_return_log.py:55` (`get_json_for`) | no args | `bytes` → `get_decompressed_data()` → `gzip.decompress(content)` (`gst_return_log.py:334-335`) | Gzip magic `\x1f\x8b` normally fails UTF-8 decode so frappe returns bytes *by accident*. No `isinstance` guard. Also catches only `FileNotFoundError` (`:57`) — an S3 `ClientError` would escape. |
| `india_compliance/.../gst_return_log.py:98` (`update_json_for`, `overwrite=False`) | no args | `bytes` → `gzip.decompress` | Same; then re-writes via `file.save_file(content=..., overwrite=True)` at `:103`. |
| `india_compliance/.../purchase_reconciliation_tool.py:116,207` | *not* `get_content` — goes through local path, see §2 | — | GSTR-2A/2B JSON upload path. |
| `frappe/email/email_body.py:251` (`attach_file`) | no args | `bytes` for non-text MIME: `MIMEImage(fcontent)` / `MIMEAudio` / `MIMEBase.set_payload` (`email_body.py:464-473`); only `maintype == "text"` re-encodes `str` (`:459-463`) | **Outbound email attachments require bytes for PDFs/images.** |
| `frappe/email/doctype/email_queue/email_queue.py:393` | no args | same as above (`add_attachment(**attachment)` `:396`) | Main production outbound path (`include_attachments`). |
| `frappe/handler.py:286` (`download_file`, `allow_guest`) | no args | value assigned to `frappe.local.response.filecontent`, emitted by `response.py:115-129 as_raw()` → `response.data = ...` | Werkzeug re-encodes a `str` as UTF-8; silent corruption risk for binaries that happened to decode. |
| `frappe/utils/pdf.py:291` (`_get_base64_image`) | no args | **`bytes`** — `base64.b64encode(file.get_content())` | Print/PDF inline private-image embedding; `str` → `TypeError` (swallowed by `except Exception` `:293`, image silently disappears from the PDF). |
| `frappe/core/doctype/data_import/importer.py:435` | no args | `bytes` for xlsx (`BytesIO(fcontent)`, `xlsxutils.py:107,113`) and xls (`xlrd.open_workbook(file_contents=...)`, `xlsxutils.py:122-123`); `str` **or** `bytes` for csv (`csvutils.py:39-50`) | Data Import template read. |
| `frappe/core/doctype/data_import/importer.py:598` (`read_file`) | no args | same | Path-based variant. |
| `frappe/core/doctype/prepared_report/prepared_report.py:96,97` | no args | **`bytes`** — `gzip.decompress(...)` | Prepared Report result retrieval; no guard. |
| `frappe/utils/csvutils.py:31` | no args | `str` or `bytes` — `read_csv_content` handles both (`csvutils.py:39-50`) | Tolerant. |
| `frappe/core/doctype/file/file.py:111` (`before_insert`) | no args | round-trips into `self.save_file(content=...)` | Insert path. |
| `frappe/core/doctype/file/file.py:658` (`save_file`) | no args | stored as `flags.original_content`; `on_rollback` (`file.py:178-186`) branches on `isinstance(..., bytes)` → `"wb+"` vs `str` → `"w+"` | **Proof frappe itself knows both types are returned.** |
| `frappe/core/doctype/file/file.py:801` (`optimize_file`) | no args | **`bytes`** — `optimize_image(content=...)` | Image optimization. |
| `frappe/core/doctype/file/file.py:833` (`zip_files`) | no args | `str` or `bytes` — `zf.writestr` accepts both | |
| `frappe/recorder.py:409` | no args | `json.loads` — both OK | |
| `frappe/patches/v12_0/fix_public_private_files.py:27` | no args | passed to `new_doc.save_file(content=...)` | Patch-time. |
| `erpnext/stock/stock_ledger.py:321-323` | no args | **explicitly defensive**: `if isinstance(content, str): content = content.encode("utf-8")` then `gzip.decompress` | The only caller in any target app that defends against the `str` return. Repost Item Valuation reposting-data. |
| `erpnext/stock/doctype/closing_stock_balance/closing_stock_balance.py:134` | no args | **`bytes`** — `gzip.decompress` | No guard. |
| `erpnext/accounts/doctype/chart_of_accounts_importer/chart_of_accounts_importer.py:138` | no args | **`bytes`** for xlsx/xls (`:141-143`) | |
| `frappe/core/doctype/file/file.py:528-561` (`unzip`) | uses `get_full_path()`, **not** `get_content()` — `zipfile.ZipFile(zip_path)` at `:533` | local path | See §2; `unzip` is exposed via `frappe/core/api/file.py:10-13 unzip_file`. |

**Non-File false positives** (do not constrain us): `frappe/email/receive.py:925` is `Email.get_content()`; `frappe/core/doctype/communication/mixins.py:289` is `Communication.get_content(print_format=...)`; `frappe/website/doctype/website_meta_tag/website_meta_tag.py:29,33`; `frappe/public/js/frappe/form/controls/html.js:7,20`.

---

## 2. LOCAL-PATH ASSUMERS (need materialization/caching when content is S3-only)

Ranked by production likelihood on an ordinary-attachment workload.

| # | Site | Mechanism | Why it matters |
|---|---|---|---|
| 1 | `frappe/core/doctype/file/file.py:564` `exists_on_disk` → `os.path.exists(get_full_path())` | called from `save_file` dedup (`file.py:687-695`) and tests | Runs on **every File insert** that hits an existing `content_hash`. If the twin lives only in S3 this returns False and dedup silently degrades. |
| 2 | `frappe/core/doctype/file/file.py:204-213` `validate_file_path` (`os.path.realpath(...).startswith(base_path)`) and `:358-366` `validate_file_on_disk` (`os.path.exists(full_path)` → `frappe.throw("File {0} does not exist")`) | `validate()` on every save (`file.py:135-137`) | **Every `File.save()` of an S3-only file throws IOError** unless overridden. Highest-frequency blocker. |
| 3 | `frappe/core/doctype/file/file.py:226-268` `handle_is_private_changed` → `shutil.move(source, target)`, `frappe.throw("Cannot find file {} on disk", FileNotFoundError)` at `:255` | public↔private toggle | Must be reimplemented as an S3 key/ACL move. |
| 4 | `frappe/core/doctype/file/file.py:163-195` `on_rollback` → `open(get_full_path(), "wb+"/"w+")`, `shutil.move` | DB rollback | Rollback semantics for S3 writes must be designed here. |
| 5 | `frappe/core/doctype/file/utils.py:86-115` `get_local_image` → `Image.open(frappe.get_site_path(...))` | thumbnails via `file.py:435-474 make_thumbnail` (`:449` chooses local vs `get_web_image`); thumbnail is written to `frappe.get_site_path("public", ...)` at `:464-471` | **Thumbnails/avatars/item images.** Note `make_thumbnail` swallows `OSError` (`:451`) → thumbnails silently vanish. |
| 6 | `frappe/utils/data.py:1433-1462` `get_thumbnail_base64_for_image` → `frappe.get_site_path("public", src)` + `file_exists` + `get_local_image` | website/list image previews | Also gated on `src.startswith("/files")` (`:1444`). |
| 7 | `frappe/utils/xlsxutils.py:103-107` `read_xlsx_file_from_attached_file(file_url=...)` → `_file.get_full_path()` → `load_workbook(filename=path)` | xlsx by URL | All in-repo callers currently use `fcontent=` (`importer.py:610`, `chart_of_accounts_importer.py:141`, `erpnext/.../bank_transaction_upload.py:29`), so this is a third-party/API surface risk. |
| 8 | `erpnext/accounts/doctype/bank_statement_import/bank_statement_import.py:202-212` `write_files` → `get_full_path()` then `open(full_file_path, "w")` / `write_xlsx(file_path=...)` | Bank Statement Import | **Writes back through the local path**, bypassing File. Needs read-modify-write-to-S3. |
| 9 | `erpnext/stock/stock_ledger.py:407-424` `create_json_gz_file` → `file_doc.get_full_path()` then `open(path, "wb")` | Repost Item Valuation | Also contains the app-name hack, §3. |
| 10 | `erpnext/accounts/doctype/chart_of_accounts_importer/chart_of_accounts_importer.py:116-118` `get_full_path()` + `open(file_path)` (CSV branch) | CoA import | |
| 11 | `erpnext/regional/doctype/import_supplier_invoice/import_supplier_invoice.py:59` `zipfile.ZipFile(zip_file.get_full_path())` | Italy XML zip import | |
| 12 | `frappe/core/doctype/file/file.py:533` `zipfile.ZipFile(self.get_full_path())` in `unzip()` | user-triggered unzip (`core/api/file.py:10-13`) | |
| 13 | `india_compliance/gst_india/doctype/purchase_reconciliation_tool/purchase_reconciliation_tool.py:116` (`upload_gstr`) and `:207` (`get_return_period_from_file`) → `get_json_from_file` (`gst_india/utils/__init__.py:671-672`) → `frappe.utils.file_manager.get_file_path` (`file_manager.py:346-377`) → `frappe.get_file_json` → `open(path)` (`frappe/__init__.py:1708-1711`) | **GSTR-2A/2B JSON upload — a real india_compliance production flow** | `get_file_path` resolves a File name/`file_name` to `file_url` via SQL then to a **local** path and `frappe.throw`s on anything not `/files/` or `/private/files/` (`file_manager.py:373-374`). No remote branch at all. `:207` swallows all exceptions (`:214-215`). |
| 14 | `frappe/utils/response.py:267-311` `download_private_file` → `send_private_file` → `os.path.exists(filepath)` else `NotFound` (`:298-300`), or `X-Accel-Redirect` to `/protected/...` (`:284-291`) | **every `/private/files/...` browser hit** (routed at `frappe/app.py:125-126`) | This is the private-serving chokepoint to replace with permission-check → signed URL. Permission is checked by `find_file_by_url` (`file/utils.py:430-445`) + `File.is_downloadable` → `has_permission` (`file.py:757-758`, `:843-876`); access logged at `response.py:279`. |
| 15 | `frappe/core/doctype/file/file.py:424-433` `generate_content_hash` → `open(get_files_path(...), "rb")` | migration/patch paths | Throws `File {0} does not exist` on `OSError`. |
| 16 | `frappe/core/doctype/package_import/package_import.py:47-56` → `get_files_path(...)` passed to `subprocess tar xzf` | Package Import | External process → **must** be a real local file. |
| 17 | `erpnext/regional/italy/utils.py:126-135` `download_zip` → `frappe.utils.get_files_path(file.file_name, is_private=...)` + `zip_file.write(file_path)` | Italy e-invoice bulk export | |
| 18 | `frappe/utils/pdf.py:281-290` `pdf_to_base64` (in `frappe/utils/data.py:1490` region → actually `frappe/utils/data.py:1486-1496`) → `get_file_path(filename)` + `open(file_path, "rb")` | Jinja `pdf_to_base64` in print formats | |
| 19 | `frappe/utils/print_format.py:286-289` `output.write(open(file_path,"wb"))` + `conn.printFile(...)` | network printing | tmp path, not attachment storage. |

---

## 3. `file_url` SHAPE ASSUMPTIONS

**Canonical shapes tolerated:** `"/files/<name>"`, `"/private/files/<name>"`. **Remote shapes:** `URL_PREFIXES = ("http://", "https://", "/api/method/")` (`file.py:40`), detected by `is_remote_file` (`file.py:81-83`).

Where each is enforced:

| Site | Rule | Consequence for non-canonical `file_url` |
|---|---|---|
| `file.py:215-224` `validate_file_url` | early-returns for `is_remote_file`; else **must** start with `("/files/", "/private/files/")` or `frappe.throw("URL must start with http:// or https://")` | An `/api/method/...` or `https://` URL passes validation. |
| `file.py:594-627` `get_full_path` | `/private/files/`→local, `/files/`→local, `URL_PREFIXES`→**pass-through unchanged** (`:615-616`), then `is_safe_path(file_path)` (`:620`) | `is_safe_path` (`file_manager.py:422-431`) returns True only for `http(s)://` — so **`/api/method/...` file_urls `frappe.throw("Cannot access file path")` here**, and `https://...` survives `get_full_path` only to be handed to `open()` in `get_content` (`file.py:581`) → `FileNotFoundError`. **Conclusion: neither remote shape is a viable `file_url` for content access.** |
| `file.py:600-604` | strips a leading site URL when `"/files/" in file_path` | absolute site URLs are normalized, S3 hostnames are not. |
| `file.py:330-334` `validate_remote_file` | strips site_url prefix when `"/files/" in file_url` | |
| `file.py:782-785` `set_is_private` | `is_private = cint(file_url.startswith("/private"))` | A non-`/private` S3 route makes a private file look **public**. |
| `file/utils.py:313-361` `attach_files_to_document` (registered on `on_update`/`on_submit` for `*` in `frappe/hooks.py:156,177`) | `if not (value or "").startswith(("/files", "/private/files")): continue` (`:326`); `is_private` derived from `value.startswith("/private")` (`:358`) | **Attach / Attach Image fields whose value is an `/api/method/...` or absolute URL are silently never linked to a File record** → attachments orphaned, `protect_attached_files`/refcount logic blind. |
| `frappe/app.py:125-126` | `request.path.startswith("/private/files/")` → `download_private_file` | any other private route bypasses the permission gate entirely. |
| `frappe/utils/data.py:1444` | `if not src.startswith("/files") or ".." in src: return` | image thumbnail base64 silently disabled. |
| `file.py:449` `make_thumbnail` | `startswith(("/files","/private/files"))` → local, else `get_web_image` HTTP fetch (`file/utils.py:118-150`) | An S3 URL would be **re-downloaded over HTTP**, and thumbnail saved to `get_site_path("public", ...)` (`file.py:464`) — signed URLs expire → `HTTPError` swallowed at `:451`. |
| `file.py:811-818` `unique_url` | `file_url + "?" + urlencode({"fid": name})` for private | consumed by `pdf.py:283-289` (`urlparse(src).path` + `fid` query) and `file/utils.py:283`. Query-string-bearing signed URLs break this concatenation. |
| `file/utils.py:430-445` `find_file_by_url` | **exact string equality** on `file_url` | signed/expiring URLs can never be resolved back to a File. |
| `file.py:838-840` `on_doctype_update` | `frappe.db.add_index("File", ["file_url(100)"])` | long S3/signed URLs collide in the 100-char index prefix → lookup scans. |
| `frappe/utils/file_manager.py:373-374` `get_file_path` | `else: frappe.throw("There is some problem with the file url")` | no remote branch at all (hits india_compliance §2 #13). |
| `frappe/utils/file_manager.py:309-326` `delete_file` | splits path, maps `files` → `public/files`, **everything else → `private/files`** | a remote URL is mangled into a bogus private path; deletion becomes a silent no-op. Reached from `File.delete_file_from_filesystem` (`file.py:752-757`), which is itself behind the `delete_file_data_content` hook (`file.py:742-747`). |
| **`erpnext/stock/stock_ledger.py:416`** | `if "/frappe_s3_attachment." in file_doc.file_url:` → delete + recreate | **Hard-coded old app name in a `file_url` substring test.** After the rename to `cloud_file_storage` this branch stops firing and `create_json_gz_file` falls through to `file_doc.get_full_path()` + `open(path,"wb")` (`:420-422`) on a file that is not local → `FileNotFoundError` in Repost Item Valuation. Either keep the `frappe_s3_attachment.` token in generated URLs or make `get_full_path()`/writes S3-aware. |
| JS | `frappe/public/js/frappe/form/sidebar/attachments.js:182` `file_url = "/files/" + attachment.file_name;` fallback; `frappe/public/js/frappe/form/controls/attach.js:101-105` parses `filename,dataurl` via `/^([^:]+),(.+):(.+)$/` and sets the raw `attachment.file_url` into the model (`:139,:143`) | a `file_url` containing `,` and `:` (query-string signed URLs) is misparsed as a dataurl by `attach.js:101`. |

---

## 4. UPLOAD-SIDE CONSUMERS (write-path contract)

Two distinct write paths exist and **both** must be handled:

**(a) `File` document path** — `File.save_file` → `call_hook_method("before_write_file", file_size=...)` then `get_hook_method("write_file")` → falls back to `save_file_on_filesystem` (`file.py:704-715`, `:717-726`). Hook resolution: `frappe/utils/__init__.py:624-638`. Dedup by `content_hash` + `is_private` (`file.py:684-702`), gated by `exists_on_disk()`.

**(b) legacy `frappe.utils.file_manager.save_file`** — different signature and a **different fallback**: `write_file_method(fname, content, content_type=..., is_private=...)` (`file_manager.py:148-190`, hook call at `:167-169`), plus `get_file_data_from_hash` which copies only `frappe.get_hooks()["write_file_keys"] = ["file_url", "file_name"]` (`frappe/hooks.py:70`, `file_manager.py:192-198`).

| Caller | Path | Payload |
|---|---|---|
| `frappe/email/receive.py:581-598` `save_attachments_in_doc` | (a) `frappe.get_doc({"doctype":"File", ..., "is_private":1, "content": attachment["fcontent"]}).save()` | **Inbound email attachments — raw bytes, always private.** Swallows `MaxFileSizeReachedError` (`:603-605`). |
| `frappe/handler.py:236-248` `upload_file` (`allow_guest`) | (a) File doc with `content` bytes, `file_url`, `is_private` | Primary interactive upload; optional `optimize_image` at `:215-221`. |
| `frappe/core/doctype/file/utils.py:266-278` `_save_file` (`extract_images_from_html`) | (a) File insert with decoded+optimized bytes, `decode: False` | Rich-text inline images; returns `_file.unique_url` into the HTML (`:280`). |
| `frappe/utils/print_format.py:205-215` | (a) File insert with `merged_pdf.getvalue()`, `is_private: 1`, then publishes `_file.unique_url` (`:217`) | Bulk print/PDF. |
| `frappe/core/doctype/data_import/*` / `frappe/utils/csvutils.py` | reads only | — |
| `hrms/api/__init__.py:739-751` `upload_base64_file` | (a) File insert, `content=file_content` bytes, `is_private: 1`, `folder: "Home"` | HRMS mobile/PWA uploads. |
| `india_compliance/.../gst_return_log.py:71-86` | (a) File insert, `content` = **gzip bytes**, `is_private: 1`, `attached_to_field=<file_field>` | GST return JSON storage; `db_set(file_field, file.file_url)` at `:87` — **the `file_url` is persisted into a business field**. |
| `india_compliance/.../gst_return_log.py:103` | (a) `file.save_file(content=content, overwrite=True)` then `db_set(file_field, file.file_url)` | `overwrite=True` skips the `generate_file_name` hash-suffix rename (`file.py:705-710`) → **same key rewritten in place**; content-hash dedup + refcount must handle mutate-in-place. |
| `india_compliance/gst_india/utils/e_waybill.py:741` | **(b)** `save_file(pdf_filename, pdf_content, doc.doctype, doc.name, is_private=1)` (import at `e_waybill.py:17`) | e-Waybill PDF attach; preceded by `delete_file(doc, pdf_filename)` (`:740`, defined `:745-758`, uses `frappe.delete_doc("File", ..., force=True)`). **Proves path (b) is live in production.** |
| `erpnext/regional/doctype/import_supplier_invoice/import_supplier_invoice.py:104-113` | **(b)** `save_file(file_name, encoded_content, "Purchase Invoice", pi_name, folder=None, decode=False, is_private=0, df=None)` | Bulk per-invoice XML attach. |
| `erpnext/stock/stock_ledger.py:407-441` `create_json_gz_file` / `create_file` | (a) for new (`:429-441`), **raw `open(path,"wb")`** for existing (`:420-422`) | Repost Item Valuation; see §3 app-name hack. |
| `erpnext/stock/.../closing_stock_balance.py:125-127` | via `create_json_gz_file` | |
| `frappe/patches/v12_0/fix_public_private_files.py:27` | (a) `new_doc.save_file(content=..., ignore_existing_file_check=True)` | Shows the `ignore_existing_file_check` escape hatch. |
| `frappe/utils/file_manager.py:94-101` `save_uploaded` / `:103-136` `save_url` | (b) | Legacy `cmd=uploadfile` surface. |

---

## 5. CONTRACT SUMMARY — hard invariants for the `File` override

1. **`get_content()` MUST accept an optional `encodings` keyword.** Strictest caller: `india_compliance/.../gst_return_log/gst_return_log.py:188` — `file.get_content(encodings=[])`. Anything else is an immediate `TypeError` on the GST Return Log download endpoint. Signature target: `def get_content(self, encodings=None) -> bytes | str`.
2. **`get_content(encodings=[])` MUST return raw `bytes` with no decode attempt** — same caller; the payload is gzip and is piped straight to `response.py:115-129 as_raw()`.
3. **`get_content()` with no args MUST return `bytes` for any content that is not valid UTF-8** — strictest callers: `frappe/utils/pdf.py:291` (`base64.b64encode`, `TypeError` on str), `prepared_report.py:96-97` and `closing_stock_balance.py:134` and `gst_return_log.py:55,98` (`gzip.decompress`, requires bytes-like), `email_body.py:465-470` (`MIMEImage`/`set_payload`). Test matrix must assert bytes for: PNG/JPG, PDF, `.gz`, `.xlsx`, `.zip`.
4. **`get_content()` with no args MUST preserve the legacy behaviour of returning `str` for UTF-8-decodable content** — `erpnext/stock/stock_ledger.py:321-323` re-encodes when it sees `str`, and `File.on_rollback` (`file.py:178-186`) branches on `isinstance(..., bytes)` vs `str` to pick `"wb+"`/`"w+"`. Changing text files to bytes is a behaviour change; keep byte-for-byte round-trip either way (`csvutils.py:39-50` accepts both).
5. **`get_content()` MUST NOT require the object to exist on local disk, and MUST NOT raise a bare local-FS exception.** Strictest caller: `gst_return_log.py:53-59`, which catches **only** `FileNotFoundError` and on it performs `self.db_set(file_field, None)` — i.e. it *destroys the pointer*. Any S3 transport error must therefore be mapped to `FileNotFoundError` **only** when the object is genuinely absent; transient S3 failures must raise something else or the GST log is silently wiped.
6. **`File.save()` / `validate()` MUST NOT throw when the object is remote-only.** Strictest sites: `file.py:358-366 validate_file_on_disk` (`frappe.throw("File {0} does not exist", IOError)`) and `file.py:204-213 validate_file_path` (realpath must be under `get_files_path()`). Every re-save of an S3-only File hits both.
7. **`get_full_path()` MUST return a path that `open()`, `zipfile.ZipFile()`, `Image.open()`, `load_workbook()` and `csv.reader` can consume** (i.e. materialize-on-demand + cache). Strictest callers: `import_supplier_invoice.py:59`, `file.py:533` (`unzip`), `bank_statement_import.py:202`, `chart_of_accounts_importer.py:116`, `stock_ledger.py:420`, `xlsxutils.py:106`. Note `get_full_path` currently **throws** for `/api/method/` URLs via `is_safe_path` (`file.py:620`, `file_manager.py:422-431`).
8. **Writes performed directly against `get_full_path()` MUST be flushed back to S3.** Strictest callers: `bank_statement_import.py:202-212` (`open(path,"w")` + `write_xlsx(file_path=...)`) and `stock_ledger.py:420-422` (`open(path,"wb")`). A read-only materialization cache silently loses these writes.
9. **`file_url` MUST remain exactly `/files/<name>` or `/private/files/<name>`.** Traced to: `file/utils.py:326` (`attach_files_to_document` skips anything else → orphaned attachments), `file.py:784` (`is_private` derived from `startswith("/private")`), `frappe/app.py:125` (private routing), `file/utils.py:430-445` (`find_file_by_url` exact match), `data.py:1444`, `file.py:449`. Signed/expiring URLs must be issued at *serve* time (redirect from `/private/files/...`), never stored in `file_url`.
10. **Private serving MUST keep the `find_file_by_url` → `File.is_downloadable()` → `has_permission()` gate before issuing any URL.** Traced to `frappe/utils/response.py:267-280` and `file.py:757-758` / `:843-876`; the access log at `response.py:279` (`make_access_log`) must also still fire.
11. **`File.save_file(content=..., overwrite=True)` MUST rewrite the same object in place and keep `file_url` stable.** Strictest caller: `gst_return_log.py:103` followed by `db_set(file_field, file.file_url)` at `:104`; `overwrite=True` bypasses the hash-suffix rename at `file.py:705-710`. Content-hash dedup and refcounting must tolerate an object whose content changes under a fixed key.
12. **Both write hooks must be implemented with their distinct signatures.** `write_file(self: File)` for `File.save_file` (`file.py:709-712`) **and** `write_file(fname, content, content_type=, is_private=)` returning a dict for `frappe.utils.file_manager.save_file` (`file_manager.py:167-169`), whose result is filtered to `write_file_keys = ["file_url", "file_name"]` (`frappe/hooks.py:70`, `file_manager.py:192-198`). Live path-(b) callers: `india_compliance/gst_india/utils/e_waybill.py:741`, `erpnext/.../import_supplier_invoice.py:104`.
13. **Deletes MUST be refcount-safe and MUST go through `delete_file_data_content`.** `File._delete_file_on_disk` (`file.py:511-526`) already queries other Files sharing `content_hash` (**ignoring `is_private`**, see the comment at `:518-519`) and dispatches through the `delete_file_data_content` hook (`file.py:742-747`). The fallback `frappe/utils/file_manager.py:309-326 delete_file` maps any non-`files/` path into `private/files/` — an S3-shaped path becomes a silent no-op, so the hook must be authoritative.
14. **`exists_on_disk()` MUST report remote existence**, because `File.save_file` uses it to decide whether to reuse `duplicate_file.file_url` (`file.py:687-695`). Returning False for S3-only twins breaks dedup and creates orphan objects.
15. **`content_hash` MUST continue to be MD5 of the *bytes*.** `file/utils.py:187-190 get_content_hash` / `file_manager.py:380-383` encode `str` before hashing; the Cloud Storage Object key/dedup index must use the same value, and `generate_content_hash` (`file.py:424-433`) must not `frappe.throw` for remote files (it already early-returns on `is_remote_file` at `:425`).
16. **`is_private` toggling MUST work without `shutil.move`.** `file.py:226-268 handle_is_private_changed` throws `FileNotFoundError`/`FileExistsError` on local paths and propagates the new `file_url` back into `attached_to_field` on the parent doc (`:279-292`) and sibling Files via `update_existing_file_docs` (`file/utils.py:301-310`) — that propagation must be preserved.
17. **Do not break `erpnext/stock/stock_ledger.py:416`'s `"/frappe_s3_attachment." in file_doc.file_url` sentinel** when renaming to `cloud_file_storage`, or make `get_full_path()`+write S3-aware so the fallthrough at `:420-422` is safe. This is the single hard-coded old-app-name dependency in the target apps.
18. **`Attach`/`Attach Image` values ending up in business fields must be re-resolvable.** `gst_return_log.py:87,104` store `file.file_url` in GST Return Log fields; `stock_ledger.py:308-318` and `:395-405` look Files up **by `file_url` + `attached_to_field`**. Any URL rewrite must be applied to these stored values or lookups return empty (`stock_ledger.py:318` returns `frappe._dict()` → reposting silently produces no data).
19. **india_compliance fixture reads are NOT in scope**: `gst_india/setup/__init__.py:174`, `overrides/company.py:78`, `income_tax_india/overrides/company.py:97`, `tests/__init__.py:60`, `utils/test_e_invoice.py:50`, `utils/test_e_waybill.py:65,309`, `.../test_purchase_reconciliation_tool.py:68` all call `frappe.get_file_json` on **app-source** paths, not File records. Only `gst_india/utils/__init__.py:672` (`get_json_from_file`) resolves through the File table.