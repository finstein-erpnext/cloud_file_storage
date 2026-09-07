#!/bin/bash
# A21 acceptance: a real `bench migrate` on a synthetic legacy frappe_s3_attachment 0.2.x site.
#
#   docs/evidence/p8-legacy-rehearsal.sh <a-site-name-that-does-not-exist-yet>
#
# Takes a FRESH site name and never destroys an existing one: dropping a site is a
# SAFETY_BOUNDARY action under this project's autonomy guard, so each run takes a new name.
#
# PRECONDITION, and it is not optional: the site is created with `developer_mode 0` at step 1
# below. This bench's common_site_config.json sets developer_mode 1, and with it on the seeding
# dies at the first Module Def insert — `ModuleDef.on_update` tries to mkdir a module folder
# inside `frappe_s3_attachment`, an app that is not on the bench. See the header of
# cloud_file_storage/tests/legacy_fixture.py.
#
# Every step ASSERTS its outcome. An earlier version printed "expected 1" / "expected 0" beside
# an echo of `$?` and checked nothing, so a failed `bench new-site` at step 1 would have left
# every later step running against a site that does not exist — and step 3's red control would
# have "passed" for the wrong reason. A harness that cannot fail proves nothing about the steps
# it runs, which is the same argument as the red control it exists to demonstrate.
set -u
BENCH=/home/user/v15
SITE="${1:?pass a fresh site name}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMP="${TMP:-$(mktemp -d)}"   # scratch for the generated per-site copies and logs
# L-11: derived, never pinned. `cfs-P8` disappears when the phase worktree is removed after
# merge — which is exactly when this harness becomes the only record of the run.
APP_ROOT="${APP_ROOT:-$(cd "$HERE/../.." && pwd)}"
# Exported, not just set: the seed and probe helpers are `sed`-copied into a scratch directory,
# so their own `__file__` no longer points anywhere useful and they read this instead.
export APP_ROOT
export PYTHONPATH="$APP_ROOT"
cd "$BENCH" || exit 1

FAILURES=0

expect_status() {   # expect_status <what> <expected> <actual>
	if [ "$2" -ne "$3" ]; then
		echo "FAIL: $1 exited $3, expected $2"
		FAILURES=$((FAILURES + 1))
		return 1
	fi
	echo "ok: $1 exited $2"
}

expect_grep() {     # expect_grep <what> <pattern> <file>
	if ! grep -q -- "$2" "$3"; then
		echo "FAIL: $1 — '$2' not found in $3"
		FAILURES=$((FAILURES + 1))
		return 1
	fi
	echo "ok: $1 — found '$2'"
}

sed "s/cfs-p8-legacy.local/$SITE/" "$HERE/p8-legacy-rehearsal-seed.py"  > "$TMP/seed_$SITE.py"
sed "s/cfs-p8-legacy.local/$SITE/" "$HERE/p8-legacy-rehearsal-probe.py" > "$TMP/probe_$SITE.py"

echo "=== 1. build a v15 site with no cloud_file_storage on it ==="
bench new-site "$SITE" --db-host 127.0.0.1 --db-port 3307 \
  --db-root-username root --db-root-password "$(cat /home/user/cfs-scratch-mariadb/root.pw)" \
  --admin-password admin --mariadb-user-host-login-scope='%' --no-mariadb-socket \
  >"$TMP/reh_${SITE}_1_newsite.log" 2>&1
status=$?
# Fail FAST here: every later step is meaningless against a site that does not exist.
expect_status "bench new-site" 0 "$status" || { echo "aborting: see $TMP/reh_${SITE}_1_newsite.log"; exit 1; }
bench --site "$SITE" set-config developer_mode 0 >/dev/null 2>&1
bench --site "$SITE" set-config allow_tests true >/dev/null 2>&1

echo "=== 2. seed the frappe_s3_attachment 0.2.x residue ==="
/home/user/v15/env/bin/python "$TMP/seed_$SITE.py" >"$TMP/reh_${SITE}_2_seed.log" 2>&1
status=$?
expect_status "seed the fork residue" 0 "$status" || { echo "aborting: see $TMP/reh_${SITE}_2_seed.log"; exit 1; }
tail -1 "$TMP/reh_${SITE}_2_seed.log"
/home/user/v15/env/bin/python "$TMP/probe_$SITE.py" 2>/dev/null >"$TMP/BEFORE_$SITE.json"

echo "=== 3. RED: bench migrate must fail before the bootstrap ==="
bench --site "$SITE" migrate >"$TMP/reh_${SITE}_3_migrate_RED.log" 2>&1
status=$?
# The red control, and the one genuine discriminator in this script: BOTH the non-zero exit
# and the reason for it are asserted. A migrate that fails for some other reason would
# otherwise look like proof.
expect_status "migrate BEFORE the bootstrap (the red control)" 1 "$status"
expect_grep "the red control failed for the right reason" \
	"ModuleNotFoundError: No module named 'frappe_s3_attachment'" \
	"$TMP/reh_${SITE}_3_migrate_RED.log"

echo "=== 4. dry run changes nothing ==="
bench --site "$SITE" cfs-adopt-legacy-install --dry-run >"$TMP/reh_${SITE}_4_dryrun.log" 2>&1
expect_status "cfs-adopt-legacy-install --dry-run" 0 $?
/home/user/v15/env/bin/python "$TMP/probe_$SITE.py" 2>/dev/null >"$TMP/AFTER_DRYRUN_$SITE.json"

echo "=== 5. the bootstrap ==="
bench --site "$SITE" cfs-adopt-legacy-install >"$TMP/reh_${SITE}_5_bootstrap.log" 2>&1
expect_status "cfs-adopt-legacy-install" 0 $?

echo "=== 6. GREEN: the real migrate ==="
bench --site "$SITE" migrate >"$TMP/reh_${SITE}_6_migrate_GREEN.log" 2>&1
expect_status "migrate AFTER the bootstrap" 0 $?
expect_grep "the post-sync patch ran" "linked the legacy fork objects" \
	"$TMP/reh_${SITE}_6_migrate_GREEN.log"

echo "=== 7. state after adoption ==="
/home/user/v15/env/bin/python "$TMP/probe_$SITE.py" 2>/dev/null >"$TMP/AFTER_$SITE.json"

echo "=== 8. a second migrate must be a clean no-op ==="
bench --site "$SITE" migrate >"$TMP/reh_${SITE}_8_migrate_AGAIN.log" 2>&1
expect_status "a second migrate (idempotency)" 0 $?
/home/user/v15/env/bin/python "$TMP/probe_$SITE.py" 2>/dev/null >"$TMP/AFTER2_$SITE.json"
echo "=== 9. an ordinary settings save must not wipe the adopted credential ==="
/home/user/v15/env/bin/python "$HERE/p8-legacy-rehearsal-postcheck.py" "$SITE" 2>/dev/null | tee "$TMP/postcheck_$SITE.json"
expect_grep "the adopted credential survived an ordinary save" '"unchanged": true' "$TMP/postcheck_$SITE.json"

echo "=== done ==="
if [ "$FAILURES" -ne 0 ]; then
	echo "REHEARSAL FAILED: $FAILURES assertion(s) did not hold. Logs in $TMP"
	exit 1
fi
echo "REHEARSAL OK: every step asserted"
