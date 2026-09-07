#!/bin/bash
# Install the L3 ecosystem apps onto an existing bench + site (F5 / PLAN §B P4).
#
# Run AFTER `bench new-site` and BEFORE installing cloud_file_storage, mirroring the
# order the L3 site was validated in: India Compliance adds custom fields to Item and
# erpnext seeds the doctypes the reposting tests use, and cloud_file_storage's install
# should see the finished schema rather than race it.
#
# The refs are pinned, not floating. `version-15` moving under us would turn an ecosystem
# regression into "the job went red on a Tuesday" with nothing to bisect; these are the
# exact revisions the tests were written against.
set -euo pipefail

BENCH_DIR="${BENCH_DIR:-$HOME/frappe-bench}"
SITE_NAME="${SITE_NAME:-test_site}"

ERPNEXT_REF="${ERPNEXT_REF:-v15.93.0}"
HRMS_REF="${HRMS_REF:-v15.49.2}"
INDIA_COMPLIANCE_REF="${INDIA_COMPLIANCE_REF:-v15.18.1}"

cd "$BENCH_DIR"

# `--resolve-deps` ONLY on erpnext, which needs it to pull `payments`. hrms and
# india_compliance both depend on erpnext, and re-resolving it once it is already present made
# bench refuse with "Incompatible version of erpnext is already installed", followed by a
# nonsense path (./apps/frappe/erpnext/erpnext/__init__.py) as its resolver lost its footing.
# The dependency is already satisfied by the line above, at the ref this job pins.
bench get-app --branch "$ERPNEXT_REF" --resolve-deps erpnext
bench get-app --branch "$HRMS_REF" hrms
bench get-app --branch "$INDIA_COMPLIANCE_REF" \
	https://github.com/resilient-tech/india-compliance

# The four apps above installed into the bench env. `install-app` below imports frappe and
# syncs doctypes -- the exact command class that died on ModuleNotFoundError: pkg_resources.
# install.sh re-pins after this script returns, which is too late for these commands.
"$BENCH_DIR/env/bin/pip" install --quiet "setuptools<81"
"$BENCH_DIR/env/bin/python" -c "import pkg_resources" || {
	echo "::error::pkg_resources missing after the ecosystem installs; the pin did not hold"
	exit 1
}

bench --site "$SITE_NAME" install-app erpnext
bench --site "$SITE_NAME" install-app hrms
bench --site "$SITE_NAME" install-app india_compliance

echo "ecosystem apps installed on $SITE_NAME:"
bench --site "$SITE_NAME" list-apps
