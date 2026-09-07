"""Does the adopted credential survive an ordinary save of the settings document?"""

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

frappe.init(site=sys.argv[1], sites_path="/home/user/v15/sites")
frappe.connect()
frappe.set_user(  # nosemgrep: frappe-setuser
	"Administrator"
)
from frappe.utils.password import get_decrypted_password

before = get_decrypted_password(
	"Cloud Storage Settings", "Cloud Storage Settings", "secret_access_key", raise_exception=False
)
settings = frappe.get_single(  # nosemgrep: frappe-breaks-multitenancy
	"Cloud Storage Settings"
)
settings.flags.ignore_mandatory = True
settings.save(ignore_permissions=True)
frappe.db.commit()  # nosemgrep: frappe-manual-commit
after = get_decrypted_password(
	"Cloud Storage Settings", "Cloud Storage Settings", "secret_access_key", raise_exception=False
)
print(
	json.dumps(
		{
			"secret_before_save": bool(before),
			"secret_after_save": bool(after),
			"unchanged": before == after,
			"singles_placeholder": frappe.db.get_singles_dict("Cloud Storage Settings").get(
				"secret_access_key"
			),
		},
		indent=2,
	)
)
frappe.destroy()
