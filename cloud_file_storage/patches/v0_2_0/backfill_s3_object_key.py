"""Backfill File.s3_object_key from File.content_hash for cloud-managed rows.

Earlier versions of this app stored the object key in `tabFile.content_hash`,
overloading a Frappe core field whose intended use is content identity / dedupe
(see https://github.com/alyf-de/frappe-attachments-s3/issues/10).

This patch copies `content_hash -> s3_object_key` for File rows whose `file_url`
matches the upload URL shapes 0.2.x produced (public bucket URL or the private
`frappe_s3_attachment.controller.generate_file` endpoint), without overwriting
`s3_object_key` values that have already been set by a newer upload path. The predicate
deliberately matches the OLD endpoint only: the rename never rewrote stored URLs (A10),
so no row can carry the new path and also be missing its key.

Clears `content_hash` in the same ``set_value`` so core duplicate detection
does not match subsequent uploads (see
https://github.com/alyf-de/frappe-attachments-s3/issues/12).
"""

import frappe

from cloud_file_storage.install import ensure_s3_object_key_custom_field, ensure_s3_object_key_index

BATCH_SIZE = 5000


def execute():
	ensure_s3_object_key_custom_field()
	ensure_s3_object_key_index()

	while True:
		rows = frappe.db.sql(
			"""
			SELECT name, content_hash
			FROM `tabFile`
			WHERE content_hash IS NOT NULL AND content_hash != ''
			  AND (s3_object_key IS NULL OR s3_object_key = '')
			  AND (
			    file_url LIKE 'https://%%'
			    OR file_url LIKE '/api/method/frappe_s3_attachment.controller.generate_file%%'
			  )
			LIMIT %(batch_size)s
			""",
			{"batch_size": BATCH_SIZE},
			as_dict=True,
		)
		if not rows:
			return

		for row in rows:
			frappe.db.set_value(
				"File",
				row.name,
				{
					"s3_object_key": row.content_hash,
					"content_hash": None,
				},
				update_modified=False,
			)

		# Each batch leaves the candidate set (s3_object_key becomes non-empty), so the
		# next pass reads the remainder without an offset.
		# Batch durability: this loop's TERMINATION depends on committed rows leaving the
	# candidate set. Not annotated -- the rule excludes `**/patches/**`, so an annotation here
	# would suppress nothing and the hygiene sweep would strip it as dead.
	frappe.db.commit()
