from . import __version__ as app_version

app_name = "cloud_file_storage"
app_title = "Cloud File Storage"
app_publisher = "Finstein"
app_description = "Enterprise cloud storage for Frappe attachments"
app_icon = "octicon octicon-file-directory"
app_color = "grey"
app_email = "prabakaran.b@finstein.ai"
app_license = "MIT"

after_install = "cloud_file_storage.install.after_install"
after_migrate = "cloud_file_storage.install.after_migrate"

# The read side. `override_doctype_class` is last-wins (base_document.py:91), so this app
# and another File-storage app cannot coexist — install.py checks and fails loudly.
override_doctype_class = {  # nosemgrep: override-doctype-class
	"File": "cloud_file_storage.overrides.file.CloudFile"
}

# The three File-lifecycle seams v15 exposes (file.py:711-714, :743-747). `write_file` and
# `delete_file_data_content` are single-owner hooks: `get_hook_method` takes index [0].
write_file = "cloud_file_storage.core_hooks.write_file"
before_write_file = "cloud_file_storage.core_hooks.before_write_file"
delete_file_data_content = "cloud_file_storage.core_hooks.delete_file_data_content"

doc_events = {
	"File": {
		"after_insert": "cloud_file_storage.core_hooks.file_after_insert",
	}
}

# Installs the serving patches once per process. `before_request` runs inside
# `init_request`, i.e. BEFORE `validate_auth` — which is exactly why the private-file
# interception itself is a module-attribute patch rather than a `before_request` handler
# (ADR-5, proven in tests/test_serving_spike.py). This hook only installs the patch; the
# patched function runs later, when the user is authenticated.
before_request = ["cloud_file_storage.serving.runtime_patches.ensure_installed"]
before_job = ["cloud_file_storage.serving.runtime_patches.ensure_installed"]

# The public `/files/...` miss path. nginx serves local hits directly; only a miss reaches
# Python, and this renderer verifies against the DB before claiming it.
page_renderer = ["cloud_file_storage.serving.public.PublicFileRenderer"]

# Write-back for paths handed out by `get_full_path()` (A3). Both run after the framework
# has already committed or rolled back, so they own their own transaction.
after_request = ["cloud_file_storage.cache.writeback.flush_request"]
after_job = ["cloud_file_storage.cache.writeback.flush_job"]

# Keeps every `/api/method/frappe_s3_attachment.controller.generate_file?...` URL that
# 0.2.x wrote into `tabFile.file_url` (and into business fields, emails and bookmarks)
# resolving after the rename — see docs/adr/amendments-register.md A10.
override_whitelisted_methods = {
	"frappe_s3_attachment.controller.generate_file": "cloud_file_storage.api.compat.legacy_generate_file",
}

# `scheduler_events` cannot target a custom queue (scheduled_job_type.py:181-182), so these
# entries stay O(ms) dispatchers that enqueue the real work on `cloud_migration`.
scheduler_events = {
	# The migration dispatcher: one indexed SELECT and an immediate return when no campaign
	# is Running, so the tick costs nothing on a site that is not migrating. It is the
	# crash-recovery watchdog; continuity inside a campaign comes from jobs chaining the
	# next batch themselves (ADR-M5).
	# NOTE: a cron entry cannot fire more often than the scheduler tick, and that tick is
	# 60s on v15 but **240s by default on v16** (DEFAULT_SCHEDULER_TICK). Recovery latency
	# follows the tick, so engine.dispatch_tick reports it and warns while a campaign runs;
	# set `scheduler_tick_interval: 60` in common_site_config.json to keep v15 timing.
	"cron": {
		"* * * * *": ["cloud_file_storage.migration.engine.dispatch_tick"],
		"0 3 * * *": ["cloud_file_storage.migration.cleanup.purge_expired_quarantine"],
		"30 2 * * 0": ["cloud_file_storage.migration.reconcile.scheduled_reconcile"],
	},
	# `hourly`/`daily` rather than `hourly_maintenance`/`daily_maintenance`: the *_maintenance
	# frequencies first exist in frappe **v15.79.0**, and this app's declared floor is
	# **v15.16.0** (docs/supported-versions.md — the floor is load-bearing, tied to A34's `fid`
	# argument and core's permission gate). Registering a frequency the floor does not have made
	# `install-app` refuse outright with "Frequency cannot be \"Hourly Maintenance\"" — the app
	# could not install on any frappe below 15.79. Caught by the F1 reference matrix on its first
	# run that got far enough to try.
	#
	# **What that frequency change actually costs, corrected.** An earlier version of this
	# comment said the loss was the per-site `maintenance_offset` stagger. That was wrong twice
	# over: the offset is computed and then discarded (`scheduled_job_type.py:137-140` returns a
	# freshly recomputed value), so it was never delivering a stagger at all — and the real cost
	# was invisible to it. `get_queue_name` (`:183`) routes `*Maintenance*`/`*Long*` to `long`
	# and everything else to `default`, and the scheduler enqueues without an explicit timeout,
	# so `background_jobs.py:45-52` applies 1500s on `long` against 300s on `default`. The move
	# therefore put this app's maintenance work onto the ERP's shared `default` queue with a 5x
	# shorter timeout — the exact thing `background.py`'s docstring forbids and F3 measures.
	#
	# So every entry below that is not already O(ms) now goes through its own dispatcher, which
	# enqueues the real work on `cloud_migration` with `timeout=1500` (loud `long` fallback).
	# That makes the claim above this dict true for the first time: it was inherited, and it was
	# already false for the four inline jobs before this frequency change touched them.
	"hourly": [
		"cloud_file_storage.gc.repair_pending_uploads_dispatch",
		# One self-gating dispatcher rather than one entry per frequency (design B3): the
		# scheduler cannot target a custom queue, so this is an O(ms) gate that enqueues the
		# real job on `long` when a backup is actually due.
		"cloud_file_storage.backup.tasks.run_scheduled_backup",
	],
	"daily": [
		# Each of these four does real inline work — batched S3 deletes, per-row commits, a
		# full cache walk with re-hashing — so each is dispatched rather than run on `default`.
		"cloud_file_storage.gc.run_deferred_object_gc_dispatch",
		"cloud_file_storage.gc.run_orphan_sweep_dispatch",
		# Re-derives sidecars for cache entries that can otherwise never be evicted. Runs
		# BEFORE the sweep so an entry repaired here is evictable in the same daily pass
		# rather than a day later.
		"cloud_file_storage.cache.eviction.repair_unevictable_entries_dispatch",
		"cloud_file_storage.cache.eviction.run_eviction_dispatch",
	],
	"weekly_long": [
		"cloud_file_storage.backup.tasks.purge_backup_logs",
	],
}
