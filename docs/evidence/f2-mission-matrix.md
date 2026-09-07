# F2 — mission matrix traceability (built at 902ac28, CLOSED after remediation)

**STATUS: 43/43 mapped, 43/43 executed, 0 missing, 0 skipped, 0 failed — F2 PASS.**

F2 requires: "Every CFS-10 scenario has a named green T-test at its layer." 43 named
scenarios, plus A25-A36 regression tests and thumbnail availability.

## Status: 38 of 43 have a covering test; **5 have NONE**

### Tagged already (22 unique T- ids, word-bounded)
T-ADOPT T-ALIAS T-BACKUP T-CHKSUM T-CLI T-GATE T-HTML T-IC-LIVE T-INTR T-ITEMIMG T-LEGACY
T-MODEGATE T-OUTAGE T-PAUSE T-PERM T-PRIV T-PRIVFLIP T-RECON T-RELINK T-RESTART T-RETRY T-RIV

### Covered but UNTAGGED (16) — need only a tag
| Scenario | Covering test |
|---|---|
| T-PUB | `test_serving_public.py` (30 tests) |
| T-DUP1/2/3 | `test_reference_counting.py`, `test_cloud_storage_object.py` |
| T-DEL1/2 | `test_gc.py`, `test_reference_counting.py` |
| T-ATTACH | `test_cloud_file.py:145` |
| T-ATTIMG | `test_thumbnails.py` |
| T-DATAIMP | `test_core_hooks.py`, `test_modes.py` |
| T-PREPREP | `test_modes.py`, `test_install.py` |
| T-UNI | `test_storage_engine.py:232` (`Pflanzenrückgabe.pdf`) |
| T-LARGE | `test_storage_engine.py:102` (multipart threshold) |
| T-API | `test_cloud_file.py`, contract |
| T-IC-RAW | contract C1-C4 (gate text scopes it there) |
| T-IC-FLOW | contract C5/C11, `test_ecosystem.py` |
| T-RENAME | `test_compat.py`, `test_legacy_install.py` |

### **NO COVERING TEST — F2 cannot pass until these exist (5)**
| Scenario | What it requires | Nearest existing coverage |
|---|---|---|
| **T-EMAIL-OUT** | outbound email attachment bytes (`email_body.py:251`) | **none** — no test references Communication/Email Queue attachment bytes |
| **T-EMAIL-IN** | inbound email attachment save (`receive.py:581-598`, private path) | **none** |
| **T-ZIP** | `File.unzip()` on an S3-backed zip via the materialized path | `test_C7_the_path_is_consumable_by_zipfile` proves the *path* is usable; **`File.unzip()` is never called** |
| **T-IMG** | image serve + `optimize_file` overwrite-in-place | `TestC11OverwriteInPlace` covers overwrite; **`optimize_file` is never called** |
| **T-AMEND** | `copy_attachments_from_amended_from` re-reads via get_content, dedups | **none** — `test_cloud_file.py:6` mentions "amend/copy" in a *module docstring* only; no test drives `amended_from` |

**The T-AMEND case is this project's own named failure mode**: a grep for "amend" matches the
prose describing the file, not a test. Counting it as covered is exactly the error the
documentation scanner was built to catch.

## Consequence

**F2 is NOT MET, not merely NOT ASSESSED.** The earlier verdict (NOT ASSESSED — "no artefact
lets anyone check this gate") was correct at the time and is now superseded by having built the
artefact: five named scenarios have no test at any layer. Three of them — outbound email,
inbound email, and unzip — are live frappe content-consumer paths, which is the exact surface
this application replaces.

## To close F2
1. Write the five missing tests. They may expose real defects; that is the point.
2. Tag all 43 scenarios with their T- id.
3. Add `.github/helper/check_mission_matrix.sh`, asserting all 43 execute in the junit —
   the same shape as the contract, backup, MinIO and ecosystem gates, which F2 alone lacks.


---

# Remediation — what changed

## Six missing tests written, not five

Building the manifest surfaced a **sixth** gap the first inventory missed. **T-PREPREP**'s gz
round-trip was "covered" only by an assertion belonging to T-IC-RAW — one test satisfying two
scenarios, which is a gap wearing a tag. It now has its own test driving the Prepared Report
shape.

All six drive the **real frappe call**, never a re-implementation of it:

| Scenario | Test | Frappe path driven |
|---|---|---|
| T-EMAIL-OUT | `TestEmailAttachmentBytes` | `EMail.attach_file` -> `get_content()` -> MIME part |
| T-EMAIL-IN | `TestInboundEmailAttachmentIsStored` | the `receive.py:581` document shape, `.save()` |
| T-ZIP | `TestUnzipOnACloudBackedArchive` | `File.unzip()` -> `get_full_path()` -> `zipfile` |
| T-IMG | `TestOptimizeFileOverwritesInPlace` | `File.optimize_file()` -> `save_file(overwrite=True)` |
| T-AMEND | `TestAmendCopiesAttachmentsWithoutANewObject` | `Document.copy_attachments_from_amended_from()` |
| T-PREPREP | `TestPreparedReportGzRoundTrip` | gz payload -> `get_content()` -> `gzip.decompress` |

## Proven discriminating, and proven *specific*

- Breaking the remote read (`get_content` -> `b""`) reds **all six** — each depends on the real
  read rather than a stub.
- Breaking **only** materialisation (`get_full_path` skips it) reds **only T-ZIP** — so they are
  sensitive to the behaviour they name, not merely to a shared dependency.

## The gate: a manifest of test ids, not a tag grep

`cloud_file_storage/mission_matrix.py` maps each of the 43 scenarios to an exact
`module.Class[.method]`. `.github/helper/check_mission_matrix.sh` parses the junit as XML and
fails on **every** condition required:

| Condition | Control run | Result |
|---|---|---|
| scenario absent from the manifest | drop `T-ZIP` | **rc=1** — "named in ACCEPTANCE_GATES.md with no manifest entry" |
| mapped test never executed | point `T-ZIP` at a non-existent class | **rc=1** — "mapped test did not execute" |
| mapped test skipped | force `T-DUP3`'s case to `<skipped>` | **rc=1** — "every mapped test was SKIPPED" |
| two scenarios sharing one test | point `T-ZIP` at `T-IMG`'s test | **rc=1** — "share one test; each scenario needs its own" |
| clean baseline | unmodified | **rc=0** — 43/43 |

Tag-without-execution cannot count by construction: the gate reads junit `<testcase>` elements,
never source text. Grep-in-prose is structurally impossible.

The manifest is also cross-checked against `ACCEPTANCE_GATES.md` itself, so it cannot drift from
F2's own definition — adding a scenario to the gate document without a test fails the gate.

**The manifest verifier caught five class names I had inferred rather than read**, before the
gate ever ran. Guessing a test id and writing it into a manifest is the same failure as grepping
a docstring: a claim standing in for a measurement.

## Evidence

```
L2 (cfs-rc2.local, real MinIO)      Ran 1373 tests   OK (skipped=4)
L3 (cfs-ecosystem.local, 4 apps)    Ran 1387 tests   OK (skipped=1)
mission matrix completeness OK: 43/43 scenarios executed across 2 report(s),
                                0 missing, 0 skipped, 0 failed
```

## CI

F2 spans layers, so its gate is a **job** rather than a step: `mission-matrix` needs
`tests-minio` and `tests-ecosystem`, downloads both junits, and runs the gate across them. A
gate reading one report would call the other layer's scenarios missing.
