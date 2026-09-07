# Threat model — cloud_file_storage v1.0.0

Scope: this app, on a Frappe v15 site, with an S3-compatible object store. It does not
model the security of Frappe itself, of the ERP applications on the site, of the S3
provider, or of the network between them, except where this app's design depends on a
property of one of those.

Written to be checkable. Every control names the file that implements it and the test that
would fail if it stopped holding, and the last section lists what this model does **not**
cover — including one control that is not delivered in v1 and is documented as aspirational.

---

## 1. Assets

| # | Asset | Why an attacker wants it |
|---|---|---|
| A1 | **Private attachment bytes** in the bucket (`prv/` prefix) | Invoices, statutory filings, HR documents, ID scans |
| A2 | **Public attachment bytes** (`pub/` prefix) | Lower value, but a bucket-wide read grant leaks A1 too |
| A3 | **Object-store credentials** — `access_key_id` / `secret_access_key`, or the instance's IAM role | Full read/write/delete on every attachment the site has ever stored |
| A4 | **Backup artifacts** in the backup bucket | A database dump: every credential, session and business record on the site |
| A5 | **Presigned URLs** | Bearer tokens for A1/A2/A4 that carry no identity and no revocation |
| A6 | **The File→object mapping** (`Cloud Storage Object`, `File.cloud_storage_object`, `File.s3_object_key`) | Object keys are a search index into A1; corrupting the mapping serves the wrong bytes |
| A7 | **The audit trail** (`Cloud Storage Audit Log`) | Erasing it hides everything below |
| A8 | **Availability of attachments** | Deleting objects, or scheduling their deletion, is the loudest form of damage |

## 2. Actors

| Actor | Capability assumed |
|---|---|
| **Anonymous internet** | HTTP to the site and to any public bucket/CDN endpoint; can guess or replay URLs |
| **Authenticated site user** | A Frappe session or API key with ordinary business roles; may hold read on *some* documents |
| **Cloud Storage Manager** | The operator role this app ships. Desk access; may run migrations; **read-only on all twelve of this app's doctypes** |
| **System Manager** | Full administrative role on the site. Trusted for destructive actions, but not for accidents |
| **Compromised background worker** | Code execution inside an RQ worker (e.g. via a malicious app on the same bench) |
| **Bucket-side attacker** | Can read or modify objects in the bucket directly, without going through the site |

## 3. Trust boundaries

```
        anonymous ─┐
   authenticated ──┼──► nginx ──► Frappe request path ──► this app ──► S3
        operator ──┤                    │                    │
  System Manager ──┘                    │                    └──► presigned URL ──► client ──► S3
                                        └──► RQ worker (cloud_migration queue)
```

1. **nginx → Frappe.** nginx serves `sites/<site>/public/$uri` off disk before any Python
   runs. Anything under the public tree is world-readable. See H-3 below.
2. **Frappe → this app.** Every endpoint is `@frappe.whitelist()` and reachable at
   `/api/method/…` with an API key and no browser. The Desk is a client, never a gate.
3. **This app → S3.** Credentials live in a Password field (`__Auth`) or in the instance's
   IAM chain; they never leave the server.
4. **Presigned URL → client.** The one place bytes leave the trust boundary without the
   site in the loop. Everything about their minting is therefore constrained.

## 4. Attack paths and the controls that close them

### H-1 — Anonymous user guesses a private object URL

`file_url` is never a bucket URL: it is always canonical `/files/…` or `/private/files/…`
(PLAN §A "Identity"), and signed URLs are minted at serve time and never stored. A private
GET goes through `serving/private.py`, which intercepts `download_private_file` **after**
`validate_auth`, re-runs core's `find_file_by_url` / `is_downloadable` permission gate,
writes exactly one access-log row, and only then redirects to a short-TTL presigned GET.
Keys are content-addressed SHA256, so they are not guessable from a filename.
*Tests:* `test_serving_private.py`, `test_serving_spike.py`, C10 in the contract suite.

### H-2 — Bucket-wide public read

No `ACL` key is sent on any S3 call, for any object, ever — the single private bucket with
`pub/`/`prv/` prefixes is the model, and public delivery is a bucket-policy or CloudFront-OAC
concern scoped to `pub/*`. There is no `public_bucket` setting to point at the wrong bucket
(A24). *Tests:* `test_minio_integration.test_no_object_is_granted_public_read` (against a
real endpoint, checking the object's real ACL), plus the storage-engine no-ACL assertions.

### H-3 — nginx serves a private thumbnail off disk

Upstream frappe writes a private file's thumbnail to `public/private/files/…`, which nginx
serves with no permission check. This app does not cause it, fixes it for the rows it
manages, and **detects and reports** it for the rows it does not: `health.detect_public_private_residue`
runs on every migrate and in the Settings health panel, and the remediation is in
`docs/runbooks/deployment.md`. Written up for upstream in
`docs/security/upstream-frappe-private-thumbnail-disclosure.md`. **This is a real,
unfixed-in-place exposure on any site that has such residue** — it is surfaced, not closed.

### H-4 — Presigned URL is replayed with a different disposition, or leaks

Presigned URLs are minted at exactly **four** sites, and nowhere else:

| Site | For | TTL |
|---|---|---|
| `serving/private.py:163` | a private attachment, after the full permission gate | `private_presign_ttl` (300s) |
| `serving/public.py:211` | a public attachment, after `is_private = 0` is verified against the DB | `public_presign_ttl` (3600s) |
| `api/compat.py:66` | a legacy fork URL, after the same any-one-readable gate | `private_presign_ttl` |
| `backup/restore.py:228` | a backup artifact, System Manager only | short-lived, audited |

Content-Disposition and Content-Type are **pinned into the signature**, so a leaked URL
cannot be replayed to serve the same bytes inline (`serving/disposition.py`). `.html`,
`.htm`, `.svg` and `.xml` are always forced-attachment (Policy B, ADR 0023), which is what
stops a stored attachment becoming stored XSS on the site's own origin. The backup mint
additionally requires the key to be inside the backup prefix **and** to appear in a Backup
Log manifest, is audited before the URL exists, and the audit row never contains the URL.
*Tests:* `test_disposition.py`, `test_backup_restore.TestDownloadUrlIsScopedAndAudited`
(five refusal guards, all in the CI gate).

### H-5 — An operator escalates to the credentials

`access_key_id` and `secret_access_key` are at **permlevel 1**, with a level-1 DocPerm for
System Manager and none for Cloud Storage Manager. The operator role exists precisely so
storage can be run without credential access (design §3.2). The `v0_6_0` patches assert this
at migrate time — in the shipped JSON **and** in `Custom DocPerm`, because once anyone edits
permissions through Role Permission Manager it is the latter that governs. *Tests:*
`test_desk_permissions.TestTheC1PatchGuardBites` and `TestTheC1PatchBodyGuardsBite`,
`test_backup_permissions.TestCredentialAndKeyFieldsAreLevelOne`.

### H-6 — Someone deletes attachments

Deletion is the most guarded path in the app, in four layers:

1. **Nothing is deleted before it is verified.** A local file is removed only in an
   operator-approved CLEANUP pass, which re-verifies the remote object (fresh HEAD) and
   re-stats/re-hashes the local file immediately before quarantining it. The UPLOAD and
   VERIFY modules contain **no deletion call at all**, and that is lint-enforced.
2. **Physical S3 deletes happen only in deferred GC**: `pending_delete` → grace window
   (30 days by default, coupled to the restore window) → a **locking** reference recount →
   delete + tombstone. CSOs owned by a non-terminal migration campaign are excluded.
3. **Destructive endpoints are System Manager only** (`MANAGER_ROLE`, not `OPERATOR_ROLES`),
   enforced in `migration/api.py` — the choke point the CLI and the Desk share — not merely
   in the Desk wrapper.
4. **Type-to-confirm, server-side, byte for byte.** `DELETE LOCAL FILES` for cleanup and
   `APPLY LIFECYCLE` for a lifecycle policy. Both are checked on the server, because the
   dialog that collects them is not what an API caller sees; both are compared exactly, with
   no normalisation (DECISIONS 2026-08-16, finding L-1).

*Tests:* `test_gc.py`, `test_migration_cleanup.py`, `test_admin_api.py` (near-miss phrases),
`test_backup_lifecycle.TestApplyRefusals`, plus the F6 safety lint.

### H-7 — A lifecycle policy quietly deletes someone else's objects

Every generated rule carries a non-empty `Filter.Prefix`; a policy with an empty prefix is
refused at generation *and* at apply; no rule is ever scoped to the bucket root. The apply
**merges** rather than replaces, preserving every rule whose ID does not start `cfs-`, and
the preview lists exactly what would be dropped. Archive storage classes are refused for the
live attachment bucket, and the live bucket's storage class is constrained to synchronously
retrievable classes by a hard assert. *Tests:* eleven refusal guards in
`test_backup_lifecycle.py`, all named in the CI gate.

### H-8 — A backup artifact is corrupt, or is deleted to hide something

An artifact is verified by ContentLength and by a streamed re-GET SHA256 before the Backup
Log says Success, and a **verification failure never deletes the remote artifact** — it is
kept, the log is marked Failed, and the operator is alerted (A5). Backups are refused
outright if the backup bucket is the attachment bucket. Dumps are taken with
`new_backup(force=True)` and their mtime is asserted to be newer than the job start, so a
stale dump cannot be uploaded as a fresh one (A28). *Tests:* `test_backup.py`
(`TestBackupNeverDeletes`, `TestBackupVerification`, `TestBackupFreshness`).

### H-9 — The audit trail is edited

`Cloud Storage Audit Log` grants **no write and no delete role to anyone**, including System
Manager, and its controller refuses an update even to `ignore_permissions` callers. Rows are
written in the same transaction as the action they describe, so an action without its audit
row cannot commit. Honest limit: this is append-only **by convention and permission**, not by
cryptography — an optional hash chain is backlogged (A24).

### H-10 — A legacy fork URL is used as a file-existence oracle

`api/compat.legacy_generate_file` answers every refusal identically — unknown key, unreadable
row, missing object — with the same `frappe.PermissionError` and the same message, and it is
not `allow_guest`. **Accepted residual, documented in DECISIONS:** the two cases remain
distinguishable by *timing*, because an unknown key does no work while a known one costs up
to 20 permission checks. Closing it would mean doing 20 checks for every unknown key, which
is a free amplifier. What leaks is bounded: the caller must already hold an opaque fork-era
key this app never mints, and learns only that *some* row references it.

### H-11 — An S3 outage becomes a site outage

Request-path S3 calls use `connect_timeout=3, read_timeout=15, max_attempts=2` and a
Redis-backed circuit breaker. After N consecutive transport failures, DUAL_WRITE and
S3_PRIMARY go straight to local; S3_ONLY fails fast with a typed error. Transient S3 errors
are typed (`CloudStorageTransportError`) and never converted into `FileNotFoundError`, which
would make a temporary outage look like data loss. *Tests:* `test_modes.py`, T-OUTAGE.

### H-12 — A compromised worker or a malicious sibling app

Not defended against, and stated plainly: any app on the bench runs in the same process with
the same credentials. What this app does do is refuse to *share* the File doctype — install
fails loudly if another app also overrides it — and route all its background work to a
dedicated `cloud_migration` queue so its jobs are separable and observable.

## 5. Design properties this model relies on

- **For every control in this model, the control is server-side and reachable over
  `/api/method/` without a browser.** No
  gate lives in a Desk dialog, in JavaScript, or in a CLI flag. The CLI and the Desk call the
  same functions in `migration/api.py`, and `tests/test_migration_cli.py` asserts that mapping
  by spying on those functions, so a command that grew its own validation fails the suite.
- **The operator role is read-only on all twelve of this app's doctypes.** Verified in the
  shipped JSON and asserted at migrate time against both permission stores.
- **Every destructive action is System-Manager-gated, type-to-confirm gated where it deletes,
  and audited.**
- **Secrets are never logged and never committed.** The bootstrap's report names fields, never
  values; `test_legacy_install.TestSettingsMapping.test_the_report_never_carries_a_value`
  asserts it.

## 6. What this model does not cover

1. **Two-person control is NOT delivered.** Cleanup is two audited actions, but nothing
   requires the approver and the starter to be different people, and the Desk flow chains
   them in one dialog. Any claim that this app enforces a two-person rule is wrong
   (DECISIONS 2026-08-16, finding L-2). Backlogged for post-v1.
2. **The audit log is append-only by permission, not by construction.** A database
   administrator can edit `tabCloud Storage Audit Log` directly.
3. **H-3 residue is detected, not remediated.** Files this app does not own are not moved.
4. **Presigned URLs cannot be revoked** before their TTL expires. The mitigation is a short
   TTL (300s for private), not revocation.
5. **The S3 provider, the network, and bench/nginx configuration** are outside scope. The
   deployment runbook lists what the operator must configure (bucket policy, versioning,
   AbortIncompleteMultipartUpload, the `internal` guard on `/private/`).
6. **No penetration test has been performed.** The controls above are asserted by unit,
   integration and real-endpoint tests, and reviewed by an independent security reviewer at
   each phase gate; that is not the same thing as an adversary having tried.
