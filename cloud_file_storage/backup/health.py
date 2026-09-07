"""Backup health findings — detected here, rendered by the Settings health panel.

Design §2.2(b) and §2.6 both ask the health panel to show conditions that only the backup
domain can compute: a schedule that has stopped producing artifacts, file tarballs still
being taken after the cloud cutover, a lifecycle policy that was never applied or has
drifted from the configured one. The detection lives here so that the panel, the CLI and
any later release report all read the same answer, and so that a condition nobody has
built a chip for yet is still discoverable.

Nothing here mutates, exactly as `health.py` does for the storage side.
"""

import frappe
from frappe import _
from frappe.utils import cint, get_datetime, now_datetime, time_diff_in_hours

from cloud_file_storage.backup import lifecycle
from cloud_file_storage.backup import settings as backup_settings
from cloud_file_storage.backup.settings import BACKUP_LOG_DOCTYPE, get_backup_settings

#: Hours between runs, per frequency. A backup is reported stale past **twice** its own
#: interval: one missed run is a slow worker, two is a schedule that has stopped.
FREQUENCY_HOURS = {"Hourly": 1, "Every 6 Hours": 6, "Daily": 24, "Weekly": 24 * 7}
STALE_MULTIPLIER = 2

#: Modes in which attachment bytes live in the object store, so tarring them locally is
#: duplicated storage rather than a backup.
CLOUD_MODES = ("S3_PRIMARY_LOCAL_FALLBACK", "S3_ONLY")


def backup_health() -> dict:
	"""Findings about the backup configuration and its recent history.

	Returns `{"enabled", "findings": [{level, code, message}], "last_backup_on",
	"last_backup_status"}`. `level` is `error`, `warn` or `info`.
	"""
	settings = get_backup_settings()
	findings: list[dict] = []

	if not cint(settings.enabled):
		return {
			"enabled": False,
			"findings": [
				{
					"level": "warn",
					"code": "backups_disabled",
					"message": _("Cloud backups are disabled. Nothing is being uploaded."),
				}
			],
			"last_backup_on": settings.last_backup_on,
			"last_backup_status": settings.last_backup_status,
		}

	findings.extend(_staleness_findings(settings))
	findings.extend(_tarball_findings(settings))
	findings.extend(_lifecycle_findings(settings))

	return {
		"enabled": True,
		"findings": findings,
		"last_backup_on": settings.last_backup_on,
		"last_backup_status": settings.last_backup_status,
	}


def _staleness_findings(settings) -> list[dict]:
	interval = FREQUENCY_HOURS.get((settings.frequency or "Daily").strip(), 24)
	limit = interval * STALE_MULTIPLIER

	if not settings.last_backup_on:
		return [
			{
				"level": "error",
				"code": "no_backup_yet",
				"message": _("No cloud backup has completed yet."),
			}
		]

	age_hours = time_diff_in_hours(now_datetime(), get_datetime(settings.last_backup_on))
	findings = []
	if age_hours > limit:
		findings.append(
			{
				"level": "error",
				"code": "backup_stale",
				"message": _(
					"The last cloud backup was {0} hours ago; {1} backups should be at most {2} "
					"hours apart. Check that the scheduler is running and the long queue is not "
					"stuck."
				).format(round(age_hours, 1), settings.frequency, limit),
			}
		)
	if (settings.last_backup_status or "") == "Failed":
		findings.append(
			{
				"level": "error",
				"code": "last_backup_failed",
				"message": _(
					"The last cloud backup failed. Read the error on the newest Cloud Storage "
					"Backup Log row; a verification failure means the artifact in the bucket is "
					"not the artifact we sent, and it has been kept for inspection."
				),
			}
		)
	return findings


def _tarball_findings(settings) -> list[dict]:
	"""File tarballs after the cutover are duplicated storage (design B2/B6)."""
	from cloud_file_storage.storage.modes import get_mode

	if not (cint(settings.include_public_files) or cint(settings.include_private_files)):
		return []
	if get_mode() not in CLOUD_MODES:
		return []

	return [
		{
			"level": "warn",
			"code": "tarballs_after_cutover",
			"message": _(
				"This site is in {0}, so attachment bytes live in the object store and their "
				"links ride in the database dump — but the backup job is still tarring local "
				"files. Turn off the file tarball checkboxes, and remove any --with-files "
				"entries from the bench backup crontab."
			).format(get_mode()),
		}
	]


def _lifecycle_findings(settings) -> list[dict]:
	"""Whether the bucket's rules match the configured policy.

	Compared by hash against what an apply would write, so a settings change that was never
	applied is visible without a network call. It cannot see a rule an operator changed in
	the console — that needs `preview_lifecycle_policy`, which does talk to the bucket.
	"""
	if not (settings.backup_bucket or "").strip():
		return [
			{
				"level": "error",
				"code": "no_backup_bucket",
				"message": _("Cloud backups are enabled but no backup bucket is configured."),
			}
		]

	# Reported, not raised: the generator refuses an empty prefix (A20), and a health panel
	# that threw instead of saying so would hide the condition it exists to show.
	if not backup_settings.expand_prefix(settings.backup_prefix):
		return [
			{
				"level": "error",
				"code": "no_backup_prefix",
				"message": _(
					"No backup prefix is configured, so no lifecycle policy can be generated: "
					"an empty prefix would scope every rule to the whole of {0}."
				).format(settings.backup_bucket),
			}
		]

	if not settings.lifecycle_applied_hash:
		return [
			{
				"level": "warn",
				"code": "lifecycle_never_applied",
				"message": _(
					"No lifecycle policy has been applied to {0}. Backup artifacts will "
					"accumulate there indefinitely."
				).format(settings.backup_bucket),
			}
		]

	# The configured policy alone, not merged: a foreign rule appearing in the bucket is not
	# a drift in what THIS app would write, and reporting it as one would cry wolf.
	# The stored hash covers the MERGED policy, so equality here only holds when the bucket
	# had no foreign rules at apply time. Inequality is therefore reported as "may have
	# drifted", not as a fact: it is the strongest honest statement available without a
	# bucket call, and the message says which two things could explain it.
	current = lifecycle.policy_hash({"Rules": lifecycle.build_lifecycle_policy(settings)["Rules"]})
	if current == settings.lifecycle_applied_hash:
		return []
	return [
		{
			"level": "warn",
			"code": "lifecycle_may_have_drifted",
			"message": _(
				"The retention settings have changed since the lifecycle policy was last "
				"applied to {0}, or the bucket carries rules this app does not manage. Run "
				"Preview Lifecycle Policy to see the difference."
			).format(settings.backup_bucket),
		}
	]


@frappe.whitelist()
def get_backup_health() -> dict:
	"""Health findings for the Desk panel. Read-only; no bucket call."""
	frappe.only_for("System Manager")
	return backup_health()


def recent_backups(limit: int = 5) -> list[dict]:
	"""The newest Backup Log rows, for the panel's history strip."""
	return frappe.get_all(
		BACKUP_LOG_DOCTYPE,
		fields=["name", "status", "trigger", "started_at", "ended_at", "total_bytes"],
		order_by="creation desc",
		limit=cint(limit) or 5,
	)
