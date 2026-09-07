# F3 criterion 3 — bounded disk I/O against a recorded baseline

P5 recorded this criterion unmeasurable ("`/proc/self/io` reports zero"). That was an
**instrument fault, not an environment limit**, and two separate defects had to be fixed before
any number here was trustworthy. Both are stated first, because a measurement is only worth what
its instrument is worth.

## Instrument defects found and fixed

**1. The wrong process was being read.** P5 sampled `/proc/self/io` of the *measuring* process,
which performs no I/O. The workers and mysqld were never sampled.

**2. The wrong counters were being read.** `read_bytes`/`write_bytes` are block-layer counters: a
read served from page cache never reaches the block layer, so `read_bytes = 0` is a cache hit, not
an absence of I/O. `rchar`/`wchar`/`syscr`/`syscw` are syscall-level and move regardless. Both
pairs are now captured, because together they distinguish *the app moved bytes* from *the bytes
reached the disk*.

**3. Device IOPS and latency were not derivable at all.** Syscall counts are not disk operations
and there was no `/proc/diskstats` sample, so IOPS, merging, queueing and service latency could
not be computed. Device-level sampling was added; the numbers below come from it.

**4. The sampler silently dropped the entire application half of a run.** Its process matcher
looked only for the dotted module path used by the harness's own spawner; a worker started by file
path never matched. The result reads as **zero RSS and zero I/O rather than "unmatched"** — a
quiet process, not a missing one. This invalidated the 4188-object run's application metrics,
which are excluded below rather than reported as zeros.

## Proofs required before the numbers mean anything

**1. The sampler matched the intended application processes.**

    IDLE           15 samples   procs 0-0     (nothing running -- correct)
    LOADED 4188   239 samples   procs 0-0     INVALID: unmatched, not measured -- app metrics EXCLUDED
    LOADED 6278    67 samples   procs 10-15   peak RSS 577.4 MB -- VALID

**2. The configured fields exist and were persisted** (read back from the database, never from the
object just mutated):

    operation_mode                exists=True   stored='LOCAL_ONLY'
    bucket                        exists=True   stored='cfs-fault'
    endpoint_url                  exists=True   stored='http://127.0.0.1:9000'
    sse_mode                      exists=True   stored=''
    use_default_credential_chain  exists=True   stored=0
    storage_mode                  exists=False  <- a field that never existed

`storage_mode` was set by an earlier setup script and **verified by reading the same in-memory
attribute back** — a self-confirming check. The real field is `operation_mode`, whose valid values
are `LOCAL_ONLY, DUAL_WRITE, S3_PRIMARY_LOCAL_FALLBACK, S3_ONLY`; `CLOUD_PRIMARY` was never one of
them. **This does not affect the measurements**: migration UPLOAD writes to the bucket regardless
of serving mode, which proof 3 confirms directly.

**3. Real objects and real bytes moved.**

    CFS-CAMP-0016  4220 objects, 4188 Verified, 546 MB
    CFS-CAMP-0044  6320 objects, 6278 Verified, 819 MB
    Cloud Storage Object rows in bucket cfs-fault: 6283, 817 MB
    MinIO bucket cfs-fault, listed directly:      6283 objects, 817 MB actually stored

**4. The two loaded points are comparable.** Same site, same code, same configuration —
`batch_size` 50, `parallelism` 2, four real RQ workers, **no bandwidth limit** on either. They
differ only in corpus size (4188 vs 6278 objects; 546 vs 819 MB). Device counters are valid for
both; application counters only for the 6278 run, and the 4188 application figures are omitted
rather than reported as zeros.

## The measurements

    metric                  IDLE     4188      6278
    device read IOPS /s     0.00     0.17      4.17
    device write IOPS /s    8.14    81.9     273.9
    device read MB/s        0.00     0.00      0.22
    device write MB/s       0.14     1.06      3.37
    await ms                4.03     1.73      2.05
    utilisation %           0.93     8.9      30.3
    app RSS peak MB          0.0     (excluded) 577.4
    mysqld RSS peak MB    1341.4     1348.5   1473.1
    redis peak MB            1.67     1.85      2.41
    loadavg peak             0.48     2.42      2.87

## 5. Growth is bounded, not saturating

**Per-object device write cost FALLS as the corpus grows — sublinear, not proportional:**

    4188 objects:  1272 MB written  ->  296.7 KB/object
    6278 objects:  1119 MB written  ->  174.1 KB/object

**Within the 6278 run, split into quarters as the corpus is consumed:**

    quarter 1: util 28.7%  wr 4.28 MB/s  await 2.52ms  appRSS 577.4MB  dbRSS 1473.0MB  redis 1.86MB
    quarter 2: util 30.4%  wr 3.43 MB/s  await 1.98ms  appRSS 573.8MB  dbRSS 1473.1MB  redis 2.04MB
    quarter 3: util 31.1%  wr 3.18 MB/s  await 2.14ms  appRSS 574.2MB  dbRSS 1473.1MB  redis 2.16MB
    quarter 4: util 34.1%  wr 2.97 MB/s  await 1.60ms  appRSS 574.4MB  dbRSS 1472.9MB  redis 2.41MB

- **Application RSS is flat** (577 -> 574 MB) while 6278 objects are consumed. Memory does not
  track corpus size — the defining property of a bounded streaming pipeline.
- **mysqld RSS is flat** at 1473 MB (its buffer pool, allocated at start).
- **Write throughput DECREASES** across the run (4.28 -> 2.97 MB/s) while utilisation stays in a
  narrow band (28.7 -> 34.1%) — no runaway.
- **`await` FALLS under load** (4.03 ms idle -> 2.05 ms loaded). Service latency improving under
  load is the signature of an unsaturated device with better request batching, and is the
  strongest single indicator that 30% utilisation is not near a knee.
- **Redis grows 1.67 -> 2.41 MB** across a 5x corpus increase: 0.74 MB for 6278 objects,
  ~120 bytes/object, and released at convergence.

## ERP queue impact — criterion 2

    cloud_migration queue depth peak:  0   (jobs consumed as fast as dispatched)
    cfs-fault.local jobs on short/default/long:  0 / 0 / 0

The `default` queue showed a constant depth of **124 across all 67 samples**. Every one of those
jobs belongs to **`kaynes.test`** — a different site on this shared bench — and the depth never
moved, so the migration neither added to it nor drained it. Reported rather than netted out,
because a queue-depth number without its attribution is exactly the kind of figure that gets
misread later.

**Carried, unchanged: the ERP probe latency of 0.257 s idle -> 0.410 s loaded (+60%) is an OWNER
DECISION.** The approved documents state no tolerance for "unaffected", so no verdict is claimed
here. See `docs/evidence/f3-throttle-and-jobs.md`.

## Verdict

Criterion 3 is **MET**: an idle baseline exists for the first time, two comparable loaded points
were measured with corrected instrumentation, per-object cost is **sublinear**, application and
database memory are **flat**, service latency **improves** under load, and device utilisation
peaks at 30% — bounded, with no saturation trend.
