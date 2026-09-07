# Cloud File Storage — Approved Architecture & Delivery Plan (BINDING)

Status: **APPROVED — AUTONOMOUS_EXECUTION_READY** (owner-approved 2026-08-14).
This document is the authoritative plan. Where any other document (including
`docs/design/*`) conflicts with this file or `docs/adr/`, this file wins.
Companion documents: `docs/ARCHITECTURE.md`, `docs/REQUIREMENTS.md`,
`docs/ACCEPTANCE_GATES.md`, `docs/adr/`, `docs/PROGRESS.md`, `docs/DECISIONS.md`.

---

## A. Final architecture

**cloud_file_storage** — production-grade Frappe v15 cloud storage app rebuilt from the
`frappe-attachments-s3` fork (MIT three-party attribution). Production scale target:
**~1.2M File rows / ~100GB**, site live throughout migration.

### P0 — PASS (historical, ops-only)
Repair: `env/lib/python3.10/site-packages/frappe_s3_attachment.pth`, old target
`/home/user/v15/apps/frappe_s3_attachment` → correct target
`/home/user/v15/apps/cloud_file_storage`. Verified: `frappe_s3_attachment` imports
successfully; `finstein.erp list-apps` succeeds; all 13 DB-backed sites initialize;
skc.local excluded (no DB); no application source changed; no DB changed;
`installed_apps` unchanged; no app installed.

### Frappe integration (verified v15.93.0; declared floor v15.16.0)
Native `write_file` (both live call conventions) + `before_write_file` +
`delete_file_data_content` hooks, plus a minimal `CloudFile(File)` subclass for read-side
methods. `get_content(self, encodings=None)` superset — no-arg = exact v15 behavior;
`encodings=[]` = raw bytes — providing the **India Compliance `get_content(encodings=[])`
compatibility** vanilla v15 lacks. Cloud resolution via fallback chain (own link →
`file_url`-sibling → `content_hash`+`is_private` sibling → legacy stash) in both File
methods and serving; overwrite flows persist their own relink; `FileNotFoundError` only on
true absence; typed errors, never swallowed; the `get_file_path` bypass (IC GSTR ingest,
erpnext EDI) patched at the frappe symbol and IC's import binding.

### Identity (FROZEN)
Physical = content-addressed `<prefix>/<site>/<pub|prv>/<sha256[:2]>/<sha256[2:4]>/<sha256>`;
the human filename is never the physical uniqueness mechanism. `Cloud Storage Object`:
UUID name; unique `(content_sha256, visibility, bucket)` + unique `s3_key`; Long Int
sizes; **refcount-safe deferred GC** (`pending_delete` → grace → locking per-object
recount → delete + tombstone; tombstones revive to `pending_upload` on re-upload;
campaign-owned CSOs excluded from sweeps). Logical = canonical `/files/...` |
`/private/files/...`; **healthy legacy URLs are never mass-renamed**; only genuine
conflict/relink/adoption cases change a URL — always with `Cloud File URL Alias` (302,
consulted by both serving paths, permission gate re-run), exact parent
Attach/Attach Image/image_field consistency, and an audit row. `content_hash` (MD5)
always preserved.

### Bucket model (FROZEN)
**One private live bucket**, logical `pub/`/`prv/` prefixes; **no public-read ACL
dependency**; public delivery via CloudFront-OAC (scoped `pub/*`) or controlled presigned
serving; SSE-S3 default / SSE-KMS optional; IAM default chain preferred. **Live objects
remain synchronously retrievable** — STANDARD / STANDARD_IA / INTELLIGENT_TIERING only
(structurally asserted); **Glacier archive classes only for backup/archive data**,
prefix-scoped, merge-not-replace.

### Serving
Private = cloud-aware interception of `download_private_file` after `validate_auth`,
preserving `find_file_by_url`/`is_downloadable` permission semantics, **exactly one
access-log row**, then 302 to a short-TTL presigned GET with pinned disposition; local
fallback served directly. The interception target is **not frozen from documentation** —
the P3 spike must prove the full chain on each exact supported revision before freeze.
Public = nginx/static fast path with verified miss-fallthrough to a DB-verifying renderer
(`website_404` neutralized; `is_private=0` required) → CDN/presign. **Public HTML edge —
deterministic release policy: Policy B, forced attachment** — `.html/.htm` (with
`.svg/.xml`) always served with attachment disposition, never inline, never CDN-inline;
renderer resolves both `/{endpoint}` and `/{endpoint}.html`; tested as release behavior.
Legacy fork URLs served forever via the `override_whitelisted_methods` remap to
`cloud_file_storage.api.compat.legacy_generate_file`. Bounded S3 timeouts (3s/15s,
2 retries) + circuit breaker on the request path — an S3 outage degrades file operations
per mode, never the ERP. **All four storage modes** (LOCAL_ONLY / DUAL_WRITE /
S3_PRIMARY_LOCAL_FALLBACK / S3_ONLY) with server-validated transition gates; failures are
explicit queryable states, never silent fallbacks. **Materialization/write-back safety**:
bounded cache (`sites/{site}/cloud_storage_cache/`, filelock, hash sidecars), write-back
tracks every handed-out path (cache and canonical-local), commits its own transaction;
covers the erpnext/IC write-through callers.

### Thumbnail & local-residue policy (v1 — supersedes all "permanently local" wording)
- **Thumbnails are derived data associated with the source File/CSO.** Cloud-managed
  derived objects (`<prefix>/<site>/thm/<sha256[:2]>/<sha256[2:4]>/<sha256>_<w>x<h>`,
  linked `derived_of` → source CSO). Migration: after the source object is **Verified**,
  existing local thumbnails are migrated as derived objects, or safely regenerated from
  the verified source when missing/corrupt. **A local thumbnail is never deleted until
  the source object is verified AND thumbnail availability is guaranteed** (derived
  object verified or regeneration validated). After S3_ONLY, no permanent attachment
  thumbnail requires local persistence: `make_thumbnail` renders via bounded
  materialization, uploads the derived object, and `/files/*_small.*` misses serve
  through the renderer's derived-object lookup. Temporary materialization/cache remains
  allowed and bounded.
- **Ignored operational doctypes (Data Import, Prepared Report, Package Import) are
  classified LOCAL_OPERATIONAL / TEMPORARY_LOCAL** — never claimed as migrated cloud
  attachments. Why local: consumed via direct local paths (Package Import feeds a tar
  subprocess; import templates and prepared-report artifacts are short-lived operational
  bytes). Retention/cleanup: core expiry + an app janitor report for aged operational
  files. Backup: regenerable; included in file tarballs only when explicitly enabled.
  S3_ONLY: these doctypes continue writing locally as a **documented, health-panel-
  reported mode exemption**. The final release report and storage-health dashboard
  clearly distinguish **cloud-managed permanent attachment bytes** vs **bounded
  temporary/local operational bytes**.

### Migration engine
Physical-object unit (Campaign/Batch/Object/File Ref/Conflict; deterministic sha1-derived
names), keyset analyzer + 9-class classifier honoring LOCAL_OPERATIONAL scope,
**dedicated `cloud_migration` queue** (config-gated; loud explicit fallback only),
batched jobs — default **`migration_batch_size` = 1,000** (campaign-overridable; ~2,400
jobs total, never 1.2M), **MariaDB as the migration source of truth** (Redis = dispatch
hint), per-object CAS + commits, in-transfer heartbeats, stale recovery, finalize-CAS
requiring zero non-terminal objects, pause/resume/stop-after-batch, bounded retry/backoff,
bandwidth throttle. **UPLOAD and VERIFY contain no local deletion path** (lintable).
Independent VERIFY (checksum strategy split at the 64MB multipart threshold; File→CSO
link written only in the guarded VERIFY transaction that never clobbers newer runtime
relinks) → operator-approved **CLEANUP requiring fresh remote verification** + local
re-stat/re-hash per object, collision-safe quarantine paths, DB-keyed TTL purge.
**ALYF legacy adoption support**: fork rows adopted in place as `legacy_unverified`,
promoted only after real hash verification; conflicts (privacy mismatch = Blocker) to an
audited triage queue.

### Backup
Separate `Cloud Backup Settings` + isolated backup bucket/prefix; `force=True` +
freshness-asserted dumps; SHA256-verified uploads (artifacts never deleted on
verification failure); per-frequency prefixes/retention; Glacier-economics validator;
`restore_window_days` coupled to object grace + attachment-bucket versioning.

### Support scope
**v15-only supported branch for v1** (floor v15.16.0, bench rev v15.93.0, version-15
tip); MariaDB-only, documented.

---

## B. P0.5–P8 final plan

Dependency graph (FROZEN):
**P0 PASS → P0.5 BASELINE FREEZE → P1 → P2 → P3 → P4 → P5 → P6 ∥ P7 (separate isolated
worktrees) → integration merge/retest → P8 → FINAL RELEASE AUDIT**

| Phase | Scope | Exit criteria |
|---|---|---|
| **P0** ✅ PASS | Ops-only `.pth` repair (recorded in §A) | Met |
| **P0.5 — Architecture & Autonomous Baseline Freeze** | Regenerate/reconcile the authoritative scaffold from the approved architecture: `INVARIANTS.md`; the delivery role definitions = principal-architect, runtime-engineer, migration-engineer, product-engineer, security-engineer, performance-engineer, test-engineer, reviewer, release-manager; the delivery workflow definition; the repository automation guard + `(internal tooling)/settings.json`; regenerate authoritative `docs/` (requirements frozen, acceptance gates frozen, ADRs reconciled, dependency graph frozen, P0 PASS recorded); independent read-only architecture review; commit baseline; optional tag `architecture-approved-v1` | Scaffold independently reviewed **PASS**; no untrusted/invented requirements remain; runtime source unchanged; P1 not started |
| **P1 — App/package rename + CI baseline** (no architecture generation) | Package/dirs/hooks rename; `Cloud Storage Settings` + `Cloud Storage Ignored DocType`; patches idempotent; locale regenerated; README/CHANGELOG/license; CI: pinned frappe matrix `{v15.16.0, v15.93.0, version-15}`, MinIO job, lint/semgrep/pip-audit/codeql, coverage + contract-completeness gates | Fresh scratch-site install green; `git grep frappe_s3_attachment` clean outside compat/patches/history; 3-ref matrix green; pre-commit live |
| **P2 — Runtime core** | CSO + Settings (frozen schemas), storage engine (timeouts/breaker, checksummed PUTs, no-ACL), dual-convention `write_file` + persistence, delete hook + deferred GC (locking recount, tombstone revival, campaign exclusion), `CloudFile` resolution chain, materialization + write-back — all four modes at the storage layer | C1–C9, C11–C16 at L2; S3_ONLY URL-only-File tests (amend/attach/file_manager); overwrite-without-save persistence; fresh-connection write-back; tombstone re-upload; concurrency race tests |
| **P3 — Serving + modes** | **Private-serving spike first** — prove on each supported revision: `validate_auth` → cloud-aware `download_private_file` interception → `find_file_by_url`/`is_downloadable` semantics → exactly one access log → signed redirect, for session auth, token/API auth, unauthorized access, shared `file_url`, aliases, local fallback; freeze the mechanism only after the spike passes. Then public renderer (`website_404`, privacy, endpoint semantics), **HTML Policy B tested**, alias doctype + consumption, legacy remap endpoint, `get_file_path` patches, deprecation detector, mode gates, thumbnail derived-object serving | Spike matrix green on all refs; C10; T-PRIV/T-PERM/T-OUTAGE/T-LEGACY/T-ALIAS/T-MODEGATE/T-PRIVFLIP/T-HTML green; exactly-one access-log asserted |
| **P4 — Ecosystem hardening** | L3 job (erpnext+hrms+india_compliance); fix findings | T-RIV, T-ITEMIMG, T-IC-LIVE (incl. upload-gstr-after-cleanup) green; job mandatory in CI |
| **P5 — Migration engine** | Full engine per §A incl. guarded VERIFY link, finalize-CAS, quarantine layout, deterministic names, thumbnail derived-object migration, LOCAL_OPERATIONAL scoping, conflicts/relink/aliases, adoption, reconcile, CLI; **Migration Capacity Preflight** | Entry: CSO interface-conformance + migration-CSO-servable tests. Exit: T-INTR/T-RESTART/T-RETRY/T-CHKSUM/T-PAUSE/T-GATE/T-RECON/T-RELINK/T-ADOPT/T-CLI green; **100,000-file rehearsal** passes gates F3+F4; zero local deletions in UPLOAD/VERIFY |
| **P6 — Backup + lifecycle** ∥ **P7 — Desk UI** (separate isolated worktrees) | P6: backup settings/log, verified+fresh backup job, restore helpers/runbook, lifecycle generator + economics validator + scoped merge. P7: Settings form + health panel (cloud vs operational bytes), 12 mission buttons as thin wrappers over `migration/api.py` against the frozen campaign state machine, Workspace, dashboard Page, roles | P6: T-BACKUP suite + all refusal guards. P7: per-button state-validation tests, type-to-confirm server checks, Playwright smoke on 100-file fixture |
| **Integration merge/retest** | Merge P6+P7 locally into the feature integration branch; rerun convergence tests | Full matrix + MinIO + ecosystem green post-merge |
| **P8 — Compat patch + docs + release** | External-install bootstrap (`bench cfs-adopt-legacy-install` + guarded pre_model_sync patch) proven via real `bench migrate` on a synthetic legacy site; final docs/runbooks/threat model/source-comparison/supported-versions; release report distinguishing cloud-managed vs operational bytes; v1.0.0 | T-RENAME green; release checklist executed |
| **FINAL RELEASE AUDIT** | Complete integration matrix; security audit; migration rehearsal; release-manager audit; produce **RELEASE_EVIDENCE.md** | Single verdict PASS with evidence → **RELEASE_CANDIDATE** |

---

## C. Hard invariants

**Data loss**: no local file or thumbnail deleted/quarantined until its remote object is
independently verified — re-verified at deletion time (fresh remote HEAD + local
re-stat/re-hash); thumbnails additionally require guaranteed availability; deletion
exists only in operator-approved CLEANUP; **UPLOAD and VERIFY contain no local deletion
path** (lintable); physical S3 deletes only via deferred GC (grace + locking recount,
campaign exclusion, no status fabrication); every hook-side/post-commit mutation owns its
transaction; **MariaDB is the migration source of truth**; backup artifacts verified
before Success and never deleted on failure; restore window ≥ object grace, backed by
bucket versioning; **zero delete-before-verify, everywhere**.

**Compatibility**: C1–C19 on every supported ref; **India Compliance
`get_content(encodings=[])`**; canonical `/files`/`/private/files` URLs only; healthy
legacy URLs never mass-renamed; unavoidable changes = alias(302) + parent-field
consistency + audit; `content_hash` preservation; both `write_file` conventions; ALYF
legacy adoption; all four storage modes; materialization/write-back safety; v15-only for
v1 (floor v15.16.0); MariaDB-only documented.

**Security**: private bytes only after the full permission gate + exactly one access log;
short-TTL, disposition-pinned, no-store presigned URLs; single private live bucket,
`pub/`/`prv/` prefixes, no public-read ACLs, CloudFront-OAC scoped `pub/*`; live objects
in synchronous-retrieval classes only; Glacier only for backup/archive; HTML/htm/svg/xml
forced-attachment; secrets in Password fields or IAM chain, never logged/committed;
destructive actions role-gated + server-side type-to-confirm + audited.

**Concurrency**: **no two write-capable agents or sessions modify the same worktree
concurrently — ever.** P1–P5: one isolated phase worktree with explicit single-owner file
scopes. P6/P7: separate isolated worktrees, converged and retested before P8. **Read-only
reviewer means genuinely read-only: no Bash writes, no redirects, no generated files, no
git mutation** — mechanically enforced by the repository automation guard, not
convention. Raw SQL on tabFile/CSO only at the three sanctioned audited sites
(analyzer SELECTs, guarded VERIFY link UPDATE, compat-patch rewrites).

---

## D. Autonomous execution contract

Encoded in the delivery workflow definition. Topology: MASTER →
`dynamic-workflow ALL` → per-phase {PHASE WORKTREE (implementation) ⇄ tests ⇄ REVIEW
AGENTS (read-only)} → PASS → local merge → next phase.

**`dynamic-workflow ALL`:** read `docs/PROGRESS.md`; **WHILE release is not
RELEASE_CANDIDATE**: determine the next dependency-ready phase from the frozen graph; if
P1–P5 → execute one isolated phase workflow; if P6 and P7 are dependency-ready → execute
both in isolated worktrees, merge both locally, rerun convergence tests; for every phase:
implement → test → independent test-engineer → independent read-only reviewer → fix →
retest → re-review — **do not advance until the phase gate is PASS** — update PROGRESS,
update DECISIONS/ADRs, commit the phase, merge locally into the feature integration
branch, proceed immediately. Ordinary failure → fix and continue. HARD STOP → record
`BLOCKED` + exact evidence in `docs/PROGRESS.md` and stop. **After P8**: run the complete
integration matrix, the security audit, the migration rehearsal, and the release-manager
audit; produce `RELEASE_EVIDENCE.md`. Stop only at **RELEASE_CANDIDATE** or a defined
HARD STOP.

The orchestrator does **not** stop for: implementation bugs; failing tests; lint
failures; review findings; security findings fixable within the approved architecture;
CI configuration problems; merge conflicts generated by its own phase branches;
performance issues optimizable inside approved constraints — it fixes these autonomously
and continues, and never stops merely to ask which implementation style is preferred when
the approved architecture already determines the answer. Write ownership is explicit per
phase; implementers never approve their own work.

---

## E. Hard-stop taxonomy

Halt + write `BLOCKED` with exact evidence into `docs/PROGRESS.md`, only for:
1. **OWNER_DECISION_REQUIRED** — a genuine business/architecture choice not resolved by
   approved docs.
2. **SAFETY_BOUNDARY** — work would require an existing non-scratch site, production
   credentials, production DB, production S3, deployment, or destructive external action.
3. **ARCHITECTURE_CONTRADICTION** — two binding approved invariants cannot both be
   satisfied.
4. **ENVIRONMENT_BLOCKER** — required test infrastructure cannot be created/repaired
   after bounded retries.
5. **DATA_SAFETY_BLOCKER** — a zero-loss invariant cannot be proven.

---

## F. Release gates

See `docs/ACCEPTANCE_GATES.md` (F1 contract, F2 mission matrix, F3 100k rehearsal
workload gates, F4 capacity preflight, F5 ecosystem, F6 safety lint, F7 release →
RELEASE_CANDIDATE).

## G. Amendments register

Binding amendments A1–A36 (all adversarial-review fixes) live in
`docs/adr/amendments-register.md` and override any conflicting design-doc text.

## H. Owner decisions on record (non-blocking for build; gate production cutover)

1. Attachment recovery window — default adopted: 30 days + attachment-bucket versioning
   ON (settings-overridable).
2. Presigned-URL exposure for statutory documents — default 300s TTL; proxy-stream mode
   backlogged pending compliance review.
3. Legacy adoption rewriting parent business fields (fork raw-bucket URLs) — audited +
   aliased; executes only in a real production campaign; owner sign-off at cutover.

## I. Verdict

**AUTONOMOUS_EXECUTION_READY** — owner-approved; P0.5 onward executes per §D.
