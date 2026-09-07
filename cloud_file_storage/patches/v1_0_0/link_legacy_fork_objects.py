"""Adopt a `frappe_s3_attachment` 0.2.x install — the half that needs our own tables (A21).

`[post_model_sync]`, because both steps need schema that `sync_all` has only just created:
the settings child table, and `Cloud Storage Object` itself.

It runs after `patches/v0_2_0/backfill_s3_object_key`, which is what puts a fork key on the
0.2.x rows that stored it in `content_hash` — so by the time this patch reads
`tabFile.s3_object_key`, both generations of fork row are visible to it.

What it produces is `legacy_unverified` objects (A15): bytes that are in the bucket, at the
key the fork chose, with no hash and no claim to have been verified. That is what makes the
site's existing private URLs resolve through `api/compat.legacy_generate_file` the moment
migrate finishes. Promotion to `verified` is a streamed re-GET and belongs to the migration
engine (`migration.adoption.promote_adopted_object`).
"""

import frappe

from cloud_file_storage import legacy_install


def execute():
	if not legacy_install.is_legacy_install():
		return

	report = legacy_install.finish()
	print(f"cloud_file_storage: linked the legacy fork objects — {report}")
