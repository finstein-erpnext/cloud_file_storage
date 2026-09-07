"""Create the File -> Cloud Storage Object link field and the indexes the runtime needs.

Ships with the schema it depends on (docs/INVARIANTS.md invariant 9). Idempotent: every helper it
calls checks before it creates, so re-running the patch touches nothing.

The `tabFile.content_hash` index is created here rather than inside a locked transition —
`add_index` commits before its DDL (A24).
"""

import frappe

from cloud_file_storage.install import (
	ensure_cloud_storage_object_field,
	ensure_cloud_storage_object_indexes,
	ensure_file_indexes,
)


def execute():
	ensure_cloud_storage_object_field()
	ensure_file_indexes()
	ensure_cloud_storage_object_indexes()
	frappe.clear_cache(doctype="File")
