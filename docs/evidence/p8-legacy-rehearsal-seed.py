import os
import sys
import traceback

os.chdir("/home/user/v15/sites")
# L-11: derived from this file's own location, so the harness keeps working after the phase
# worktree is removed at merge — which is when it becomes the only record of the run.
sys.path.insert(
	0, os.environ.get("APP_ROOT") or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
)
sys.path.insert(0, "/home/user/v15/apps/frappe")
import frappe

frappe.init(site="cfs-p8-legacy.local", sites_path="/home/user/v15/sites")
frappe.connect()
frappe.set_user(  # nosemgrep: frappe-setuser
	"Administrator"
)
try:
	from cloud_file_storage.tests import legacy_fixture

	print(legacy_fixture.seed())
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
except Exception:
	traceback.print_exc()
	frappe.db.rollback()
finally:
	frappe.destroy()
