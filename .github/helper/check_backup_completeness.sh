#!/bin/bash
# Assert that the P6 exit criteria actually EXECUTED on this job (PLAN §B P6 row, F2).
#
# PLAN's P6 exit is "the T-BACKUP suite plus all refusal guards". A count cannot express
# that: a suite can grow by ten tests while the one that proves a stale dump is refused
# quietly stops running. So the gate names the exit-criterion identities, exactly as the
# C1..C19 and ecosystem gates do, and a renamed or deleted one fails the job rather than
# silently shrinking what "P6 passed" means.
#
# Every identity here is a REFUSAL: the property each names is something the app must
# refuse to do. Those are the checks that pass by accident when they stop running, because
# nothing downstream notices a guard that never fired.
set -euo pipefail

WORKSPACE="${GITHUB_WORKSPACE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
BACKUP_MODULE="$WORKSPACE/cloud_file_storage/tests/test_backup.py"
JUNIT_XML="${JUNIT_XML:-$WORKSPACE/junit.xml}"

if [ ! -f "$BACKUP_MODULE" ]; then
	echo "::error::the backup suite is missing at $BACKUP_MODULE"
	exit 1
fi

if [ ! -f "$JUNIT_XML" ]; then
	echo "::error::no junit report was produced at $JUNIT_XML"
	exit 1
fi

python3 - "$JUNIT_XML" "$WORKSPACE" <<'PY'
import ast
import pathlib
import sys
import xml.etree.ElementTree as ET

junit_path = sys.argv[1]
workspace = pathlib.Path(sys.argv[2])

TASKS = "cloud_file_storage.tests.test_backup"
PERMS = "cloud_file_storage.tests.test_backup_permissions"
CONTRACT = "cloud_file_storage.tests.contract.test_file_compat_contract"
DESK = "cloud_file_storage.tests.test_desk_permissions"
LIFECYCLE = "cloud_file_storage.tests.test_backup_lifecycle"
RESTORE = "cloud_file_storage.tests.test_backup_restore"
MODULES = (TASKS, LIFECYCLE, RESTORE, PERMS)

#: PLAN §B (the P6 row), amendments A5/A20/A27/A28, one entry per named refusal.
REQUIRED = {
	"A28: a stale dump is refused before any upload": f"{TASKS}.TestBackupFreshness.test_a_dump_older_than_the_job_is_refused_before_anything_uploads",
	"A28: freshness is checked on every artifact": f"{TASKS}.TestBackupFreshness.test_freshness_is_checked_on_every_artifact_not_only_the_database",
	"A28: new_backup is asked to force": f"{TASKS}.TestBackupFreshness.test_new_backup_is_asked_to_force_a_fresh_dump",
	"A5: a size mismatch fails and keeps the artifact": f"{TASKS}.TestBackupVerification.test_a_size_mismatch_fails_verification_and_keeps_the_remote_artifact",
	"A5: a checksum mismatch fails and keeps the artifact": f"{TASKS}.TestBackupVerification.test_a_checksum_mismatch_fails_verification_and_keeps_the_remote_artifact",
	"A5: a lying HEAD is caught by the streamed re-GET": f"{TASKS}.TestBackupVerification.test_a_lying_head_on_a_large_artifact_is_caught_by_the_reget",
	"A5: a failed verification is never logged Success": f"{TASKS}.TestBackupVerification.test_a_failed_verification_is_never_logged_success",
	"A5: the backup module has no deletion path": f"{TASKS}.TestBackupNeverDeletes.test_the_backup_module_contains_no_delete_call",
	"A5: upload_and_verify raises without deleting": f"{TASKS}.TestBackupNeverDeletes.test_upload_and_verify_raises_without_touching_the_bucket_again",
	"A5: a HEAD failure after upload keeps the artifact": f"{TASKS}.TestBackupNeverDeletes.test_a_head_failure_after_a_successful_upload_keeps_the_remote_artifact",
	"A5: a re-GET failure keeps the artifact": f"{TASKS}.TestBackupNeverDeletes.test_a_reget_failure_keeps_the_artifact_too",
	"A5: an upload failure deletes nothing": f"{TASKS}.TestBackupNeverDeletes.test_an_upload_failure_deletes_nothing",
	"B5: a backup to the attachment bucket is refused": f"{TASKS}.TestBackupRefusals.test_a_backup_is_refused_when_the_backup_bucket_is_the_attachment_bucket",
	"A20: every generated rule is prefix-scoped": f"{LIFECYCLE}.TestGeneratedRulesAreAlwaysScoped.test_every_generated_rule_has_a_non_empty_filter_prefix",
	"A20: an empty prefix is refused at generation": f"{LIFECYCLE}.TestAnEmptyPrefixIsRefused.test_generating_a_policy_with_an_empty_prefix_is_refused",
	"A20: an empty prefix is refused at apply": f"{LIFECYCLE}.TestAnEmptyPrefixIsRefused.test_the_apply_is_refused_with_an_empty_prefix",
	"A20: no rule is ever scoped to the bucket root": f"{LIFECYCLE}.TestAnEmptyPrefixIsRefused.test_no_generated_rule_is_ever_scoped_to_the_bucket_root",
	"A20: a configured prefix reaches all four rule shapes": f"{LIFECYCLE}.TestAnEmptyPrefixIsRefused.test_a_configured_prefix_reaches_all_four_generated_rule_shapes",
	"A20: the two bucket-wide shapes are scoped": f"{LIFECYCLE}.TestAnEmptyPrefixIsRefused.test_the_two_bucket_wide_shapes_are_scoped_under_the_prefix_not_the_root",
	"A20: a foreign rule survives an apply": f"{LIFECYCLE}.TestMergeNeverReplaces.test_the_applied_policy_contains_the_foreign_rule",
	"A20: the preview lists the dropped rules": f"{LIFECYCLE}.TestMergeNeverReplaces.test_the_preview_lists_the_rules_that_would_be_dropped",
	"A20: the apply needs the confirmation phrase": f"{LIFECYCLE}.TestApplyRefusals.test_the_apply_is_refused_without_the_confirmation_phrase",
	"L-1: the apply phrase is compared byte for byte": f"{LIFECYCLE}.TestApplyRefusals.test_a_near_miss_is_refused_by_the_confirm_gate",
	"A20: the apply is refused on a shared bucket": f"{LIFECYCLE}.TestApplyRefusals.test_the_apply_is_refused_when_the_backup_bucket_is_the_attachment_bucket",
	"A20: an economics error blocks the apply": f"{LIFECYCLE}.TestApplyRefusals.test_the_apply_is_refused_when_the_economics_are_an_error",
	"F6: an archive class is refused for the live bucket": f"{LIFECYCLE}.TestArchiveClassesNeverTouchLiveObjects.test_an_archive_class_is_refused_for_the_attachment_bucket",
	"A27: a grace shorter than the restore window is refused": f"{RESTORE}.TestRestoreWindowCoupling.test_a_grace_shorter_than_the_window_is_refused",
	"A27: versioning off is a hard warning": f"{RESTORE}.TestAttachmentBucketVersioning.test_versioning_disabled_is_a_hard_warning",
	"A27: test_connection reports the versioning step": f"{RESTORE}.TestAttachmentBucketVersioning.test_test_connection_reports_the_versioning_step",
	"A27: a corrupt artifact is refused before bench restore": f"{RESTORE}.TestArtifactVerificationBeforeRestore.test_a_corrupted_artifact_of_the_right_length_is_refused",
	"A27: a rejected artifact is never deleted": f"{RESTORE}.TestArtifactVerificationBeforeRestore.test_verification_never_deletes_the_artifact_it_rejected",
	"H-1: an unscoped key is refused a presign": f"{RESTORE}.TestDownloadUrlIsScopedAndAudited.test_a_key_outside_the_backup_prefix_is_refused",
	"H-1: the prefix check alone refuses a recorded key": f"{RESTORE}.TestDownloadUrlIsScopedAndAudited.test_a_recorded_key_outside_the_prefix_is_still_refused",
	"H-1: the manifest check alone refuses a scoped key": f"{RESTORE}.TestDownloadUrlIsScopedAndAudited.test_an_unrecorded_key_inside_the_prefix_isolates_the_manifest_check",
	"A20: the generator runs the prefix backstop": f"{LIFECYCLE}.TestAnEmptyPrefixIsRefused.test_the_generator_actually_runs_the_backstop",
	"M-4/M-5: the shipped JSON declares permlevel 1": f"{PERMS}.TestCredentialAndKeyFieldsAreLevelOne.test_the_shipped_json_declares_permlevel_one",
	"H-1: an attachment key is refused a presign": f"{RESTORE}.TestDownloadUrlIsScopedAndAudited.test_an_attachment_object_key_is_refused",
	"H-1: an unrecorded key is refused a presign": f"{RESTORE}.TestDownloadUrlIsScopedAndAudited.test_a_key_under_the_prefix_but_in_no_manifest_is_refused",
	"H-1: the mint is audited before the URL exists": f"{RESTORE}.TestDownloadUrlIsScopedAndAudited.test_the_audit_row_is_committed_before_the_url_exists",
	"H-1: the audit row never holds the URL": f"{RESTORE}.TestDownloadUrlIsScopedAndAudited.test_the_audit_row_never_contains_the_url_or_a_signature",
	"M-1: the role harness itself can fail": f"{PERMS}.TestTheHarnessCanFail.test_acting_as_makes_only_for_bite",
	"M-1: download_url refuses an outsider": f"{PERMS}.TestBackupEndpointsRefuseAnOutsider.test_download_url_refuses",
	"M-1: the operator cannot mint a download URL": f"{PERMS}.TestTheOperatorRoleIsNotASystemManager.test_the_operator_cannot_mint_a_download_url",
	"M-2: the attachment bucket cannot match the backup bucket": f"{LIFECYCLE}.TestBucketIsolationIsTwoSided.test_pointing_the_attachment_bucket_at_the_backup_bucket_is_refused",
	"M-4/M-5: credential and key fields are permlevel 1": f"{PERMS}.TestCredentialAndKeyFieldsAreLevelOne.test_the_credential_and_key_fields_are_at_permlevel_one",
	"M-5: the operator cannot query an artifact key": f"{PERMS}.TestCredentialAndKeyFieldsAreLevelOne.test_the_operator_cannot_query_a_backup_artifact_key",
	"L-7: only_for is the first statement of every endpoint": f"{PERMS}.TestTheGateRunsFirst.test_only_for_is_the_first_executable_statement_of_every_endpoint",
	"L-7: the endpoint walk finds something": f"{PERMS}.TestTheGateRunsFirst.test_the_walk_finds_the_endpoints",
	"M-9: the patch refuses an operator level-1 row": f"{PERMS}.TestCredentialAndKeyFieldsAreLevelOne.test_the_patch_refuses_an_operator_level_one_row",
	"M-9: the patch reads Custom DocPerm": f"{PERMS}.TestCredentialAndKeyFieldsAreLevelOne.test_the_patch_reads_custom_docperm_and_throws_on_one",
	"M-9: execute runs both permission checks": f"{PERMS}.TestCredentialAndKeyFieldsAreLevelOne.test_execute_actually_runs_both_permission_checks",
	"M-8: the C10 matcher catches every minting form": f"{CONTRACT}.TestC10PermissionGate.test_C10_the_matcher_catches_every_minting_form",
	"L-8: the operator row grants no export": f"{PERMS}.TestCredentialAndKeyFieldsAreLevelOne.test_the_operator_row_no_longer_grants_export",
	"L-6: a credential field back at permlevel 0 is refused": f"{DESK}.TestTheC1PatchBodyGuardsBite.test_a_credential_field_back_at_permlevel_zero_is_refused",
	"L-6: a credential field missing from the meta is refused": f"{DESK}.TestTheC1PatchBodyGuardsBite.test_a_credential_field_missing_from_the_meta_is_refused",
	"L-6: no level-1 DocPerm for System Manager is refused": f"{DESK}.TestTheC1PatchBodyGuardsBite.test_no_level_one_docperm_for_system_manager_is_refused",
	"L-6: an operator level-1 DocPerm is refused": f"{DESK}.TestTheC1PatchBodyGuardsBite.test_an_operator_level_one_docperm_is_refused",
	"footprint: no Custom DocPerm survives": f"{PERMS}.TestTheSuiteLeavesNothingBehind.test_no_custom_docperm_survives_on_the_backup_doctypes",
	"footprint: fixture users hold only declared roles": f"{PERMS}.TestTheSuiteLeavesNothingBehind.test_the_fixture_users_hold_only_their_declared_roles",
	"footprint: role removal is not suppressed": f"{PERMS}.TestTheSuiteLeavesNothingBehind.test_role_removal_is_not_silently_suppressed",
	"footprint: drop_test_user really removes the user": f"{PERMS}.TestTheSuiteLeavesNothingBehind.test_drop_test_user_actually_removes_the_user",
}


# --- F-2: the list cannot silently fall behind the suite -----------------------------------
#
# Everything below this point checks that the guards NAMED here ran. That is one direction,
# and on its own it lets a newly written refusal guard exist, pass, and be watched by nothing
# — nobody is obliged to add it. So the other direction is checked first: every test marked
# `@refusal_guard` (cloud_file_storage/tests/markers.py) must appear in REQUIRED.
#
# The marker is per-test rather than per-class because `TestApplyRefusals` and
# `TestBackupVerification` both deliberately mix acceptance cases in with their refusals; a
# class-membership rule would drag those in and make this gate demand the wrong things.
#
# **What escapes this**: an unmarked refusal guard. The decorator is what makes a test
# visible here, so forgetting BOTH the marker and the REQUIRED entry is still possible. What
# is closed is the realistic half — marking a guard is local to the test being written, while
# editing a CI script is a separate act in a separate directory that nothing prompts.
tests_dir = workspace / "cloud_file_storage" / "tests"
marked = {}
for path in sorted(tests_dir.rglob("*.py")) if tests_dir.is_dir() else []:
	module = ".".join(path.relative_to(workspace).with_suffix("").parts)
	try:
		tree = ast.parse(path.read_text(encoding="utf-8"))
	except SyntaxError as exc:
		print(f"::error::could not parse {path} while looking for refusal guards: {exc}")
		raise SystemExit(1)
	for node in ast.walk(tree):
		if not isinstance(node, ast.ClassDef):
			continue
		for child in node.body:
			if not isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
				continue
			names = {getattr(d, "id", None) or getattr(d, "attr", None) for d in child.decorator_list}
			if "refusal_guard" in names:
				marked[f"{module}.{node.name}.{child.name}"] = str(path)

unlisted = sorted(set(marked) - set(REQUIRED.values()))
for identity in unlisted:
	print(f"::error::{identity} is marked @refusal_guard but is not in this gate's REQUIRED map")
	print("::error::(add it, with a label naming the amendment or finding it closes)")

# A workspace that carries the marker module must carry the guards that use it. Without this
# floor an empty or missing tests tree would satisfy the subset check vacuously.
floor_expected = (tests_dir / "markers.py").is_file()
if floor_expected and len(marked) < 50:
	print(f"::error::only {len(marked)} tests are marked @refusal_guard; the walk found almost none")
	print("::error::(did the tests tree move, or did the decorator get renamed?)")
	raise SystemExit(1)

if unlisted:
	raise SystemExit(1)

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

	# A `setUpClass` that raises SkipTest is reported as ONE synthetic case per class, with
	# an EMPTY classname and the module path inside the name. Matching only on classname
	# would report "absent from the report" and bury the reason.
	if not classname and any(module in name for module in MODULES):
		skip_node = case.find("skipped")
		reason = (skip_node.get("message") or "no reason given").strip() if skip_node is not None else ""
		class_level_skips.append((name, reason))
		continue

	# The skip sweep below is scoped to the backup modules; the REQUIRED lookup is not,
	# because one named guard lives in the contract suite. Recording any testcase that is
	# named in REQUIRED keeps the two concerns separate — adding the whole contract module
	# to MODULES would make this gate fail on a ref where a C-test legitimately skips, which
	# is the contract gate's job to judge, not this one's.
	if identity not in REQUIRED.values() and not any(
		identity.startswith(module) for module in MODULES
	):
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
	print(f"::error::a whole backup class was SKIPPED: {name} -> {reason}")
	status = 1

if not seen:
	if class_level_skips:
		raise SystemExit(1)
	print("::error::no backup testcase appears in the junit report at all")
	print(f"::error::(expected classnames under {', '.join(MODULES)} - did a module move?)")
	raise SystemExit(1)

for label, identity in sorted(REQUIRED.items()):
	outcome = seen.get(identity)
	if outcome is None:
		print(f"::error::{label} did not run: {identity} is absent from the report")
		status = 1
	elif outcome[0] == "skipped":
		print(f"::error::{label} was SKIPPED: {identity} -> {outcome[1]}")
		status = 1
	elif outcome[0] == "failed":
		print(f"::error::{label} failed: {identity}")
		status = 1

# A refusal guard that skipped is worth nothing, wherever in these modules it lives.
for identity, (outcome, reason) in sorted(seen.items()):
	if outcome == "skipped" and identity not in REQUIRED.values():
		print(f"::error::backup test SKIPPED: {identity} -> {reason}")
		status = 1

if status == 0:
	print(f"backup completeness OK: {len(REQUIRED)} refusal guards executed, 0 skipped")

raise SystemExit(status)
PY
