# Desk smoke test (Playwright)

`cloud_file_storage/tests/playwright/smoke.py` drives the P7 Desk surfaces in a real
browser: the Settings form loads, the storage-health panel renders, Test Connection paints
its per-check result, and the mission buttons take a campaign from **Analyze → Plan → Start
→ Pause** over a 100-file fixture. Every state change goes through a click; the script never
calls `api.admin` directly and never writes to the database to make an assertion pass.

It is **not** part of `bench run-tests`. It needs a served site, a worker and a bucket, and a
python suite that silently skipped when those were missing would be worse than no smoke at
all — the point of this test is that it exercises the things a headless assertion cannot.

## Prerequisites

Scratch site only. Never run this against a site with real data.

1. **A bucket.** Local MinIO is enough:
   `docker run -d -p 9000:9000 quay.io/minio/minio:latest server /data --address ":9000"`,
   then create a bucket and point `Cloud Storage Settings` at it (endpoint `http://…` is
   accepted only on a site with `allow_tests`).

2. **The `cloud_migration` queue, and a worker consuming it.** The campaign's jobs are
   enqueued, not run inline — `migration.api.start_analysis` refuses outright when the
   dedicated queue is missing (invariant 8). Declare it on the **site**, not on the bench,
   if the bench hosts anything you do not own:

   ```bash
   bench --site <scratch> set-config -p workers '{"cloud_migration": {"timeout": 1500}}'
   ```

   `bench worker --queue cloud_migration` validates the queue name against the bench-wide
   `common_site_config.json` and will refuse a site-scoped declaration. On a bench where
   editing the shared config is not acceptable, run a worker directly instead:

   ```python
   import os; os.chdir("<bench>/sites")
   import frappe
   from frappe.utils.background_jobs import generate_qname, get_redis_conn
   from rq import Worker

   with frappe.init_site():
       connection, queue = get_redis_conn(), generate_qname("cloud_migration")
   Worker([queue], connection=connection).work()
   ```

3. **The fixture** — 100 File rows with bytes on disk and no Cloud Storage Object. Create
   them in `LOCAL_ONLY`, then switch the mode: a file created while the mode is already
   `S3_PRIMARY_LOCAL_FALLBACK` is uploaded on insert and there is nothing left to migrate.

   ```python
   for index in range(100):
       frappe.get_doc({
           "doctype": "File",
           "file_name": f"p7-smoke-{index:03d}.txt",
           "content": f"p7 smoke fixture object number {index}\n".encode() * 8,
           "is_private": 1,
       }).insert(ignore_permissions=True)
   ```

4. **A served site**: `bench --site <scratch> serve --port 8007`.

## Running

```bash
python3 -m cloud_file_storage.tests.playwright.smoke \
    --base-url http://127.0.0.1:8007 --password admin --artefacts /tmp/cfs-shots
```

Screenshots land in `--artefacts` at each step, and on failure. `--headed` watches it live.

`locale="en-US"` is set on the browser context on purpose: frappe's desk bundle builds an
`Intl.Locale` from the browser's, and a headless chromium started without one throws
`RangeError: Incorrect locale information provided` while wiring keyboard shortcuts, leaving
the whole form region hidden. Every later assertion then fails for a reason unrelated to
this app.

## Evidence

`--artefacts <dir>` writes a screenshot per step and `smoke-evidence.json`, which records the
campaign counters **read off the rendered form** (`cur_frm.doc`) at each stage — analyzed,
planned, started, paused — plus the health-panel and connection-panel text. Reading the
counters from the Desk rather than querying the database is the point: what a browser smoke
adds is that the numbers an operator can see are real, and a SQL count would be evidence about
the database, which the python suite already covers.

## Known environment fault: the Desk loops between /app and the setup wizard

**On this bench the Desk does not boot, and it is not this app.** Symptom: `/app` and
`/app/setup-wizard/0` alternate indefinitely (20–40 full page loads in a few seconds) and the
layout never renders.

**Controlled.** Reproduced on a brand-new `bench new-site` with **frappe only and no apps
installed at all** — 26 navigations, 12 wizard bounces, no layout. `cloud_file_storage` is not
involved, and a site carrying it behaves identically to one that does not.

**A wrong lead, recorded so nobody re-walks it.** The `frappe` checkout here carries uncommitted
local modifications including `frappe/app.py`, which contains a patch redirecting requests whose
resolved site is `localhost`/`127.0.0.1` to `common_site_config.default_site`. That looks like
the cause and is not. The line reads
`site = _site or request.headers.get("X-Frappe-Site-Name") or get_site_name(request.host)`, and
`bench --site X serve` sets `_site`, which wins outright — so `site` is never `localhost` on a
server started that way and the block never runs. Measured rather than argued: the served page
is **byte-identical with and without** an `X-Frappe-Site-Name` header and both carry
`"sitename": "<the site bench serve was started for>"`. Setting that header in the browser
context does **not** lift the loop. The modified files also predate this work by months
(`app.py` 2026-01-02), so they are not what changed.

**The flag that actually governs it is not the obvious one.** `router.js:147-151` redirects
whenever `frappe.boot.setup_complete` is falsy, and that comes from `frappe.is_setup_complete()`
(`frappe/__init__.py:2420-2432`), which reads **`Installed Application.is_setup_complete`** for
`frappe`/`erpnext`. It does **not** read `System Settings.setup_complete` and it does **not**
read `sys_defaults`. Setting either of those — the two things an operator reaches for first —
changes nothing about the redirect:

```python
for row in frappe.get_all("Installed Application", fields=["name", "app_name"]):
    if row.app_name in ("frappe", "erpnext"):
        frappe.db.set_value("Installed Application", row.name, "is_setup_complete", 1)
frappe.db.commit(); frappe.clear_cache()
```

That takes a fresh site from "settles on the wizard" (correct behaviour for an incomplete
setup) to the `/app` ⇄ wizard oscillation, which is the fault proper.

**What remains unexplained.** On a site in this state the served boot payload carries
`"setup_complete": true` at top level, `frappe.is_setup_complete()` returns True, and
`Installed Application` has `frappe=1` — yet the browser still oscillates. `router.js:147-151`
routes to the wizard **only** when `frappe.boot.setup_complete` is falsy, so something other
than that branch is driving the redirect, and it has not been found. Stated as observed: no
cause is claimed, and "transient" would be a guess rather than a finding.

**Consequence for this smoke:** it cannot run on this bench until the Desk boots. That is an
environment condition and not a code one — the frappe-only control proves the app is not
involved — but the cause is genuinely unknown, and the next step lies inside frappe's own desk
bundle in a shared checkout serving thirteen business sites, which is out of scope to change.

## Afterwards — remove the fixture, not just the campaign## Afterwards — remove the fixture, not just the campaign

Two separate cleanups, and the second is the one that is easy to miss.

**The campaign.** The run ends **Paused**, which is one of `ACTIVE_STATUSES`, and at most one
campaign may be active at a time. Left there, the next `bench run-tests` on this site fails
with "Campaign … is already active".

**The 100 files.** Several migration suites analyse the *whole* `tabFile` table, so a hundred
leftover fixture rows change what they measure. Observed on this bench: with the fixture still
present, `test_migration_adoption`, `test_migration_reconcile` and `test_migration_resilience`
produced four failures that vanished on purging it, and their runtimes fell from 25.6s/49.6s/
73.6s to 3.6s/5.6s/7.3s. Nothing was wrong with those tests or with the app — the site simply
was not the site they were written against.

```python
for campaign in frappe.db.get_all(
    "Cloud Migration Campaign", filters={"title": ("like", "P7 smoke%")}, pluck="name"
):
    for doctype in (
        "Cloud Migration File Ref",
        "Cloud Migration Object",
        "Cloud Migration Batch",
        "Cloud Migration Conflict",
        "Cloud Storage Audit Log",
    ):
        frappe.db.delete(doctype, {"campaign": campaign})
    frappe.db.delete("Cloud Migration Campaign", {"name": campaign})

for name in frappe.db.get_all("File", filters={"file_name": ("like", "p7-smoke-%")}, pluck="name"):
    frappe.delete_doc("File", name, force=True, ignore_permissions=True, delete_permanently=True)

frappe.db.commit()
```
