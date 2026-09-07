> **DISPOSITION (P0.5): ALL FINDINGS CLOSED.** Every confirmed finding in this report
> was bound as a fix in `docs/adr/amendments-register.md` (round-1 reports → A1–A24;
> final-round reports → A25–A36) and carries a named regression test and phase. Nothing
> in this file is an OPEN issue. Paths like `scratchpad/design|research/...` refer to
> the planning workspace — the same documents now live at `docs/design/` and
> `docs/research/`.

# Adversarial Plan Review — Technical Reality Lens
**Target:** `(internal planning record)` + 3 design docs
**Verified against:** frappe v15.93.0 (`/home/user/v15/apps/frappe`), bench templates (`/home/user/.local/lib/python3.10/site-packages/bench/`), werkzeug in `/home/user/v15/env`

---

## [SEVERITY: BLOCKER] [CONFIRMED] Canonical `file_url` + S3-only content makes `File.before_insert` re-read bytes from local disk and raise `FileNotFoundError` — breaking amend, Attach-field attachment, and the legacy `file_manager.save_file` path the design cites as contract #12

- **Evidence:**
  - `frappe/core/doctype/file/file.py:97-112` — `before_insert` → `if self.is_remote_file: … else: self.save_file(content=self.get_content())` (line 111).
  - `frappe/core/doctype/file/file.py:81-83` — `is_remote_file` → `self.file_url.startswith(URL_PREFIXES)`; `file.py:40` — `URL_PREFIXES = ("http://", "https://", "/api/method/")`. **Canonical `/files/…` / `/private/files/…` URLs are NOT remote**, so the guard that protects the *current* fork (whose URLs are `/api/method/…`) disappears under ADR-1.
  - `frappe/core/doctype/file/file.py:566-592` — `get_content()` with no in-memory `content` → `file_path = self.get_full_path()` → `with open(file_path, mode="rb")`.
  - Three live callers create File rows from a URL with **no content and no `cloud_storage_object`**:
    - `frappe/model/document.py:446-464` `copy_attachments_from_amended_from` → `frappe.get_doc({... "file_url": attach_item.file_url ...})` → `_file.save()` (no try/except → **amend raises**).
    - `frappe/core/doctype/file/utils.py:363-374` `attach_files_to_document` → `frappe.get_doc(doctype="File", file_url=value, …).insert()` inside `except Exception: doc.log_error("Error Attaching File")` → **silently drops the attachment row**.
    - `frappe/utils/file_manager.py:161-190` — hook returns only `{file_url, file_name}` (filtered by `write_file_keys`, `frappe/hooks.py:70`); the resulting `frappe.get_doc(file_data).insert()` has no `cloud_storage_object`.
  - Design `runtime-storage.md §5`: `_read_bytes` order is `(1) local path (2) cache (3) cloud-backed → engine.get_object_bytes(cso) (4) raise`; `_is_cloud_backed()` is defined purely as "`self.cloud_storage_object` set". Design `§6.3` links the CSO in `after_insert` — but `frappe/model/document.py:303` runs `before_insert` and `:309` runs `validate` **before** the row is inserted, so `after_insert` can never rescue it.
  - Same failure re-fires one layer later at `validate()`: `file.py:121-137` calls `validate_file_path()` / `validate_file_on_disk()` (`file.py:358-366` throws `IOError`), and the design's overrides return early only when `_is_cloud_backed()` is true.
- **Why it breaks the plan:** In `S3_PRIMARY_LOCAL_FALLBACK` / `S3_ONLY` (the whole point of the app, and the post-cutover mode) amending any submitted document with an attachment throws, every Attach/Attach-Image field silently loses its File row, and the india_compliance `e_waybill.py:741` / erpnext `import_supplier_invoice.py:104` path (explicitly named as contract #12) fails. The plan's P2 gate lists `T-AMEND`, `T-ATTACH` but P2 only ships LOCAL_ONLY + DUAL_WRITE — where local bytes exist and the bug is invisible. It would surface first in P3/P4, after the CSO/hook architecture is frozen.
- **Suggested fix:** Make cloud resolution independent of the not-yet-written link. `CloudFile._resolve_cso()` fallback chain: (a) `self.cloud_storage_object`; (b) sibling `File` with identical `file_url` and a non-empty `cloud_storage_object`; (c) sibling with identical `content_hash` + `is_private`. Use it in `_is_cloud_backed`, `_read_bytes`, `exists_on_disk`, `validate_file_on_disk`, `get_full_path`. Additionally make `_write_file_legacy` stash the CSO name on `frappe.local` keyed by `content_hash` so `file_manager`'s insert can pick it up in `before_insert`. Add explicit S3_ONLY-mode tests for all three call sites to the P2 exit gate, not P3.

---

## [SEVERITY: MAJOR] [CONFIRMED] The dev-mode `StaticDataMiddleware` shim cannot work as designed — the loader closure is baked at middleware construction, and the middleware runs before any `before_request` hook

- **Evidence:**
  - `werkzeug/middleware/shared_data.py:116-134` — `self.exports: list[…] = []` … `loader = self.get_directory_loader(value)` (line 130) … `self.exports.append((key, loader))` (line 134). The loader is captured **once in `__init__`**.
  - `frappe/app.py:531` — `application = StaticDataMiddleware(application, {"/files": …})` inside `application_with_statics()`; the instance is built before any request.
  - `frappe/middlewares.py:22-27` — `raise NotFound` on miss (design §8.2 is right about this; **the plan's own §"Frappe v15.93 mechanics" line 138-139 says the opposite**: "in dev is served by StaticDataMiddleware (passes through on miss)").
  - Design `runtime-storage.md §8.2`: "`runtime_patches` also wraps `StaticDataMiddleware`'s loader (dev-only, gated on `frappe.local.dev_server`)" — and `runtime_patches.ensure_installed` is registered as a `before_request` hook (`§1.1`), which runs at `frappe/app.py:209-210`, i.e. *inside* the WSGI app the middleware wraps. A `/files` miss never reaches it.
- **Why it breaks the plan:** P3's acceptance criterion is "private patch + presign policy, **public renderer + dev shim**". As specified the shim is a no-op: patching `StaticDataMiddleware.get_directory_loader` after construction changes nothing, and even a correct patch can't be installed by a request that the middleware kills first. Every dev bench (and any Playwright smoke test in P7 run under `bench serve`) 404s S3-only public files.
- **Suggested fix:** Patch the **instance**, not the class: at first successful request, rewrite `frappe.app.application.exports` (walk the middleware chain for a `StaticDataMiddleware`) replacing the loader with one that returns `(None, None)` on miss (werkzeug `shared_data.py:249-250` then falls through to `self.app`). Better: drop the shim and register a `website_path_resolver`/renderer-independent short-circuit, and document dev 404 as an accepted degradation with the CI public-serving tests running under gunicorn, not `bench serve`.

---

## [SEVERITY: MAJOR] [CONFIRMED] The public `page_renderer` receives the *resolved endpoint*, not the request path — `.html` public files are unreachable, contradicting design §8.2's explicit claim to serve them

- **Evidence:**
  - `frappe/website/path_resolver.py:47` — `endpoint = resolve_path(self.path)`; `:68` — `renderer_instance = renderer(endpoint, self.http_status_code)`.
  - `frappe/website/path_resolver.py:155-160` — `def resolve_path(path): … if path.endswith(".html"): path = path[:-5]`. So `files/foo.html` → endpoint `files/foo`; the design's `can_render` lookup `frappe.db.get_value("File", {"file_url": "/" + path})` misses.
  - `frappe/website/path_resolver.py:47` also runs `resolve_from_map(path)` (`:173-179`) — dynamic website route rules can rewrite the endpoint before the renderer sees it.
  - Bench nginx template `nginx.conf:82-84` — `rewrite ^(.+)\.html$ $1 permanent;` sits in `location /`; the nested `location ~* ^/files/.*.(htm|html|svg|xml)` (`:86-89`) declares no rewrite-module directive, so it **inherits** the rewrite: `/files/foo.html` 301s to `/files/foo` before try_files ever runs.
  - Design `runtime-storage.md §8.2`: "`.htm/.html/.svg/.xml` always presigned-attachment (the nginx template forces attachment for these locally; we mirror it)."
  - Interface detail: `frappe/website/page_renderers/base_renderer.py:6-19` — `__init__(self, path=None, http_status_code=None)`, `can_render(self)` takes **no arguments**; design §8.2 writes `PublicFileRenderer.can_render(path)`.
- **Why it breaks the plan:** A documented serving case silently 404s, and the design's mental model ("the renderer sees the request path") is wrong in two other ways that will surface as intermittent misses once website route rules exist on the site. The `T-PUB` test would pass with `.pdf` fixtures and never catch it.
- **Suggested fix:** In `can_render`, use `frappe.local.request.path` (or `frappe.local.path`, set at `path_resolver.py:167` *before* the map lookup) rather than `self.path`; look up both `/{endpoint}` and `/{endpoint}.html`. State in the design that `.html` public objects also require an nginx snippet override, or explicitly declare `.html/.htm` public objects out of scope.

---

## [SEVERITY: MAJOR] [CONFIRMED] `website_404` negative cache short-circuits the custom renderer permanently in production

- **Evidence:**
  - `frappe/website/path_resolver.py:33-35` — `if request.url and can_cache() and frappe.cache.hget("website_404", request.url): return self.path, NotFoundPage(self.path)` — **before** `get_custom_page_renderers()` at `:56`.
  - `frappe/website/page_renderers/not_found_page.py:23-24` — `frappe.cache.hset("website_404", self.request_url, True)`.
  - Invalidation is only `frappe.cache.delete_value("website_404")` in `frappe/website/utils.py:388` (website cache clear).
  - `frappe/website/utils.py:52-59` — `can_cache()` returns **False** when `frappe.conf.developer_mode` — this bench has `"developer_mode": 1` (`/home/user/v15/sites/common_site_config.json`), so neither the dev bench nor a developer-mode CI site reproduces it.
- **Why it breaks the plan:** During the migration window (plan: 1-2 days with the site fully live), any public `/files/...` URL requested after its local copy is quarantined but before the renderer can answer — or requested at all while the object is still `uploaded` not `verified` — gets 404'd and the 404 is cached. After the campaign completes, those URLs stay broken until someone clears the website cache. The failure is invisible in every test environment the plan specifies.
- **Suggested fix:** Have the CLEANUP/VERIFY transitions and `Cloud Storage Settings.on_update` call `frappe.cache.delete_value("website_404")`; better, intercept `/files/*` before `PathResolver` entirely (a `before_request` short-circuit or `website_path_resolver` hook, `path_resolver.py:43-45`) so the negative cache is never consulted for file paths. Add a regression test that 404s a public file, uploads it, and asserts the second request 302s.

---

## [SEVERITY: MAJOR] [CONFIRMED] `handle_is_private_changed` replication is incomplete — `update_existing_file_docs` flips siblings' `is_private`/`file_url` but leaves their `cloud_storage_object` pointing at the old-visibility CSO

- **Evidence:**
  - `frappe/core/doctype/file/utils.py:301-310` — `update_existing_file_docs(doc)` sets `file_url` **and** `is_private` on **every** `tabFile` row with the same `content_hash` (`WHERE content_hash = doc.content_hash AND name != doc.name`). Nothing touches any other column.
  - `frappe/core/doctype/file/file.py:226-292` — core's flip; `update_existing_file_docs(self)` at `:280`.
  - Design `runtime-storage.md §5` (`handle_is_private_changed` row): "…`ensure_cso` for the target …, relink **`self.cloud_storage_object`**, decrement old CSO …; then replicate core's URL rewrite + parent `attached_to_field` propagation + `update_existing_file_docs` sibling rewrite … propagation preserved verbatim." Only `self` is relinked.
  - CSO identity is `(content_sha256, visibility, bucket)` (design §10) and the key encodes `pub/` vs `prv/` (§4).
- **Why it breaks the plan:** After a private→public flip on one File, its content-hash siblings claim `is_private=0` and `/files/...` while still linked to a `prv/…` CSO. Consequences, all confirmed by the design's own rules: (a) `PublicFileRenderer` redirects to `cdn_base_url + s3_key` — but §8.2 restricts the CloudFront distribution behavior to `pub/*`, so those siblings **403 from the CDN**; (b) the `→ S3_ONLY` transition gate and dedup identity are computed against a CSO whose `visibility` contradicts `File.is_private`; (c) neither CSO ever reaches refcount 0, so both leak past GC (safe direction, but permanent).
- **Suggested fix:** Replace the verbatim replication with a cloud-aware version: after computing the new CSO, run one `UPDATE tabFile SET file_url=…, is_private=…, cloud_storage_object=<new_cso> WHERE content_hash=… AND name != …`, then recount both CSOs under lock. Add a test that creates two File rows sharing one content hash, flips one, and asserts both rows' `cloud_storage_object.visibility` matches `is_private`.

---

## [SEVERITY: MAJOR] [CONFIRMED] Materialization write-back registered on `after_request` / `after_job` will silently lose its DB writes — neither hook site commits, and `after_request` swallows all exceptions

- **Evidence:**
  - `frappe/utils/background_jobs.py:257-269` — the `after_job` loop is in the `finally` block, **after** `frappe.db.commit()` in the `else:` branch (`:254-256`) or after `frappe.db.rollback()` in the `except` branches; it is immediately followed by `frappe.local.job.after_job.run()` and `frappe.destroy()` (`:270-271`). No commit follows the hook loop.
  - `frappe/app.py:145-153` and `:161-168` — `run_after_request_hooks` runs in `finally`, after `sync_database(rollback)`, and is wrapped in `try/except Exception: frappe.logger().error("Failed to run after request hook")` — every write-back failure is swallowed with no user-visible signal.
  - Design `runtime-storage.md §7`, write-back step 2: "`flush_request` (`after_request` hook) and `flush_job` (`after_job` hook) … relink `File.cloud_storage_object`, update `content_hash`, `file_size` via `db_set(update_modified=False)` → decrement old CSO → rewrite sidecar." This is the sole mechanism for contract #7/#8 (`bank_statement_import.py:202-212`, `stock_ledger.py:420-422`).
- **Why it breaks the plan:** Bytes get re-uploaded to S3 (a new CSO is created) but the `tabFile` relink is rolled back when the connection closes — leaving `File.cloud_storage_object` pointing at the *old* object while the sidecar says clean. The erpnext Repost Item Valuation write-through (T-RIV, the P4 gate) would appear to pass under MinIO if the test asserts S3 state, and silently regress on `File`. Worse, the exception swallow means the whole mechanism can be dead in production without a single Error Log.
- **Suggested fix:** `flush_request`/`flush_job` must end with an explicit `frappe.db.commit()` and wrap their own body in try/except + `frappe.log_error`. Prefer `frappe.local.request.after_response` / `frappe.local.job.after_job` CallbackManagers (`frappe/app.py:210`, `background_jobs.py:218`) only if you verify their commit semantics; otherwise commit explicitly. Add a test that mutates a materialized path inside a background job and asserts `File.cloud_storage_object` changed **after** the job process exits.

---

## [SEVERITY: MAJOR] [CONFIRMED] The external-install compat patch can never execute — `bench migrate` resolves `patches.txt` from `installed_apps`, which still names the vanished `frappe_s3_attachment` module

- **Evidence:**
  - `frappe/modules/patch_handler.py:84-92` — `for app in frappe.get_installed_apps(): patches.extend(get_patches_from_app(app, …))`; `:102` — `patches_file = frappe.get_app_path(app, "patches.txt")`.
  - `frappe/__init__.py:1541-1559` — `get_installed_apps(*, _ensure_on_bench=False)`; the default call in `get_all_patches` does **not** filter to apps present on bench, so a stale entry raises rather than being skipped.
  - Design `delivery-platform.md §1.4` registers the patch under `[pre_model_sync]` in `cloud_file_storage/patches.txt`, and lists "5. `installed_apps` global: read `frappe.db.get_global("installed_apps")`, replace element…" as **step 5 inside the patch**.
  - Verified fork state matching the legacy shape: `/home/user/v15/sites/apps.txt` lists `frappe_s3_attachment`; `env/.../frappe_s3_attachment.pth` → `/home/user/v15/apps/frappe_s3_attachment` (absent); package lives at `/home/user/v15/apps/cloud_file_storage/frappe_s3_attachment/`; `modules.txt` = `Frappe S3 Attachment`.
- **Why it breaks the plan:** Circular dependency. On a real legacy 0.2.x site, `installed_apps` contains `frappe_s3_attachment` (module gone after rename) → migrate raises before any patch runs, and `cloud_file_storage` is not in `installed_apps` so its patch is never even collected. The plan's P8 acceptance ("synthetic 'legacy site' integration test … run patch, assert all rewrites") invokes `execute()` directly and therefore **cannot detect this**.
- **Suggested fix:** Ship an out-of-band adoption entrypoint that runs *before* migrate: a `bench cfs-adopt-legacy-install --site X` command (or documented `bench --site X execute cloud_file_storage.compat.adopt_installed_apps`) that rewrites the `installed_apps` global and the Module Def/app_name first, and make the pre_model_sync patch idempotently finish the rest. Alternatively ship a minimal `frappe_s3_attachment/` shim package (with an empty `patches.txt` and `hooks.py`) so migrate survives long enough for the patch to run. Change the acceptance test to run a real `bench migrate` on the synthetic legacy site.

---

## [SEVERITY: MAJOR] [CONFIRMED] CSO status vocabulary and the `ensure_cso` API disagree between the runtime and migration designs; the plan's cross-design rulings do not reconcile them

- **Evidence:**
  - `runtime-storage.md §3`: CSO `status` Select = `pending_upload\nuploaded\nverified\nfailed\norphaned\npending_delete\ndeleted\nlegacy_unverified` (lowercase); fields are `content_sha256`, `content_hash_md5`, `file_size`. Constructor is `ensure_cso(sha256, md5, size, mime, visibility) -> cso` (§10).
  - `migration-engine.md §6.3` step 3: `SELECT name FROM tabCloud Storage Object WHERE sha256=%s AND size=%s AND status='Verified'` — three names that do not exist (`sha256`, `size`, `'Verified'`).
  - `migration-engine.md §6.3` step 4: `get_or_create_cso(provider, bucket, key, sha256, md5, size, mime, visibility, source_path, etag, version_id, status="Uploaded")` — a different function name, a different arity, and a capitalised status.
  - `migration-engine.md §6.4` step 3: "CSO status → `Verified`".
  - Plan "Principal rulings on cross-design conflicts" (lines 329-360) resolves serving, doctype names, backup config, settings schema, multipart, runtime verification, URL rewrite, and raw-SQL boundaries — **no ruling on the CSO status enum or the CSO constructor API**. The only schema-freeze instruction (ruling #4) covers `Cloud Storage Settings`, not CSO.
- **Why it breaks the plan:** P5 (migration engine) is built by a different agent than P2 (runtime core) against a frozen contract that was never written down. The dedup pre-check silently returns zero rows (every object re-uploaded, defeating the dedup that the whole 100GB throughput budget assumes) or raises on a missing column; the status-string mismatch means `→ S3_ONLY`'s "zero unverified managed Files" gate never passes.
- **Suggested fix:** Add a binding ruling: canonical CSO status values are the lowercase runtime set; the only public constructor is `storage.objects.ensure_cso(...)`; the migration engine calls it with `status` derived, never literal. Freeze `storage/objects.py`'s public signatures at end of P2 into `docs/design/runtime-storage.md` and make P5's first task a signature-conformance test (import the module, assert the callable names/params) rather than prose.

---

## [SEVERITY: MAJOR] [CONFIRMED] Select fields with no explicit `default` silently take the first option — `Cloud Storage Object.visibility` defaults to `public`, defeating `reqd`

- **Evidence:**
  - `frappe/model/create_new.py:117-118` — `elif df.fieldtype == "Select" and df.options and df.options not in ("[Select]", "Loading..."): return df.options.split("\n", 1)[0]`; applied via `frappe/model/document.py:830-836` `_set_defaults` → `frappe.new_doc(..., as_dict=True)` → `update_if_missing`.
  - Design `runtime-storage.md §3`: `visibility | Select | public\nprivate, reqd, read_only`. No `default`.
  - Same pattern (no default, non-empty first option) in `migration-engine.md §2.3` `identity_kind` (`local_public\n…`), `classification` (`healthy_unique\n…`); `§2.5` `conflict_type` (`conflicting_url\n…`), `severity` (`Blocker\nWarning`); `runtime-storage.md §3` `provider` (`AWS S3\n…`).
  - The design *does* use the leading-`\n` idiom correctly elsewhere (`active_phase`, `control_flag`, `sse_mode`), proving the authors know the mechanism — the omissions are inconsistent, not deliberate.
- **Why it breaks the plan:** `reqd` on a Select with a non-empty first option is a no-op: any code path that forgets to set `visibility` produces a `public` CSO, hence a `pub/…` content-addressed key (§4), hence an object under the prefix the CloudFront distribution is authorised to serve (§8.2) — a confidentiality failure that no `reqd` validation will catch. `classification`/`identity_kind` silently defaulting to `healthy_unique`/`local_public` means a mis-inserted migration object looks like a clean local public file and gets uploaded and cleaned up.
- **Suggested fix:** Every non-workflow Select in all four new doctype families gets either an explicit `default` or a leading `\n` in `options`. Add a lint/test that loads each shipped doctype JSON and asserts `default` is set (or `options` starts with `\n`) for every `Select` field.

---

## [SEVERITY: MINOR] [CONFIRMED] The private-serving patch double-writes Access Log rows and re-does the full `find_file_by_url` scan whenever a local copy exists

- **Evidence:** Design `runtime-storage.md §8.1` pseudo-code: `make_access_log(doctype="File", document=file.name, file_type=ext)` then `if os.path.exists(file._canonical_local_path()): return _original(path)`. `_original` is `frappe/utils/response.py:267-279`, which itself calls `find_file_by_url(path, name=frappe.form_dict.fid)` (`:274`) — which loads **all** File rows sharing the URL and `frappe.get_doc`s each (`frappe/core/doctype/file/utils.py:430-443`) — and `make_access_log(...)` again (`:278`).
- **Why it breaks the plan:** In DUAL_WRITE (the mode the site runs in for the entire 1-2 day migration and beyond) every private download writes two Access Log rows and pays the permission scan twice. The plan's P3 acceptance says "Access Log rows asserted" — a test asserting `>= 1` row passes; a test asserting exactly one fails, and the design claims "byte-for-byte preserved (contract #10)".
- **Suggested fix:** Replace `return _original(path)` with a direct `return frappe.utils.response.send_private_file(path.split("/private", 1)[1])` (`response.py:282-311`), preserving X-Accel behaviour without re-running the gate.

---

## [SEVERITY: MINOR] [CONFIRMED] `migration-engine.md §6.2` states the scheduler tick is "≥240s (`scheduler_interval` default)" — the real default tick is 60s, and the plan says "per-minute"

- **Evidence:** `frappe/utils/scheduler.py:247-248` — `def get_scheduler_tick() -> int: return cint(frappe.get_conf().scheduler_tick_interval) or 60`; `:51-54` — `while True: time.sleep(tick); enqueue_events_for_all_sites()`. The 240 figure comes from `frappe/core/doctype/scheduled_job_type/scheduled_job_type.py:126` — `"All": f"*/{(frappe.get_conf().scheduler_interval or 240) // 60} * * * *"` — which only parameterises the `All` **frequency**, not the loop.
- **Why it breaks the plan:** The plan and the design contradict each other (plan line 244: "per-minute scheduler `dispatch_tick`"; design: "≥240s"). The design's stale-batch-recovery SLA and the "the tick exists for crash recovery and cold start" rationale are argued from the wrong number. The claim is conservative rather than dangerous, but it will be copied into the runbook.
- **Suggested fix:** Correct the design to "default tick 60 s, configurable via `scheduler_tick_interval`; effective per-site cadence is 60 s + the sequential all-sites loop time (14 sites on this bench)". Note also that `ScheduledJobType.validate` forces `create_log = 1` for `Cron` frequency (`scheduled_job_type.py:56-58`), producing one `Scheduled Job Log` row per minute per site.

---

## [SEVERITY: MINOR] [CONFIRMED] `Float` byte counters are `decimal(21,9)` on MariaDB, not DOUBLE — ~1 TB ceiling, and the stated rationale is wrong

- **Evidence:** `migration-engine.md §2` — "Byte counters use **Float** (DOUBLE — exact ≤2^53) because Frappe `Int` is INT(11)". Actual mapping: `frappe/database/mariadb/database.py:179` — `"Float": ("decimal", "21,9")`. `Long Int` **is** available: `frappe/model/__init__.py:11` and `mariadb/database.py:178` — `"Long Int": ("bigint", "20")` — and the runtime design already uses it for `CSO.file_size`.
- **Why it breaks the plan:** `decimal(21,9)` gives 12 integer digits ≈ 1 TB; today's 100 GB corpus fits, a later 10 TB one does not, and DECIMAL arithmetic on 1.2 M-row aggregates is materially slower than BIGINT. The stated justification is factually false and will be reused for future fields.
- **Suggested fix:** Use `Long Int` for `bytes_total`, `bytes_uploaded`, `bytes_verified`, `disk_size`, `size_bytes` — matching the CSO schema — and correct the rationale text.

---

## [SEVERITY: MINOR] [CONFIRMED] `frappe.db.add_index("File", ["content_hash(32)"])` creates a bogus Property Setter and commits the caller's transaction

- **Evidence:** `frappe/database/mariadb/database.py:413-436` — after the `ALTER TABLE`, `if len(fields) == 1 and not (frappe.flags.in_install or frappe.flags.in_migrate): make_property_setter(doctype, fields[0], property="search_index", …)` with `fields[0] == "content_hash(32)"` (a non-existent fieldname). Also `:419` — `self.commit()` is issued before the ALTER. Design `migration-engine.md §3.1` calls this from `start_analysis`, i.e. outside install/migrate.
- **Why it breaks the plan:** Every campaign start leaves a junk `Property Setter` row on `File` and commits whatever transaction the caller had open. Harmless individually, but it pollutes a core doctype in production and violates the design's own "sanctioned raw SQL registry" spirit.
- **Suggested fix:** Call `frappe.db.add_index("File", ["content_hash"], index_name="content_hash_index")` (varchar(140) index is fine for a 32-char MD5), or run the ALTER inside the app's `after_migrate` where `frappe.flags.in_migrate` suppresses the Property Setter.

---

## [SEVERITY: MINOR] [CONFIRMED] SCAN_DB's `ref_count = ref_count + VALUES(ref_count)` upsert is not idempotent across campaign-level re-runs

- **Evidence:** `migration-engine.md §3.2` step 4 and the claim in the same section: "Resume after any crash: `WHERE name > scan_cursor_file` — pages are idempotent (`INSERT IGNORE`/upsert)". `INSERT IGNORE` on File Ref is idempotent; `ref_count = ref_count + VALUES(ref_count)` is not — re-running any already-committed page (operator resets the cursor, or SCAN_DB is re-entered from `Analyzed`) double-counts. The value feeds `adopt_references(cso, n)` (§6.3).
- **Why it breaks the plan:** Inflated `ref_count` means CSOs never reach refcount 0 in GC (safe direction) and the campaign counters/dashboard lie. It also means the analyzer is not truly "re-runnable" as §3.4/§4 assume.
- **Suggested fix:** Drop the incremental counter; derive it in CLASSIFY step 9 with `UPDATE … SET ref_count = (SELECT COUNT(*) FROM \`tabCloud Migration File Ref\` r WHERE r.migration_object = o.name)`.

---

## [SEVERITY: MINOR] [CONFIRMED] Residual doc-vs-plan drift the plan claims to have resolved but leaves in the frozen artifacts

- **Evidence:**
  - `runtime-storage.md §2` still ships `multipart_threshold_mb` default **32** / `multipart_chunksize_mb` **32**; plan ruling #5 mandates 64/16.
  - `delivery-platform.md §1.4` step 7 rewrites URLs to `cloud_file_storage.api.serve.generate_file`; plan ruling #1 renames the target to `cloud_file_storage.api.compat.legacy_generate_file`.
  - `delivery-platform.md §1.3` still names "Cloud Migration Campaign / Batch / **Item** / Conflict"; plan ruling #2 mandates "Object"/"File Ref".
  - `runtime-storage.md §2` defines a `public_bucket` setting ("if set, `pub/` objects go here instead"); plan line 220 mandates "visibility via `pub/`/`prv/` key prefix in **one** private bucket", and the CSO dedup identity includes `bucket` (§10) — a second bucket silently forks the dedup namespace.
  - Plan lines 138-139 ("StaticDataMiddleware … passes through on miss", "`website_redirects` never sees file URLs") contradict verified behaviour (`frappe/middlewares.py:25`; `frappe/website/path_resolver.py:37` runs `resolve_redirect` on every fallthrough path) and plan line 219.
- **Why it breaks the plan:** Plan §"Design documents" says the three docs are "to be committed into `docs/design/` in Phase P1 as durable artifacts", and P2/P5 implementers are told to build from them. Only the Settings schema has a designated freeze step (ruling #4). The stale values will be implemented verbatim.
- **Suggested fix:** Add a P1 task: apply all eight rulings **into** the three design files before committing them, and add a `docs/design/CHANGELOG` line per ruling. Delete `public_bucket` or add a ruling that makes `bucket` part of the frozen single-bucket invariant.

---

## Explicitly: mechanisms I tried to refute and could not

No BLOCKER-level defect was found in any of the following; each is confirmed to work exactly as the plan claims:

- **nginx fallthrough** — `bench .../templates/nginx.conf:91` `try_files /{{ site_name }}/public/$uri @webserver;` and `:94-105` `location @webserver` proxying with the original URI; a missing `/files/x.pdf` does reach `frappe/app.py:128-129` `get_response()` → `PathResolver` → custom `page_renderer` hooks first (`path_resolver.py:56-70`). Nothing 404s it earlier.
- **`download_private_file` monkeypatch** — `frappe/app.py:23` `import frappe.utils.response` + `:125-126` `frappe.utils.response.download_private_file(request.path)` is a call-time attribute lookup; `validate_auth()` at `:107` has already run, so token-auth clients are authenticated (ADR-5's premise is correct). `before_request` hooks run at `:209-210` on every request (idempotent installation works). Production gunicorn runs `frappe.app:application --preload` (`bench supervisor.conf:7`) *without* `application_with_statics()`, so there is no middleware interaction in prod.
- **`override_whitelisted_methods` for a non-existent module** — consulted **before** any import in both routers: v1 `frappe/handler.py:67` `cmd = frappe.override_whitelisted_method(cmd)` then `:73` `get_attr(cmd)` (reached via `frappe/api/v1.py:34-40` + `frappe/api/__init__.py:80-84`); v2 `frappe/api/v2.py:36` then `:43`. `frappe/__init__.py:2521-2524` takes the **last** override. `/api/method/frappe_s3_attachment.controller.generate_file?...` resolves with no `frappe_s3_attachment` package on the bench.
- **DB API surface** — `for_update` is real and honoured (`database/database.py:476, 880` → `database/query.py:88-89` `.for_update(skip_locked=…, nowait=…)`); `add_index`/`add_unique` exist with the claimed signatures (`mariadb/database.py:413, 438`); `Long Int` → `bigint(20)`; `autoname "hash"` is timestamp-prefixed base32 (`model/naming.py:275-293`), and `format:{field}` substitution works (`naming.py:216-217, 566-583`).
- **Scheduler / queues** — a `"cron": {"* * * * *": [...]}` entry really does fire ~per minute (60 s tick, `scheduler.py:51-54, 247`); `ScheduledJobType.get_queue_name()` (`scheduled_job_type.py:183`) only returns `long`/`default`, so the thin-dispatcher pattern is **required and correct**; `hourly_maintenance`/`daily_maintenance` are valid hook keys (`scheduled_job_type.py:235-241` `.replace("_"," ").title()` → the `DF.Literal` set at `:29-43`) and route to `long`. Custom queue `cloud_migration` needs `workers` in `common_site_config.json` (`background_jobs.py:41-56`, `@lru_cache` → restart required) and bench's supervisor template renders `bench worker --queue <name>` with `stopwaitsecs = timeout`, `numprocs = background_workers` (`supervisor.conf:76-90`). `enqueue(deduplicate=True, job_id=…)` namespaces consistently (`background_jobs.py:93-108`, `559-582`), and the design correctly refuses to rely on it.
- **`write_file` dual convention** — `file.py:711-715` `write_file_method(self)` (return value discarded, since `before_insert` at `:111` ignores it — the hook must mutate `self.file_url`, which the design does) vs `file_manager.py:165-166` `(fname, content, content_type=…, is_private=…)` with `write_file_keys` (`hooks.py:70`); `get_hook_method` takes `method[0]` (`utils/__init__.py:624-628`) — first app wins, single-owner as stated. `validate()` runs after `before_insert` (`document.py:303` then `:309`) against the hook-set `file_url`.
- **Core dedup interplay** — `file.py:685-702`: when `duplicate.exists_on_disk()` is true, `file_exists = True` and the whole `if not file_exists:` block (including `write_file`) is skipped; `after_insert`/`ensure_cso_link` is adequate for *that* path because content is already in memory. `_delete_file_on_disk` (`file.py:511-526`) refcounts by `content_hash` ignoring `is_private`, exactly as the design's GC compensation assumes.
- **`override_doctype_class`** — last-wins, no MRO chaining (`model/base_document.py:89-110`); `find_file_by_url` builds docs via `frappe.get_doc(doctype="File", **file_data)` with `fields="*"` (`file/utils.py:435-443`), so `CloudFile` and the custom `cloud_storage_object` column are both available in the serving path.
- **Signatures the overrides target** all exist verbatim at v15.93: `validate_file_on_disk` (`file.py:358`), `exists_on_disk` (`:563`), `get_content(self)` (`:566`), `make_thumbnail(self, set_as_thumbnail=True, width=300, height=300, suffix="small", crop=False)` (`:435-442`), `generate_content_hash` (`:424`), `on_rollback` (`:163`), `handle_is_private_changed` (`:226`). `make_access_log(doctype=, document=, file_type=)` matches (`access_log.py:41-50`).
- **Phase 0 premise** verified: `sites/apps.txt` lists `frappe_s3_attachment`, `env/.../frappe_s3_attachment.pth` → `/home/user/v15/apps/frappe_s3_attachment` (absent), package lives at `/home/user/v15/apps/cloud_file_storage/frappe_s3_attachment/`, `modules.txt` = `Frappe S3 Attachment`, `pyproject.toml` pins `frappe = ">=15.0.0,<17.0.0"` (the "silently testing v16" claim is accurate).

**Severity levels with no findings:** none — findings exist at BLOCKER, MAJOR and MINOR.

---

## Verdict

The plan's *architecture* is sound and its load-bearing frappe mechanisms are, with one exception, real: the nginx fallthrough, the `page_renderer` seam, the call-time `download_private_file` lookup after `validate_auth`, the `override_whitelisted_methods` pre-import remap on both API routers, `for_update`/`add_unique`/`add_index`/`Long Int`, the 60-second scheduler tick, the `workers`-key custom queue with bench supervisor support, and the dual-convention `write_file` seam all behave as claimed at v15.93 — I could not refute any of them. **It is not, however, implementable as written.** The single decision that makes the design better than DFP — keeping `file_url` canonical so `is_remote_file` stays `False` — is precisely what makes `File.before_insert` re-read bytes from local disk for every File row created from a URL alone, and the design's cloud-resolution path is gated on a `cloud_storage_object` link that is only written in `after_insert`; amend, Attach-field attachment, and the `file_manager.save_file` path named as contract #12 all break in S3-primary modes, and the P2 gate as scoped (LOCAL_ONLY + DUAL_WRITE only) cannot detect it. Beyond that, three mechanisms are specified in ways that cannot work — the dev `StaticDataMiddleware` shim (loader closure baked at construction, middleware runs outside `before_request`), the `after_request`/`after_job` write-back (neither hook site commits, and the request path swallows exceptions), and the external-install compat patch (circular: it must fix `installed_apps` from inside a patch that `installed_apps` gates) — plus two silent-correctness traps in frappe itself that the designs do not account for: the `website_404` negative cache short-circuiting the public renderer in production only, and Select fields without an explicit `default` silently taking the first option (making `CSO.visibility` default to `public`). None of these is architecturally fatal; all are addressable inside the existing phase structure. The concrete prerequisite before P2 starts is: (1) redefine cloud resolution as a `link → file_url sibling → content_hash sibling` fallback chain and add S3_ONLY-mode amend/attach/file_manager tests to the P2 exit gate; (2) issue binding rulings on the CSO status enum and `ensure_cso` signature, and apply all eight existing rulings into the three design files before they are committed in P1; (3) re-specify the dev shim, the write-back commit discipline, the `website_404` invalidation, and the legacy-install adoption entrypoint. With those changes the plan is implementable.