"""Apply and verify the A12 permlevel separation on the backup doctypes (M-4, M-5, M-9).

Two doctypes, one rule: a field at `permlevel: 1` with **no** level-1 DocPerm locks everyone
out of it, and a level-1 DocPerm granted to the operator role hands over exactly what the
level was added to withhold. So this reloads both doctypes and asserts **three** things,
failing the migrate loudly on any of them:

* the fields carry the level (positive);
* a level-1 row exists for System Manager (positive — without it, nobody can read them);
* the operator role holds **no** level-1 row (negative — the one that actually protects the
  credential, and the one the first version of this patch was missing).

**Why `Custom DocPerm` is queried separately.** `patch_handler` sets `frappe.flags.in_patch`,
and `meta.py` returns early on that flag *before* applying the `Custom DocPerm` override — so
inside a patch, `meta.permissions` is always the shipped JSON rows. That is right for catching
a JSON regression and blind to the rows that actually govern any site where Role Permission
Manager has ever touched these doctypes, because frappe copies every DocPerm into
`Custom DocPerm` on first edit. Checking only the meta would mean a silent success on exactly
the upgrade path this patch exists for: the JSON is fixed, the assertion reads the JSON,
`bench migrate` passes, and the permission is still held.

The two checks get different remediation, because they have different fixes. A JSON problem is
fixed in the JSON; a `Custom DocPerm` problem is fixed in **Role Permission Manager**, and
telling an operator to edit JSON that is no longer authoritative for their site is worse than
saying nothing.
"""

import frappe

OPERATOR_ROLE = "Cloud Storage Manager"
MANAGER_ROLE = "System Manager"

CREDENTIAL_FIELDS = {
	"Cloud Backup Settings": ("access_key", "secret_key"),
	"Cloud Storage Backup Log": (
		"db_key",
		"config_key",
		"public_files_key",
		"private_files_key",
		"backup_bucket",
		"sha256_manifest",
	),
}


def execute():
	for doctype, fieldnames in CREDENTIAL_FIELDS.items():
		# `force=True` because a reload that skips on an unchanged hash would leave the
		# assertions below reading whatever the site already had.
		frappe.reload_doc("cloud_file_storage", "doctype", frappe.scrub(doctype), force=True)
		meta = frappe.get_meta(doctype)

		_assert_fields_are_level_one(doctype, fieldnames, meta)
		_assert_shipped_permissions(doctype, meta)
		_assert_custom_permissions(doctype)


def _assert_fields_are_level_one(doctype: str, fieldnames: tuple[str, ...], meta):
	for fieldname in fieldnames:
		field = meta.get_field(fieldname)
		if field is None:
			frappe.throw(f"{doctype}.{fieldname} is missing; the permlevel patch cannot verify it")
		if int(field.permlevel or 0) != 1:
			frappe.throw(
				f"{doctype}.{fieldname} is at permlevel {field.permlevel!r}, expected 1. "
				"A12 keeps credentials and artifact keys away from the operator role. "
				f"Fix: set permlevel 1 on that field in {frappe.scrub(doctype)}.json."
			)


def _assert_shipped_permissions(doctype: str, meta):
	"""The DocPerm rows as shipped. Inside a patch this is always the JSON (see module docstring)."""
	level_one = [perm for perm in meta.permissions if int(perm.permlevel or 0) == 1]

	if not level_one:
		frappe.throw(
			f"{doctype} has permlevel-1 fields and no permlevel-1 DocPerm, which locks every "
			"role out of them. Both halves ship together or neither does. "
			f"Fix: add a permlevel-1 row for {MANAGER_ROLE} in {frappe.scrub(doctype)}.json."
		)
	if not any(perm.role == MANAGER_ROLE and perm.read for perm in level_one):
		frappe.throw(
			f"{doctype} has no permlevel-1 read for {MANAGER_ROLE}. "
			f"Fix: add `read` to that row in {frappe.scrub(doctype)}.json."
		)

	offenders = [perm.role for perm in level_one if perm.role == OPERATOR_ROLE]
	if offenders:
		frappe.throw(
			f"{doctype} grants {OPERATOR_ROLE} a permlevel-1 DocPerm, which hands that role "
			"the credentials and artifact keys permlevel 1 exists to withhold (A12). "
			f"Fix: remove that row from {frappe.scrub(doctype)}.json."
		)


def _assert_custom_permissions(doctype: str):
	"""The rows that actually govern a site that has used Role Permission Manager.

	Invisible to `meta.permissions` during a patch, and therefore invisible to every
	assertion above.
	"""
	rows = frappe.get_all(
		"Custom DocPerm",
		filters={"parent": doctype, "role": OPERATOR_ROLE, "permlevel": 1},
		fields=["name", "read", "write"],
	)
	if rows:
		frappe.throw(
			f"{doctype} has a Custom DocPerm granting {OPERATOR_ROLE} permlevel 1 "
			f"({', '.join(row.name for row in rows)}). This overrides the shipped rows on this "
			"site, so the credentials and artifact keys are readable by the operator role. "
			"Fix: remove that permission in Role Permission Manager — editing the DocType JSON "
			"will NOT help, because Custom DocPerm takes precedence once it exists."
		)
