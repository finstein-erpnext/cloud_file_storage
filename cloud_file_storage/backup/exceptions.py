"""Typed failures for the backup domain.

Both inherit the storage taxonomy so a caller that already handles `CloudStorageError`
does not silently miss them, and neither is ever converted to a generic exception —
`take_cloud_backup` distinguishes "the object store was unreachable" from "the object
store accepted bytes that are not the bytes we sent", and only the second one means the
artifact in the bucket cannot be trusted.
"""

from cloud_file_storage.storage.exceptions import CloudStorageError, CloudStorageIntegrityError


class BackupVerificationError(CloudStorageIntegrityError):
	"""The uploaded artifact does not match the local one.

	The remote object is deliberately left in place — see `tasks.upload_and_verify`.
	"""


class BackupFreshnessError(CloudStorageError):
	"""A dump older than the job that asked for it (A28).

	Raised *before* anything is uploaded, so a stale artifact never reaches the bucket and
	never gets a Success row.
	"""
