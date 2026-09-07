import json
import os
import sys

os.chdir("/home/user/v15/sites")
# L-11: derived from this file's own location, so the harness keeps working after the phase
# worktree is removed at merge — which is when it becomes the only record of the run.
sys.path.insert(
	0, os.environ.get("APP_ROOT") or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
)
sys.path.insert(0, "/home/user/v15/apps/frappe")
import frappe

frappe.init(
	site=sys.argv[1] if len(sys.argv) > 1 else "cfs-p8-legacy.local", sites_path="/home/user/v15/sites"
)
frappe.connect()
from frappe.utils.password import get_decrypted_password

out = {  # nosemgrep: frappe-breaks-multitenancy
	"installed_apps": json.loads(frappe.db.get_global("installed_apps") or "[]"),
	"module_def_app": frappe.db.get_value("Module Def", "Frappe S3 Attachment", "app_name"),
	"our_singles": dict(frappe.db.get_singles_dict("Cloud Storage Settings")),
	"fork_singles": dict(frappe.db.get_singles_dict("S3 File Attachment")),
	"our_secret_set": bool(
		get_decrypted_password(
			"Cloud Storage Settings", "Cloud Storage Settings", "secret_access_key", raise_exception=False
		)
	),
	"fork_secret_set": bool(
		get_decrypted_password(
			"S3 File Attachment", "S3 File Attachment", "secret_key", raise_exception=False
		)
	),
	"fork_patch_log": frappe.db.count("Patch Log", {"patch": ("like", "frappe_s3_attachment%")}),
	"our_patch_log": frappe.db.count("Patch Log", {"patch": ("like", "cloud_file_storage%")}),
	"cso_table": frappe.db.table_exists("Cloud Storage Object"),
	"fork_doctype_rows_present": [
		d for d in ("S3 File Attachment", "S3 Ignored DocType Row") if frappe.db.exists("DocType", d)
	],
	"fork_child_rows": frappe.db.sql("SELECT doctype_name FROM `tabS3 Ignored DocType Row`", pluck=True)
	if frappe.db.table_exists("S3 Ignored DocType Row")
	else None,
}
if out["cso_table"]:
	out["csos"] = frappe.db.get_all(  # nosemgrep: frappe-breaks-multitenancy
		"Cloud Storage Object",
		fields=[
			"name",
			"s3_key",
			"status",
			"visibility",
			"bucket",
			"content_sha256",
			"content_hash_md5",
			"file_size",
		],
	)
cols = ["name", "file_name", "file_url", "is_private", "content_hash"]
for extra in ("s3_object_key", "cloud_storage_object"):
	if frappe.db.has_column("File", extra):
		cols.append(extra)
out["files"] = frappe.db.sql(  # nosemgrep: frappe-breaks-multitenancy
	"SELECT " + ", ".join(cols) + " FROM `tabFile` ORDER BY file_name", as_dict=True
)
if frappe.db.table_exists("Cloud Storage Ignored DocType"):
	out["ignored"] = frappe.db.get_all(  # nosemgrep: frappe-breaks-multitenancy
		"Cloud Storage Ignored DocType", filters={"parent": "Cloud Storage Settings"}, pluck="doctype_name"
	)
print(json.dumps(out, indent=2, default=str))
frappe.destroy()
