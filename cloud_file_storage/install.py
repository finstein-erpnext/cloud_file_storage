import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

MODULE_NAME = "Cloud File Storage"
SETTINGS_DOCTYPE = "Cloud Storage Settings"
CSO_DOCTYPE = "Cloud Storage Object"
CLOUD_STORAGE_MANAGER_ROLE = "Cloud Storage Manager"

#: Number Cards the "Cloud Storage" workspace references by name. Created here rather than
#: shipped as `is_standard` fixtures: a standard card is exported back to disk on every save
#: in developer mode, which would make an operator's tweak show up as an app diff. These are
#: seeded once and then belong to the site.
WORKSPACE_NUMBER_CARDS = (
	{
		"label": "Cloud Objects Verified",
		"document_type": CSO_DOCTYPE,
		"filters_json": '{"status": "verified"}',
		"color": "#29CD42",
	},
	{
		"label": "Cloud Objects Awaiting Upload",
		"document_type": CSO_DOCTYPE,
		"filters_json": '{"status": "pending_upload"}',
		"color": "#FFC107",
	},
	{
		"label": "Cloud Objects Failed",
		"document_type": CSO_DOCTYPE,
		"filters_json": '{"status": "failed"}',
		"color": "#E24C4C",
	},
	{
		"label": "Open Migration Conflicts",
		"document_type": "Cloud Migration Conflict",
		"filters_json": '{"status": "Open"}',
		"color": "#E24C4C",
	},
)
# Operational, short-lived attachment bytes that stay on local disk (ADR 0022).
DEFAULT_IGNORED_DOCTYPES = ("Data Import", "Prepared Report", "Package Import")

S3_OBJECT_KEY_FIELD = "s3_object_key"
S3_OBJECT_KEY_INDEX_NAME = "s3_object_key_index"
S3_OBJECT_KEY_LENGTH = 255
# 191 keeps the index byte size safe under utf8mb4 across MariaDB/MySQL configurations.
S3_OBJECT_KEY_INDEX_PREFIX = 191

CSO_LINK_FIELD = "cloud_storage_object"
CSO_LINK_INDEX_NAME = "cloud_storage_object_index"
THUMBNAIL_LINK_FIELD = "cloud_thumbnail_object"
# Core queries tabFile.content_hash unindexed on every insert and every delete
# (file.py:688, :513). At 1.2M rows that is the hot spot; 32 chars is the full MD5.
CONTENT_HASH_INDEX_NAME = "content_hash_index"
CONTENT_HASH_INDEX_PREFIX = 32


def after_install():
	# `get_doctype_module` memoises the whole DocType -> Module map in the cache
	# (frappe/modules/utils.py). During `install-app` that map can predate our own
	# sync, so resolving our Single falls back to frappe's `Core` module and
	# `frappe.get_single("Cloud Storage Settings")` dies with
	# "No module named 'frappe.core.doctype.cloud_storage_settings'". Dropping the
	# stale map first is what makes a fresh install seed its defaults.
	frappe.clear_cache()
	assert_single_file_owner()
	ensure_cloud_storage_manager_role()
	ensure_default_ignored_doctypes()
	# Custom fields first, then every index: creating a Custom Field re-syncs the File
	# table, and that sync drops indexes on columns whose DocField has no `search_index`.
	# An index added before the last field would silently disappear.
	ensure_s3_object_key_custom_field()
	ensure_cloud_storage_object_field()
	ensure_cloud_thumbnail_object_field()
	ensure_s3_object_key_index()
	ensure_file_indexes()
	ensure_cloud_storage_object_indexes()
	ensure_serving_indexes()
	ensure_migration_indexes()
	ensure_workspace_number_cards()


def after_migrate():
	frappe.clear_cache()
	assert_single_file_owner()
	ensure_cloud_storage_manager_role()
	# A failed or partial install can leave the operational trio unseeded; migrate is
	# the repair path. Rows an operator deliberately removed are NOT re-added — only a
	# completely unconfigured list is seeded (see ensure_default_ignored_doctypes).
	ensure_default_ignored_doctypes(only_when_unconfigured=True)
	ensure_s3_object_key_custom_field()
	ensure_cloud_storage_object_field()
	ensure_cloud_thumbnail_object_field()
	ensure_s3_object_key_index()
	ensure_file_indexes()
	ensure_cloud_storage_object_indexes()
	ensure_serving_indexes()
	ensure_migration_indexes()
	ensure_workspace_number_cards()
	report_deprecated_hooks()
	report_public_private_residue()


def assert_single_file_owner():
	"""Refuse to share a site with another File-storage app, loudly and at install time."""
	from cloud_file_storage.core_hooks import validate_single_file_owner

	validate_single_file_owner()


def ensure_cloud_storage_manager_role():
	"""Desk role that may operate storage without holding credentials."""
	if frappe.db.exists("Role", CLOUD_STORAGE_MANAGER_ROLE):
		return

	role = frappe.new_doc("Role")
	role.role_name = CLOUD_STORAGE_MANAGER_ROLE
	role.desk_access = 1
	role.insert(ignore_permissions=True)


def ensure_workspace_number_cards():
	"""Seed the four counts the workspace shows. Never overwrites an existing card.

	A card the operator has since re-pointed or re-filtered is theirs; re-asserting our
	version on every migrate would silently undo that. Missing cards are added, existing ones
	are left exactly as they are.
	"""
	if not frappe.db.table_exists("Number Card"):
		return

	for card in WORKSPACE_NUMBER_CARDS:
		if frappe.db.exists("Number Card", card["label"]):
			continue
		if not frappe.db.table_exists(card["document_type"]):
			continue

		doc = frappe.new_doc("Number Card")
		doc.update(
			{
				"type": "Document Type",
				"function": "Count",
				"is_public": 1,
				"show_percentage_stats": 0,
				"stats_time_interval": "Daily",
				**card,
			}
		)
		doc.insert(ignore_permissions=True)


def ensure_default_ignored_doctypes(only_when_unconfigured: bool = False):
	"""Seed the LOCAL_OPERATIONAL trio (ADR 0022).

	Args:
		only_when_unconfigured: when True, seed only if the list is completely empty.
			Used by `after_migrate` so a repair never resurrects rows an operator
			deliberately removed, while still fixing a site whose install never seeded.
	"""
	settings = frappe.get_single(SETTINGS_DOCTYPE)
	configured_ignored = {row.doctype_name for row in (settings.ignored_doctypes or []) if row.doctype_name}
	if only_when_unconfigured and configured_ignored:
		return

	missing = [name for name in DEFAULT_IGNORED_DOCTYPES if name not in configured_ignored]
	if not missing:
		return

	for doctype_name in missing:
		settings.append("ignored_doctypes", {"doctype_name": doctype_name})
	settings.flags.ignore_mandatory = True
	settings.save(ignore_permissions=True)


def ensure_s3_object_key_custom_field():
	"""Ensure the File DocType carries the s3_object_key locator field.

	Stored as Data with explicit length 255 so realistic key paths
	(`{prefix}/YYYY/MM/DD/{doctype}/{rand}_{filename}`) fit comfortably.
	Read-only because the app manages the value, but visible in the
	Desk so administrators can audit which object backs a File row.

	Deprecated in favour of the ``cloud_storage_object`` link, and still created on every
	install because legacy lookups and the adoption analyzer depend on it (A13).
	"""
	create_custom_fields(
		{
			"File": [
				{
					"fieldname": S3_OBJECT_KEY_FIELD,
					"label": "S3 Object Key",
					"fieldtype": "Data",
					"length": S3_OBJECT_KEY_LENGTH,
					"insert_after": "content_hash",
					"read_only": 1,
					"hidden": 0,
					"no_copy": 1,
					"print_hide": 1,
					"description": (
						"Deprecated legacy object key managed by cloud_file_storage. "
						"Used to resolve presigned URLs and cloud deletion."
					),
					"module": MODULE_NAME,
				}
			]
		}
	)


def ensure_s3_object_key_index():
	"""Add a fast-lookup index on File.s3_object_key.

	On MariaDB/MySQL we use a prefix index of length 191 so the index size
	stays well within utf8mb4 byte limits across DB configurations.
	Every other backend gets a plain index on the (varchar 255) column.

	Tested for MariaDB explicitly rather than "not postgres": frappe v16 adds a **sqlite**
	backend (`frappe/database/sqlite/`), and `col(191)` prefix syntax is MariaDB-only. The
	old negative test would have routed a sqlite site straight into it.
	"""
	if (frappe.db.db_type or "mariadb") == "mariadb":
		frappe.db.add_index(
			"File",
			[f"{S3_OBJECT_KEY_FIELD}({S3_OBJECT_KEY_INDEX_PREFIX})"],
			index_name=S3_OBJECT_KEY_INDEX_NAME,
		)
	else:
		frappe.db.add_index("File", [S3_OBJECT_KEY_FIELD], index_name=S3_OBJECT_KEY_INDEX_NAME)


def ensure_cloud_storage_object_field():
	"""The File -> Cloud Storage Object link.

	Read-only because the runtime owns it, but visible so an administrator can see which
	object backs a row. `no_copy` is 0 on purpose: an amended document copies the link, and
	the A1 resolution chain then has one less hop to make.
	"""
	create_custom_fields(
		{
			"File": [
				{
					"fieldname": CSO_LINK_FIELD,
					"label": "Cloud Storage Object",
					"fieldtype": "Link",
					"options": CSO_DOCTYPE,
					"insert_after": "content_hash",
					"read_only": 1,
					"no_copy": 0,
					"print_hide": 1,
					"description": "Physical object backing this file. Managed by cloud_file_storage.",
					"module": MODULE_NAME,
				}
			]
		}
	)


def ensure_cloud_thumbnail_object_field():
	"""The File -> derived (thumbnail) object link.

	Core keeps no File row for a thumbnail — `make_thumbnail` writes the image to
	`public/files` and records only `thumbnail_url` on the source row (`file.py:462-468`).
	After S3_ONLY there is no local file at that URL, so something has to say which object
	serves it. It is a link on the File rather than a back-reference on the object because
	a thumbnail's bytes can legitimately BE the source's bytes (a box larger than the image
	re-encodes to the same file), and one object cannot be recorded as derived from itself.
	"""
	create_custom_fields(
		{
			"File": [
				{
					"fieldname": THUMBNAIL_LINK_FIELD,
					"label": "Cloud Thumbnail Object",
					"fieldtype": "Link",
					"options": CSO_DOCTYPE,
					"insert_after": CSO_LINK_FIELD,
					"read_only": 1,
					"no_copy": 0,
					"print_hide": 1,
					"description": "Object serving this file's thumbnail_url. Managed by cloud_file_storage.",
					"module": MODULE_NAME,
				}
			]
		}
	)


def ensure_file_indexes():
	"""Indexes on tabFile that the runtime and core both depend on (A24).

	`cloud_storage_object` and `content_hash(32)` are also what keep the A6 locking recount
	(`storage.objects.live_reference_count`) locking matching rows rather than escalating to
	a scan of a 1.2M-row table. Dropping either is not a performance decision — it changes
	what that `FOR UPDATE` locks.

	`add_index` commits before its DDL, so this must run outside a locked transition — it
	is called only from `after_install`/`after_migrate`.

	Quirk worth knowing: for a single-field index outside install/migrate, `add_index` also
	writes a `search_index` Property Setter. With a prefix expression like
	`content_hash(32)` that Property Setter would name a field that does not exist. Because
	both callers run with `in_install`/`in_migrate` set, the branch is never taken here.
	"""
	frappe.db.add_index("File", [CSO_LINK_FIELD], index_name=CSO_LINK_INDEX_NAME)

	# MariaDB explicitly, not "not postgres" -- see _add_s3_object_key_index: v16's sqlite
	# backend would otherwise be handed MariaDB-only prefix-index syntax.
	if (frappe.db.db_type or "mariadb") == "mariadb":
		frappe.db.add_index(
			"File",
			[f"content_hash({CONTENT_HASH_INDEX_PREFIX})"],
			index_name=CONTENT_HASH_INDEX_NAME,
		)
	else:
		frappe.db.add_index("File", ["content_hash"], index_name=CONTENT_HASH_INDEX_NAME)


def ensure_serving_indexes():
	"""Indexes the P3 serving paths look things up through (alias hash, derived objects)."""
	from cloud_file_storage.patches.v0_4_0.create_url_alias_indexes import execute as create_indexes

	create_indexes()


def report_deprecated_hooks():
	"""Surface `s3_key_generator` if a site still ships it (A24 / rename ADR R5).

	Loud beats silent: the hook is not honoured, and a site that still defines one is
	expecting object keys this app cannot produce.
	"""
	from cloud_file_storage.api.compat import detect_deprecated_hooks

	detect_deprecated_hooks()


def report_public_private_residue():
	"""Surface private-namespaced files sitting in the PUBLIC site tree.

	Loud beats silent, and this one is a live unauthenticated disclosure rather than a
	misconfiguration: nginx serves `sites/<site>/public/$uri` off disk, so a thumbnail core
	wrote to `public/private/files/` is readable by anyone who can guess the URL. It is
	upstream behaviour and the files are not ours to move, so migrate is where an operator
	is told about it. Detection only — see `health.detect_public_private_residue`.
	"""
	from cloud_file_storage.health import detect_public_private_residue

	detect_public_private_residue()


def ensure_migration_indexes():
	"""Composite indexes the migration engine dispatches and reports on.

	`on_doctype_update` already runs these during a DocType sync; re-asserting them here
	makes them deterministic on a site whose sync predated the module (and keeps the same
	shape as `ensure_cloud_storage_object_indexes`). Every call is idempotent.
	"""
	from cloud_file_storage.patches.v0_5_0.create_migration_indexes import execute as create_indexes

	create_indexes()


def ensure_cloud_storage_object_indexes():
	"""Identity and GC indexes on the CSO table (unique identity, unique key, GC scan)."""
	from cloud_file_storage.cloud_file_storage.doctype.cloud_storage_object.cloud_storage_object import (
		on_doctype_update,
	)

	if not frappe.db.table_exists(CSO_DOCTYPE):
		return
	on_doctype_update()
