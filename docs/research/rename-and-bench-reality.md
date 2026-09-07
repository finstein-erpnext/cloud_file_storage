> **STATUS (P0.5): HISTORICAL EVIDENCE.** Captured 2026-08-14 during planning; verified
> file:line ground truth at capture time. Where later decisions supersede content here,
> `docs/PLAN.md` / `docs/adr/` win. Known supersessions: P0 is COMPLETE (the "bench is
> currently broken" state described in rename-and-bench-reality §0 was RESOLVED by the
> ops-only .pth repair — see PROGRESS.md); the CI matrix/floor is {v15.16.0, v15.93.0,
> version-15} with `frappe>=15.16.0,<16.0.0` (A34), not the v15.0.0-based examples.

I have everything needed.

# Rename & Test-Infra Ground Truth: `frappe_s3_attachment` → `cloud_file_storage`

## 0. HEADLINE (RESOLVED BY P0 — historical): THE BENCH WAS BROKEN AT CAPTURE TIME

The app directory was already renamed on disk to `/home/user/v15/apps/cloud_file_storage`, but **nothing else was renamed**. Every bench command on every site now dies:

- `/home/user/v15/sites/apps.txt` (last line) still lists `frappe_s3_attachment`; `sites/apps.json` still keys it `"frappe_s3_attachment"` with `"version": "0.2.2"`.
- The editable-install pointer is stale: `/home/user/v15/env/lib/python3.10/site-packages/frappe_s3_attachment.pth` contains the single line `/home/user/v15/apps/frappe_s3_attachment` (a path that no longer exists), and `frappe_s3_attachment-0.2.2.dist-info/direct_url.json` records `"url": "file:///home/user/v15/apps/frappe_s3_attachment"`.
- The inner python package is still `cloud_file_storage/frappe_s3_attachment/` and `frappe_s3_attachment/hooks.py:3` still says `app_name = "frappe_s3_attachment"`.

Failure chain (reproduced by running `bench --site erp.local list-apps`):
`frappe/commands/site.py:521` → `frappe.init()` → `frappe/__init__.py:258` `setup_module_map(include_all_apps=not (frappe.request or frappe.job or frappe.flags.in_migrate))` → `frappe/__init__.py:1670` `get_module_list(app)` → `frappe/__init__.py:1518` `get_file_items(get_app_path(app_name, "modules.txt"))` → `frappe/__init__.py:1513` `get_module(scrub(modulename))` → `ModuleNotFoundError: No module named 'frappe_s3_attachment'`.

Because `get_all_apps()` (`frappe/__init__.py:1521-1537`) reads **`sites/apps.txt`**, not the site's installed list, this breaks **all 14 sites**, including sites that never installed the app. Restoring service requires only: revert the `apps.txt` entry OR complete the rename (apps.txt + package dir + `.pth`/reinstall).

---

## 1. INSTALLED-STATE REALITY

**`frappe_s3_attachment` is installed on ZERO sites.** Verified by direct read-only SQL against each site DB (`bench list-apps` is unusable — see §0), reading `` `tabInstalled Application` `` per `db_name` from each `sites/<site>/site_config.json`:

| Site dir | `tabInstalled Application` app_name list | s3 app? |
|---|---|---|
| avalon.erp | frappe, payments, erpnext, avalon_erp | no |
| circuits.local | frappe, payments, erpnext, hrms, india_compliance | no |
| ejay_plastics | frappe, ejay_plastics_erpnext, payments, erpnext | no |
| erp.local | frappe, payments, erpnext, hrms, india_compliance, healthcare, kaynes, lending | no |
| finstein.erp *(default_site)* | frappe, payments, erpnext, hrms, finerp, india_compliance | no |
| kaynes.semicon | frappe, erpnext, hrms | no |
| kaynes.site | frappe, payments, erpnext, india_compliance, e_invoicing, healthcare, lending, hrms, kaynes | no |
| kaynes.test | *(shares db `_b4dd58d01acce279` with kaynes.site)* same list | no |
| learning.local | frappe, payments, erpnext, india_compliance, mint, hrms | no |
| nextgen_ui.local | frappe, nextgen_ui, doppio, payments, erpnext | no |
| site.local | frappe, airplane_mode | no |
| skc.erp | frappe, erpnext, payments, hrms, india_compliance, posawesome, skc_erpnext, pinelabs_integration | no |
| skc.local | **DB `_03764da98a88aac0` does not exist** (dead site dir) | n/a |
| vnc.erp | frappe, payments, erpnext, vnc_erpnext | no |

Residue sweep across all 13 live DBs (`tabModule Def` where `app_name`/`name` matches `%s3%`/`%cloud%`; `tabDefaultValue` where `defkey='installed_apps'`; `information_schema.tables` matching `%S3%`/`%Cloud%`; `` `tabPatch Log` `` matching `%s3%`/`%cloud_file%`) returned **exactly 1 row per site — the `installed_apps` DefaultValue itself, none containing `s3` or `cloud`**. No `tabS3 File Attachment`, no `tabS3 Ignored DocType Row`, no `Module Def` named "Frappe S3 Attachment", no `frappe_s3_attachment.patches.*` Patch Log rows anywhere.

**Planning consequence:** the rename is a **greenfield rename**. No production site carries old-name state. There is **no need to ship a data-migration rename patch for this bench**. A rename patch is only needed for *external* installs of `frappe_s3_attachment` 0.2.x — treat it as an optional, defensive, opt-in patch, not a blocker.

Note: `kaynes.site` and `kaynes.test` **share one database**. Any per-site install/uninstall of the new app on one will mutate the other.

---

## 2. HOW FRAPPE TRACKS APPS / MODULES

**Where installed-app names live — two independent stores that must stay in sync:**

1. **Authoritative:** `tabDefaultValue` with `parent='__global'`, `defkey='installed_apps'`, a JSON array.
   - Read: `frappe/__init__.py:1553` `installed = json.loads(db.get_global("installed_apps") or "[]")`.
   - `get_global` → `get_default(key, "__global")` → `frappe/database/database.py:1127-1132`, backed by `frappe.defaults` (`database.py:1141` `set_default(key, val, parent, parenttype)`).
   - Write on install: `frappe/installer.py:347` `frappe.db.set_global("installed_apps", json.dumps(installed_apps))`.
   - Write on uninstall: `frappe/installer.py:360-362` `frappe.db.set_value("DefaultValue", {"defkey": "installed_apps"}, "defvalue", ...)`.
2. **Derived / display only:** the `Installed Applications` **Single** doctype with child table `Installed Application` (`istable: 1`, module `Core`, fields `app_name, app_version, git_branch, has_setup_wizard, is_setup_complete`). Rebuilt wholesale by `frappe/core/doctype/installed_applications/installed_applications.py:28-57` `update_versions()` → `self.delete_key("installed_applications")` then re-appends from `frappe.utils.get_installed_apps_info()` (`frappe/utils/__init__.py:807-819`), which itself iterates `get_versions()`. Called from `installer.py:352`, `installer.py:365`, `installer.py:422`, and every migrate (`frappe/migrate.py:151`).

**Two flavors of `get_installed_apps` — the distinction is the single most important rename fact:**
- `frappe.get_installed_apps()` (`frappe/__init__.py:1541`, `@request_cache`) returns the **raw DB list**, including apps not present on the bench.
- `frappe.get_installed_apps(_ensure_on_bench=True)` (`frappe/__init__.py:1555-1557`) filters against `get_all_apps()` i.e. `sites/apps.txt`.

Hooks loading uses the *filtered* form (`frappe/__init__.py:1584` `apps = [app_name] if app_name else get_installed_apps(_ensure_on_bench=True)`), and still hard-raises on ImportError (`frappe/__init__.py:1589-1595`: prints `Could not find app "{app}"` then `raise`). But **migrate and patches use the unfiltered form** — see below.

**Module Def rows per app:** created on install by `frappe/installer.py:314` `add_module_defs(name, ...)` → `installer.py:684-690`, one `Module Def` per line of the app's `modules.txt`, with `app_name = <app>`, `module_name = <module>`. This app declares exactly one module: `frappe_s3_attachment/modules.txt` = `Frappe S3 Attachment`.

**doctype → module → app resolution:**
- `frappe/modules/utils.py:235-245` `get_doctype_module(doctype)` — cached dict from `tabDocType.name → tabDocType.module`. `tabDocType.module` is a **Link to `Module Def`** (`doctype.json`: `{'fieldname': 'module', 'fieldtype': 'Link', 'options': 'Module Def', 'reqd': 1, 'search_index': 1}`).
- `frappe/modules/utils.py:277-281` `get_module_app(module)` — reads the **in-memory** `frappe.local.module_app` map (built from `modules.txt` files, *not* from `tabModule Def.app_name`), and `frappe.throw(... DoesNotExistError)` if absent.
- `frappe/__init__.py:1467-1475` `get_module_path(module, *joins)` = `get_pymodule_path(get_module_app(module) + "." + scrub(module), *joins)`.
- `frappe/modules/utils.py:270-274` `get_module_name()` builds the dotted controller path `f"{app}.{module}.doctype.{doctype}.{doctype}"`; `utils.py:248-267` `load_doctype_module` imports it and re-raises failures as `ImportError`.
- Separately, `frappe/modules/utils.py:284-294` `get_doctype_app_map()` joins `tabDocType.module` → `tabModule Def.app_name` (DB-based, `@site_cache`) — so `tabModule Def.app_name` **also** must be rewritten, it is not merely cosmetic.
- Collision warning if two apps declare the same module: `frappe/__init__.py:1683-1687`.

**What happens on `bench migrate` when a DB-installed app is missing from apps.txt:**

There is **no graceful orphan-app handling — migrate crashes before it starts.** `frappe/migrate.py:174-197` `run()` calls `frappe.init(site=site)` at line 181 **before** `frappe.flags.in_migrate = True` is set (that happens in `setUp()`, `frappe/migrate.py:88`). So `frappe/__init__.py:258` evaluates `include_all_apps=True` and imports **every app in apps.txt** — this is the §0 crash.

If instead the app is in the DB but *absent from apps.txt*, migrate still crashes, one layer deeper, because patch collection uses the **unfiltered** installed list:
```
frappe/modules/patch_handler.py:89   for app in frappe.get_installed_apps():
frappe/modules/patch_handler.py:90       patches.extend(get_patches_from_app(app, patch_type=patch_type))
frappe/modules/patch_handler.py:102      patches_file = frappe.get_app_path(app, "patches.txt")
```
`get_app_path` → `get_pymodule_path` → `get_module()` → uncaught `ModuleNotFoundError`. Same for `frappe/migrate.py:113` (`before_migrate` hooks), `frappe/migrate.py:155` (`after_migrate` hooks), and `frappe/model/sync.py:43` `for app in frappe.get_installed_apps():`.

The only legacy tolerance is a hardcoded two-app allowlist: `frappe/installer.py:692-705` `remove_missing_apps()` handles only `("frappe_subscription", "shopping_cart")`.

**THE DOCTYPE REAPER** — `frappe/migrate.py:148` `frappe.model.sync.remove_orphan_doctypes()`, run every migrate in `post_schema_updates`:
```python
# frappe/model/sync.py:143-177
def remove_orphan_doctypes():
	"""...Note: Deleting the entry doesn't delete any data.
	So this is supposed to be non-destrictive operation."""
	doctype_names = frappe.get_all("DocType", {"custom": 0}, pluck="name")
	...
	for doctype in doctype_names:
		if doctype in class_overrides: continue
		try:
			get_controller(doctype=doctype)
		except ImportError:
			orphan_doctypes.append(doctype)      # <-- reaped
		except Exception:
			continue                              # <-- spared
	...
	for i, name in enumerate(orphan_doctypes):
		frappe.delete_doc("DocType", name, force=True, ignore_missing=True)
```
Precise trigger semantics (this is subtle and decides the whole rename design):
- `frappe/model/base_document.py:66-67` — under `frappe.flags.in_migrate`, `get_controller` **bypasses the cache** and re-imports every time.
- Reaped **only** on `ImportError`. `load_doctype_module` (`frappe/modules/utils.py:262-265`) converts a failed controller import into `ImportError`. So: **`tabDocType.module` still resolves to an app, but the app's python package/controller file is gone → REAPED.**
- Spared when `get_module_app()` raises `DoesNotExistError` (`frappe/modules/utils.py:280`), because `DoesNotExistError(ValidationError)` (`frappe/exceptions.py:38`, `:18`) is **not** an `ImportError`, so it lands in `except Exception: continue`. So: **`Module Def` row exists but the module is not in any app's `modules.txt` → SPARED (but broken).**

The docstring's "non-destrictive" claim is **only partly true**. `frappe.delete_doc("DocType", name, force=True)` does not `DROP TABLE` (contrast `frappe/installer.py:515` which does), but it **does** destroy metadata and customizations:
- `frappe/model/delete_doc.py:84-86` — `frappe.db.delete("Custom Field", {"options": name, "fieldtype": ("in", frappe.model.table_fields)})`
- `frappe/model/delete_doc.py:87` — `frappe.db.delete("__global_search", {"doctype": name})`
- `frappe/model/delete_doc.py:89` → `delete_from_table` (`delete_doc.py:206-226`) — deletes the `tabDocType` row, and **all child rows of every child doctype** (`frappe.db.delete(child_doctype, {"parenttype": doctype, "parent": name})`, line 226).
- `frappe/model/delete_doc.py:64` `delete_all_passwords_for(doctype, name)` — **destroys `__Auth` secrets**. For this app that means the `S3 File Attachment.secret_key` Password field is unrecoverable.
- For a **Single**, `delete_from_table` takes the `doctype == "DocType"` branch (`delete_doc.py:207-210`), so **`tabSingles` rows are left orphaned** — the settings values survive as garbage but are unreachable, and if the doctype is later recreated under a *new* name they are not carried over.
- `frappe/model/delete_doc.py:91-105` also deletes controller source files, but that path is guarded by `not frappe.flags.in_migrate`, so migrate does not touch disk.

⚠️ **This bench has `"developer_mode": 1` in `/home/user/v15/sites/common_site_config.json`**, which changes several code paths (see §3).

---

## 3. RENAME MECHANICS

**There is NO core-supported app rename.** Exhaustive search of `frappe`, `erpnext`, `hrms`, `payments`, `india_compliance`, `lending`, `healthcare` found no `rename_module`, no `frappe.rename_doc("Module Def", ...)`, and no app-rename command. Every real-world app split in this bench is implemented as **delete-in-old-app + fresh-install-of-new-app**, never a rename.

### Concrete precedent: ERPNext → `lending` split
`/home/user/v15/apps/erpnext/erpnext/patches/v15_0/remove_loan_management_module.py` (full file, 47 lines). Steps, in order:
1. `:5-6` **Guard/idempotency** — `if "lending" in frappe.get_installed_apps(): return`. The patch is a no-op when the successor app is present; the successor app owns the records.
2. `:8` `frappe.delete_doc("Module Def", "Loan Management", ignore_missing=True, force=True)`
3. `:10` delete the `Workspace`
4. `:12-16` delete `Print Format` where `{module: ..., standard: "Yes"}`
5. `:18-20` delete `Report` where `{module: ..., is_standard: "Yes"}`
6. `:22-24` delete `DocType` where `{module: ..., custom: 0}`
7. `:26-30` delete standard `Notification`
8. `:32-35` delete standard `Web Form`, `Dashboard`, `Dashboard Chart`, `Number Card`
9. `:37-46` delete the app's owned `Custom Field` rows by explicit `(dt, fieldname)` allowlist

The parallel HR split is `erpnext/erpnext/patches/v14_0/remove_hr_and_payroll_modules.py:5` — same `if "hrms" in frappe.get_installed_apps(): return` guard. `hrms/hrms/patches.txt` contains **zero** module/app-rename patches; hrms simply installs fresh.

Also note `frappe/installer.py:439-469` `_delete_modules()` — the canonical uninstall path — which for Singles does the two-step `frappe.delete_doc(dt, dt, ...)` **then** `frappe.delete_doc("DocType", dt, ...)` (`installer.py:457-459`), precisely to clear `tabSingles` before dropping the DocType row. Any hand-rolled rename patch must replicate that ordering or leak Singles data.

### If you nevertheless write a rename patch, this is what actually happens

**`frappe.rename_doc("Module Def", "Frappe S3 Attachment", "Cloud File Storage")` — viable, and does more than you'd expect:**
- `module_def.json`: `allow_rename: 1`, `autoname: field:module_name`, `module_name` is `Data` + `unique`, `app_name` is a **`Select`** (options injected client-side, `module_def.js:7`) — so `app_name` is a plain string column, freely settable by `frappe.db.set_value`.
- `frappe/model/rename_doc.py:177` → `rename_parent_and_child` (`:327-331`) renames the row and, via `update_autoname_field` (`:334-339`), rewrites `module_name` to the new value.
- `frappe/model/rename_doc.py:182-183` `get_link_fields("Module Def")` + `update_link_field_values` — **automatically rewrites every `Link`-to-`Module Def` column**, i.e. `tabDocType.module`, `tabReport.module`, `tabPage.module`, `tabWorkspace.module`, `tabWeb Form.module`, `tabPrint Format.module`, **`tabCustom Field.module`** (`custom_field.json`: `module Link Module Def`) and **`tabProperty Setter.module`** (`property_setter.json`: `module Link Module Def`). It also handles Single-hosted link fields via ORM save (`rename_doc.py:425-434`).
- ⚠️ **Side effect under `developer_mode: 1`:** `frappe/core/doctype/module_def/module_def.py:29-35` `on_update` calls `create_modules_folder()` (mkdir `<app>/<Module Name>` + `__init__.py`, `:37-43`) and `add_to_modules_txt()` (**appends to the app's `modules.txt` on disk**, `:45-57`). Running the rename patch on this bench will mutate the app source tree.
- ⚠️ `frappe.delete_doc("Module Def", ...)` under `developer_mode` triggers `on_trash` → `delete_module_from_file` (`module_def.py:62-80`) which **deletes the module folder and strips the line from `modules.txt`** — unless `frappe.flags.in_uninstall` is set (`:65`).
- ✅ `tabModule Def.app_name` is **not** updated by `rename_doc` — must be set explicitly (`frappe.db.set_value("Module Def", new, "app_name", "cloud_file_storage")`).

**Items `rename_doc` does NOT cover — each needs explicit handling:**

| Artifact | Where old dotted path / name is persisted | Behavior on rename |
|---|---|---|
| `installed_apps` | `tabDefaultValue` `parent='__global'`, `defkey='installed_apps'` (`frappe/__init__.py:1553`) | **Manual.** Rewrite the JSON array element `frappe_s3_attachment` → `cloud_file_storage`, then invalidate: `get_installed_apps` is `@request_cache` (`frappe/__init__.py:1540`) and `remove_from_installed_apps` pairs its write with `_clear_cache("__global")` (`installer.py:363`) — copy that. |
| `Installed Application` child rows | `tabInstalled Application` (`istable: 1`) | **Self-healing** — wiped and rebuilt by `update_versions()` (`installed_applications.py:33-57`) on every migrate (`migrate.py:151`). No action needed. |
| `Patch Log` | `` `tabPatch Log`.patch `` (`Code` field), holds `frappe_s3_attachment.patches.v0_2_0.backfill_s3_object_key` etc. | **Matters.** `patch_handler.py:56` builds `executed` as a **literal string set** and `:75` skips only exact matches. Old-named rows will *not* match new-named `patches.txt` entries → **every historic patch re-runs**. Must `UPDATE` the `patch` column prefix, or make all patches idempotent. Two rows to rewrite per site (`frappe_s3_attachment/patches.txt`). |
| `Scheduled Job Type` | `tabScheduled Job Type.method` (`Data`, name = last 2 dotted segments, `scheduled_job_type.py:53`) | **Auto-reaped, destructively.** `clear_events` (`scheduled_job_type.py:282-293`) deletes any job whose `method` is not in current hooks; its `on_trash` (`:186`) also deletes all `Scheduled Job Log` history. This app currently defines **no** `scheduler_events` (`hooks.py:99-115` all commented) — zero exposure today, but the planned migration engine will add jobs, so this becomes live. |
| RQ / background jobs | `frappe.enqueue("frappe_s3_attachment.controller.run_migrate_existing_files", ...)` (`controller.py:322-323`); job id `"frappe_s3_attachment.migrate_existing_files"` (`controller.py:19`) | **Manual.** In-flight Redis jobs serialize the dotted path and will `ModuleNotFoundError` post-rename. Drain the queue before rename. Critical for the planned 1.2M-row `cloud_migration` queue. |
| Custom Fields owned by the app | `tabCustom Field` name `File-s3_object_key`, created by `install.py:41-62` with `"module": "Frappe S3 Attachment"` (`install.py:58`) | `module` column auto-follows the Module Def rename. The **`fieldname`** (`s3_object_key`) and the physical `tabFile.s3_object_key` column do **not** — renaming the field is a separate, riskier operation. Also note the index `s3_object_key_index` (`install.py:5`, `:65-79`). |
| Property Setters | `tabProperty Setter.module` (Link to Module Def), `doc_type`, `field_name` | `module` auto-follows; none currently owned by this app. |
| Translations | `frappe_s3_attachment/locale/de.po`, `locale/main.pot` (40 + 38 occurrences of the old name) | Path-based (`<app>/locale`), moves with the package dir. Regenerate to fix embedded source references. |
| Fixtures | — | **Non-issue.** `hooks.py` defines no `fixtures` key (`frappe/utils/fixtures.py:12,70` iterate `get_hooks("fixtures", app_name=app)`). |
| **`tabFile.file_url` (⚠️ WORST)** | `controller.py:243-244`: `generate_method = "frappe_s3_attachment.controller.generate_file"` → `file_url = f"/api/method/{generate_method}?key={key}&file_name={...}"`, written straight to the DB at `controller.py:249-250` | **Manual, unavoidable, and touches every private file row.** Every private-file URL ever produced embeds the app's dotted path. The detector regex `controller.py:291` hardcodes it too. A rename without a `tabFile.file_url` rewrite **404s every private attachment**. Must be batched/resumable at 1.2M-row scale, and must keep the old whitelisted route alive as an alias during rollout. |

---

## 4. DOCTYPE RENAME MECHANICS (`S3 File Attachment`, a Single)

`s3_file_attachment.json`: `issingle: 1`, `module: "Frappe S3 Attachment"`, no `allow_rename` key (→ falsy), `custom` unset. Child table `S3 Ignored DocType Row` (`istable: 1`, same module). `doctype.json` itself has `allow_rename: 1`.

`frappe.rename_doc("DocType", "S3 File Attachment", "Cloud Storage Settings")` — **fully supported for Singles**, provided you satisfy the guards:

1. **Guards** (`frappe/core/doctype/doctype/doctype.py:638-647` `before_rename`):
   - `if not self.custom and frappe.session.user != "Administrator": frappe.throw(...)` — **must run as Administrator**.
   - `self.check_developer_mode()` — **requires `developer_mode`** (set here, `common_site_config.json`), unless `frappe.flags.in_patch`.
   - `merge=True` is rejected outright (`:646-647`).
   - `frappe/model/rename_doc.py:390-391`: `if not (force or ignore_permissions) and not meta.allow_rename: throw` — for `doctype="DocType"` the meta is *DocType's own* meta (`allow_rename: 1`), so this passes.
2. **`tabSingles` IS handled** — `frappe/core/doctype/doctype/doctype.py:649-662` `after_rename`:
```python
if self.issingle:
    frappe.db.sql("""update tabSingles set doctype=%s where doctype=%s""", (new, old))
    frappe.db.sql("""update tabSingles set value=%s
        where doctype=%s and field='name' and value = %s""", (new, new, old))
else:
    frappe.db.rename_table(old, new)
```
   → all stored settings values survive; both the `doctype` column and the `field='name'` self-reference row are rewritten. No `RENAME TABLE` is attempted (correct — Singles have no `tab` table).
3. **Also runs** (`rename_doc.py:190-198`): `rename_doctype()` (`:402-410`) rewriting `options` on every `Link`/`Table`/`Table MultiSelect` field pointing at the old name (`update_options_for_fieldtype`) and every `parenttype` value (`update_parenttype_values`, `:623-649`); `update_customizations()` (`:298`); `update_attachments()`; `rename_versions()`; `rename_eps_records()`; `rename_dynamic_links()` (`:652-672`, Singles-aware).
4. **Passwords survive** — `rename_doc.py:209-210` `rename_password(doctype, old, new)` when `not merge`. This preserves `S3 File Attachment.secret_key` in `__Auth`. (Contrast: `delete_doc` destroys it — `delete_doc.py:64`.)
5. **⚠️ Disk side effect:** `doctype.py:665-667` — `if not self.custom: if not frappe.flags.in_patch: self.rename_files_and_folders(old, new)` → `shutil.move` of the doctype source folder + renaming files containing `frappe.scrub(old)` (`doctype.py:675-686`). In a patch (`frappe.flags.in_patch` set by `patch_handler._patch_mode(True)`, `patch_handler.py:159`) this is **skipped**, so the JSON/py files must already be at their new path in the shipped code.
6. **Renaming the child table** `S3 Ignored DocType Row` must happen **too**, and note `update_parenttype_values` (`rename_doc.py:623-649`) looks up child doctypes by `{"parent": new}` — i.e. *after* the parent row rename — so order matters: rename the parent first, then the child, or the `parenttype` rewrite on the child's rows will target the wrong set.

**Recommendation:** given §1 (installed on zero sites), do **not** rename the doctype in place. Ship the new `Cloud Storage Settings` / `Cloud Storage Object` doctypes fresh, and gate any rename patch behind `if not frappe.db.exists("DocType", "S3 File Attachment"): return`.

---

## 5. TEST INFRA

**How `bench run-tests` works on this bench** (frappe 15.67.0, bench 5.25.9, python 3.10):
- Entry: `frappe/commands/utils.py:732-809`. Options include `--app`, `--doctype`, `--module`, `--module-def`, `--case`, `--test`, `--failfast`, `--coverage`, `--junit-xml-output`, `--skip-test-records`, `--skip-before-tests`.
- **Gate:** `utils.py:777-783` — requires `allow_tests` in site config **or** `os.environ.get("CI")`; otherwise it prints "Testing is disabled for the site!" and returns 0. (CI does `bench --site test_site set-config allow_tests true`.)
- `utils.py:785` `frappe.init(site)` → **the §0 crash blocks tests too**, on every site, for every app.
- `utils.py:789` → `frappe/test_runner.py:40` `main(...)`; `before_tests` hooks run at `test_runner.py:83-86` (`frappe.get_hooks("before_tests", app_name=app)` — this app declares none, `hooks.py:120` commented).
- **Discovery** (`test_runner.py:142-162`): `os.walk(frappe.get_app_path(app))`, pruning `locals`, `.git`, `public`, `__pycache__`; collects every `test_*.py` except `test_runner.py`. So discovery is **path-based via `get_app_path`** — it needs the app importable under its new name, but does **not** require the app to be installed on the site (though doctype/module resolution inside the tests does).
- `utils.py:808-809` `sys.exit(ret)` only under `CI`.
- Parallel variant: `utils.py:812+` `run-parallel-tests --app X --total-builds N --build-number M` (what erpnext/hrms/payments/lending use).

**App's current test surface:** 53 test methods total — `tests/test_controller.py` (38), `tests/test_install.py` (7), `doctype/s3_file_attachment/test_s3_file_attachment.py` (5), `tests/test_minio_integration.py` (3).

**`tox.ini`** is a stub — it is **not** a tox config at all, only `[flake8] max-line-length = 90` (2 lines). Dead config; the real linting is `[tool.ruff]` in `pyproject.toml` (line-length 110, `target-version = "py310"`, tab indent, double quotes) plus `.pre-commit-config.yaml` (whose `files: "frappe_s3_attachment.*"` filter, line 11, **must be updated or pre-commit silently stops checking anything**).

**MinIO in CI: YES.** `.github/workflows/ci.yml` defines two jobs:
- `tests` — services: `redis:alpine` on 13000 & 11000, `mariadb:11.8` on 3306; python 3.14, node 24; `bench get-app --resolve-deps frappe_s3_attachment $GITHUB_WORKSPACE`; `bench --site test_site install-app frappe_s3_attachment`; `bench --site test_site run-tests --app frappe_s3_attachment`.
- `tests-minio` — additionally `docker run -d --name minio -p 9000:9000 -e MINIO_ROOT_USER=minioadmin ... quay.io/minio/minio:latest server /data --address ":9000"`, a 30×1s `curl /minio/health/live` readiness loop, and an inline `boto3` heredoc that creates bucket `frappe-s3-attachment-test` with `Config(signature_version="s3v4", s3={"addressing_style": "path"})`. Runs the same command with `RUN_MINIO_INTEGRATION_TESTS: "1"` plus `FRAPPE_S3_ATTACHMENT_MINIO_{ENDPOINT,ACCESS_KEY,SECRET_KEY,BUCKET,REGION}`.
- Gating on the test side: `tests/test_minio_integration.py:23-24` — `if os.environ.get("RUN_MINIO_INTEGRATION_TESTS") != "1": raise SkipTest(...)`; env var names read at `:26-30`. **All 5 env var names and the bucket name are rename-coupled.**
- Also `.github/workflows/ci.yml` `concurrency.group: develop-frappe_s3_attachment-...` and 7 total occurrences of the old name in that file.

**⚠️ CI has no frappe version pin at all.** `ci.yml` does `bench init --skip-redis-config-generation --skip-assets --python "$(which python)" ~/frappe-bench` with **no `--frappe-branch`** and no `git clone --branch`. That defaults to frappe `develop` (v16+), while `pyproject.toml` declares `[tool.bench.frappe-dependencies] frappe = ">=15.0.0,<17.0.0"`. bench validates that constraint (`bench/app.py:313-322` `validate_app_dependencies` → `validate_dependency`; `bench/app.py:545-563` `get_required_frappe_version`; `bench/commands/make.py:264-268` exposes `--validate-app-dependencies` for `get-app`), but `<17.0.0` permits v16 — so **the current CI is not testing v15 at all.**

**What a "compatibility matrix vs multiple frappe v15 revisions" needs — the upstream pattern:**
Every upstream app in this bench pins frappe by **cloning it explicitly before `bench init`**, then passing `--frappe-path`:
- `hrms/.github/helper/install.sh:13-14`:
  `git clone https://github.com/frappe/frappe --branch "$BRANCH_TO_CLONE" --depth 1` then `bench init --skip-assets --frappe-path ~/frappe --python "$(which python)" frappe-bench`. `BRANCH_TO_CLONE` is supplied as an env var from the workflow; sibling apps are pulled with `bench get-app payments --branch ${BRANCH_TO_CLONE%"-hotfix"}` (`:43-45`) — note the `-hotfix` suffix stripping.
- `india_compliance/.github/helper/install.sh:30-31` — identical shape; the workflow derives `BRANCH: ${{ inputs.base_ref || github.base_ref || github.ref_name }}` (`server-tests.yml:32`) and passes `BRANCH_TO_CLONE: ${{ env.BRANCH }}` (`:107-109`), i.e. **the frappe branch tracks the PR's target branch**.
- `erpnext/.github/workflows/server-tests-mariadb.yml:12-23` exposes `workflow_dispatch` inputs `user` (default `frappe`) and `branch` (default `develop`), forwarded as `FRAPPE_USER` / `FRAPPE_BRANCH` (`:117`); `erpnext/.github/helper/install.sh:15-18` resolves `frappebranch=${FRAPPE_BRANCH:-$githubbranch}` then clones + `--frappe-path`.
- The `strategy.matrix.container: [1,2,3,4]` in erpnext (`:38-39`) / hrms / payments / lending is **test sharding**, not a version matrix — pair it with `run-parallel-tests --total-builds N --build-number ${{ matrix.container }}` (e.g. `erpnext ...:120`).

So a v15 compatibility matrix = `strategy.matrix.frappe_ref: [version-15, v15.0.0, v15.30.0, v15.67.0, ...]` driving a `FRAPPE_BRANCH`-style env var into an `install.sh` that does `git clone --branch "$FRAPPE_REF" --depth 1` + `bench init --frappe-path ~/frappe`, `fail-fast: false`, and a tightened `[tool.bench.frappe-dependencies] frappe = ">=15.0.0,<16.0.0"`.

---

## 6. ORDERED RENAME + BACKWARD-COMPAT CHECKLIST

Data-loss risk flags: 🔴 irreversible / 🟠 recoverable-but-painful.

**Phase A — unbreak the bench (do this first, today)**
1. **Restore importability.** Either (a) revert `sites/apps.txt` + `sites/apps.json` to `frappe_s3_attachment` *and* rename the dir back, or (b) complete the rename atomically per Phase B. Nothing else works until `frappe.get_module("<app>")` succeeds, because `frappe/__init__.py:258` → `:1670` → `:1513` runs on **every** `frappe.init()` for **every** site. 🟠
2. Refresh the editable install so `env/lib/python3.10/site-packages/*.pth` and `*.dist-info/direct_url.json` point at the real path (currently both say `/home/user/v15/apps/frappe_s3_attachment`).
3. Confirm `bench --site finstein.erp list-apps` succeeds before touching anything else.

**Phase B — code-side rename (no DB writes; safe because §1 proves 0 installs)**
4. `git mv frappe_s3_attachment/ cloud_file_storage/` (the inner package), update `pyproject.toml` `[project] name`, `hooks.py:3` `app_name`, `hooks.py:4` `app_title`, and `hooks.py:56-57,89-94` dotted paths (`after_install`, `after_migrate`, `doc_events.File.*`).
5. Rewrite `modules.txt` (`Frappe S3 Attachment` → `Cloud File Storage`) and move `<pkg>/frappe_s3_attachment/` → `<pkg>/cloud_file_storage/` so `get_module_name()` (`frappe/modules/utils.py:270-274`) resolves `cloud_file_storage.cloud_file_storage.doctype.<dt>.<dt>`. ⚠️ Do **not** leave a `Module Def` whose module is absent from `modules.txt` — `get_module_app` then throws `DoesNotExistError` (`utils.py:280`), which is spared by the reaper (`sync.py:166-167`) but leaves the doctype permanently unopenable.
6. Rewrite `patches.txt` (both entries) and the `patches/` package paths.
7. Update `.pre-commit-config.yaml:11` `files: "frappe_s3_attachment.*"` — **otherwise pre-commit silently checks nothing.**
8. Update `.gitignore:6`, `config/docs.py:5-6`, `README.md`, `CHANGELOG.md`.
9. Update `.github/workflows/ci.yml` — all 7 old-name references, the `concurrency.group`, `bench get-app`/`install-app`/`run-tests` args, the 5 `FRAPPE_S3_ATTACHMENT_MINIO_*` env vars, and the bucket name `frappe-s3-attachment-test`; mirror the renames into `tests/test_minio_integration.py:26-30`.
10. **Pin frappe in CI** — add `git clone https://github.com/frappe/frappe --branch "$FRAPPE_REF" --depth 1` + `bench init --frappe-path ~/frappe` per `hrms/.github/helper/install.sh:13-14`; tighten `[tool.bench.frappe-dependencies]` to `<16.0.0`; add `strategy.matrix.frappe_ref` with `fail-fast: false`.
11. Delete or replace the dead `tox.ini` (`[flake8] max-line-length = 90`, contradicting ruff's 110).
12. Fix `sites/apps.txt` + `sites/apps.json` + reinstall editable package under the new name.

**Phase C — the compat patch (ship it, but make it a guarded no-op here)**
13. First line of every rename patch: `if "cloud_file_storage" in frappe.get_installed_apps(): return` — the exact guard used by `erpnext/patches/v15_0/remove_loan_management_module.py:5-6` and `erpnext/patches/v14_0/remove_hr_and_payroll_modules.py:5`. Second guard: `if not frappe.db.exists("Module Def", "Frappe S3 Attachment"): return`. On all 13 live sites here both short-circuit immediately.
14. 🔴 **Register the patch as `pre_model_sync`.** If it runs `post_model_sync` (`frappe/migrate.py:124-126`), `sync_all` has already executed and `remove_orphan_doctypes()` fires at `frappe/migrate.py:148` in the *same* migrate. With `tabDocType.module = "Frappe S3 Attachment"` still resolving to an app whose controller file has moved, `get_controller` raises `ImportError` (`frappe/model/base_document.py:96` → `frappe/modules/utils.py:262-265`) and `frappe/model/sync.py:174` `frappe.delete_doc("DocType", name, force=True, ignore_missing=True)` executes. Collateral: `__Auth` passwords destroyed (`delete_doc.py:64`) — **`secret_key` unrecoverable**; Custom Fields of table type deleted (`:84-86`); all child rows deleted (`delete_doc.py:226`); `tabSingles` rows orphaned (`:207-210`). **This is the single highest-risk step in the entire rename.** 🔴
15. Rename the Module Def **before** anything reads `tabDocType.module`: `frappe.rename_doc("Module Def", "Frappe S3 Attachment", "Cloud File Storage", force=True, ignore_permissions=True)` — this cascades to `tabDocType.module`, `tabCustom Field.module`, `tabProperty Setter.module`, Report/Page/Workspace/Web Form/Print Format via `rename_doc.py:182-183`. ⚠️ Under this bench's `developer_mode: 1`, `ModuleDef.on_update` (`module_def.py:29-35`) will `mkdir` a module folder and append to `modules.txt` on disk (`:37-57`).
16. Then `frappe.db.set_value("Module Def", "Cloud File Storage", "app_name", "cloud_file_storage")` — **not** covered by `rename_doc`, and required by `get_doctype_app_map()` (`frappe/modules/utils.py:284-294`).
17. Rename the doctypes as Administrator, **parent before child**: `S3 File Attachment` → new name, then `S3 Ignored DocType Row` → new name. Singles data is preserved by `doctype.py:653-659`; `__Auth` secrets preserved by `rename_doc.py:209-210`. `frappe.flags.in_patch` suppresses the `shutil.move` at `doctype.py:665-667`. Child-first ordering breaks `update_parenttype_values` (`rename_doc.py:623-649` filters on `{"parent": new}`). 🟠
18. Rewrite `installed_apps`: read `frappe.db.get_global("installed_apps")`, swap the element, `frappe.db.set_global(...)` (mirroring `installer.py:347`), then `_clear_cache("__global")` as `installer.py:363` does — `get_installed_apps` is `@request_cache` (`frappe/__init__.py:1540`).
19. Rewrite `` `tabPatch Log`.patch `` prefix `frappe_s3_attachment.patches.` → `cloud_file_storage.patches.`. `patch_handler.py:56,75` matches literal strings; skipping this **re-runs every historic patch**. 🟠
20. 🔴 **Rewrite `tabFile.file_url`** for every private object: `/api/method/frappe_s3_attachment.controller.generate_file?key=...` → the new dotted path (written at `controller.py:243-250`). Skipping this **404s every private attachment**. At 1.2M rows this must be batched, resumable, idempotent, and paired with (a) keeping a thin `frappe_s3_attachment.controller.generate_file`-compatible whitelisted alias alive for at least one release, and (b) updating the detector regex `controller.py:291` to accept **both** prefixes so `run_migrate_existing_files` (`controller.py:294-307`) does not re-upload already-migrated files.
21. **Drain the RQ queues before the rename.** In-flight jobs serialize `"frappe_s3_attachment.controller.run_migrate_existing_files"` (`controller.py:322-323`) and the job id `"frappe_s3_attachment.migrate_existing_files"` (`controller.py:19`); post-rename they `ModuleNotFoundError` in the worker. Directly relevant to the planned dedicated `cloud_migration` queue. 🟠
22. If/when scheduler events are added: expect `clear_events` (`scheduled_job_type.py:282-293`) to delete every `Scheduled Job Type` whose `method` is no longer in hooks, and its `on_trash` (`:186`) to delete the associated `Scheduled Job Log` history. Rename `method` in the patch rather than letting it be reaped. 🟠
23. Custom Field `File-s3_object_key` (`install.py:41-62`) and index `s3_object_key_index` (`install.py:65-79`): the `module` value auto-follows step 15, but the **fieldname and DB column do not**. Decide explicitly whether to keep `s3_object_key` (recommended — zero risk) or rename it (requires an `ALTER TABLE` + Custom Field rename + rewriting `controller.py:249`, `controller.py:274`, and `run_migrate_existing_files`'s `["s3_object_key", "is", "not set"]` filter at `controller.py:301`). 🟠
24. Regenerate `locale/main.pot` / `locale/de.po` (78 combined old-name occurrences).

**Phase D — compatibility guardrails (non-negotiable for this codebase)**
25. 🔴 **`File.get_content` signature.** Frappe v15 defines `def get_content(self) -> bytes:` with **no kwargs** (`frappe/core/doctype/file/file.py:566`). India Compliance calls `file.get_content(encodings=[])` at `india_compliance/india_compliance/gst_india/doctype/gst_return_log/gst_return_log.py:188` (and bare `file.get_content()` at `:55`, `:98`). India Compliance is installed on **7 of 13 live sites** (circuits.local, erp.local, finstein.erp, kaynes.site, kaynes.test, learning.local, skc.erp). The DFP reference app repeats the v15 signature exactly — `dfp_external_storage_reference/.../dfp_external_storage.py:577`: `def get_content(self) -> bytes:` — which is precisely why `get_content(encodings=[])` blew up under it. **Any `File` override in `cloud_file_storage` MUST accept `encodings` (and ideally `*args, **kwargs`) and MUST return raw `bytes` unchanged.** Add a regression test asserting `get_content(encodings=[])` returns byte-identical content.
26. Note frappe's own `get_content` opportunistically `.decode()`s (`file.py:585-590`) and returns `str` for text files — any replacement must preserve that exact behavior, not "fix" it, or GST JSON decompression (`gst_return_log.py:55,98` via `get_decompressed_data`) breaks.
27. If you register `override_doctype_class` for `File`, be aware `remove_orphan_doctypes` explicitly skips overridden doctypes (`frappe/model/sync.py:157,160-161`) — useful shielding, but it also means an override masks controller-import breakage from the reaper's detection.
28. `kaynes.site` and `kaynes.test` share database `_b4dd58d01acce279` — never treat them as independent test targets.
29. `sites/skc.local` has no database (`Unknown database '_03764da98a88aac0'`) — exclude it from any bench-wide loop or the loop aborts.