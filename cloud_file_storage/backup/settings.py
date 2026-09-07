"""Reading `Cloud Backup Settings` — connection, prefixes and the bucket-isolation guard.

Everything that needs to know *where* a backup goes comes through here, so the one rule
that must never bend — the backup bucket is not the attachment bucket — is asserted in a
single function that the settings validator, the backup job and the lifecycle applier all
call.
"""

import frappe
from frappe import _
from frappe.utils import cint

from cloud_file_storage.storage import client as storage_client

BACKUP_SETTINGS_DOCTYPE = "Cloud Backup Settings"
BACKUP_LOG_DOCTYPE = "Cloud Storage Backup Log"

#: Frequency label -> key segment. Manual runs get their own segment so an operator can see
#: at a glance which artifacts a person asked for, and it is generated a lifecycle rule of
#: its own for exactly the reason A20 wants per-frequency prefixes: a prefix with no
#: expiration rule accumulates for ever.
FREQUENCY_SLUGS = {
	"Hourly": "hourly",
	"Every 6 Hours": "every-6-hours",
	"Daily": "daily",
	"Weekly": "weekly",
}
MANUAL_SLUG = "manual"

#: Prefixes billed as sub-daily: they gain 24 and 4 artifacts a day, so they expire on
#: `subdaily_retention_days` rather than on `delete_after_days`.
SUBDAILY_SLUGS = ("hourly", "every-6-hours")

ALL_SLUGS = ("hourly", "every-6-hours", "daily", "weekly", MANUAL_SLUG)


def get_backup_settings():
	return frappe.get_cached_doc(BACKUP_SETTINGS_DOCTYPE)


def frequency_slug(frequency: str | None) -> str:
	"""Key segment for a frequency label; unknown labels fall back to `daily`."""
	return FREQUENCY_SLUGS.get((frequency or "").strip(), "daily")


def expand_prefix(prefix: str | None) -> str:
	"""`{site}` expands to the site name; the result never starts or ends with `/`."""
	raw = (prefix or "").strip().replace("{site}", frappe.local.site or "site")
	return raw.strip("/")


def artifact_prefix(settings=None, *, slug: str) -> str:
	settings = settings or get_backup_settings()
	base = expand_prefix(settings.backup_prefix)
	return f"{base}/{slug}" if base else slug


def build_key(settings, *, slug: str, timestamp: str, filename: str) -> str:
	"""`<prefix>/<frequency>/<timestamp>/<artifact filename>`.

	The timestamp segment is the dump's own `YYYYMMDD_HHMMSS`, which makes a re-run of the
	same job overwrite the same keys rather than accumulate half-uploaded twins — the
	idempotency the timeout self-re-enqueue depends on.
	"""
	return f"{artifact_prefix(settings, slug=slug)}/{timestamp}/{filename}"


def assert_backup_bucket_isolated(settings=None, *, context: str = "backup", attachment_bucket=None):
	"""The backup bucket is never the attachment bucket (B5, invariant 6).

	Sharing them would put archive-class transitions and expiration rules on live
	attachment objects, which is the one thing PLAN §A's storage-class rule forbids —
	an archived attachment answers a download with 403 InvalidObjectState.
	"""
	from cloud_file_storage.storage.modes import get_settings

	settings = settings or get_backup_settings()
	backup_bucket = (settings.backup_bucket or "").strip()
	if not backup_bucket:
		return

	# The caller may be validating an unsaved Cloud Storage Settings doc, in which case the
	# stored bucket is the OLD one and checking it would miss the edit being made (M-2).
	if attachment_bucket is None:
		attachment_bucket = get_settings().bucket
	attachment_bucket = (attachment_bucket or "").strip()
	if attachment_bucket and backup_bucket == attachment_bucket:
		frappe.throw(
			_(
				"The backup bucket must be a different bucket from the attachment bucket "
				"({0}). Backup lifecycle rules move artifacts to archive storage classes, "
				"and a live attachment in an archive class cannot be served."
			).format(attachment_bucket),
			title=_("Backup Bucket Not Isolated"),
			exc=frappe.ValidationError,
		)


def resolve_connection(settings=None) -> dict:
	"""Connection parameters for the backup bucket, inherited or explicit."""
	from cloud_file_storage.storage.modes import get_settings

	settings = settings or get_backup_settings()
	if cint(settings.use_storage_credentials):
		storage_settings = get_settings()
		access_key_id = secret = None
		if not cint(storage_settings.use_default_credential_chain):
			secret = storage_settings.get_password("secret_access_key", raise_exception=False)
			access_key_id = storage_settings.access_key_id
		return {
			"region": storage_settings.region,
			"endpoint_url": storage_settings.endpoint_url,
			"addressing_style": storage_client.resolve_addressing_style(storage_settings),
			"access_key_id": access_key_id if secret else None,
			"secret_access_key": secret,
		}

	secret = settings.get_password("secret_key", raise_exception=False)
	endpoint_url = (settings.endpoint_url or "").strip()
	return {
		"region": settings.region_name,
		"endpoint_url": endpoint_url,
		# Same rule as the attachment client: a custom endpoint means path-style.
		"addressing_style": "path" if endpoint_url else "",
		"access_key_id": settings.access_key if secret else None,
		"secret_access_key": secret,
	}


def get_backup_client(settings=None):
	"""An S3 client for the backup bucket, on the long-timeout job profile.

	Not cached: backup work is a handful of calls per hour on a job worker, and a cached
	client keyed on two singletons is a stale-credential trap for no measurable gain.
	"""
	connection = resolve_connection(settings)
	return storage_client.build_s3_client(profile=storage_client.PROFILE_JOB, **connection)


def sse_extra_args(settings=None) -> dict:
	"""Encryption ExtraArgs for a backup PUT.

	`Inherit` takes the attachment bucket's mode, because the commonest configuration is one
	account with one KMS key; an explicit choice overrides it. There is no "off" — a backup
	artifact carries the whole database.
	"""
	from cloud_file_storage.storage.modes import get_settings

	settings = settings or get_backup_settings()
	mode = (settings.sse_type or "Inherit").strip()
	kms_key_id = (settings.kms_key_id or "").strip()
	if mode == "Inherit":
		storage_settings = get_settings()
		mode = (storage_settings.sse_mode or "SSE-S3").strip()
		kms_key_id = kms_key_id or (storage_settings.kms_key_id or "").strip()

	if mode == "SSE-KMS":
		extra = {"ServerSideEncryption": "aws:kms"}
		if kms_key_id:
			extra["SSEKMSKeyId"] = kms_key_id
		return extra
	return {"ServerSideEncryption": "AES256"}
