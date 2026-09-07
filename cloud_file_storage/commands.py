"""bench CLI for the migration engine (design §11).

Discovered by frappe through `{app}.commands` (`frappe/utils/bench_helper.py:72-86`), which
imports this module and reads the module-level `commands` list.

Every command connects, becomes Administrator, and calls **the same** `migration.api`
function a Desk button calls. That is the point of the module: a validation that lived in
the CLI would be one the UI does not have, and vice versa. `tests/test_migration_cli.py`
asserts that mapping by spying on the api functions, so a command that grows its own logic
fails the suite.
"""

import json

import click
import frappe
from frappe.commands import get_site, pass_context


def _connect(context):
	site = get_site(context)
	frappe.init(site=site)
	frappe.connect()
	frappe.set_user("Administrator")  # nosemgrep: frappe-setuser
	return site


def _finish():
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)
	frappe.destroy()


def _echo(payload):
	click.echo(json.dumps(payload, indent=2, default=str))


@click.command("cfs-migrate-analyze")
@click.option("--new", "title", help="Create a new campaign with this title first.")
@click.option("--campaign", help="An existing campaign to analyze.")
@click.option("--now/--queued", default=False, help="Run inline instead of enqueueing.")
@pass_context
def migrate_analyze(context, title=None, campaign=None, now=False):
	"""Scan the site and classify every physical object."""
	_connect(context)
	try:
		from cloud_file_storage.migration import api

		if not campaign:
			if not title:
				raise click.UsageError("pass --campaign or --new '<title>'")
			campaign = api.create_campaign(title)
			click.echo(f"created {campaign}")
		_echo(api.start_analysis(campaign, now=now))
	finally:
		_finish()


@click.command("cfs-migrate-plan")
@click.option("--campaign", required=True)
@click.option("--batch-size", type=int, default=None)
@pass_context
def migrate_plan(context, campaign, batch_size=None):
	"""Assign the analyzed objects to batches."""
	_connect(context)
	try:
		from cloud_file_storage.migration import api

		_echo(api.plan(campaign, batch_size=batch_size))
	finally:
		_finish()


@click.command("cfs-migrate-start")
@click.option("--campaign", required=True)
@click.option("--parallelism", type=int, default=None)
@click.option("--bandwidth-mbps", type=int, default=None)
@click.option("--force-fallback-queue", is_flag=True, default=False)
@pass_context
def migrate_start(context, campaign, parallelism=None, bandwidth_mbps=None, force_fallback_queue=False):
	"""Start (or resume) transferring."""
	_connect(context)
	try:
		from cloud_file_storage.migration import api

		_echo(
			api.start_migration(
				campaign,
				parallelism=parallelism,
				bandwidth_mbps=bandwidth_mbps,
				force_fallback_queue=force_fallback_queue,
			)
		)
	finally:
		_finish()


@click.command("cfs-migrate-pause")
@click.option("--campaign", required=True)
@pass_context
def migrate_pause(context, campaign):
	"""Pause after the current object."""
	_connect(context)
	try:
		from cloud_file_storage.migration import api

		_echo(api.pause(campaign))
	finally:
		_finish()


@click.command("cfs-migrate-resume")
@click.option("--campaign", required=True)
@pass_context
def migrate_resume(context, campaign):
	"""Resume a paused campaign."""
	_connect(context)
	try:
		from cloud_file_storage.migration import api

		_echo(api.resume(campaign))
	finally:
		_finish()


@click.command("cfs-migrate-stop")
@click.option("--campaign", required=True)
@pass_context
def migrate_stop(context, campaign):
	"""Stop after the current object/batch."""
	_connect(context)
	try:
		from cloud_file_storage.migration import api

		_echo(api.stop(campaign))
	finally:
		_finish()


@click.command("cfs-migrate-status")
@click.option("--campaign", required=True)
@click.option("--watch", is_flag=True, default=False)
@click.option("--interval", type=int, default=5)
@pass_context
def migrate_status(context, campaign, watch=False, interval=5):
	"""Render the counter snapshot."""
	_connect(context)
	try:
		import time

		from cloud_file_storage.migration import api

		while True:
			payload = api.status(campaign)
			_render_status(payload)
			if not watch:
				break
			time.sleep(max(1, interval))
			frappe.db.rollback()
	except KeyboardInterrupt:  # pragma: no cover - interactive only
		click.echo("")
	finally:
		_finish()


def _render_status(payload: dict):
	snapshot = payload.get("snapshot") or {}
	convergence = payload.get("convergence") or {}
	click.echo(
		f"{snapshot.get('name')}  {snapshot.get('status')}  phase={snapshot.get('active_phase') or '-'}  "
		f"{snapshot.get('progress_pct', 0)}%"
	)
	for field in (
		"objects_pending",
		"objects_uploading",
		"objects_uploaded",
		"objects_verified",
		"objects_cleaned",
		"objects_failed",
		"objects_conflict",
		"objects_skipped",
		"objects_adopted",
		"objects_dedup_reused",
	):
		click.echo(f"  {field:<22} {snapshot.get(field, 0)}")
	click.echo(
		f"  convergence            {convergence.get('ratio', 0):.4f} of {convergence.get('in_scope', 0)}"
	)
	click.echo(f"  open blockers          {snapshot.get('open_blockers', 0)}")


@click.command("cfs-migrate-verify")
@click.option("--campaign", required=True)
@pass_context
def migrate_verify(context, campaign):
	"""Re-open the VERIFY phase for batches that failed it."""
	_connect(context)
	try:
		from cloud_file_storage.migration import api

		_echo(api.start_verify(campaign))
	finally:
		_finish()


@click.command("cfs-migrate-retry")
@click.option("--campaign", required=True)
@pass_context
def migrate_retry(context, campaign):
	"""Requeue the failed objects that still have attempts left."""
	_connect(context)
	try:
		from cloud_file_storage.migration import api

		_echo(api.retry_failed(campaign))
	finally:
		_finish()


@click.command("cfs-migrate-approve-cleanup")
@click.option("--campaign", required=True)
@click.option("--note", default=None)
@pass_context
def migrate_approve_cleanup(context, campaign, note=None):
	"""Record approval to delete verified local copies. Deletes nothing by itself."""
	_connect(context)
	try:
		from cloud_file_storage.migration import api

		_echo(api.approve_cleanup(campaign, note=note))
	finally:
		_finish()


@click.command("cfs-migrate-cleanup")
@click.option("--campaign", required=True)
@click.option(
	"--confirm-phrase",
	default=None,
	help="Must be exactly: DELETE LOCAL FILES. The server, not this flag, enforces it.",
)
@click.option("--direct-delete", is_flag=True, default=False)
@pass_context
def migrate_cleanup(context, campaign, confirm_phrase=None, direct_delete=False):
	"""Start the approved cleanup pass. Requires the type-to-confirm phrase."""
	_connect(context)
	try:
		from cloud_file_storage.migration import api

		_echo(api.start_cleanup(campaign, confirm_phrase=confirm_phrase, direct_delete=direct_delete))
	finally:
		_finish()


@click.command("cfs-migrate-report")
@click.option("--campaign", required=True)
@click.option("--format", "fmt", type=click.Choice(["csv", "jsonl"]), default="csv")
@pass_context
def migrate_report(context, campaign, fmt="csv"):
	"""Export one row per migration object."""
	_connect(context)
	try:
		from cloud_file_storage.migration import api

		click.echo(api.export_report(campaign, fmt))
	finally:
		_finish()


@click.command("cfs-migrate-reconcile")
@click.option("--prefix", default=None)
@pass_context
def migrate_reconcile(context, prefix=None):
	"""Three-way diff between the bucket, the objects and the File rows."""
	_connect(context)
	try:
		from cloud_file_storage.migration import api

		_echo(api.reconcile(prefix=prefix))
	finally:
		_finish()


@click.command("cfs-migrate-preflight")
@click.option("--campaign", required=True)
@click.option("--target-rows", type=int, default=1_200_000)
@pass_context
def migrate_preflight(context, campaign, target_rows=1_200_000):
	"""Measure the F4 capacity quantities and project them to production scale."""
	_connect(context)
	try:
		from cloud_file_storage.migration import api

		_echo(api.run_preflight(campaign, target_rows=target_rows))
	finally:
		_finish()


@click.command("cfs-migrate-purge")
@click.option("--campaign", required=True)
@click.option("--older-than-days", type=int, default=90)
@pass_context
def migrate_purge(context, campaign, older_than_days=90):
	"""Drop the snapshot rows of a finished campaign, keeping its record."""
	_connect(context)
	try:
		from cloud_file_storage.migration import api

		_echo(api.purge_snapshot(campaign, older_than_days=older_than_days))
	finally:
		_finish()


# --- backup ------------------------------------------------------------------------------


@click.command("cfs-backup-now")
@click.option("--inline/--queued", default=True, help="Run in this process instead of enqueueing.")
@pass_context
def backup_now(context, inline=True):
	"""Take a verified cloud backup."""
	_connect(context)
	try:
		from cloud_file_storage.backup import tasks

		if inline:
			_echo({"backup_log": tasks.take_cloud_backup(trigger="manual")})
		else:
			_echo({"job_id": tasks.backup_now()})
	finally:
		_finish()


@click.command("cfs-backup-verify")
@click.argument("path", type=click.Path(exists=True))
@click.option("--key", default=None, help="The object key, when it differs from the filename.")
@pass_context
def backup_verify(context, path, key=None):
	"""Check a downloaded artifact against the Backup Log SHA256 — run before bench restore."""
	_connect(context)
	try:
		from cloud_file_storage.backup import restore

		_echo(restore.verify_downloaded_artifact(path, key))
	except Exception as exc:  # noqa: BLE001 - a non-zero exit is the point
		_echo({"verified": False, "error": str(exc)})
		raise SystemExit(1) from exc
	finally:
		_finish()


@click.command("cfs-backup-list")
@click.option("--limit", type=int, default=20)
@pass_context
def backup_list(context, limit=20):
	"""Recent backups with their artifact keys and recorded digests."""
	_connect(context)
	try:
		from cloud_file_storage.backup import restore

		_echo(restore.list_backups(limit=limit))
	finally:
		_finish()


@click.command("cfs-lifecycle-preview")
@pass_context
def lifecycle_preview(context):
	"""The lifecycle policy that would be applied, what it drops, and the economics."""
	_connect(context)
	try:
		from cloud_file_storage.backup import lifecycle

		_echo(lifecycle.preview_lifecycle_policy())
	finally:
		_finish()


@click.command("cfs-lifecycle-apply")
@click.option("--confirm", required=True, help='Must be exactly "APPLY LIFECYCLE".')
@pass_context
def lifecycle_apply(context, confirm):
	"""Apply the merged lifecycle policy to the backup bucket."""
	_connect(context)
	try:
		from cloud_file_storage.backup import lifecycle

		_echo(lifecycle.apply_lifecycle_policy(confirm_phrase=confirm))
	finally:
		_finish()


# --- external-install bootstrap ------------------------------------------------------------


@click.command("cfs-adopt-legacy-install")
@click.option("--dry-run", is_flag=True, default=False, help="Report what would change, change nothing.")
@pass_context
def adopt_legacy_install(context, dry_run=False):
	"""Prepare a frappe_s3_attachment 0.2.x site so `bench migrate` can run (A21).

	The one command in this module that is NOT a thin wrapper over `migration/api.py`, and
	deliberately so: it exists precisely for the window in which this app's Desk does not
	exist yet on that site. `bench migrate` reads `installed_apps` unfiltered
	(`frappe/modules/patch_handler.py:89`, `frappe/model/sync.py:43`) and raises on the
	vanished module before any patch of ours can run — hook *loading* filters to apps present
	on the bench (`frappe/__init__.py:1584`), which is why this command still works there.

	Idempotent, additive, and safe to run on a site that was never a legacy install: it
	reports "not a legacy install" and changes nothing.
	"""
	site = _connect(context)
	try:
		from cloud_file_storage import legacy_install

		report = legacy_install.bootstrap(dry_run=dry_run)
		_echo(report)
		if not report.get("legacy_install"):
			click.echo(f"{site}: no frappe_s3_attachment residue found — nothing to adopt.")
		elif dry_run:
			click.echo("Dry run: nothing was written.")
		else:
			click.echo(f"Now run: bench --site {site} migrate")
	finally:
		_finish()


commands = [
	migrate_analyze,
	migrate_plan,
	migrate_start,
	migrate_pause,
	migrate_resume,
	migrate_stop,
	migrate_status,
	migrate_verify,
	migrate_retry,
	migrate_approve_cleanup,
	migrate_cleanup,
	migrate_report,
	migrate_reconcile,
	migrate_preflight,
	migrate_purge,
	backup_now,
	backup_verify,
	backup_list,
	lifecycle_preview,
	lifecycle_apply,
	adopt_legacy_install,
]
