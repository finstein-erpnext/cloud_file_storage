#!/bin/bash
# Assert that the real-S3 tests actually EXECUTED on the MinIO job (A22, PLAN §C security).
#
# `test_minio_integration.setUpClass` raises SkipTest unless RUN_MINIO_INTEGRATION_TESTS=1,
# and that skip is right on every other job. On THIS job it means the thing the job exists
# for did not happen — and nothing noticed: the backup gate's MODULES tuple does not include
# this module, and the contract gate only knows C1..C19, both of which pass with every real
# S3 round trip skipped. A typo in one of the six env vars in `ci.yml` would therefore ship a
# green `tests-minio` job that never touched S3.
#
# So the gate is driven by the JOB, not by the env var: it names all nineteen identities and
# fails if any is absent, skipped or failed. A mistyped variable makes them skip, and a skip
# is a failure here.
#
# Named identities, not counts, for the same reason as every other gate in this directory: a
# renamed or deleted test must fail the job rather than quietly shrink what it proves.
set -euo pipefail

WORKSPACE="${GITHUB_WORKSPACE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
MINIO_MODULE="$WORKSPACE/cloud_file_storage/tests/test_minio_integration.py"
JUNIT_XML="${JUNIT_XML:-$WORKSPACE/junit.xml}"

if [ ! -f "$MINIO_MODULE" ]; then
	echo "::error::the MinIO integration module is missing at $MINIO_MODULE"
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

#: Every test in the module, because every one of them is a real round trip against a real
#: endpoint and there is no such thing as an optional one here.
MODULE = "cloud_file_storage.tests.test_minio_integration"
REQUIRED = {
	"L2: a private upload lands at the content-addressed key": f"{MODULE}.TestMinioIntegration.test_a_private_upload_lands_at_the_content_addressed_key",
	"L2: a public upload uses the public prefix in the same bucket": f"{MODULE}.TestMinioIntegration.test_a_public_upload_uses_the_public_prefix_in_the_same_bucket",
	"L2: the bytes come back unchanged": f"{MODULE}.TestMinioIntegration.test_the_bytes_come_back_unchanged",
	"L2: no object is granted public read": f"{MODULE}.TestMinioIntegration.test_no_object_is_granted_public_read",
	"L2: the upload is checksummed and verifies": f"{MODULE}.TestMinioIntegration.test_the_upload_is_checksummed_and_verifies",
	"L2: verify detects a size mismatch": f"{MODULE}.TestMinioIntegration.test_verify_detects_a_size_mismatch",
	"L2: a presigned GET serves the bytes with the pinned disposition": f"{MODULE}.TestMinioIntegration.test_a_presigned_get_serves_the_bytes_with_the_pinned_disposition",
	"L2: a presigned URL cannot be replayed with another disposition": f"{MODULE}.TestMinioIntegration.test_a_presigned_url_cannot_be_replayed_with_another_disposition",
	"L2: an unsigned request is refused": f"{MODULE}.TestMinioIntegration.test_an_unsigned_request_is_refused",
	"L2: DUAL_WRITE leaves both copies": f"{MODULE}.TestMinioIntegration.test_dual_write_leaves_both_copies",
	"L2: S3_ONLY leaves no canonical local copy": f"{MODULE}.TestMinioIntegration.test_s3_only_leaves_no_canonical_local_copy",
	"L2: materialization downloads a usable path": f"{MODULE}.TestMinioIntegration.test_materialization_downloads_a_usable_path",
	"L2: deleting the last reference does not remove the object": f"{MODULE}.TestMinioIntegration.test_deleting_the_last_reference_does_not_remove_the_object",
	"L2: GC removes the object after the grace window": f"{MODULE}.TestMinioIntegration.test_gc_removes_the_object_after_the_grace_window",
	"L2: a tombstoned object revives on re-upload": f"{MODULE}.TestMinioIntegration.test_a_tombstoned_object_revives_on_re_upload",
	"L2: a privacy flip copies server-side to the public prefix": f"{MODULE}.TestMinioIntegration.test_a_privacy_flip_copies_server_side_to_the_public_prefix",
	"L2: two identical uploads share one object": f"{MODULE}.TestMinioIntegration.test_two_identical_uploads_share_one_object",
	"L2: the legacy endpoint redirects to a working URL": f"{MODULE}.TestMinioIntegration.test_the_legacy_endpoint_redirects_to_a_working_url",
	"L2: the configured timeouts reach the boto client": f"{MODULE}.TestMinioIntegration.test_the_configured_timeouts_reach_the_boto_client",
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
	#   classname=""  name="setUpClass (cloud_file_storage.tests.test_minio_integration.TestMinioIntegration)"
	# That is the exact shape this module produces when RUN_MINIO_INTEGRATION_TESTS is unset,
	# and it is the shape the gap was hiding in: match only on classname and the job fails
	# with "absent from the report" while the real reason sits in a message nobody printed.
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
	print(f"::error::the whole MinIO class was SKIPPED: {name} -> {reason}")
	print("::error::this job runs MinIO and sets RUN_MINIO_INTEGRATION_TESTS=1 - a skip means it did not reach the tests")
	status = 1

if not seen:
	if class_level_skips:
		# Already reported above with the reason; do not bury it under a generic message.
		raise SystemExit(1)
	print("::error::no MinIO testcase appears in the junit report at all")
	print(f"::error::(expected classnames under {MODULE} - did the module move?)")
	raise SystemExit(1)

for label, identity in sorted(REQUIRED.items()):
	outcome = seen.get(identity)
	if outcome is None:
		print(f"::error::{label} did not run: {identity} is absent from the report")
		status = 1
	elif outcome[0] == "skipped":
		print(f"::error::{label} was SKIPPED: {identity} -> {outcome[1]}")
		print("::error::check RUN_MINIO_INTEGRATION_TESTS and the CLOUD_FILE_STORAGE_MINIO_* variables")
		status = 1
	elif outcome[0] == "failed":
		print(f"::error::{label} failed: {identity}")
		status = 1

# Anything else in the module that skipped is still a silent hole in the gate.
for identity, (outcome, reason) in sorted(seen.items()):
	if outcome == "skipped" and identity not in REQUIRED.values():
		print(f"::error::MinIO test SKIPPED: {identity} -> {reason}")
		status = 1

if status == 0:
	print(f"MinIO completeness OK: {len(REQUIRED)} real-S3 tests executed, 0 skipped")

raise SystemExit(status)
PY
