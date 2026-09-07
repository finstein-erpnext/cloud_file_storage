# Release Acceptance Gates (FROZEN at P0.5)

The release terminal state is **RELEASE_CANDIDATE**, reached only when F1–F7 all PASS.
Phase-level exit criteria live in `docs/PLAN.md` §B. Estimates (throughput, durations)
are planning information only and are never acceptance criteria.

## F1 — Contract gate
C1–C19 (`tests/contract/test_file_compat_contract.py`, mapped 1:1 to the 19 invariants
in `docs/research/content-consumers-contract.md` §5) execute on **every supported frappe
ref** — junit-asserted per ref, zero silent skips. Any change under the runtime core
cannot merge with a skipped C-test.

## F2 — Mission test matrix
Every CFS-10 scenario has a named green T-test at its layer (L1 unit / L2 MinIO / L3
ecosystem), including: T-PUB T-PRIV T-PERM T-DUP1/2/3 T-DEL1/2 T-AMEND T-ATTACH T-ATTIMG
T-ITEMIMG T-EMAIL-IN/OUT T-DATAIMP T-PREPREP T-RIV T-ZIP T-IMG T-UNI T-LARGE T-API
T-IC-RAW T-IC-FLOW T-IC-LIVE T-OUTAGE T-RESTART T-INTR T-RETRY T-CHKSUM T-PAUSE T-GATE
T-LEGACY T-BACKUP T-RENAME T-RECON T-RELINK T-ADOPT T-MODEGATE T-PRIVFLIP T-CLI T-ALIAS
T-HTML — plus the A25–A36 regression tests (tombstone re-upload, verify-clobber guard,
restore-window validator, backup freshness, capacity-preflight refusal, breaker under
injected outage, batch-residue, quarantine collision, deterministic-name idempotency,
floor-ref private serving, serving-chain resolution, upload-gstr-after-cleanup) and the
thumbnail-availability tests.

## F3 — Migration rehearsal (release-scale: 100,000 synthetic files, seeded anomalies)
Pass/fail strictly on:
- migration executes only on dedicated `cloud_migration` workers;
- normal short/default/long ERP queues unaffected (probe-job latency at baseline);
- bounded CPU, bounded DB load, bounded Redis pressure, bounded memory, bounded disk I/O
  (vs recorded baselines);
- bandwidth throttle demonstrably enforced;
- bounded RQ job count (≈ 2× batch count; no job explosion);
- crash/restart recovery (kill -9, Redis flush, bench restart) converges;
- pause/resume and stop-after-batch honored;
- convergence ≥99.9% verified;
- **zero delete-before-verify** (lint + runtime assertion).

## F4 — Migration Capacity Preflight
During the 100k rehearsal, measure: MariaDB bytes per Migration Object; File Ref bytes;
index bytes; conflict rows; audit rows; temporary growth; peak DB disk consumption;
Redis growth; process memory; disk I/O; queue depth. Project to ~1.2M rows. A real
(production-scale) campaign start is **refused** unless configured capacity safety
thresholds are met; the measured numbers and the projection are recorded on the
Campaign. The 1.5–2.5GB InnoDB figure is planning information only.

## F5 — Ecosystem gate
The L3 CI job (erpnext + hrms + india_compliance installed, MinIO-backed) is green and
mandatory: Repost Item Valuation, Item image, GST Return Log create/read/update/download,
e-Waybill save_file path, upload-gstr-after-cleanup.

## F6 — Safety lint gates
- No `os.remove`/`os.rename`/deletion calls in UPLOAD/VERIFY modules (grep-enforced).
- No `ACL` key in any S3 ExtraArgs (regression test).
- Archive storage classes rejected for the live attachment bucket (validator test).
- Every Select field in shipped doctypes has an explicit default or leading `\n` (lint).
- Raw SQL against tabFile/CSO only at the three sanctioned audited sites (grep-enforced).

## F7 — Release
Integration-merge retest green (post P6∥P7 convergence); real-`bench migrate`
legacy-site test green; security audit PASS; release-manager audit PASS;
supported-versions matrix published; `RELEASE_EVIDENCE.md` produced, clearly
distinguishing cloud-managed permanent attachment bytes vs bounded temporary/local
operational bytes → **RELEASE_CANDIDATE**.
