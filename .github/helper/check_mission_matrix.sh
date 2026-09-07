#!/bin/bash
# F2 — assert every CFS-10 mission scenario actually EXECUTED and PASSED on this ref.
#
# F2 names 43 scenarios and requires "a named green T-test at its layer" for each. Until this
# gate existed F2 was the only acceptance gate with no mechanical check: F1, F5, backup and
# MinIO each ship one, F2 shipped prose. That is why F2 was recorded NOT ASSESSED rather than
# PASS -- an unassessed gate is not a pass.
#
# Why the junit is parsed as XML rather than grepped for `T-` tags: every attempt to count this
# gate by grepping was wrong in both directions. It matched `T-ID` inside `AKIA-SECRET-ID`, and
# it counted `T-AMEND` because a module docstring said "amend/copy" while no test drove
# `amended_from`. A tag is a claim; a testcase element in a junit is a measurement.
#
# Fails if a scenario is absent from the manifest, mapped to a test that did not run, skipped,
# failed, or errored. Two scenarios cannot be satisfied by one test: the manifest is checked for
# duplicate targets, so a shared assertion is a gate failure rather than a silent pass.
set -euo pipefail

WORKSPACE="${GITHUB_WORKSPACE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
MANIFEST="$WORKSPACE/cloud_file_storage/mission_matrix.py"
GATES_DOC="$WORKSPACE/docs/ACCEPTANCE_GATES.md"
# F2 spans layers (L1 unit / L2 MinIO / L3 ecosystem), so its evidence spans junits: the
# ecosystem scenarios only execute on a site with erpnext/hrms/india_compliance installed.
# JUNIT_XML may therefore name several reports, space- or colon-separated, and every scenario
# must be green in at least one of them.
JUNIT_XML="${JUNIT_XML:-$WORKSPACE/junit.xml}"

if [ ! -f "$MANIFEST" ]; then
	echo "::error::F2 manifest missing at $MANIFEST"
	exit 1
fi

for report in ${JUNIT_XML//:/ }; do
	if [ ! -f "$report" ]; then
		echo "::error::F2 junit report not found: $report"
		exit 1
	fi
done

python3 - "$MANIFEST" "$GATES_DOC" ${JUNIT_XML//:/ } <<'PY'
import re
import sys
import xml.etree.ElementTree as ET

manifest_path, gates_path = sys.argv[1], sys.argv[2]
junit_paths = sys.argv[3:]

ns = {}
exec(compile(open(manifest_path, encoding="utf8").read(), manifest_path, "exec"), ns)
matrix = ns["MISSION_MATRIX"]

# 1. The manifest must cover exactly the scenarios the gate document names. This is what stops
#    the manifest drifting from F2's own definition -- adding a scenario to the gate without a
#    test, or quietly dropping one from the manifest, both fail here.
section = re.search(r"^## F2(.*?)^## F3", open(gates_path, encoding="utf8").read(), re.S | re.M)
if not section:
	print("::error::could not locate the F2 section of ACCEPTANCE_GATES.md")
	sys.exit(1)
named = set()
for token in re.findall(r"\bT-[A-Z][A-Z0-9]*(?:-[A-Z]+)?(?:[0-9](?:/[0-9])*)?\b", section.group(1)):
	# Expand the document's compact forms: T-DUP1/2/3, T-DEL1/2, T-EMAIL-IN/OUT, T-IC-RAW etc.
	named.add(token)
for compact, expanded in (
	("T-DUP1/2/3", ("T-DUP1", "T-DUP2", "T-DUP3")),
	("T-DEL1/2", ("T-DEL1", "T-DEL2")),
):
	if compact in section.group(1):
		named.update(expanded)
# Expanded from what the DOCUMENT says, never unioned in as constants: a constant union makes
# the count pin blind for those ids, so deleting one from the gate document would leave
# `len(named)` unchanged and the gate green.
for compact, expanded in (
	("T-EMAIL-IN/OUT", ("T-EMAIL-IN", "T-EMAIL-OUT")),
	("T-IC-RAW", ("T-IC-RAW",)),
	("T-IC-FLOW", ("T-IC-FLOW",)),
	("T-IC-LIVE", ("T-IC-LIVE",)),
):
	if compact in section.group(1):
		named.update(expanded)
named = {n for n in named if not re.fullmatch(r"T-DUP1/2/3|T-DEL1/2|T-EMAIL|T-IC", n)}
named = {n for n in named if n not in ("T-DUP1/2/3", "T-DEL1/2")}

unmapped = sorted(named - set(matrix))
if unmapped:
	print(f"::error::F2 scenarios named in ACCEPTANCE_GATES.md with no manifest entry: {unmapped}")
	sys.exit(1)

# The count is pinned, not merely compared. Without this, deleting a scenario from BOTH the gate
# document and the manifest leaves them agreeing with each other and the gate prints "42/42 OK":
# a shrinking gate that reports success. Found by independent review, after five controls that
# each mutated only one side of the comparison and so could not see it.
EXPECTED_SCENARIOS = 43
if len(matrix) != EXPECTED_SCENARIOS or len(named) != EXPECTED_SCENARIOS:
	print(
		f"::error::F2 scenario count changed: manifest has {len(matrix)}, "
		f"ACCEPTANCE_GATES.md names {len(named)}, both must be {EXPECTED_SCENARIOS}. "
		"If the mission matrix genuinely changed, update EXPECTED_SCENARIOS deliberately."
	)
	sys.exit(1)

# F2 has three clauses, not one. The 43 scenarios are the first; the A25-A36 regression tests and
# the thumbnail-availability tests are the other two, and they are lowercase prose that the
# `T-` harvest above cannot see. An earlier version declared "F2 PASS" on the first clause alone,
# which meant deleting the tombstone re-upload test left this gate green.
A_SERIES = {
	"A25": "test_gc.TestTombstoneRevival",
	"A26": "test_migration_pipeline.TestVerifyLinkGuard",
	"A27": "test_backup_restore.TestRestoreWindowCoupling",
	"A28": "test_backup.TestBackupFreshness",
	"A29": "test_migration_reconcile.TestCapacityPreflight",
	"A31": "test_migration_resilience.TestBatchResidue",
	"A30": "test_storage_primitives.TestCircuitBreaker",
	"A32": "test_migration_cleanup.TestQuarantineLayout",
	"A33": "test_migration_pipeline.TestAnalyzerClassification",
	"A34": "test_serving_spike.TestDispatchMechanismAcrossDeclaredRefs",
	"A35": "test_serving_private.TestPrivateServingResolutionChain",
	"A36": "test_ecosystem.TestIndiaComplianceLive.test_the_import_bound_get_file_path_is_patched_on_the_real_module",
}
THUMBNAIL_AVAILABILITY = "test_thumbnails.TestThumbnailAvailabilityGate"

# 2. No two scenarios may share a test: one assertion satisfying two scenarios is a gap.
seen = {}
for scenario, target in matrix.items():
	if target in seen:
		print(f"::error::F2 scenarios {seen[target]} and {scenario} share one test ({target}); each scenario needs its own")
		sys.exit(1)
	seen[target] = scenario

# String equality is not enough: `resolve()` expands a CLASS target to every method in it, so
# mapping one scenario to `Foo` and another to `Foo.test_bar` produces two distinct strings that
# are satisfied by overlapping executed tests. Compare on the class prefix as well. Found by
# independent review; most entries are class-level, so this is how a future scenario would
# naturally collide.
# A_SERIES and the thumbnail target are included: the gate's own list contained exactly the
# shape this check rejects -- A36 was a method inside T-IC-LIVE's class-level target, so the two
# were satisfied by overlapping executed tests while the string check saw two distinct entries.
by_class = {}
for scenario, target in {**matrix, **A_SERIES, "thumbnail-availability": THUMBNAIL_AVAILABILITY}.items():
	parts = target.split(".")
	cls = ".".join(parts[:-1]) if parts[-1].startswith("test_") else target
	by_class.setdefault(cls, []).append((scenario, target))
for cls, entries in sorted(by_class.items()):
	if len(entries) > 1 and any(e[1] == cls for e in entries):
		names = ", ".join(f"{s} -> {t}" for s, t in entries)
		print(
			f"::error::F2 scenarios overlap on class {cls}: {names}. A class-level mapping "
			"covers every method in it, so a sibling method-level mapping is the same test."
		)
		sys.exit(1)

# 3. Every mapped test must appear in the junit, and must have run green.
executed, skipped, bad = {}, set(), {}
cases = []
for path in junit_paths:
	cases.extend(ET.parse(path).getroot().iter("testcase"))
for case in cases:
	cls, name = case.get("classname", ""), case.get("name", "")
	full = f"{cls}.{name}"
	if case.find("skipped") is not None:
		skipped.add(full)
	elif case.find("failure") is not None or case.find("error") is not None:
		bad[full] = "failed" if case.find("failure") is not None else "errored"
	executed.setdefault(cls, set()).add(name)

def resolve(target):
	"""Return (matched_full_ids, how) for a manifest target."""
	parts = target.split(".")
	if len(parts) >= 2 and parts[-1].startswith("test_"):
		cls_suffix, method = ".".join(parts[:-1]), parts[-1]
		for cls, names in executed.items():
			if cls.endswith(cls_suffix) and method in names:
				return [f"{cls}.{method}"], "method"
		return [], "method"
	for cls, names in executed.items():
		if cls.endswith(target):
			return [f"{cls}.{n}" for n in names], "class"
	return [], "class"

missing, was_skipped, was_bad, partial = [], [], [], []
for scenario, target in sorted(matrix.items()):
	matched, _how = resolve(target)
	live = [m for m in matched if m not in skipped]
	if not matched:
		missing.append((scenario, target))
	elif not live:
		was_skipped.append((scenario, target))
	else:
		# ANY skip inside a mapped target is a failure, not just "every method skipped". 34 of
		# the 43 targets are class-level, so without this a class could have all but one method
		# skipped and still print green -- a scenario's coverage shrinking to one surviving
		# assertion, silently. F1 demands "zero silent skips" for the C-tests; F2 now matches.
		hidden = [m for m in matched if m in skipped]
		if hidden:
			partial.append((scenario, target, hidden))
		for m in live:
			if m in bad:
				was_bad.append((scenario, target, bad[m]))

for scenario, target in missing:
	print(f"::error::F2 {scenario}: mapped test did not execute -- {target}")
for scenario, target in was_skipped:
	print(f"::error::F2 {scenario}: every mapped test was SKIPPED -- {target}")
for scenario, target, how in was_bad:
	print(f"::error::F2 {scenario}: mapped test {how} -- {target}")
for scenario, target, hidden in partial:
	print(
		f"::error::F2 {scenario}: {len(hidden)} test(s) inside {target} were SKIPPED, so this "
		f"scenario's coverage shrank silently -- {sorted(hidden)[:3]}"
	)

if missing or was_skipped or was_bad or partial:
	sys.exit(1)

# Clause 2 and 3: the A25-A36 regression tests and thumbnail availability.
extra_missing = []
for label, target in list(A_SERIES.items()) + [("thumbnail-availability", THUMBNAIL_AVAILABILITY)]:
	matched, _how = resolve(target)
	live = [m for m in matched if m not in skipped]
	if not live:
		extra_missing.append((label, target))
	else:
		for m in live:
			if m in bad:
				extra_missing.append((label, f"{target} ({bad[m]})"))
for label, target in extra_missing:
	print(f"::error::F2 {label}: required test did not execute green -- {target}")
if extra_missing:
	sys.exit(1)

print(
	f"mission matrix completeness OK: {len(matrix)}/{len(matrix)} scenarios + "
	f"{len(A_SERIES)} A-series + thumbnail availability executed across "
	f"{len(junit_paths)} report(s), 0 missing, 0 skipped, 0 failed"
)
PY
