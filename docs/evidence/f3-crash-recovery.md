# F3 criterion 4 — crash/restart recovery at campaign scale

Both faults were previously **unit-level only**, recorded as unmet since P5. Both are now met,
injected into live campaigns with real RQ workers on the site's own `cloud_migration` queue.

Site `cfs-fault.local` — a disposable scratch site created for this, so the throttle and
job-count evidence on `cfs-throttle.local` is untouched.

## 4a. Redis flush — PASSED

Campaign `CFS-CAMP-0001`: 1200 objects, 156 MB, 20 batches, throttled to 6 Mbps so the
transfer window was minutes rather than seconds.

```
redis keys deleted              4
batches in flight when flushed  2
objects moved when flushed      505
seconds to resume progress      20.1
final                           1200 Verified / 20 batches Verified
converged after fault           572.2 s
passed                          true
```

Key patterns deleted: `rq:queue:home-user-v15:cloud_migration`,
`rq:*:home-user-v15:cloud_migration*`, `rq:job:cfs::CFS-CAMP-0001*`, `rq:clean_registries:*`.

**Scoped deletion, not FLUSHDB.** The queue Redis is shared with other sites on this bench and a
rehearsal is not entitled to discard work that is not its own. The campaign's experience is
identical: its queue, job hashes and registries are gone and its dispatched batches will never be
delivered to anybody.

## 4b. Worker restart (the `bench restart` shape) — PASSED

Campaign `CFS-CAMP-0007`: 2992 objects, 60 batches, unthrottled so the fault plan's 55%
threshold was crossed inside the fault timeout.

```
workers killed                  [119258, 119259]
workers started                 [121309, 121310]
batches in flight at restart    2
objects moved at restart        1728
seconds to resume progress      10.1
objects moved after             1928
final                           2992 Verified / 60 batches Verified
converged after fault           75.4 s
passed                          true
```

## Three failed attempts first, and why they are worth recording

None was a product defect; all three were harness misuse, and each produced an honest report
rather than a false pass.

1. **"corpus never reached 1331"** — the fault waits for 55% of the corpus before injecting, and
   a 6 Mbps throttle could not get there inside FAULT_TIMEOUT. The stage reported what it
   observed and did not claim a pass.
2. **The harness cannot restart workers it did not start.** `fault_worker_restart` terminates
   `harness.worker_procs`; workers launched by hand are invisible to it.
3. **`--title` did not reach the child processes.** The harness spawns its workers and dispatcher
   as separate processes, so a title set on the parent's globals never arrived. The fault watched
   one campaign while its own dispatcher drove another — reporting a true and completely
   misleading "corpus never reached N". Fixed by forwarding the flag in `Harness._spawn`.

## A product guard blocked the third attempt, correctly

The real cause was in the dispatcher's log rather than the fault's output:

    ValidationError: Campaign CFS-CAMP-0002 is already active; only one campaign may run at a time.

An earlier campaign had been left Running. It was cleared through the product's own API rather
than by editing rows: `stop()` -> two batches still claimed by killed workers ->
`recover_stale_batches()` released both -> `finalize_stop()` -> `Stopped`.

**That is the orphaned-claim recovery path working on real orphans**, produced by accident, which
is better evidence than a staged test of the same thing.
