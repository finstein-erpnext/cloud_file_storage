# Installation

Pick the branch that matches your bench's Frappe major. **Do this deliberately** — see the
warning below.

**Frappe v15 bench** (Python 3.10–3.13):

```bash
bench get-app --branch version-15 https://github.com/finstein-erpnext/cloud_file_storage
bench --site <site> install-app cloud_file_storage
```

**Frappe v16 bench** (Python 3.14):

```bash
bench get-app --branch version-16 https://github.com/finstein-erpnext/cloud_file_storage
bench --site <site> install-app cloud_file_storage
```

> **The Frappe dependency declaration is advisory.** The app declares its supported Frappe
> range under `[tool.bench.frappe-dependencies]` in `pyproject.toml`, but bench treats a
> mismatch as a **warning and installs anyway**. Nothing stops you putting the `version-15` app
> on a v16 bench; you get a warning in the install output and a broken app afterwards. Choose
> the branch yourself — do not rely on the tool to choose it for you.

After installation the app sits in `LOCAL_ONLY` mode, which is byte-identical to core Frappe
behaviour. Nothing is sent anywhere until you configure a bucket and change the mode.

**Deployment prerequisites** — the dedicated `cloud_migration` RQ queue, bucket and IAM
settings, and the procedure for adopting an existing `frappe_s3_attachment` 0.2.x install — are
in [docs/runbooks/deployment.md](docs/runbooks/deployment.md).


---

Configuration, the dedicated `cloud_migration` queue, bucket and IAM settings are in
[runbooks/deployment.md](runbooks/deployment.md). Migrating an existing site's attachments
is in [runbooks/migration.md](runbooks/migration.md).
