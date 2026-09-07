"""The 100,000-file migration rehearsal (gates F3 and F4).

Not a unit test and deliberately not importable from the app: it creates a synthetic corpus
of a hundred thousand files and drives a real campaign over it against real MinIO and real
RQ workers. It lives under `tests/` so nothing in the shipped package can reach it, and it
refuses to run on a site whose name is not a scratch site.

Run it in stages so a crash costs one stage rather than the whole run::

    python -m cloud_file_storage.tests.rehearsal seed    --site cfs-rehearsal.local --count 100000
    python -m cloud_file_storage.tests.rehearsal analyze --site cfs-rehearsal.local
    python -m cloud_file_storage.tests.rehearsal run     --site cfs-rehearsal.local
    python -m cloud_file_storage.tests.rehearsal measure --site cfs-rehearsal.local
    python -m cloud_file_storage.tests.rehearsal gates   --site cfs-rehearsal.local

Every stage is resumable: `seed` skips what it already made, `analyze` resumes from the
scan cursor and the classify step, and `run` re-derives its work from the database exactly
as a worker does after a crash.

The seeded anomalies are the ones F3 names — duplicate filenames, shared URLs, identical
bytes behind different URLs, missing files, orphans on disk, privacy mismatches, legacy
ALYF rows and a checksum failure — plus the ignored-doctype population A17 scopes out.
"""

import argparse
import json
import math
import os
import random
import shutil
import sys
import time

import frappe
from frappe.query_builder.functions import Count
from frappe.utils import cint

#: Only these site names may be touched. A rehearsal creates 100k File rows and uploads
#: them; running it anywhere else would be a production action.
SCRATCH_SITES = (
	"cfs-rehearsal.local",
	"cfs-autonomous.local",
	"cfs-throttle.local",
	"cfs-fault.local",
	"cfs-bench.local",
	# Added deliberately for the F3 throttle measurement: that criterion needs a site with no
	# residual Cloud Storage Objects, since dedupe-converged content transfers zero bytes and a
	# zero-byte rate cannot demonstrate a limit. The guard's purpose — never a business site — is
	# unchanged; this is a disposable site created for the measurement.
	"cfs-thr4.local",
)

#: Mutable so a single scratch site can host more than one campaign: a fault round needs work
#: in flight, and a campaign that has already converged cannot provide it. `--title` overrides.
CAMPAIGN_TITLE = "P5 100k rehearsal"
#: The shipped default, kept separately so `_spawn` can tell an override from the default and
#: forward it to the child processes only when it is one.
DEFAULT_CAMPAIGN_TITLE = CAMPAIGN_TITLE
SEED_PREFIX = "cfs-rehearsal"
FILE_DOCTYPE = "File"

#: Payload size band. The design's corpus averages ~83KB, but the property under test is
#: request-rate behaviour and row cost, not bandwidth, so the payloads are small and the
#: run stays inside a bench.
MIN_BYTES = 120
MAX_BYTES = 400

#: F3's throttle criterion needs a corpus whose upload time is dominated by *bytes*, not by
#: per-object overhead. At 120-400 B the transfer is instant and a bandwidth limit is
#: unobservable, which is why the P5 attempt could not complete this measurement: the harness
#: had no way to seed a bandwidth-bound corpus, so a purpose-seeded one was swallowed by the
#: site's existing 100k. `seed` now takes the band from these, and `--min-bytes/--max-bytes`
#: override them.
PAYLOAD_BAND = {"min": MIN_BYTES, "max": MAX_BYTES}

SEED_CHUNK = 2000


def _guard(site: str):
	if site not in SCRATCH_SITES:
		raise SystemExit(f"refusing to run the rehearsal on {site!r}; scratch sites only")


def _connect(site: str):
	_guard(site)
	frappe.init(site=site)
	frappe.connect()
	frappe.set_user("Administrator")


def _public_dir() -> str:
	path = frappe.get_site_path("public", "files")
	os.makedirs(path, exist_ok=True)
	return path


def _private_dir() -> str:
	path = frappe.get_site_path("private", "files")
	os.makedirs(path, exist_ok=True)
	return path


# --------------------------------------------------------------------------------- seed


def _payload(index: int, rng: random.Random) -> bytes:
	size = rng.randint(PAYLOAD_BAND["min"], PAYLOAD_BAND["max"])
	return (f"cfs-rehearsal-{index}-".encode() + os.urandom(size))[:size]


def seed(count: int) -> dict:
	"""Create the corpus. Resumable: already-seeded indices are skipped.

	Rows are written with `bulk_insert` rather than through `File.insert`, which would run
	the whole document stack a hundred thousand times and take hours. That is honest for
	this purpose: the rehearsal is about the *migration* reading an existing corpus, not
	about how the corpus was created.
	"""
	from frappe.utils import now_datetime

	from cloud_file_storage.storage.hashing import digest_bytes

	rng = random.Random(20260815)
	existing = set(
		frappe.db.get_all(
			FILE_DOCTYPE,
			filters={"file_name": ("like", f"{SEED_PREFIX}-%")},
			pluck="file_name",
			limit_page_length=0,
		)
	)
	stamp = now_datetime()
	created = 0
	anomalies = {
		"shared_url": 0,
		"identical_bytes": 0,
		"missing_physical": 0,
		"orphan_on_disk": 0,
		"privacy_mismatch": 0,
		"legacy_fork": 0,
		"ignored_doctype": 0,
		"duplicate_filename": 0,
	}

	rows: list[dict] = []
	legacy_uploads: list[tuple[str, bytes]] = []
	private_dir, public_dir = _private_dir(), _public_dir()
	shared_bytes = b"identical payload shared by many rehearsal files"

	for index in range(count):
		file_name = f"{SEED_PREFIX}-{index:06d}.bin"
		if file_name in existing:
			continue

		is_private = index % 4 != 0  # a quarter public
		directory = private_dir if is_private else public_dir
		url_root = "/private/files/" if is_private else "/files/"

		# --- anomaly selection, deterministic in `index` so a resume matches -------------
		content = _payload(index, rng)
		attached_to_doctype = None
		write_file = True
		file_url = f"{url_root}{file_name}"
		s3_object_key = None
		display_name = file_name

		if index % 997 == 0 and index:  # identical bytes behind their own URL
			content = shared_bytes
			anomalies["identical_bytes"] += 1
		if index % 1499 == 0 and index:  # a File row whose bytes are not on disk
			write_file = False
			anomalies["missing_physical"] += 1
		if index % 1999 == 0 and index:  # the is_private column disagrees with the URL
			is_private = not is_private
			anomalies["privacy_mismatch"] += 1
		if index % 2503 == 0 and index:  # a 0.2.x fork row: remote already, no local copy
			write_file = False
			s3_object_key = f"attachments/2021/07/14/legacy/{index}_{file_name}"
			file_url = f"/api/method/frappe_s3_attachment.controller.generate_file?key={s3_object_key}"
			# Two thirds have their bytes in the bucket (adoptable); the rest point at
			# nothing, which is the other real shape a fork site has after somebody tidied a
			# bucket by hand. Both paths are worth exercising: one adopts, one is a Blocker.
			if index % 3 != 0:
				legacy_uploads.append((s3_object_key, content))
			anomalies["legacy_fork"] += 1
		if index % 3001 == 0 and index:  # LOCAL_OPERATIONAL parent (A17)
			attached_to_doctype = "Data Import"
			anomalies["ignored_doctype"] += 1
		if index % 401 == 0 and index:  # the same human filename on a different object
			display_name = f"{SEED_PREFIX}-duplicate-name.bin"
			anomalies["duplicate_filename"] += 1

		if write_file:
			with open(os.path.join(directory, file_name), "wb") as handle:
				handle.write(content)

		digest = digest_bytes(content)
		rows.append(
			{
				"name": frappe.generate_hash(length=10),
				"file_name": display_name,
				"file_url": file_url,
				"is_private": 1 if is_private else 0,
				"is_folder": 0,
				"file_size": len(content),
				"content_hash": None if s3_object_key else digest.md5,
				"s3_object_key": s3_object_key,
				"attached_to_doctype": attached_to_doctype,
				"attached_to_name": f"DI-REH-{index}" if attached_to_doctype else None,
				"folder": "Home",
				"creation": stamp,
				"modified": stamp,
				"modified_by": "Administrator",
				"owner": "Administrator",
				"docstatus": 0,
				"idx": 0,
			}
		)

		# a second File row on the SAME url — the supported shared-URL pattern
		if index % 701 == 0 and index and write_file:
			rows.append({**rows[-1], "name": frappe.generate_hash(length=10), "content_hash": digest.md5})
			anomalies["shared_url"] += 1

		created += 1
		if len(rows) >= SEED_CHUNK:
			_flush(rows)
			rows = []
			print(f"  seeded {created}/{count}", flush=True)

	_flush(rows)

	# Bytes on disk that no File row claims.
	for index in range(20):
		path = os.path.join(private_dir, f"{SEED_PREFIX}-orphan-{index}.bin")
		if not os.path.exists(path):
			with open(path, "wb") as handle:
				handle.write(os.urandom(64))
			anomalies["orphan_on_disk"] += 1

	adopted = _seed_legacy_objects(legacy_uploads)

	frappe.db.commit()
	total = frappe.db.count(FILE_DOCTYPE, {"file_name": ("like", f"{SEED_PREFIX}-%")})
	anomalies["legacy_bytes_present"] = adopted
	return {"created": created, "rows_total": total, "anomalies": anomalies}


def _seed_legacy_objects(uploads: list[tuple[str, bytes]]) -> int:
	"""Put the fork-era bytes in the bucket, at the fork's key.

	Written straight through the S3 client rather than through the storage engine: these
	objects are supposed to predate this app, and creating them through `ensure_cso` would
	give them the content-addressed key adoption exists to avoid.
	"""
	if not uploads:
		return 0

	from cloud_file_storage.storage import client
	from cloud_file_storage.storage.modes import get_settings

	settings = get_settings()
	s3 = client.get_client(profile=client.PROFILE_JOB, settings=settings)
	for key, content in uploads:
		s3.put_object(Bucket=settings.bucket, Key=key, Body=content)
	return len(uploads)


def _flush(rows: list[dict]):
	if not rows:
		return
	fields = list(rows[0])
	frappe.db.bulk_insert(
		FILE_DOCTYPE, fields, [tuple(row[field] for field in fields) for row in rows], ignore_duplicates=True
	)
	frappe.db.commit()


# ------------------------------------------------------------------------------ campaign


def _campaign_name() -> str | None:
	return frappe.db.get_value("Cloud Migration Campaign", {"title": CAMPAIGN_TITLE}, "name")


def _ensure_campaign() -> str:
	from cloud_file_storage.migration import api

	existing = _campaign_name()
	if existing:
		return existing
	return api.create_campaign(CAMPAIGN_TITLE, batch_size=1000, parallelism=2)


def analyze() -> dict:
	from cloud_file_storage.migration import analyzer

	campaign = _ensure_campaign()
	started = time.monotonic()
	result = analyzer.run_analysis(campaign)
	result["campaign"] = campaign
	result["seconds"] = round(time.monotonic() - started, 2)
	return result


def plan() -> dict:
	from cloud_file_storage.migration import planner

	campaign = _campaign_name()
	started = time.monotonic()
	result = planner.plan_campaign(campaign)
	result["seconds"] = round(time.monotonic() - started, 2)
	return result


def run(*, inline: bool = False, max_rounds: int = 100000) -> dict:
	"""Drive the campaign to convergence.

	`inline` runs the batch jobs in this process (used by the fault-injection stages, which
	need to be able to kill one deterministically). Without it the jobs are enqueued on the
	real `cloud_migration` queue and picked up by the workers.
	"""
	from cloud_file_storage.migration import api, engine

	# Set by the fault matrix, and only there: the crash faults after `kill_worker` cannot
	# each afford a ten-minute wait on the shipped window. `kill_worker` itself runs with
	# the shipped value, so that constant is what the gate proves.
	override = os.environ.get(STALE_SECONDS_ENV)
	if override:
		engine.STALE_AFTER_SECONDS = int(override)

	campaign = _campaign_name()
	status = frappe.db.get_value("Cloud Migration Campaign", campaign, "status")
	if status in ("Planned", "Paused", "Stopped"):
		api.start_migration(campaign, skip_preflight=True)

	started = time.monotonic()
	rounds = 0
	jobs = 0
	while rounds < max_rounds:
		rounds += 1
		# End the transaction first. InnoDB's REPEATABLE READ pins this connection's read
		# view at its first statement, so without this the poller watches a snapshot taken
		# before the workers started: it sees the same batches in flight forever, concludes
		# every parallelism slot is occupied, and stops dispatching. (`dispatch_tick` does
		# not need this in production — each tick is a fresh job.)
		frappe.db.rollback()
		# The tick, not `dispatch_batches` alone: the tick is the crash-recovery watchdog,
		# and a rehearsal that never ran it could not observe a killed worker's batch coming
		# back. In production the scheduler calls it every 60 seconds.
		dispatched = engine.dispatch_tick().get("dispatched", 0)
		jobs += dispatched
		if not dispatched:
			if inline:
				break
			if not _in_flight(campaign):
				break
			time.sleep(2)

	return {
		"campaign": campaign,
		"rounds": rounds,
		"dispatched": jobs,
		"seconds": round(time.monotonic() - started, 2),
	}


def _in_flight(campaign: str) -> int:
	from cloud_file_storage.cloud_file_storage.doctype.cloud_migration_batch.cloud_migration_batch import (
		IN_FLIGHT_STATUSES,
	)

	return frappe.db.count(
		"Cloud Migration Batch", {"campaign": campaign, "status": ("in", IN_FLIGHT_STATUSES)}
	)


# ------------------------------------------------------------------------------ measure


#: A1's frozen tolerance (owner ruling, 2026-08-18). "ERP queues unaffected" had no numeric
#: definition in the approved documents, which is why four audit rounds recorded F3 as blocked
#: rather than negotiating a threshold. Both conditions must hold, per queue.
A1_MAX_DELTA_SECONDS = 0.250
A1_MAX_RATIO = 2.0
A1_MIN_SAMPLES = 30
ERP_QUEUES = ("short", "default", "long")


def _probe_queue(queue: str, samples: int) -> dict:
	"""Enqueue `samples` real no-op jobs on `queue` and time enqueue -> finished for each."""
	latencies = []
	failures = 0
	for index in range(samples):
		job_id = f"cfs-probe-{queue}-{time.time_ns()}-{index}"
		started = time.monotonic()
		job = frappe.enqueue("frappe.handler.ping", queue=queue, job_id=job_id, timeout=60, deduplicate=True)
		deadline = started + 60
		status = None
		while time.monotonic() < deadline:
			status = job.get_status(refresh=True)
			if status in ("finished", "failed"):
				break
			# 5 ms, not 50: A1's worst measured margin is 44.8 ms, and an instrument whose
			# quantisation is larger than the headroom it resolves cannot support the verdict.
			time.sleep(0.005)
		if status != "finished":
			failures += 1
			continue
		latencies.append(time.monotonic() - started)
	latencies.sort()
	return {"latencies": latencies, "failures": failures}


def _p95(values: list) -> float | None:
	"""The 95th percentile, nearest-rank. `None` for an empty sample."""
	if not values:
		return None
	ordered = sorted(values)
	# Nearest-rank: the smallest value at or above 95% of the distribution.
	rank = max(1, math.ceil(0.95 * len(ordered)))
	return ordered[rank - 1]


#: Where the rehearsal workers append one row per job they execute. The probe reads it to
#: witness load, so a "loaded" measurement can prove it was actually loaded.
JOB_LOG_NAME = "rehearsal-jobs.jsonl"


def _fresh_count(doctype: str, filters: dict) -> int:
	"""Count on a **new** snapshot.

	MariaDB here runs REPEATABLE-READ, so two counts inside one long-lived transaction return
	the same number even when another connection has committed between them. A progress reading
	taken that way cannot observe concurrent load at all: it reports a stalled migration that is
	in fact transferring. Measured directly — same-txn counts read 0 and 0 across a committed
	insert, and 1 only after a rollback.
	"""
	frappe.db.rollback()
	return frappe.db.count(doctype, filters)


def _default_job_log() -> str:
	return frappe.get_site_path("private", JOB_LOG_NAME)


#: The prefix the engine gives a dispatched batch job, before frappe namespaces it:
#: `cfs::<campaign>::<target>::<batch>` (engine.py:409, api.py:207).
ENGINE_JOB_PREFIX = "cfs"


def migration_job_marker() -> str:
	"""How *this* frappe renders the engine's job-id prefix.

	Derived, never hardcoded. v15 namespaces as ``<site>::<job_id>``; v16 rewrites ``:``
	to ``|`` and joins with ``||`` (``frappe.utils.background_jobs.create_job_id``), so the
	literal ``"cfs::"`` this module used to match stopped appearing in any job id on v16 —
	and every workload-isolation gate below then measured an **empty set and passed**. That
	is the check-that-cannot-fail shape, so the marker is asked of frappe instead of assumed,
	and a marker that lost the prefix raises rather than quietly matching nothing.
	"""
	from frappe.utils.background_jobs import create_job_id

	sentinel = "ZZREHEARSALMARKERZZ"
	rendered = create_job_id(f"{ENGINE_JOB_PREFIX}::{sentinel}")
	# Everything frappe put in front of the sentinel: the site namespace *and* our prefix,
	# in this version's separator. Keeping the site in the marker is deliberate -- a job
	# belonging to another site on the same bench is not this rehearsal's work.
	# (Do not try to strip it with `create_job_id("")`: a falsy job_id makes frappe mint a
	# random uuid, so the "prefix" would never match and the strip would silently no-op.)
	marker = rendered[: rendered.index(sentinel)]
	if ENGINE_JOB_PREFIX not in marker:
		raise AssertionError(
			f"derived migration job marker {marker!r} lost the {ENGINE_JOB_PREFIX!r} prefix "
			f"(create_job_id rendered {rendered!r}); the workload-isolation gates would "
			"match nothing and pass vacuously"
		)
	return marker


def _is_migration_job(row: dict) -> bool:
	return migration_job_marker() in str(row.get("job_id") or "")


def _job_rows(job_logs, start: float, end: float) -> list:
	"""Job-log rows executed within [start, end], across every log given.

	Takes a **list** because the two things A1 reads from job logs are written by different
	workers: migration progress is recorded by the `cloud_migration` workers, while a migration
	job that wrongly landed on `short`/`default`/`long` would be executed — and therefore
	logged — by an ERP worker. Reading only one log makes one of the two undetectable.

	Missing or unreadable logs contribute nothing rather than raising; a log that does not
	exist is not evidence of absence and the caller's `load_state` says so.
	"""
	if job_logs is None:
		paths = [_default_job_log()]
	elif isinstance(job_logs, str):
		paths = [job_logs]
	else:
		paths = list(job_logs)

	rows = []
	for path in paths:
		try:
			with open(path) as handle:
				for line in handle:
					line = line.strip()
					if not line:
						continue
					try:
						row = json.loads(line)
					except ValueError:
						continue
					at = row.get("at")
					if at is not None and start <= at <= end:
						rows.append(row)
		except OSError:
			continue
	return paths, rows


def _assemble_witness(
	campaign: str | None,
	job_logs,
	started_at: float,
	finished_at: float,
	*,
	migration_queue: str,
	progress_samples=None,
) -> dict:
	"""Build the load witness from its two real inputs: the job logs, and the window.

	Extracted so it can be **driven** by a test. Every A1 test used to assert on a witness its
	own fixture had written, which let `load_state = "LOAD_PRESENT"` (unconditional) survive the
	entire suite — the one mutation that makes A1 vacuous.
	"""
	paths, rows = _job_rows(job_logs, started_at, finished_at)
	missing = [path for path in paths if not os.path.exists(path)]
	midpoint = started_at + (finished_at - started_at) / 2.0
	mig_rows = [row for row in rows if _queue_of(row) == migration_queue and _is_migration_job(row)]
	return {
		"campaign": campaign,
		"started_at": started_at,
		"finished_at": finished_at,
		"window_seconds": round(finished_at - started_at, 3),
		"migration_jobs_during_probe": len(mig_rows),
		"migration_jobs_on_erp_queues": _migration_jobs_on_erp(rows),
		# **Which halves of the window the load actually covered.** "A migration job ran at some
		# instant" is not the claim A1 needs: three objects finishing in the first two seconds of a
		# 36 s probe would certify 34 seconds of idle machine as "loaded", and an idle machine is
		# exactly what makes the delta small enough to pass.
		"migration_jobs_first_half": sum(1 for row in mig_rows if row["at"] <= midpoint),
		"migration_jobs_second_half": sum(1 for row in mig_rows if row["at"] > midpoint),
		# **The instrument is recorded, not just its reading.** A job log that was never read
		# yields zero rows, and zero rows is indistinguishable from "no violations" unless the
		# absence is written down.
		"job_logs": paths,
		"job_logs_missing": missing,
		"job_log_rows_read": len(rows),
		"progress_samples": progress_samples,
		"progressed_first_half": _half_progress(progress_samples, first=True),
		"progressed_second_half": _half_progress(progress_samples, first=False),
	}


#: A window is only "loaded" if migration work covered **both** halves of it. Binary, not a
#: tunable fraction: the owner froze A1's thresholds and this is not one of them, it is the
#: difference between "load happened during the probe" and "load happened at some instant".
def _half_progress(samples, *, first: bool) -> int | None:
	"""Objects that advanced in the first or the second half of the probe window."""
	if not samples or any(value is None for value in samples):
		return None
	mid = len(samples) // 2
	return (samples[mid] - samples[0]) if first else (samples[-1] - samples[mid])


def _load_state(witness: dict, *, progressed: int | None) -> str:
	# Continuity may be shown either by object progress in both halves (the signal that works at
	# release scale) or by migration job starts in both halves (the one that works for small
	# batches, where a job start happens per handful of objects).
	by_progress = (witness.get("progressed_first_half") or 0) > 0 and (
		witness.get("progressed_second_half") or 0
	) > 0
	by_jobs = witness["migration_jobs_first_half"] > 0 and witness["migration_jobs_second_half"] > 0
	both_halves = by_progress or by_jobs
	if both_halves:
		return "LOAD_PRESENT"
	if witness["migration_jobs_during_probe"] > 0 or (progressed or 0) > 0:
		# Work was seen, but not across the whole window — reported as its own state rather than
		# rounded up to LOAD_PRESENT or down to NO_LOAD.
		return "PARTIAL_LOAD"
	return "NO_LOAD"


def _queue_of(row: dict) -> str:
	"""`home-user-v15:cloud_migration` -> `cloud_migration`."""
	return str(row.get("queue") or "").rsplit(":", 1)[-1]


def _migration_jobs_on_erp(rows: list) -> int:
	"""**Migration** jobs observed executing on an ERP queue — an A1 hard fail, from evidence.

	The migration-job test is not optional: A1's own probe enqueues ~105 pings onto exactly
	these three queues, so counting every job on an ERP queue would report the measurement
	instrument as the violation it is looking for.
	"""
	return sum(1 for row in rows if _queue_of(row) in ERP_QUEUES and _is_migration_job(row))


def probe(samples: int = 10, *, campaign: str | None = None, job_logs=None) -> dict:
	"""F3/A1 — are the ERP's own queues still responsive while the migration runs?

	Enqueues real no-op jobs on **each** of `short`, `default` and `long` and times enqueue ->
	finished. Run once with the campaign idle and again while it is transferring; the two p95s
	are the claim.

	**Per-queue, and p95 rather than median**, because A1's frozen definition is per-queue and
	a median hides exactly the tail that "unaffected" is about. An earlier version probed only
	`default` and reported min/median/max, which could not answer the criterion as written.
	"""
	from frappe.utils.background_jobs import get_queue

	from cloud_file_storage.migration.engine import MIGRATION_QUEUE

	started_at = time.time()

	def _progress():
		if not campaign:
			return None
		return _fresh_count("Cloud Migration Object", {"campaign": campaign, "status": ("!=", "Pending")})

	# **Progress is sampled between queues, not only at the ends.** At release scale a batch is
	# 1000 objects, so migration *job starts* are rare clustered events even while work runs
	# continuously - judging continuity by job timestamps reported PARTIAL_LOAD for a window in
	# which 783 objects demonstrably moved. "Loaded throughout" means work advanced in every part
	# of the window, and that is what these samples measure.
	progress_samples = [_progress()]
	done_start = progress_samples[0]

	results = {}
	for queue in ERP_QUEUES:
		measured = _probe_queue(queue, samples)
		progress_samples.append(_progress())
		results[queue] = {
			"samples": samples,
			"completed": len(measured["latencies"]),
			"failures": measured["failures"],
			"p95_seconds": round(_p95(measured["latencies"]), 4) if measured["latencies"] else None,
			"median_seconds": (
				round(measured["latencies"][len(measured["latencies"]) // 2], 4)
				if measured["latencies"]
				else None
			),
			"max_seconds": round(measured["latencies"][-1], 4) if measured["latencies"] else None,
		}

	depths = {}
	for queue in ERP_QUEUES + ("cloud_migration",):
		try:
			depths[queue] = len(get_queue(queue))
		except Exception as exc:  # noqa: BLE001 - an unconfigured queue is a finding
			depths[queue] = f"unavailable: {exc}"

	finished_at = time.time()
	witness = _assemble_witness(
		campaign,
		job_logs,
		started_at,
		finished_at,
		migration_queue=MIGRATION_QUEUE,
		progress_samples=progress_samples,
	)
	if campaign:
		done_end = _fresh_count("Cloud Migration Object", {"campaign": campaign, "status": ("!=", "Pending")})
		witness["objects_done_start"] = done_start
		witness["objects_done_end"] = done_end
		witness["objects_progressed"] = done_end - done_start
		witness["load_state"] = _load_state(witness, progressed=done_end - done_start)
	else:
		witness["load_state"] = _load_state(witness, progressed=None)

	return {
		"queues": results,
		"queue_depths": depths,
		"min_samples_required": A1_MIN_SAMPLES,
		"load_witness": witness,
	}


def a1_verdict(baseline: dict, loaded: dict, *, migration_jobs_on_erp: int | None = None) -> dict:
	"""Apply A1's frozen tolerance to a baseline/loaded probe pair. One queue fails => A1 FAIL.

	The thresholds are module constants set by the owner ruling and are deliberately NOT
	derived from the measurement — a tolerance chosen after seeing the number is not a gate.

	**A verdict is only rendered on a measurement that proved itself.** A1 compares an idle
	baseline against a loaded run, so "loaded" has to be a fact and not an intention: if the
	loaded probe cannot witness migration progress, the comparison is between two idle runs and
	would pass trivially, which is the most dangerous way for this gate to be wrong. Such a pair
	returns `INVALID_MEASUREMENT` and never `passed: True` — the same three-state vocabulary the
	disk-I/O stage uses, for the same reason.
	"""
	measurement_reasons = []
	base_witness = baseline.get("load_witness") or {}
	load_witness = loaded.get("load_witness") or {}
	if load_witness.get("load_state") != "LOAD_PRESENT":
		measurement_reasons.append(
			f"loaded run did not witness migration load across the whole probe window "
			f"(load_state={load_witness.get('load_state', 'ABSENT')!r})"
		)
	if base_witness.get("load_state") in ("LOAD_PRESENT", "PARTIAL_LOAD"):
		measurement_reasons.append(
			f"baseline run was taken while the migration was transferring "
			f"(load_state={base_witness.get('load_state')!r})"
		)

	# **An override may only raise the count, never lower it.** An explicit `0` used to win over
	# both witnesses, so a driver that passed "we didn't instrument this" would switch off the
	# ERP-contamination condition even where the witnesses had recorded violations.
	derived_on_erp = int(base_witness.get("migration_jobs_on_erp_queues") or 0) + int(
		load_witness.get("migration_jobs_on_erp_queues") or 0
	)
	migration_jobs_on_erp = max(int(migration_jobs_on_erp or 0), derived_on_erp)

	# **A log that was never read is not a clean log.** Zero rows and zero violations are the same
	# number; only the instrument's own record separates them.
	for label, witness in (("baseline", base_witness), ("loaded", load_witness)):
		if witness and witness.get("job_log_rows_read") == 0:
			measurement_reasons.append(
				f"{label} probe read 0 job-log rows, so 'no migration jobs on ERP queues' is "
				f"unwitnessed (missing: {witness.get('job_logs_missing') or 'none recorded'})"
			)

	per_queue = {}
	failures_total = 0
	for queue in ERP_QUEUES:
		b = (baseline.get("queues") or {}).get(queue) or {}
		l = (loaded.get("queues") or {}).get(queue) or {}
		bp, lp = b.get("p95_seconds"), l.get("p95_seconds")
		fails = int(b.get("failures") or 0) + int(l.get("failures") or 0)
		failures_total += fails
		enough = (b.get("completed") or 0) >= A1_MIN_SAMPLES and (l.get("completed") or 0) >= A1_MIN_SAMPLES
		reasons = []
		if bp is None or lp is None:
			reasons.append("a p95 could not be computed")
		if not enough:
			reasons.append(f"fewer than {A1_MIN_SAMPLES} completed probes")
		if fails:
			reasons.append(f"{fails} probe failure(s)/timeout(s)")
		delta = ratio = None
		if bp is not None and lp is not None:
			# Compare the **reported** figures, not the raw floats. `0.30 + 0.250` is
			# 0.25000000000000006 in binary floating point, so a measurement sitting exactly on
			# the frozen bound failed while the report printed "delta 0.2500s" beside it — a
			# verdict nobody could reconcile with its own evidence. Rounding first makes the
			# decision and the number it is justified by the same quantity. The threshold itself
			# is untouched; only the comparison is made well-defined.
			delta = round(lp - bp, 4)
			ratio = round(lp / bp, 3) if bp else None
			if delta > A1_MAX_DELTA_SECONDS:
				reasons.append(f"delta {delta:.4f}s > {A1_MAX_DELTA_SECONDS}s")
			if ratio is not None and ratio > A1_MAX_RATIO:
				reasons.append(f"ratio {ratio:.3f}x > {A1_MAX_RATIO}x")
		per_queue[queue] = {
			"baseline_p95": bp,
			"loaded_p95": lp,
			"delta": delta,
			"ratio": ratio,
			"failures": fails,
			"pass": not reasons,
			"reasons": reasons,
		}

	if migration_jobs_on_erp:
		for q in per_queue.values():
			q["pass"] = False
			q["reasons"].append(f"{migration_jobs_on_erp} migration job(s) executed on an ERP queue")

	state = "INVALID_MEASUREMENT" if measurement_reasons else "VALID_MEASUREMENT"
	return {
		"per_queue": per_queue,
		"probe_failures": failures_total,
		"migration_jobs_on_erp_queues": migration_jobs_on_erp,
		"thresholds": {"max_delta_seconds": A1_MAX_DELTA_SECONDS, "max_ratio": A1_MAX_RATIO},
		"measurement_state": state,
		"measurement_reasons": measurement_reasons,
		"load_witness": {"baseline": base_witness, "loaded": load_witness},
		# The ruling also requires no sustained queue-depth accumulation and depths recovering.
		# Carried into the verdict so that claim is read from the artifact, not from an eyeball.
		"queue_depths": {
			"baseline": baseline.get("queue_depths"),
			"loaded": loaded.get("queue_depths"),
		},
		"passed": state == "VALID_MEASUREMENT" and all(q["pass"] for q in per_queue.values()),
	}


def a1(baseline_path: str, loaded_path: str, out_path: str | None = None) -> dict:
	"""F3/A1 — render the verdict from two `probe` artifacts, in-tree.

	`a1_verdict` used to be reachable only from an out-of-tree driver, which is exactly how this
	cycle produced two false readings (the sampler matched no process; a count was taken inside
	one transaction). A gate whose application is optional is not a gate, so the verdict is a
	stage like every other and its output is the evidence artifact.
	"""
	with open(baseline_path) as handle:
		baseline = json.load(handle)
	with open(loaded_path) as handle:
		loaded = json.load(handle)
	verdict = a1_verdict(baseline, loaded)
	if out_path:
		with open(out_path, "w") as handle:
			json.dump(verdict, handle, indent=2)
	return verdict


def triage() -> dict:
	"""Resolve the seeded conflicts the way the runbook tells an operator to.

	A rehearsal that stops at "155 objects are halted" has only shown that the engine
	*halts*. What a real cutover does next is triage, so the run does too, through the same
	whitelisted actions the Desk buttons and the CLI reach:

	* `ambiguous_privacy` → `resolve_privacy` (the operator declares the true visibility),
	  after which the object migrates normally;
	* `missing_physical` and `adoption_failed` → `skip`; the bytes are genuinely gone, and
	  the runbook's answer is restore-from-backup or skip. Skipped objects leave the
	  convergence denominator because they were never migratable.
	"""
	from cloud_file_storage.migration import conflicts

	campaign = _campaign_name()
	resolved = {"resolve_privacy": 0, "skip": 0}

	for row in conflicts.open_conflicts(campaign):
		if row["conflict_type"] == "ambiguous_privacy":
			# The seeded corpus flipped the column, so the URL prefix is the truth.
			object_row = frappe.db.get_value(
				"Cloud Migration Object", row["migration_object"], ["file_url"], as_dict=True
			)
			is_private = 1 if (object_row.file_url or "").startswith("/private/files/") else 0
			conflicts.resolve_privacy(row["name"], is_private=is_private)
			resolved["resolve_privacy"] += 1
		elif row["conflict_type"] in ("missing_physical", "adoption_failed"):
			conflicts.skip(row["name"], note="rehearsal: bytes are gone, nothing to migrate")
			resolved["skip"] += 1

	# `skip` marks the object; the ones whose bytes are gone but that never opened a
	# conflict of their own (an adoption that failed after its object was already Failed)
	# are swept the same way, so the denominator is honest.
	frappe.db.sql(
		"""
		UPDATE `tabCloud Migration Object`
		SET status = 'Skipped', skip_reason = 'operator_skipped'
		WHERE campaign = %(campaign)s AND status = 'Failed'
		""",
		{"campaign": campaign},
	)
	frappe.db.commit()
	return resolved


def measure() -> dict:
	"""Every F4 quantity, plus the queue and job counts F3 asks for."""
	from cloud_file_storage.migration import preflight, report

	campaign = _campaign_name()
	measured = preflight.measure(campaign)
	projected = preflight.project(measured)
	checks = preflight.evaluate(measured, projected)

	batches = frappe.db.count("Cloud Migration Batch", {"campaign": campaign})
	return {
		"campaign": campaign,
		"measured": measured,
		"projected": projected,
		"checks": checks,
		"convergence": report.convergence(campaign),
		"summary": report.summary(campaign),
		"batches": batches,
		"snapshot": report.campaign_snapshot(campaign),
	}


def gates() -> dict:
	"""The F3 pass/fail list, evaluated against what actually happened."""
	from cloud_file_storage.migration import report

	campaign = _campaign_name()
	convergence = report.convergence(campaign)
	batches = frappe.db.count("Cloud Migration Batch", {"campaign": campaign})
	objects = frappe.db.count("Cloud Migration Object", {"campaign": campaign})

	quarantined = frappe.db.count(
		"Cloud Migration Object", {"campaign": campaign, "status": ("in", ("Quarantined", "CleanedUp"))}
	)
	unverified_quarantine = frappe.db.sql(
		"""
		SELECT COUNT(*) FROM `tabCloud Migration Object` o
		LEFT JOIN `tabCloud Storage Object` c ON c.name = o.cloud_storage_object
		WHERE o.campaign = %(campaign)s
		  AND o.status IN ('Quarantined', 'CleanedUp')
		  AND (c.name IS NULL OR c.status NOT IN ('verified', 'legacy_unverified'))
		""",
		{"campaign": campaign},
	)[0][0]

	return {
		"campaign": campaign,
		"objects": objects,
		"batches": batches,
		"convergence_ratio": convergence["ratio"],
		"convergence_target": 0.999,
		"convergence_pass": convergence["ratio"] >= 0.999,
		"quarantined": quarantined,
		"delete_before_verify": int(unverified_quarantine),
		"delete_before_verify_pass": int(unverified_quarantine) == 0,
		"counts": convergence["counts"],
	}


def purge() -> dict:
	"""Remove the corpus. Used between runs, never during one."""
	names = frappe.db.get_all(
		FILE_DOCTYPE, filters={"file_name": ("like", f"{SEED_PREFIX}-%")}, pluck="name", limit_page_length=0
	)
	for directory in (_private_dir(), _public_dir()):
		for entry in os.scandir(directory):
			if entry.name.startswith(SEED_PREFIX):
				os.remove(entry.path)

	campaign = _campaign_name()
	if campaign:
		for doctype in (
			"Cloud Migration File Ref",
			"Cloud Migration Object",
			"Cloud Migration Batch",
			"Cloud Migration Conflict",
			"Cloud Storage Audit Log",
		):
			frappe.db.delete(doctype, {"campaign": campaign})
		frappe.db.delete("Cloud Migration Campaign", {"name": campaign})

	frappe.db.delete(FILE_DOCTYPE, {"file_name": ("like", f"{SEED_PREFIX}-%")})
	frappe.db.delete("Cloud Storage Object")
	frappe.db.commit()
	return {"files_removed": len(names)}


# ------------------------------------------------------------------- workers and job log


#: One JSON line per job an RQ worker executes, appended by the forked work-horse. A file
#: rather than a Redis counter because one of the injected faults *is* the loss of the
#: queue's Redis state, and a counter that the fault erases cannot answer the question the
#: fault exists to ask.
JOB_LOG_ENV = "CFS_REHEARSAL_JOB_LOG"

#: The dispatcher's staleness window, overridable in the driving process only. The kill -9
#: fault deliberately leaves it at the shipped 600s so the production constant is what gets
#: proved; the later faults shorten it so seven injections fit inside one campaign. The
#: recovery path itself is never touched — only how old a heartbeat has to be.
STALE_SECONDS_ENV = "CFS_REHEARSAL_STALE_SECONDS"


def job_log_path() -> str:
	return os.environ.get(JOB_LOG_ENV) or frappe.get_site_path("private", "rehearsal-jobs.jsonl")


def worker(queue: str = "cloud_migration", burst: bool = False):
	"""A real RQ worker on the site's dedicated queue, with per-job accounting.

	`bench worker` resolves the queue name against `common_site_config.json`, which on this
	bench is shared with thirteen business sites and is deliberately not being edited, so the
	site is initialised first and the queue comes from that site's own `workers` block. From
	`get_redis_conn` onward this is exactly what `background_jobs.start_worker` does.

	Every executed job is appended to the job log together with the queue it came off. Both
	of F3's queue gates are read from that file: the total job count has to stay near two per
	batch, and no migration job may ever appear on `short`, `default` or `long`.
	"""
	from frappe.utils.background_jobs import generate_qname, get_queues_timeout, get_redis_conn
	from rq import Worker

	if queue not in get_queues_timeout():
		raise SystemExit(f"{queue!r} is not configured for this site")

	log_path = job_log_path()

	class AccountingWorker(Worker):
		def perform_job(self, job, queue_, *args, **kwargs):
			record = {
				"at": time.time(),
				"pid": os.getpid(),
				"queue": getattr(queue_, "name", str(queue_)),
				"job_id": job.id,
				"func": job.func_name,
				"batch": (job.kwargs or {}).get("batch"),
			}
			# O_APPEND on a line shorter than PIPE_BUF, so concurrent work-horses interleave
			# whole lines rather than fragments.
			with open(log_path, "a") as handle:
				handle.write(json.dumps(record) + "\n")
			return super().perform_job(job, queue_, *args, **kwargs)

	connection = get_redis_conn()
	qname = generate_qname(queue)
	print(f"worker {os.getpid()} on {qname}; job log {log_path}", flush=True)
	AccountingWorker([qname], connection=connection).work(logging_level="WARNING", burst=burst)


def jobs() -> dict:
	"""F3 — the RQ job count stayed bounded, and stayed off the ERP's queues.

	The design enqueues one UPLOAD job and one VERIFY job per batch, plus one CLEANUP job per
	batch once an operator approves it, so "≈2× batch count" is the gate for a campaign that
	stopped at VERIFY and "≈3×" for one that was cleaned. Anything materially above that is
	the job explosion the gate is looking for: a batch re-dispatched in a loop, or a job that
	re-enqueues itself.
	"""
	path = job_log_path()
	if not os.path.exists(path):
		return {"job_log": path, "present": False}

	executed = []
	with open(path) as handle:
		for line in handle:
			line = line.strip()
			if line:
				executed.append(json.loads(line))

	by_queue: dict[str, int] = {}
	by_func: dict[str, int] = {}
	for record in executed:
		queue_name = record.get("queue") or "?"
		by_queue[queue_name] = by_queue.get(queue_name, 0) + 1
		func = record.get("func") or "?"
		by_func[func] = by_func.get(func, 0) + 1

	campaign = _campaign_name()
	batches = frappe.db.count("Cloud Migration Batch", {"campaign": campaign}) if campaign else 0
	distinct = len({record.get("job_id") for record in executed})
	repeats = len(executed) - distinct

	# A qname is `<site>:<queue>`; the ERP queues are the three frappe ships with.
	off_queue = [
		record
		for record in executed
		if (record.get("queue") or "").rsplit(":", 1)[-1] in ("short", "default", "long")
	]

	return {
		"job_log": path,
		"present": True,
		"batches": batches,
		"jobs_executed": len(executed),
		"jobs_distinct": distinct,
		"jobs_redelivered": repeats,
		"jobs_per_batch": round(len(executed) / batches, 3) if batches else None,
		"by_queue": by_queue,
		"by_func": by_func,
		"jobs_on_erp_queues": len(off_queue),
		"dedicated_queue_only_pass": not off_queue,
	}


# ------------------------------------------------------------------- resource sampling


#: Tables whose on-disk size is followed through the run, so F4's "temporary growth" and
#: "peak DB disk" are measured curves rather than two endpoints.
SAMPLED_TABLES = (
	"tabCloud Migration Object",
	"tabCloud Migration File Ref",
	"tabCloud Migration Conflict",
	"tabCloud Storage Audit Log",
	"tabCloud Storage Object",
	"tabFile",
)

_CLOCK_TICKS = os.sysconf("SC_CLK_TCK")
_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")


def _diskstats(device: str | None = None) -> dict | None:
	"""Device-level counters from `/proc/diskstats` — the only source of real IOPS and latency.

	`/proc/<pid>/io` gives *syscall* counts and *block-layer bytes* for one process. Neither is
	device IOPS: syscalls are not requests, and bytes are not operations. Service latency is not
	in /proc/<pid>/io at all. F3 asks for "bounded disk I/O vs recorded baselines", so the device
	counters are sampled directly and the derived rates carry their own units.

	Fields (kernel Documentation/iostats.rst): 3=name 4=reads 5=reads_merged 6=read_sectors
	7=read_ms 8=writes 9=writes_merged 10=write_sectors 11=write_ms 12=in_flight 13=io_ms.
	Sectors are 512 B by convention here, independent of the filesystem block size.
	"""
	if device is None:
		device = os.environ.get("CFS_REHEARSAL_DEVICE") or _backing_device()
	if not device:
		return None
	try:
		with open("/proc/diskstats") as handle:
			for line in handle:
				fields = line.split()
				if len(fields) >= 14 and fields[2] == device:
					return {
						"device": device,
						"reads": int(fields[3]),
						"reads_merged": int(fields[4]),
						"read_sectors": int(fields[5]),
						"read_ms": int(fields[6]),
						"writes": int(fields[7]),
						"writes_merged": int(fields[8]),
						"write_sectors": int(fields[9]),
						"write_ms": int(fields[10]),
						"in_flight": int(fields[11]),
						"io_ms": int(fields[12]),
					}
	except OSError:
		return None
	return None


def _backing_device() -> str | None:
	"""The device backing the bench, resolved once from the mount table."""
	try:
		import subprocess

		out = subprocess.run(
			["df", "--output=source", os.path.dirname(os.path.abspath(frappe.get_site_path()))],
			capture_output=True,
			text=True,
			timeout=10,
		).stdout.splitlines()
		if len(out) >= 2:
			return os.path.basename(out[1].strip())
	except Exception:
		return None
	return None


def _proc_metrics(pid: int) -> dict | None:
	"""CPU ticks, RSS and block I/O for one pid, straight from /proc."""
	try:
		with open(f"/proc/{pid}/stat") as handle:
			fields = handle.read().rsplit(") ", 1)[1].split()
		metrics = {"cpu_ticks": int(fields[11]) + int(fields[12])}
		with open(f"/proc/{pid}/statm") as handle:
			metrics["rss_bytes"] = int(handle.read().split()[1]) * _PAGE_SIZE
		try:
			with open(f"/proc/{pid}/io") as handle:
				for line in handle:
					key, _, value = line.partition(":")
					# `read_bytes`/`write_bytes` are block-layer counters: a read served from
					# page cache never reaches the block layer, so `read_bytes` legitimately
					# stays 0 on a warm host. Reading only those is why P5 recorded disk I/O as
					# "unmeasurable on this bench" -- the counters were fine, the wrong pair was
					# being read. `rchar`/`wchar` are the syscall-level byte counts and move for
					# every read and write, cached or not, so both pairs are captured: together
					# they distinguish "the app moved bytes" from "the bytes reached the disk".
					if key in ("read_bytes", "write_bytes", "rchar", "wchar", "syscr", "syscw"):
						metrics[key] = int(value)
		except OSError:
			pass
		return metrics
	except (OSError, IndexError, ValueError):
		return None


def _is_rehearsal_cmdline(cmdline: str) -> bool:
	"""True for a process actually running this module — not a shell that merely names it.

	The *interpreter* must be python. A shell wrapper carries the whole command in its own
	cmdline, so `bash -c '... -m cloud_file_storage.tests.rehearsal ...'` satisfies every string
	test here while being no such process. That is the same false positive as the `pkill -f`
	pattern which twice killed the shell issuing it during this delivery, and it made the throttle
	preflight refuse a site with nothing running on it.

	Both invocation forms count: `-m cloud_file_storage.tests.rehearsal` (dotted) and a worker
	started by path as `.../cloud_file_storage/tests/rehearsal.py`. Matching only the dotted form
	silently drops path-started workers from every sample — the whole application half of a run
	then reads as zero RSS, which looks like a quiet process rather than an unmatched one.

	**Input contract:** `cmdline` must be the raw NUL-delimited `/proc/<pid>/cmdline`. A caller
	that space-joins it first would compute `argv0` from the whole string and this would always
	return False — silently, at all three call sites.
	"""
	argv0 = cmdline.split("\x00", 1)[0]
	if "python" not in os.path.basename(argv0):
		return False
	return (
		"cloud_file_storage.tests.rehearsal" in cmdline or "cloud_file_storage/tests/rehearsal.py" in cmdline
	)


def _rehearsal_pids() -> list[int]:
	"""Every process running this module — the poller and the workers, forks included."""
	found = []
	for entry in os.scandir("/proc"):
		if not entry.name.isdigit():
			continue
		try:
			with open(f"/proc/{entry.name}/cmdline", "rb") as handle:
				cmdline = handle.read().decode("utf-8", "replace")
		except OSError:
			continue
		# Both invocation forms. `Harness._spawn` uses `-m cloud_file_storage.tests.rehearsal`
		# (dotted), but a worker started by path shows `.../cloud_file_storage/tests/rehearsal.py`
		# and matching only the dotted form silently drops it from every sample -- the whole
		# application half of a run reads as zero RSS and zero I/O, which looks like a quiet
		# process rather than an unmatched one.
		if _is_rehearsal_cmdline(cmdline):
			found.append(int(entry.name))
	return sorted(found)


def _mysqld_pid() -> int | None:
	try:
		pid_file = frappe.db.sql("SELECT @@pid_file")[0][0]
		with open(pid_file) as handle:
			return int(handle.read().strip())
	except Exception:  # noqa: BLE001 - the sampler must not be able to stop the rehearsal
		return None


def _table_bytes() -> dict:
	rows = frappe.db.sql(
		"""
		SELECT TABLE_NAME, DATA_LENGTH, INDEX_LENGTH
		FROM information_schema.TABLES
		WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN %(tables)s
		""",
		{"tables": SAMPLED_TABLES},
	)
	return {name: {"data": int(data or 0), "index": int(index or 0)} for name, data, index in rows}


def _cpu_total() -> int:
	with open("/proc/stat") as handle:
		fields = handle.readline().split()[1:]
	return sum(int(value) for value in fields)


def sample(seconds: int, interval: int, out: str) -> dict:
	"""Write one JSON line per interval until `seconds` elapse or the stop file appears.

	Runs beside the campaign rather than inside it: every quantity here is a property of the
	machine while the migration is happening, and a sampler that shares the workers' process
	would perturb the very numbers it reports.
	"""
	from frappe.utils.background_jobs import get_queue

	stop_file = out + ".stop"
	deadline = time.monotonic() + seconds
	campaign = _campaign_name()
	written = 0

	with open(out, "a") as handle:
		while time.monotonic() < deadline and not os.path.exists(stop_file):
			pids = _rehearsal_pids()
			mysqld = _mysqld_pid()
			depths = {}
			for queue_name in ("short", "default", "long", "cloud_migration"):
				try:
					depths[queue_name] = len(get_queue(queue_name))
				except Exception as exc:  # noqa: BLE001 - an unusable queue is a finding
					depths[queue_name] = f"unavailable: {exc}"

			redis_info = {}
			try:
				redis_info = get_queue("cloud_migration").connection.info(section="memory")
			except Exception:  # noqa: BLE001
				redis_info = {}

			disk = shutil.disk_usage(frappe.get_site_path())
			row = {
				"at": time.time(),
				"cpu_total_ticks": _cpu_total(),
				"loadavg": os.getloadavg()[0],
				"rehearsal": {str(pid): _proc_metrics(pid) for pid in pids},
				# **Recorded explicitly so "nothing matched" can never read as "nothing
				# happened".** `_rehearsal_pids` matches only processes running this module, so
				# work driven from anywhere else is invisible to it and the sample carries zero
				# RSS and zero I/O -- indistinguishable from an idle application. That is not
				# hypothetical: it invalidated the 4188-object run in
				# `docs/evidence/f3-disk-io-scale.md`, and it recurred when a loaded run was
				# driven from an out-of-tree script while 79 MB and 131 MB were moving.
				"matched_process_count": len(pids),
				"matched_pids": [str(pid) for pid in pids],
				"mysqld": _proc_metrics(mysqld) if mysqld else None,
				"tables": _table_bytes(),
				"queue_depths": depths,
				"redis_used_memory_bytes": int(redis_info.get("used_memory") or 0),
				"redis_peak_memory_bytes": int(redis_info.get("used_memory_peak") or 0),
				"disk_free_bytes": disk.free,
				# Device counters, not process counters: the only place real IOPS and service
				# latency come from. Deltas between samples give reads/s, writes/s, sectors/s
				# and await; `io_ms` gives utilisation.
				"diskstats": _diskstats(),
				"objects": dict(
					frappe.db.sql(
						"""
						SELECT status, COUNT(*) FROM `tabCloud Migration Object`
						WHERE campaign = %(campaign)s GROUP BY status
						""",
						{"campaign": campaign},
					)
				)
				if campaign
				else {},
			}
			handle.write(json.dumps(row) + "\n")
			handle.flush()
			written += 1
			# The connection has to end its read view or every later sample reports the
			# counts of the first one (InnoDB REPEATABLE READ).
			frappe.db.rollback()
			time.sleep(interval)

	return {"samples": written, "path": out}


#: The three states a loaded measurement can be in. Collapsing them is how this project
#: produced two false readings in one cycle, so they are named rather than inferred.
VALID_MEASUREMENT = "VALID_MEASUREMENT"
INVALID_MEASUREMENT = "INVALID_MEASUREMENT"
RUN_FAILED = "RUN_FAILED"


def classify_loaded_run(
	*,
	run_stage_rc,
	result_parsed,
	matched_process_count,
	bytes_transferred,
	expected_objects,
	verified,
	pending,
	failed,
	allow_failed=0,
):
	"""Decide whether a loaded F3 point may be used, and say which of three states it is in.

	**Why three states and not two.** "Did it pass?" hides the distinction that actually
	matters here, and this cycle produced one of each:

	* `RUN_FAILED` — the workload never ran. A second campaign was started while another was
	  still `Running`; the product correctly refused it, the wrapper sent stderr to
	  `/dev/null`, and the result was `wall 0s` with an empty artifact. Nothing was measured
	  because nothing happened.
	* `INVALID_MEASUREMENT` — work happened, the evidence is unusable. A loaded run driven
	  from an out-of-tree script matched zero application processes, so peak RSS and all app
	  I/O read as `0` while 79 MB and 131 MB were genuinely transferred.
	* `VALID_MEASUREMENT` — work happened and was measured.

	The first two are *not* failures of the product, and neither may contribute a number to a
	scaling verdict. Reporting either as a low-resource reading is how a gate comes to certify
	an idle machine.

	Returns `(state, reasons)`. `reasons` is empty only for `VALID_MEASUREMENT`.
	"""
	reasons = []

	# --- did the workload run at all? -------------------------------------------------
	if run_stage_rc not in (0, None):
		reasons.append(f"run stage exited {run_stage_rc}")
	if not result_parsed:
		reasons.append("run-stage result artifact missing, empty or unparseable")
	if reasons:
		return RUN_FAILED, reasons

	# --- it ran; is the evidence usable? ----------------------------------------------
	if cint(matched_process_count) <= 0:
		reasons.append(
			"the sampler matched no application process, so every application metric would be "
			"zero-by-absence rather than measured"
		)
	if cint(bytes_transferred) <= 0:
		reasons.append("no bytes were transferred, so there is no load to characterise")
	if cint(pending) != 0:
		reasons.append(f"{cint(pending)} object(s) still Pending: the corpus did not converge")
	if cint(verified) != cint(expected_objects):
		reasons.append(f"Verified {cint(verified)} != expected {cint(expected_objects)}")
	if cint(failed) > cint(allow_failed):
		reasons.append(f"{cint(failed)} failed object(s), above the permitted {cint(allow_failed)}")

	if reasons:
		return INVALID_MEASUREMENT, reasons
	return VALID_MEASUREMENT, []


def sample_report(path: str, *, idle_baseline: bool = False) -> dict:
	"""Turn the sample file into the bounded-resource claims F3 asks for.

	`idle_baseline=True` declares that this window is *supposed* to have no application
	processes. Only a run explicitly classified that way may report zero matched processes; a
	loaded run may not, and the distinction is the whole point (see below).
	"""
	rows = []
	with open(path) as handle:
		for line in handle:
			line = line.strip()
			if line:
				rows.append(json.loads(line))
	if len(rows) < 2:
		return {"samples": len(rows), "insufficient": True}

	# ---- the invariant that this report exists to enforce -------------------------------
	#
	# **Zero matched processes is not zero resource use — it is an unmeasured run.**
	#
	# `_rehearsal_pids` matches only processes running this module. Work driven any other way
	# is invisible, and every application field then reports 0: peak RSS 0, read 0, write 0.
	# That reads as a beautifully behaved application and is in fact a measurement of nothing.
	#
	# It has now caused two false readings in this project. `docs/evidence/f3-disk-io-scale.md`
	# records the first (the 4188-object run, whose application metrics were excluded rather
	# than reported). The second came from driving a loaded run out of an out-of-tree script:
	# 79 MB and 131 MB genuinely transferred while the report showed `app write 0.0 B/s`.
	#
	# So the report refuses rather than returns. A caller cannot mistake an exception for a
	# tidy number, and `INVALID_UNMATCHED_PROCESS` names the cause instead of leaving the next
	# reader to rediscover it.
	matched_counts = [int(row.get("matched_process_count") or 0) for row in rows]
	matched_pids = sorted({pid for row in rows for pid in (row.get("matched_pids") or [])})
	peak_matched = max(matched_counts) if matched_counts else 0

	if peak_matched == 0 and not idle_baseline:
		return {
			"samples": len(rows),
			"status": "INVALID_UNMATCHED_PROCESS",
			"matched_process_count": 0,
			"matched_pids": [],
			"app_peak_rss": "INVALID_UNMATCHED_PROCESS",
			"app_read_bytes": "INVALID_UNMATCHED_PROCESS",
			"app_write_bytes": "INVALID_UNMATCHED_PROCESS",
			"reason": (
				"the sampler matched no application process in any sample, so every "
				"application metric would be zero-by-absence rather than measured. A loaded "
				"run cannot be reported from this window; re-run with the workers started "
				"through this harness so `_rehearsal_pids` can see them."
			),
		}

	def _sum(row, group, key):
		if group == "mysqld":
			return (row.get("mysqld") or {}).get(key, 0) or 0
		return sum((metrics or {}).get(key, 0) or 0 for metrics in (row.get("rehearsal") or {}).values())

	cpu_windows = {"rehearsal": [], "mysqld": []}
	io_windows = {
		# Block-layer (what reached the device) and syscall-level (what the process moved).
		# Both, because a cached read is real I/O the app performed and zero I/O the disk saw.
		"rehearsal_write": [],
		"mysqld_write": [],
		"rehearsal_read": [],
		"mysqld_read": [],
		"rehearsal_wchar": [],
		"rehearsal_rchar": [],
		"rehearsal_syscw": [],
		"rehearsal_syscr": [],
		"mysqld_wchar": [],
		"mysqld_rchar": [],
	}
	for previous, current in zip(rows, rows[1:], strict=False):
		elapsed = current["at"] - previous["at"]
		if elapsed <= 0:
			continue
		for group in ("rehearsal", "mysqld"):
			delta = _sum(current, group, "cpu_ticks") - _sum(previous, group, "cpu_ticks")
			# A worker restart resets the tick total; a negative window is that, not usage.
			if delta >= 0:
				cpu_windows[group].append(delta / _CLOCK_TICKS / elapsed)
		for group, key in (
			("rehearsal_write", "write_bytes"),
			("rehearsal_read", "read_bytes"),
			("rehearsal_wchar", "wchar"),
			("rehearsal_rchar", "rchar"),
			("rehearsal_syscw", "syscw"),
			("rehearsal_syscr", "syscr"),
		):
			delta = _sum(current, "rehearsal", key) - _sum(previous, "rehearsal", key)
			if delta >= 0:
				io_windows[group].append(delta / elapsed)
		for group, key in (
			("mysqld_write", "write_bytes"),
			("mysqld_read", "read_bytes"),
			("mysqld_wchar", "wchar"),
			("mysqld_rchar", "rchar"),
		):
			delta = _sum(current, "mysqld", key) - _sum(previous, "mysqld", key)
			if delta >= 0:
				io_windows[group].append(delta / elapsed)

	def _stats(values):
		if not values:
			return None
		ordered = sorted(values)
		return {
			"min": round(ordered[0], 4),
			"median": round(ordered[len(ordered) // 2], 4),
			"max": round(ordered[-1], 4),
		}

	table_peaks = {}
	for table in SAMPLED_TABLES:
		totals = [
			(row["tables"].get(table) or {}).get("data", 0) + (row["tables"].get(table) or {}).get("index", 0)
			for row in rows
			if row.get("tables")
		]
		if totals:
			table_peaks[table] = {"first": totals[0], "peak": max(totals), "last": totals[-1]}

	depths = {}
	for queue_name in ("short", "default", "long", "cloud_migration"):
		numbers = [
			row["queue_depths"].get(queue_name)
			for row in rows
			if isinstance(row.get("queue_depths", {}).get(queue_name), int)
		]
		depths[queue_name] = (
			{"max": max(numbers), "median": sorted(numbers)[len(numbers) // 2]} if numbers else None
		)

	rss = [
		sum((metrics or {}).get("rss_bytes", 0) for metrics in (row.get("rehearsal") or {}).values())
		for row in rows
	]
	mysqld_rss = [(row.get("mysqld") or {}).get("rss_bytes", 0) for row in rows]

	return {
		"path": path,
		"samples": len(rows),
		# Always present, so a reader never has to infer whether the application half of
		# this window was measured at all.
		"status": "IDLE_BASELINE" if idle_baseline else "MEASURED",
		"matched_process_count": peak_matched,
		"matched_pids": matched_pids,
		"app_peak_rss": max(
			(
				sum((mm or {}).get("rss_bytes", 0) or 0 for mm in (row.get("rehearsal") or {}).values())
				for row in rows
			),
			default=0,
		),
		"app_read_bytes": _sum(rows[-1], "rehearsal", "rchar") - _sum(rows[0], "rehearsal", "rchar"),
		"app_write_bytes": _sum(rows[-1], "rehearsal", "wchar") - _sum(rows[0], "rehearsal", "wchar"),
		"window_seconds": round(rows[-1]["at"] - rows[0]["at"], 1),
		"cpu_cores": {group: _stats(values) for group, values in cpu_windows.items()},
		"disk_io_bytes_per_second": {group: _stats(values) for group, values in io_windows.items()},
		"rehearsal_rss_bytes": {"max": max(rss), "last": rss[-1]},
		"mysqld_rss_bytes": {"max": max(mysqld_rss), "last": mysqld_rss[-1]},
		"redis_used_memory_bytes": {
			"max": max(row.get("redis_used_memory_bytes", 0) for row in rows),
			"last": rows[-1].get("redis_used_memory_bytes", 0),
		},
		"queue_depths": depths,
		"table_bytes": table_peaks,
		"disk_free_bytes": {
			"min": min(row["disk_free_bytes"] for row in rows),
			"first": rows[0]["disk_free_bytes"],
			"last": rows[-1]["disk_free_bytes"],
		},
		"loadavg": _stats([row["loadavg"] for row in rows]),
	}


# ------------------------------------------------------------------- fault injection


#: When each fault fires, as a fraction of the corpus that has left `Pending`. Ordered so
#: the cheap operator controls are proved while the campaign is young and the expensive
#: crash recoveries happen while there is still enough work left for the campaign to have
#: to converge afterwards.
FAULT_PLAN = (
	(0.02, "pause_resume"),
	(0.05, "stop_after_batch"),
	(0.09, "retry_backoff"),
	(0.14, "kill_worker"),
	(0.40, "redis_flush"),
	(0.55, "worker_restart"),
	(0.70, "interrupt"),
)

#: How long a fault may take to be observed before it is recorded as a failure rather than
#: waited on forever. Generous: these are wall-clock waits on a loaded machine.
FAULT_TIMEOUT = 1800


class Harness:
	"""The processes a fault injection needs to be able to start, watch and kill.

	The poller and the workers are real subprocesses of this module, not threads, because
	three of the seven faults are "this process dies" and a thread cannot be `kill -9`ed.
	"""

	def __init__(self, site: str, workers: int, log_dir: str, stale_seconds: int | None = None):
		self.site = site
		self.workers = workers
		self.log_dir = log_dir
		self.stale_seconds = stale_seconds
		self.poller = None
		self.worker_procs: list = []

	def _spawn(self, args: list[str], log_name: str, env_extra: dict | None = None):
		import subprocess

		env = dict(os.environ)
		env["PYTHONPATH"] = os.environ.get("PYTHONPATH", "")
		if env_extra:
			env.update(env_extra)
		handle = open(os.path.join(self.log_dir, log_name), "a")
		# `--title` must be forwarded: the poller and the workers are separate processes, so a
		# title set on the parent's globals does not reach them. Without this they drive the
		# default campaign while the fault watches the one under test, and the fault reports
		# "corpus never reached N" while the corpus it is watching never moves at all.
		title_args = ["--title", CAMPAIGN_TITLE] if CAMPAIGN_TITLE != DEFAULT_CAMPAIGN_TITLE else []
		return subprocess.Popen(
			[
				sys.executable,
				"-m",
				"cloud_file_storage.tests.rehearsal",
				*args,
				"--site",
				self.site,
				*title_args,
			],
			stdout=handle,
			stderr=handle,
			env=env,
			cwd=os.path.dirname(os.path.abspath(frappe.get_site_path())),
		)

	def start_workers(self, count: int | None = None):
		for _index in range(count if count is not None else self.workers):
			self.worker_procs.append(self._spawn(["worker"], f"fault-worker-{len(self.worker_procs)}.log"))
		return [proc.pid for proc in self.worker_procs]

	def start_poller(self):
		extra = {}
		if self.stale_seconds:
			extra[STALE_SECONDS_ENV] = str(self.stale_seconds)
		self.poller = self._spawn(["run"], "fault-poller.log", extra)
		return self.poller.pid

	def stop_all(self):
		import signal

		for proc in [*self.worker_procs, self.poller]:
			if proc and proc.poll() is None:
				try:
					proc.send_signal(signal.SIGTERM)
				except OSError:
					pass
		time.sleep(5)
		for proc in [*self.worker_procs, self.poller]:
			if proc and proc.poll() is None:
				try:
					proc.kill()
				except OSError:
					pass
		self.worker_procs = []
		self.poller = None


def _counts(campaign: str) -> dict:
	frappe.db.rollback()
	return dict(
		frappe.db.sql(
			"SELECT status, COUNT(*) FROM `tabCloud Migration Object` WHERE campaign=%(c)s GROUP BY status",
			{"c": campaign},
		)
	)


def _batch_counts(campaign: str) -> dict:
	frappe.db.rollback()
	return dict(
		frappe.db.sql(
			"SELECT status, COUNT(*) FROM `tabCloud Migration Batch` WHERE campaign=%(c)s GROUP BY status",
			{"c": campaign},
		)
	)


def _moved(campaign: str) -> int:
	"""Objects that have left `Pending` — the progress signal every fault is timed against."""
	frappe.db.rollback()
	return int(
		frappe.db.sql(
			"SELECT COUNT(*) FROM `tabCloud Migration Object` WHERE campaign=%(c)s AND status <> 'Pending'",
			{"c": campaign},
		)[0][0]
	)


def _wait_for(predicate, timeout: int = FAULT_TIMEOUT, interval: float = 2.0):
	"""Poll until the predicate is true. Returns the seconds it took, or None on timeout."""
	started = time.monotonic()
	while time.monotonic() - started < timeout:
		frappe.db.rollback()
		if predicate():
			return round(time.monotonic() - started, 1)
		time.sleep(interval)
	return None


def _work_horse_pids(harness: Harness) -> list[int]:
	"""The forked RQ children — the processes actually executing a batch right now."""
	parents = {proc.pid for proc in harness.worker_procs}
	horses = []
	for pid in _rehearsal_pids():
		try:
			with open(f"/proc/{pid}/stat") as handle:
				ppid = int(handle.read().rsplit(") ", 1)[1].split()[1])
		except (OSError, IndexError, ValueError):
			continue
		if ppid in parents:
			horses.append(pid)
	return horses


# --------------------------------------------------------------------- the seven faults


def fault_pause_resume(campaign: str, harness: Harness) -> dict:
	"""The operator's PAUSE is felt within one object, and nothing moves until RESUME."""
	from cloud_file_storage.migration import api

	before = _moved(campaign)
	api.pause(campaign)
	quiesced = _wait_for(lambda: _in_flight(campaign) == 0, timeout=600)
	at_pause = _moved(campaign)

	# The claim under test is "nothing moves", so the observation window has to be long
	# enough for the fastest batch seen so far to have finished several times over.
	time.sleep(30)
	after_idle = _moved(campaign)

	status_paused = frappe.db.get_value("Cloud Migration Campaign", campaign, "status")
	api.resume(campaign)
	progressed = _wait_for(lambda: _moved(campaign) > after_idle, timeout=900)

	return {
		"fault": "pause_resume",
		"objects_moved_when_pause_issued": before,
		"objects_moved_when_quiesced": at_pause,
		"objects_moved_after_30s_paused": after_idle,
		"seconds_to_quiesce": quiesced,
		"campaign_status_while_paused": status_paused,
		"seconds_to_first_progress_after_resume": progressed,
		"passed": bool(
			quiesced is not None
			and after_idle == at_pause
			and status_paused == "Paused"
			and progressed is not None
		),
	}


def fault_stop_after_batch(campaign: str, harness: Harness) -> dict:
	"""STOP drains the current object and stops on a batch boundary, mid-transition free."""
	from cloud_file_storage.migration import api

	before = _moved(campaign)
	api.stop(campaign)

	def _stopped():
		return api.finalize_stop(campaign).get("status") == "Stopped"

	seconds = _wait_for(_stopped, timeout=900, interval=3.0)
	batches = _batch_counts(campaign)
	half = int(
		frappe.db.sql(
			"SELECT COUNT(*) FROM `tabCloud Migration Object` "
			"WHERE campaign=%(c)s AND status IN ('Uploading','Verifying')",
			{"c": campaign},
		)[0][0]
	)
	at_stop = _moved(campaign)

	api.start_migration(campaign, skip_preflight=True)
	resumed = _wait_for(lambda: _moved(campaign) > at_stop, timeout=900)

	return {
		"fault": "stop_after_batch",
		"objects_moved_when_stop_issued": before,
		"objects_moved_when_stopped": at_stop,
		"seconds_to_stop": seconds,
		"batches_at_stop": batches,
		"objects_left_mid_transition": half,
		"in_flight_batches_at_stop": sum(
			count
			for status, count in batches.items()
			if status not in ("Pending", "Uploaded", "Verified", "Cleaned", "Failed")
		),
		"seconds_to_first_progress_after_restart": resumed,
		"passed": bool(seconds is not None and half == 0 and resumed is not None),
	}


def fault_retry_backoff(campaign: str, harness: Harness, sample_size: int = 12) -> dict:
	"""A local read that fails is retried with a bounded backoff, and still converges.

	The injected failure is a real one an operator would recognise: the bytes are there but
	unreadable. `chmod 000` denies the owner too, so `digest_path` raises `PermissionError` —
	neither a transport error nor an integrity error, which is exactly the shape the engine
	has to treat as retryable-but-not-transient.
	"""
	rows = frappe.db.sql(
		"""
		SELECT name, disk_path FROM `tabCloud Migration Object`
		WHERE campaign=%(c)s AND status='Pending' AND on_disk=1 AND is_thumbnail=0
		  AND disk_path IS NOT NULL
		ORDER BY name LIMIT %(limit)s
		""",
		{"c": campaign, "limit": sample_size},
		as_dict=True,
	)
	targets = [row for row in rows if row.disk_path and os.path.exists(row.disk_path)]
	if not targets:
		return {"fault": "retry_backoff", "passed": False, "reason": "no eligible Pending object on disk"}

	modes = {}
	for row in targets:
		modes[row.disk_path] = os.stat(row.disk_path).st_mode
		os.chmod(row.disk_path, 0o000)

	names = [row.name for row in targets]
	placeholders = ", ".join(["%s"] * len(names))

	def _attempted():
		return (
			int(
				frappe.db.sql(
					f"SELECT COUNT(*) FROM `tabCloud Migration Object` "
					f"WHERE name IN ({placeholders}) AND attempt_count > 0",
					names,
				)[0][0]
			)
			>= 1
		)

	seconds_to_first_retry = _wait_for(_attempted, timeout=1200, interval=3.0)
	observed = frappe.db.sql(
		f"SELECT name, status, attempt_count, error_class, "
		f"TIMESTAMPDIFF(SECOND, NOW(), next_retry_at) AS backoff_seconds "
		f"FROM `tabCloud Migration Object` WHERE name IN ({placeholders}) AND attempt_count > 0",
		names,
		as_dict=True,
	)

	for path, mode in modes.items():
		os.chmod(path, mode)

	def _recovered():
		return (
			int(
				frappe.db.sql(
					f"SELECT COUNT(*) FROM `tabCloud Migration Object` "
					f"WHERE name IN ({placeholders}) AND status IN ('Pending','Uploading')",
					names,
				)[0][0]
			)
			== 0
		)

	seconds_to_recover = _wait_for(_recovered, timeout=FAULT_TIMEOUT, interval=5.0)
	final = dict(
		frappe.db.sql(
			f"SELECT status, COUNT(*) FROM `tabCloud Migration Object` "
			f"WHERE name IN ({placeholders}) GROUP BY status",
			names,
		)
	)

	backoffs = [row.backoff_seconds for row in observed if row.backoff_seconds is not None]
	return {
		"fault": "retry_backoff",
		"objects_made_unreadable": len(targets),
		"objects_that_recorded_an_attempt": len(observed),
		"error_classes": sorted({row.error_class for row in observed if row.error_class}),
		"backoff_seconds_seen": sorted(backoffs)[:10],
		"backoff_within_cap": all(0 < value <= 3600 for value in backoffs) if backoffs else False,
		"seconds_to_first_retry": seconds_to_first_retry,
		"seconds_to_recover_after_permissions_restored": seconds_to_recover,
		"final_statuses": final,
		"passed": bool(
			observed
			and backoffs
			and all(0 < value <= 3600 for value in backoffs)
			and seconds_to_recover is not None
			and not final.get("Failed")
		),
	}


def fault_kill_worker(campaign: str, harness: Harness) -> dict:
	"""`kill -9` a worker mid-batch. Recovery is the dispatcher's, at the shipped 600s window.

	This is the one fault deliberately timed against the production
	`engine.STALE_AFTER_SECONDS`, so what is proved is the constant the app ships, not a
	rehearsal-only value. The later crash faults shorten it — see `STALE_SECONDS_ENV`.
	"""
	import signal

	horses = _work_horse_pids(harness)
	if not horses:
		return {"fault": "kill_worker", "passed": False, "reason": "no work-horse was executing a batch"}

	victim = horses[0]
	in_flight_before = frappe.db.get_all(
		"Cloud Migration Batch",
		filters={
			"campaign": campaign,
			"status": ("in", ("UploadDispatched", "Uploading", "VerifyDispatched", "Verifying")),
		},
		fields=["name", "status", "attempts"],
		limit_page_length=0,
	)
	moved_before = _moved(campaign)

	# The parent too: killing only the horse leaves a live worker that would pick the next
	# job up immediately, which is a job failure, not the crash the gate is about.
	parents = [proc for proc in harness.worker_procs if proc.poll() is None]
	victim_parent = None
	for proc in parents:
		try:
			with open(f"/proc/{victim}/stat") as handle:
				ppid = int(handle.read().rsplit(") ", 1)[1].split()[1])
		except (OSError, IndexError, ValueError):
			break
		if proc.pid == ppid:
			victim_parent = proc
			break

	os.kill(victim, signal.SIGKILL)
	if victim_parent is not None:
		os.kill(victim_parent.pid, signal.SIGKILL)
		harness.worker_procs.remove(victim_parent)

	stranded = [row.name for row in in_flight_before]
	placeholders = ", ".join(["%s"] * len(stranded)) if stranded else None

	def _recovered():
		if not stranded:
			return _moved(campaign) > moved_before
		remaining = int(
			frappe.db.sql(
				f"SELECT COUNT(*) FROM `tabCloud Migration Batch` WHERE name IN ({placeholders}) "
				"AND status IN ('UploadDispatched','Uploading','VerifyDispatched','Verifying')",
				stranded,
			)[0][0]
		)
		return remaining == 0

	seconds = _wait_for(_recovered, timeout=FAULT_TIMEOUT, interval=10.0)
	harness.start_workers(1)
	progressed = _wait_for(lambda: _moved(campaign) > moved_before, timeout=FAULT_TIMEOUT, interval=5.0)

	return {
		"fault": "kill_worker",
		"stale_window_seconds": 600,
		"killed_work_horse_pid": victim,
		"killed_worker_pid": victim_parent.pid if victim_parent else None,
		"batches_in_flight_at_kill": [dict(row) for row in in_flight_before],
		"seconds_to_recover_stranded_batches": seconds,
		"seconds_to_next_progress": progressed,
		"objects_moved_at_kill": moved_before,
		"objects_moved_after": _moved(campaign),
		"passed": bool(seconds is not None and progressed is not None),
	}


def fault_redis_flush(campaign: str, harness: Harness) -> dict:
	"""Lose the queue's Redis state under a running campaign.

	Scoped to this site's own queue keys rather than `FLUSHDB`: the queue Redis is shared
	with every other site on the bench, and a rehearsal is not allowed to throw away work
	that is not its own. What the campaign experiences is identical — its queue, its job
	hashes and its registries are gone, and the batches that were dispatched will never be
	delivered to anybody.
	"""
	from frappe.utils.background_jobs import generate_qname, get_redis_conn

	connection = get_redis_conn()
	qname = generate_qname("cloud_migration")
	# `rq:job:` keys carry the namespaced id, whose separator is version-dependent -- see
	# migration_job_marker(). Building it from the marker keeps this correct on v15 and v16.
	job_key_glob = f"rq:job:{migration_job_marker()}{campaign}*"
	patterns = [f"rq:queue:{qname}", f"rq:*:{qname}*", job_key_glob, "rq:clean_registries:*"]
	deleted = 0
	for pattern in patterns:
		keys = list(connection.scan_iter(match=pattern, count=1000))
		if keys:
			deleted += int(connection.delete(*keys))

	moved_before = _moved(campaign)
	in_flight_before = _in_flight(campaign)
	seconds = _wait_for(lambda: _moved(campaign) > moved_before + 50, timeout=FAULT_TIMEOUT, interval=10.0)

	return {
		"fault": "redis_flush",
		"stale_window_seconds": harness.stale_seconds,
		"redis_keys_deleted": deleted,
		"key_patterns": patterns,
		"batches_in_flight_when_flushed": in_flight_before,
		"objects_moved_when_flushed": moved_before,
		"seconds_to_resume_progress": seconds,
		"objects_moved_after": _moved(campaign),
		"passed": seconds is not None,
	}


def fault_worker_restart(campaign: str, harness: Harness) -> dict:
	"""A `bench restart`: every worker goes away mid-batch and a new set comes back."""
	moved_before = _moved(campaign)
	in_flight_before = _in_flight(campaign)
	old = [proc.pid for proc in harness.worker_procs]
	for proc in harness.worker_procs:
		if proc.poll() is None:
			proc.terminate()
	time.sleep(5)
	for proc in harness.worker_procs:
		if proc.poll() is None:
			proc.kill()
	harness.worker_procs = []
	time.sleep(5)
	new = harness.start_workers()

	seconds = _wait_for(lambda: _moved(campaign) > moved_before + 50, timeout=FAULT_TIMEOUT, interval=10.0)
	return {
		"fault": "worker_restart",
		"stale_window_seconds": harness.stale_seconds,
		"workers_killed": old,
		"workers_started": new,
		"batches_in_flight_at_restart": in_flight_before,
		"objects_moved_at_restart": moved_before,
		"seconds_to_resume_progress": seconds,
		"objects_moved_after": _moved(campaign),
		"passed": seconds is not None,
	}


def fault_interrupt(campaign: str, harness: Harness) -> dict:
	"""The driving process is interrupted — `kill -9` on the poller, then restarted.

	The campaign must not depend on it: workers chain the next batch themselves, so the only
	thing lost while the poller is gone is the stale-batch watchdog.
	"""
	import signal

	moved_before = _moved(campaign)
	poller_pid = harness.poller.pid if harness.poller else None
	if poller_pid:
		os.kill(poller_pid, signal.SIGKILL)
		harness.poller.wait(timeout=30)
		harness.poller = None

	time.sleep(30)
	moved_without_poller = _moved(campaign)
	new_pid = harness.start_poller()
	seconds = _wait_for(lambda: _moved(campaign) > moved_without_poller, timeout=FAULT_TIMEOUT, interval=5.0)

	return {
		"fault": "interrupt",
		"killed_poller_pid": poller_pid,
		"objects_moved_at_kill": moved_before,
		"objects_moved_30s_later_with_no_poller": moved_without_poller,
		"campaign_kept_moving_without_the_poller": moved_without_poller > moved_before,
		"restarted_poller_pid": new_pid,
		"seconds_to_progress_after_restart": seconds,
		"passed": bool(seconds is not None),
	}


FAULTS = {
	"pause_resume": fault_pause_resume,
	"stop_after_batch": fault_stop_after_batch,
	"retry_backoff": fault_retry_backoff,
	"kill_worker": fault_kill_worker,
	"redis_flush": fault_redis_flush,
	"worker_restart": fault_worker_restart,
	"interrupt": fault_interrupt,
}


def faults(
	site: str, log_dir: str, workers: int = 4, stale_seconds: int = 45, only: str | None = None
) -> dict:
	"""Drive the campaign with real workers and inject the F3 fault matrix on the way.

	Resumable in the sense that matters: every fault's verdict is written out as it is
	observed, and the campaign it operates on is re-derivable from the database, so a rerun
	continues from wherever the corpus actually is.
	"""
	campaign = _campaign_name()
	harness = Harness(site, workers, log_dir, stale_seconds=stale_seconds)
	total = frappe.db.count("Cloud Migration Object", {"campaign": campaign})
	plan = [(fraction, name) for fraction, name in FAULT_PLAN if not only or name == only]
	results = []
	out_path = os.path.join(log_dir, "faults.json")

	harness.start_workers()
	harness.start_poller()
	try:
		for fraction, name in plan:
			target = int(total * fraction)
			reached = _wait_for(
				lambda goal=target: _moved(campaign) >= goal, timeout=FAULT_TIMEOUT, interval=5.0
			)
			if reached is None:
				results.append({"fault": name, "passed": False, "reason": f"corpus never reached {target}"})
				break

			# The kill -9 fault is timed against the shipped 600s window, so the poller it
			# is watching must be running with that value; every later fault uses the
			# shortened one so the matrix fits inside a single campaign.
			wants_production_window = name == "kill_worker"
			if wants_production_window != (harness.stale_seconds is None):
				harness.stale_seconds = None if wants_production_window else stale_seconds
				if harness.poller and harness.poller.poll() is None:
					harness.poller.terminate()
					harness.poller.wait(timeout=30)
				harness.start_poller()

			print(f"--- fault {name} at {_moved(campaign)}/{total}", flush=True)
			started = time.monotonic()
			record = FAULTS[name](campaign, harness)
			record["at_objects_moved"] = _moved(campaign)
			record["seconds"] = round(time.monotonic() - started, 1)
			results.append(record)
			with open(out_path, "w") as handle:
				handle.write(
					json.dumps({"campaign": campaign, "faults": results}, indent=2, default=str) + "\n"
				)
			print(f"--- fault {name}: passed={record.get('passed')} in {record['seconds']}s", flush=True)

		converged = _wait_for(
			lambda: _in_flight(campaign) == 0 and _counts(campaign).get("Pending", 0) == 0,
			timeout=FAULT_TIMEOUT * 3,
			interval=15.0,
		)
	finally:
		harness.stop_all()

	summary = {
		"campaign": campaign,
		"faults": results,
		"all_passed": all(record.get("passed") for record in results),
		"seconds_to_converge_after_last_fault": converged,
		"final_counts": _counts(campaign),
		"final_batches": _batch_counts(campaign),
	}
	with open(out_path, "w") as handle:
		handle.write(json.dumps(summary, indent=2, default=str) + "\n")
	return summary


# ------------------------------------------------------------------ bandwidth throttle


#: The 5% slack on the ceiling absorbs the last partial sleep of a token bucket; it is not a
#: negotiating margin and is asserted at its bound by `test_f3_throttle_verdict.py`.
THROTTLE_CEILING_SLACK = 1.05

#: The throttled batch must have spent at least this fraction of its elapsed time asleep.
#: `slept > 0` alone is satisfiable by an overhead-bound run containing one object large enough
#: to overshoot the bucket once: a single 1 MB object followed by 99 tiny ones sleeps ~0.3s and
#: then crawls for two minutes on per-object overhead, landing at ~3% of the ceiling with
#: `throttle_engaged: true`. That is the very run this criterion exists to reject. Pinned from
#: both sides by `test_f3_throttle_verdict.py`.
THROTTLE_MIN_SLEEP_FRACTION = 0.25

#: Campaign statuses meaning "something is running against this site right now", taken from the
#: DocType's own Select options. `Draft`/`Analyzed`/`Planned` are pre-run; `Stopped`/`Completed`/
#: `Failed` are terminal. `Paused` is in because a paused campaign holds partial state a benchmark
#: must not start on top of.
CAMPAIGN_IN_FLIGHT_STATUSES = ("Analyzing", "Running", "Paused", "Stopping", "Cleanup Running")


def _starting_state_valid(
	residual_cso: int, by_status: dict, campaigns: int, pending_batches: int = 0
) -> bool:
	"""Is this site in the known state a throttle benchmark may start from?

	Pure, so it can be tested against the degenerate inputs a live site rarely produces.

	The subset test alone was vacuous: `set({}) <= {"Pending", "Skipped"}` is True, so a site with
	**no migration objects at all** — nothing seeded, or a campaign someone had just purged —
	reported a positive verdict on a state it had never examined. A precondition check must
	confirm that what it needs is present, not merely that what it forbids is absent, so this
	requires what the stage actually consumes: at least one Pending object, and the **two Pending
	UPLOAD batches** `throttle()` needs to have two comparable runs to compare. A zero-Pending
	start is not a judgement call: `run_upload_batch` iterates `iter_batch_objects(batch,
	("Pending",))`, so such a batch processes nothing, `consumed` stays 0, and the verdict is
	`NO_TRANSFER` / `INVALID_MEASUREMENT`. Refusing up front only rejects runs that could not have
	produced a number, two minutes earlier and legibly.

	**Scoped to this benchmark, deliberately.** The subset clause encodes "a purpose-seeded clean
	corpus", so it would refuse the anomaly-rich 100k rehearsal corpus, which carries `Conflict`
	rows at analysis time. That is correct for a throttle measurement and wrong for a general
	preflight — do not reach for this as one.
	"""
	if residual_cso != 0 or campaigns != 1:
		return False
	if not by_status or not by_status.get("Pending"):
		return False
	# `throttle()` compares two batches, and refuses with "needs two Pending UPLOAD batches" if
	# it cannot get them. A precondition that stops at "some Pending object exists" therefore
	# still passes a site the stage will reject — so it checks the quantity actually required.
	if pending_batches < 2:
		return False
	return set(by_status) <= {"Pending", "Skipped"}


def throttle_preflight() -> dict:
	"""F3 — the six mechanical checks that must pass BEFORE a throttle benchmark is believed.

	A stage rather than a runbook paragraph, because a gate whose application is optional is not
	a gate — and because the first attempt at this measurement was invalidated by exactly the
	conditions checked here: a credential that was never persisted (a Password field read through
	`hasattr` instead of `get_password`, so it silently read as absent), and colliding
	content/state from a previous campaign that turned every upload into a typed retryable
	duplicate. Neither was a product defect; both made the measurement meaningless.

	Every check answers a question about the *environment*, never about the product:

	* `CREDENTIALS_VALID` — read through `get_password`, the getter the runtime itself uses. A
	  credential that only appears present under `hasattr` is the defect this exists for.
	* `BUCKET_EXISTS` / `BUCKET_PREFIX_EMPTY_OR_UNIQUE` — a bucket holding prior objects makes
	  every upload a dedup hit, and dedup bytes never reach the limiter.
	* `ACTIVE_CAMPAIGNS` — an in-flight campaign (or a batch past Pending) means a second writer
	  is moving the same rows; the measurement would not be attributable to this run.
	* `STARTING_OBJECT_STATE_VALID` — exactly one campaign, no residual Cloud Storage Objects,
	  every object Pending or deliberately Skipped.
	* `WORKERS_HEALTHY` — the throttle stage runs inline; a competing worker invalidates it.

	Returns the verdicts and `PREFLIGHT` = PASS/FAIL. **If it is FAIL, do not run the benchmark.**
	"""
	from cloud_file_storage.storage import breaker, client

	checks: dict = {}
	notes: dict = {}
	settings = frappe.get_single("Cloud Storage Settings")

	# 1 — through the real getter, never `hasattr`.
	secret = settings.get_password("secret_access_key", raise_exception=False)
	checks["CREDENTIALS_VALID"] = "YES" if (secret and settings.access_key_id) else "NO"
	notes["credentials"] = (
		f"access_key_id set={bool(settings.access_key_id)}; secret via get_password set={bool(secret)}"
	)

	s3 = client.get_client()
	bucket = settings.bucket
	notes["bucket"] = bucket

	# 2 — the bucket is really there, asked of S3 rather than of the settings row.
	try:
		s3.head_bucket(Bucket=bucket)
		checks["BUCKET_EXISTS"] = "YES"
	except Exception as exc:  # noqa: BLE001 — the reason is the evidence
		checks["BUCKET_EXISTS"] = "NO"
		notes["bucket_error"] = str(exc)

	# 3 — empty *under the prefix in use*, or the corpus dedupes against whatever is already
	#     there. Scoped to `key_prefix` because that is what the check is named for: listing the
	#     whole bucket answers a stricter question than the one the name asks.
	prefix = (settings.key_prefix or "").strip()
	notes["key_prefix"] = prefix or "<none: whole bucket>"
	try:
		scope = {"Prefix": prefix} if prefix else {}
		listing = s3.list_objects_v2(Bucket=bucket, MaxKeys=5, **scope)
		keys = cint(listing.get("KeyCount", 0))
		checks["BUCKET_PREFIX_EMPTY_OR_UNIQUE"] = "YES" if keys == 0 else "NO"
		notes["bucket_keycount"] = keys
	except Exception as exc:  # noqa: BLE001
		checks["BUCKET_PREFIX_EMPTY_OR_UNIQUE"] = "NO"
		notes["bucket_list_error"] = str(exc)

	# 4 — "active" means in-flight and competing, not merely planned. A freshly planned campaign
	#     with every batch still Pending IS the intended starting state.
	# The Select has no `Dispatched` — an earlier revision filtered on a status that cannot occur,
	# so that clause matched nothing, and it missed `Analyzing`, `Stopping` and `Cleanup Running`,
	# which do. `Analyzing` is the dangerous one: a second process writes Cloud Migration Object
	# rows while no batch exists and every object still reads Pending, so every sibling check
	# passes and the preflight would wave a competing writer through.
	in_flight = frappe.db.count(
		"Cloud Migration Campaign", {"status": ["in", list(CAMPAIGN_IN_FLIGHT_STATUSES)]}
	)
	moved_batches = frappe.db.count("Cloud Migration Batch", {"status": ["not in", ["Pending"]]})
	# `phase` matters: `throttle()` selects `{"status": "Pending", "phase": "UPLOAD"}`, so counting
	# every Pending batch would let a site with two CLEANUP-phase batches satisfy a precondition
	# the stage then refuses. Unreachable from a state that passes the other five checks, but the
	# point of this count is to be the quantity the consumer consumes.
	pending_batches = frappe.db.count("Cloud Migration Batch", {"status": "Pending", "phase": "UPLOAD"})
	checks["ACTIVE_CAMPAIGNS"] = str(in_flight + moved_batches)
	notes["campaigns"] = frappe.db.get_all(
		"Cloud Migration Campaign", fields=["name", "status"], limit_page_length=0
	)
	notes["batches_past_pending"] = moved_batches
	notes["batches_pending"] = pending_batches

	# 5 — one campaign, no residual CSOs, a known starting state.
	residual_cso = frappe.db.count("Cloud Storage Object")
	# `count(name) as n` as a string is rejected by v16's SELECT parser
	# (frappe/database/query.py:1881-1897). The query builder is correct on v15 and v16 alike.
	_cmo = frappe.qb.DocType("Cloud Migration Object")
	by_status = {
		grouped.status: grouped.n
		for grouped in (
			frappe.qb.from_(_cmo).select(_cmo.status, Count("*").as_("n")).groupby(_cmo.status)
		).run(as_dict=True)
	}
	campaigns = frappe.db.count("Cloud Migration Campaign")
	checks["STARTING_OBJECT_STATE_VALID"] = (
		"YES" if _starting_state_valid(residual_cso, by_status, campaigns, pending_batches) else "NO"
	)
	notes["residual_cso"] = residual_cso
	notes["object_status"] = by_status
	notes["campaign_count"] = campaigns

	# 6 — the throttle stage runs inline; ANY other rehearsal process (worker, `run`, `analyze`,
	#     `sample`) is a second writer and makes the number unattributable. Reuses the module's
	#     existing matcher rather than a second one: `_rehearsal_pids` handles both invocation
	#     forms already, and its comment records what a narrower matcher cost last time.
	workers = [pid for pid in _rehearsal_pids() if pid != os.getpid()]
	checks["WORKERS_HEALTHY"] = "YES" if not workers else "NO"
	notes["worker_processes"] = workers or "none"

	# Not one of the six, but a closed breaker is a precondition for any byte to move at all.
	notes["breaker_open"] = breaker.is_open()

	passed = (
		checks["CREDENTIALS_VALID"] == "YES"
		and checks["BUCKET_EXISTS"] == "YES"
		and checks["BUCKET_PREFIX_EMPTY_OR_UNIQUE"] == "YES"
		and checks["ACTIVE_CAMPAIGNS"] == "0"
		and checks["STARTING_OBJECT_STATE_VALID"] == "YES"
		and checks["WORKERS_HEALTHY"] == "YES"
	)
	return {
		"site": frappe.local.site,
		"checks": checks,
		"notes": notes,
		"PREFLIGHT": "PASS" if passed else "FAIL",
		"at": frappe.utils.now(),
	}


def throttle_measurement_row(*, batch: str, limit, elapsed: float, row, result) -> dict:
	"""Derive one measurement row from a finished batch. Pure: no DB, no clock, no S3.

	Extracted for the same reason as `throttle_verdict` — while this arithmetic lived inline in
	`throttle()` the only way to test it was to restate it in the test, and a test that restates
	its subject cannot fail when the subject changes. Deleting the `NO_TRANSFER` branch left the
	suite green. Now the tests call this.

	`row` is the batch's counters (`objects_done`, `bytes_done`, `objects_failed`); `result` is
	what `run_upload_batch` returned.
	"""
	if row is None:
		# `frappe.db.get_value(..., as_dict=True)` returns None for a batch that does not exist.
		# Falling through would report `objects: 0, failed_objects: 0` beside a positive
		# `bytes_transferred` — a measurement row describing a batch nobody read.
		raise ValueError(f"no counters for batch {batch!r}: the row does not exist")
	get = row.get if hasattr(row, "get") else lambda k, d=None: getattr(row, k, d)
	res = result if isinstance(result, dict) else {}

	objects_done = cint(get("objects_done") or 0)
	bytes_done = cint(get("bytes_done") or 0)
	# Read explicitly, and never with a silent default: `objects_failed` was absent from the
	# `get_value` field list for the whole first measurement campaign, so `row.objects_failed`
	# was None and `failed_objects` was structurally 0 — a field that reported "no failures"
	# without ever consulting one. The evidence it produced said nothing at all.
	objects_failed = cint(get("objects_failed") or 0)

	# **The rate denominator is TRANSFERRED bytes, not `bytes_done`.**
	#
	# `bytes_done` is progress accounting: a `DedupReused` object advanced, so it is counted at
	# its nominal size even though its content already existed in the bucket and nothing was
	# sent. Dividing that by elapsed measures a network rate using bytes that never touched the
	# network. It read correctly only while the corpus happened to hold no duplicate content;
	# the first corpus that did would have read 411,432.9 B/s against a 250,000 B/s ceiling, a
	# `passed: false` verdict on a throttle that was in fact holding at 99.7%.
	#
	# `transferred_bytes` comes from `ThrottleBudget.consumed`, incremented only on the real
	# upload path, so a dedup-reused object contributes zero by construction, not by subtraction.
	transferred = cint(res.get("transferred_bytes") or 0)
	# `consume` is called once per object whose bytes actually crossed the network, so it
	# separates uploaded objects from dedupe-converged ones by count rather than by inference.
	consume_calls = cint(res.get("consume_calls") or 0)
	slept = float(res.get("throttle_slept_seconds") or 0.0)
	advanced = objects_done - objects_failed

	if not elapsed:
		achieved, status = None, "NOT_MEASURABLE_ZERO_ELAPSED"
	elif transferred <= 0:
		# Nothing crossed the network. A rate of 0 B/s would compare favourably with any
		# ceiling and read as a PASS, which is the one answer this must never give.
		achieved, status = None, "NO_TRANSFER"
	else:
		achieved, status = transferred / elapsed, "MEASURED"

	ceiling = limit * 1_000_000 / 8 if limit else None
	return {
		"batch": batch,
		"limit_mbps": limit,
		"limit_bytes_per_second": ceiling,
		"objects": objects_done,
		"bytes_progress_accounted": bytes_done,
		"bytes_transferred": transferred,
		# Clamped: an object that fails between `throttle.consume` and its CAS to Uploaded adds to
		# `consumed` but contributes 0 to `bytes_done`, which would render a negative "reused".
		"bytes_dedup_reused": max(0, bytes_done - transferred),
		"consume_calls": consume_calls,
		"uploaded_objects": consume_calls,
		# Objects that advanced without their bytes crossing the network. On a general corpus this
		# also counts adopted objects and thumbnail derivatives, neither of which calls `consume`;
		# it is exact only on a corpus with neither, which is what F3 measures on.
		"dedup_reused_objects": max(0, advanced - consume_calls),
		"failed_objects": objects_failed,
		"throttle_slept_seconds": round(slept, 6),
		"percent_of_ceiling": (round(100.0 * achieved / ceiling, 2) if achieved and ceiling else None),
		"seconds": round(elapsed, 2),
		"status": status,
		"achieved_bytes_per_second": round(achieved, 1) if achieved is not None else None,
	}


def throttle_verdict(measurements: list) -> dict:
	"""Decide the throttle criterion from two measured batches, and say so in one field.

	Extracted so the decision is **driven by tests rather than re-implemented in them** — a test
	that recomputes the rule it is checking passes whatever the rule becomes. It is also the
	reason the verdict is a field the harness emits: a criterion whose PASS/FAIL is settled by a
	reader doing arithmetic over a JSON blob is a criterion that can be talked into passing.

	A batch whose rate could not be measured is never a pass. `NO_TRANSFER` in particular must
	not satisfy "<= ceiling": a corpus that transfers nothing achieves 0 B/s, and 0 beats every
	ceiling there is.

	Being under the ceiling is likewise not sufficient. If the corpus is small or overhead-bound
	the run can sit far under the limit with `time.sleep` never once called — the limiter was
	present but idle, and the run then evidences the corpus, not the throttle. So the throttled
	batch must also show `throttle_slept_seconds > 0`: the limiter demonstrably held something
	back — and for a meaningful share of the run, not once. `THROTTLE_MIN_SLEEP_FRACTION` is that
	share; below it the state is INVALID_MEASUREMENT, which is neither PASS nor FAIL.
	"""
	throttled = next((row for row in measurements if row.get("limit_mbps")), None)
	free = next((row for row in measurements if not row.get("limit_mbps")), None)
	measurable = bool(
		throttled and free and throttled.get("status") == "MEASURED" and free.get("status") == "MEASURED"
	)
	# The limiter must not merely have slept, but have dominated the run (see docstring).
	slept = (throttled or {}).get("throttle_slept_seconds") or 0
	elapsed = (throttled or {}).get("seconds") or 0
	sleep_fraction = (slept / elapsed) if (measurable and elapsed) else 0.0
	engaged = bool(measurable and slept > 0 and sleep_fraction >= THROTTLE_MIN_SLEEP_FRACTION)
	passed = bool(
		measurable
		and engaged
		and throttled["achieved_bytes_per_second"]
		<= throttled["limit_bytes_per_second"] * THROTTLE_CEILING_SLACK
		and free["achieved_bytes_per_second"] > throttled["achieved_bytes_per_second"]
	)
	out = {
		"measurements": measurements,
		"passed": passed,
		"THROTTLE_CRITERION": "PASS" if passed else "FAIL",
		"measurement_state": "VALID_MEASUREMENT" if (measurable and engaged) else "INVALID_MEASUREMENT",
		"throttle_engaged": engaged,
		"sleep_fraction": round(sleep_fraction, 4),
	}
	if not measurable:
		out["reason"] = "a batch produced no measurable transfer; see each batch's `status`"
	elif not engaged:
		out["reason"] = (
			f"the limiter was not what bounded this run: it slept for {sleep_fraction:.1%} of the "
			f"elapsed time, below the {THROTTLE_MIN_SLEEP_FRACTION:.0%} floor. The run was bounded "
			"by the corpus or by per-object overhead, so it does not evidence the ceiling"
		)
	else:
		out["utilisation_of_ceiling"] = round(
			throttled["achieved_bytes_per_second"] / throttled["limit_bytes_per_second"], 4
		)
		out["unthrottled_speedup"] = round(
			free["achieved_bytes_per_second"] / throttled["achieved_bytes_per_second"], 2
		)
	return out


def throttle(mbps: int = 2) -> dict:
	"""F3 — the bandwidth limit is enforced, measured end to end on real batches.

	Two comparable batches are run inline through the same `run_upload_batch` a worker calls,
	one with the campaign's limit set and one with it off, and the achieved byte rate of each
	is compared with the configured ceiling. Inline and with no workers running, so the only
	thing that differs between the two numbers is the throttle.
	"""
	from cloud_file_storage.migration import engine

	campaign = _campaign_name()
	camp = frappe.get_doc("Cloud Migration Campaign", campaign)
	original = {"bandwidth_limit_mbps": camp.bandwidth_limit_mbps, "parallelism": camp.parallelism}

	pending = frappe.db.get_all(
		"Cloud Migration Batch",
		filters={"campaign": campaign, "status": "Pending", "phase": "UPLOAD"},
		fields=["name"],
		order_by="batch_no asc",
		limit_page_length=2,
	)
	if len(pending) < 2:
		return {"passed": False, "reason": "needs two Pending UPLOAD batches"}

	measurements = []
	try:
		for batch, limit in ((pending[0].name, mbps), (pending[1].name, 0)):
			frappe.db.set_value(
				"Cloud Migration Campaign",
				campaign,
				{"bandwidth_limit_mbps": limit, "parallelism": 1},
				update_modified=False,
			)
			frappe.db.commit()
			if not engine.cas_batch(batch, expected="Pending", to="UploadDispatched", heartbeat=True):
				continue
			started = time.monotonic()
			result = engine.run_upload_batch(campaign, batch)
			elapsed = time.monotonic() - started
			row = frappe.db.get_value(
				"Cloud Migration Batch",
				batch,
				["objects_done", "bytes_done", "objects_failed"],
				as_dict=True,
			)
			measurements.append(
				throttle_measurement_row(batch=batch, limit=limit, elapsed=elapsed, row=row, result=result)
			)
	finally:
		frappe.db.set_value("Cloud Migration Campaign", campaign, original, update_modified=False)
		# `chain_next` dispatches into an empty queue at the end of an inline batch; those
		# claims are released here rather than left for the stale watchdog to find.
		for row in frappe.db.get_all(
			"Cloud Migration Batch",
			filters={"campaign": campaign, "status": "UploadDispatched"},
			pluck="name",
			limit_page_length=0,
		):
			engine.cas_batch(row, expected="UploadDispatched", to="Pending")
		frappe.db.commit()

	verdict = throttle_verdict(measurements)
	# Provenance. Batch names are per-campaign and repeat across sites — `CFS-CAMP-0004-B1` is
	# also a batch of the 100,020-object rehearsal on a different site — so an artifact without
	# these cannot be told apart from one by its contents alone.
	verdict["site"] = frappe.local.site
	verdict["campaign"] = campaign
	verdict["bucket"] = frappe.get_single("Cloud Storage Settings").bucket
	verdict["at"] = frappe.utils.now()
	return verdict


# --------------------------------------------------------- A29 production-scale refusal


def refuse() -> dict:
	"""F4 — a production-scale campaign is refused when a threshold is not met.

	Two refusals, both driven by real quantities rather than by a patched threshold:

	* the **backup** check, by moving the site's backup directory aside so `latest_backup`
	  genuinely finds nothing, then asking `assert_startable` — with `skip_preflight=True`,
	  because the property that matters is that the flag cannot reach a production-scale
	  campaign;
	* the **disk** check, by evaluating the measured projection against a safety factor big
	  enough that this machine's real free space does not cover it.
	"""
	from cloud_file_storage.migration import preflight

	campaign = _campaign_name()
	backups = frappe.get_site_path("private", "backups")
	hidden = backups + ".rehearsal-hidden"
	result = {
		"campaign": campaign,
		"objects": frappe.db.count("Cloud Migration Object", {"campaign": campaign}),
		"production_scale_threshold": preflight.PRODUCTION_SCALE_OBJECTS,
		"is_production_scale": preflight.is_production_scale(campaign),
	}

	moved = False
	try:
		if os.path.isdir(backups):
			os.rename(backups, hidden)
			moved = True
		try:
			preflight.assert_startable(campaign, skip_preflight=True)
			result["backup_refusal"] = {"raised": False, "passed": False}
		except Exception as exc:  # noqa: BLE001 - the refusal is the observation
			result["backup_refusal"] = {
				"raised": True,
				"exception": type(exc).__name__,
				"message": str(exc)[:400],
				"skip_preflight_was_true": True,
				"passed": True,
			}
		result["preflight_status_after_refusal"] = frappe.db.get_value(
			"Cloud Migration Campaign", campaign, "preflight_status"
		)
	finally:
		if moved:
			if os.path.isdir(backups):
				for entry in os.scandir(hidden):
					os.replace(entry.path, os.path.join(backups, entry.name))
				os.rmdir(hidden)
			else:
				os.rename(hidden, backups)

	measured = preflight.measure(campaign)
	projected = preflight.project(measured)
	free = measured["disk"]["db_disk_free_bytes"]
	# Big enough that this machine's real free space cannot cover the real projection.
	factor = round((free / projected["total_bytes"]) * 2, 2)
	checks = preflight.evaluate(measured, projected, disk_safety_factor=factor)
	disk_check = next(check for check in checks if check["check"] == "database_free_space")
	result["disk_refusal"] = {
		"safety_factor_used": factor,
		"projected_total_bytes": projected["total_bytes"],
		"required_bytes": disk_check["required_bytes"],
		"free_bytes": disk_check["free_bytes"],
		"passed": disk_check["passed"] is False,
	}

	# …and the same evaluation at the shipped factor still passes, so the check above is
	# measuring the machine rather than always failing.
	shipped = next(
		check for check in preflight.evaluate(measured, projected) if check["check"] == "database_free_space"
	)
	result["disk_check_at_shipped_factor_passes"] = shipped["passed"]
	result["passed"] = bool(
		result["is_production_scale"]
		and result["backup_refusal"]["passed"]
		and result["disk_refusal"]["passed"]
		and shipped["passed"]
	)
	frappe.db.commit()
	return result


STAGES = {
	"seed": lambda args: seed(args.count),
	"analyze": lambda args: analyze(),
	"plan": lambda args: plan(),
	"run": lambda args: run(inline=args.inline),
	"worker": lambda args: worker(queue=args.queue, burst=args.burst),
	"jobs": lambda args: jobs(),
	"sample": lambda args: sample(args.seconds, args.interval, args.path or "rehearsal-samples.jsonl"),
	"sample_report": lambda args: sample_report(args.path, idle_baseline=args.idle_baseline),
	"faults": lambda args: faults(args.site, args.log_dir, workers=args.workers, only=args.fault),
	"throttle": lambda args: throttle(args.mbps),
	"throttle_preflight": lambda args: throttle_preflight(),
	"refuse": lambda args: refuse(),
	"a1": lambda args: a1(args.baseline, args.loaded, args.out),
	"probe": lambda args: probe(
		args.count if args.count < 1000 else 10, campaign=args.campaign, job_logs=args.job_log
	),
	"triage": lambda args: triage(),
	"measure": lambda args: measure(),
	"gates": lambda args: gates(),
	"purge": lambda args: purge(),
}

#: Stages that must not open a database connection of their own. An RQ worker forks a
#: work-horse per job and each horse connects for itself; a connection inherited across the
#: fork is shared by every child and corrupts as soon as two of them use it.
NO_CONNECT_STAGES = frozenset({"worker"})


def main(argv=None):
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("stage", choices=sorted(STAGES))
	parser.add_argument("--site", required=True)
	parser.add_argument("--count", type=int, default=100_000)
	parser.add_argument("--inline", action="store_true")
	parser.add_argument("--burst", action="store_true")
	parser.add_argument("--seconds", type=int, default=3600)
	parser.add_argument("--interval", type=int, default=10)
	parser.add_argument("--workers", type=int, default=4)
	parser.add_argument("--mbps", type=int, default=2)
	parser.add_argument("--title", default=None, help="campaign title; lets one site host several")
	parser.add_argument("--min-bytes", dest="min_bytes", type=int, default=None)
	parser.add_argument("--max-bytes", dest="max_bytes", type=int, default=None)
	parser.add_argument("--fault", default=None, choices=sorted(FAULTS))
	parser.add_argument("--log-dir", dest="log_dir", default=".")
	parser.add_argument(
		"--queue",
		default="cloud_migration",
		help="queue for the `worker` stage; ERP workers for A1 are started with short/default/long",
	)
	parser.add_argument("--campaign", default=None, help="`probe`: campaign to witness load against")
	parser.add_argument("--baseline", default=None, help="`a1`: idle probe artifact")
	parser.add_argument("--loaded", default=None, help="`a1`: loaded probe artifact")
	parser.add_argument(
		"--job-log",
		dest="job_log",
		action="append",
		default=None,
		help="`probe`: job log to read (repeatable — migration and ERP workers write different logs)",
	)
	# `--path`, not `--out`: `--out` is where a stage's *result* is written, and the sampler
	# would otherwise overwrite its own JSONL with its summary.
	parser.add_argument("--path", default=None)
	parser.add_argument("--out", default=None)
	parser.add_argument(
		"--idle-baseline",
		dest="idle_baseline",
		action="store_true",
		help="declare that this window is SUPPOSED to have no application processes; only "
		"then may a report show zero matched processes",
	)
	args = parser.parse_args(argv)

	if args.title:
		globals()["CAMPAIGN_TITLE"] = args.title

	if args.min_bytes or args.max_bytes:
		PAYLOAD_BAND["min"] = args.min_bytes or PAYLOAD_BAND["min"]
		PAYLOAD_BAND["max"] = args.max_bytes or PAYLOAD_BAND["max"]
		if PAYLOAD_BAND["min"] > PAYLOAD_BAND["max"]:
			raise SystemExit(f"--min-bytes {PAYLOAD_BAND['min']} exceeds --max-bytes {PAYLOAD_BAND['max']}")

	if args.stage in NO_CONNECT_STAGES:
		_guard(args.site)
		frappe.init(site=args.site)
		STAGES[args.stage](args)
		return 0

	_connect(args.site)
	try:
		result = STAGES[args.stage](args)
	finally:
		frappe.db.commit()
		frappe.destroy()

	payload = json.dumps(result, indent=2, default=str)
	if args.out:
		with open(args.out, "w") as handle:
			handle.write(payload + "\n")
	print(payload)
	return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
	sys.exit(main())
