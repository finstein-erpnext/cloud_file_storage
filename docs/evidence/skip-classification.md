# Skip classification — every skipped test accounted for

The release contract requires that **no required criterion is satisfied only by an unexplained
skip**. Both layers are enumerated below from their junit reports, not from memory.

## L2 (`cfs-rc2.local`, real MinIO) — 1389 tests, 4 skips

| # | Test | Reason | Expected? | Exercised elsewhere? | Belongs to |
|---|---|---|---|---|---|
| 1 | `test_ecosystem.TestIndiaComplianceLive.setUpClass` | needs erpnext + india_compliance on the site | **Yes** — L2 is a bare site by design | **Yes — executed on L3** | F2 **T-IC-LIVE**, F5 |
| 2 | `test_ecosystem.TestItemImage.setUpClass` | same | **Yes** | **Yes — executed on L3** | F2 **T-ITEMIMG**, F5 |
| 3 | `test_ecosystem.TestRepostItemValuation.setUpClass` | same | **Yes** | **Yes — executed on L3** | F2 **T-RIV**, F5 |
| 4 | `TestDispatchMechanismAcrossDeclaredRefs.test_every_declared_ref_dispatches_through_the_module_attribute` (`ref='version-15'`) | the `version-15` branch is not checked out in `apps/frappe`, so `frappe/app.py` cannot be analysed for that ref locally | **Yes** — this bench pins one ref (v15.93.0) | **Yes — the CI 3-ref matrix runs `version-15` as a job** | F1 (A34 floor/ref matrix) |

## L3 (`cfs-ecosystem.local`, erpnext + hrms + india_compliance + payments) — 1403 tests, 1 skip

| # | Test | Reason | Expected? | Exercised elsewhere? | Belongs to |
|---|---|---|---|---|---|
| 1 | `TestDispatchMechanismAcrossDeclaredRefs...` (`ref='version-15'`) | as above — same single-ref bench | **Yes** | **Yes — CI 3-ref matrix** | F1 |

## The required-criteria question, answered directly

**Nothing required is satisfied only by a skip.**

- **C1–C19: 19/19 executed** on L2 — gate output, not inference.
- **F2 mission matrix: 43/43 scenarios + 12/12 A-series + thumbnail availability executed**, and
  the gate resolves them **across both junits**, which is precisely how the three ecosystem
  scenarios that skip on L2 are proven to execute on L3. A gate reading one report would have
  called them missing — its first real run did exactly that, correctly.
- **Ecosystem: 9 exit-criterion tests executed, 0 skipped** on L3.
- **MinIO: 19 real-S3 tests executed, 0 skipped.**
- **Backup: 61 refusal guards executed, 0 skipped.**

The only skip not covered by another local layer is the `version-15` ref case, and that is
**exactly what F1's three-reference CI matrix exists to close**. It is therefore carried as a CI
dependency, not as a local gap — and F1 is not claimed as PASS until that matrix runs green
against the final candidate SHA.

## Added 2026-08-18 — two version-conditional skips on refs below frappe v15.50.0

`FrappeTestCase.secondary_connection` first exists at frappe **v15.50.0**; the declared floor is
**v15.16.0**. Two tests need a genuine second database connection to observe cross-transaction
visibility, so on refs below that they skip **loudly**, naming the version and the reason:

| test | file:line | skips on |
|---|---|---|
| `test_a_reference_committed_after_the_read_view_opened_is_still_seen` | `tests/test_reference_counting.py` | frappe < v15.50.0 |
| `test_the_flush_commits_its_own_transaction` | `tests/test_writeback.py` | frappe < v15.50.0 |

**Neither is a coverage gap on any ref at or above v15.50.0**, which includes the bench, the
`v15.93.0` and `version-15` CI jobs, the MinIO job and the L3 ecosystem job — so both execute in
five of the six places the suite runs, and skip only on the floor.

**No substitute is written, deliberately.** Hand-rolling a second connection would exercise this
app's connection helper rather than the locking behaviour under test, which is the failure mode
A22 exists to prevent: a control that appears to run while testing something else.

Recorded here because this document claims to enumerate every skip, and an unlisted skip is the
one nobody re-examines. Found by an independent reviewer, not by the gates — no CI gate reads
these identities (`check_contract_completeness.sh` counts only contract identities; F2 resolves
against the L2/L3 junits and maps a different reference-counting test), so nothing would have
caught the omission mechanically.
