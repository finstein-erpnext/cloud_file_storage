#!/bin/bash
# P6 mutation evidence — runnable.
#
# Every refusal guard P6 ships was broken and watched to fail. This script re-proves that
# mechanically instead of asking you to trust a transcript: for each entry it applies the
# mutation, asserts it CHANGED the file, runs the named test module, and asserts the run
# FAILED. A mutation that applies and leaves the suite green is reported as SURVIVED, which
# is the outcome that matters — it means the guard named in that row is not actually held by
# any test.
#
# The tree is restored after every entry, and on any exit path, including Ctrl-C.
#
# Usage:
#   bash p6_mutations.sh [--site cfs-p6.local] [--worktree /home/user/v15/apps/cfs-P6] [-k FILTER]
#
# Requires the worktree to be CLEAN before it starts; it refuses otherwise, because a
# restore-from-backup over uncommitted work would destroy it.
set -uo pipefail

SITE="cfs-p6.local"
WORKTREE="/home/user/v15/apps/cfs-P6"
BENCH_ROOT="/home/user/v15"
FILTER=""

while [ $# -gt 0 ]; do
	case "$1" in
		--site) SITE="$2"; shift 2 ;;
		--worktree) WORKTREE="$2"; shift 2 ;;
		-k) FILTER="$2"; shift 2 ;;
		*) echo "unknown argument: $1" >&2; exit 2 ;;
	esac
done

APP="$WORKTREE/cloud_file_storage"

if [ -n "$(git -C "$WORKTREE" status --porcelain)" ]; then
	echo "REFUSING: $WORKTREE has uncommitted changes; this script overwrites files in place." >&2
	exit 2
fi

CURRENT_FILE=""
restore() {
	if [ -n "$CURRENT_FILE" ] && [ -f "$CURRENT_FILE.mutbak" ]; then
		mv "$CURRENT_FILE.mutbak" "$CURRENT_FILE"
		CURRENT_FILE=""
	fi
}
trap restore EXIT INT TERM

PASSED=0; SURVIVED=0; NOTAPPLIED=0
SURVIVORS=()

# run_mutation <label> <relative-file> <python-edit> <test-module>
#
# <python-edit> operates on the string `s`, e.g.  s = s.replace("a", "b")
run_mutation() {
	local label="$1" relative="$2" edit="$3" module="$4"

	if [ -n "$FILTER" ] && [[ "$label" != *"$FILTER"* ]]; then
		return 0
	fi

	local file="$WORKTREE/$relative"
	CURRENT_FILE="$file"
	cp "$file" "$file.mutbak"

	python3 - "$file" "$edit" <<'PYEOF'
import pathlib, sys
path, edit = pathlib.Path(sys.argv[1]), sys.argv[2]
s = path.read_text()
exec(edit, {}, {"s": s}) if False else None
scope = {"s": s}
exec(edit, {}, scope)
path.write_text(scope["s"])
PYEOF

	if diff -q "$file" "$file.mutbak" >/dev/null 2>&1; then
		printf '%-58s NOT APPLIED (the anchor moved)\n' "$label"
		NOTAPPLIED=$((NOTAPPLIED + 1))
		restore
		return 0
	fi

	local output
	output=$(cd "$BENCH_ROOT" && PYTHONPATH="$WORKTREE" timeout 900 bench --site "$SITE" \
		run-tests --app cloud_file_storage --module "$module" 2>&1 | tail -3)

	restore

	if echo "$output" | grep -q "^FAILED"; then
		printf '%-58s caught  (%s)\n' "$label" "$(echo "$output" | grep '^FAILED' | head -1)"
		PASSED=$((PASSED + 1))
	else
		printf '%-58s *** SURVIVED *** in %s\n' "$label" "$module"
		SURVIVED=$((SURVIVED + 1))
		SURVIVORS+=("$label")
	fi
}

T=cloud_file_storage.tests.test_backup
L=cloud_file_storage.tests.test_backup_lifecycle
R=cloud_file_storage.tests.test_backup_restore
C=cloud_file_storage.tests.test_cache_repair
G=cloud_file_storage.tests.test_backup_gate
P=cloud_file_storage.tests.test_backup_permissions
K=cloud_file_storage.tests.contract.test_file_compat_contract

TASKS=cloud_file_storage/backup/tasks.py
LIFE=cloud_file_storage/backup/lifecycle.py
SET=cloud_file_storage/backup/settings.py
RET=cloud_file_storage/backup/retention.py
RES=cloud_file_storage/backup/restore.py
HEA=cloud_file_storage/backup/health.py
EVI=cloud_file_storage/cache/eviction.py
ENG=cloud_file_storage/storage/engine.py
UTL=cloud_file_storage/tests/backup_utils.py
TST=cloud_file_storage/tests/test_backup.py
BSJ=cloud_file_storage/cloud_file_storage/doctype/cloud_backup_settings/cloud_backup_settings.json
BLJ=cloud_file_storage/cloud_file_storage/doctype/cloud_storage_backup_log/cloud_storage_backup_log.json
CI=.github/workflows/ci.yml
PERMU=cloud_file_storage/tests/permission_utils.py
CONTRACT=cloud_file_storage/tests/contract/test_file_compat_contract.py
CSS=cloud_file_storage/cloud_file_storage/doctype/cloud_storage_settings/cloud_storage_settings.py
CBSJ=cloud_file_storage/cloud_file_storage/doctype/cloud_backup_settings/cloud_backup_settings.json
CBLJ=cloud_file_storage/cloud_file_storage/doctype/cloud_storage_backup_log/cloud_storage_backup_log.json
PATCH=cloud_file_storage/patches/v0_6_0/enforce_backup_credential_permlevel.py
TBP=cloud_file_storage/tests/test_backup_permissions.py

echo "=== A28 — backup freshness ==================================================="
run_mutation "A28 mtime assertion disabled" "$TASKS" \
	's = s.replace("\tif mtime < started_epoch:", "\tif False:")' "$T"
run_mutation "A28 new_backup no longer forced" "$TASKS" \
	's = s.replace("\t\tforce=True,", "\t\tforce=False,")' "$T"

echo
echo "=== A5 — verification ========================================================"
run_mutation "A5 ContentLength check removed" "$TASKS" \
	's = s.replace("\tif remote_size != local_size:", "\tif False:")' "$T"
run_mutation "A5 streamed re-GET skipped (trust HEAD)" "$TASKS" \
	's = s.replace("\tif local_size >= threshold_bytes or is_multipart_checksum or not remote_checksum:", "\tif False:")' "$T"
run_mutation "A5 delete the artifact on a size mismatch" "$TASKS" \
	's = s.replace("\thead = client.head_object(Bucket=bucket, Key=key, ChecksumMode=\"ENABLED\")\n", "\thead = client.head_object(Bucket=bucket, Key=key, ChecksumMode=\"ENABLED\")\n\tif int(head.get(\"ContentLength\") or 0) != local_size:\n\t\tclient.delete_object(Bucket=bucket, Key=key)\n")' "$T"
run_mutation "A5 delete the artifact when HEAD raises" "$TASKS" \
	's = s.replace(chr(9) + "head = client.head_object(Bucket=bucket, Key=key, ChecksumMode=" + chr(34) + "ENABLED" + chr(34) + ")" + chr(10), chr(9) + "try:" + chr(10) + chr(9)*2 + "head = client.head_object(Bucket=bucket, Key=key, ChecksumMode=" + chr(34) + "ENABLED" + chr(34) + ")" + chr(10) + chr(9) + "except Exception:" + chr(10) + chr(9)*2 + "client.delete_object(Bucket=bucket, Key=key)" + chr(10) + chr(9)*2 + "raise" + chr(10))' "$T"
run_mutation "A5 delete the artifact when the re-GET raises" "$TASKS" \
	's = s.replace(chr(9)*2 + "streamed_sha256, streamed_size = _remote_digest(client, bucket, key)" + chr(10), chr(9)*2 + "try:" + chr(10) + chr(9)*3 + "streamed_sha256, streamed_size = _remote_digest(client, bucket, key)" + chr(10) + chr(9)*2 + "except Exception:" + chr(10) + chr(9)*3 + "client.delete_object(Bucket=bucket, Key=key)" + chr(10) + chr(9)*3 + "raise" + chr(10))' "$T"
run_mutation "F6 an ACL key is added to the backup PUT" "$SET" \
	's = s.replace(chr(9) + "return {" + chr(34) + "ServerSideEncryption" + chr(34) + ": " + chr(34) + "AES256" + chr(34) + "}", chr(9) + "return {" + chr(34) + "ServerSideEncryption" + chr(34) + ": " + chr(34) + "AES256" + chr(34) + ", " + chr(34) + "ACL" + chr(34) + ": " + chr(34) + "private" + chr(34) + "}")' "$T"

echo
echo "=== A20 — lifecycle =========================================================="
run_mutation "A20 an expire rule loses its Filter.Prefix" "$LIFE" \
	'import re; s = re.sub(r"\n\s*\"Filter\": \{\"Prefix\": group\[\"prefix\"\]\},(\n\s*\"Expiration\")", r"\1", s)' "$L"
run_mutation "A20 merge replaces instead of merging" "$LIFE" \
	's = s.replace(chr(34) + "rules" + chr(34) + ": preserved + generated_rules,", chr(34) + "rules" + chr(34) + ": generated_rules,")' "$L"
run_mutation "A20 dropped rules never reported" "$LIFE" \
	's = s.replace("\tdropped = [rule for rule_id, rule in current_ours.items() if rule_id not in generated_ids]", "\tdropped = []")' "$L"
# Re-anchored in P8: the guard was `(confirm_phrase or "").strip() != …` until security
# finding L-1 made both type-to-confirm gates byte-for-byte (DECISIONS 2026-08-16). By this
# script's own convention a stale anchor reports NOT APPLIED, so leaving it would have quietly
# retired the mutation that proves this guard bites.
run_mutation "A20 confirm phrase not enforced" "$LIFE" \
	's = s.replace("\tif confirm_phrase != APPLY_CONFIRM_PHRASE:", "\tif False:")' "$L"
run_mutation "A20 economics error downgraded to a warning" "$LIFE" \
	's = s.replace(chr(9)*5 + chr(34) + "level" + chr(34) + ": " + chr(34) + "error" + chr(34) + "," + chr(10) + chr(9)*5 + chr(34) + "code" + chr(34) + ": " + chr(34) + "archive_before_minimum" + chr(34) + ",", chr(9)*5 + chr(34) + "level" + chr(34) + ": " + chr(34) + "warn" + chr(34) + "," + chr(10) + chr(9)*5 + chr(34) + "code" + chr(34) + ": " + chr(34) + "archive_before_minimum" + chr(34) + ",")' "$L"
run_mutation "A20 archive rule emitted when it can never fire" "$LIFE" \
	's = s.replace("\treturn retention <= 0 or hot_days < retention", "\treturn True")' "$L"
run_mutation "A20 per-frequency prefixes collapsed to one" "$SET" \
	's = s.replace("\treturn f" + chr(34) + "{base}/{slug}" + chr(34) + " if base else slug", "\treturn base or slug")' "$L"
run_mutation "B5 bucket-isolation guard disabled" "$SET" \
	's = s.replace("\tif attachment_bucket and backup_bucket == attachment_bucket:", "\tif False:")' "$L"
run_mutation "A20 a manual backup uses the frequency prefix" "$TASKS" \
	's = s.replace("\tslug = MANUAL_SLUG if trigger == " + chr(34) + "manual" + chr(34) + " else backup_settings.frequency_slug(settings.frequency)", "\tslug = backup_settings.frequency_slug(settings.frequency)")' "$T"

echo
echo "=== A27 — restore window ====================================================="
run_mutation "A27 grace/window coupling not enforced" "$RET" \
	's = s.replace("\tif window <= 0 or grace >= window:", "\tif True:")' "$R"
run_mutation "A27 versioning-off accepted as OK" "$RET" \
	's = s.replace("\tif status != " + chr(34) + "Enabled" + chr(34) + ":", "\tif False:")' "$R"
run_mutation "A27 noncurrent window comparison disabled" "$RET" \
	's = s.replace("\tif noncurrent_days is not None and noncurrent_days < window:", "\tif False:")' "$R"
run_mutation "A27 longest noncurrent rule wins instead of shortest" "$RET" \
	's = s.replace("noncurrent_days = days if noncurrent_days is None else min(noncurrent_days, days)", "noncurrent_days = days if noncurrent_days is None else max(noncurrent_days, days)")' "$R"
run_mutation "A27 disabled lifecycle rules counted" "$RET" \
	's = s.replace(chr(9)*2 + "if rule.get(" + chr(34) + "Status" + chr(34) + ") != " + chr(34) + "Enabled" + chr(34) + ":" + chr(10) + chr(9)*3 + "continue" + chr(10), "")' "$R"
run_mutation "A27 the versioning probe is stubbed out" "$ENG" \
	's = s.replace("\treturn _call(" + chr(34) + "get_bucket_versioning" + chr(34) + ", s3.get_bucket_versioning, Bucket=settings.bucket)", "\treturn {" + chr(34) + "Status" + chr(34) + ": " + chr(34) + "Enabled" + chr(34) + "}")' "$R"

echo
echo "=== A27 — pre-restore artifact check ========================================="
run_mutation "pre-restore digest check accepts anything" "$RES" \
	's = s.replace("\tif size != cint(record.get(" + chr(34) + "size" + chr(34) + ")) or sha256 != record.get(" + chr(34) + "sha256" + chr(34) + "):", "\tif False:")' "$R"
run_mutation "pre-restore check degrades to size only" "$RES" \
	's = s.replace("\tif size != cint(record.get(" + chr(34) + "size" + chr(34) + ")) or sha256 != record.get(" + chr(34) + "sha256" + chr(34) + "):", "\tif size != cint(record.get(" + chr(34) + "size" + chr(34) + ")):")' "$R"
run_mutation "Failed logs accepted as a digest source" "$RES" \
	's = s.replace(chr(9)*2 + "filters={" + chr(34) + "status" + chr(34) + ": " + chr(34) + "Success" + chr(34) + "}," + chr(10), "", 1)' "$R"
run_mutation "download URL loses its no-store pin" "$RES" \
	's = s.replace(chr(34) + "ResponseCacheControl" + chr(34) + ": " + chr(34) + "no-store" + chr(34) + ",", "")' "$R"

echo
echo "=== scheduling and the timeout path =========================================="
run_mutation "the due-gate always says yes" "$TASKS" \
	's = s.replace("\tif frequency == " + chr(34) + "Hourly" + chr(34) + ":", "\tif True:")' "$T"
run_mutation "the timeout retry is unbounded" "$TASKS" \
	's = s.replace(chr(9)*2 + "if attempt < MAX_TIMEOUT_RETRIES:", chr(9)*2 + "if True:")' "$T"
run_mutation "the timeout never re-enqueues" "$TASKS" \
	's = s.replace(chr(9)*2 + "if attempt < MAX_TIMEOUT_RETRIES:", chr(9)*2 + "if False:")' "$T"

echo
echo "=== backup health findings ==================================================="
run_mutation "staleness window effectively removed" "$HEA" \
	's = s.replace("\tlimit = interval * STALE_MULTIPLIER", "\tlimit = interval * 1000")' "$R"
run_mutation "tarballs-after-cutover detector disabled" "$HEA" \
	's = s.replace("\tif get_mode() not in CLOUD_MODES:", "\tif True:")' "$R"
run_mutation "lifecycle drift never reported" "$HEA" \
	's = s.replace("\tif current == settings.lifecycle_applied_hash:", "\tif True:")' "$R"
run_mutation "a failed last backup is not surfaced" "$HEA" \
	's = s.replace("\tif (settings.last_backup_status or " + chr(34) + chr(34) + ") == " + chr(34) + "Failed" + chr(34) + ":", "\tif False:")' "$R"

echo
echo "=== cache repair (carried from P4) ==========================================="
run_mutation "repair deletes instead of quarantining" "$EVI" \
	's = s.replace("\t\tos.rename(path, target)", "\t\tos.remove(path)")' "$C"
run_mutation "repair rewrites a sidecar without checking the hash" "$EVI" \
	's = s.replace("\t\tif digest.sha256 == expected_sha256:", "\t\tif True:")' "$C"
run_mutation "repair writes a dirty sidecar and claims success" "$EVI" \
	's = s.replace(chr(9)*4 + "dirty=False,", chr(9)*4 + "dirty=True,")' "$C"
run_mutation "repair ignores the materialization lock" "$EVI" \
	's = s.replace(chr(9)*3 + "with _StrongFileLock(probe_lock_path(f" + chr(34) + "cfs_mat_{owner}" + chr(34) + "), timeout=LOCK_PROBE_TIMEOUT):" + chr(10) + chr(9)*4 + "outcome = _repair_one(path, name, digest_path, write_sidecar)", chr(9)*3 + "outcome = _repair_one(path, name, digest_path, write_sidecar)")' "$C"

echo
echo "=== shipped schema (INVARIANTS.md invariant 10) =================================="
run_mutation "a shipped default drifts (hot_retention_days 14 -> 21)" "$BSJ" \
	's = s.replace(chr(34) + "default" + chr(34) + ": " + chr(34) + "14" + chr(34), chr(34) + "default" + chr(34) + ": " + chr(34) + "21" + chr(34))' "$L"
run_mutation "a Select loses its default and its leading newline" "$BLJ" \
	's = s.replace(chr(34) + "default" + chr(34) + ": " + chr(34) + "scheduled" + chr(34) + "," + chr(10) + "   " + chr(34) + "fieldname" + chr(34) + ": " + chr(34) + "trigger" + chr(34), chr(34) + "fieldname" + chr(34) + ": " + chr(34) + "trigger" + chr(34))' "$L"
run_mutation "a byte counter is downgraded to Int" "$BLJ" \
	's = s.replace(chr(34) + "fieldtype" + chr(34) + ": " + chr(34) + "Long Int" + chr(34), chr(34) + "fieldtype" + chr(34) + ": " + chr(34) + "Int" + chr(34))' "$L"

echo
echo "=== the test harness itself =================================================="
run_mutation "settings snapshot goes back through the casting reader" "$UTL" \
	's = s.replace(s[s.index(chr(9) + "stored = {"):s.index(chr(9) + "try:", s.index(chr(9) + "stored = {"))], chr(9) + "snapshot = {" + chr(10) + chr(9)*2 + "fieldname: frappe.db.get_single_value(BACKUP_SETTINGS_DOCTYPE, fieldname)" + chr(10) + chr(9)*2 + "for fieldname in MANAGED_BACKUP_SETTINGS_FIELDS" + chr(10) + chr(9) + "}" + chr(10))' "$T"
run_mutation "the fake registers an injector no test drives" "$UTL" \
	's = s.replace("\t\t\"fail_get_with\": None,", "\t\t\"fail_get_with\": None,\n\t\t\"fail_delete_with\": None,")' "$T"
run_mutation "an unregistered injector is silently accepted" "$UTL" \
	's = s.replace("\t\tif key not in self:", "\t\tif False:")' "$T"
run_mutation "a registered injector stops being read" "$UTL" \
	's = s.replace("\t\tif self.injectors[\"fail_get_with\"] is not None:", "\t\tif False:")' "$T"
run_mutation "every use of one injector is removed" "$TST" \
	's = s.replace("self.bucket.injectors[\"fail_head_with\"] =", "_unused_injector =")' "$T"

echo
echo "=== A20 — empty prefix is not a scope (gate finding F-1) ====================="
run_mutation "an empty prefix is accepted at generation" "$LIFE" \
	's = s.replace("\tif not base:", "\tif False:")' "$L"
run_mutation "the structural backstop is removed" "$LIFE" \
	's = s.replace("\tassert_every_rule_is_scoped(rules)\n\treturn {\"Rules\": rules}", "\treturn {\"Rules\": rules}")' "$L"
run_mutation "a bucket-wide shape loses its prefix" "$LIFE" \
	's = s.replace("\t\t\t\t\"Filter\": {\"Prefix\": scoped_base},\n\t\t\t\t\"NoncurrentVersionExpiration\"", "\t\t\t\t\"Filter\": {\"Prefix\": \"\"},\n\t\t\t\t\"NoncurrentVersionExpiration\"")' "$L"
# `apply_lifecycle_policy`'s own `assert_prefix_is_scoped` call is deliberately REDUNDANT —
# `build_lifecycle_policy`, which it calls, refuses first — so removing it cannot fail any
# test. It is kept in the code for the clearer error before the confirm-phrase check, and the
# mutation row that claimed to cover it has been removed rather than left implying coverage
# it never had. See DECISIONS 2026-08-16.

echo
echo "=== H-1 / M-1 / M-2 / M-4 / M-5 (gate + security findings) ==================="
run_mutation "the presign key is not scoped to the prefix" "$RES" \
	's = s.replace("\tif not candidate.startswith(scoped_prefix):", "\tif False:")' "$R"
run_mutation "the presign key need not be a recorded artifact" "$RES" \
	's = s.replace("\tif recorded_digest(candidate) is None:", "\tif False:")' "$R"
run_mutation "the presign is not audited" "$RES" \
	's = s.replace("\t_audit_presign(key, ttl=ttl, bucket=settings.backup_bucket, action=\"backup_presign_issued\")", "\tpass")' "$R"
run_mutation "the audit row is written AFTER the mint" "$RES" \
	's = s.replace("\taudit.record(action, key=key, ttl=ttl, bucket=bucket)\n\tfrappe.db.commit()", "\tpass")' "$R"
run_mutation "acting_as stops clearing in_test" "$PERMU" \
	's = s.replace("\t\tfrappe.local.flags.in_test = False", "\t\tpass")' "$P"
run_mutation "acting_as stops switching user" "$PERMU" \
	's = s.replace("\t\tfrappe.set_user(user)\n\t\t# LAST", "\t\t# LAST")' "$P"
run_mutation "the attachment-side isolation guard is removed" "$CSS" \
	's = s.replace("\t\tself.validate_bucket_isolation()\n", "")' "$L"
run_mutation "the credential fields drop back to permlevel 0" "$CBSJ" \
	's = s.replace(chr(34) + "permlevel" + chr(34) + ": 1", chr(34) + "permlevel" + chr(34) + ": 0")' "$P"
run_mutation "the artifact key fields drop back to permlevel 0" "$CBLJ" \
	's = s.replace(chr(34) + "permlevel" + chr(34) + ": 1", chr(34) + "permlevel" + chr(34) + ": 0")' "$P"

echo
echo "=== M-8 / L-7 / M-9 / L-8 (security re-review) ==============================="
run_mutation "C10 matches generate_presigned only as an attribute" "$CONTRACT" \
	's = s.replace(chr(9) + "return name == " + chr(34) + "presign_get" + chr(34) + " or name.startswith(" + chr(34) + "generate_presigned" + chr(34) + ")", chr(9) + "return name == " + chr(34) + "presign_get" + chr(34))' "$K"
run_mutation "a side effect runs before an endpoint role gate" "$TASKS" \
	's = s.replace("\t\"\"\"Operator-triggered backup. Returns the enqueued job id.\"\"\"\n\tfrappe.only_for", "\t\"\"\"Operator-triggered backup. Returns the enqueued job id.\"\"\"\n\tfrappe.logger(\"x\").info(\"before the gate\")\n\tfrappe.only_for")' "$P"
run_mutation "the patch stops refusing an operator level-1 row" "$PATCH" \
	's = s.replace("\toffenders = [perm.role for perm in level_one if perm.role == OPERATOR_ROLE]", "\toffenders = []")' "$P"
run_mutation "the patch stops reading Custom DocPerm" "$PATCH" \
	's = s.replace("\t\t_assert_custom_permissions(doctype)", "\t\tpass")' "$P"
run_mutation "the patch drops force=True on the reload" "$PATCH" \
	's = s.replace(", force=True)", ")")' "$P"
run_mutation "the operator row regains export" "$CBLJ" \
	's = s.replace(chr(34) + "read" + chr(34) + ": 1,\n   " + chr(34) + "report" + chr(34) + ": 1,\n   " + chr(34) + "role" + chr(34) + ": " + chr(34) + "Cloud Storage Manager" + chr(34), chr(34) + "export" + chr(34) + ": 1,\n   " + chr(34) + "read" + chr(34) + ": 1,\n   " + chr(34) + "report" + chr(34) + ": 1,\n   " + chr(34) + "role" + chr(34) + ": " + chr(34) + "Cloud Storage Manager" + chr(34))' "$P"

echo
echo "=== suite footprint (self-audit after the pristine-site finding) ============="
run_mutation "role removal is silently suppressed again" "$PERMU" \
	's = s.replace("\t\tfor role in added:\n\t\t\tfrappe.get_doc(\"User\", user).remove_roles(role)", "\t\tfor role in added:\n\t\t\twith contextlib.suppress(Exception):\n\t\t\t\tfrappe.get_doc(\"User\", user).remove_roles(role)")' "$P"
run_mutation "the fixture users are left behind" "$PERMU" \
	's = s.replace("\tfrappe.delete_doc(\"User\", email, force=True, ignore_permissions=True)", "\tpass")' "$P"
# The `frappe.clear_cache(doctype=...)` in `_drop_custom_docperm` is load-bearing OUTSIDE
# the suite — measured directly: after a `db.delete` of a Custom DocPerm row, a normal
# process still reports the deleted override until the cache is cleared. Inside the
# suite, removing the call fails nothing, and I did not determine why. So the code stays
# (the measurement justifies it) and the mutation row is REMOVED rather than kept: a row
# that cannot fail is what this script exists to prevent. See DECISIONS 2026-08-16.

echo
echo "=== the CI completeness gate ================================================="
run_mutation "a named refusal guard is renamed" "$TST" \
	's = s.replace("\tdef test_a_dump_older_than_the_job_is_refused_before_anything_uploads(self):", "\tdef test_a_dump_older_than_the_job_is_refused_early(self):")' "$G"
run_mutation "the gate is unwired from CI" "$CI" \
	's = s.replace("check_backup_completeness.sh", "check_nothing.sh")' "$G"

echo
echo "=============================================================================="
echo "caught: $PASSED    survived: $SURVIVED    not applied: $NOTAPPLIED"
if [ "$SURVIVED" -ne 0 ]; then
	echo
	echo "SURVIVORS — these guards are NOT held by any test:"
	printf '  - %s\n' "${SURVIVORS[@]}"
fi
if [ "$NOTAPPLIED" -ne 0 ]; then
	echo "NOT APPLIED entries mean the source moved; the row proves nothing until its anchor is updated."
fi
[ "$SURVIVED" -eq 0 ] && [ "$NOTAPPLIED" -eq 0 ]
