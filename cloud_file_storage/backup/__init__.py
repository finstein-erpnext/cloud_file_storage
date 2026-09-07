"""Backup domain — isolated bucket, verified artifacts, prefix-scoped lifecycle.

Three properties this package exists to guarantee, none of which core's S3 backup has:

* **freshness** (A28) — the dump uploaded is the dump this job produced. `new_backup`
  hands back a *recent* dump when it is allowed to, so `force=True` is asked for and the
  mtime is then checked anyway; a stale artifact recorded as a fresh backup is a restore
  that silently loses a day.
* **verification** (A5) — SHA256 computed locally, `head_object(ChecksumMode="ENABLED")`
  compared, and above the multipart threshold a streamed re-GET re-hash on top of the
  ContentLength check. A failed verification **never deletes the remote artifact**: the
  bytes that are there are the only copy of something, and deleting them to tidy up the
  bucket is the delete-before-verify mistake wearing a different hat.
* **isolation** (B5/A20/invariant 6) — archive storage classes and expiration rules apply
  to the backup bucket only, every generated rule is `Filter.Prefix`-scoped, and rules the
  app did not generate are merged, never replaced.
"""
