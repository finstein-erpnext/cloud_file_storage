#!/usr/bin/env bash
# One-time label bootstrap.
#
# `.github/labeler.yml`, `.github/release.yml`, `.github/stale.yml` and `backport.yml` all
# reference labels by name. A label that does not exist is not an error on GitHub -- the
# action simply applies nothing -- so a missing label makes automation silently do nothing
# rather than fail. This creates them all, idempotently.
#
#   gh auth login          # needs repo admin
#   bash scripts/create-labels.sh
#
# Re-running is safe: an existing label is updated to the colour/description below.
set -euo pipefail

REPO="${REPO:-RamachandranMD/cloud_file_storage}"

create() {  # name colour description
	if gh label create "$1" --repo "$REPO" --color "$2" --description "$3" 2>/dev/null; then
		echo "  created  $1"
	else
		gh label edit "$1" --repo "$REPO" --color "$2" --description "$3" >/dev/null
		echo "  updated  $1"
	fi
}

echo "Area labels (applied automatically by .github/labeler.yml):"
create storage      "1d76db" "Runtime storage core: objects, modes, S3 client"
create serving      "1d76db" "Private/public serving, aliases, thumbnails"
create migration    "1d76db" "Migration engine: analyze, plan, upload, verify, cleanup"
create backup       "1d76db" "Backup, restore and lifecycle"
create cache        "1d76db" "Materialisation cache and write-back"
create api          "1d76db" "Whitelisted API surface"
create desk         "1d76db" "Desk UI, settings form, workspace"
create schema       "b60205" "DocType schema or patch"
create contract     "b60205" "Compatibility contract (C1-C19) or acceptance gate"
create cli          "1d76db" "bench commands"
create hooks        "b60205" "hooks.py / core hook seams"
create docs         "0075ca" "Documentation only"
create ci           "0075ca" "CI, workflows, tooling"
create dependencies "0075ca" "Dependency updates"
create needs-tests  "d93f0b" "Change lacks the test that would catch its regression"

echo "Release-note labels (applied by hand; used by .github/release.yml):"
create breaking-change "d93f0b" "Requires action from an operator on upgrade"
create security        "d93f0b" "Security fix or hardening"
create perf            "0e8a16" "Performance"
create feat            "0e8a16" "New capability"
create fix             "0e8a16" "Bug fix"
create skip-release-notes "ededed" "Exclude from generated release notes"

echo "Process labels:"
create bug             "d93f0b" "Something is broken"
create feature-request "0e8a16" "Requested capability"
create hotfix          "d93f0b" "Urgent; exempt from stale"
create blocked         "ededed" "Waiting on something external; exempt from stale"
create inactive        "ededed" "Marked stale by the bot"
create backport        "5319e7" "Opened automatically by backport.yml"
create "backport version-15" "5319e7" "Backport this merged PR to the v15 line"
create "backport version-16" "5319e7" "Backport this merged PR to the v16 line"

echo
echo "Done. Labels the automation depends on now exist on $REPO."
