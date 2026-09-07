# Migration Runbook (P5)

The engine moves a live site's existing attachments into the bucket without taking the site
down. It never deletes a local file as part of that: deletion is a separate, operator-
approved phase that re-proves every safety property immediately before it moves anything.

The queue prerequisite lives in `deployment.md` and is **required** — `start_migration`
refuses without it unless you explicitly pass `--force-fallback-queue`, which puts migration
jobs on the same `long` queue the ERP uses and says so loudly in the Error Log, on the
campaign row and in the health panel.

---

## 0. Before you start

| Check | Why |
|---|---|
| `cloud_migration` queue configured **and every process restarted** | `get_queues_timeout()` is `lru_cache`d, so a config change is invisible until restart |
| A recent, verified database backup | the snapshot tables are droppable; VERIFY's `tabFile` link writes are not |
| Free space on the **database** volume | ~1.2M objects + ~1.2M refs is a real InnoDB cost — measure it, do not assume it |
| `innodb_file_per_table` ON | a shared tablespace never returns the space a snapshot purge frees |
| Operation mode | UPLOAD/VERIFY run in any mode; CLEANUP needs S3_PRIMARY_LOCAL_FALLBACK or S3_ONLY |

`bench --site <site> cfs-migrate-preflight --campaign <name>` measures all of this and
projects it to production scale. A production-scale campaign (≥100,000 objects) is
**refused** if the thresholds are not met; a smaller one records the numbers and proceeds.

---

## 1. The campaign lifecycle

```
Draft ─analyze─▶ Analyzing ─▶ Analyzed ─plan─▶ Planned ─start─▶ Running
Running ⇄ Paused                     Running ─stop─▶ Stopping ─▶ Stopped
Running ─approve_cleanup + start_cleanup─▶ Cleanup Running ─▶ Completed
```

```bash
bench --site S cfs-migrate-analyze --new "Attachment migration 2026-08"
bench --site S cfs-migrate-plan     --campaign CFS-CAMP-0001
bench --site S cfs-migrate-preflight --campaign CFS-CAMP-0001
bench --site S cfs-migrate-start    --campaign CFS-CAMP-0001
bench --site S cfs-migrate-status   --campaign CFS-CAMP-0001 --watch
```

Only one campaign may be active at a time; the rule is enforced under a row lock, not only
in the form.

**What the phases do.**

* **ANALYZE** — a keyset scan of `tabFile`, a streaming walk of both file trees, and a
  twelve-step classification. It writes only migration rows; it never touches `tabFile`.
  Resumable: a crash resumes from the scan cursor and the classify step.
* **UPLOAD** — one streamed hash pass per file and one checksummed PUT. It has **no
  deletion path at all**; the local file is not touched.
* **VERIFY** — an independent check against the bucket (a HEAD checksum below the multipart
  threshold, a streamed re-GET and re-hash at or above it), and then the *only* write this
  engine makes to `tabFile`: the guarded link. Also has no deletion path.
* **CLEANUP** — the only phase that moves a local file, and only after §3.

---

## 2. Conflict triage

Nothing is guessed. An object whose bytes or visibility are ambiguous stops and waits for
you, and the local file stays where it is.

| Conflict | What it means | Usual action |
|---|---|---|
| `missing_physical` | a File row whose bytes are not on disk | restore from backup, or `skip` |
| `conflicting_url` | one URL, two different `content_hash` values | `relink` — pick the row that keeps the URL; every other row gets its **own copy** |
| `ambiguous_privacy` | the `is_private` column disagrees with the URL prefix | `resolve_privacy` — you declare the true visibility, and both halves are made to agree |
| `checksum_mismatch` | the bucket returned different bytes than we uploaded | `retry` (re-uploads); if it recurs, investigate the bucket |
| `checksum_unstable` | the *local* bytes changed between attempts | the site is actively rewriting that file; migrate it later |
| `adoption_failed` | a legacy row pointing at an object that is not in the bucket | restore, or `skip` |
| `cleanup_blocked` | a deletion-time gate refused | read the reason; the file was not touched |
| `corrupt_metadata` | a `file_url` this engine cannot interpret | repair the row, then `retry` |

Blockers must be resolved or skipped before cleanup can be approved.

---

## 3. Deleting the verified local copies

Two separate, audited actions, deliberately. **This is not a two-person rule** (see DECISIONS 2026-08-16: this is **aspirational** — nothing requires the two actors to be different users, and the Desk flow chains them in one dialog):

```bash
bench --site S cfs-migrate-approve-cleanup --campaign CFS-CAMP-0001 --note "reviewed by ..."
bench --site S cfs-migrate-cleanup         --campaign CFS-CAMP-0001
```

Approving deletes nothing. When the job then runs, **every** gate is re-evaluated per
object, inside the job:

1. approval present on the campaign;
2. operation mode allows it;
3. the object is Verified, its cloud object is `verified`, and every File reference carries
   the link — a reference the live runtime superseded bars the object outright;
4. no reference hangs off Data Import / Prepared Report / Package Import (hard refusal —
   those bytes are read through local paths);
5. a **fresh** remote verification, right now, using the same strategy split VERIFY uses;
6. the local file is re-`stat`ed and, on any drift, re-hashed against what we uploaded;
7. for a thumbnail, both the source object and the derived object must be verified.

Failing any of them opens a `cleanup_blocked` conflict and leaves the file alone.

**Quarantine, not deletion.** The default mode renames the file to
`sites/<site>/private/cloud_migration_trash/<campaign>/<migration-object>` — outside both
served trees, collision-free, atomic, and instantly reversible:

```python
frappe.get_attr("cloud_file_storage.migration.cleanup.restore_from_quarantine")("<object>")
```

The purge runs daily and is keyed on when the quarantine happened (`cleaned_at`), never on
the file's mtime — a rename preserves mtime, so an mtime-keyed purge would delete a file
quarantined a minute ago simply because the attachment was old.

`--direct-delete` skips quarantine. It runs the same seven gates and is not reversible.

---

## 4. Legacy (0.2.x fork) rows

Rows the old app wrote are **adopted in place**: no byte moves, and the Cloud Storage Object
points at the key the fork chose. An adopted object is `legacy_unverified` — servable,
because those bytes were already what the site served, but never called `verified`, because
nobody has hashed them. A background job promotes them after a streamed re-GET:

```python
frappe.get_attr("cloud_file_storage.migration.adoption.promote_adopted_batch")(limit=500)
```

Until an object is promoted it fails CLEANUP's gate, so unhashed bytes keep their local copy.

Remote `https://` rows are adopted **only** when the URL names the bucket this site is
configured for. Anything else is left completely alone.

---

## 5. Monitoring and reporting

```bash
bench --site S cfs-migrate-status    --campaign C --watch
bench --site S cfs-migrate-report    --campaign C --format csv     # one row per object
bench --site S cfs-migrate-reconcile                               # bucket ⇄ objects ⇄ files
```

Reconcile is a read-only three-way diff. `missing_remote` — a `verified` object whose key is
not in the bucket — raises an Error Log; it is the class that means something already
believes those bytes are safe.

---

## 6. After the campaign

```bash
bench --site S cfs-migrate-purge --campaign C --older-than-days 90
```

Drops the working set (Objects, File Refs, Batches) of a finished campaign and keeps the
record: the Campaign, its Conflicts, the Aliases and the Audit Log.

---

## 7. Rehearsing before production

`cloud_file_storage/tests/rehearsal.py` builds a synthetic corpus with the anomalies a real
site has and drives a whole campaign over it, stage by stage, against real object storage
and real workers. It refuses to run on any site not named in its scratch-site list.

```bash
python -m cloud_file_storage.tests.rehearsal seed    --site <scratch> --count 100000
python -m cloud_file_storage.tests.rehearsal analyze --site <scratch>
python -m cloud_file_storage.tests.rehearsal plan    --site <scratch>
python -m cloud_file_storage.tests.rehearsal run     --site <scratch>
python -m cloud_file_storage.tests.rehearsal measure --site <scratch>
python -m cloud_file_storage.tests.rehearsal gates   --site <scratch>
```

The measured numbers from the P5 rehearsal are in `docs/PROGRESS.md`.
