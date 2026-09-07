"""A synthetic `frappe_s3_attachment` 0.2.x site, built from the fork's own schema (A21).

The bootstrap in `cloud_file_storage/legacy_install.py` is only as good as the site it is
proven on, and the fork is no longer on this bench to install. So this module recreates the
DB state a 0.2.x install leaves behind, taken from the fork's own doctype JSON at the last
commit before the P1 rename (`6eb9acc^`) and from `controller.py`'s upload path:

* `Module Def` "Frappe S3 Attachment", `app_name` pointing at the vanished package — the
  thing that makes `bench migrate` raise;
* the `S3 File Attachment` Single and its `S3 Ignored DocType Row` child table, with real
  values and a real encrypted `secret_key`;
* the `File-s3_object_key` Custom Field the fork's `install.py` created, owned by the fork's
  module;
* File rows of **both** fork generations: the later one that stored the object key in
  `s3_object_key`, and the earlier one that overloaded `content_hash` with it (which is what
  `patches/v0_2_0/backfill_s3_object_key.py` exists to repair);
* a plain local File that the fork never touched, as the control that must come through
  adoption completely unchanged;
* a fork `Patch Log` row, and `frappe_s3_attachment` in the `installed_apps` global.

Two pieces of scaffolding are needed because the fork's *code* is genuinely absent:

* **The site must have `developer_mode: 0`.** This bench's `common_site_config.json` sets it
  to 1, and with it on `_seed_module_def` dies before anything else: `ModuleDef.on_update`
  calls `create_modules_folder()` whenever `not self.custom and frappe.conf.get(
  "developer_mode")` (`frappe/core/doctype/module_def/module_def.py:29-35`), which resolves
  `get_app_path("frappe_s3_attachment")` for an app that is not there. `frappe.flags.in_patch`
  does **not** help with that one — it is the bypass for `DocType.check_developer_mode`
  (`frappe/core/doctype/doctype/doctype.py:329-338`), which is a different guard. Every
  rehearsal site carries the site-level override, and `docs/evidence/p8-legacy-rehearsal.sh`
  sets it; the independent test-engineer found this by hitting it, because an earlier version
  of the evidence document did not say so.
* DocTypes are therefore written with `frappe.flags.in_patch` for `check_developer_mode`, and
  with `developer_mode: 0` doing the rest — which also stops `DocType.on_update` exporting JSON
  into an app directory that does not exist.
* `load_doctype_module` is stubbed for the two inserts. `DocType.on_update` calls it to ask
  whether the doctype's python module defines `on_doctype_update`
  (`doctype.py:536,634`), and resolution goes through `frappe.local.module_app` — a map built
  from the `modules.txt` of every app **on the bench** (`frappe/__init__.py:1649-1690`), not
  from `Module Def`. A module whose app is gone is simply not in it. That is also why every
  read in `legacy_install.harvest_settings` is meta-free: on a real adopted site the fork's
  Single cannot be loaded through the ORM at all.
"""

from contextlib import contextmanager
from types import ModuleType
from unittest.mock import patch

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

LEGACY_APP = "frappe_s3_attachment"
LEGACY_MODULE = "Frappe S3 Attachment"
LEGACY_SETTINGS_DOCTYPE = "S3 File Attachment"
LEGACY_IGNORED_DOCTYPE = "S3 Ignored DocType Row"

LEGACY_ENDPOINT = "/api/method/frappe_s3_attachment.controller.generate_file"

#: What the fork's Settings Single held. Field names are the fork's, not ours.
LEGACY_SETTINGS = {
	"bucket_name": "legacy-fork-bucket",
	"region_name": "ap-south-1",
	"endpoint_url": "https://s3.ap-south-1.amazonaws.com",
	"folder_name": "attachments",
	"access_key": "AKIALEGACYFORKKEY",
	"signed_url_expiry_time": "900",
	"delete_file_from_cloud": "1",
	"timeout_for_migration_job": "1500",
}
LEGACY_SECRET = "legacy-fork-secret-value"
#: One row this app already seeds by default, and one it does not -- without the second, the
#: adoption of the fork's list would be indistinguishable from `install.py` seeding its own.
#: The second is *probed* rather than named: frappe moved Blog out of core for v16
#: (`64db88228f`), so the literal "Blog Post" is not a DocType there. `Blog Post` leads the
#: candidates, so a v15 site picks exactly what it picked before.
LEGACY_SEEDED_IGNORED_ROW = "Data Import"
UNSEEDED_IGNORED_ROW_CANDIDATES = ("Blog Post", "Web Page", "Note", "ToDo")


def unseeded_ignored_row() -> str:
	"""The single DocType both the fixture and the L-12 class use as the fork's extra row.

	One resolver, deliberately: two probes with different rules can resolve to different
	names, and the cleanup that deletes only one of them then leaks the other into
	`ignored_doctypes` -- which is exactly what broke
	`test_the_seeded_defaults_are_still_in_place` when this was first made version-aware.
	Deterministic (first candidate that exists), so every caller agrees.
	"""
	for name in UNSEEDED_IGNORED_ROW_CANDIDATES:
		if frappe.db.exists("DocType", name):
			return name
	raise AssertionError(
		f"none of {UNSEEDED_IGNORED_ROW_CANDIDATES} exists on this site, so no fork row can "
		"be adopted and the L-12 premise cannot be armed"
	)


def legacy_ignored_rows() -> tuple[str, ...]:
	"""The fork's ignored-doctype list, valid on whichever frappe is installed.

	Raises rather than falling back to a single row: the whole point of the second entry is
	that it is one this app does not seed itself, so a fixture that quietly dropped it would
	make every adoption assertion pass without distinguishing adoption from seeding.
	"""
	return (LEGACY_SEEDED_IGNORED_ROW, unseeded_ignored_row())


#: The fork's key shape: `{folder_name}/{YYYY}/{MM}/{DD}/{doctype}/{rand}_{filename}`.
NEW_GEN_KEY = "attachments/2021/07/14/Sales Invoice/2f9a1c_invoice.txt"
OLD_GEN_KEY = "attachments/2019/03/02/Purchase Invoice/91bd77_bill.txt"


def seed():
	"""Build the whole fixture. Idempotent enough to re-run while iterating."""
	# Restored to what it WAS, not to False: `patch_handler` sets this flag around every patch,
	# so a fixture that seeds from inside one and then clears it would silently change the
	# behaviour of everything that ran after it in the same migrate.
	previous_in_patch = frappe.flags.in_patch
	frappe.flags.in_patch = True
	try:
		_seed_module_def()
		_seed_legacy_doctypes()
		_seed_legacy_settings()
		_seed_custom_field()
		files = _seed_files()
		_seed_patch_log()
		_seed_installed_apps()
		frappe.db.commit()
	finally:
		frappe.flags.in_patch = previous_in_patch

	return {"files": files, "settings": LEGACY_SETTINGS, "ignored": list(legacy_ignored_rows())}


def _seed_module_def():
	if frappe.db.exists("Module Def", LEGACY_MODULE):
		return
	doc = frappe.new_doc("Module Def")
	doc.module_name = LEGACY_MODULE
	doc.app_name = LEGACY_APP
	doc.custom = 0
	doc.insert(ignore_permissions=True)


@contextmanager
def _module_lookup_stubbed():
	"""Let `DocType.on_update` ask about a python module the bench does not have.

	It looks up `on_doctype_update` through `load_doctype_module`, imported inside
	`DocType.run_module_method` (`doctype.py:631-636`) — so `frappe.modules` is the binding to
	patch. The fork's package is gone, so the lookup has to answer "no such method" rather than
	raise. Nothing else in the insert touches the module.
	"""
	import frappe.modules

	with patch.object(frappe.modules, "load_doctype_module", return_value=ModuleType("stub")):
		yield


def _seed_legacy_doctypes():
	# Child before parent: the Table field on the Single needs its options to resolve.
	if not frappe.db.exists("DocType", LEGACY_IGNORED_DOCTYPE):
		with _module_lookup_stubbed():
			frappe.get_doc(
				{
					"doctype": "DocType",
					"name": LEGACY_IGNORED_DOCTYPE,
					"module": LEGACY_MODULE,
					"istable": 1,
					"custom": 0,
					"editable_grid": 1,
					"fields": [{"fieldname": "doctype_name", "fieldtype": "Link", "options": "DocType"}],
				}
			).insert(ignore_permissions=True)

	if frappe.db.exists("DocType", LEGACY_SETTINGS_DOCTYPE):
		return

	with _module_lookup_stubbed():
		frappe.get_doc(
			{
				"doctype": "DocType",
				"name": LEGACY_SETTINGS_DOCTYPE,
				"module": LEGACY_MODULE,
				"issingle": 1,
				"custom": 0,
				"fields": [
					{"fieldname": "delete_file_from_cloud", "fieldtype": "Check", "label": "Delete"},
					{"fieldname": "bucket_name", "fieldtype": "Data", "label": "Bucket Name"},
					{"fieldname": "region_name", "fieldtype": "Data", "label": "Region"},
					{"fieldname": "folder_name", "fieldtype": "Data", "label": "Folder Name"},
					{"fieldname": "signed_url_expiry_time", "fieldtype": "Int", "label": "Expiry"},
					{"fieldname": "access_key", "fieldtype": "Data", "label": "Access Key"},
					{"fieldname": "endpoint_url", "fieldtype": "Data", "label": "Endpoint URL"},
					{"fieldname": "secret_key", "fieldtype": "Password", "label": "Secret Key"},
					{
						"fieldname": "ignored_doctypes",
						"fieldtype": "Table",
						"label": "Ignored DocTypes",
						"options": LEGACY_IGNORED_DOCTYPE,
					},
					{"fieldname": "timeout_for_migration_job", "fieldtype": "Int", "label": "Timeout"},
				],
				"permissions": [{"role": "System Manager", "read": 1, "write": 1}],
			}
		).insert(ignore_permissions=True)


def _seed_legacy_settings():
	"""The Single's values and its child rows, written the only way they can be read back.

	`frappe.get_single` would need a controller the bench no longer has, so this writes
	`tabSingles` and the child table directly — which is precisely the state a real adopted
	site is in, and precisely what `legacy_install.harvest_settings` has to cope with.
	"""
	from frappe.utils.password import set_encrypted_password

	frappe.db.set_single_value(LEGACY_SETTINGS_DOCTYPE, dict(LEGACY_SETTINGS), update_modified=False)

	existing = frappe.get_all(
		LEGACY_IGNORED_DOCTYPE, filters={"parent": LEGACY_SETTINGS_DOCTYPE}, pluck="doctype_name"
	)
	now = frappe.utils.now()
	for idx, name in enumerate(legacy_ignored_rows(), start=1):
		if name in existing:
			continue
		frappe.qb.into(LEGACY_IGNORED_DOCTYPE).columns(
			"name",
			"creation",
			"modified",
			"modified_by",
			"owner",
			"docstatus",
			"idx",
			"parent",
			"parentfield",
			"parenttype",
			"doctype_name",
		).insert(
			(
				frappe.generate_hash(length=10),
				now,
				now,
				"Administrator",
				"Administrator",
				0,
				idx,
				LEGACY_SETTINGS_DOCTYPE,
				"ignored_doctypes",
				LEGACY_SETTINGS_DOCTYPE,
				name,
			)
		).run()

	# The fork stored the secret in a Password field, so it lives in `__Auth` keyed by the
	# Single's own doctype/name — not by ("DocType", "S3 File Attachment").
	set_encrypted_password(LEGACY_SETTINGS_DOCTYPE, LEGACY_SETTINGS_DOCTYPE, LEGACY_SECRET, "secret_key")


def _seed_custom_field():
	"""The fork's own `install.py` field, owned by the fork's module.

	Guarded on the field being absent. `create_custom_fields` updates an existing field in
	place, so on a site where this app is already installed an unguarded call would re-own
	**our** `File-s3_object_key` — reassigning its `module` to a module we do not ship. The
	fixture is for a site the fork owned and this app does not; refusing to touch an existing
	field is what keeps it from being usable anywhere it would do damage.
	"""
	if frappe.db.exists("Custom Field", {"dt": "File", "fieldname": "s3_object_key"}):
		return

	create_custom_fields(
		{
			"File": [
				{
					"fieldname": "s3_object_key",
					"label": "S3 Object Key",
					"fieldtype": "Data",
					"insert_after": "content_hash",
					"read_only": 1,
					"module": LEGACY_MODULE,
				}
			]
		}
	)


def _seed_files() -> dict:
	"""Three File rows: both fork generations, plus an untouched local control."""
	names = {}

	names["new_gen"] = _fork_file(
		file_name="invoice.txt",
		key=NEW_GEN_KEY,
		is_private=1,
		values={"s3_object_key": NEW_GEN_KEY, "content_hash": None},
	)
	# The 0.2.x generation that overloaded `content_hash` with the object key
	# (alyf-de/frappe-attachments-s3#10) — `patches/v0_2_0` is what repairs it.
	names["old_gen"] = _fork_file(
		file_name="bill.txt",
		key=OLD_GEN_KEY,
		is_private=1,
		values={"s3_object_key": None, "content_hash": OLD_GEN_KEY},
	)
	names["local_control"] = _local_file()
	return names


def _fork_file(*, file_name: str, key: str, is_private: int, values: dict) -> str:
	existing = frappe.db.get_value("File", {"file_name": file_name}, "name")
	if existing:
		return existing

	doc = frappe.new_doc("File")
	doc.file_name = file_name
	doc.is_private = is_private
	doc.content = b"legacy fork bytes"
	doc.flags.ignore_permissions = True
	doc.insert()

	# What the fork's `after_insert` did once the upload succeeded: point `file_url` at its
	# own endpoint and record the key. It never removed the local copy unless the operator
	# asked it to, so the local file is left in place here too.
	frappe.db.set_value(
		"File",
		doc.name,
		{"file_url": f"{LEGACY_ENDPOINT}?key={key}", **values},
		update_modified=False,
	)
	return doc.name


def _local_file() -> str:
	"""A file the fork never uploaded. Adoption must leave it byte-for-byte alone."""
	existing = frappe.db.get_value("File", {"file_name": "untouched.txt"}, "name")
	if existing:
		return existing

	doc = frappe.new_doc("File")
	doc.file_name = "untouched.txt"
	doc.is_private = 0
	doc.content = b"not a cloud file"
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name


def _seed_patch_log():
	"""A fork patch entry, so the run can show we neither rewrite nor forge patch history."""
	patch = "frappe_s3_attachment.patches.v0_0_1.migrate_existing_files"
	if frappe.db.exists("Patch Log", {"patch": patch}):
		return
	frappe.get_doc({"doctype": "Patch Log", "patch": patch}).insert(ignore_permissions=True)


def _seed_installed_apps():
	import json

	apps = json.loads(frappe.db.get_global("installed_apps") or "[]")
	if LEGACY_APP in apps:
		return
	apps.append(LEGACY_APP)
	frappe.db.set_global("installed_apps", json.dumps(apps))
