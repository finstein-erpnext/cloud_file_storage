# Backup & Restore Runbook (P6)

The app takes its own verified backups to an **isolated bucket** and manages that bucket's
lifecycle rules. It never restores a database: `bench restore` drops and recreates the
schema, so it stays on the CLI, in front of a human, behind a checksum.

Two rules run through everything below.

* **A backup is not a backup until it has been verified.** The job computes the artifact's
  SHA256 locally, uploads it, and reads it back from the bucket before writing `Success`.
  An artifact above the multipart threshold is re-downloaded and re-hashed, because a
  multipart object's `ChecksumSHA256` is a checksum-of-checksums and cannot be compared to
  the whole-object digest.
* **Nothing is deleted to tidy up a failure.** A verification failure keeps the remote
  artifact, marks the run `Failed` and alerts. The bytes may be perfectly good ones whose
  HEAD raced a replication lag, and even genuinely corrupt bytes are evidence.

---

## 0. Before you enable it

| Check | Why |
|---|---|
| A **separate** bucket from the attachment bucket | Backup lifecycle rules move objects to archive classes; a live attachment in GLACIER answers a download with `403 InvalidObjectState`. The validator refuses a shared bucket outright. |
| Block Public Access ON, SSE on the bucket | The site config artifact carries the database password and the backup encryption key. |
| Versioning ON, `NoncurrentVersionExpiration >= restore_window_days` | An artifact deleted out of band is only recoverable from a noncurrent version. |
| `keep_backups_for_hours` on the site | Local artifacts are the app's responsibility to create, core's to expire. This app never deletes one. |
| Bench crontab backups | `bench backup-all-sites` stays as it is. After the cloud cutover, remove any `--with-files` entries: attachment bytes live in the object store and ride in the database dump as links. |

Enable at **Cloud Backup Settings**. `frequency`, `backup_hour` and the `include_*`
checkboxes decide what a run produces; the retention section decides what the bucket does
with it afterwards.

---

## 1. What a run does

```
hourly scheduler tick
  └─ run_scheduled_backup()            O(ms) gate; returns immediately unless due
       └─ enqueue take_cloud_backup    queue "long", job_id "cfs::backup", deduplicated
            ├─ new_backup(force=True)  a dump belonging to THIS run
            ├─ mtime >= job start      asserted on EVERY artifact, before any upload (A28)
            ├─ upload_file             explicit TransferConfig, ChecksumAlgorithm SHA256,
            │                          SSE from settings, never an ACL key
            ├─ head_object(ChecksumMode="ENABLED")
            │     ContentLength == local size          — always
            │     ChecksumSHA256 == local digest       — below the multipart threshold
            │     streamed re-GET + re-hash            — at or above it (A5)
            └─ Cloud Storage Backup Log row: keys, sizes, SHA256 manifest
```

Keys are `<prefix>/<frequency>/<dump timestamp>/<artifact filename>`, with `{site}` in the
prefix expanded. The frequency segment is what lets the lifecycle rules give hourly and
daily artifacts different retentions; a manual run lands under `manual/`, which has an
expiration rule of its own so it cannot accumulate silently.

Manual run: `bench --site <site> cfs-backup-now` (or **Backup Now** on the settings form).

**A `Failed` row is not a transient.** Read the `error` column. A verification failure
means the bucket holds bytes that are not the bytes we sent; the artifact is still there,
under the key in the log row, waiting to be looked at.

---

## 2. Lifecycle policy

`bench --site <site> cfs-lifecycle-preview` prints exactly what
`cfs-lifecycle-apply --confirm "APPLY LIFECYCLE"` would write:

* **generated rules** — all `cfs-`-prefixed, all `Filter.Prefix`-scoped to this site's
  backup prefix. An archive transition on day `hot_retention_days`, an expiration per
  frequency prefix, one `NoncurrentVersionExpiration` and one
  `AbortIncompleteMultipartUpload` over the whole prefix.
* **preserved rules** — everything in the bucket whose ID does not start `cfs-`, carried
  through untouched. `put_bucket_lifecycle_configuration` replaces the entire
  configuration, so a generator that did not merge would silently delete an operator's
  compliance hold.
* **dropped rules** — `cfs-` rules currently in the bucket that the new policy no longer
  contains. **Read this list before confirming.** It is the only thing the apply removes.
* **overlapping rules** — foreign rules whose prefix overlaps ours. S3 applies the union
  of every matching rule and the earliest expiration wins, so a foreign
  `Expiration.Days: 1` on the bucket root silently overrides a year of retention.
* **economics** — the cost arithmetic, rendered. An `error` blocks the apply; the commonest
  is archiving on day 30 with a 60-day retention, which pays GLACIER's full 90-day minimum
  for storage you deleted on day 60.

The apply is System Manager only, requires the phrase `APPLY LIFECYCLE` typed **server
side**, and writes an audit row with the policy hash and the dropped rule IDs.

> Archive storage classes are for backup artifacts only. `assert_synchronous_retrieval`
> refuses one on the attachment bucket, and the apply refuses outright when the two buckets
> are the same bucket.

---

## 3. Restore

### 3.1 Pick the artifact

```
bench --site <site> cfs-backup-list --limit 20
```

Each row carries its keys and the SHA256 manifest recorded at upload time. Only a
`Success` row is a source of truth: a `Failed` row's manifest describes bytes that were
never confirmed to have arrived.

### 3.2 Thaw it, if it has been archived

An artifact past `hot_retention_days` is in GLACIER or DEEP_ARCHIVE and cannot be
downloaded until it is restored to warm storage.

```python
# bench --site <site> console
from cloud_file_storage.backup import restore
restore.initiate_restore("<key>", days=3, tier="Standard")   # Expedited | Standard | Bulk
restore.restore_status("<key>")                              # {"ongoing": …, "ready": …}
```

DEEP_ARCHIVE at `Standard` takes 9–48 hours. Plan the recovery-time objective around that,
not around the download.

### 3.3 Download

```python
restore.download_url("<key>")     # short-TTL presigned GET, System Manager only
```

### 3.4 **Verify before you restore — every time**

```
bench --site <site> cfs-backup-verify /path/to/20260816_010000-<site>-database.sql.gz
```

The command re-hashes the local file and compares it against the SHA256 the Backup Log
recorded when the artifact was uploaded. It exits non-zero and prints `DO NOT restore this
artifact` on any mismatch, and it never deletes the file it rejected.

**This step is not optional and it is not a formality.** Restoring a truncated or corrupt
artifact replaces a working database with a broken one, and the moment `bench restore`
starts, the database you would have compared against no longer exists. A mismatch means:
stop, keep the file, and look at the `Failed` rows and the bucket.

### 3.5 Restore

```
bench --site <site> --force restore /path/to/<database>.sql.gz \
    [--db-root-username root --db-root-password ***]
```

Then re-check `Cloud Storage Settings`: a restored database carries the bucket
configuration it had at dump time, including `operation_mode`.

---

## 4. Retention, the restore window and the object grace period (A27)

Three numbers have to agree, and the app refuses configurations where they do not:

| Setting | Where | Meaning |
|---|---|---|
| `restore_window_days` | Cloud Storage Settings | How long after a delete an attachment is promised to be recoverable. |
| `object_delete_grace_days` | Cloud Storage Settings | How long deferred GC waits before physically deleting an unreferenced object. Validated `>= restore_window_days` — a shorter grace means the object is gone before the window it promises has expired. |
| `NoncurrentVersionExpiration` | The **attachment** bucket | How long a version deleted outside this app survives. `test_connection` reports a red step when versioning is off, or when this is shorter than the restore window. |

Shipped defaults are 30 and 30 (supersession S1 raised the grace from 7 for exactly this
reason), so a fresh install satisfies the coupling. The bucket half cannot be validated on
save — it is someone else's bucket — so it is a hard warning on the connection test.

---

## 5. Failure modes

| Symptom | What it means | What to do |
|---|---|---|
| `Failed` with a size or SHA256 mismatch | The bucket does not hold the bytes we sent. | The remote artifact is kept. Inspect it. Do not restore from it. |
| `BackupFreshnessError` | `new_backup` returned a dump older than the job. | Nothing was uploaded. Check disk space and `private/backups` for a stale artifact at the path the generator wanted. |
| `Success` rows stop appearing | The scheduler is paused, or the `long` queue is stuck. | `bench doctor`; `last_backup_on` on the settings form is the age to watch. |
| Orphan multipart uploads billed | A worker was killed mid-upload. | The `cfs-abort-mpu` rule cleans them up; the managed transfer also aborts its own on a normal failure. |
| Lifecycle apply refused | Bucket not isolated, phrase not typed, or an economics error. | The message names which. None of the three is worth working around. |
| An artifact will not thaw | It is already in a synchronous class. | `restore_status` reports the storage class; download it directly. |
