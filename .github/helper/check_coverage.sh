#!/bin/bash
# Combined coverage gate (docs/PLAN.md §B, P1: "coverage + contract-completeness gates").
#
# THE CONTRACT, and why this script combines rather than checking each job.
# ------------------------------------------------------------------------
# The 90% floor is a measured baseline: P1 measured 94% over `cloud_file_storage/` on a fresh
# scratch site running **123 unit tests + 4 MinIO integration tests** — one corpus, one number.
# The floor was set just below that measurement.
#
# CI then split that corpus across jobs that are *deliberately partial*: three frappe refs run
# the app suite, a separate job runs the real-S3 tests, another runs L3 with erpnext installed.
# Applying a whole-corpus floor to each shard asks every fragment to clear a bar that was
# measured on the whole. On `dcb2ada` TWO of the three matrix refs reported **76%** and failed
# (the third, v15.16.0, never reached a coverage number -- it died with 414 AttributeErrors).
#
# **CORRECTION, from the gate's first complete run.** An earlier version of this header added
# "while the same tree measures 92-94% locally", and concluded the gate was simply misapplied.
# The combining design is still right -- a floor measured over one corpus belongs on the union,
# not on deliberately partial shards -- but that justification was FALSE. On `2df3bbb` the gate
# combined all five shards and reported **76%**, and the same figure reproduces on the bench:
# 8379 statements, ~2007 missed. The "92-94%" was a P1-era measurement over 123 tests against a
# far smaller app, carried forward and never re-measured.
#
# The 76% is honest and includes the test tree itself (1795 statements at 39%), of which two
# harnesses are 0% by design because the suite never runs them: `tests/rehearsal.py` (781, the
# 100k F3/F4 rehearsal) and `tests/playwright/smoke.py` (184). Product code alone is **86%**.
# Whether the denominator should exclude the test tree is an OWNER decision and is deliberately
# NOT made here -- changing the include/omit or the floor to obtain green is exactly what this
# gate exists to prevent.
#
# So: shards publish coverage data, this gate combines them, and the floor applies to the union.
# The floor is NOT lowered, no omit patterns are added, and no job is allowed to fail softly —
# those would all trade the gate's meaning for a green tick.
#
# EXIT CODES — deliberately distinct, because a gate that reports the wrong cause sends the next
# person to look at test coverage when the fault is the harness:
#   0  coverage >= floor
#   1  COVERAGE FAILURE   -- the combined corpus is below the floor
#   2  GATE EXECUTION FAILURE -- missing/corrupt/foreign/empty artifact, or the tool cannot run
set -uo pipefail

COVERAGE_FLOOR="${COVERAGE_FLOOR:-90}"
COVERAGE_INCLUDE="${COVERAGE_INCLUDE:-*cloud_file_storage*}"

EXIT_COVERAGE_FAILURE=1
EXIT_GATE_FAILURE=2

die_gate() {
	echo "::error::GATE EXECUTION FAILURE: $* (this is NOT a coverage shortfall)"
	exit "$EXIT_GATE_FAILURE"
}

ARTIFACTS_DIR="${COVERAGE_ARTIFACTS_DIR:-}"
[ -n "$ARTIFACTS_DIR" ] || die_gate "COVERAGE_ARTIFACTS_DIR is not set"
[ -d "$ARTIFACTS_DIR" ] || die_gate "coverage artifacts directory '$ARTIFACTS_DIR' does not exist"

# A gate with no expected shards passes vacuously on an empty directory. Requiring the list to
# be declared is what makes "a test job was skipped" a failure instead of a silent green.
EXPECTED_SHARDS="${COVERAGE_EXPECTED_SHARDS:-}"
[ -n "$EXPECTED_SHARDS" ] || die_gate "COVERAGE_EXPECTED_SHARDS is not set - a gate that expects nothing cannot fail"

EXPECTED_SHA="${COVERAGE_SHA:-}"
[ -n "$EXPECTED_SHA" ] || die_gate "COVERAGE_SHA is not set - shards could then be combined across commits"

# coverage lives in the BENCH env, not the runner's python3. Getting this wrong once reported a
# missing module as "fell below the floor" — a true-sounding verdict for an absent tool.
# Accepts a path OR a bare command name. `COVERAGE_PYTHON: python` used to be inert -- the
# `-x` test fails on a bare name, so it silently fell through to `python3`, which happened to
# be the same interpreter. Working by accident is not working.
PY="${COVERAGE_PYTHON:-${BENCH_ROOT:-$PWD}/env/bin/python}"
if [ ! -x "$PY" ]; then
	RESOLVED="$(command -v "$PY" 2>/dev/null || true)"
	[ -n "$RESOLVED" ] || RESOLVED="$(command -v python3 2>/dev/null || true)"
	PY="$RESOLVED"
fi
[ -n "$PY" ] || die_gate "no usable python interpreter"
"$PY" -c "import coverage" 2>/dev/null || die_gate "coverage is not importable by $PY"

WORK="$(mktemp -d)" || die_gate "could not create a working directory"
[ -n "$WORK" ] && [ -d "$WORK" ] || die_gate "mktemp produced no working directory"
trap 'rm -rf "$WORK"' EXIT

shard_count=0
for shard in $EXPECTED_SHARDS; do
	dir="$ARTIFACTS_DIR/coverage-$shard"
	[ -d "$dir" ] || die_gate "no coverage artifact for required shard '$shard' at '$dir' - the job did not run, was skipped, or failed to upload"

	sha_file="$dir/coverage-sha.txt"
	[ -f "$sha_file" ] || die_gate "shard '$shard' has no coverage-sha.txt - cannot prove it belongs to this commit"
	got_sha="$(tr -d '[:space:]' < "$sha_file")"
	[ "$got_sha" = "$EXPECTED_SHA" ] || die_gate "shard '$shard' was produced at '$got_sha', not the candidate '$EXPECTED_SHA'"

	data="$(find "$dir" -maxdepth 1 -type f -name '.coverage*' | sort | head -1)"
	[ -n "$data" ] || die_gate "shard '$shard' contains no .coverage data file"
	[ -s "$data" ] || die_gate "coverage data for shard '$shard' is EMPTY (0 bytes) - an empty artifact must never combine into a green"

	"$PY" "$(dirname "$0")/validate_coverage_shard.py" "$data"
	rc=$?
	case "$rc" in
		0) ;;
		3) die_gate "coverage data for shard '$shard' measured NO files - the shard ran nothing, so it cannot contribute to the union" ;;
		4) die_gate "coverage data for shard '$shard' is unreadable or corrupt" ;;
		*) die_gate "could not validate coverage data for shard '$shard' (validator exit $rc)" ;;
	esac

	cp "$data" "$WORK/.coverage.$shard" || die_gate "could not stage coverage data for shard '$shard'"
	shard_count=$((shard_count + 1))
	echo "  shard '$shard': coverage data accepted"
done

[ "$shard_count" -gt 0 ] || die_gate "no shards were staged"

# **Path aliasing, and it is what makes this gate able to return a verdict at all.**
#
# frappe's coverage helper measures `<bench>/apps/<app>`, and coverage canonicalises every
# filename to an ABSOLUTE path. So the shards say
# `/home/runner/frappe-bench/apps/cloud_file_storage/...`. This gate runs in its own job that
# has no bench — only the repository checkout — so `coverage report`, which must re-parse each
# source file to count statements, raises `NoSource` and exits 1. The gate would then map 1 to
# GATE EXECUTION FAILURE and CI would be red on every single run, with no coverage number ever
# computed. Verified by reproduction, not by reading.
#
# `[paths]` rewrites those recorded roots onto the tree that actually exists here. The first
# entry is the destination and must exist; the rest are patterns.
#
# Aliasing is applied **only** by `combine` (coverage `control.py`), which rewrites the paths
# into the combined database; `report` then reads paths that are already rewritten. So passing
# `--rcfile` to combine alone would in fact be sufficient. It is passed to both because a future
# reader reaching for `report` in isolation should get the same mapping -- not, as an earlier
# version of this comment claimed, because report needs it to work.
RCFILE_ARG=()
if [ -n "${COVERAGE_SOURCE_ROOT:-}" ]; then
	[ -d "$COVERAGE_SOURCE_ROOT" ] || die_gate "COVERAGE_SOURCE_ROOT '$COVERAGE_SOURCE_ROOT' does not exist - the shards' sources cannot be resolved"
	{
		echo "[paths]"
		echo "source ="
		echo "    $COVERAGE_SOURCE_ROOT"
		echo "    */apps/cloud_file_storage"
	} > "$WORK/.coveragerc" || die_gate "could not write the path-alias config"
	# An array, not a bare string: unquoted `$RCFILE_ARG` word-splits a `$WORK` containing a
	# space into a bogus argument, and the resulting rc=1 would then be misattributed by the
	# message below. CI's mktemp has no spaces; a developer's TMPDIR might.
	#
	# Expanded below as `${RCFILE_ARG[@]+"${RCFILE_ARG[@]}"}`, never bare: under `set -u`, an
	# EMPTY array is an unbound-variable error before bash 4.4, and macOS still ships 3.2.57.
	# That path is the common one — every test that does not configure a source root takes it —
	# and the error would surface inside the subshell as "coverage combine failed", i.e. a
	# misattribution inside the branch added to fix a misattribution.
	RCFILE_ARG=(--rcfile="$WORK/.coveragerc")
	echo "Aliasing shard source roots onto $COVERAGE_SOURCE_ROOT"
fi

echo "Combining $shard_count shard(s) at $EXPECTED_SHA, floor ${COVERAGE_FLOOR}%, measured with $PY:"

( cd "$WORK" && "$PY" -m coverage combine ${RCFILE_ARG[@]+"${RCFILE_ARG[@]}"} --keep . >/dev/null 2>&1 ) || die_gate "coverage combine failed across $shard_count shard(s)"
[ -f "$WORK/.coverage" ] || die_gate "coverage combine produced no combined database"

( cd "$WORK" && "$PY" -m coverage report ${RCFILE_ARG[@]+"${RCFILE_ARG[@]}"} --include="$COVERAGE_INCLUDE" --fail-under="$COVERAGE_FLOOR" )
report_rc=$?

case "$report_rc" in
	0) echo "coverage gate OK (combined >= ${COVERAGE_FLOOR}%)"; exit 0 ;;
	2) echo "::error::COVERAGE FAILURE: combined coverage across $shard_count shard(s) is below the ${COVERAGE_FLOOR}% floor"; exit "$EXIT_COVERAGE_FAILURE" ;;
	1)
		# Branch the diagnosis: once aliasing IS configured, pointing at the same knob sends the
		# reader to something already correct. Same misattribution class the header warns about.
		if [ ${#RCFILE_ARG[@]} -eq 0 ]; then
			die_gate "coverage report exited 1 - it could not read the sources the shards name. The shards were produced on a bench and this job has only a checkout: set COVERAGE_SOURCE_ROOT so [paths] can alias them"
		fi
		die_gate "coverage report exited 1 with aliasing onto '$COVERAGE_SOURCE_ROOT' already configured - so a measured file did not resolve there. Most likely a .py present on the bench but absent from the checkout, rather than a missing source root"
		;;
	*) die_gate "coverage report exited $report_rc - the gate could not produce a verdict" ;;
esac
