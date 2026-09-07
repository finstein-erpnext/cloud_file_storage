# Semgrep adjudication — CI run against `f399fd49`

Every blocking finding classified individually. **No wildcard or global suppression is used**, and
no suppression is applied to any of the categories the owner prohibited (string formatting or
injection, exception-string leakage, unsafe path construction, permission bypasses,
credential/logging issues, or avoidable manual transaction control).

Classification key: **A** REAL_DEFECT · **B** ARCHITECTURE_REQUIRED · **C** FALSE_POSITIVE ·
**D** TEST_ONLY_ACCEPTABLE · **E** NEEDS_FIX

---

## A / E — REAL_DEFECT, fixed in code (no suppression)

| Rule | File:line | Action | Rationale |
|---|---|---|---|
| `security.frappe-format-string-injection` | `storage/diagnostics.py:141` | **FIXED** → `.format(str(exc))` | An exception object rendered into a translated string can carry `__str__`/`__format__` behaviour and, for botocore errors, endpoint and key material into a user-visible panel. `str(exc)` first is the rule's own prescribed fix. |
| `security.frappe-format-string-injection` | `storage/diagnostics.py:179` | **FIXED** → `.format(str(exc))` | As above. |
| `security.frappe-format-string-injection` | `storage/diagnostics.py:192` | **FIXED** → `.format(str(exc))` | As above. |
| `security.frappe-format-string-injection` | `doctype/cloud_backup_settings/cloud_backup_settings.py:132` | **FIXED** → `.format(..., str(exc))` | Same class; the exception reached a settings-form message. |

**Swept app-wide afterwards**: no bare exception object remains inside any `.format(...)` in
runtime code. **These were not suppressed** — exception-string leakage is on the do-not-suppress
list, and it was a genuine finding.

---

## B — ARCHITECTURE_REQUIRED (narrowest possible suppression, each with its invariant)

| Rule | Files | Rationale |
|---|---|---|
| `frappe-manual-commit` | `gc.py`, `cache/writeback.py`, `commands.py`, `legacy_install.py`, `migration/{engine,analyzer,planner,verify,cleanup,conflicts,adoption,reconcile,thumbnails,preflight}.py`, `backup/{tasks,lifecycle,restore}.py` | **Hard invariant 7** (`INVARIANTS.md:38`): *"Every hook-side or post-commit mutation commits its own transaction (A2/A3)."* These run in `after_request`/`after_job` hooks and in background workers. **`migration/api.py` is listed separately**: its 14 commits are inside `@frappe.whitelist()` endpoints, and their justification names enqueue- and audit-durability, not invariant 7 — corrected after independent review found the recorded reason was not the real one. outside any request transaction, where frappe's implicit commit never fires. The migration engine's CAS operations additionally require per-row durability so a crash costs exactly one object — the property F3's crash-fault criterion measures. Removing them would violate an approved invariant, not improve one. |
| `override-doctype-class` | `hooks.py:17` | The runtime architecture **is** the `File` subclass: `override_doctype_class = {"File": ...CloudFile}`. `install.py` already fails loudly if a second File-storage app is present, which is the risk the rule exists to flag. |

**Suppression style:** per-line `# nosemgrep: <rule>` with the invariant named on the same line.
The rule text itself prescribes this (*"add a comment explaining why and `# nosemgrep` on the same
line"*). No file-level or directory-level ignores.

---

## C — FALSE_POSITIVE (individually proven, then suppressed per line)

**Corrected after independent security review.** An earlier version of this section said these
were "audited and recorded rather than suppressed", and a later section said they "were not
suppressed, and will not be without an explicit owner ruling". **Both statements became false**
once the owner's ruling of 2026-08-17 permitted narrow per-finding suppressions for proven-safe
occurrences, and 38 per-line annotations were added. The prose was not updated with the tree.
The reviewer found the contradiction; it is corrected here rather than quietly overwritten,
because a security artefact that describes a different tree than the one shipping is worse than
no artefact.

| Rule | Files | Evidence, and what was actually done |
|---|---|---|
| `security.frappe-sql-format-injection` | `migration/{analyzer,engine,planner,verify}.py` | Interpolates **only** identifiers: module-level doctype constants and `_file_columns()`, a closed literal list. Every value is a bound parameter. **Suppressed per line** with the rule id, after the review confirmed no ORDER BY, table name or condition is built from input at any of the 21 sites. One site — `cas_object` — turned out **not** to satisfy the claim and was fixed rather than suppressed on that basis; see the F2 entry below. |
| `security.frappe-security-file-traversal` | `storage/hashing.py`, `cache/{materialize,eviction}.py`, `overrides/file.py`, `core_hooks.py`, `migration/{preflight,reconcile,report,thumbnails}.py`, `backup/tasks.py`, the repository automation guard | Paths come from frappe-controlled File resolution, from the confined cache root, from `listdir` of a site directory, or from a fixed literal. **Suppressed per line**, after the review independently attacked the confinement (separators, `..` ordering, symlinks, TOCTOU, null bytes, unicode, over-long names) and found it sound. |

## D — TEST_ONLY_ACCEPTABLE

| Rule | Files | Action |
|---|---|---|
| `security.frappe-setuser`, `frappe-breaks-multitenancy`, `frappe-manual-commit` | `docs/evidence/p8-legacy-rehearsal-{seed,probe,postcheck}.py` | Standalone diagnostic scripts executed manually via `bench execute` against **one** scratch site; they are evidence artefacts, not app code, and are not imported by the package. Suppressed **per line, in those three files only** — never by directory glob, so a future `.py` added under `docs/evidence/` is still scanned. |

---

## Remaining low-severity findings

`frappe-missing-translate-function-python` (`migration/{engine,preflight}.py`,
`patches/v0_6_0/apply_credential_permlevel.py`) is **non-blocking** in the Semgrep configuration
and is carried as a recorded finding, not suppressed. It affects operator-facing strings, not
behaviour or security.

### `string-concat-in-list` — this section was wrong, and the correction is instructive

It previously said this rule was non-blocking in `backup/retention.py` and carried unsuppressed.
**Both halves were false.** On the first CI run ever executed against this branch the rule fired
**blocking** at three sites — `backup/retention.py` ×2 and the repository automation guard ×1
— and took the Linters job red. All three were single wrapped messages, not mis-typed list
elements, so the code was correct and the finding was a false positive.

**It is now resolved by restructuring, with no suppression anywhere.** Each message is held in a
local (or, in the guard, a module constant) that the literal then references.

**What triggers this rule is deliberately NOT stated here, because four attempts to state it were
each falsified by running the tool.** The last of them appeared in this document and in two code
comments: *"the rule fires on an implicit concatenation inside a dict or list literal"*. It does
not. `cloud_file_storage/health.py` and `cloud_file_storage/storage/diagnostics.py` nest the same
`_()` concatenation inside dict literals at nine sites between them, carry no suppression, and are
clean under both CI configs — and the whole-tree scan reports **0** findings, not a dozen.
Reduced synthetic cases fire and clear inconsistently with the real ones, so the trigger depends
on context this adjudication has not isolated.

What is **measured**, and all that is claimed: these three sites fired; hoisting the message out
of the literal removes the finding at each; structurally similar sites elsewhere never fired; and
the tree now scans clean. Anyone editing these messages should re-run the scan rather than reason
from a rule of thumb — this document has now been wrong about that rule twice. The literal stays inside `_()`, which is what keeps
babel able to extract it — hoisting the *string* rather than the *call* would silently drop the
msgid, which is the trap this change had to avoid and which was checked against babel directly.

**Why not `# nosemgrep`, given that is this document's prescribed style.** It was tried first and
**it does not work here**, measured three ways: above the match, inline on the reported line, and
on the enclosing literal — all three left the finding blocking, because annotating the first
string moves the match start to the second. This document already records the same class of
mistake once (§ the closing-paren incident). Restructuring removes the finding instead of
annotating around it, leaves the rule fully active across the tree, and cannot rot when a line
moves.

**Verification, at the shipping tree:** `semgrep --config <frappe-rules> --config
r/python.lang.correctness` over 153 targets and 74 rules → **0 findings, 0 blocking**. Same two
configs as the CI job; note the CI job invokes `semgrep ci` while this was run as plain `semgrep`
— equivalent on a full scan of a clean tree, but not the identical command.

**Note for whoever maintains the local gates:** nothing local catches this class. There is no
semgrep hook in `.pre-commit-config.yaml`, and `tests/test_semgrep_rules.py` scans only the
frappe rules directory, never `r/python.lang.correctness`. "pre-commit clean" is not evidence
about this ruleset, and was mistaken for it once.

---

## What was deliberately NOT done

- No `.semgrepignore` entry was added for any application path.
- No rule was disabled globally.
- No finding in the owner's do-not-suppress categories was suppressed: the only
  format-string/exception-leakage findings were **fixed in code**.

---

# Residual after adjudication — verified by running Semgrep locally

The frappe ruleset was cloned and run locally against this tree
(`semgrep --config frappe-semgrep-rules/rules --error`), because an adjudication that is not
executed is a claim rather than a check. Running it found two things reading could not.

## What the local run proved about the suppressions

**86 `frappe-manual-commit` suppressions work.** That rule no longer appears.

**Five did not, and the cause is a tool interaction worth recording:** `ruff` reformatted the
annotated calls onto multiple lines because the appended comment made them exceed the line
limit, which pushed `# nosemgrep` onto the **closing-paren line** — where Semgrep does not look.
It looks at the line the match *starts* on. Re-placed on the opening line with short comments so
ruff will not rewrap them. Findings: 50 → 45.

**One annotation had landed inside a module docstring** (`cache/writeback.py:10`), where a regex
matched prose describing `frappe.db.commit()` rather than a call. Removed, and every remaining
annotation was then verified by AST to sit on a code line rather than inside a string literal.

## The residual, after the owner's ruling

The owner's ruling of 2026-08-17 permitted narrow, individually-proven suppressions for the three
rule classes below, explicitly amending the earlier blanket prohibition **for proven false
positives and architecture-required uses only**. Each was proven before it was suppressed, and
the proofs are the table at the end of this document.

**Semgrep now reports zero findings on this tree.** That is a measured result — the ruleset was
cloned and run locally after formatting — not an inference from the annotations existing.

---|---|---|---|
| `security.frappe-security-file-traversal` | 9 | "unsafe path construction" | The rule fires on **any** `open(...)`. This is a file-storage application; `digest_path`, the materialisation cache, thumbnails and the backup writer all open files by definition. Paths derive from `frappe.get_site_path()` and `File` rows, never from request input, and hard invariant 2 constrains `file_url` to canonical form. |
| `security.frappe-sql-format-injection` | 4 | "injection" | Fires on any f-string in SQL. These interpolate **only fixed identifiers** — module constants and `_file_columns()`, a closed literal list — while every value is a bound parameter. They are also the three sanctioned sites under hard invariant 5. |
| `security.frappe-setuser` | 1 (`commands.py:24`) | "permission bypasses" | A `bench` CLI entrypoint must establish an identity; there is no session user on the CLI. Every frappe app's CLI does this. |

Also residual: `frappe-missing-translate-function-python` (3 — internal patch assertions and
operator messages, not UI strings) and `frappe-breaks-multitenancy` (2, in one evidence script).

**Superseded.** At the time this was written, none of these were suppressed and the note recorded
why: the instruction not to suppress them was more specific than the instruction to make CI
green. **The owner then ruled** (2026-08-17) that narrow, individually-proven suppressions are
permitted for proven false positives and architecture-required uses. Each was proven and then
suppressed per line; see the table. Kept rather than deleted so the sequence — refuse, escalate,
rule, act — is legible in the record.

**Context the owner needs:** Semgrep has **never passed on this repository**. CI first ran on
this branch at `5ebe22a`, and these findings were present in full at `f399fd49` before any
release-fix work. They are pre-existing, not introduced here. Making the job green requires
either a ruling that narrow, individually-justified suppressions are acceptable in those three
categories, or a refactor of a file-storage application to stop opening files — which is not a
change the approved architecture contemplates.

---

# Complete adjudication table — all 45 findings from the `f399fd49` CI run

Every finding ends in exactly **one** state. Counts: **10 FIXED · 31 FALSE_POSITIVE · 3 TEST_ONLY · 1 ARCHITECTURE_REQUIRED**.

**The most important row is a FIXED one that began as a false positive.** `entry_path()` was
classified FALSE_POSITIVE on first reading. Proving confinement per-site — as the owner
required rather than asserting it — showed it escaped the cache root for any component
containing `..` or a leading `/`. It was safe only because callers happened to pass frappe
docnames. **Semgrep was right and the first adjudication was wrong.**

| # | rule | file | line | classification | suppression | safety proof | reviewer |
|---|---|---|---|---|---|---|---|
| 1 | `frappe-breaks-multitenancy` | `docs/evidence/p8-legacy-rehearsal-probe.py` | 46 | **TEST_ONLY** | `# nosemgrep: frappe-breaks-multitenancy` | module-level assignment in a single-site diagnostic script run via bench execute; not imported by the package | independent security review (pending) |
| 2 | `frappe-breaks-multitenancy` | `docs/evidence/p8-legacy-rehearsal-probe.py` | 67 | **TEST_ONLY** | `# nosemgrep: frappe-breaks-multitenancy` | module-level assignment in a single-site diagnostic script run via bench execute; not imported by the package | independent security review (pending) |
| 3 | `frappe-format-string-injection` | `cloud_file_storage/cloud_file_storage/doctype/cloud_storage_settings/cloud_storage_settings.py` | 190 | **FIXED** | `none` | exception object now passed as str(exc); swept app-wide | independent security review (pending) |
| 4 | `frappe-missing-translate-function-python` | `cloud_file_storage/migration/engine.py` | 134 | **FIXED** | `none` | operator-facing message wrapped in _() | independent security review (pending) |
| 5 | `frappe-missing-translate-function-python` | `cloud_file_storage/migration/preflight.py` | 410 | **FIXED** | `none` | operator-facing message wrapped in _() | independent security review (pending) |
| 6 | `frappe-missing-translate-function-python` | `cloud_file_storage/patches/v0_6_0/apply_credential_permlevel.py` | 36 | **FIXED** | `none` | operator-facing message wrapped in _() | independent security review (pending) |
| 7 | `frappe-missing-translate-function-python` | `cloud_file_storage/patches/v0_6_0/apply_credential_permlevel.py` | 41 | **FIXED** | `none` | operator-facing message wrapped in _() | independent security review (pending) |
| 8 | `frappe-security-file-traversal` | the repository automation guard | 74 | **TEST_ONLY** | `# nosemgrep: frappe-security-file-traversal` | repo tooling (git hook); path is a fixed filename 'the per-phase write-scope manifest' discovered by walking up from cwd; no caller-supplied component | independent security review (pending) |
| 9 | `frappe-security-file-traversal` | `cloud_file_storage/backup/tasks.py` | 181 | **FALSE_POSITIVE** | `# nosemgrep: frappe-security-file-traversal` | core-parity: `_canonical_local_path` enforces **site-directory** containment via frappe's `is_safe_path` (not files-dir), matching core's own `get_full_path`; cloud-backed rows additionally get the stricter files-dir realpath check at `overrides/file.py:267-287`. Other sites: confined cache root, `listdir` of a site directory, or a fixed literal | independent security review (pending) |
| 10 | `frappe-security-file-traversal` | `cloud_file_storage/cache/eviction.py` | 116 | **FALSE_POSITIVE** | `# nosemgrep: frappe-security-file-traversal` | core-parity: `_canonical_local_path` enforces **site-directory** containment via frappe's `is_safe_path` (not files-dir), matching core's own `get_full_path`; cloud-backed rows additionally get the stricter files-dir realpath check at `overrides/file.py:267-287`. Other sites: confined cache root, `listdir` of a site directory, or a fixed literal | independent security review (pending) |
| 11 | `frappe-security-file-traversal` | `cloud_file_storage/cache/materialize.py` | 54 | **FIXED** | `# nosemgrep: frappe-security-file-traversal` | entry_path() now confines by construction (components flattened, resolved path asserted under resolved cache root); was a REAL DEFECT, proven by TestTheCachePathCannotEscapeItsRoot | independent security review (pending) |
| 12 | `frappe-security-file-traversal` | `cloud_file_storage/cache/materialize.py` | 73 | **FIXED** | `# nosemgrep: frappe-security-file-traversal` | entry_path() now confines by construction (components flattened, resolved path asserted under resolved cache root); was a REAL DEFECT, proven by TestTheCachePathCannotEscapeItsRoot | independent security review (pending) |
| 13 | `frappe-security-file-traversal` | `cloud_file_storage/cache/materialize.py` | 85 | **FIXED** | `# nosemgrep: frappe-security-file-traversal` | entry_path() now confines by construction (components flattened, resolved path asserted under resolved cache root); was a REAL DEFECT, proven by TestTheCachePathCannotEscapeItsRoot | independent security review (pending) |
| 14 | `frappe-security-file-traversal` | `cloud_file_storage/cache/materialize.py` | 256 | **FIXED** | `# nosemgrep: frappe-security-file-traversal` | entry_path() now confines by construction (components flattened, resolved path asserted under resolved cache root); was a REAL DEFECT, proven by TestTheCachePathCannotEscapeItsRoot | independent security review (pending) |
| 15 | `frappe-security-file-traversal` | `cloud_file_storage/cache/materialize.py` | 273 | **FIXED** | `# nosemgrep: frappe-security-file-traversal` | entry_path() now confines by construction (components flattened, resolved path asserted under resolved cache root); was a REAL DEFECT, proven by TestTheCachePathCannotEscapeItsRoot | independent security review (pending) |
| 16 | `frappe-security-file-traversal` | `cloud_file_storage/core_hooks.py` | 248 | **FALSE_POSITIVE** | `# nosemgrep: frappe-security-file-traversal` | core-parity: `_canonical_local_path` enforces **site-directory** containment via frappe's `is_safe_path` (not files-dir), matching core's own `get_full_path`; cloud-backed rows additionally get the stricter files-dir realpath check at `overrides/file.py:267-287`. Other sites: confined cache root, `listdir` of a site directory, or a fixed literal | independent security review (pending) |
| 17 | `frappe-security-file-traversal` | `cloud_file_storage/migration/preflight.py` | 123 | **FALSE_POSITIVE** | `# nosemgrep: frappe-security-file-traversal` | core-parity: `_canonical_local_path` enforces **site-directory** containment via frappe's `is_safe_path` (not files-dir), matching core's own `get_full_path`; cloud-backed rows additionally get the stricter files-dir realpath check at `overrides/file.py:267-287`. Other sites: confined cache root, `listdir` of a site directory, or a fixed literal | independent security review (pending) |
| 18 | `frappe-security-file-traversal` | `cloud_file_storage/migration/reconcile.py` | 52 | **FALSE_POSITIVE** | `# nosemgrep: frappe-security-file-traversal` | core-parity: `_canonical_local_path` enforces **site-directory** containment via frappe's `is_safe_path` (not files-dir), matching core's own `get_full_path`; cloud-backed rows additionally get the stricter files-dir realpath check at `overrides/file.py:267-287`. Other sites: confined cache root, `listdir` of a site directory, or a fixed literal | independent security review (pending) |
| 19 | `frappe-security-file-traversal` | `cloud_file_storage/migration/report.py` | 231 | **FALSE_POSITIVE** | `# nosemgrep: frappe-security-file-traversal` | core-parity: `_canonical_local_path` enforces **site-directory** containment via frappe's `is_safe_path` (not files-dir), matching core's own `get_full_path`; cloud-backed rows additionally get the stricter files-dir realpath check at `overrides/file.py:267-287`. Other sites: confined cache root, `listdir` of a site directory, or a fixed literal | independent security review (pending) |
| 20 | `frappe-security-file-traversal` | `cloud_file_storage/migration/thumbnails.py` | 111 | **FALSE_POSITIVE** | `# nosemgrep: frappe-security-file-traversal` | core-parity: `_canonical_local_path` enforces **site-directory** containment via frappe's `is_safe_path` (not files-dir), matching core's own `get_full_path`; cloud-backed rows additionally get the stricter files-dir realpath check at `overrides/file.py:267-287`. Other sites: confined cache root, `listdir` of a site directory, or a fixed literal | independent security review (pending) |
| 21 | `frappe-security-file-traversal` | `cloud_file_storage/overrides/file.py` | 163 | **FALSE_POSITIVE** | `# nosemgrep: frappe-security-file-traversal` | core-parity: `_canonical_local_path` enforces **site-directory** containment via frappe's `is_safe_path` (not files-dir), matching core's own `get_full_path`; cloud-backed rows additionally get the stricter files-dir realpath check at `overrides/file.py:267-287`. Other sites: confined cache root, `listdir` of a site directory, or a fixed literal | independent security review (pending) |
| 22 | `frappe-security-file-traversal` | `cloud_file_storage/overrides/file.py` | 414 | **FALSE_POSITIVE** | `# nosemgrep: frappe-security-file-traversal` | core-parity: `_canonical_local_path` enforces **site-directory** containment via frappe's `is_safe_path` (not files-dir), matching core's own `get_full_path`; cloud-backed rows additionally get the stricter files-dir realpath check at `overrides/file.py:267-287`. Other sites: confined cache root, `listdir` of a site directory, or a fixed literal | independent security review (pending) |
| 23 | `frappe-security-file-traversal` | `cloud_file_storage/storage/hashing.py` | 50 | **FALSE_POSITIVE** | `# nosemgrep: frappe-security-file-traversal` | core-parity: `_canonical_local_path` enforces **site-directory** containment via frappe's `is_safe_path` (not files-dir), matching core's own `get_full_path`; cloud-backed rows additionally get the stricter files-dir realpath check at `overrides/file.py:267-287`. Other sites: confined cache root, `listdir` of a site directory, or a fixed literal | independent security review (pending) |
| 24 | `frappe-setuser` | `cloud_file_storage/commands.py` | 24 | **ARCHITECTURE_REQUIRED** | `# nosemgrep: frappe-setuser` | commands.py is a bench/Click CLI module discovered via bench_helper; 0 @frappe.whitelist in the file; identity is the literal 'Administrator', never a request value; only runtime set_user in the app | independent security review (pending) |
| 25 | `frappe-sql-format-injection` | `cloud_file_storage/migration/analyzer.py` | 233 | **FALSE_POSITIVE** | `# nosemgrep: frappe-sql-format-injection` | identifier is a module-level doctype constant (verified by inspection at every site); values parameter-bound. `analyzer.py:233` additionally uses `_file_columns()`, asserted by `TestTheAnalyzerColumnListIsAClosedSet`; `engine.py:cas_object` is now gated by `ALLOWED_OBJECT_CAS_FIELDS`, asserted by `TestCasObjectRefusesUnknownIdentifiers` | independent security review (pending) |
| 26 | `frappe-sql-format-injection` | `cloud_file_storage/migration/analyzer.py` | 518 | **FALSE_POSITIVE** | `# nosemgrep: frappe-sql-format-injection` | identifier is a module-level doctype constant (verified by inspection at every site); values parameter-bound. `analyzer.py:233` additionally uses `_file_columns()`, asserted by `TestTheAnalyzerColumnListIsAClosedSet`; `engine.py:cas_object` is now gated by `ALLOWED_OBJECT_CAS_FIELDS`, asserted by `TestCasObjectRefusesUnknownIdentifiers` | independent security review (pending) |
| 27 | `frappe-sql-format-injection` | `cloud_file_storage/migration/analyzer.py` | 609 | **FALSE_POSITIVE** | `# nosemgrep: frappe-sql-format-injection` | identifier is a module-level doctype constant (verified by inspection at every site); values parameter-bound. `analyzer.py:233` additionally uses `_file_columns()`, asserted by `TestTheAnalyzerColumnListIsAClosedSet`; `engine.py:cas_object` is now gated by `ALLOWED_OBJECT_CAS_FIELDS`, asserted by `TestCasObjectRefusesUnknownIdentifiers` | independent security review (pending) |
| 28 | `frappe-sql-format-injection` | `cloud_file_storage/migration/analyzer.py` | 629 | **FALSE_POSITIVE** | `# nosemgrep: frappe-sql-format-injection` | identifier is a module-level doctype constant (verified by inspection at every site); values parameter-bound. `analyzer.py:233` additionally uses `_file_columns()`, asserted by `TestTheAnalyzerColumnListIsAClosedSet`; `engine.py:cas_object` is now gated by `ALLOWED_OBJECT_CAS_FIELDS`, asserted by `TestCasObjectRefusesUnknownIdentifiers` | independent security review (pending) |
| 29 | `frappe-sql-format-injection` | `cloud_file_storage/migration/analyzer.py` | 654 | **FALSE_POSITIVE** | `# nosemgrep: frappe-sql-format-injection` | identifier is a module-level doctype constant (verified by inspection at every site); values parameter-bound. `analyzer.py:233` additionally uses `_file_columns()`, asserted by `TestTheAnalyzerColumnListIsAClosedSet`; `engine.py:cas_object` is now gated by `ALLOWED_OBJECT_CAS_FIELDS`, asserted by `TestCasObjectRefusesUnknownIdentifiers` | independent security review (pending) |
| 30 | `frappe-sql-format-injection` | `cloud_file_storage/migration/analyzer.py` | 676 | **FALSE_POSITIVE** | `# nosemgrep: frappe-sql-format-injection` | identifier is a module-level doctype constant (verified by inspection at every site); values parameter-bound. `analyzer.py:233` additionally uses `_file_columns()`, asserted by `TestTheAnalyzerColumnListIsAClosedSet`; `engine.py:cas_object` is now gated by `ALLOWED_OBJECT_CAS_FIELDS`, asserted by `TestCasObjectRefusesUnknownIdentifiers` | independent security review (pending) |
| 31 | `frappe-sql-format-injection` | `cloud_file_storage/migration/analyzer.py` | 710 | **FALSE_POSITIVE** | `# nosemgrep: frappe-sql-format-injection` | identifier is a module-level doctype constant (verified by inspection at every site); values parameter-bound. `analyzer.py:233` additionally uses `_file_columns()`, asserted by `TestTheAnalyzerColumnListIsAClosedSet`; `engine.py:cas_object` is now gated by `ALLOWED_OBJECT_CAS_FIELDS`, asserted by `TestCasObjectRefusesUnknownIdentifiers` | independent security review (pending) |
| 32 | `frappe-sql-format-injection` | `cloud_file_storage/migration/analyzer.py` | 755 | **FALSE_POSITIVE** | `# nosemgrep: frappe-sql-format-injection` | identifier is a module-level doctype constant (verified by inspection at every site); values parameter-bound. `analyzer.py:233` additionally uses `_file_columns()`, asserted by `TestTheAnalyzerColumnListIsAClosedSet`; `engine.py:cas_object` is now gated by `ALLOWED_OBJECT_CAS_FIELDS`, asserted by `TestCasObjectRefusesUnknownIdentifiers` | independent security review (pending) |
| 33 | `frappe-sql-format-injection` | `cloud_file_storage/migration/analyzer.py` | 764 | **FALSE_POSITIVE** | `# nosemgrep: frappe-sql-format-injection` | identifier is a module-level doctype constant (verified by inspection at every site); values parameter-bound. `analyzer.py:233` additionally uses `_file_columns()`, asserted by `TestTheAnalyzerColumnListIsAClosedSet`; `engine.py:cas_object` is now gated by `ALLOWED_OBJECT_CAS_FIELDS`, asserted by `TestCasObjectRefusesUnknownIdentifiers` | independent security review (pending) |
| 34 | `frappe-sql-format-injection` | `cloud_file_storage/migration/analyzer.py` | 773 | **FALSE_POSITIVE** | `# nosemgrep: frappe-sql-format-injection` | identifier is a module-level doctype constant (verified by inspection at every site); values parameter-bound. `analyzer.py:233` additionally uses `_file_columns()`, asserted by `TestTheAnalyzerColumnListIsAClosedSet`; `engine.py:cas_object` is now gated by `ALLOWED_OBJECT_CAS_FIELDS`, asserted by `TestCasObjectRefusesUnknownIdentifiers` | independent security review (pending) |
| 35 | `frappe-sql-format-injection` | `cloud_file_storage/migration/analyzer.py` | 829 | **FALSE_POSITIVE** | `# nosemgrep: frappe-sql-format-injection` | identifier is a module-level doctype constant (verified by inspection at every site); values parameter-bound. `analyzer.py:233` additionally uses `_file_columns()`, asserted by `TestTheAnalyzerColumnListIsAClosedSet`; `engine.py:cas_object` is now gated by `ALLOWED_OBJECT_CAS_FIELDS`, asserted by `TestCasObjectRefusesUnknownIdentifiers` | independent security review (pending) |
| 36 | `frappe-sql-format-injection` | `cloud_file_storage/migration/analyzer.py` | 852 | **FALSE_POSITIVE** | `# nosemgrep: frappe-sql-format-injection` | identifier is a module-level doctype constant (verified by inspection at every site); values parameter-bound. `analyzer.py:233` additionally uses `_file_columns()`, asserted by `TestTheAnalyzerColumnListIsAClosedSet`; `engine.py:cas_object` is now gated by `ALLOWED_OBJECT_CAS_FIELDS`, asserted by `TestCasObjectRefusesUnknownIdentifiers` | independent security review (pending) |
| 37 | `frappe-sql-format-injection` | `cloud_file_storage/migration/analyzer.py` | 871 | **FALSE_POSITIVE** | `# nosemgrep: frappe-sql-format-injection` | identifier is a module-level doctype constant (verified by inspection at every site); values parameter-bound. `analyzer.py:233` additionally uses `_file_columns()`, asserted by `TestTheAnalyzerColumnListIsAClosedSet`; `engine.py:cas_object` is now gated by `ALLOWED_OBJECT_CAS_FIELDS`, asserted by `TestCasObjectRefusesUnknownIdentifiers` | independent security review (pending) |
| 38 | `frappe-sql-format-injection` | `cloud_file_storage/migration/analyzer.py` | 884 | **FALSE_POSITIVE** | `# nosemgrep: frappe-sql-format-injection` | identifier is a module-level doctype constant (verified by inspection at every site); values parameter-bound. `analyzer.py:233` additionally uses `_file_columns()`, asserted by `TestTheAnalyzerColumnListIsAClosedSet`; `engine.py:cas_object` is now gated by `ALLOWED_OBJECT_CAS_FIELDS`, asserted by `TestCasObjectRefusesUnknownIdentifiers` | independent security review (pending) |
| 39 | `frappe-sql-format-injection` | `cloud_file_storage/migration/analyzer.py` | 964 | **FALSE_POSITIVE** | `# nosemgrep: frappe-sql-format-injection` | identifier is a module-level doctype constant (verified by inspection at every site); values parameter-bound. `analyzer.py:233` additionally uses `_file_columns()`, asserted by `TestTheAnalyzerColumnListIsAClosedSet`; `engine.py:cas_object` is now gated by `ALLOWED_OBJECT_CAS_FIELDS`, asserted by `TestCasObjectRefusesUnknownIdentifiers` | independent security review (pending) |
| 40 | `frappe-sql-format-injection` | `cloud_file_storage/migration/engine.py` | 301 | **FALSE_POSITIVE** | `# nosemgrep: frappe-sql-format-injection` | identifier is a module-level doctype constant (verified by inspection at every site); values parameter-bound. `analyzer.py:233` additionally uses `_file_columns()`, asserted by `TestTheAnalyzerColumnListIsAClosedSet`; `engine.py:cas_object` is now gated by `ALLOWED_OBJECT_CAS_FIELDS`, asserted by `TestCasObjectRefusesUnknownIdentifiers` | independent security review (pending) |
| 41 | `frappe-sql-format-injection` | `cloud_file_storage/migration/engine.py` | 305 | **FALSE_POSITIVE** | `# nosemgrep: frappe-sql-format-injection` | identifier is a module-level doctype constant (verified by inspection at every site); values parameter-bound. `analyzer.py:233` additionally uses `_file_columns()`, asserted by `TestTheAnalyzerColumnListIsAClosedSet`; `engine.py:cas_object` is now gated by `ALLOWED_OBJECT_CAS_FIELDS`, asserted by `TestCasObjectRefusesUnknownIdentifiers` | independent security review (pending) |
| 42 | `frappe-sql-format-injection` | `cloud_file_storage/migration/engine.py` | 447 | **FALSE_POSITIVE** | `# nosemgrep: frappe-sql-format-injection` | identifier is a module-level doctype constant (verified by inspection at every site); values parameter-bound. `analyzer.py:233` additionally uses `_file_columns()`, asserted by `TestTheAnalyzerColumnListIsAClosedSet`; `engine.py:cas_object` is now gated by `ALLOWED_OBJECT_CAS_FIELDS`, asserted by `TestCasObjectRefusesUnknownIdentifiers` | independent security review (pending) |
| 43 | `frappe-sql-format-injection` | `cloud_file_storage/migration/engine.py` | 592 | **FALSE_POSITIVE** | `# nosemgrep: frappe-sql-format-injection` | identifier is a module-level doctype constant (verified by inspection at every site); values parameter-bound. `analyzer.py:233` additionally uses `_file_columns()`, asserted by `TestTheAnalyzerColumnListIsAClosedSet`; `engine.py:cas_object` is now gated by `ALLOWED_OBJECT_CAS_FIELDS`, asserted by `TestCasObjectRefusesUnknownIdentifiers` | independent security review (pending) |
| 44 | `frappe-sql-format-injection` | `cloud_file_storage/migration/planner.py` | 90 | **FALSE_POSITIVE** | `# nosemgrep: frappe-sql-format-injection` | identifier is a module-level doctype constant (verified by inspection at every site); values parameter-bound. `analyzer.py:233` additionally uses `_file_columns()`, asserted by `TestTheAnalyzerColumnListIsAClosedSet`; `engine.py:cas_object` is now gated by `ALLOWED_OBJECT_CAS_FIELDS`, asserted by `TestCasObjectRefusesUnknownIdentifiers` | independent security review (pending) |
| 45 | `frappe-sql-format-injection` | `cloud_file_storage/migration/verify.py` | 340 | **FALSE_POSITIVE** | `# nosemgrep: frappe-sql-format-injection` | identifier is a module-level doctype constant (verified by inspection at every site); values parameter-bound. `analyzer.py:233` additionally uses `_file_columns()`, asserted by `TestTheAnalyzerColumnListIsAClosedSet`; `engine.py:cas_object` is now gated by `ALLOWED_OBJECT_CAS_FIELDS`, asserted by `TestCasObjectRefusesUnknownIdentifiers` | independent security review (pending) |

---

# Independent security review — findings and disposition

An independent security reviewer (read-only, static analysis, no shell) audited every suppression
site against the code rather than the justification text. **Verdict: `UNSAFE_SUPPRESSIONS=0`,
`BLANKET_SUPPRESSIONS=0`** — but `SEMGREP_ADJUDICATION=FAIL`, because *the artefact contradicted
the tree*. All nine findings are closed below.

## What the review confirmed by attack, not by reading

**The `entry_path` confinement is real.** The reviewer attacked it with separators, `..` after
flattening, `....//` ordering bypasses, Windows separators, empty components, null bytes,
over-long names, unicode/NFKD, and a symlinked root — and confirmed the ordering is correct
(`replace("\\","/")` → `rsplit("/")` → `replace("..","")`, so removal cannot re-create a
separator), that the `+ os.sep` guard rejects sibling-prefix escapes, and that the TOCTOU window
between the check and `open` is **not a privilege boundary** (same uid either side). Its
conclusion: the property is enforced by construction, and the realpath check is genuine
defence-in-depth rather than the load-bearing guard.

**The SQL identifiers are closed at 20 of 21 sites**, and the `set_user` suppression holds — 0
whitelists in the module, the only runtime `set_user` in the app, unreachable from HTTP or RQ,
fixed literal identity, context destroyed in `_finish()`.

## Findings, all closed

**F1 (HIGH) — the artefact stated the opposite of the tree.** Two sections still said the
traversal/SQL/setuser findings were "not suppressed" while 38 per-line annotations existed. The
sections are rewritten to describe the shipping state, and the supersession is recorded rather
than overwritten, so the sequence *refuse → escalate → owner ruling → act* stays legible.

**F2 (MEDIUM) — a justification that was false, and the fix it demanded.** `cas_object` builds
`SET \`col\`=%(col)s` from its `**values` **keys**, not from module constants — so the "closed
set" was a *call-site convention*, and CPython permits a non-identifier string through `**{...}`
unpacking. Every one of the 22 call sites passes literals, so there was no injection; but the
suppression claimed an enforced property that nothing enforced. Now gated by
`ALLOWED_OBJECT_CAS_FIELDS` and asserted by `TestCasObjectRefusesUnknownIdentifiers`, including
an injection-shaped key and a control proving the allow-list does not break real callers.

**F3 (MEDIUM) — a proof cited for 21 rows that covered 1.** The safety-proof column claimed
`TestTheAnalyzerColumnListIsAClosedSet` for all SQL rows; it asserts only on `_file_columns()`,
interpolated at one site. Corrected per row.

**F4 (MEDIUM) — four SQL f-strings were never adjudicated, and now the reason is known.** The
reviewer could not run semgrep and inferred the cause; it was verified here with one command:
**the rule's `frappe.db.sql(f"...")` pattern does not match an implicitly-concatenated f-string**,
so `migration/report.py:251,257,263,285` never fire. They are substantively safe (module constant
interpolated, `campaign` bound) — but **a `ruff` reflow that joins those strings would surface
them**, exactly as the ruff/`# nosemgrep` interaction already bit once. Recorded as a known gate
blind spot rather than left as a silent absence.

**F5 (MEDIUM) — the anti-vacuity control would have skipped in CI and read as green.** It guarded
on a `/tmp` clone that the linter workflow creates in a *different job*. Now: CI clones the rules
and sets `CFS_SEMGREP_RULES`, and a **set-but-missing** path raises instead of skipping — proven
by running it against a bad path and getting a loud failure. Two assertion messages were also
overstated (those rules fire on *any* `open()` / `set_user()`, so they prove the rule is *live*,
not that it discriminates) and are reworded; a crashed scan now fails instead of letting the
negative control pass vacuously.

**F6 (LOW)** — the 14 `api.py` commits are inside whitelisted endpoints, not hooks; their
justification now names enqueue- and audit-durability instead of invariant 7.

**F7 (LOW)** — `is_safe_path` constrains to the **site** directory, not `files/`. Wording
corrected across 15 rows to "core-parity", noting the stricter files-dir check that cloud-backed
rows additionally get at `overrides/file.py:267-287`.

**F8 (INFO) — does not reproduce.** Re-measured mechanically with `--disable-nosem`:

```
live findings (suppressions disabled)  123
annotations                            123
annotations covering NO finding          0
findings with NO annotation              0
rule-id mismatches                       0
```

**Every annotation maps 1:1 to exactly one live finding, and names its rule correctly.**

**F9 (INFO)** — `_confine` aliasing is unreachable (entries keyed by two frappe-generated
docnames, and `materialize` re-checks `sidecar["sha256"] == cso.content_sha256` before reuse). No
action, agreed.

## Suppression integrity — final measurement

Run in the mandated order: **format → lint → Semgrep → AST verification → Semgrep again**.

```
live findings with suppressions disabled   123
annotations                                123   (+2 inside the anti-vacuity fixture string)
annotations covering NO live finding         0
live findings with NO annotation             0
rule-id mismatches                           0
annotations attached to executable code    123
annotations attached to nothing              0   (in app code)
semgrep on the formatted tree             exit 0
```

**Exact 1:1.** Every annotation suppresses exactly one live finding and names that finding's rule.

The two additional annotations live inside `SAFE` in `tests/test_semgrep_rules.py` — a module-level
string constant the anti-vacuity control writes to a temporary file and scans. They are fixture
*content*, not suppressions of anything in this tree.

**They were briefly deleted by this project's own cleanup, and the negative control caught it.**
The dead-annotation sweep operated on raw lines, correctly saw them as non-executable, and
stripped them — after which `test_the_approved_safe_patterns_are_not_blockers` failed on both L2
and L3 with *"fired on the approved safe pattern despite its narrow suppression"*. That is the
test the independent reviewer singled out as the genuinely valuable one, because it reproduces the
real codebase shape rather than a synthetic one. It earned that description on its first
opportunity, against a defect introduced by the adjudication process itself.

---

## Round 3 — independent re-check of the read sinks, and a refusal that could never run

The third security pass was scoped to the fourth path class (exfiltration) closed in round 2. It
returned two clean sinks and one defect, and the defect is worth recording in full because the
**security property was intact the whole time** — what was broken was the handler behind it.

### The dead refusal handler (`migration/engine.py`)

The confinement guard refused correctly and refused *before* `digest_path`, so an operator-supplied
path outside the site was never read and never uploaded. But the refusal called:

```python
_record_object_failure(obj, camp, error_class=..., details=...)   # signature: (obj, camp, exc)
```

which raises `TypeError` on the spot. The blanket per-object handler caught it and fed the
`TypeError` back into the same function — correctly this time — so the object was recorded as a
**transient** `TypeError`, returned to `Pending` with backoff, retried up to `max_attempts` against
an attack input, and finally surfaced as an `upload_failed` **Warning** indistinguishable from an
ordinary bug.

Fixed by calling `_open_conflict(...)`, which accepts those keywords and CAS's `Uploading → Failed`:
one conflict per (object, type), `severity="Blocker"`, `error_class="PathOutsideSite"`. Terminal is
the correct disposition on the merits, not merely the working one — a path outside the site will
never become a path inside it, so retrying it is meaningless work.

`conflict_type="upload_failed"` is deliberately reused rather than minting `path_outside_site`: the
field is a Select on `Cloud Migration Conflict`, and this repo has already shipped an audit write
that raised *inside the transaction it was auditing* because the Select had no matching option. A
new option costs a DocType change + patch + test under invariants 9 and 10.

### Why the existing suite was blind to it

Every test covering this path asserted **the bytes are not uploaded** — and that is green whether
the guard refuses or the handler explodes, because in both worlds no upload happens. It is the same
shape as the `FakeObjectStore` stub and the lying-HEAD test this delivery was bitten by twice: an
outcome assertion that a second, wrong mechanism also satisfies.

The replacement asserts the **recorded disposition**: status `Failed`, exactly one conflict,
`severity="Blocker"`, object `error_class="PathOutsideSite"`, and no leaked `Uploading` lease.
Mutation-proven both ways:

| Mutation | Result |
|---|---|
| M1 — restore the original `_record_object_failure(...)` call | `AssertionError: 'Pending' != 'Failed'` — the defect reproduced exactly, **while the other 23 tests stayed green** |
| M2 — delete the confinement guard entirely | `AssertionError: 'Uploaded' != 'Failed'` + the structural per-sink test fails by name |

M2 is the load-bearing one: it shows the object reaches `Uploaded`, i.e. **`site_config.json` is
genuinely published into the bucket** without the guard. The exfiltration is demonstrated, not
asserted.

### One security predicate, not two

`analyzer.confined_to_site_files` and `cleanup._confined_under` were byte-identical implementations
of the same check — one parameterised over roots, one hardcoding them. That is a live drift risk on
a security primitive: hardening one (a null-byte guard, `commonpath`, a `normpath` step) would
silently leave the other on the old semantics, and the other guarded the sinks that *destroy*.

Collapsed to `analyzer.confined_under(path, roots)`, with `confined_to_site_files` as the
site-roots specialisation. `cleanup` now imports it at module level (no cycle: `analyzer → cleanup`
is lazy at `analyzer.py:407`).

Consolidating surfaced a second instance of the bug I had just fixed: `_purge_one` — reached
directly by the nightly cron, bypassing the gate chain — also called the predicate, and the
per-function lazy import I had added covered only one of three call sites. It failed as a
`NameError` in the full L2 run. Per-function lazy imports were themselves the drift; the
module-level import removes the class.

### Both controls now agree about `patches/**`

`frappe_correctness.yml:213-216` excludes `**/patches/**` and `**/demo/**` from
`frappe-manual-commit`. The R9 test demanded an annotation on a commit in that directory, where the
rule covers no finding — so the 1:1 hygiene sweep would strip the annotation as dead, R9 would fail
again, and the two controls would oscillate over one line indefinitely. R9 now mirrors the rule's
own exclusions. The commit's justification — *the loop's termination depends on committed rows
leaving the candidate set* — is kept as ordinary prose, which is where it belonged.

R9 re-proven live after the exemption widened: an unannotated commit planted outside `patches/`
still fails it by file and line.

### Detector hygiene

One `#:` prose comment in `engine.py` contained the literal token `nosemgrep` while describing a
suppression. It was inert (a comment line carries no finding), but the 1:1 sweep would have counted
it dead and mangled the prose. Reworded — the same trap as the docstring occurrence found earlier.

**Round 3 close:** 123 findings, 123 annotations, 0 unannotated, 0 dead, semgrep exit 0.
