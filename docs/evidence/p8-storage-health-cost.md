# P8 evidence — what `storage_health()` actually costs (security finding L-3)

L-3: `health.storage_health()` is unbounded work behind a pollable endpoint. The ruling was
**measure before optimising**, with a specific trap named: `detect_public_private_residue`'s
`os.walk` costs ~nothing on a healthy site, so a scratch-site measurement would record "no
problem" about the only case that has one.

So this was measured on a site seeded with **both** a non-trivial residue tree **and** a large
`tabFile`, at two scales, on this bench (MariaDB 11.8 on 127.0.0.1:3307, site
`cfs-p8-perf.local`). Five timed calls per figure; median reported.

## Measurements

| Scale | `tabFile` rows | CSO rows | residue files | `detect_public_private_residue` | `storage_health` |
|---|---|---|---|---|---|
| empty site | 2 | 0 | 0 | 0.000s | 0.006s |
| A | 200,000 | 50,000 | 25,000 | **0.014s** | **0.221s** |
| B | 600,000 | 150,000 | 75,000 | **0.033s** | **0.833s** |

The residue count reported was exact at both scales (25,000 and 75,000), so the walk really
did traverse the tree it was accused of traversing.

## What the numbers say

1. **The `os.walk` is not the cost.** With a 75,000-file residue tree it is 0.033s — **4% of
   `storage_health()`**, at both scales. The finding's implied fix (bound or defer the walk)
   would remove 4% of the runtime and lose the one detector for a live unauthenticated
   disclosure (threat model H-3). It should not be made.
2. **The cost is the aggregate scans.** `storage_health()` runs four full-table
   `COUNT`/`SUM`/`GROUP BY` passes: two over `tabCloud Storage Object` (source and derived) and
   two over `tabFile` (operational and unmigrated splits), plus a `COUNT` of linked File rows.
   None can use a covering index for a `SUM(file_size)` over the whole table.
3. **Scaling is close to linear in row count.** 3× the rows cost 3.8× the time (0.221 → 0.833).
   Projecting the same slope to the production target of **1.2M File rows / ~300k objects gives
   ≈1.7–2.0s per call.**

## Decision

**No optimisation in v1, and the reason is not "it is fast enough" — it is that the measured
cost sits somewhere different from where the finding assumed.** Concretely:

- The endpoint is `System Manager` / `Cloud Storage Manager` only, is not reachable
  anonymously, and is polled by a Desk panel with an explicit refresh button rather than on a
  timer.
- ~2s of read-only aggregate work per poll by an authenticated operator is a real cost but not
  an availability risk, and it is bounded — every query has a fixed shape and no unbounded
  result set.
- Caching it (a short-TTL cached snapshot with the refresh button forcing a recompute) is the
  obvious improvement and is **backlogged, not done**: it changes P7's health surface, and
  changing it late in P8 buys ~2s on a panel while risking a stale number that an operator
  would read as current during a migration.

## Limits of this measurement

- **One machine, warm cache.** The residue walk read a page cache that had just written those
  files; a cold-cache walk over 75,000 dentries on spinning storage would be slower. It would
  have to be ~25× slower to become the dominant term.
- **The 1.2M figure is a projection from two points**, not a measurement at 1.2M.
- **The seeded rows are uniform** — same `file_size`, same handful of `attached_to_doctype`
  values. Real data has different cardinality, which changes `GROUP BY` costs somewhat.
- **`materialize.cache_size_bytes()` was measured against an empty cache directory.** A full
  5GB bounded cache adds its own directory walk that this measurement did not exercise.
