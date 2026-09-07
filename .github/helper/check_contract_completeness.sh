#!/bin/bash
# Assert that every contract invariant C1..C19 actually EXECUTED on this ref (A22).
#
# A22 requires the C-tests to be "junit-asserted per ref, zero silent skips". A test that
# was collected but skipped (missing boto3, absent env var, guarded @skipUnless) must fail
# this gate exactly like a missing one — otherwise the entire contract suite can quietly
# stop running while the gate still reports green. Grepping for the identifier cannot tell
# an executed test from a skipped one, so the report is parsed as XML.
#
# The contract module lands in P2. Until `cloud_file_storage/tests/contract/` exists this
# is a deliberate no-op, so the gate can ship with the CI baseline and start biting the
# moment the module appears.
set -euo pipefail

WORKSPACE="${GITHUB_WORKSPACE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
CONTRACT_DIR="$WORKSPACE/cloud_file_storage/tests/contract"
JUNIT_XML="${JUNIT_XML:-$WORKSPACE/junit.xml}"

if [ ! -d "$CONTRACT_DIR" ]; then
	echo "contract suite not present yet (arrives with P2) - skipping completeness gate"
	exit 0
fi

if [ ! -f "$JUNIT_XML" ]; then
	echo "::error::contract suite exists but no junit report was produced at $JUNIT_XML"
	exit 1
fi

python3 - "$JUNIT_XML" <<'PY'
import re
import sys
import xml.etree.ElementTree as ET

junit_path = sys.argv[1]

try:
	root = ET.parse(junit_path).getroot()
except ET.ParseError as exc:
	print(f"::error::junit report at {junit_path} is not parseable XML: {exc}")
	raise SystemExit(1)

executed = set()
skipped = {}
failed = {}
saw_contract_case = False

# C<n> as a whole token, so C1 never matches inside C19.
pattern = re.compile(r"C([1-9]|1[0-9])(?![0-9])")

# A <testsuite> may be the root, or nested under <testsuites>.
for case in root.iter("testcase"):
	identity = f"{case.get('classname', '')}.{case.get('name', '')}"

	# Only the contract suite counts: an unrelated test that happens to contain "C3"
	# in its name must never satisfy an invariant.
	if "contract" not in identity.lower():
		continue
	saw_contract_case = True

	numbers = {int(m) for m in pattern.findall(identity)}
	if not numbers:
		continue

	skip_node = case.find("skipped")
	problem = case.find("failure")
	if problem is None:
		problem = case.find("error")

	for n in numbers:
		if skip_node is not None:
			reason = (skip_node.get("message") or "no reason given").strip()
			skipped.setdefault(n, f"{identity}: {reason}")
		else:
			executed.add(n)
			if problem is not None:
				failed.setdefault(n, identity)

required = set(range(1, 20))
status = 0

# The contract suite exists on disk (checked by the caller), so a report with no
# contract testcase at all means the suite did not run or the naming convention moved.
# Failing loudly beats reporting green on an empty match.
if not saw_contract_case:
	print("::error::the contract suite exists but no contract testcase appears in the junit report")
	print("::error::(expected 'contract' in the testcase classname/name - did the module move?)")
	raise SystemExit(1)

missing = sorted(required - executed - set(skipped))
if missing:
	print("::error::contract tests absent from this report: " + ", ".join(f"C{n}" for n in missing))
	status = 1

# A skip only counts if no other case executed that invariant.
silently_skipped = sorted(n for n in skipped if n not in executed)
if silently_skipped:
	print("::error::contract tests were SKIPPED (A22 forbids silent skips):")
	for n in silently_skipped:
		print(f"::error::  C{n} -> {skipped[n]}")
	status = 1

if failed:
	print("::error::contract tests failed:")
	for n in sorted(failed):
		print(f"::error::  C{n} -> {failed[n]}")
	status = 1

if status == 0:
	print(f"contract completeness OK: C1..C19 all executed ({len(executed)}/19 accounted for)")

raise SystemExit(status)
PY
