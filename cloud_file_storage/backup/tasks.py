"""The backup job: force a fresh dump, upload it, prove it arrived intact (A28 + A5).

The order of operations is the whole design. Nothing is logged Success until every
artifact has been independently verified against the bucket, and nothing is uploaded until
the dump has been shown to belong to *this* run. Both checks fail closed, and neither one
deletes anything.
"""

import math
import os
import time

import frappe
from frappe import _
from frappe.utils import cint, get_datetime, now_datetime
from rq.timeouts import JobTimeoutException

from cloud_file_storage.backup import lifecycle
from cloud_file_storage.backup import settings as backup_settings
from cloud_file_storage.backup.exceptions import BackupFreshnessError, BackupVerificationError
from cloud_file_storage.backup.settings import (
	BACKUP_LOG_DOCTYPE,
	BACKUP_SETTINGS_DOCTYPE,
	MANUAL_SLUG,
	build_key,
	get_backup_client,
	get_backup_settings,
)
from cloud_file_storage.storage import client as storage_client
from cloud_file_storage.storage.hashing import STREAM_CHUNK_SIZE

#: The artifacts one run can produce, in the order they are uploaded, mapped to the Backup
#: Log column that records each key.
ARTIFACT_KINDS = (
	("database", "backup_path_db", "db_key"),
	("site_config", "backup_path_conf", "config_key"),
	("public_files", "backup_path_files", "public_files_key"),
	("private_files", "backup_path_private_files", "private_files_key"),
)

#: Backups upload on the pre-existing `long` queue, deliberately NOT on `cloud_migration`:
#: a nightly single-file dump is not migration work, and coupling it to a queue that is a
#: deployment prerequisite would mean an unconfigured bench silently stops backing up.
BACKUP_QUEUE = "long"
BACKUP_JOB_ID = "cfs::backup"
BACKUP_TIMEOUT = 3600

#: Bounded self-re-enqueue after a job timeout (design §2.2, pattern from core's
#: s3_backup_settings). Bounded because an artifact that times out three times is a
#: condition an operator has to see, not a loop to hide it in.
MAX_TIMEOUT_RETRIES = 2


# ------------------------------------------------------------------ scheduling


def run_scheduled_backup():
	"""Hourly dispatcher. O(ms) when nothing is due (B3)."""
	if not frappe.db.exists("DocType", BACKUP_SETTINGS_DOCTYPE):
		return

	settings = get_backup_settings()
	if not cint(settings.enabled):
		return
	if not is_backup_due(settings, now_datetime()):
		return

	frappe.enqueue(
		"cloud_file_storage.backup.tasks.take_cloud_backup",
		queue=BACKUP_QUEUE,
		timeout=BACKUP_TIMEOUT,
		job_id=BACKUP_JOB_ID,
		deduplicate=True,
		trigger="scheduled",
	)


def is_backup_due(settings, now) -> bool:
	"""Whether this tick should start a backup.

	The gate is "the current slot has not been backed up yet", computed from
	`last_backup_on`, rather than "the clock reads 01:00". A tick that is missed because
	the scheduler was paused or the worker was busy therefore still runs when the scheduler
	comes back, instead of skipping a day silently — the failure mode the health chip in
	§2.6 exists to catch would otherwise be routine.
	"""
	now = get_datetime(now)
	frequency = (settings.frequency or "Daily").strip()
	last = get_datetime(settings.last_backup_on) if settings.last_backup_on else None

	if frequency == "Hourly":
		return last is None or (now - last).total_seconds() >= 3600 - 60

	if frequency == "Every 6 Hours":
		return last is None or (now - last).total_seconds() >= 6 * 3600 - 60

	hour = max(0, min(23, cint(settings.backup_hour)))
	if frequency == "Weekly":
		weekdays = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
		target = (
			weekdays.index((settings.backup_weekday or "Monday").strip())
			if (settings.backup_weekday or "Monday").strip() in weekdays
			else 0
		)
		if now.weekday() != target or now.hour < hour:
			return False
		return last is None or (now - last).total_seconds() >= 6 * 24 * 3600

	# Daily
	if now.hour < hour:
		return False
	return last is None or last.date() < now.date()


@frappe.whitelist()
def backup_now() -> str:
	"""Operator-triggered backup. Returns the enqueued job id."""
	frappe.only_for("System Manager")

	settings = get_backup_settings()
	if not cint(settings.enabled):
		frappe.throw(_("Enable Cloud Backup Settings first."), title=_("Backups Disabled"))
	backup_settings.assert_backup_bucket_isolated(settings, context="backup")

	frappe.enqueue(
		"cloud_file_storage.backup.tasks.take_cloud_backup",
		queue=BACKUP_QUEUE,
		timeout=BACKUP_TIMEOUT,
		job_id=BACKUP_JOB_ID,
		deduplicate=True,
		trigger="manual",
	)
	return BACKUP_JOB_ID


# ------------------------------------------------------------------ freshness (A28)


def job_start_epoch() -> int:
	"""Job start, floored to the second.

	Floored on purpose. The comparison is against a filesystem mtime, and a filesystem with
	one-second timestamp granularity would report a dump written at `start + 0.4` as
	`floor(start)`, failing a strict comparison against a fractional start. Flooring keeps
	the check strict against what it is actually for — a dump from a previous run, which is
	minutes or hours old — while immune to sub-second truncation.
	"""
	return math.floor(time.time())


def assert_artifact_is_fresh(path: str, started_epoch: int, *, kind: str):
	"""A28: the artifact was written by this run, not handed back from an earlier one.

	`new_backup(force=True)` is asked for and this is checked anyway. `force` is a request
	to skip `get_recent_backup`; the assertion is the proof. If a future frappe changes what
	`force` means, or a partial run leaves a previous dump at the path we are about to
	upload, the backup fails loudly here instead of recording yesterday's database as
	today's Success — which is a restore that silently loses a day.
	"""
	try:
		mtime = os.path.getmtime(path)
	except OSError as exc:
		raise BackupFreshnessError(f"{kind} artifact {path} disappeared before upload") from exc

	if mtime < started_epoch:
		raise BackupFreshnessError(
			f"{kind} artifact {path} has mtime {mtime} which predates this job "
			f"({started_epoch}); new_backup returned a stale dump"
		)


# ------------------------------------------------------------------ verification (A5)


def file_digest(path: str) -> tuple[str, int]:
	"""Streamed SHA256 and size of a local file. Never loads the artifact into memory."""
	import hashlib

	digest = hashlib.sha256()
	size = 0
	with open(path, "rb") as handle:  # nosemgrep: frappe-security-file-traversal
		while True:
			chunk = handle.read(STREAM_CHUNK_SIZE)
			if not chunk:
				break
			digest.update(chunk)
			size += len(chunk)
	return digest.hexdigest(), size


def _remote_digest(client, bucket: str, key: str) -> tuple[str, int]:
	"""Streamed SHA256 and size of the REMOTE object — a full re-GET, re-hashed."""
	import hashlib

	response = client.get_object(Bucket=bucket, Key=key)
	digest = hashlib.sha256()
	size = 0
	body = response["Body"]
	while True:
		chunk = body.read(STREAM_CHUNK_SIZE)
		if not chunk:
			break
		digest.update(chunk)
		size += len(chunk)
	return digest.hexdigest(), size


def upload_and_verify(
	client, bucket: str, key: str, local_path: str, extra_args: dict, *, threshold_bytes: int
) -> dict:
	"""Upload one artifact and prove the bucket holds exactly those bytes (A5).

	Verification, in the order it runs:

	1. `head_object(ChecksumMode="ENABLED")` — `ContentLength` must equal the local size.
	   This check runs for every artifact, whatever its size.
	2. Below the multipart threshold, S3's whole-object `ChecksumSHA256` is compared to
	   the locally computed one.
	3. **At or above the threshold, ContentLength plus a streamed re-GET SHA256.** A
	   multipart object's `ChecksumSHA256` is a checksum-of-checksums (`...-N`), so
	   comparing it to the whole-object digest is meaningless; the only honest check is to
	   read the object back and hash it.

	**On failure the remote object is left exactly where it is.** Deleting it would be the
	delete-before-verify mistake in reverse: the artifact may be perfectly good bytes whose
	HEAD raced a replication lag, and even a genuinely corrupt artifact is evidence. The
	run is marked Failed, the operator is alerted, and the object stays for inspection.
	"""
	local_sha256, local_size = file_digest(local_path)

	client.upload_file(
		local_path,
		bucket,
		key,
		ExtraArgs={"ChecksumAlgorithm": "SHA256", **extra_args},
		Config=_transfer_config(threshold_bytes),
	)

	head = client.head_object(Bucket=bucket, Key=key, ChecksumMode="ENABLED")
	remote_size = cint(head.get("ContentLength"))
	if remote_size != local_size:
		raise BackupVerificationError(
			f"size mismatch for {key}: remote {remote_size} != local {local_size} "
			"(remote object kept for inspection)"
		)

	strategy = "head_checksum"
	remote_checksum = head.get("ChecksumSHA256") or ""
	is_multipart_checksum = "-" in remote_checksum

	if local_size >= threshold_bytes or is_multipart_checksum or not remote_checksum:
		strategy = "streamed_reget"
		streamed_sha256, streamed_size = _remote_digest(client, bucket, key)
		if streamed_size != local_size or streamed_sha256 != local_sha256:
			raise BackupVerificationError(
				f"streamed SHA256 mismatch for {key}: remote {streamed_sha256} "
				f"!= local {local_sha256} (remote object kept for inspection)"
			)
	else:
		import base64

		expected = base64.b64encode(bytes.fromhex(local_sha256)).decode()
		if remote_checksum != expected:
			raise BackupVerificationError(
				f"SHA256 mismatch for {key}: remote {remote_checksum} != local {expected} "
				"(remote object kept for inspection)"
			)

	return {
		"key": key,
		"sha256": local_sha256,
		"size": local_size,
		"etag": (head.get("ETag") or "").strip('"'),
		"strategy": strategy,
	}


def _transfer_config(threshold_bytes: int):
	"""Explicit TransferConfig (A5) — never s3transfer's defaults.

	The threshold decides which verification strategy an artifact gets, so the value the
	upload uses and the value the check branches on must be the same number.
	"""
	from boto3.s3.transfer import TransferConfig

	return TransferConfig(
		multipart_threshold=threshold_bytes,
		multipart_chunksize=max(5 * 1024 * 1024, threshold_bytes // 4),
		max_concurrency=4,
		use_threads=True,
	)


# ------------------------------------------------------------------ the job


def take_cloud_backup(trigger: str = "scheduled", attempt: int = 0) -> str:
	"""Take one backup end to end. Returns the Cloud Storage Backup Log name."""
	settings = get_backup_settings()
	if not cint(settings.enabled):
		frappe.throw(_("Cloud backups are disabled."), title=_("Backups Disabled"))
	if not (settings.backup_bucket or "").strip():
		frappe.throw(_("Configure a backup bucket first."), title=_("Backup Not Configured"))
	backup_settings.assert_backup_bucket_isolated(settings, context="backup")
	# An artifact written under an empty prefix lands where no generated lifecycle rule can
	# reach it, so it would accumulate for ever (A20). Refused here as well as at generation.
	lifecycle.assert_prefix_is_scoped(settings)

	slug = MANUAL_SLUG if trigger == "manual" else backup_settings.frequency_slug(settings.frequency)
	started_epoch = job_start_epoch()

	log = frappe.new_doc(BACKUP_LOG_DOCTYPE)
	log.update(
		{
			"status": "Running",
			"trigger": trigger,
			"frequency": settings.frequency,
			"started_at": now_datetime(),
			"backup_bucket": settings.backup_bucket,
		}
	)
	log.insert(ignore_permissions=True)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)

	try:
		manifest, total_bytes, keys, local_path = _run_backup(settings, slug, started_epoch, log)
	except JobTimeoutException:
		_fail(log, settings, "job timed out")
		if attempt < MAX_TIMEOUT_RETRIES:
			frappe.enqueue(
				"cloud_file_storage.backup.tasks.take_cloud_backup",
				queue=BACKUP_QUEUE,
				timeout=BACKUP_TIMEOUT,
				job_id=f"{BACKUP_JOB_ID}::retry{attempt + 1}",
				trigger=trigger,
				attempt=attempt + 1,
			)
		raise
	except Exception as exc:  # noqa: BLE001 - every failure is recorded, then re-raised
		_fail(log, settings, str(exc))
		raise

	import json

	log.reload()
	log.update(
		{
			"status": "Success",
			"ended_at": now_datetime(),
			"total_bytes": total_bytes,
			"sha256_manifest": json.dumps(manifest, indent=2, sort_keys=True),
			"frappe_backup_path": local_path,
			**keys,
		}
	)
	log.save(ignore_permissions=True)
	frappe.db.set_single_value(
		BACKUP_SETTINGS_DOCTYPE,
		{"last_backup_on": now_datetime(), "last_backup_status": "Success"},
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)
	frappe.clear_document_cache(BACKUP_SETTINGS_DOCTYPE)

	if cint(settings.email_on_success):
		_notify(settings, subject=f"Cloud backup {log.name} succeeded", message=log.name)

	return log.name


def _run_backup(settings, slug: str, started_epoch: int, log) -> tuple[dict, int, dict, str]:
	from frappe.utils.backups import new_backup

	include_public = cint(settings.include_public_files)
	include_private = cint(settings.include_private_files)

	# A28: `force=True` skips `get_recent_backup`, so the dump below is this run's.
	odb = new_backup(
		ignore_files=not (include_public or include_private),
		compress=bool(cint(settings.compress_files)),
		force=True,
		verbose=False,
	)

	wanted = {"database"}
	if cint(settings.include_site_config):
		wanted.add("site_config")
	if include_public:
		wanted.add("public_files")
	if include_private:
		wanted.add("private_files")

	artifacts = []
	for kind, attribute, column in ARTIFACT_KINDS:
		if kind not in wanted:
			continue
		path = getattr(odb, attribute, None)
		if not path or not os.path.exists(path):
			if kind == "database":
				raise BackupFreshnessError("new_backup produced no database dump")
			continue
		# A28 runs on every artifact, before a single byte is uploaded.
		assert_artifact_is_fresh(path, started_epoch, kind=kind)
		artifacts.append((kind, column, path))

	timestamp = getattr(odb, "todays_date", None) or str(started_epoch)
	client = get_backup_client(settings)
	extra_args = backup_settings.sse_extra_args(settings)
	threshold_bytes = storage_client.multipart_threshold_bytes()

	manifest: dict = {}
	keys: dict = {}
	total_bytes = 0
	for kind, column, path in artifacts:
		key = build_key(settings, slug=slug, timestamp=timestamp, filename=os.path.basename(path))
		result = upload_and_verify(
			client,
			settings.backup_bucket,
			key,
			path,
			extra_args,
			threshold_bytes=threshold_bytes,
		)
		manifest[key] = {
			"kind": kind,
			"sha256": result["sha256"],
			"size": result["size"],
			"verified_by": result["strategy"],
		}
		keys[column] = key
		total_bytes += result["size"]

	local_path = os.path.dirname(artifacts[0][2]) if artifacts else ""
	return manifest, total_bytes, keys, local_path


def _fail(log, settings, error: str):
	"""Record the failure and tell somebody. The local artifacts are left alone.

	Owns its transaction: this runs after an exception, and the caller re-raises, so a
	failure row that is not committed here is a failure nobody ever sees (A2/A3).
	"""
	try:
		frappe.db.rollback()
		frappe.db.set_value(
			BACKUP_LOG_DOCTYPE,
			log.name,
			{"status": "Failed", "ended_at": now_datetime(), "error": error[:1000]},
			update_modified=False,
		)
		frappe.db.set_single_value(
			BACKUP_SETTINGS_DOCTYPE,
			{"last_backup_on": now_datetime(), "last_backup_status": "Failed"},
		)
		frappe.db.commit()
		frappe.clear_document_cache(BACKUP_SETTINGS_DOCTYPE)
		_notify(settings, subject=f"Cloud backup {log.name} FAILED", message=error)
	except Exception:  # noqa: BLE001 - never mask the original failure
		frappe.log_error(title="cloud_file_storage: could not record a backup failure")


def _notify(settings, *, subject: str, message: str):
	recipient = (settings.notify_email or "").strip()
	if not recipient:
		return
	try:
		frappe.sendmail(recipients=[recipient], subject=subject, message=message)
	except Exception:  # noqa: BLE001 - a mail failure must not change the backup verdict
		frappe.log_error(title="cloud_file_storage: backup notification failed")


# ------------------------------------------------------------------ log retention


def purge_backup_logs():
	"""Weekly: drop Backup Log rows past `log_retention_days`.

	Only the rows. The remote artifacts they describe are expired by the bucket's own
	lifecycle rules, never by this app — a log purge that deleted backups would make the
	retention policy two half-policies that disagree.
	"""
	if not frappe.db.exists("DocType", BACKUP_SETTINGS_DOCTYPE):
		return

	days = cint(get_backup_settings().log_retention_days)
	if days <= 0:
		return

	from frappe.utils import add_days

	cutoff = add_days(now_datetime(), -days)
	frappe.db.delete(BACKUP_LOG_DOCTYPE, {"creation": ("<", cutoff)})
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)
