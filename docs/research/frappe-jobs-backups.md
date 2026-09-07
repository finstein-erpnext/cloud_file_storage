> **STATUS (P0.5): HISTORICAL EVIDENCE.** Captured 2026-08-14 during planning; verified
> file:line ground truth at capture time. Where later decisions supersede content here,
> `docs/PLAN.md` / `docs/adr/` win. Known supersessions: P0 is COMPLETE (the "bench is
> currently broken" state described in rename-and-bench-reality §0 was RESOLVED by the
> ops-only .pth repair — see PROGRESS.md); the CI matrix/floor is {v15.16.0, v15.93.0,
> version-15} with `frappe>=15.16.0,<16.0.0` (A34), not the v15.0.0-based examples.

Report follows.

# FRAPPE v15 BENCH RESEARCH — cloud_file_storage architecture inputs

Environment: `/home/user/v15/apps/frappe` @ `v15.93.0` (commit `405aa71f`, 2025-12-23). Venv: rq `1.15.1`, redis-py `4.5.5`, boto3/botocore `1.34.162`. bench (host tool) `frappe_bench-5.25.9` at `/home/user/.local/lib/python3.10/site-packages/bench`.

---

## 1. QUEUES

### 1.1 `enqueue()` signature and semantics
`/home/user/v15/apps/frappe/frappe/utils/background_jobs.py:59-75`:

```python
def enqueue(method, queue="default", timeout=None, event=None, is_async=True,
            job_name=None, now=False, enqueue_after_commit=False, *,
            on_success=None, on_failure=None, at_front=False,
            job_id=None, deduplicate=False, **kwargs) -> Job | Any
```

Semantics, in execution order:
- `deduplicate` branch (`:93-103`) — requires `job_id`, else `frappe.throw`.
- `job_id = create_job_id(job_id)` (`:108`) namespaces to `f"{frappe.local.site}::{job_id}"` (`:559-564`); if no `job_id` a `uuid4()` is generated, so every job always has an id.
- `job_name` is deprecated (`:110-111`); `is_async=False` outside tests is deprecated (`:113-116`).
- `now=True` (or `is_async=False` outside tests) → `frappe.call(method, **kwargs)` **inline, synchronously** (`:118-120`).
- Redis unreachable + `frappe.local.flags.in_migrate` → falls back to synchronous execution instead of raising (`:122-130`). Relevant: during `bench migrate`, any enqueue in patches runs inline.
- `_check_queue_size(q)` (`:132`) — see 1.4.
- Timeout default resolution: `timeout = get_queues_timeout().get(queue) or 300` (`:134-135`).
- **All `**kwargs` are forwarded to the target method**, not to RQ. RQ-native options are *not* reachable (no `retry=`, no `depends_on=`, no `result_ttl=` override, no `description=`) — see `queue_args` and `enqueue_call()` at `:143-166`. The only RQ knobs frappe exposes are `timeout`, `at_front`, `job_id`, `on_success`, `on_failure`.
- The actual RQ payload is always the same function `execute_job` with `kwargs=queue_args` where `queue_args = {"site","user","method","event","job_name","is_async","kwargs"}` (`:143-151`, `:156-166`).
- `on_failure` defaults to `truncate_failed_registry` (`:153`) — overriding `on_failure` silently disables failed-registry trimming.
- `failure_ttl = conf.rq_job_failure_ttl or 7 days`, `result_ttl = conf.rq_results_ttl or 600s` (`:163-164`, constants at `:32-34`).
- `enqueue_after_commit=True` → the enqueue closure is appended to `frappe.db.after_commit` and **`None` is returned** (`:168-170`). Any code that needs the `Job` object back must not use this flag.
- Public wrapper `frappe.enqueue` at `frappe/__init__.py:2235-2247` (thin passthrough).

### 1.2 Can `queue` be an arbitrary new name like `cloud_migration`? — Yes, but only via `common_site_config.workers`
The full queue namespace is defined by `get_queues_timeout()` (`background_jobs.py:41-56`):

```python
@lru_cache
def get_queues_timeout():
    custom_workers_config = frappe.get_conf().get("workers", {})
    return {"short": 300, "default": 300, "long": 1500,
            **{w: c.get("timeout", 300) for w, c in custom_workers_config.items()}}
```

Enforcement path (this is the code that decides validity, both producer- and consumer-side):
- `get_queue(qtype)` → `validate_queue(qtype)` → `frappe.throw(_("Queue should be one of {0}"))` if not in `get_queues_timeout()` (`:456-459`, `:462-467`).
- `get_queue_list()` validates every `--queue` name a worker is started with (`:423-435`).
- Physical Redis key name is namespaced: `generate_qname(qtype) -> f"{get_bench_id()}:{qtype}"` (`:532-540`); `get_bench_id()` = `conf.bench_id` or bench path with `/`→`-` (`frappe/utils/__init__.py:559-560`) — here it resolves to `home-user-v15`. So the queue key would be `home-user-v15:cloud_migration`.
- `is_queue_accessible()` (`:542-546`) and `get_queues()` (`:526-530`) filter Redis queues to only the configured set — a queue not in `workers` is invisible to `truncate_failed_registry` and to monitoring.

**Consequence for the plan:** `cloud_migration` requires a `common_site_config.json` edit:
```json
"workers": { "cloud_migration": { "timeout": 3600, "background_workers": 2 } }
```
Note `@lru_cache` on `get_queues_timeout` (`:41`) means **every web/worker process must be restarted** after adding the key; the app cannot self-register the queue at runtime. This is a hard deployment prerequisite the migration engine must degrade gracefully around (e.g. fall back to `long` with a clear System-Health warning if `"cloud_migration" not in get_queue_list()`).

### 1.3 How workers attach to queues
- CLI: `frappe/commands/scheduler.py:184-211` — `bench worker [--queue a,b] [--quiet] [-u/--rq-username] [-p/--rq-password] [--burst] [--strategy round_robin|random]`. Also `bench worker-pool --queue X --num-workers N` (`:214-228`, EXPERIMENTAL per `background_jobs.py:330-337`).
- `start_worker()` (`background_jobs.py:287-328`): splits comma string → `get_queue_list(queue, build_queue_name=True)` → plain `rq.Worker(queues, connection=...)` → `worker.work(dequeue_strategy=strategy)`. **No `--queue` ⇒ consumes ALL queues** in dict order: `short, default, long, <custom workers…>`. With `DequeueStrategy.DEFAULT` (rq `worker.py:94-97`, `queue.py:1325-1340`) this is *strict priority order* — a custom queue registered via `workers` lands **last**, i.e. lowest priority, for any generic `bench worker`.
- `set_niceness()` (`:588-604`) applies `os.nice(+10)` (`background_process_niceness` overridable) to all workers.
- Supervisor generation (`/home/user/.local/lib/python3.10/site-packages/bench/config/templates/supervisor.conf:76-90`) emits a dedicated program per `workers` entry: `command=bench worker --queue {{worker_name}}`, `numprocs={{ worker_details["background_workers"] or background_workers }}`, `stopwaitsecs={{ worker_details["timeout"] }}`, `killasgroup=true`. Procfile equivalent at `bench/config/templates/Procfile:18-20` (`worker_{{name}}: bench worker --queue {{name}}`). `bench/config/supervisor.py:57-60` reads `background_workers` and `workers` from common_site_config.

### 1.4 Queue-size guard
`_check_queue_size` (`:621-638`) throws `frappe.QueueOverloaded` when `q.count >= cint(frappe.conf.max_queued_jobs)`. Note the module constant `MAX_QUEUED_JOBS = 500` (`:36`) is **not used** by the check — with no `max_queued_jobs` in site config (absent here) the guard is disabled entirely. If it were enabled, a naive per-file enqueue loop would start throwing mid-migration; another argument for the batched design.

### 1.5 This bench's actual config

`/home/user/v15/Procfile` (verbatim structure):
- `redis_cache: redis-server config/redis_cache.conf`
- `redis_queue: redis-server config/redis_queue.conf`
- `web: bench serve --port 8001` (a debugpy/gunicorn `web:` line is present but commented out, lines ~5-11)
- `socketio: <node v18.12.0> apps/frappe/socketio.js`
- `watch: bench watch`
- `schedule: bench schedule`
- `worker: bench worker 1>> logs/worker.log 2>> logs/worker.error.log`

**There is exactly ONE worker process and it has no `--queue` filter** → it consumes short→default→long in strict priority. There is **no `worker_*` line**, i.e. no custom queue is configured today.

`/home/user/v15/sites/common_site_config.json` — structural keys only (secrets redacted):
`background_workers=1`, `default_site="finstein.erp"`, `developer_mode=1`, `file_watcher_port=6788`, `frappe_user="user"`, `gunicorn_workers=25`, `live_reload=true`, `maintenance_mode=0`, `pause_scheduler=0`, `rebase_on_pull=false`, `redis_cache="redis://127.0.0.1:13001"`, `redis_queue="redis://127.0.0.1:11001"`, `redis_socketio="redis://127.0.0.1:13001"`, `restart_supervisor_on_update=false`, `restart_systemd_on_update=false`, `serve_default_site=true`, `server_script_enabled=1`, `shallow_clone=true`, `socketio_port=9001`, `use_redis_auth=<REDACTED — boolean, currently falsey>`, `webserver_port=8001`.
**Absent (all significant):** `workers`, `max_queued_jobs`, `use_rq_auth`, `rq_job_failure_ttl`, `rq_results_ttl`, `background_process_niceness`, `scheduler_interval`, `keep_backups_for_hours`, `backup`, `backup_path`, `max_file_size`, `bench_id`.

---

## 2. JOB CONTROL

### 2.1 Deduplication
`background_jobs.py:93-105`: with `deduplicate=True` + `job_id`, `get_job(job_id)` is fetched; if status ∈ {`QUEUED`,`STARTED`} the enqueue is **skipped and `None` returned** (logged as error, `:98`); if the job exists in any other state it is `job.delete()`d first (comment cites rq#793 arg-reuse bug).
Helpers: `is_job_enqueued(job_id)` → status ∈ {QUEUED, STARTED} (`:567-568`); `get_job_status` (`:571-575`); `get_job` → `Job.fetch(create_job_id(job_id))`, returns `None` on `NoSuchJobError` (`:578-583`).

Caveats for an idempotent migration engine:
- Dedup window covers QUEUED/STARTED only — **not** `DEFERRED`, `SCHEDULED`, or `FAILED`. A job that failed and is still in FailedJobRegistry will be *deleted and re-queued*, silently.
- Job keys are global to the Redis instance (not per-queue): `Job.fetch` by id, so `{site}::{job_id}` must be unique bench-wide. Canonical pattern in core: `ScheduledJobType.rq_job_id = f"scheduled_job::{self.method}"` (`frappe/core/doctype/scheduled_job_type/scheduled_job_type.py:97-100`) checked via `is_job_enqueued` (`:94-95`) before `enqueue(..., job_id=self.rq_job_id)` (`:71-87`).
- Since a finished job's hash expires after `result_ttl` (600 s default, `:34`/`:164`), dedup gives **no durable idempotency**. The migration engine must keep its own phase/state rows in MariaDB (per-batch cursor + phase + attempt count) and treat `job_id` dedup purely as a "don't double-enqueue right now" guard.

### 2.2 `at_front`
`at_front: bool = False` (`:71`) is passed straight into `q.enqueue_call(..., at_front=at_front)` (`:162`) → LPUSH instead of RPUSH. Useful for CANCEL/abort control jobs on the migration queue.

### 2.3 Retries
- **No RQ-native retry**: `enqueue_call` (`:156-166`) never passes `retry=Retry(...)`, and `**kwargs` go to the target method, so `Retry` cannot be injected through `frappe.enqueue`. `job.retries_left` is therefore always None → rq's abandoned-job requeue path (`rq/registry.py:243-247`) never fires for frappe jobs.
- **Frappe-level retry** lives in `execute_job` (`:227-242`): on `frappe.db.InternalError` or `frappe.RetryBackgroundJobError` (`frappe/exceptions.py:200`), and only when `frappe.db.is_deadlocked(e) or frappe.db.is_timedout(e)` or the error is explicitly `RetryBackgroundJobError`, it rolls back, resets `frappe.job.after_job`, `frappe.destroy()`, `time.sleep(retry+1)`, and recurses with `retry+1`, max 5 attempts. Crucially this is **in-process, inside the same RQ job and the same `timeout` death-penalty window** — sleeps count against `timeout`. Any other exception → `frappe.log_error()` + re-raise → job FAILED (`:248-253`).
- Prior-art app-level retry: `s3_backup_settings.take_backups_s3(retry_count)` catches `JobTimeoutException` and re-enqueues itself up to 2 times (`.../s3_backup_settings.py:109-126`).
- Failed jobs are trimmed by `truncate_failed_registry` (`:606-618`) to `conf.rq_failed_jobs_limit or 1000` across *all accessible queues* on every failure — note it iterates `get_queues()`, so a high-failure migration will also evict unrelated failure history.

### 2.4 Per-enqueue timeout override
`timeout=` on `enqueue` wins over the queue default (`:134-135` only fills when falsy) and is passed to `q.enqueue_call(timeout=timeout)` (`:160`). Enforcement is `UnixSignalDeathPenalty` (SIGALRM) inside the work horse: `rq/worker.py:1426` `with self.death_penalty_class(timeout, JobTimeoutException, job_id=job.id)`. A hard backstop exists in the parent: if `current_job_working_time > job.timeout + 60`, the worker `kill_horse()`s (`rq/worker.py:1218-1222`). Long S3 multipart uploads must therefore either fit inside `timeout` or be split — a SIGALRM mid-`upload_part` leaves an **orphan multipart upload in S3** unless aborted in a `finally`/lifecycle rule.

### 2.5 Worker restart / Redis restart semantics (rq 1.15.1)
- **SIGTERM (warm shutdown)**: `request_stop` (`rq/worker.py:981-993`) re-arms SIGINT/SIGTERM to `request_force_stop`, then `_shutdown()` (`:998-1012`) — if BUSY it sets `_stop_requested`, records the shutdown date, and **lets the current job finish**, then exits. A second signal → `request_force_stop` (`:954-978`) kills the horse and `raise SystemExit()`. The work horse itself resets SIGTERM to `SIG_DFL` (`:1326`).
- Supervisor `stopwaitsecs` is set to the queue timeout for custom workers (`supervisor.conf:86`) — i.e. `stopwaitsecs == workers.cloud_migration.timeout`; if a job legitimately runs to the full timeout, supervisor may SIGKILL right at the boundary. Keep per-job wall time comfortably under the configured timeout.
- **Hard kill / worker crash**: `monitor_work_horse` (`rq/worker.py:1196-1261`) detects a non-`EX_OK` exit and, if job status ∉ {FINISHED, FAILED}, moves it to FailedJobRegistry with "Work-horse terminated unexpectedly". If the *parent* dies too, the job is stranded in `StartedJobRegistry` until some worker's periodic `clean_registries()` (`rq/worker.py:309-320`, one worker wins `queue.acquire_maintenance_lock()`) calls `StartedJobRegistry.cleanup` (`rq/registry.py:215-263`), which moves expired entries to FailedJobRegistry as `AbandonedJobError` (and would requeue only if `retries_left`, which frappe never sets). **Net: a killed migration batch is never automatically retried — the engine must reconcile from its own DB state.**
- **Redis restart**: `redis_queue.conf` (`/home/user/v15/config/redis_queue.conf`) sets `dbfilename redis_queue.rdb` with **no `save` directive and no `appendonly`**, so default redis.conf save rules apply at best and any RDB-less start loses all queued jobs and registries. Producer-side reconnect is retried 5×1 s on `BusyLoadingError`/`ConnectionError` via tenacity (`background_jobs.py:470-476`), but in-flight jobs are simply lost. Again: **DB-backed batch state is mandatory; Redis is a dispatch hint only.**

### 2.6 Job lifecycle hooks
`execute_job` (`:193-268`) runs `frappe.get_hooks("before_job")` before (`:221-222`) and `after_job` after, in `finally`, re-initialising the site if needed (`:259-268`), plus `frappe.local.job.after_job` `CallbackManager` (`:218`, `:265`). Core registrations: `before_job = ["frappe.recorder.record", "frappe.monitor.start"]`, `after_job = ["frappe.recorder.dump", "frappe.monitor.stop", "frappe.utils.file_lock.release_document_locks"]` (`frappe/hooks.py:448-465`). `frappe.local.job` exposes `site/method/job_name/kwargs/user` — usable for migration telemetry without touching RQ internals. Commit/rollback are automatic: commit on success (`:256`), rollback+`log_error`+commit on exception (`:248-253`).

---

## 3. LOCKING / THROTTLING PRIMITIVES

- **Strong cross-process lock (use this for migration phase mutexes)**: `frappe/utils/synchronization.py:17-49` — `@contextmanager filelock(lock_name, *, timeout=30, is_global=False)` built on `filelock.FileLock`. Site scope → `sites/<site>/locks/<name>.lock`; `is_global=True` → `<bench>/config/<name>.lock`. On `Timeout` it `frappe.log_error`s and raises `frappe.utils.file_lock.LockTimeoutError` with the lock path in the message. Note: file-based ⇒ **single-host only**; a multi-server bench needs a Redis/DB lock instead.
- **Weak lock (do not use for correctness)**: `frappe/utils/file_lock.py:1-9` explicitly documents it as "weak … prone to race conditions", for background document submission only. API: `create_lock/lock_exists/lock_age/check_lock(timeout=600)/delete_lock/get_lock_path/release_document_locks` (`:25-74`).
- **Document locking**: `Document.lock(timeout=None)` (`frappe/model/document.py:1599-1620`) — signature-based lock file, auto-expiry via `DOCUMENT_LOCK_EXPIRY`, polls 1 s/iteration up to `timeout`, else `frappe.DocumentLockedError`; `unlock()` at `:1621-1625`; locks registered in `frappe.local.locked_documents` and released by the `after_job` hook. `queue_action(action, **kwargs)` (`:1572-1597`) is the built-in "lock doc then enqueue" pattern and defaults to `enqueue_after_commit=True`.
- **Scheduler singleton lock**: `FileLock` on `<bench>/config/scheduler_process` (`frappe/utils/scheduler.py:43-50`, `:57-58`, `is_schduler_process_running` `:61-73`) — the model to copy for "only one migration coordinator alive".
- **Rate limiting**: `frappe/rate_limiter.py`. Request-level `RateLimiter(limit, window)` driven by `conf.rate_limit` (`:16-20`, class `:33-102`) counts *CPU-time microseconds* per window in the cache Redis. Decorator `@rate_limit(key=None, limit=5, seconds=86400, methods="ALL", ip_based=True)` (`:105-200`) is a simple `INCR` + `SETEX` counter on `frappe.cache` — **request-scoped only** (`if not frappe.request: return fn(...)`, `:132-136`), so it is useless inside RQ jobs.
- **There is no generic semaphore / token bucket / concurrency limiter in frappe** — greps for `setnx`, `nx=True`, `Lock(`, `semaphore`, `throttle` return only the file-lock and User-doctype hits above. Bandwidth/parallelism throttling for the migration engine must be built: e.g. a fixed `numprocs` on the `cloud_migration` supervisor program (natural parallelism cap, `supervisor.conf:88`), plus a per-batch sleep/byte-budget, plus an optional Redis `INCR`-based in-flight counter using `frappe.cache` (`frappe/utils/redis_wrapper.py:27+`, note keys are auto site-prefixed by `make_key`, `:41-50`).

---

## 4. SCHEDULER

- Loop: `start_scheduler()` (`frappe/utils/scheduler.py:36-54`) — acquires the global FileLock, then forever `sleep(tick)` → `enqueue_events_for_all_sites()`. Tick = `get_scheduler_tick()` (`conf.scheduler_interval`, default 240 s; see also the `"All"` cron mapping `*/{interval//60} * * * *` in `scheduled_job_type.py:125`). Also auto-started as a daemon thread by `FrappeWorker` when using `worker-pool` (`background_jobs.py:271-284`).
- Per site: `enqueue_events_for_site` (`:92-111`) skips when `is_scheduler_inactive()` — `conf.maintenance_mode`, `conf.pause_scheduler`, `conf.disable_scheduler`, or `System Settings.enable_scheduler` unset (`:132-161`). This bench has `pause_scheduler=0`, `maintenance_mode=0`.
- `enqueue_events` (`:114-129`) loads **all** non-stopped `Scheduled Job Type` rows, `random.shuffle`s them, and calls `job_type.enqueue()`.
- `hooks.scheduler_events` → DocTypes via `sync_jobs()/insert_events()/insert_cron_jobs()` on migrate (`scheduled_job_type.py:207-232`). Supported frequencies: `All, Hourly, Hourly Long, Hourly Maintenance, Daily, Daily Long, Daily Maintenance, Weekly, Weekly Long, Monthly, Monthly Long, Cron, Yearly, Annual` (`:29-44`), plus arbitrary `cron` dict keys (`frappe/hooks.py:222-241`).
- **Queue convention**: `get_queue_name()` returns `"long"` if `"Long" in frequency or "Maintenance" in frequency` else `"default"` (`scheduled_job_type.py:181-182`). There is **no way to target a custom queue from `scheduler_events`** — a scheduled entrypoint must be a thin `default`/`long` job that immediately re-`enqueue`s onto `cloud_migration`. That thin dispatcher must be O(ms) so it never blocks the shared workers.
- Dedup is built in: `is_job_in_queue()` → `is_job_enqueued(f"scheduled_job::{method}")` (`:94-100`), so overlapping ticks don't double-run. `*_Maintenance` frequencies add a deterministic per-site 0-59 min offset (`:106-140`) — good pattern for spreading cloud-storage housekeeping.
- **Not starving default workers** — concrete constraints on this bench: one `bench worker` with no `--queue` serves short→default→long by strict priority; `background_workers=1`. Therefore (a) any long-running upload/verify work MUST NOT land on `default`/`long`, (b) `cloud_migration` must get its **own** supervisor/Procfile worker (`bench worker --queue cloud_migration`) so the generic worker's strict-priority ordering never reaches it, (c) keep `numprocs` for that program explicit via `workers.cloud_migration.background_workers` rather than inheriting `background_workers`, and (d) remember `os.nice(+10)` already de-prioritises all workers vs web (`background_jobs.py:588-604`).

---

## 5. BACKUPS

`frappe/utils/backups.py`.

- **`BackupGenerator.__init__`** (`:42-90`) takes `backup_path`, `backup_path_db`, `backup_path_files`, `backup_path_private_files`, `backup_path_conf`, `ignore_conf`, `compress_files`, `include_doctypes`, `exclude_doctypes`, `verbose`, `old_backup_metadata`, `rollback_callback`.
- **Include/exclude public+private files** — the *only* switch is `ignore_files` on `get_backup(older_than=24, ignore_files=False, force=False)` (`:164`); when false it runs `self.backup_files` for **both** `public` and `private` (`:196-200` → `:349-373`). There is **no separate public-only / private-only flag**; `backup_files` loops `for folder in ("public", "private")` and tars `sites/<site>/{public,private}/files` (`:350-352`), gzip if `compress_files` (`:354-357`), via `frappe.utils.execute_in_shell(..., low_priority=True, check_exit_code=True)`; it tolerates only `tar: file changed as we read it` (`:365-373`).
- Include/exclude apply to **DocType tables**, not files: `setup_backup_tables` (`:119-152`) merges `include_doctypes`/`exclude_doctypes` args with `frappe.conf["backup"]["includes"] / ["excludes"]` (unless `ignore_conf`), always adding `base_tables = ["__Auth","__global_search","__UserSettings"]` (`:29`), and sets `self.partial`.
- File naming (`:211-230`): `{ts}-{site_slug}[-partial]-database[-enc].sql.gz`, `-files[-enc].{tar|tgz}`, `-private-files[-enc].{tar|tgz}`, `-site_config_backup[-enc].json`.
- Encryption: if `System Settings.encrypt_backup` → `backup_encryption()` gpg-symmetric over db+public+private (`:201-202`, `:232-256`), key from `conf.backup_encryption_key`, auto-generated and written to site config (`:680-691`, key name `:31`).
- **CLI flags** (`frappe/commands/site.py:795-830` → `:845-859`): `bench backup [--with-files] [-i/--include DT,DT] [-e/--exclude DT,DT] [--backup-path] [--backup-path-db] [--backup-path-files] [--backup-path-private-files] [--backup-path-conf] [--ignore-backup-conf] [--verbose] [--compress] [--old-backup-metadata]`; always `force=True`.
- **`scheduled_backup(...)`** (`:548-585`) is a pure passthrough to **`new_backup(...)`** (`:587-628`) with identical kwargs; `new_backup` first runs `delete_temp_backups()` then `odb.get_backup(older_than, ignore_files, force=force)`.
- Retention: `delete_temp_backups(older_than=24)` uses `cint(conf.keep_backups_for_hours) or 24` and deletes everything older in `get_backup_path()` = `sites/<site>/` + `conf.backup_path or "private/backups"` (`:630-642`, `:670-672`). Downloadable-backup rotation is a separate scheduler job: `frappe.desk.page.backups.backups.delete_downloadable_backups` (registered `hourly_maintenance`, `frappe/hooks.py:236`), which groups files by prefix and keeps `System Settings.backup_limit` sets (`frappe/desk/page/backups/backups.py:52-80`; field default `3`, `system_settings.json:194-206`, validated ≥1 at `system_settings.py:182-185`).
- **Who triggers periodic backups**: *not* the frappe scheduler. `scheduler_events` contains only the offsite integrations (`hooks.py:252-254`, `266-271`, `279`). Full-site backups come from bench's crontab (`bench setup backups` → `bench backup-all-sites`, `bench/commands/setup.py:113-117`, `bench/commands/utils.py:137-141`). `frappe.installer:402-405` also takes `scheduled_backup(ignore_files=True)` before install/uninstall of an app — **relevant: installing/renaming `cloud_file_storage` triggers a DB backup**.
- **Hooks around backup: none exist.** No `before_backup`/`after_backup` hook is defined or consumed anywhere in frappe (grep over `frappe/**.py`). The only extension point is `rollback_callback` (a `CallbackManager` passed in and fed by `add_to_rollback`/`delete_if_step_fails`, `:496-522`) and wrapping `scheduled_backup`/`new_backup` yourself. Practical options for the plan: register your own `scheduler_events` job that calls `new_backup(...)` and then ships artifacts to S3/Glacier, or monkey-patch-free wrapper + `System Settings` fields of your own.

Relevant System Settings fields: `max_file_size` (`system_settings.json:81`, `:594`), `sec_backup_limit`/`backup_limit` (`:95-96`, `:194-206`), `encrypt_backup` (`:97`, `:466`).

---

## 6. PRIOR ART — `frappe/integrations/doctype/s3_backup_settings/*`

Files: `s3_backup_settings.py` (197 lines), `.json`, `.js`, `test_s3_backup_settings.py`.

DocType fields (typed block `:24-43`): `access_key_id: Data`, `secret_access_key: Password`, `bucket: Data`, `endpoint_url: Data|None`, `backup_path: Data|None`, `backup_files: Check`, `frequency: Literal["Daily","Weekly","Monthly","None"]`, `enabled: Check`, `notify_email: Data`, `send_email_for_successful_backup: Check`.

- **Credential handling** (`:55-60`, `:143-148`): `boto3.client("s3", aws_access_key_id=..., aws_secret_access_key=self.get_password("secret_access_key"), endpoint_url=... or "https://s3.amazonaws.com")`. Secret stored as a `Password` field (encrypted `__Auth`); **no IAM-role / instance-profile / STS / AWS_PROFILE support, no region parameter, no signature-version or addressing-style config** — S3-compatible endpoints work only via `endpoint_url`.
- **Validation** (`:45-76`): `head_bucket` on save, mapping `403` → "no permission", `404` → "bucket not found". Good, cheap credential smoke-test worth reusing.
- **Upload** (`:194-197`): `conn.upload_file(filename, bucket, destpath)`. This is boto3's managed transfer, so **multipart is implicit** via the default `TransferConfig` (8 MB threshold/chunk, 10 concurrent threads) — there is **no explicit `TransferConfig`, no threshold tuning, no `Callback` progress, no checksum/ETag verification, no `ExtraArgs` (no SSE, no StorageClass, no ContentType, no Metadata), and no `abort_multipart_upload` on failure**. Note `frappe/integrations/offsite_backup_utils.py:77-92` defines `get_chunk_site(file_size)` (15→200 MB by size) but it is **dead code for S3** — only Dropbox-style callers use it.
- **What it uploads** (`backup_to_s3`, `:134-191`): db dump + site_config, and if `backup_files` also private-files and public-files tarballs. Key layout: `folder = backup_path + basename(db_filename)[:15] + "/"` — i.e. the `YYYYMMDD_HHMMSS` prefix as a pseudo-directory; `destpath = os.path.join(folder, basename(filename))`.
- **New-vs-reuse logic**: `validate_file_size()` (`offsite_backup_utils.py:95-101`) sets `frappe.flags.create_new_backup = True`, then flips it to **False if the latest db backup is > 1 GB** — i.e. large sites silently re-upload a stale existing backup rather than taking a fresh one. Combined with `get_latest_backup_file()` scanning `older_than=24*30` (`:45-62`), a large site can ship a **month-old** backup and report success.
- **Scheduling** (`:90-105`, `hooks.py:252-253/266-267/279`): `take_backups_daily/weekly/monthly` are separate `daily_maintenance`/`weekly_long`/`monthly_long` entries, each re-checking `enabled` and `frequency == freq`.
- **Retry/timeout**: `take_backup()` (`:79-87`) enqueues on `queue="long", timeout=1500`; `take_backups_s3(retry_count=0)` catches `rq.timeouts.JobTimeoutException` and re-enqueues up to 2 more times (`:109-126`), else `notify()` emails the traceback (`:129-131`, `offsite_backup_utils.py:11-42`).

**Reusable patterns:** `head_bucket` preflight; `Password` field + `get_password()`; `endpoint_url` for S3-compatible providers; timestamped key prefix; enabled+frequency guard; `JobTimeoutException` self-re-enqueue; success/failure email helper.
**Shortcomings to not repeat:** no retention/rotation in the bucket **at all** (nothing ever deletes remote objects — Glacier/lifecycle must be bucket-side or app-managed); no verification of the uploaded object (no ETag/checksum/`head_object`); no `ExtraArgs` so no SSE-KMS/StorageClass — meaning **Glacier economics cannot be expressed through this code path**; no orphaned-multipart cleanup; the >1 GB stale-backup trap; boto client rebuilt per call; no region; blocking single-threaded sequential uploads inside one RQ job under a 1500 s death penalty; `generate_files_backup()` calls the **deprecated** `BackupGenerator.zip_files()` (`offsite_backup_utils.py:118` vs `backups.py:302-308`).

---

## 7. FILE SIZE / UPLOAD LIMITS

**Resolution order** — `frappe/core/api/file.py:85-91` (the canonical one, whitelisted `allow_guest=True`):
```python
cint(frappe.get_system_settings("max_file_size")) * 1024 * 1024   # System Settings, in MB
or cint(frappe.conf.get("max_file_size"))                          # site/common config, in BYTES
or 25 * 1024 * 1024                                                # 25 MB
```
Note the **unit mismatch**: System Settings is MB, site_config is bytes.

Enforcement points:
- Werkzeug hard cap per request: `frappe/app.py:199-204` — `/api/method/upload_file` gets `request.max_content_length = get_max_file_size()`; **every other endpoint** gets `cint(conf.max_file_size) or 25MB` (System Settings is *not* consulted), so custom upload endpoints inherit the raw-conf value only.
- Document-level: `File.check_max_file_size()` (`frappe/core/doctype/file/file.py:728-741`) compares `len(self._content or b"")` against `get_max_file_size()` and throws `MaxFileSizeReachedError`; called from `:682`.
- Legacy duplicate with different defaults: `frappe/utils/file_manager.py:212-223` — `conf.get("max_file_size") or 10485760` (10 MB), used by `save_file` path there.
- Boot: `bootinfo.max_file_size` (`frappe/boot.py:170-172`), so the JS uploader mirrors the same number.
- Backward-compat hook mapping: `frappe.core.doctype.file.file.get_max_file_size` → `frappe.core.api.file.get_max_file_size` (`frappe/hooks.py:399`).

**Chunked/resumable upload: none in v15 core.** `frappe/handler.py:171-231` `upload_file()` does `content = file.stream.read()` — the whole body into memory in one shot, then optional image optimisation; there are zero occurrences of `chunk` in `frappe/handler.py`, `frappe/core/api/file.py`, or `frappe/core/doctype/file/*.py`. Implication for cloud_file_storage: any >`max_file_size` or streaming path (multipart to S3, presigned direct-to-S3 PUT/POST) must be a **new endpoint** of your own, and it must set its own `request.max_content_length` because `app.py:204` will otherwise clamp it to `conf.max_file_size or 25MB`. `frappe.local.uploaded_file / uploaded_filename / uploaded_file_url` (`handler.py:223-225`) are the documented interception points already used by the s3-attachment fork.

---

## 8. REDIS TOPOLOGY

Per `sites/common_site_config.json` there are **three URLs but only two physical servers**:

| logical | URL | server |
|---|---|---|
| `redis_queue` | `redis://127.0.0.1:11001` | `config/redis_queue.conf` |
| `redis_cache` | `redis://127.0.0.1:13001` | `config/redis_cache.conf` |
| `redis_socketio` | `redis://127.0.0.1:13001` | **same instance as cache** |

`/home/user/v15/config/redis_queue.conf`: `dbfilename redis_queue.rdb`, `dir /home/user/v15/config/pids`, `bind 127.0.0.1`, `port 11001`, `aclfile /home/user/v15/config/redis_queue.acl`. **No `maxmemory`, no `maxmemory-policy`, no explicit `save`/`appendonly`.**
`/home/user/v15/config/redis_cache.conf`: `port 13001`, `maxmemory 992mb`, `maxmemory-policy allkeys-lru`, `appendonly no`, `save ""`, `aclfile redis_cache.acl`.
`Procfile` starts only `redis_cache` and `redis_queue` — there is **no third redis process**.

Connection code:
- Queue: `get_redis_conn()` (`background_jobs.py:470-516`) → `RedisQueue.get_connection()` → `redis.from_url(conf.redis_queue)` + `ping()` (`frappe/utils/redis_queue.py:18-36`), with sentinel support via `redis_queue_sentinel_enabled`. ACL auth only when `conf.use_rq_auth` (absent here) or `RQ_ADMIN_PASWORD` env (`:490-501`); `use_redis_auth` is currently falsey so the unauthenticated singleton `get_redis_connection_without_auth()` (`:518-523`) is used and cached in a module global.
- Cache: `setup_cache()` → `RedisWrapper.from_url(conf.redis_cache)` (`frappe/utils/redis_wrapper.py:296-311`), sentinel-capable.
- **Realtime rides on the QUEUE instance, not `redis_socketio`**: `frappe/realtime.py:100-115` `emit_via_redis` uses `get_redis_connection_without_auth()` and `r.publish(...)`; the node side subscribes with `get_redis_subscriber()` whose default kind is `"redis_queue"` (`frappe/node_utils.js:59-71`, `frappe/realtime/index.js:44-48`). The `redis_socketio` key is effectively vestigial in v15 (only bench references it: `bench/config/common_site_config.py:86,105,119`).

**Implications for a ~1.2 M-row / ~100 GB migration:**
1. Queue Redis has **no `maxmemory` cap** — a naive 1.2 M-job enqueue would grow unbounded (each RQ job is a hash + registry entries) and can OOM the box; job hashes also linger for `failure_ttl` = 7 days on failure. This is the strongest technical argument for the batched design (e.g. ~1200 jobs of 1000 rows) rather than per-file jobs.
2. Because realtime pub/sub shares the queue instance, flooding it degrades **live UI progress and all desk realtime**. Publish migration progress at a throttled cadence (per batch, not per file) via `frappe.publish_progress`/`publish_realtime` (`frappe/realtime.py:12-21`, `:23-83`).
3. Cache Redis is `allkeys-lru` at 992 MB with **no persistence** — never store migration state, cursors, or dedup markers there; they can be evicted at any time. Same for `frappe.cache`-based counters (use them only as best-effort throttles).
4. Queue Redis has no AOF and no configured save points → a restart can drop the entire pending queue and the started/failed registries. Combined with §2.5 (no automatic requeue), **the migration engine's source of truth must be MariaDB rows with (batch_id, phase, state, attempt, verified_at)**, and each tick must re-derive work from the DB, using `job_id`+`deduplicate` only to avoid double dispatch.
5. Both redises `bind 127.0.0.1` → single-host; combined with the file-based locks (§3) the migration coordinator is inherently single-node. Sentinel support exists in code (`redis_queue.py:20-33`, `redis_wrapper.py:296-321`) if HA is ever needed.