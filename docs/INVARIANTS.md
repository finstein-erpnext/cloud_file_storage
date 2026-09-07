# cloud_file_storage — Agent Instructions (authoritative, P0.5 baseline)

Production-grade Frappe v15 cloud storage app. Read `docs/PLAN.md` FIRST — it is the
approved, binding architecture. Precedence: `docs/PLAN.md` > `docs/adr/` (incl.
`amendments-register.md` A1–A36) > `docs/design/*` (source material with reconciliation
headers). Requirements: `docs/REQUIREMENTS.md`. Gates: `docs/ACCEPTANCE_GATES.md`.
Durable state: `docs/PROGRESS.md` (update at phase transitions), `docs/DECISIONS.md`
(append-only).

## Build / test
- Tests: `bench --site test_site run-tests --app cloud_file_storage`
  (site needs `bench --site test_site set-config allow_tests true`).
- MinIO integration: `RUN_MINIO_INTEGRATION_TESTS=1` + `CLOUD_FILE_STORAGE_MINIO_
  {ENDPOINT,ACCESS_KEY,SECRET_KEY,BUCKET,REGION}` env vars; local MinIO:
  `docker run -d -p 9000:9000 quay.io/minio/minio:latest server /data --address ":9000"`.
- Lint: `pre-commit run -a` (ruff + format; conventional commits enforced).
- Bench root: `/home/user/v15`; scratch sites only — NEVER an existing business site.

## HARD INVARIANTS (violating any of these fails review automatically)
1. **Zero delete-before-verify**: no local file/thumbnail is deleted or quarantined
   until its remote object is independently verified — and re-verified (remote HEAD +
   local re-stat/re-hash) at deletion time. UPLOAD/VERIFY migration modules contain NO
   deletion calls at all.
2. `file_url` is only ever canonical `/files/...` or `/private/files/...`. Signed URLs
   are issued at serve time, never stored. Healthy legacy URLs are never mass-renamed.
3. `content_hash` (MD5) stays populated on File rows. `get_content(self, encodings=None)`
   keeps exact v15 no-arg behavior and returns raw bytes for `encodings=[]`.
   `FileNotFoundError` only when the object is truly absent — transient S3 errors are
   typed (`CloudStorageTransportError` etc.), never converted, never swallowed.
4. Physical S3 deletes happen only in deferred GC (grace + locking recount + tombstone).
   CSO statuses/columns/API are frozen (A7) — the migration engine imports
   `storage.objects`, never inlines SQL against CSO.
5. Raw SQL against tabFile/CSO exists at exactly three sanctioned audited sites:
   analyzer SELECTs, the guarded VERIFY link UPDATE (A26), compat-patch rewrites.
   Nowhere else.
6. No `ACL` key in any S3 call. No archive storage class for the live bucket. No
   public-read anything. Secrets never logged/committed.
7. Every hook-side or post-commit mutation commits its own transaction (A2/A3).
8. All migration/repair background work targets the `cloud_migration` queue (explicit
   loud fallback only). MariaDB rows are the migration source of truth.
9. Every schema change ships a patch + test. Never weaken a failing test to get green —
   fix root causes. TODO/stub code is never "complete".
10. Byte counters are Long Int. Every Select field has an explicit default or leading
    `\n` in options.

## Concurrency & process (autonomy_guard.py enforces: read-only roles' writes AND
## write-capable roles' file-edit-tool scopes mechanically; write-capable Bash output
## paths are convention + reviewer-enforced)
- **No two write-capable agents/sessions modify the same worktree concurrently — ever.**
  P1–P5: one isolated phase worktree (`phase/P<n>-<slug>`), explicit single-owner file
  scopes (the per-phase write-scope manifest). P6/P7: separate worktrees, converged + retested
  before P8.
- Read-only roles (reviewer, security-engineer, principal-architect) are genuinely
  read-only: no Bash writes/redirects, no generated files, no git mutation.
- An implementer never approves its own work: every phase gate = independent
  test-engineer + independent reviewer PASS, recorded in docs/PROGRESS.md.
- Conventional commits; feature integration branch `feat/enterprise-cloud-storage-v15`;
  `main` untouched until release; no force-push; no push without owner request.
- Hard stops (record BLOCKED + evidence in PROGRESS.md): OWNER_DECISION_REQUIRED,
  SAFETY_BOUNDARY (non-scratch site / production credentials / prod DB / prod S3 /
  deployment / destructive external action), ARCHITECTURE_CONTRADICTION,
  ENVIRONMENT_BLOCKER, DATA_SAFETY_BLOCKER. Everything else: fix and continue.

## Orchestration
`/dynamic-workflow` (see the delivery workflow definition) drives the phase
loop: P0 PASS → P0.5 → P1 → P2 → P3 → P4 → P5 → {P6 ∥ P7} → integration merge/retest →
P8 → final release audit → RELEASE_CANDIDATE (or a defined HARD STOP). Agents:
the delivery role definitions — principal-architect, runtime-engineer, migration-engineer,
product-engineer, security-engineer, performance-engineer, test-engineer, reviewer,
release-manager.
