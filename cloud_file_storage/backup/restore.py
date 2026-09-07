"""Restore helpers — find the artifact, thaw it, download it, prove it is the one.

Deliberately stops short of restoring anything. `bench restore` drops and recreates the
database; putting that behind a Desk button would make the single most destructive
operation in the product reachable by a mis-click. What this module does is make the
manual procedure safe: the artifact you are about to feed to `bench restore` is checked,
byte for byte, against the SHA256 the Backup Log recorded when it was uploaded (A27).
"""

import json
import os

import frappe
from frappe import _
from frappe.utils import cint

from cloud_file_storage.backup import settings as backup_settings
from cloud_file_storage.backup.exceptions import BackupVerificationError
from cloud_file_storage.backup.settings import (
	BACKUP_LOG_DOCTYPE,
	get_backup_client,
	get_backup_settings,
)

#: S3 restore tiers for archived objects, slowest/cheapest last.
RESTORE_TIERS = ("Expedited", "Standard", "Bulk")

#: Ceiling on a presigned download URL for a backup artifact. Same reasoning as the serving
#: TTLs: a link to the whole database must not outlive the operator's terminal session.
DOWNLOAD_TTL = 900


@frappe.whitelist()
def list_backups(limit: int = 30) -> list[dict]:
	"""Recent Backup Log rows with their artifact keys and recorded digests."""
	frappe.only_for("System Manager")

	rows = frappe.get_all(
		BACKUP_LOG_DOCTYPE,
		fields=[
			"name",
			"status",
			"trigger",
			"frequency",
			"started_at",
			"ended_at",
			"total_bytes",
			"backup_bucket",
			"db_key",
			"config_key",
			"public_files_key",
			"private_files_key",
			"sha256_manifest",
		],
		order_by="creation desc",
		limit=cint(limit) or 30,
	)
	for row in rows:
		row["manifest"] = _parse_manifest(row.get("sha256_manifest"))
	return rows


def _parse_manifest(raw) -> dict:
	try:
		return json.loads(raw or "{}")
	except ValueError:
		return {}


def recorded_digest(key: str) -> dict | None:
	"""The `{sha256, size, kind}` a successful backup recorded for this object key.

	Read from the Backup Log rather than from the object's own metadata on purpose: the
	whole point of the check is to compare the artifact against a record kept somewhere the
	bucket cannot rewrite.
	"""
	rows = frappe.get_all(
		BACKUP_LOG_DOCTYPE,
		filters={"status": "Success"},
		fields=["name", "sha256_manifest"],
		order_by="creation desc",
		limit=500,
	)
	for row in rows:
		manifest = _parse_manifest(row.sha256_manifest)
		if key in manifest:
			return {**manifest[key], "backup_log": row.name, "key": key}
	return None


@frappe.whitelist()
def initiate_restore(key: str, days: int = 3, tier: str = "Standard") -> dict:
	"""Ask S3 to thaw an archived artifact back into readable storage.

	Only needed for objects a `cfs-archive-*` lifecycle rule has already transitioned; an
	object still in STANDARD is downloadable now and S3 answers `InvalidObjectState`.
	"""
	frappe.only_for("System Manager")

	if tier not in RESTORE_TIERS:
		frappe.throw(_("Restore tier must be one of {0}.").format(", ".join(RESTORE_TIERS)))

	settings = get_backup_settings()
	backup_settings.assert_backup_bucket_isolated(settings, context="restore")
	key = assert_key_is_a_backup_artifact(key, action=_("restored from archive"))
	client = get_backup_client(settings)

	try:
		response = client.restore_object(
			Bucket=settings.backup_bucket,
			Key=key,
			RestoreRequest={"Days": cint(days) or 3, "GlacierJobParameters": {"Tier": tier}},
		)
	except Exception as exc:  # noqa: BLE001 - surfaced to the operator, not swallowed
		return {"key": key, "ok": False, "error": str(exc)}

	return {
		"key": key,
		"ok": True,
		"days": cint(days) or 3,
		"tier": tier,
		"status_code": (response.get("ResponseMetadata") or {}).get("HTTPStatusCode"),
	}


@frappe.whitelist()
def restore_status(key: str) -> dict:
	"""Parse the `Restore` header: is the thaw still running, and until when."""
	frappe.only_for("System Manager")

	settings = get_backup_settings()
	backup_settings.assert_backup_bucket_isolated(settings, context="restore status")
	key = assert_key_is_a_backup_artifact(key, action=_("inspected"))
	client = get_backup_client(settings)
	head = client.head_object(Bucket=settings.backup_bucket, Key=key)
	restore_header = head.get("Restore") or ""

	return {
		"key": key,
		"storage_class": head.get("StorageClass") or "STANDARD",
		"restore": restore_header,
		"ongoing": 'ongoing-request="true"' in restore_header,
		"ready": bool(restore_header) and 'ongoing-request="false"' in restore_header,
		"expiry": restore_header.split('expiry-date="')[1].rstrip('"')
		if 'expiry-date="' in restore_header
		else None,
		"size": cint(head.get("ContentLength")),
	}


def assert_key_is_a_backup_artifact(key: str, *, action: str) -> str:
	"""Refuse any key that is not a backup artifact under this site's scoped prefix (H-1).

	Without this, `key` reaches `generate_presigned_url` unvalidated and **any object in the
	bucket** can be signed for. Two independent conditions, because either alone is weak: the
	key must sit under the configured prefix (so a shared or mis-set bucket cannot be walked),
	and it must appear in the manifest of a successful Backup Log row (so only artifacts this
	app uploaded and verified are reachable).

	The shape is borrowed from `api/compat.legacy_generate_file`, which resolves a key to a
	File row the caller may read before it presigns anything. It cannot be reused: a backup
	artifact deliberately has **no File row and no CSO** — it is not an attachment, which is
	exactly why `legacy_generate_file` refuses it and why this endpoint had to exist. So the
	*shape* is reused and the resolution is not: prefix scoping stands in for "a row you may
	read", and the audit row below stands in for the access log.
	"""
	from cloud_file_storage.backup.lifecycle import assert_prefix_is_scoped

	settings = get_backup_settings()
	scoped_prefix = assert_prefix_is_scoped(settings)
	candidate = (key or "").strip()

	if not candidate.startswith(scoped_prefix):
		frappe.throw(
			_("{0} is not a backup artifact of this site: it is not under {1}.").format(
				frappe.bold(candidate or _("(empty)")), frappe.bold(scoped_prefix)
			),
			title=_("Key Refused"),
			exc=frappe.PermissionError,
		)

	if recorded_digest(candidate) is None:
		frappe.throw(
			_(
				"No successful Cloud Storage Backup Log records {0}. Only artifacts this site "
				"uploaded and verified can be {1}."
			).format(frappe.bold(candidate), action),
			title=_("Key Refused"),
			exc=frappe.PermissionError,
		)

	return candidate


def _audit_presign(key: str, *, ttl: int, bucket: str, action: str):
	"""One audit row per mint, committed BEFORE the URL exists.

	Before, not after: if the process dies between the two you want a record of an attempt
	with no URL, not a URL with no record. Owns its transaction per invariant 7.

	The URL and every signature component are deliberately absent — an audit log that
	recorded them would be a second copy of the credential it exists to make accountable.
	"""
	from cloud_file_storage.migration import audit

	audit.record(action, key=key, ttl=ttl, bucket=bucket)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)


@frappe.whitelist()
def download_url(key: str, ttl: int = DOWNLOAD_TTL) -> str:
	"""Short-lived presigned GET for a backup artifact.

	Order, copied from `api/compat.legacy_generate_file`: resolve and refuse first, record
	second, mint last. The artifact may be the site_config JSON, which carries the database
	password and the backup encryption key, so "who asked for this and when" has to survive
	even when the mint does not.
	"""
	frappe.only_for("System Manager")

	settings = get_backup_settings()
	backup_settings.assert_backup_bucket_isolated(settings, context="backup download")
	key = assert_key_is_a_backup_artifact(key, action=_("downloaded"))
	ttl = max(1, min(cint(ttl) or DOWNLOAD_TTL, DOWNLOAD_TTL))
	_audit_presign(key, ttl=ttl, bucket=settings.backup_bucket, action="backup_presign_issued")

	client = get_backup_client(settings)
	return client.generate_presigned_url(
		"get_object",
		Params={
			"Bucket": settings.backup_bucket,
			"Key": key,
			# Pinned into the signature, as `engine.presign_get` does for attachments and for
			# the same reasons. The site-config artifact is JSON carrying the database
			# password: served inline it renders in the browser, and cacheable it survives in
			# a proxy. Neither is acceptable for a link to the whole database.
			"ResponseContentDisposition": f'attachment; filename="{key.rsplit("/", 1)[-1]}"',
			"ResponseCacheControl": "no-store",
		},
		ExpiresIn=ttl,
	)


def verify_downloaded_artifact(path: str, key: str | None = None) -> dict:
	"""Prove a downloaded artifact matches what the Backup Log recorded (A27).

	**Run this before `bench restore`, every time.** Restoring an artifact that was
	truncated in transit, or thawed from a corrupted archive, replaces a working database
	with a broken one — and the moment `bench restore` starts, the thing you would have
	compared against is gone.

	Raises :class:`BackupVerificationError` on any mismatch. Never deletes the file: a
	mismatching artifact is evidence, and it may still be the only copy of something.
	"""
	from cloud_file_storage.backup.tasks import file_digest

	if not os.path.exists(path):
		raise BackupVerificationError(f"{path} does not exist")

	key = key or os.path.basename(path)
	record = recorded_digest(key)
	if record is None:
		# Fall back to matching on the artifact's file name, so an operator who downloaded
		# with the browser (and lost the key) can still verify.
		record = _digest_by_filename(os.path.basename(path))
	if record is None:
		raise BackupVerificationError(
			f"no successful Cloud Storage Backup Log records an artifact for {key!r}; "
			"there is nothing to verify this file against"
		)

	sha256, size = file_digest(path)
	if size != cint(record.get("size")) or sha256 != record.get("sha256"):
		raise BackupVerificationError(
			f"{path} does not match Backup Log {record.get('backup_log')}: "
			f"local sha256 {sha256} size {size} != recorded {record.get('sha256')} "
			f"size {record.get('size')}. DO NOT restore this artifact."
		)

	return {
		"path": path,
		"key": record.get("key", key),
		"sha256": sha256,
		"size": size,
		"backup_log": record.get("backup_log"),
		"verified": True,
	}


def _digest_by_filename(filename: str) -> dict | None:
	rows = frappe.get_all(
		BACKUP_LOG_DOCTYPE,
		filters={"status": "Success"},
		fields=["name", "sha256_manifest"],
		order_by="creation desc",
		limit=500,
	)
	for row in rows:
		for key, entry in _parse_manifest(row.sha256_manifest).items():
			if key.rsplit("/", 1)[-1] == filename:
				return {**entry, "backup_log": row.name, "key": key}
	return None
