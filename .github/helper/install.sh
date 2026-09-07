#!/bin/bash
# Bootstrap a frappe-bench and a test site against a pinned frappe ref.
#
# Required env: FRAPPE_REF (tag or branch), GITHUB_WORKSPACE (this app's checkout).
# Optional env: BENCH_DIR (default ~/frappe-bench), SITE_NAME (default test_site).
set -euo pipefail

FRAPPE_REF="${FRAPPE_REF:-version-15}"
BENCH_DIR="${BENCH_DIR:-$HOME/frappe-bench}"
SITE_NAME="${SITE_NAME:-test_site}"
FRAPPE_SRC="${FRAPPE_SRC:-$HOME/frappe}"

cd "$HOME"

mariadb --host 127.0.0.1 --port 3306 -u root -proot -e "SET GLOBAL character_set_server = 'utf8mb4'"
mariadb --host 127.0.0.1 --port 3306 -u root -proot -e "SET GLOBAL collation_server = 'utf8mb4_unicode_ci'"

# Pin frappe by cloning the exact ref first; bench init then builds from that clone.
git clone https://github.com/frappe/frappe --branch "$FRAPPE_REF" --depth 1 "$FRAPPE_SRC"

# `pkg_resources` was removed in setuptools 81. Frappe's own Dropbox Settings controller still
# imports it, so a runner that resolves a newer setuptools fails `bench migrate` with
# "No module named 'pkg_resources'" while syncing frappe's core doctypes -- an upstream
# incompatibility surfaced by the runner image, not by this app. Pinning below the removal is a
# CI/toolchain fix; the application is not modified to compensate.
pip install "setuptools<81" wheel
pip install frappe-bench

# `bench init` clones and yarn-installs; codeload.github.com rate-limits (HTTP 429) under CI
# load and takes the whole job down before a single test runs. Retried with backoff because a
# transient upstream 429 is not a result -- a job that fails on it has measured nothing. The
# bench directory is removed between attempts so a partial init cannot be mistaken for a good
# one.
bench_init() {
	bench init \
		--skip-redis-config-generation \
		--skip-assets \
		--frappe-path "$FRAPPE_SRC" \
		--python "$(which python)" \
		"$BENCH_DIR"
}

for attempt in 1 2 3; do
	if bench_init; then
		break
	fi
	if [ "$attempt" = "3" ]; then
		echo "::error::bench init failed three times; see the log above for the underlying cause"
		exit 1
	fi
	echo "bench init attempt $attempt failed (commonly a codeload 429); retrying in $((attempt * 30))s"
	rm -rf "$BENCH_DIR"
	sleep $((attempt * 30))
done

cd "$BENCH_DIR"

# The pin above applies to the *runner's* Python. `bench init` builds its own virtualenv with a
# fresh pip that resolves the newest setuptools, and every command below runs inside that env --
# so without a pin there `bench new-site` dies on "No module named 'pkg_resources'" while
# syncing frappe's Dropbox Settings controller. Diagnosed correctly the first time and applied
# to the wrong interpreter; the fix is the same pin in the environment that actually runs frappe.
#
# Re-applied after every step that installs into that env, because each one may resolve
# dependencies: pip's default strategy argues it will not upgrade an already-satisfied
# setuptools, but this defect has now cost two CI rounds and the pin is idempotent and free.
pin_setuptools() {
	"$BENCH_DIR/env/bin/pip" install --quiet "setuptools<81"
	# Assert the actual property rather than trusting the pin. Under `set -e` a regression
	# fails here, naming the cause, instead of dying inside a bench command twenty minutes
	# into three matrix jobs with a traceback that points at frappe.
	"$BENCH_DIR/env/bin/python" -c "import pkg_resources" || {
		echo "::error::pkg_resources missing from the bench env; the setuptools pin did not hold"
		exit 1
	}
}

pin_setuptools
bench get-app --resolve-deps cloud_file_storage "$GITHUB_WORKSPACE"
bench setup requirements --dev
pin_setuptools
bench new-site --db-root-password root --admin-password admin "$SITE_NAME"

# The L3 ecosystem job installs erpnext/hrms/india_compliance BEFORE this app, which is the
# order the ecosystem site was validated in: India Compliance adds custom fields to Item and
# erpnext seeds the doctypes the reposting tests use.
if [ "${INSTALL_ECOSYSTEM:-0}" = "1" ]; then
	BENCH_DIR="$BENCH_DIR" SITE_NAME="$SITE_NAME" \
		bash "$GITHUB_WORKSPACE/.github/helper/install_ecosystem_apps.sh"
	# erpnext + payments + hrms + india_compliance just installed into the same env.
	pin_setuptools
fi

bench --site "$SITE_NAME" install-app cloud_file_storage
bench --site "$SITE_NAME" set-config allow_tests true

echo "frappe ref under test: $(git -C "$FRAPPE_SRC" describe --tags --always)"
