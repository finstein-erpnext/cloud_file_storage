"""The migration engine (PLAN.md §A "Migration engine", docs/design/migration-engine.md).

Module map, and the invariant each module carries:

* `names`      — A33 deterministic sha1-derived row names; upserts are idempotent by
                 construction rather than by convention.
* `audit`      — the append-only audit writer. Every destructive or mutating action here
                 writes one row in the same transaction as the action.
* `analyzer`   — SCAN_DB / SCAN_FS / CLASSIFY. Holds two of the three sanctioned raw-SQL
                 sites against `tabFile` (docs/INVARIANTS.md invariant 5): the keyset SELECT and the
                 classification reads.
* `planner`    — batch assignment.
* `engine`     — dispatch, claims, heartbeats, stale recovery, UPLOAD. **Contains no
                 deletion path at all** (PLAN §C, F6, enforced by
                 `tests/test_migration_safety_lint.py`).
* `verify`     — independent verification and the third sanctioned raw-SQL site: the A26
                 guarded link UPDATE. Also contains no deletion path.
* `cleanup`    — the ONLY module that touches a local file, and only after operator
                 approval, a mode gate, a fresh remote HEAD and an A4 local re-stat/re-hash.
* `adoption`   — A15/A18 legacy adoption; writes `legacy_unverified` and nothing better.
* `conflicts`  — the triage queue and its operator actions.
* `aliases`    — URL rewrites, always with an alias row, parent-field consistency and audit.
* `reconcile`  — bucket ⇄ CSO ⇄ File three-way diff.
* `preflight`  — A29/F4 capacity preflight.
* `report`     — progress snapshots, realtime publishing, CSV/JSONL export.
* `api`        — the single validation path shared by the Desk buttons and the CLI.
"""
