# Supported versions — cloud_file_storage v1.0.0

## The short version

| | v1.0.0 |
|---|---|
| Frappe | **v15 and v16.** Floor `15.16.0`, ceiling `<17.0.0` |
| Database | **MariaDB only** |
| Python | **3.10–3.13 on v15, 3.14 on v16** — see below; the ranges are disjoint |
| Object store | S3 or S3-compatible (verified against AWS S3 semantics and MinIO) |

Declared in `pyproject.toml`:

```toml
[tool.bench.frappe-dependencies]
frappe = ">=15.16.0,<17.0.0"
```

## Why the floor is 15.16.0

The private-serving path depends on `frappe.core.doctype.file.utils.find_file_by_url` and on
the `fid` query argument that `download_private_file` accepts. Both first exist at **v15.16.0**
(A34). Below that floor the interception cannot re-run core's own permission gate, and this app
would have to reimplement it — which is exactly the thing PLAN §A forbids. Both symbols were
re-verified present on v16.

**The declaration is advisory, not enforcing — do not rely on it to stop a bad install.**
`App.install()` calls `validate_app_dependencies()` with the default `throw=False`
(`bench/app.py:244`), and `validate_dependency` only prints a yellow "might not work as
expected" before returning (`bench/app.py:497-518`). Only the separate
`bench validate-dependencies` command passes `throw=True` and exits non-zero
(`bench/commands/make.py:277`). This was confirmed empirically: the app installed cleanly onto
a frappe v16 bench while `pyproject.toml` still declared `<16.0.0`. An earlier revision of this
page claimed `bench get-app` refuses an out-of-range frappe; that was false, and the practical
consequence is the opposite of safety — an unsupported combination installs with a warning
rather than being refused. Run `bench validate-dependencies` if you want the check to bite.

## Why v15 only, and why MariaDB only

**v15 and v16, from one source tree.** An earlier revision of this page said the integration
seams "are not stable across the v15/v16 boundary" and backlogged v16. That was measured against
`v16.0.0-rc.1` and found to be wrong: `write_file` / `before_write_file` /
`delete_file_data_content` still fire from the same places through the same single-owner
`get_hook_method` mechanism, `override_doctype_class` still resolves last-wins (and now
additionally *enforces* that the override subclasses core `File`, which `CloudFile` already
does), `frappe/middlewares.py` is an empty diff, and the File DocType JSON is field-for-field
identical. The three monkey-patch targets — `download_private_file`,
`file_manager.get_file_path`, `StaticDataMiddleware` — all survive.

`get_content` is the one real semantic divergence, and it runs the other way from what was
assumed: **v16 changed the no-argument default** to a four-encoding ladder ending in
`windows-1252`, which decodes small binaries into a mojibake `str`. This app's override pins the
v15 contract, so on v16 it is a *deliberate* divergence from core rather than parity with it —
and it is load-bearing, because v16's own `frappe/utils/pdf.py:327` and
`prepared_report.py:100-101` hand `get_content()` straight to bytes-only APIs. The override
protects every File read on the site.

**What genuinely does not port is the interpreter.** frappe v15 declares
`requires-python = ">=3.10,<3.14"`; v16.0.0-rc.1 declares `">=3.14,<3.15"`. Those ranges are
**disjoint**, so one bench cannot serve both branches and CI needs a leg per interpreter. One
codebase: yes. One bench: no.

**MariaDB only.** The app contains Postgres branches in a few places (`install.py` chooses a
plain index instead of a prefix index, for example) and they are kept because they are correct
and cheap. They are **not tested**: no CI job runs Postgres, the 100k rehearsal was MariaDB,
and the GC locking recount relies on `SELECT … FOR UPDATE` behaviour that was reasoned about
and measured on MariaDB's REPEATABLE READ. Treat the Postgres branches as unsupported code that
happens to exist.

## The CI matrix

`.github/workflows/ci.yml` runs the server suite against four frappe refs, `fail-fast: false`.
Each ref is pinned to an interpreter it can install on, because the two branches'
`requires-python` ranges are disjoint:

| Ref | Python | Role |
|---|---|---|
| `v15.16.0` | 3.10 | the declared floor — proves the floor is real |
| `v15.93.0` | 3.10 | the revision this bench runs — the one everything else was verified against |
| `version-15` | 3.10 | the branch tip — catches a v15 change before a user does |
| `v16.0.0-rc.1` | 3.14 | the v16 branch — catches a v16 regression before a user does |

The v16 leg additionally installs `libmariadb-dev`: frappe v16 pins `mysqlclient==2.2.7`, which
publishes wheels only for cp39–cp313, so pip builds it from source on 3.14.

plus a MinIO job (real S3 round trips, with its own completeness gate) and an L3 ecosystem job
(erpnext + hrms + india_compliance).

### Honest status of the matrix

**The v16 leg has never run in CI.** It was added together with the v16 compatibility work and
is unproven in that environment. What exists instead is a local dual-version verification, on
this bench, at the commit that added it: frappe **16.0.0-dev on Python 3.14.7** and frappe
**15.93.0 on Python 3.10.12**, both benches running byte-identical source from the same commit
(verified by recursive diff, not asserted), each on a freshly reinstalled site, run strictly
sequentially so neither could contend with the other for the shared MariaDB. Both discover the
same 1557 tests. That is a real measurement and it is not a CI result; treat the v16 row as
unproven until the workflow runs it.

**The three v15 refs have been exercised in CI and are green.** The matrix ran at candidate
`203d67e7c44d136d36075ff476138f01178b0506` (workflow run 32254745359), three independent bench
builds each cloning a different frappe ref:

| ref | job | result | contract gate |
|---|---|---|---|
| v15.16.0 (floor) | 96073731537 | 1557 tests OK, 9 skipped | `C1..C19 all executed (19/19)` |
| v15.93.0 | 96073731861 | 1557 tests OK, 9 skipped | `C1..C19 all executed (19/19)` |
| version-15 (tip) | 96073731838 | 1557 tests OK, 9 skipped | `C1..C19 all executed (19/19)` |

Claims about all three refs are now backed by executed tests, and the P1 exit criterion
"3-ref matrix green" is **met**, not carried.

**What this section said before, and why it is recorded rather than deleted.** Until the
owner-authorised push it read *"Only `v15.93.0` has been exercised here … No push has been made
… the P1 exit criterion is therefore carried into the release audit, not claimed here."* That was
accurate when written and became false once the push happened and CI ran; it survived into a
release-candidate audit and was caught there. The correction is kept visible because a
supported-versions document that silently acquires its claims is worth less than one whose
history is legible.

Still true, and not superseded by the CI run:

- This **bench** runs a single frappe checkout at v15.93.0, so every local test run, rehearsal
  and gate recorded in `docs/PROGRESS.md` was executed against that ref. The other two refs are
  exercised in CI only.
- The floor-specific reasoning (A34) was verified by reading frappe's own history for the two
  symbols involved. CI now also executes the suite on that ref, which is stronger evidence than
  the reading alone — but the reading is what motivated the floor.

## Compatible-by-contract, not by version

C1–C19 (`cloud_file_storage/tests/contract/`) are the compatibility contract: the behaviours a
Frappe site and its apps are entitled to expect from `File` regardless of what this app does
underneath. They run storage-mocked on every ref in the matrix and live against MinIO in the
integration job, and a junit completeness gate asserts each one actually executed rather than
silently skipping (A22).

## Ecosystem apps exercised

`erpnext`, `hrms` and `india_compliance` are installed and tested together in the L3 job
(PLAN §B P4, gate F5). India Compliance is the reason `get_content(encodings=[])` is supported
at all: it calls that signature, and vanilla v15 does not have it.

## Upgrading from `frappe_s3_attachment` 0.2.x

Supported, and proven on a synthetic legacy site — see `docs/runbooks/deployment.md` for the
procedure and `docs/evidence/p8-legacy-install-rehearsal.md` for the run. The short form: fix
`apps.txt`, reinstall the package, run `bench cfs-adopt-legacy-install`, then `bench migrate`.
