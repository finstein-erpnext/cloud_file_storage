#!/bin/bash
# Assert that the L3 ecosystem tests actually EXECUTED on this job (F5 / A22).
#
# The ecosystem module skips loudly when erpnext or India Compliance is absent, which is
# right for the unit site and catastrophic here: without this gate the L3 job installs
# three large apps, skips every test that uses them, and reports green. That is the exact
# shape A22 forbids, and the reason the C1..C19 gate exists in the same directory.
#
# Named identities, not counts: a renamed or deleted exit-criterion test must fail the job
# rather than quietly shrink what "the ecosystem gate passed" means.
set -euo pipefail

WORKSPACE="${GITHUB_WORKSPACE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
ECOSYSTEM_MODULE="$WORKSPACE/cloud_file_storage/tests/test_ecosystem.py"
JUNIT_XML="${JUNIT_XML:-$WORKSPACE/junit.xml}"

if [ ! -f "$ECOSYSTEM_MODULE" ]; then
	echo "::error::the ecosystem module is missing at $ECOSYSTEM_MODULE"
	exit 1
fi

if [ ! -f "$JUNIT_XML" ]; then
	echo "::error::no junit report was produced at $JUNIT_XML"
	exit 1
fi

python3 - "$JUNIT_XML" <<'PY'
import sys
import xml.etree.ElementTree as ET

junit_path = sys.argv[1]

#: PLAN §B (the P4 row) and ACCEPTANCE_GATES F5, one entry per named exit criterion.
MODULE = "cloud_file_storage.tests.test_ecosystem"
REQUIRED = {
	"T-RIV: first write is canonical and cloud-backed": f"{MODULE}.TestRepostItemValuation.test_the_first_write_produces_a_canonical_cloud_backed_attachment",
	"T-RIV: rewrite through get_full_path reaches the bucket": f"{MODULE}.TestRepostItemValuation.test_a_rewrite_through_get_full_path_reaches_the_object_store",
	"T-RIV: the fork sentinel stays false": f"{MODULE}.TestRepostItemValuation.test_the_fork_sentinel_never_matches_a_canonical_url",
	"T-ITEMIMG: Item image round-trip": f"{MODULE}.TestItemImage.test_an_item_image_round_trips_through_the_object_store",
	"T-IC-LIVE: GST Return Log create/read/update/download": f"{MODULE}.TestIndiaComplianceLive.test_gst_return_log_create_read_update_and_download",
	"T-IC-LIVE: e-Waybill save_file path": f"{MODULE}.TestIndiaComplianceLive.test_the_e_waybill_save_file_path",
	"T-IC-LIVE: get_content(encodings=[])": f"{MODULE}.TestIndiaComplianceLive.test_get_content_with_an_empty_encoding_list_returns_raw_bytes",
	"T-IC-LIVE: A36 binds the real IC module": f"{MODULE}.TestIndiaComplianceLive.test_the_import_bound_get_file_path_is_patched_on_the_real_module",
	"T-IC-LIVE: upload-gstr-after-cleanup": f"{MODULE}.TestIndiaComplianceLive.test_upload_gstr_after_cleanup",
}

try:
	root = ET.parse(junit_path).getroot()
except ET.ParseError as exc:
	print(f"::error::junit report at {junit_path} is not parseable XML: {exc}")
	raise SystemExit(1)

seen = {}
class_level_skips = []
for case in root.iter("testcase"):
	classname = case.get("classname", "")
	name = case.get("name", "")
	identity = f"{classname}.{name}"

	# A `setUpClass` that raises SkipTest is reported by unittest as ONE synthetic case per
	# class, with an EMPTY classname and the module path buried in the name:
	#   classname=""  name="setUpClass (cloud_file_storage.tests.test_ecosystem.TestItemImage)"
	# Verified against a real run on a site without erpnext. Matching only on classname
	# misses it, and the job would then fail with "absent from the report" while the real
	# reason — the apps were never installed — sits in a message nobody printed.
	if not classname and MODULE in name:
		skip_node = case.find("skipped")
		reason = (skip_node.get("message") or "no reason given").strip() if skip_node is not None else ""
		class_level_skips.append((name, reason))
		continue

	if not identity.startswith(MODULE):
		continue
	if case.find("skipped") is not None:
		outcome = ("skipped", (case.find("skipped").get("message") or "no reason given").strip())
	elif case.find("failure") is not None or case.find("error") is not None:
		outcome = ("failed", "")
	else:
		outcome = ("executed", "")
	seen[identity] = outcome

status = 0

for name, reason in class_level_skips:
	print(f"::error::a whole ecosystem class was SKIPPED on the L3 job: {name} -> {reason}")
	print("::error::the L3 job installs erpnext/hrms/india_compliance - a skip here means it did not")
	status = 1

if not seen:
	if class_level_skips:
		# Already reported above with the reason; do not bury it under a generic message.
		raise SystemExit(1)
	print("::error::no ecosystem testcase appears in the junit report at all")
	print(f"::error::(expected classnames under {MODULE} - did the module move?)")
	raise SystemExit(1)

for label, identity in sorted(REQUIRED.items()):
	outcome = seen.get(identity)
	if outcome is None:
		print(f"::error::{label} did not run: {identity} is absent from the report")
		status = 1
	elif outcome[0] == "skipped":
		print(f"::error::{label} was SKIPPED on the L3 job: {identity} -> {outcome[1]}")
		print("::error::the L3 job installs erpnext/hrms/india_compliance - a skip here means it did not")
		status = 1
	elif outcome[0] == "failed":
		print(f"::error::{label} failed: {identity}")
		status = 1

# Anything else in the module that skipped is still a silent hole in the gate.
for identity, (outcome, reason) in sorted(seen.items()):
	if outcome == "skipped" and identity not in REQUIRED.values():
		print(f"::error::ecosystem test SKIPPED on the L3 job: {identity} -> {reason}")
		status = 1

if status == 0:
	print(f"ecosystem completeness OK: {len(REQUIRED)} exit-criterion tests executed, 0 skipped")

raise SystemExit(status)
PY
