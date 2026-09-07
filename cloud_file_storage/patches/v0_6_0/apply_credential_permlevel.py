"""A12's deferred item: `permlevel: 1` on the credential fields, with the DocPerms to match.

Deferred out of P1 because a permlevel with no level-1 DocPerm row locks *everyone* out of
the field, including the role that has to configure it — and the roles were P7's to wire
(DECISIONS.md 2026-08-14).

Why a patch rather than trusting the DocType sync: an existing site's `tabDocPerm` rows were
written by the previous version of the JSON, and `frappe.reload_doc` is what replaces them.
Custom DocPerms an operator added by hand are left alone — the reload only rewrites the rows
this app ships — and the assertion at the end fails the migrate loudly rather than leaving a
site where the secret is readable by a role that should not see it.
"""

import frappe
from frappe import _

DOCTYPE = "Cloud Storage Settings"
CREDENTIAL_FIELDS = ("access_key_id", "secret_access_key")
OPERATOR_ROLE = "Cloud Storage Manager"


def execute():
	frappe.reload_doc("cloud_file_storage", "doctype", "cloud_storage_settings", force=True)
	frappe.clear_cache(doctype=DOCTYPE)

	meta = frappe.get_meta(DOCTYPE)
	for fieldname in CREDENTIAL_FIELDS:
		field = meta.get_field(fieldname)
		if not field or int(field.permlevel or 0) != 1:
			frappe.throw(
				f"{DOCTYPE}.{fieldname} did not come back from the sync at permlevel 1; "
				"the credential fields would be readable at level 0."
			)

	level_one_roles = {perm.role for perm in meta.permissions if int(perm.permlevel or 0) == 1}
	if "System Manager" not in level_one_roles:
		frappe.throw(
			_("{0} has no permlevel-1 DocPerm for System Manager.").format(DOCTYPE)
			+ " "
			+ _("With the fields at permlevel 1 and no matching row, nobody could configure the credentials.")
		)
	if OPERATOR_ROLE in level_one_roles:
		frappe.throw(
			_("{0} holds a permlevel-1 DocPerm on {1}.").format(OPERATOR_ROLE, DOCTYPE)
			+ " "
			+ _(
				"That role exists precisely so storage can be operated without credential access (design 3.2)."
			)
		)

	return assert_operator_holds_no_write(meta)


#: The three capabilities that, at permlevel 0, hand the operator role every field outside the
#: two credential ones.
GUARDED_CAPABILITIES = ("write", "create", "delete")

_JSON_REMEDY = (
	"TO FIX: in cloud_storage_settings.json, the "
	'{"role": "Cloud Storage Manager"} permission row must carry read/report/email/print '
	"and NO write, create or delete. This most often fires after a merge resolved that "
	"permissions array by taking both sides. Restore the read-only row and re-run bench migrate."
)

_CUSTOM_REMEDY = (
	"TO FIX: this is a Custom DocPerm row, so editing cloud_storage_settings.json will NOT "
	"clear it — the customised rows override the shipped ones. Open Role Permission Manager "
	"for Cloud Storage Settings, remove write/create/delete from the Cloud Storage Manager "
	"row at level 0 (or delete that row to fall back to the shipped permissions), then re-run "
	"bench migrate."
)


def _granted(row) -> list[str]:
	return [capability for capability in GUARDED_CAPABILITIES if int(row.get(capability) or 0)]


def assert_operator_holds_no_write(meta=None):
	"""P7-C1, enforced at migrate time rather than left to a reviewer reading an array.

	permlevel 1 protects only `access_key_id` and `secret_access_key`. A level-0 write
	therefore covers `bucket` and `endpoint_url` — where every attachment is sent — as well as
	`operation_mode` and `object_delete_grace_days`, none of which pass any endpoint this app
	gates. The realistic way that comes back is a merge resolving the permissions array by
	taking both sides, which no behavioural test catches unless somebody runs it afterwards
	and thinks to look.

	**Both stores are checked, because only one of them governs at a time.** `frappe.get_meta`
	swaps `self.permissions` for `Custom DocPerm` rows whenever any exist
	(`frappe/model/meta.py:542-554`) — except it returns early while `frappe.flags.in_patch` is
	set, which is precisely when this runs. So the meta read below genuinely sees the shipped
	JSON, and is genuinely blind to the rows that actually apply on a site whose permissions
	have ever been edited: Role Permission Manager copies every DocPerm into Custom DocPerm on
	the first edit to a doctype. A site that installed a pre-fix P7 and then touched
	permissions here has `write: 1` frozen in Custom DocPerm, and correcting the JSON would
	leave it in place while this assertion passed — the guard for C-1 reporting green on the
	one path it exists to protect. Hence the second query, with its own remedy: telling an
	operator to edit JSON that is no longer authoritative would be worse than saying nothing.
	"""
	meta = meta or frappe.get_meta(DOCTYPE)

	for perm in meta.permissions:
		if perm.role != OPERATOR_ROLE or int(perm.permlevel or 0) != 0:
			continue
		granted = _granted(perm)
		if granted:
			frappe.throw(
				f"P7-C1: {OPERATOR_ROLE} holds {', '.join(granted)} on {DOCTYPE} at permlevel 0.\n\n"
				f"{_JSON_REMEDY}"
			)

	for perm in frappe.get_all(
		"Custom DocPerm",
		filters={"parent": DOCTYPE, "role": OPERATOR_ROLE, "permlevel": 0},
		fields=["name", *GUARDED_CAPABILITIES],
	):
		granted = _granted(perm)
		if granted:
			frappe.throw(
				f"P7-C1: {OPERATOR_ROLE} holds {', '.join(granted)} on {DOCTYPE} at permlevel 0 "
				f"via Custom DocPerm {perm.name}.\n\n{_CUSTOM_REMEDY}"
			)
