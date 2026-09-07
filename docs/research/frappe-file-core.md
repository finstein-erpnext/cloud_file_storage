> **STATUS (P0.5): HISTORICAL EVIDENCE.** Captured 2026-08-14 during planning; verified
> file:line ground truth at capture time. Where later decisions supersede content here,
> `docs/PLAN.md` / `docs/adr/` win. Known supersessions: P0 is COMPLETE (the "bench is
> currently broken" state described in rename-and-bench-reality §0 was RESOLVED by the
> ops-only .pth repair — see PROGRESS.md); the CI matrix/floor is {v15.16.0, v15.93.0,
> version-15} with `frappe>=15.16.0,<16.0.0` (A34), not the v15.0.0-based examples.

# FRAPPE v15 FILE SUBSYSTEM — GROUND TRUTH REPORT

**Target revision:** `/home/user/v15/apps/frappe` @ `405aa71fc738a33a63930d0b71ca7f21fe41527c` — "chore(release): Bumped to Version 15.93.0", branch `version-15.93`, `__version__ = "15.93.0"` (`/home/user/v15/apps/frappe/frappe/__init__.py:54`).
All citations below are from this revision. Path prefix used throughout: `/home/user/v15/apps/frappe/`.

---

## 1. FILE CLASS ANATOMY

File: `/home/user/v15/apps/frappe/frappe/core/doctype/file/file.py` (895 lines). Module constants: `URL_PREFIXES = ("http://", "https://", "/api/method/")` (line 40); `exclude_from_linked_with = True` (34). Class `File(Document)` starts at line 43; `no_feed_on_delete = True` (70).

### Exact signatures (all methods of class File, in file order)

| Line | Signature |
|---|---|
| 72 | `def __init__(self, *args, **kwargs)` — sets `self.content = self.get("content") or b""`, `self.decode = self.get("decode", False)` (77-78) |
| 80-81 | `@property def is_remote_file(self)` — `if self.file_url: return self.file_url.startswith(URL_PREFIXES)` else `return not self.content` (82-84) |
| 86 | `def autoname(self)` — folders: path-based; files: `frappe.generate_hash(length=10)` (95) |
| 97 | `def before_insert(self)` |
| 117 | `def after_insert(self)` |
| 121 | `def validate(self)` |
| 140 | `def validate_attachment_references(self)` |
| 150 | `def after_rename(self, *args, **kwargs)` |
| 154 | `def on_trash(self)` |
| 163 | `def on_rollback(self)` |
| 197 | `def get_name_based_on_parent_folder(self) -> str | None` |
| 201 | `def get_successors(self)` |
| 204 | `def validate_file_path(self)` |
| 215 | `def validate_file_url(self)` |
| 226 | `def handle_is_private_changed(self)` |
| 293 | `def fetch_attached_to_field(self, old_file_url)` |
| 304 | `def validate_attachment_limit(self)` |
| 330 | `def validate_remote_file(self)` |
| 336 | `def set_folder_name(self)` |
| 347 | `def set_file_type(self)` |
| 358 | `def validate_file_on_disk(self)` |
| 368 | `def validate_file_extension(self)` |
| 380 | `def check_content(self)` |
| 384 | `def validate_duplicate_entry(self)` |
| 414 | `def set_file_name(self)` |
| 424 | `def generate_content_hash(self)` |
| 435 | `def make_thumbnail(self, set_as_thumbnail: bool = True, width: int = 300, height: int = 300, suffix: str = "small", crop: bool = False) -> str` |
| 476 | `def validate_empty_folder(self)` |
| 481 | `def validate_protected_file(self)` |
| 511 | `def _delete_file_on_disk(self)` |
| 528 | `def unzip(self) -> list["File"]` |
| 563 | `def exists_on_disk(self)` |
| 566 | `def get_content(self) -> bytes` |
| 594 | `def get_full_path(self)` |
| 629 | `def write_file(self)` |
| 647 | `def save_file(self, content: bytes | str | None = None, decode=False, ignore_existing_file_check=False, overwrite=False)` |
| 717 | `def save_file_on_filesystem(self)` |
| 728 | `def check_max_file_size(self)` |
| 742 | `def delete_file_data_content(self, only_thumbnail=False)` |
| 749 | `def delete_file_from_filesystem(self, only_thumbnail=False)` |
| 757 | `def is_downloadable(self)` |
| 760 | `def get_extension(self)` |
| 764 | `def create_attachment_record(self)` |
| 774 | `def add_comment_in_reference_doc(self, comment_type, text)` |
| 782 | `def set_is_private(self)` |
| 786-787 | `@frappe.whitelist() def optimize_file(self)` |
| 810-811 | `@property def unique_url(self) -> str` |
| 820-821 | `@staticmethod def zip_files(files)` |

Module-level (not methods): `on_doctype_update()` (838), `has_permission(doc, ptype=None, user=None, debug=False)` (843), `get_permission_query_conditions(user: str | None = None) -> str` (878). Note `from .utils import *` (32) and `from frappe.core.api.file import *` (895) merge those namespaces into `frappe.core.doctype.file.file`.

### Lifecycle trace: insert → content save

`before_insert` (97-115), strictly ordered:
1. `set_folder_name()` (98/336) — attaches to `Home/Attachments` folder when `attached_to_doctype` set (342).
2. `set_is_private()` (99/782) — **`is_private` is derived from `file_url` prefix**: `self.is_private = cint(self.file_url.startswith("/private"))`; only runs if `file_url` truthy.
3. `set_file_name()` (100/414) — throws `frappe.MandatoryError` if neither `file_name` nor `file_url` (416-418); derives name from URL tail (420); else `re.sub(r"/", "", self.file_name)` (422).
4. `validate_attachment_limit()` (101/304) — `max_attachments` on the target meta, throws `AttachmentLimitReached`.
5. `set_file_type()` (102/347) — mimetypes-derived extension, uppercased.
6. `validate_file_extension()` (103/368) — **only when `frappe.request` is truthy** (370) against System Settings `allowed_file_extensions`; code/background-created files bypass it.
7. `if self.is_folder: return` (105-106).
8. `if self.is_remote_file: self.validate_remote_file()` (108-109/330) — strips own site URL prefix; **no bytes written, no hash computed**.
9. `else: self.save_file(content=self.get_content())` (111), then `self.flags.new_file = True` (112) and `frappe.db.after_rollback.add(self.on_rollback)` (113).
10. `self.validate_duplicate_entry()` (115) — comment says "Hash is generated in save_file".

`save_file` (647-715) — the single content-write funnel:
- `if self.is_remote_file: return` (654-655).
- `if not self.flags.new_file: self.flags.original_content = self.get_content()` (657-658) — **on update this reads the old bytes into memory for rollback**.
- Sets `self.content` / `self.decode`, materialises `self._content` via `get_content()` (660-663); returns early if `not self._content` (665-666).
- `self.is_private = cint(self.is_private)`; `self.content_type = mimetypes.guess_type(self.file_name)[0]` (671-672).
- Optional EXIF strip for `image/jpeg` per System Settings (675-680).
- `self.file_size = self.check_max_file_size()` (682/728) — limit from `frappe.core.api.file.get_max_file_size` (729-731).
- `self.content_hash = get_content_hash(self._content)` (683) — MD5, `frappe/core/doctype/file/utils.py:187-190`.
- Dedup lookup on `(content_hash, is_private)` unless `ignore_existing_file_check` (686-692); if the duplicate `exists_on_disk()` → reuse its `file_url`, set `file_exists = True` (694-702).
- `if not file_exists:` (704) → unless `overwrite`, rename via `generate_file_name(name=self.file_name, suffix=self.content_hash[-6:], is_private=self.is_private)` (705-710) → `call_hook_method("before_write_file", file_size=self.file_size)` (711) → `write_file_method = get_hook_method("write_file")` (712) → `if write_file_method: return write_file_method(self)` (713-714) → else `return self.save_file_on_filesystem()` (715).

`save_file_on_filesystem` (717-726): `safe_file_name = re.sub(r"[/\\%?#]", "_", self.file_name)` (718); sets `file_url` to `/private/files/<name>` or `/files/<name>` (719-722); calls `self.write_file()` (724); returns `{"file_name": os.path.basename(fpath), "file_url": self.file_url}`.

`write_file` (629-645): returns early if remote (631-632); `file_path = self.get_full_path()` (634); encodes str content (636-637); `self.check_content()` (638, PDF-JS scan); `open(file_path, "wb+")` + `os.fsync` (639-641); registers `on_rollback` (643); returns path.

`validate()` (121-138) — runs on every save, including updates: unquotes `file_url` (126); `validate_attachment_references()` (128); if `not is_new()` and `is_private` changed → `handle_is_private_changed()` (132-133, which does a `shutil.move` on local disk, 265); `validate_file_path()` (135/204 — realpath must live under `get_files_path(is_private=...)`); `validate_file_url()` (136/215 — must start with `/files/` or `/private/files/` when not remote); **`validate_file_on_disk()` (137/358 — `os.path.exists(full_path)` or `frappe.throw(... IOError)`)**; `self.file_size = frappe.form_dict.file_size or self.file_size` (138).

`after_insert` (117-119) → `create_attachment_record()` (764) → Comment of type "Attachment" on the reference doc.

### on_trash → content deletion

`on_trash` (154-161): blocks Home/Attachments folder deletion (155-156); `validate_empty_folder()` (157/476); `validate_protected_file()` (158/481 — submitted docs with `protect_attached_files`); **`self._delete_file_on_disk()` (159)**; then "Attachment Removed" comment (160-161).

`_delete_file_on_disk` (511-526) is the **refcount gate**: `on_disk_file_not_shared = self.content_hash and not frappe.get_all("File", filters={"content_hash": ..., "name": ["!=", self.name]}, limit=1)` (513-522) — note the explicit comment at 518-519 that `is_private` is deliberately **not** part of the refcount filter. If unshared → `delete_file_data_content()` (524); else → `delete_file_data_content(only_thumbnail=True)` (526). `delete_file_data_content` (742-747) consults the `delete_file_data_content` hook, else `delete_file_from_filesystem` (749-755) → `delete_file(self.file_url)` + `delete_file(self.thumbnail_url)` (`frappe/core/doctype/file/utils.py:153-170`, which unlinks `sites/<site>/{public|private}/files/<basename>`).

Whole-document deletion cascade: `frappe/model/delete_doc.py:148` → `frappe.utils.file_manager.remove_all(doctype, name, from_delete=True, ...)` (`/home/user/v15/apps/frappe/frappe/utils/file_manager.py:244-262`) → `frappe.delete_doc("File", fid, ignore_permissions=True, ...)` per attachment.

---

## 2. EXTENSION POINTS (GROUND TRUTH)

Exhaustive grep of `frappe/core/doctype/file/*`, `frappe/core/api/file.py`, `frappe/utils/file_manager.py` for hook consultation yields **exactly three hook names** in the File lifecycle:

**Helper semantics** — `/home/user/v15/apps/frappe/frappe/utils/__init__.py:624-631`:
```python
def get_hook_method(hook_name, fallback=None):
	method = frappe.get_hooks().get(hook_name)
	if method:
		method = frappe.get_attr(method[0])   # FIRST entry wins
		return method
	if fallback:
		return fallback
```
and `/home/user/v15/apps/frappe/frappe/utils/__init__.py:633-638`:
```python
def call_hook_method(hook, *args, **kwargs):
	out = None
	for method_name in frappe.get_hooks(hook):
		out = out or frappe.get_attr(method_name)(*args, **kwargs)
	return out
```
Hook list order = `get_installed_apps()` order, aggregated in `_load_app_hooks` (`frappe/__init__.py:1580-1603`) and cached under `app_hooks` unless `developer_mode` (`frappe/__init__.py:1618-1621`). ⇒ `write_file` / `delete_file_data_content` are **single-owner** hooks (index `[0]`); a second app defining them is silently ignored unless it happens to sort first.

### 2.1 `before_write_file`
- **Call site A:** `frappe/core/doctype/file/file.py:711` — `call_hook_method("before_write_file", file_size=self.file_size)`.
- **Call site B (legacy):** `frappe/utils/file_manager.py:163` — same kwargs.
- **Contract:** every registered function is called with keyword `file_size=<int bytes>`. Return value is aggregated by `call_hook_method` but **discarded at both call sites**. Only side effects/`frappe.throw` matter (quota enforcement). Runs *only* when the file is actually new (i.e. inside the `if not file_exists:` branch at 704) — dedup hits skip it.

### 2.2 `write_file`
- **Call site A (modern, the one that matters):** `frappe/core/doctype/file/file.py:712-715`
  ```python
  write_file_method = get_hook_method("write_file")
  if write_file_method:
      return write_file_method(self)
  return self.save_file_on_filesystem()
  ```
  - **Arguments:** one positional — the `File` **document instance**. No fname/content args.
  - **Available state when invoked:** `self._content` (bytes), `self.file_name` (already hash-suffixed by `generate_file_name`, 706-710), `self.content_hash` (683), `self.file_size` (682), `self.content_type` (672), `self.is_private` (671). `self.file_url` is typically still empty/old.
  - **Required post-conditions:** the hook MUST set `self.file_url` itself, because core's `save_file_on_filesystem` (which normally sets it, 719-722) is skipped. Downstream `validate()` will then run `validate_file_path` (204), `validate_file_url` (215) and `validate_file_on_disk` (358) against that URL. The only URL shapes that bypass those disk checks are `http://`, `https://`, `/api/method/` (via `is_remote_file`, 81-84, and the early-return at 362-363 / 216).
  - **Return value:** propagated up as `save_file`'s return; `before_insert` (111) ignores it. Returning `None` is harmless; returning the core-style `{"file_name":..., "file_url":...}` dict is conventional.
  - **No hook registered (`None`)** → local filesystem write.
- **Call site B (legacy, different contract):** `frappe/utils/file_manager.py:165-167`
  ```python
  write_file_method = get_hook_method("write_file", fallback=save_file_on_filesystem)
  file_data = write_file_method(fname, content, content_type=content_type, is_private=is_private)
  file_data = copy(file_data)
  ```
  Here the hook is called as `(fname: str, content: bytes, content_type=..., is_private=...)` and **must return a dict** containing at least the keys listed by the `write_file_keys` hook (`frappe/hooks.py:70` → `["file_url", "file_name"]`); that dict is then extended into a File doc (169-189). `get_file_data_from_hash` (192-198) reconstructs the same key set from an existing File for dedup hits. **A single `write_file` implementation therefore has to be signature-polymorphic** if any installed app still calls `frappe.utils.file_manager.save_file`.

### 2.3 `delete_file_data_content`
- **Call site A:** `frappe/core/doctype/file/file.py:743-747`
  ```python
  method = get_hook_method("delete_file_data_content")
  if method:
      method(self, only_thumbnail=only_thumbnail)
  else:
      self.delete_file_from_filesystem(only_thumbnail=only_thumbnail)
  ```
  Arguments: `(file_doc, only_thumbnail=bool)`. Return value ignored. **If a hook is registered, core never deletes the local file** — the hook owns both local and remote cleanup.
- **Call site B (legacy):** `frappe/utils/file_manager.py:295-297` — `get_hook_method("delete_file_data_content", fallback=delete_file_from_filesystem)`, same `(doc, only_thumbnail=...)` shape.
- Reached only via `_delete_file_on_disk` (511), which already applied the content-hash refcount gate.

### 2.4 Non-hook extension surfaces touching File
- `permission_query_conditions["File"] = "frappe.core.doctype.file.file.get_permission_query_conditions"` (`frappe/hooks.py:116`); `has_permission["File"] = "frappe.core.doctype.file.file.has_permission"` (`frappe/hooks.py:132`). These are dict-hooks; an app adding its own entry produces a **list** for the key and framework resolution picks one — do not assume additive semantics.
- `doc_events["*"]["on_update"]` includes `frappe.core.doctype.file.utils.attach_files_to_document` (`frappe/hooks.py:156`) and the same in `on_update_after_submit` (`frappe/hooks.py:177`).
- `override_whitelisted_methods` remaps the legacy File RPCs (`frappe/hooks.py:392-401`), incl. `"frappe.utils.file_manager.download_file": "download_file"` and `"frappe.core.doctype.file.file.download_file": "download_file"`.
- **No hook exists** around private-file serving, thumbnailing, or `get_content` in this revision.

### 2.5 `override_doctype_class` mechanics + single-override limitation
`/home/user/v15/apps/frappe/frappe/model/base_document.py:76-110` (`import_controller`):
```python
class_overrides = frappe.get_hooks("override_doctype_class")
if class_overrides and class_overrides.get(doctype):
    import_path = class_overrides[doctype][-1]      # LAST entry wins
    module_path, classname = import_path.rsplit(".", 1)
    module = frappe.get_module(module_path)
else:
    module = load_doctype_module(doctype, module_name)
    classname = doctype.replace(" ", "").replace("-", "")
```
- Resolution: `class_ = getattr(module, classname, None)`, `ImportError` if missing (99-105); must be a `BaseDocument` subclass else `ImportError` (107-108).
- **Cached per site** in `frappe.controllers[site][doctype]` (`base_document.py:69-73`), bypassed only when `frappe.local.dev_server` or `frappe.flags.in_migrate` (66-67).
- **Single-override limitation:** `[-1]` picks exactly one import path (the last app in installed-app order that declares it). Two apps overriding `File` ⇒ one silently loses; there is **no MRO chaining**. The winner must `class MyFile(File)` and `super()` explicitly, and it must import the *current* base — which itself may be another app's override, producing an ordering-dependent, non-composable stack. Note also the asymmetry with `get_hook_method`, which takes `[0]` (first) while class overrides take `[-1]` (last).
- The existing fork does **not** use it: `/home/user/v15/apps/cloud_file_storage/frappe_s3_attachment/hooks.py:89-94` registers only `doc_events["File"] = {"after_insert": ..., "on_trash": ...}` — i.e. it uploads *after* the local write already happened. The DFP reference app instead overrides the class and reimplements `get_content` (`/home/user/v15/apps/dfp_external_storage_reference/dfp_external_storage/dfp_external_storage/doctype/dfp_external_storage/dfp_external_storage.py:577`, signature `def get_content(self) -> bytes:`).

---

## 3. UPLOAD ENDPOINTS

**Primary endpoint:** `@frappe.whitelist(allow_guest=True) def upload_file()` — `/home/user/v15/apps/frappe/frappe/handler.py:171`. Routed at `/api/method/upload_file` both via generic `/api/method/<name>` and explicitly in v2: `Rule("/method/upload_file", endpoint=upload_file)` (`/home/user/v15/apps/frappe/frappe/api/v2.py:192`, import at :20).

Flow:
1. Guest handling: if `frappe.session.user == "Guest"` → allowed only if System Settings `allow_guests_to_upload_files`, and then `ignore_permissions = True` (172-179).
2. Reads `frappe.form_dict`: `is_private`, `doctype`, `docname`, `fieldname`, `file_url`, `folder` (default `"Home"`), `method`, `file_name`, `optimize` (182-192).
3. `library_file_name` branch (194-204): copies `is_private`/`file_url`/`file_name` from an existing File after `frappe.has_permission("File", doc=library_file, throw=True)`.
4. `check_write_permission(doctype, docname)` (206-207, defined 252-268): permission is checked on the **target document**, not on File; missing doc (`new-xxx` names) falls back to `frappe.new_doc(doctype).check_permission("write")` (263-265).
5. Multipart read: `content = file.stream.read()` — **entire body into RAM** (209-212); `filename = file.filename`.
6. Optional server-side image optimization when `optimize` and mimetype starts with `image/` → `optimize_image(content=..., content_type=..., max_width, max_height)` (215-221).
7. Guest/desk-less MIME allowlist: `ALLOWED_MIMETYPES` (`handler.py:28`, checked 227-230).
8. `method` escape hatch: `frappe.get_attr(method)` + `is_whitelisted(method)` then `return method()` (232-235) — reads `frappe.local.uploaded_file`, `uploaded_filename`, `uploaded_file_url` set at 223-225.
9. Otherwise constructs the File doc and `.save(ignore_permissions=ignore_permissions)` (237-249) with `"is_private": cint(is_private)` (246) and `"content": content` (247).

**Request size cap:** `/home/user/v15/apps/frappe/frappe/app.py:199-204` — for `/api/method/upload_file` `request.max_content_length = get_max_file_size()`; otherwise `cint(conf.max_file_size) or 25MB`. Business-level cap: `frappe/core/api/file.py:85-91` `get_max_file_size()` = `System Settings.max_file_size * 1MB` or `conf.max_file_size` or 25 MB. Enforced again in `File.check_max_file_size` (`file.py:728-740`, throws `MaxFileSizeReachedError`).

**Legacy endpoints (still live):** `uploadfile()` `@frappe.whitelist()` `handler.py:129`; `frappe.utils.file_manager.upload()` (`file_manager.py:42-71`) → `get_file_doc` (74-95) → base64 `filedata` path `save_uploaded` (98-103) or `save_url` (106-132); `frappe.utils.file_manager.save_file(fname, content, dt, dn, folder=None, decode=False, is_private=0, df=None)` (148-189); `add_attachments(doctype, name, attachments)` (401-419).

**is_private decision order (authoritative):** form value → `cint(is_private)` in the doc dict (`handler.py:246`) → overridden by `File.set_is_private()` from the URL prefix if `file_url` is present (`file.py:99, 782-784`) → coerced `cint` in `save_file` (`file.py:671`). For content uploads with no `file_url`, the form value stands.

**Filename sanitization:** `set_file_name` strips all `/` (`file.py:422`); `save_file_on_filesystem` builds the URL from `re.sub(r"[/\\%?#]", "_", self.file_name)` (718) — **the URL is sanitized but `self.file_name` retains `%?#`**; `get_full_path` throws if `os.path.sep in self.file_name` (624-625); `is_safe_path` containment check (621-622, `frappe/utils/file_manager.py:422-431`).

**Duplicate-name handling — there is NO `-1`/`-2` suffixing in this revision.** `/home/user/v15/apps/frappe/frappe/core/doctype/file/utils.py:193-217`:
```python
def generate_file_name(name: str, suffix: str | None = None, is_private: bool = False) -> str:
    def path_exists(name, is_private): return os.path.exists(encode(get_files_path(name, is_private=is_private)))
    if not path_exists(name, is_private): return name
    candidate_path = get_file_name(name, suffix)
    if path_exists(candidate_path, is_private): return generate_file_name(name, is_private=is_private)
    return candidate_path
```
`get_file_name(fname, optional_suffix=None)` (211-217) inserts `suffix` before the extension, defaulting to `frappe.generate_hash(length=6)`. Called from `file.py:706-710` with `suffix=self.content_hash[-6:]`. **Collision detection is purely `os.path.exists` on the local disk** — in an S3-only mode with no local files this check degenerates to "never collides". The legacy variant `frappe/utils/file_manager.py:386-398` additionally queries `frappe.get_all("File", {"file_name": fname})`.

---

## 4. SERVING

### `/private/files/*`
Hard-wired in the WSGI dispatcher, **before** the website router:
`/home/user/v15/apps/frappe/frappe/app.py:125-126`
```python
elif request.path.startswith("/private/files/"):
    response = frappe.utils.response.download_private_file(request.path)
```
`/home/user/v15/apps/frappe/frappe/utils/response.py:267-279`:
```python
def download_private_file(path: str) -> Response:
    """Checks permissions and sends back private file"""
    from frappe.core.doctype.file.utils import find_file_by_url
    if frappe.session.user == "Guest":
        raise Forbidden(_("You don't have permission to access this file"))
    file = find_file_by_url(path, name=frappe.form_dict.fid)
    if not file:
        raise Forbidden(_("You don't have permission to access this file"))
    make_access_log(doctype="File", document=file.name, file_type=os.path.splitext(path)[-1][1:])
    return send_private_file(path.split("/private", 1)[1])
```
- **The permission check lives in `find_file_by_url`** — `/home/user/v15/apps/frappe/frappe/core/doctype/file/utils.py:430-443`: loads *all* File rows with that `file_url` (optionally narrowed by `name` = the `fid` query param) and returns the first whose `file.is_downloadable()` is True (`file.py:757-758` → `has_permission(self, "read")`, `file.py:843`). Multiple File rows sharing one URL ⇒ access granted if **any one** is readable (documented at utils.py:437-439).
- `fid` comes from `File.unique_url` (`file.py:810-818`): `file_url + "?" + urlencode({"fid": self.name})` for private files — this is the fast path that avoids the full-table scan.
- `send_private_file(path)` (`response.py:282-311`) has two modes:
  - **X-Accel:** only if the request carries header `X-Use-X-Accel-Redirect` (286) → empty body with `X-Accel-Redirect: /protected/<private_path>/<...>`, `Cache-Control: private,max-age=3600,stale-while-revalidate=86400`, `Accept-Ranges: bytes`, guessed Content-Type (287-292). The internal prefix is `/protected/` and honours `conf.private_path` (283).
  - **Direct:** `werkzeug.utils.send_file(filepath, environ=..., conditional=True, as_attachment=..., download_name=...)` (303-309), `NotFound` if the path is missing (296-297); `.svg/.html/.htm/.xml` are forced to `as_attachment` (299-301).
- Backups use the same primitive: `download_backup(path)` (`response.py:255-264`) after `frappe.only_for(("System Manager", "Administrator"))`.

### `/files/*` (public)
- **Never reaches Python in production** — nginx serves `sites/<site>/public/files` directly (no nginx template ships in this repo/bench; `X-Use-X-Accel-Redirect` is set by that nginx config, `response.py:286` is its only consumer in the entire tree).
- **In `bench serve` / dev**, it is served by a WSGI middleware *outside* the Frappe app: `/home/user/v15/apps/frappe/frappe/app.py:531` `application = StaticDataMiddleware(application, {"/files": str(os.path.abspath(_sites_path))})` (enabled at 498-499 unless `NO_STATICS`), implemented at `/home/user/v15/apps/frappe/frappe/middlewares.py:13-29` — resolves `<sites_path>/<site>/public/files/<path>`, raising `NotFound` on traversal or non-file. **No permission check, no session, no DB.**
- `StaticPage` renderer (`frappe/website/page_renderers/static_page.py:26-60`) only serves binaries from app `www/` folders — it never handles `/files/`.

### Programmatic download (works for both, incl. Guest)
`@frappe.whitelist(allow_guest=True) def download_file(file_url: str)` — `/home/user/v15/apps/frappe/frappe/handler.py:271-287`: `find_file_by_url(file_url)` → `raise frappe.PermissionError` if none → `frappe.local.response.filecontent = file.get_content()`, `type = "download"`.

### What a redirect-to-signed-URL override must replace
1. **`frappe.utils.response.download_private_file`** — invoked by a *direct module attribute lookup* at `app.py:126`; it is **not** hookable. Because `app.py` does `import frappe.utils.response` (app.py:23) and calls `frappe.utils.response.download_private_file(...)` (attribute access at call time), monkey-patching the module attribute at import/`before_request` time does take effect.
2. **Alternative interception without patching:** `before_request` hooks run inside `init_request` (`app.py:210-211`), which is called at `app.py:105` — i.e. **before** the `/private/files/` dispatch at 125. `app.py:134-135` does `except HTTPException as e: return e`, so a `before_request` hook may raise any `werkzeug.exceptions.HTTPException` (including `werkzeug.routing.RequestRedirect`, a 308-carrying HTTPException) to short-circuit with a redirect. Caveat: this runs before `validate_auth()` completes? No — `validate_auth()` is at 107, *after* `init_request`; so `frappe.session` is established by `HTTPRequest()` at `app.py:207-208` (inside `init_request`, before the hooks at 210) — session/user IS available to `before_request` hooks.
3. **The permission gate to preserve** is `find_file_by_url` → `File.is_downloadable()` → `has_permission` (utils.py:430-443, file.py:757, 843-875) plus the Guest rejection (`response.py:271-272`) and `make_access_log` (278).
4. **Public `/files/*` cannot be intercepted in Python at all** under nginx or dev statics — legacy public URLs must be handled by rewriting `File.file_url` values, or at the nginx layer, not by a Frappe hook.

---

## 5. CONTENT HASH & DEDUP

- **Algorithm/location:** `frappe/core/doctype/file/utils.py:187-190` — `hashlib.md5(content, usedforsecurity=False).hexdigest()`. Duplicated legacy copy at `frappe/utils/file_manager.py:380-383`.
- **Computed at:** `File.save_file` line 683 (`self.content_hash = get_content_hash(self._content)`), i.e. only when bytes are present. Retro-fit path: `generate_content_hash()` (`file.py:424-433`) reads the file **off local disk** (`open(get_files_path(file_name, is_private=...), "rb")`) and throws if absent — invoked lazily by `validate_duplicate_entry` (386-387).
- **Uniqueness: NOT enforced.** `content_hash` is a plain read-only Data field in `/home/user/v15/apps/frappe/frappe/core/doctype/file/file.json:157-161` with no `"unique": 1` anywhere in that JSON. Indexes created by `on_doctype_update` (`file.py:838-840`) are `(attached_to_doctype, attached_to_name)` and `file_url(100)` — **there is no index on `content_hash`**, yet it is queried on every insert (688) and every delete (513). At 1.2 M rows this is a full scan per operation.
- **Dedup on upload (two independent passes):**
  1. `save_file` 686-702: `frappe.get_value("File", {"content_hash": ..., "is_private": ...}, ["file_url","name"])`; if that doc `exists_on_disk()` → adopt its `file_url`, set `file_exists = True` ⇒ **no write, no `before_write_file`, no `write_file` hook call**. Suppressible via `ignore_existing_file_check=True`.
  2. `validate_duplicate_entry` 384-412 (post-write): same hash + `is_private`, additionally scoped by `attached_to_doctype`/`attached_to_name` when set (399-405), `name != self.name` (396-397); if the duplicate `exists_on_disk()` → `self.file_url = duplicate_file.file_url` (411-412). Skipped when `self.flags.ignore_duplicate_entry_error`.
  ⇒ **N File rows legitimately share one physical path already** — the core model is a de-facto refcount by `content_hash`. `_delete_file_on_disk` (511-526) is the matching refcount check.
- **`update_existing_file_docs`** (`utils.py:301-310`): on privacy flip, a bulk `UPDATE tabFile SET file_url=..., is_private=... WHERE content_hash=<hash> AND name!=<self>` — a hash collision or a shared hash across unrelated docs rewrites all of them.
- **Amend:** `Document.copy_attachments_from_amended_from` (`/home/user/v15/apps/frappe/frappe/model/document.py:446-464`, called from 329-331 when `amended_from` is set) creates a **new File row per attachment** carrying the *same* `file_url`/`is_private` and no `content` ⇒ `is_remote_file` is False (file_url starts with `/files/`, `file.py:82-83`), so `before_insert` → `save_file(content=self.get_content())` **re-reads the bytes from disk**, recomputes the hash, hits the dedup branch and reuses the URL without writing. Under an S3 backend `get_content()` must therefore work for a doc whose only identity is `file_url`.
- **Copy/duplicate of documents** goes through the same `attach_files_to_document` path (`utils.py:313-374`) and `relink_files`/`relink_mismatched_files` (`utils.py:377-419`, invoked at `document.py:333`) which only rewrite `attached_to_*`, never bytes.

---

## 6. `get_content` EXACT CONTRACT

`/home/user/v15/apps/frappe/frappe/core/doctype/file/file.py:566-592` — **verbatim**:
```python
def get_content(self) -> bytes:
	if self.is_folder:
		frappe.throw(_("Cannot get file contents of a Folder"))

	if self.get("content"):
		self._content = self.content
		if self.decode:
			self._content = decode_file_content(self._content)
			self.decode = False
		# self.content = None # TODO: This needs to happen; make it happen somehow
		return self._content

	if self.file_url:
		self.validate_file_url()
	file_path = self.get_full_path()

	# read the file
	with open(file_path, mode="rb") as f:
		self._content = f.read()
		try:
			# for plain text files
			self._content = self._content.decode()
		except UnicodeDecodeError:
			# for .png, .jpg, etc
			pass

	return self._content
```
- **Signature takes NO parameters** — there is no `encodings` kwarg in this revision (grep for `encodings` under `frappe/core/doctype/file/` and `frappe/utils/file_manager.py` returns nothing).
- **Return type lies:** annotated `-> bytes` but it returns **`str` whenever the bytes decode as UTF-8** (587) and `bytes` otherwise (588-590). It also returns whatever `self.content` holds if set (570-576) — which may be `str`, `bytes`, or base64 depending on `decode`.
- **Side effect:** always sets `self._content`.
- **Always reads local disk** via `open(self.get_full_path())` — a remote (`http(s)://`, `/api/method/`) `file_url` will make `get_full_path` return that URL unchanged (615-616) and `open()` will raise `FileNotFoundError`.
- Core consumers that depend on the str-when-decodable behaviour: `frappe/utils/csvutils.py:31` → `read_csv_content` (which re-checks `isinstance(fcontent, str)`, csvutils.py:39-40); `frappe/core/doctype/data_import/importer.py:435`, `:598`; `frappe/recorder.py:409` (`json.loads`); `frappe/core/doctype/prepared_report/prepared_report.py:96-97` (`gzip.decompress` — **requires bytes**); `frappe/utils/pdf.py:291` (`base64.b64encode` — requires bytes); `frappe/email/email_body.py:251`, `frappe/email/doctype/email_queue/email_queue.py:393`; `frappe/handler.py:286`.

**⚠ India Compliance incompatibility already present in this bench:** `/home/user/v15/apps/india_compliance/india_compliance/gst_india/doctype/gst_return_log/gst_return_log.py:188` calls `file.get_content(encodings=[])`, and `/home/user/v15/apps/india_compliance/.../gst_return_log.py:55,98` call `get_content()` and feed the result to `get_decompressed_data(...)`. Against frappe 15.93's zero-arg `get_content`, line 188 raises `TypeError` today. **Any `File` override must accept `get_content(self, encodings: list | None = None, **kwargs)` and must return raw `bytes` when `encodings=[]`** (empty list ⇒ no decoding attempted), while preserving the legacy "decode if UTF-8" behaviour when called with no args. The DFP reference override does **not** do this — `/home/user/v15/apps/dfp_external_storage_reference/dfp_external_storage/dfp_external_storage/doctype/dfp_external_storage/dfp_external_storage.py:577` is `def get_content(self) -> bytes:` and delegates to `super().get_content()` (580) for non-S3 files, which is exactly the failure the mission says must not be repeated.

**Companions:**
- `def is_downloadable(self)` — `file.py:757-758`: `return has_permission(self, "read")` (module function at 843).
- `def get_full_path(self)` — `file.py:594-627`: uses `self.file_url or self.file_name`; strips own site URL when `/files/` present (599-601); bare names get `/private/files/` or `/files/` per `is_private` (603-607); maps to `get_files_path(..., is_private=1|0)` (609-613); `URL_PREFIXES` pass through **unchanged** (615-616); `frappe.throw` if unmapped and no `file_url` (618-619); then `is_safe_path` (621-622) and `os.path.sep in self.file_name` (624-625) guards. `get_files_path` = `get_site_path("private" if is_private else "public", "files", *path)` (`frappe/utils/__init__.py:549-550`).
- `def exists_on_disk(self)` — `file.py:563-564`: `return os.path.exists(self.get_full_path())`.

---

## 7. FILE PERMISSIONS

`/home/user/v15/apps/frappe/frappe/core/doctype/file/file.py:843-875` (registered at `frappe/hooks.py:132`):
```python
def has_permission(doc, ptype=None, user=None, debug=False):
	user = user or frappe.session.user
	if user == "Administrator": return True
	if ptype == "create": return frappe.has_permission("File", "create", user=user, debug=debug)
	if not doc.is_private and ptype in ("read", "select"): return True      # 852-853
	if user != "Guest" and doc.owner == user: return True                   # 855-856
	if doc.attached_to_doctype and doc.attached_to_name:                    # 858
		...ref_doc = frappe.get_doc(attached_to_doctype, attached_to_name)  # 863
		if ptype in ["write", "create", "delete"]:
			return ref_doc.has_permission("write", debug=debug, user=user)  # 870-871
		else:
			return ref_doc.has_permission("read", debug=debug, user=user)   # 872-873
	return False                                                            # 875
```
Cascade rules:
- **Public files are world-readable, including Guest** (852-853) — `ptype in ("read","select")`.
- Owner shortcut applies to any ptype except Guest (855-856).
- Attached files inherit the reference doc: read→read, write/create/delete→**write on the parent** (870-873). `ModuleNotFoundError`/`DoesNotExistError` ⇒ `False` (864-868).
- An **unattached private file owned by someone else is inaccessible** (875) — even to System Managers via this function.
- Guests are additionally blocked from `/private/files/*` at the transport layer (`response.py:271-272`), *before* `has_permission` runs.

`get_permission_query_conditions(user=None)` — `file.py:878-891` (registered `frappe/hooks.py:116`):
- Administrator ⇒ `""`.
- **Non-System-User** (`SYSTEM_USER_ROLE not in frappe.get_roles(user)`) ⇒ `` `tabFile`.`owner` = <user> `` only (883-884).
- System users ⇒ `is_private = 0` OR (`attached_to_doctype IS NULL` AND owner=user) OR `attached_to_doctype IN (<get_doctypes_with_read()>)` (886-891). Note this is a **doctype-level** grant — row-level permissions of the parent are not applied in list view.

Related gate on delete: `validate_protected_file` (`file.py:481-509`) — for submitted parents with `meta.protect_attached_files`, deletion throws unless the parent is cancelled and the user has `delete` on it.

---

## 8. ATTACH FIELDS & IMAGES

- Fieldtypes `"Attach"`/`"Attach Image"` are declared in `/home/user/v15/apps/frappe/frappe/model/__init__.py:32-33` and `:49-50`; `Meta.get_image_fields()` = `self.get("fields", {"fieldtype": "Attach Image"})` (`frappe/model/meta.py:197`).
- **No server-side URL-shape validation on the field value.** `Attach`/`Attach Image` are explicitly **excluded from HTML sanitization** (`/home/user/v15/apps/frappe/frappe/model/base_document.py:1111-1119`: `df.get("fieldtype") in ("Attach", "Attach Image", "Barcode", "Code") → continue`). No regex/protocol allowlist exists for these values in the model layer.
- **Yes — `file_url` can hold an absolute `https://` URL.** `File.is_remote_file` (`file.py:80-84`) treats `http://`, `https://`, `/api/method/` (`URL_PREFIXES`, line 40) as remote; `before_insert` then takes the `validate_remote_file()` branch (108-109) and **skips content write, hashing and disk validation entirely**; `validate_file_url` early-returns for remote (216); `validate_file_path` early-returns (205-206); `validate_file_on_disk` returns True for `URL_PREFIXES` (362-363); `is_safe_path` returns True for `http(s)://` (`frappe/utils/file_manager.py:423-424`). The web-link uploader path posts such URLs (`frappe/public/js/frappe/file_uploader/FileUploader.vue:528-537`, sent as `file_url` at :644-645). `validate_file_url`'s error message ("URL must start with http:// or https://", `file.py:222`) only fires for non-remote URLs not starting with `/files/` or `/private/files/`.
  ⇒ **`/api/method/...` and absolute HTTPS URLs are the built-in escape hatches for an S3-backed `file_url`** — everything disk-related self-disables.
- **`attach_files_to_document`** (`frappe/core/doctype/file/utils.py:313-374`, wired at `frappe/hooks.py:156` and `:177`) runs on `on_update` of **every** doctype: for each Attach/Attach Image field it **skips values not starting with `/files` or `/private/files`** (326-327) — so absolute-URL attach values are never auto-linked to a File row; otherwise it reuses an unattached File (340-361) or inserts a new one with `folder="Home/Attachments"` (363-374, errors swallowed into `doc.log_error`).
- **Thumbnails:** `File.make_thumbnail(set_as_thumbnail=True, width=300, height=300, suffix="small", crop=False)` (`file.py:435-474`). Local branch `get_local_image(self.file_url)` opens `sites/<site>/{public|<private path>}` directly (`utils.py:86-115`); remote branch `get_web_image` HTTP-GETs (`utils.py:118-150`). Output is **always written to the public folder**: `path = os.path.abspath(frappe.get_site_path("public", thumbnail_url.lstrip("/")))` then `image.save(path)` (463-466) and `self.db_set("thumbnail_url", ...)` (467-468) — **a private file's thumbnail lands in public/**. `make_thumbnail` is **never called anywhere in frappe core** in this revision (no Python, JS or JSON reference outside its own definition and the `thumbnail_url` deletes at `file.py:752,755` / `file_manager.py:303,306`) — it exists purely for apps. There is **no background thumbnail job** in core.
- **optimize_image flow:** `/home/user/v15/apps/frappe/frappe/utils/image.py:50` — `def optimize_image(content, content_type, max_width=1024, max_height=768, optimize=True, quality=85)`; returns original bytes for `image/svg+xml` (51-52) or if the "optimized" output is larger (74); swallows failures with `msgprint` (75-77). Entry points: upload-time (`handler.py:215-221`), rich-text data-URL extraction (`utils.py:250`), and `File.optimize_file()` (`file.py:786-808`) — a whitelisted method that calls `self.get_content()` (801), then `self.save_file(content=optimized_content, overwrite=True)` (807) **with `overwrite=True`, i.e. no `generate_file_name` rename → same path/URL, new bytes, new `content_hash`** — then `self.save()` (808). `strip_exif_data` similarly rewrites content pre-write (`file.py:675-680`).

---

## 9. URL/REDIRECT PRIMITIVES

- **`website_redirects` hook** — consumed in `/home/user/v15/apps/frappe/frappe/website/path_resolver.py:117`; core examples at `/home/user/v15/apps/frappe/frappe/hooks.py:60-65`. Entry shape: `{"source": <regex>, "target": <repl>, "redirect_http_status": <int>, "match_with_query_string": <bool>}`.
- **`Website Route Redirect` doctype** — merged with the hook list: `redirects += frappe.get_all("Website Route Redirect", ["source","target","redirect_http_status"], order_by=None)` (`path_resolver.py:118-120`).
- **Matching semantics** (`path_resolver.py:99-152`): pattern = `rule["source"].strip("/ ") + "$"`, matched against the path **with leading/trailing slashes stripped** (`PathResolver.__init__`, `self.path = path.strip("/ ")`, line 24); `re.sub` builds the target (146); default status 301 (148); result cached in `frappe.cache.hget/hset("website_redirects", path_to_match, {...})` (125-131, 149-151); raises `frappe.Redirect(status_code)` → caught in `PathResolver.resolve` (37-40) returning a `RedirectPage`.
- **Hard limitation for this project:** `resolve_redirect` is only reached from `PathResolver.resolve`, which is only reached from `frappe.website.serve.get_response` — i.e. the `elif request.method in ("GET","HEAD","POST"): response = get_response()` branch at `app.py:128-129`. **`/private/files/*` is intercepted earlier at `app.py:125-126` and never sees the redirect engine**; `/files/*` never reaches Python at all under nginx/dev-statics. `website_redirects` is therefore useless for aliasing legacy file URLs.
- **`before_request` hook** — `app.py:210-211`, `for before_request_task in frappe.get_hooks("before_request"): frappe.call(before_request_task)`, executed inside `init_request` (called at `app.py:105`) **after** `make_form_dict` (205) and `HTTPRequest()`/session setup (207-208) and **before** the `/private/files/` dispatch (125). Core entries: `frappe/hooks.py:437-441`. Combined with `except HTTPException as e: return e` (`app.py:134-135`, `HTTPException` imported at app.py:10), this is the **only in-Python interception point for `/files/` and `/private/files/` that does not require monkey-patching** — raise a `werkzeug` redirect exception from a `before_request` hook.
- `after_request` hooks run post-response with `response=`/`request=` kwargs (`app.py:163-168`, `frappe/hooks.py:443-445`) — too late to alter routing.
- `website_path_resolver` and `page_renderer` hooks (`path_resolver.py:42-44`, `78-96`) exist but sit downstream of the file dispatch.
- `override_whitelisted_methods` (`frappe/hooks.py:392-401`) can re-point `download_file`/`unzip_file`/etc. to app implementations.

---

## 10. ZIP/UNZIP + PREPARED REPORT

**`File.unzip(self) -> list["File"]`** — `file.py:528-561`:
- Throws unless `self.file_url.endswith(".zip")` (530-531).
- `zip_path = self.get_full_path()` (533) then `zipfile.ZipFile(zip_path)` — **requires a real local path**; a remote/S3 URL breaks here.
- Skips directories and `__MACOSX/` (538-540) and dot-files (543-545); reads each member into memory `file_doc.content = z.read(file.filename)` (549) and creates a new File inheriting `folder`, `is_private`, `attached_to_doctype`, `attached_to_name` (552-557); finally `frappe.delete_doc("File", self.name)` (560) — **the zip File row (and, if unshared, its bytes) is destroyed after extraction**.
- Whitelisted entry point: `frappe/core/api/file.py:9-13` `unzip_file(name: str)`.

**`File.zip_files(files)`** — `@staticmethod`, `file.py:820-835`: builds an in-memory `zipfile.ZipFile` over `_file.get_content()` (833), skipping folders and files failing `has_permission(_file, "read")` (829-832). Exposed at `frappe/core/api/file.py:119-124` (`frappe.response["filecontent"]`, `type="download"`).

**Prepared Report:**
- Write: `create_json_gz_file(data, dt, dn, report_name)` — `/home/user/v15/apps/frappe/frappe/core/doctype/prepared_report/prepared_report.py:237-257`. Filename `"{scrubbed_report}_{Y-m-d-H-M}.json.gz"` (240-242); `gzip.compress(frappe.safe_encode(frappe.as_json(...)))` (243-244); creates a File doc with `content=compressed_content`, `is_private: 1`, attached to the Prepared Report, `_file.save(ignore_permissions=True)` (247-257). Called from `generate_report` (`:291` region, invoked at line ~291 within `generate_report`), which itself runs on the **`long` RQ queue** via `after_insert` → `enqueue(generate_report, queue="long", timeout=..., enqueue_after_commit=True)` (`prepared_report.py:75-83`).
- Read: `get_prepared_data(self, with_file_name=False)` (`prepared_report.py:85-97`) — picks the attachment whose `file_url.endswith(".gz")` (88-91), `frappe.get_doc("File", attachment.name)` (93), then **`gzip.decompress(attached_file.get_content())`** (96-97). **This is a hard raw-bytes dependency**: if `get_content()` returns `str`, `gzip.decompress` raises `TypeError`. (Gzip bytes essentially never decode as UTF-8, so the current heuristic happens to work — but any override that eagerly decodes or re-encodes will break every Prepared Report.)
- Download: `download_attachment(dn)` (`prepared_report.py:260-269`) → `frappe.local.response.filecontent = data`, `type="binary"`.
- Deletion: `delete_prepared_reports` (`:230-235`) with `delete_permanently=True`; batched cleanup enqueued at `:59-61`.

---

## 11. RISK NOTES (S3-offloading hazards in THIS revision)

1. **`validate_file_on_disk` runs on every `File.validate()`** — `file.py:137, 358-366`: `if not os.path.exists(full_path): frappe.throw(_("File {0} does not exist"), IOError)`. In `S3_ONLY` mode every `File.save()` (including `db_set`-free saves, folder-size recalcs at `frappe/core/api/file.py:115-116`, `setup_folder_path` at `utils.py:43-51`, and the amend copy at `document.py:453-464`) will throw unless `is_remote_file` is True or this method is overridden.
2. **`validate_file_path` requires realpath containment under the site's files dir** — `file.py:204-213`. Any `file_url` shape other than `/files/…`, `/private/files/…`, `http(s)://…`, `/api/method/…` is rejected.
3. **`get_content` opens a local path unconditionally** (`file.py:583`) and returns `str`-or-`bytes` (587-590). Overriding it is mandatory, and the override must satisfy `gzip.decompress` (prepared_report.py:96), `base64.b64encode` (`frappe/utils/pdf.py:291`), `json.loads` (`frappe/recorder.py:409`), CSV str-handling (`frappe/utils/csvutils.py:31`), **and** India Compliance's `get_content(encodings=[])` (`/home/user/v15/apps/india_compliance/.../gst_return_log.py:188`).
4. **`read_xlsx_file_from_attached_file` bypasses `get_content` entirely**: `/home/user/v15/apps/frappe/frappe/utils/xlsxutils.py:104-106` does `_file = frappe.get_doc("File", {"file_url": file_url}); filename = _file.get_full_path()` and hands the **path** to `openpyxl.load_workbook`. An S3-only file yields a URL string here → hard failure. (`get_full_path` returning a `http(s)://` URL passes through untouched, `file.py:615-616`.)
5. **`unzip` uses `get_full_path()` as a filesystem path** (`file.py:533`).
6. **`handle_is_private_changed` does `shutil.move` between local dirs** (`file.py:226-270`, move at 265) and then bulk-rewrites every other File row with the same `content_hash` (`update_existing_file_docs`, `utils.py:301-310`). Toggling `is_private` on an S3-backed file will raise `FileNotFoundError` at 253-257 or silently desynchronise DB↔S3.
7. **`on_rollback` writes to disk** — `file.py:163-195`: re-opens `self.get_full_path()` in `w+`/`wb+` (185-188) and `shutil.move`s paths (191-195); registered via `frappe.db.after_rollback.add(...)` at 113, 267, 643. Also `save_file` pre-loads `self.flags.original_content = self.get_content()` on every update (657-658) — **a full extra object download per save** under S3, held in memory.
8. **Dedup and refcount both scan `content_hash` with no index** (`file.py:688`, `file.py:513-522`; indexes at 838-840 cover only `attached_to_*` and `file_url(100)`). At 1.2 M rows the migration engine will generate 1.2 M full scans unless an index is added.
9. **Refcount deliberately ignores `is_private`** (`file.py:518-519` comment) — two rows with the same hash but different privacy protect each other from deletion, and `update_existing_file_docs` can flip both. Any "one physical object, many File rows" abstraction must reconcile with this pre-existing, subtly-wrong semantics rather than assume clean refcounts.
10. **`generate_file_name` collision detection is `os.path.exists` only** (`utils.py:198-208`). With no local disk, distinct uploads with identical names produce identical object keys → silent overwrite in S3.
11. **Thumbnails always land in `public/`** (`file.py:463`) even for private files, and `delete_file` (`utils.py:153-170`) derives the delete path from the URL's first segment — an S3-hosted `thumbnail_url` would map to a bogus local path.
12. **Whole-file buffering in RAM**: `handler.py:211` (`file.stream.read()`), `file.py:639-641`, `file.py:658`, `File.zip_files` (821-835). ~100 GB migration must not reuse these paths.
13. **`frappe.form_dict` leakage into the model**: `self.file_size = frappe.form_dict.file_size or self.file_size` (`file.py:138`) — background migration jobs that construct a request-like context could corrupt `file_size`.
14. **Serving is not hookable**: `app.py:125-126` hard-calls `frappe.utils.response.download_private_file`; `/files/*` is served by nginx or `StaticDataMiddleware` (`app.py:531`, `middlewares.py:13-29`) with **zero** Frappe involvement. Redirect-to-signed-URL requires either monkey-patching that module attribute or a `before_request` hook raising an `HTTPException` (`app.py:210-211`, `134-135`).
15. **Existing fork uses the wrong seam**: `/home/user/v15/apps/cloud_file_storage/frappe_s3_attachment/hooks.py:89-94` hooks `File.after_insert`/`on_trash` — the local write has already occurred by then (`before_insert` → `save_file`, `file.py:111`), so DUAL_WRITE is accidental but LOCAL_ONLY/S3_ONLY/refcount semantics are not expressible. The `write_file` + `delete_file_data_content` hooks (§2) are the correct seams; `override_doctype_class` is required for `get_content`/`get_full_path`/`exists_on_disk`/`validate_file_on_disk`, and it is single-winner (`base_document.py:91`, `[-1]`), conflicting with DFP if both are ever installed.
16. **`validate_file_extension` only fires inside an HTTP request** (`file.py:370`) — migration/background-created files bypass the allowlist, so a migration engine must not rely on it for safety.