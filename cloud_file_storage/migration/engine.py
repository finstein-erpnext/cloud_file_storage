"""Dispatch, batch claims, heartbeats, stale recovery and the UPLOAD phase.

**This module contains no deletion path of any kind** — no `os.remove`, no `os.rename`, no
`frappe.delete_doc`, no `storage.engine.delete`. That is PLAN §C and gate F6, and
`tests/test_migration_safety_lint.py` walks this file's AST to prove it rather than trusting
the claim. UPLOAD copies bytes upward; nothing local dies until an operator approves
CLEANUP, and CLEANUP lives in its own module.

The coordination model (ADR-M4/M5), and why each piece is the way it is:

* **MariaDB rows are the only source of truth.** RQ has no persistence and never re-queues a
  killed job, so a job that vanishes must leave the DB able to re-derive the work. Every job
  starts by re-reading its batch and claiming it with a compare-and-set; a second copy of the
  same job loses the CAS and exits silently.
* **Redis is a dispatch hint.** `deduplicate=True` only covers QUEUED/STARTED, so it is an
  optimisation, never the guard.
* **The scheduler tick is the watchdog, not the engine.** `scheduler_events` cannot target a
  custom queue, so `dispatch_tick` runs on `default`, returns in milliseconds when nothing is
  running, and exists mainly to recover from a crash; continuity between ticks comes from
  jobs chaining the next batch themselves.
"""

import os
import socket
import time
from contextlib import contextmanager

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, now_datetime

from cloud_file_storage.cloud_file_storage.doctype.cloud_migration_batch.cloud_migration_batch import (
	IN_FLIGHT_STATUSES,
)
from cloud_file_storage.migration import audit, names
from cloud_file_storage.storage import engine as storage_engine
from cloud_file_storage.storage import objects
from cloud_file_storage.storage.exceptions import CloudStorageIntegrityError
from cloud_file_storage.storage.hashing import digest_path
from cloud_file_storage.storage.modes import get_settings

CAMPAIGN_DOCTYPE = "Cloud Migration Campaign"
BATCH_DOCTYPE = "Cloud Migration Batch"
OBJECT_DOCTYPE = "Cloud Migration Object"
REF_DOCTYPE = "Cloud Migration File Ref"
CONFLICT_DOCTYPE = "Cloud Migration Conflict"

MIGRATION_QUEUE = "cloud_migration"
FALLBACK_QUEUE = "long"
JOB_TIMEOUT = 3600

#: A batch whose worker has not been heard from for this long is presumed dead. Ten minutes
#: is comfortably longer than any single object transfer and far shorter than the job
#: timeout, so recovery happens while the campaign is still moving.
STALE_AFTER_SECONDS = 600

#: Heartbeat cadence inside a batch — whichever comes first.
HEARTBEAT_EVERY_OBJECTS = 25
HEARTBEAT_EVERY_SECONDS = 15

#: How many objects one job reads at a time out of its batch.
OBJECT_PAGE_SIZE = 200

#: The columns every consumer of :func:`iter_batch_objects` needs — UPLOAD, adoption,
#: thumbnails, VERIFY and CLEANUP all read from the same row. Listed explicitly rather than
#: selected with `*` so the working set is visible and bounded (these rows are read 1.2M
#: times), and complete rather than minimal because a consumer that finds a column missing
#: does not fail loudly: `cleanup` read a row with no `cloud_storage_object` and concluded
#: the object had none, which is a refusal that looks exactly like a real one.
OBJECT_WORKING_SET = (
	"name",
	"campaign",
	"batch",
	"status",
	"classification",
	"identity_kind",
	"file_url",
	"is_private",
	"is_thumbnail",
	"parent_object",
	"disk_path",
	"on_disk",
	"disk_size",
	"disk_mtime",
	"size_bytes",
	"sha256",
	"md5",
	"db_content_hash",
	"cloud_storage_object",
	"ref_count",
	"ignored_ref_count",
	"attempt_count",
	"next_retry_at",
	"skip_reason",
	"legacy_bucket",
	"legacy_key",
	"quarantine_path",
	"cleaned_at",
	"primary_file",
)

#: Retry backoff is capped here regardless of attempt count.
MAX_BACKOFF_SECONDS = 3600


# ------------------------------------------------------------------------ queue plumbing


def migration_queue_available() -> bool:
	"""Whether this bench has the dedicated `cloud_migration` queue configured.

	`get_queues_timeout` is `lru_cache`d, so a freshly added queue only appears after the
	processes are restarted — which is exactly the deployment fact
	`docs/runbooks/deployment.md` documents.
	"""
	try:
		from frappe.utils.background_jobs import get_queues_timeout

		return MIGRATION_QUEUE in get_queues_timeout()
	except Exception:  # noqa: BLE001 - a Redis/config hiccup is "not available", not a crash
		return False


def queue_for(campaign) -> str:
	"""The queue a campaign's jobs go to. Falls back only when explicitly authorised.

	Invariant 8: the fallback is loud. It is recorded on the campaign (`fallback_queue_used`,
	rendered as a warning on the form), written to the Error Log, and can only be reached by
	an operator passing `force_fallback_queue`; `api.start_migration` refuses otherwise.
	"""
	if migration_queue_available():
		return MIGRATION_QUEUE

	if not cint(getattr(campaign, "fallback_queue_used", 0)):
		frappe.throw(
			_("The {0} queue is not configured on this bench.").format(MIGRATION_QUEUE)
			+ " "
			+ _("See docs/runbooks/deployment.md, or start the campaign with force_fallback_queue.")
		)

	frappe.log_error(
		title="Cloud migration running on the fallback queue",
		message=(
			f"Campaign {campaign.name} is enqueueing on {FALLBACK_QUEUE!r} because "
			f"{MIGRATION_QUEUE!r} is not configured. Normal ERP jobs share this queue."
		),
	)
	return FALLBACK_QUEUE


@contextmanager
def migration_job():
	"""Marks the surrounding work as background migration work.

	Two effects: audit rows are attributed to `scheduler` rather than to whichever user the
	worker happens to run as, and the storage layer uses its long-timeout client profile.
	"""
	previous = frappe.flags.get("in_migration_job")
	frappe.flags.in_migration_job = True
	try:
		yield
	finally:
		frappe.flags.in_migration_job = previous


# ----------------------------------------------------------------------------- campaigns


def _campaign(name: str):
	return frappe.get_doc(CAMPAIGN_DOCTYPE, name)


def _set_campaign(name: str, values: dict):
	frappe.db.set_value(CAMPAIGN_DOCTYPE, name, values, update_modified=False)


def control_flag(campaign: str) -> str | None:
	"""Polled per object. A primary-key read, so pause is felt within one object."""
	return frappe.db.get_value(CAMPAIGN_DOCTYPE, campaign, "control_flag")


# ---------------------------------------------------------------------------- dispatcher


def scheduler_tick_seconds() -> int:
	"""How often the scheduler actually evaluates due events on this bench.

	A ``* * * * *`` cron entry cannot fire more often than the tick, so this bounds how fast
	:func:`dispatch_tick` can recover a stale batch. frappe v15 ticks every 60s; **v16 raised
	the default to 240s** (``frappe.utils.scheduler.DEFAULT_SCHEDULER_TICK``), which quadruples
	that recovery latency with nothing raised and nothing logged. Reported rather than assumed
	so the regime is visible in the campaign record instead of inferred from a comment.
	"""
	try:
		from frappe.utils.scheduler import get_scheduler_tick

		return int(get_scheduler_tick())
	except Exception:  # noqa: BLE001 - older frappe without the helper ticks at 60s
		return 60


#: What the crash-recovery reasoning in hooks.py was written against.
ASSUMED_TICK_SECONDS = 60


def dispatch_tick():
	"""Scheduler entry point: O(ms) when nothing is running (one indexed SELECT).

	Runs on the `default` queue because `scheduled_job_type` cannot target a custom one; all
	it does there is recover stale batches and enqueue the real work onto `cloud_migration`.
	"""
	campaign = frappe.db.get_value(
		CAMPAIGN_DOCTYPE,
		{"status": ("in", ("Running", "Cleanup Running"))},
		["name", "status", "parallelism", "control_flag", "fallback_queue_used", "max_attempts"],
		as_dict=True,
	)
	tick = scheduler_tick_seconds()
	if not campaign:
		return {"dispatched": 0, "scheduler_tick_seconds": tick}

	if tick > ASSUMED_TICK_SECONDS:
		# Only while a campaign is actually running, so a quiet site stays silent.
		frappe.logger("cloud_file_storage").warning(
			"migration watchdog runs on a %ss scheduler tick (assumed %ss): stale-batch "
			"recovery is correspondingly slower. Set scheduler_tick_interval in "
			"common_site_config.json to restore it.",
			tick,
			ASSUMED_TICK_SECONDS,
		)

	if campaign.control_flag == "PAUSE":
		return {"dispatched": 0, "paused": True}

	recover_stale_batches(campaign.name)
	try:
		dispatched = dispatch_batches(campaign.name)
	except Exception as exc:  # noqa: BLE001 - a 60s tick must not become a 60s stack trace
		# The realistic cause is the dedicated queue disappearing from the bench config
		# under a running campaign. Record it where an operator will see it and stop; the
		# next tick retries, and nothing has been half-dispatched (the CAS runs first).
		note_campaign_error(campaign.name, f"dispatch failed: {exc}")
		frappe.db.commit()
		return {"dispatched": 0, "error": str(exc)}

	from cloud_file_storage.migration import report

	report.publish_campaign_snapshot(campaign.name)
	return {"dispatched": dispatched}


def recover_stale_batches(campaign: str) -> int:
	"""Reset batches whose worker stopped heartbeating, and the objects they stranded.

	This is the crash-recovery path for `kill -9`, a Redis flush and a bench restart alike:
	all three end with rows claiming a worker that no longer exists. Nothing is deleted and
	nothing is fabricated — a batch goes back to the state it was dispatched from and its
	in-flight objects go back to the state they were claimed from.
	"""
	cutoff = add_to_date(now_datetime(), seconds=-STALE_AFTER_SECONDS)
	stale = frappe.db.get_all(
		BATCH_DOCTYPE,
		filters={
			"campaign": campaign,
			"status": ("in", IN_FLIGHT_STATUSES),
			"heartbeat_at": ("<", cutoff),
		},
		fields=["name", "status", "attempts", "phase"],
		limit_page_length=0,
	)
	if not stale:
		return 0

	max_attempts = cint(frappe.db.get_value(CAMPAIGN_DOCTYPE, campaign, "max_attempts")) or 5

	recovered = 0
	for batch in stale:
		reset_to = _reset_target(batch.status)
		attempts = cint(batch.attempts) + 1
		values = {"attempts": attempts, "last_error": "heartbeat expired; recovered by dispatcher"}
		values["status"] = "Failed" if attempts > max_attempts else reset_to

		frappe.db.sql(
			f"UPDATE `tab{BATCH_DOCTYPE}` SET status=%(status)s, attempts=%(attempts)s, "
			"last_error=%(last_error)s, rq_job_id=NULL WHERE name=%(name)s AND status=%(expected)s",
			{**values, "name": batch.name, "expected": batch.status},
		)
		# The UPDATE above is a CAS, so it can lose: another tick may have recovered this
		# batch first. Everything below is gated on having actually won, because an
		# append-only table full of unconditional writes is no better evidence than the
		# mutable counter it replaced — and this row is now what proves crash recovery.
		if cint(frappe.db._cursor.rowcount) != 1:
			frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)
			continue

		_release_stranded_objects(batch.name)
		# A durable record of the recovery. `attempts` and `last_error` are both overwritten
		# by later phases — `finalize_upload_batch` resets attempts on progress, and
		# `api._open_cleanup_batches` zeroes it on every batch when CLEANUP starts — so a
		# counter is not evidence that a crash was recovered from. This row is.
		audit.record(
			"campaign_transition",
			campaign=campaign,
			operation="stale_batch_recovered",
			batch=batch.name,
			from_status=batch.status,
			to_status=values["status"],
			attempts=attempts,
		)
		recovered += 1
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)

	return recovered


def _reset_target(status: str) -> str:
	"""Where a stalled batch goes back to — never forward."""
	return {
		"UploadDispatched": "Pending",
		"Uploading": "Pending",
		"VerifyDispatched": "Uploaded",
		"Verifying": "Uploaded",
		"CleanupDispatched": "Verified",
		"Cleaning": "Verified",
	}.get(status, "Pending")


def _release_stranded_objects(batch: str):
	"""Objects a dead worker left mid-transition go back one step, never forward.

	`Uploading → Pending` re-uploads to the same content-addressed key, which is
	overwrite-idempotent. `Verifying → Uploaded` re-runs a read-only HEAD. Neither can lose
	bytes, and neither can promote an object nobody actually finished.
	"""
	frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- interpolates only closed-set identifiers (module constants / _file_columns()); every value is parameter-bound
		f"UPDATE `tab{OBJECT_DOCTYPE}` SET status='Pending' WHERE batch=%(batch)s AND status='Uploading'",
		{"batch": batch},
	)
	frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- interpolates only closed-set identifiers (module constants / _file_columns()); every value is parameter-bound
		f"UPDATE `tab{OBJECT_DOCTYPE}` SET status='Uploaded' WHERE batch=%(batch)s AND status='Verifying'",
		{"batch": batch},
	)


def dispatch_batches(campaign: str) -> int:
	"""Keep at most `parallelism` batches in flight, verify-priority first."""
	camp = _campaign(campaign)
	if camp.control_flag in ("PAUSE", "STOP"):
		return 0

	in_flight = frappe.db.count(BATCH_DOCTYPE, {"campaign": campaign, "status": ("in", IN_FLIGHT_STATUSES)})
	slots = cint(camp.parallelism) - in_flight
	if slots <= 0:
		return 0

	dispatched = 0
	for batch in _next_batches(campaign, slots):
		if _enqueue_batch(camp, batch):
			dispatched += 1
	return dispatched


#: A Pending batch is only worth a job when it has work that is due *now*. A batch whose
#: remaining objects are all backing off (a transient S3 error, a thumbnail waiting for its
#: source) would otherwise be dispatched, find nothing to do, fail A31's zero-residue check,
#: go back to Pending and be dispatched again — a tight loop that burns the batch's bounded
#: attempts and marks a perfectly healthy batch Failed. The `NOT EXISTS` arm keeps a batch
#: whose objects all ended in Conflict/Skipped dispatchable, so it can still finish.
DUE_BATCHES_SQL = """
	SELECT b.name, b.status, b.phase
	FROM `tabCloud Migration Batch` b
	WHERE b.campaign = %(campaign)s
	  AND b.status = 'Pending'
	  AND (
	    EXISTS (
	      SELECT 1 FROM `tabCloud Migration Object` o
	      WHERE o.batch = b.name
	        AND o.status IN ('Pending', 'Uploading')
	        AND (o.next_retry_at IS NULL OR o.next_retry_at <= NOW())
	    )
	    OR NOT EXISTS (
	      SELECT 1 FROM `tabCloud Migration Object` o2
	      WHERE o2.batch = b.name AND o2.status IN ('Pending', 'Uploading')
	    )
	  )
	ORDER BY b.batch_no ASC
	LIMIT %(limit)s
"""


def _next_batches(campaign: str, limit: int) -> list[dict]:
	"""Uploaded batches before Pending ones: verification frees the campaign to finish."""
	if limit <= 0:
		return []

	ready = frappe.db.get_all(
		BATCH_DOCTYPE,
		filters={"campaign": campaign, "status": "Uploaded"},
		fields=["name", "status", "phase"],
		order_by="batch_no asc",
		limit_page_length=limit,
	)
	if len(ready) < limit:
		ready += frappe.db.sql(
			DUE_BATCHES_SQL, {"campaign": campaign, "limit": limit - len(ready)}, as_dict=True
		)

	verified = [
		row
		for row in frappe.db.get_all(
			BATCH_DOCTYPE,
			filters={"campaign": campaign, "status": "Verified", "phase": "CLEANUP"},
			fields=["name", "status", "phase"],
			order_by="batch_no asc",
			limit_page_length=max(0, limit - len(ready)),
		)
	]
	return ready + verified


def _enqueue_batch(camp, batch: dict) -> bool:
	"""Move the batch to its Dispatched state under CAS, then enqueue.

	The CAS comes first on purpose: if the enqueue fails, a batch sitting in `UploadDispatched`
	with no heartbeat is recovered by the next tick, whereas a job enqueued for a batch that
	was never claimed can run twice.
	"""
	if batch["status"] == "Pending":
		target, method = "UploadDispatched", "cloud_file_storage.migration.engine.run_upload_batch"
	elif batch["status"] == "Uploaded":
		target, method = "VerifyDispatched", "cloud_file_storage.migration.verify.run_verify_batch"
	elif batch["status"] == "Verified":
		target, method = "CleanupDispatched", "cloud_file_storage.migration.cleanup.run_cleanup_batch"
	else:
		return False

	if not cas_batch(batch["name"], expected=batch["status"], to=target, heartbeat=True):
		return False

	queue = queue_for(camp)
	job_id = f"cfs::{camp.name}::{target}::{batch['name']}"
	frappe.db.set_value(BATCH_DOCTYPE, batch["name"], "rq_job_id", job_id, update_modified=False)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)

	try:
		frappe.enqueue(
			method,
			queue=queue,
			timeout=JOB_TIMEOUT,
			job_id=job_id,
			deduplicate=True,
			campaign=camp.name,
			batch=batch["name"],
		)
	except frappe.QueueOverloaded:
		# v16 caps every queue by default (MAX_QUEUED_JOBS, frappe/utils/background_jobs.py);
		# v15 only capped when max_queued_jobs was configured. The CAS above already committed,
		# so this batch is left claimed with a heartbeat and the next tick recovers it as
		# stale -- the designed degradation. Letting it propagate would instead fail the whole
		# scheduler job and stall every campaign until the following tick.
		frappe.logger("cloud_file_storage").warning(
			"queue %s is at capacity; batch %s stays claimed for stale recovery", queue, batch["name"]
		)
		return False
	return True


def chain_next(campaign: str):
	"""Called at the end of every batch job — continuity between 60s scheduler ticks."""
	campaign_status = frappe.db.get_value(CAMPAIGN_DOCTYPE, campaign, "status")
	if campaign_status not in ("Running", "Cleanup Running"):
		return 0
	return dispatch_batches(campaign)


# --------------------------------------------------------------------------- batch claim


def cas_batch(batch: str, *, expected: str, to: str, heartbeat: bool = False) -> bool:
	"""Compare-and-set on the batch row. `affected_rows == 1` or the caller must give up.

	This is the real double-execution guard (ADR-M4). RQ's `deduplicate` covers only jobs
	that are queued or started, so a job that was re-enqueued after a Redis flush can
	genuinely run twice; the loser fails here and exits without touching an object.
	"""
	values = {"status": to}
	if heartbeat:
		values["heartbeat_at"] = now_datetime()
		values["worker_host"] = socket.gethostname()[:140]

	assignments = ", ".join(f"`{field}`=%({field})s" for field in values)
	frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- keys are local literals built in this function; do NOT add **kwargs here without an allow-list like ALLOWED_OBJECT_CAS_FIELDS
		f"UPDATE `tab{BATCH_DOCTYPE}` SET {assignments} WHERE name=%(name)s AND status=%(expected)s",
		{**values, "name": batch, "expected": expected},
	)
	claimed = cint(frappe.db._cursor.rowcount) == 1
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)
	return claimed


def heartbeat(batch: str, *, done: int | None = None, failed: int | None = None, bytes_done: int = 0):
	values = {"heartbeat_at": now_datetime()}
	if done is not None:
		values["objects_done"] = done
	if failed is not None:
		values["objects_failed"] = failed
	if bytes_done:
		values["bytes_done"] = bytes_done
	frappe.db.set_value(BATCH_DOCTYPE, batch, values, update_modified=False)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)


class Heartbeat:
	"""Cheap "am I due?" wrapper so a batch loop is not writing a row per object."""

	def __init__(self, batch: str):
		self.batch = batch
		self.count = 0
		self.bytes = 0
		self.failed = 0
		self._last = time.monotonic()

	def tick(self, *, size: int = 0, failed: bool = False):
		self.count += 1
		self.bytes += cint(size)
		if failed:
			self.failed += 1
		due = self.count % HEARTBEAT_EVERY_OBJECTS == 0
		if due or (time.monotonic() - self._last) >= HEARTBEAT_EVERY_SECONDS:
			self.flush()

	def flush(self):
		heartbeat(self.batch, done=self.count, failed=self.failed, bytes_done=self.bytes)
		self._last = time.monotonic()


class ThrottleBudget:
	"""Elapsed-vs-bytes token bucket, per process (design §6.3 step 6).

	The configured limit is the whole campaign's, so each of `parallelism` workers gets its
	share. Zero means off, which is the default: at ~83KB average the corpus is
	request-rate bound and the network is idle.
	"""

	def __init__(self, mbps: int | None, parallelism: int = 1):
		self.bytes_per_second = (cint(mbps) * 1_000_000 / 8) / max(1, cint(parallelism) or 1)
		self.started = time.monotonic()
		self.consumed = 0
		# Seconds this budget actually spent sleeping. A throttle that never slept did not
		# demonstrate a limit, however favourably its rate compares with a ceiling — an
		# overhead-bound corpus can sit at 10% of the ceiling with `time.sleep` never called.
		self.slept = 0.0
		# How many times `consume` was called. Since the tally moved beside the PUT, this is
		# exactly the count of objects whose bytes crossed the network, which lets a caller
		# separate uploaded objects from dedupe-converged ones without inferring it from bytes.
		self.calls = 0

	@property
	def enabled(self) -> bool:
		return self.bytes_per_second > 0

	def consume(self, size: int) -> float:
		# **Counted before the `enabled` gate, deliberately.** `consumed` is the authoritative
		# tally of bytes that actually crossed the network on this batch — it is incremented
		# only from the real upload path, and objects that dedup-reuse an existing object never
		# reach it. The F3 throttle measurement needs that number for BOTH the limited and the
		# unlimited batch, and an early return here left the unlimited one at zero.
		#
		# Only the *sleeping* is conditional on a limit being set. Counting is not.
		self.consumed += cint(size)
		self.calls += 1
		if not self.enabled:
			return 0.0
		allowed_elapsed = self.consumed / self.bytes_per_second
		sleep_for = allowed_elapsed - (time.monotonic() - self.started)
		if sleep_for > 0:
			self.slept += sleep_for
			time.sleep(sleep_for)
			return sleep_for
		return 0.0


# ------------------------------------------------------------------------- UPLOAD phase


def run_upload_batch(campaign: str, batch: str) -> dict:
	"""One batch of uploads. Owns its transaction, commits per object (ADR-M6)."""
	with migration_job():
		if not cas_batch(batch, expected="UploadDispatched", to="Uploading", heartbeat=True):
			return {"claimed": False}

		camp = _campaign(campaign)
		settings = get_settings()
		beat = Heartbeat(batch)
		throttle = ThrottleBudget(camp.bandwidth_limit_mbps, camp.parallelism)
		stopped = False

		try:
			for obj in iter_batch_objects(batch, ("Pending",)):
				flag = control_flag(campaign)
				if flag in ("PAUSE", "STOP"):
					stopped = True
					break

				size = process_object_upload(obj, camp, settings, throttle)
				beat.tick(size=size or 0, failed=size is None)
		finally:
			beat.flush()

		finalize_upload_batch(batch, camp, stopped=stopped)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)

		from cloud_file_storage.migration import analyzer

		analyzer.refresh_counters(campaign)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)

		if not stopped:
			chain_next(campaign)
		return {
			"claimed": True,
			"objects": beat.count,
			"failed": beat.failed,
			# Bytes that genuinely crossed the network, from the throttle's own tally. NOT
			# `bytes_done`, which is progress accounting and counts a dedup-reused object at
			# its nominal size because the object really did advance. Measuring a network rate
			# with progress bytes overstates it by exactly the deduplicated volume.
			"transferred_bytes": throttle.consumed,
			# Engagement witness. A rate under the ceiling proves nothing on its own — an
			# overhead-bound corpus sits under any ceiling with the limiter never sleeping.
			# The F3 verdict requires this to be > 0 on the throttled batch.
			"throttle_slept_seconds": round(throttle.slept, 6),
			"consume_calls": throttle.calls,
		}


def iter_batch_objects(batch: str, statuses: tuple[str, ...]):
	"""Keyset page over `(batch, status)`; re-reads each page so retries stay eligible.

	Deliberately re-queries rather than materialising 1000 documents: an object whose status
	changed under us (an operator skipped it, a conflict opened) must drop out of the loop.
	"""
	cursor = ""
	now = now_datetime()
	while True:
		page = frappe.db.get_all(
			OBJECT_DOCTYPE,
			filters={
				"batch": batch,
				"status": ("in", statuses),
				"name": (">", cursor),
			},
			or_filters=[["next_retry_at", "is", "not set"], ["next_retry_at", "<=", now]],
			fields=list(OBJECT_WORKING_SET),
			order_by="name asc",
			limit_page_length=OBJECT_PAGE_SIZE,
		)
		if not page:
			return
		yield from page
		cursor = page[-1].name


#: The only column names `cas_object` will interpolate. Independent security review found that
#: the closed set was a **call-site convention rather than an enforced property**: the keys come
#: from `**values`, and CPython lets a non-identifier string through `**{...}` unpacking. Every
#: call site passes literals today, so there was no injection — but "no caller does this" is not
#: a guarantee, and the suppression annotation below claimed a closed set that nothing closed.
ALLOWED_OBJECT_CAS_FIELDS = frozenset(
	{
		"status",
		"conflict",
		"attempt_count",
		"cleaned_at",
		"cloud_storage_object",
		"disk_path",
		"error_class",
		"file_url",
		"last_error",
		"legacy_bucket",
		"legacy_key",
		"md5",
		"next_retry_at",
		"quarantine_path",
		"sha256",
		"size_bytes",
		"skip_reason",
		"uploaded_at",
		"verified_at",
	}
)


def cas_object(name: str, *, expected: str, to: str, **values) -> bool:
	"""Single-row compare-and-set on a migration object, with its own commit.

	Field names are checked against `ALLOWED_OBJECT_CAS_FIELDS` before any of them reaches the
	SQL text, so the identifier set is closed **here** rather than at the call sites.
	"""
	values["status"] = to
	unknown = set(values) - ALLOWED_OBJECT_CAS_FIELDS
	if unknown:
		raise ValueError(f"cas_object refuses unknown field(s): {sorted(unknown)}")
	assignments = ", ".join(f"`{field}`=%({field})s" for field in values)
	frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- identifiers checked against ALLOWED_OBJECT_CAS_FIELDS above; values parameter-bound
		f"UPDATE `tab{OBJECT_DOCTYPE}` SET {assignments} WHERE name=%(name)s AND status=%(expected)s",
		{**values, "name": name, "expected": expected},
	)
	changed = cint(frappe.db._cursor.rowcount) == 1
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)
	return changed


def process_object_upload(obj, camp, settings, throttle) -> int | None:
	"""Move one object forward. Returns the bytes moved, or None when it did not advance.

	Three shapes of "upload", dispatched before the claim because each owns its own CAS:

	* **adoption** (`legacy_fork_key` / `remote_https`) — the bytes are already in the
	  bucket, so nothing is transferred and the object becomes `Adopted` (A15/A18);
	* **thumbnail** — a derived object keyed off its source's hash, not an attachment of
	  its own (PLAN §A);
	* everything else — one streamed hash pass and one checksummed PUT.

	Every exit path commits its own transaction, so a crash costs exactly one object.
	"""
	if obj.identity_kind in ("legacy_fork_key", "remote_https"):
		from cloud_file_storage.migration import adoption

		return cint(obj.size_bytes) if adoption.adopt_object(obj, camp, settings=settings) else None

	if cint(obj.is_thumbnail):
		from cloud_file_storage.migration import thumbnails as migration_thumbnails

		return migration_thumbnails.upload_thumbnail_object(obj, camp, settings=settings)

	if not cas_object(obj.name, expected="Pending", to="Uploading"):
		return None

	try:
		path = obj.disk_path
		if not path or not os.path.exists(path):
			return _handle_vanished_source(obj, camp)

		# `disk_path` is Desk-writable, and this path's BYTES are about to be uploaded into the
		# bucket as an ordinary object. An unconfined read here is exfiltration, not corruption:
		# `site_config.json` would become a servable object carrying the DB password and the S3
		# secret. Refused, recorded, and never read.
		from cloud_file_storage.migration import analyzer

		if not analyzer.confined_to_site_files(path):
			# TERMINAL, not transient. A path outside the site will never become a path inside
			# it, so retrying an attack input `max_attempts` times is meaningless work and a
			# Warning understates it. `_open_conflict` opens one Conflict per (object, type) and
			# CAS's Uploading -> Failed, which is the disposition this deserves.
			#
			# The first version of this call passed `error_class=`/`details=` to
			# `_record_object_failure`, which takes `(obj, camp, exc)` — so it raised TypeError,
			# was swallowed by the blanket handler below, and the object went back to Pending as
			# a retryable "TypeError". The guard still held (the raise preceded `digest_path`),
			# but the refusal path had never run. Found by independent security review, which
			# also named why the suite missed it: a test asserting "the bytes are not uploaded"
			# is green either way. The assertion has to be on what is RECORDED.
			_open_conflict(
				obj,
				camp,
				conflict_type="upload_failed",
				severity="Blocker",
				details={"disk_path": path, "reason": "outside this site's file trees"},
				error_class="PathOutsideSite",
			)
			return None

		digest = digest_path(path)
		visibility = "private" if cint(obj.is_private) else "public"

		if cint(camp.dedupe_by_content_hash):
			existing = objects.find_by_identity(digest.sha256, visibility, settings.bucket)
			if existing and _is_present(existing):
				cas_object(
					obj.name,
					expected="Uploading",
					to="DedupReused",
					cloud_storage_object=existing,
					sha256=digest.sha256,
					md5=digest.md5,
					size_bytes=digest.size,
					uploaded_at=now_datetime(),
				)
				return digest.size

		cso = objects.ensure_cso(
			content_sha256=digest.sha256,
			content_hash_md5=digest.md5,
			file_size=digest.size,
			visibility=visibility,
			mime_type=_mime_type(obj.file_url),
			source_path=path,
			migration_batch=obj.get("batch") or camp.name,
			settings=settings,
		)

		if objects.needs_upload(cso):
			response = storage_engine.put_path(cso, path, settings=settings)
			objects.set_cso_status(
				cso.name,
				"uploaded",
				etag=(response or {}).get("ETag"),
				checksum_sha256_s3=(response or {}).get("ChecksumSHA256"),
			)
			# **Counted here, beside the PUT, not after the CAS.** `consumed` is the tally of bytes
			# that actually crossed the network, and it is also what the limiter sleeps on. When
			# `ensure_cso` converges on an already-uploaded object nothing is sent, so counting it
			# both overstated the measured rate and throttled bandwidth for a transfer that never
			# happened. Progress accounting is a different quantity and still returns `digest.size`.
			throttle.consume(digest.size)

		cas_object(
			obj.name,
			expected="Uploading",
			to="Uploaded",
			cloud_storage_object=cso.name,
			sha256=digest.sha256,
			md5=digest.md5,
			size_bytes=digest.size,
			uploaded_at=now_datetime(),
			error_class=None,
			last_error=None,
		)
		return digest.size

	except Exception as exc:  # noqa: BLE001 - per-object isolation is the whole design
		_record_object_failure(obj, camp, exc)
		return None


def _is_present(cso_name: str) -> bool:
	"""Dedup may only reuse an object whose bytes are known to be in the bucket."""
	from cloud_file_storage.cloud_file_storage.doctype.cloud_storage_object.cloud_storage_object import (
		PRESENT_STATUSES,
	)

	return frappe.db.get_value(objects.CSO_DOCTYPE, cso_name, "status") in PRESENT_STATUSES


def _mime_type(file_url: str | None) -> str | None:
	import mimetypes

	if not file_url:
		return None
	return mimetypes.guess_type(file_url)[0]


def _handle_vanished_source(obj, camp) -> int:
	"""The local file is not where the scan found it (design §6.3 step 2).

	Three real causes, and all three are handled by re-reading the DB rather than by guessing:
	the File rows were deleted mid-flight, an `is_private` flip moved the bytes
	(`shutil.move` in core's `file.py`), or an overwrite replaced them. Nothing is deleted
	here and nothing is marked done.
	"""
	refs = frappe.db.get_all(
		REF_DOCTYPE, filters={"migration_object": obj.name}, pluck="file", limit_page_length=0
	)
	live = [name for name in refs if frappe.db.exists("File", name)] if refs else []

	if refs and not live:
		cas_object(
			obj.name,
			expected="Uploading",
			to="Skipped",
			skip_reason="deleted_during_migration",
		)
		return 0

	from cloud_file_storage.migration import analyzer

	current_url = frappe.db.get_value("File", live[0], "file_url") if live else None
	new_path = analyzer.local_disk_path(analyzer.normalize_url(current_url)) if current_url else None

	if new_path and os.path.exists(new_path):
		cas_object(
			obj.name,
			expected="Uploading",
			to="Pending",
			disk_path=new_path,
			file_url=current_url,
			attempt_count=cint(obj.attempt_count) + 1,
			last_error="source moved; identity refreshed from the File row",
		)
		return 0

	_open_conflict(
		obj,
		camp,
		conflict_type="missing_physical",
		severity="Blocker",
		details={"disk_path": obj.disk_path, "file_url": obj.file_url, "live_refs": live},
		error_class="FileVanished",
	)
	return 0


def backoff_seconds(attempt: int, base: int) -> int:
	"""`base × 2^(attempt-1)`, ±25% jitter, capped (design §2.1)."""
	import random

	attempt = max(1, cint(attempt))
	delay = cint(base or 60) * (2 ** min(attempt - 1, 32))
	delay = min(delay, MAX_BACKOFF_SECONDS)
	jitter = delay * 0.25
	# Capped AFTER the jitter: capping first lets +25% carry the result back over the cap,
	# which is how a "bounded" backoff quietly stops being bounded.
	return max(1, min(MAX_BACKOFF_SECONDS, int(delay + random.uniform(-jitter, jitter))))


def _record_object_failure(obj, camp, exc: Exception):
	"""Transient → back to Pending with backoff; exhausted or fatal → Failed + Conflict."""
	attempt = cint(obj.attempt_count) + 1
	error_class = type(exc).__name__
	message = str(exc)[:500]

	fatal = isinstance(exc, CloudStorageIntegrityError)
	exhausted = attempt >= cint(camp.max_attempts or 5)

	# Deliberately not conditioned on whether the failure was transient. `max_attempts` is
	# the bound on retrying a transient error, so once it is spent a transient failure has to
	# stop and surface exactly like any other — an object that retried for ever would never
	# reach the operator. The severity below is where the two differ: an integrity failure is
	# a Blocker, an exhausted transport failure a Warning.
	# (The condition previously read `fatal or (exhausted and not transient) or exhausted`,
	# in which the middle term could not affect the result.)
	if fatal or exhausted:
		_open_conflict(
			obj,
			camp,
			conflict_type="upload_failed",
			severity="Blocker" if fatal else "Warning",
			details={"error": message, "attempts": attempt},
			error_class=error_class,
			attempt_count=attempt,
		)
		return

	cas_object(
		obj.name,
		expected="Uploading",
		to="Pending",
		attempt_count=attempt,
		next_retry_at=add_to_date(
			now_datetime(), seconds=backoff_seconds(attempt, camp.retry_backoff_base_secs)
		),
		error_class=error_class,
		last_error=message,
	)


def _open_conflict(obj, camp, *, conflict_type, severity, details, error_class=None, attempt_count=None):
	"""Halt one object and record why, exactly once per (object, type)."""
	existing = frappe.db.get_value(
		CONFLICT_DOCTYPE,
		{"campaign": obj.campaign, "migration_object": obj.name, "conflict_type": conflict_type},
		"name",
	)
	if not existing:
		conflict = frappe.new_doc(CONFLICT_DOCTYPE)
		conflict.update(
			{
				"campaign": obj.campaign,
				"migration_object": obj.name,
				"conflict_type": conflict_type,
				"status": "Open",
				"severity": severity,
				"details": frappe.as_json(details),
			}
		)
		conflict.insert(ignore_permissions=True)
		existing = conflict.name

	values = {"conflict": existing, "error_class": error_class, "last_error": frappe.as_json(details)[:500]}
	if attempt_count is not None:
		values["attempt_count"] = attempt_count

	for expected in ("Uploading", "Verifying", "Pending", "Uploaded"):
		if cas_object(obj.name, expected=expected, to="Failed", **values):
			break
	return existing


# ------------------------------------------------------------------------ A31 finalisation


def finalize_upload_batch(batch: str, camp, *, stopped: bool = False) -> str:
	"""A31 — `Uploading → Uploaded` only when the batch has **zero** non-terminal objects.

	The failure this exists to prevent: a job that broke out of its loop early (pause, stop,
	a control-flag flip, an exception on the last object) used to mark the batch `Uploaded`
	anyway, and VERIFY would then declare a batch verified while some of its objects had
	never been uploaded at all. So the transition is a compare-and-set guarded by a count,
	and residue sends the batch back to `Pending` with a bounded attempt counter instead.
	"""
	residue = frappe.db.count(OBJECT_DOCTYPE, {"batch": batch, "status": ("in", ("Pending", "Uploading"))})

	if residue == 0:
		# Genuinely finished, whether or not a pause arrived after the last object.
		if cas_batch(batch, expected="Uploading", to="Uploaded"):
			return "Uploaded"
		return frappe.db.get_value(BATCH_DOCTYPE, batch, "status")

	if stopped:
		# An operator pausing or stopping is not a failed attempt. Burning one here would
		# let a few pause/resume cycles exhaust `max_attempts` and mark healthy batches
		# Failed — punishing the operator for using the control the design gives them.
		cas_batch(batch, expected="Uploading", to="Pending")
		return "Pending"

	# Attempts bound *unproductive* re-dispatch, so progress resets the budget. Without
	# this, a batch that legitimately needs several passes — thumbnails waiting for their
	# source object, objects backing off after a transient S3 error — would be marked Failed
	# for making steady progress, which is the opposite of what a bounded retry is for.
	done = cint(frappe.db.count(OBJECT_DOCTYPE, {"batch": batch, "status": ("not in", ("Pending",))}))
	progressed = done > cint(frappe.db.get_value(BATCH_DOCTYPE, batch, "objects_done"))

	attempts = 0 if progressed else cint(frappe.db.get_value(BATCH_DOCTYPE, batch, "attempts")) + 1
	exhausted = attempts > cint(camp.max_attempts or 5)
	target = "Failed" if exhausted else "Pending"

	frappe.db.sql(
		f"UPDATE `tab{BATCH_DOCTYPE}` SET status=%(target)s, attempts=%(attempts)s, "
		"last_error=%(last_error)s WHERE name=%(name)s AND status='Uploading'",
		{
			"target": target,
			"attempts": attempts,
			"last_error": f"{residue} object(s) still Pending/Uploading after the batch job",
			"name": batch,
		},
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)
	return target


def batch_has_upload_residue(batch: str) -> int:
	"""A31 — VERIFY refuses a batch that still has non-terminal upload objects."""
	return frappe.db.count(OBJECT_DOCTYPE, {"batch": batch, "status": ("in", ("Pending", "Uploading"))})


# ------------------------------------------------------------------------------ campaign


def campaign_is_complete(campaign: str) -> bool:
	"""No object left that the engine could still move forward."""
	return not frappe.db.count(
		OBJECT_DOCTYPE,
		{
			"campaign": campaign,
			"status": ("in", ("Pending", "Uploading", "Uploaded", "Verifying", "DedupReused")),
		},
	)


def object_name_for(campaign: str, identity: str) -> str:
	"""A33 name for an identity string — used by the analyzer and by conflict relinks."""
	return names.object_name(campaign, names.identity_hash(identity))


def note_campaign_error(campaign: str, message: str):
	_set_campaign(campaign, {"last_error": (message or "")[:500]})
	audit.record("campaign_transition", campaign=campaign, error=(message or "")[:500])
