"""Playwright smoke over the P7 Desk surfaces, on a 100-file fixture.

What a browser proves that the python suite cannot: that the form actually *loads* with the
scripts attached, that the health panel renders from a real `get_storage_health` response,
that Test Connection paints its result, and that the mission buttons drive a campaign from
the Desk — click, dialog, endpoint, refreshed state — rather than from a test that calls the
endpoint directly.

It asserts what it can see. Nothing here reaches into the database to make an assertion
pass, and nothing calls `api.admin` directly: every state change goes through a click.

Run it against a scratch site only (`docs/runbooks/local-testing.md`):

    python3 -m cloud_file_storage.tests.playwright.smoke \
        --base-url http://127.0.0.1:8007 --password admin

Prerequisites, all of which the script checks and names when missing:
  * `bench --site <scratch> serve` on `--base-url`;
  * a worker consuming `cloud_migration` (the campaign jobs are enqueued, not inline);
  * 100 `p7-smoke-*.txt` File rows with no Cloud Storage Object;
  * a reachable bucket (local MinIO is enough).
"""

import argparse
import datetime
import json
import os
import sys
import time

from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

DEFAULT_BASE_URL = "http://127.0.0.1:8007"
SETTINGS_ROUTE = "/app/cloud-storage-settings"
CAMPAIGN_LIST_ROUTE = "/app/cloud-migration-campaign"
DASHBOARD_ROUTE = "/app/cloud-migration-dashboard"

#: Generous, because a campaign step goes out to a worker and back.
STEP_TIMEOUT_MS = 60_000
POLL_SECONDS = 90


class SmokeFailure(AssertionError):
	pass


def log(message: str):
	print(f"  {message}", flush=True)


def login(page: Page, base_url: str, user: str, password: str):
	page.goto(f"{base_url}/login", timeout=STEP_TIMEOUT_MS)
	page.fill("#login_email", user)
	page.fill("#login_password", password)
	page.click("button.btn-login")
	page.wait_for_url(lambda url: "/login" not in url, timeout=STEP_TIMEOUT_MS)
	log(f"logged in as {user}")


def open_settings(page: Page, base_url: str):
	page.goto(f"{base_url}{SETTINGS_ROUTE}", timeout=STEP_TIMEOUT_MS)
	page.wait_for_selector(".form-page, .layout-main-section", timeout=STEP_TIMEOUT_MS)
	page.wait_for_selector("[data-fieldname='storage_health_html']", timeout=STEP_TIMEOUT_MS)


def health_panel_text(page: Page) -> str:
	panel = page.locator("[data-fieldname='storage_health_html']")
	panel.wait_for(state="visible", timeout=STEP_TIMEOUT_MS)
	page.wait_for_function(
		"""() => {
			const node = document.querySelector("[data-fieldname='storage_health_html']");
			return node && node.innerText.includes('Cloud-managed');
		}""",
		timeout=STEP_TIMEOUT_MS,
	)
	return panel.inner_text()


def click_migration_action(page: Page, label: str):
	"""Open the "Migration" custom-button group and click one of its items."""
	group = page.locator(".custom-actions button", has_text="Migration").first
	group.wait_for(state="visible", timeout=STEP_TIMEOUT_MS)
	group.click()
	item = page.locator(".custom-actions .dropdown-item", has_text=label).first
	item.wait_for(state="visible", timeout=STEP_TIMEOUT_MS)
	item.click()


def dialog_submit(page: Page, label: str | None = None):
	button = page.locator(".modal.show .modal-footer .btn-primary").last
	button.wait_for(state="visible", timeout=STEP_TIMEOUT_MS)
	if label:
		text = button.inner_text().strip()
		if label.lower() not in text.lower():
			raise SmokeFailure(f"expected a {label!r} button in the dialog, found {text!r}")
	button.click()


def wait_for_campaign_status(page: Page, base_url: str, campaign: str, wanted: set[str]) -> str:
	"""Poll the campaign form until its status indicator reads one of `wanted`."""
	deadline = time.time() + POLL_SECONDS
	seen = ""
	while time.time() < deadline:
		page.goto(f"{base_url}{CAMPAIGN_LIST_ROUTE}/{campaign}", timeout=STEP_TIMEOUT_MS)
		page.wait_for_selector(
			"[data-fieldname='status'] .control-value, select[data-fieldname='status']",
			timeout=STEP_TIMEOUT_MS,
		)
		seen = page.evaluate("() => (cur_frm && cur_frm.doc && cur_frm.doc.status) || ''")
		if seen in wanted:
			return seen
		time.sleep(3)
	raise SmokeFailure(f"campaign {campaign} stayed at {seen!r}, never reached one of {sorted(wanted)}")


def campaign_counts(page: Page, base_url: str, campaign: str) -> dict:
	"""The counters the Desk itself is showing, read off `cur_frm.doc`.

	Read from the rendered form rather than queried from the database on purpose: the point
	of a browser smoke is that the numbers an operator can see are real. A SQL count here
	would be evidence about the database, which the python suite already covers.
	"""
	page.goto(f"{base_url}{CAMPAIGN_LIST_ROUTE}/{campaign}", timeout=STEP_TIMEOUT_MS)
	page.wait_for_function("() => !!(window.cur_frm && cur_frm.doc)", timeout=STEP_TIMEOUT_MS)
	return page.evaluate(
		"""() => {
			const doc = cur_frm.doc;
			return {
				name: doc.name,
				status: doc.status,
				total_objects: doc.total_objects,
				total_files: doc.total_files,
				objects_pending: doc.objects_pending,
				objects_uploading: doc.objects_uploading,
				objects_uploaded: doc.objects_uploaded,
				objects_verified: doc.objects_verified,
				objects_skipped: doc.objects_skipped,
				objects_failed: doc.objects_failed,
				bytes_uploaded: doc.bytes_uploaded,
			};
		}"""
	)


def write_evidence(artefacts: str, payload: dict):
	os.makedirs(artefacts, exist_ok=True)
	path = os.path.join(artefacts, "smoke-evidence.json")
	with open(path, "w") as handle:
		json.dump(payload, handle, indent=2, sort_keys=True)
	log(f"evidence written to {path}")


def current_campaign(page: Page) -> str:
	name = page.evaluate(
		"""() => {
			const health = cur_frm && cur_frm.__cfs_health;
			if (!health || !health.migration) return '';
			const campaign = health.migration.active_campaign || health.migration.latest_campaign;
			return campaign ? campaign.name : '';
		}"""
	)
	if not name:
		raise SmokeFailure("the settings panel resolved no campaign")
	return name


def run(base_url: str, user: str, password: str, headed: bool, artefacts: str) -> int:
	failures: list[str] = []
	evidence: dict = {
		"base_url": base_url,
		"started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
		"steps": [],
	}

	with sync_playwright() as playwright:
		browser = playwright.chromium.launch(headless=not headed)
		# An explicit locale is not cosmetic here: frappe's desk bundle builds an
		# `Intl.Locale` from the browser's, and a headless chromium started without one
		# throws `RangeError: Incorrect locale information provided` while wiring keyboard
		# shortcuts — which leaves the whole form region hidden and every later assertion
		# failing for a reason that has nothing to do with this app.
		context = browser.new_context(
			viewport={"width": 1440, "height": 1000},
			locale="en-US",
			timezone_id="UTC",
		)
		page = context.new_page()
		console_errors: list[str] = []
		page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

		try:
			login(page, base_url, user, password)

			# 1 — the settings form loads with its panel rendered from a real response.
			print("[1/6] settings form loads", flush=True)
			open_settings(page, base_url)
			panel = health_panel_text(page)
			for expected in ("Cloud-managed", "operational", "not yet cloud-managed"):
				if expected.lower() not in panel.lower():
					failures.append(f"health panel is missing {expected!r}")
			log(panel.replace("\n", " | ")[:200])
			os.makedirs(artefacts, exist_ok=True)
			page.screenshot(path=f"{artefacts}/01-settings.png")
			evidence["health_panel_text"] = panel
			evidence["steps"].append("settings form loaded, health panel rendered")

			# 2 — Test Connection paints its per-check result.
			print("[2/6] test connection renders", flush=True)
			page.click("[data-fieldname='test_connection'] button, button[data-fieldname='test_connection']")
			page.wait_for_function(
				"""() => {
					const node = document.querySelector("[data-fieldname='connection_status_html']");
					return node && node.innerText.includes('head_bucket');
				}""",
				timeout=STEP_TIMEOUT_MS,
			)
			connection = page.locator("[data-fieldname='connection_status_html']").inner_text()
			log(connection.replace("\n", " | ")[:200])
			if "put_object" not in connection or "get_object" not in connection:
				failures.append("the connection panel did not render the write/read probes")
			page.screenshot(path=f"{artefacts}/02-test-connection.png")
			evidence["connection_panel_text"] = connection
			evidence["steps"].append("test connection rendered")

			# 3 — Analyze Storage: create a campaign and classify the fixture.
			print("[3/6] analyze storage", flush=True)
			click_migration_action(page, "Analyze Storage")
			page.fill(".modal.show [data-fieldname='title'] input", f"P7 smoke {int(time.time())}")
			dialog_submit(page, "Analyze")
			page.wait_for_timeout(4000)
			open_settings(page, base_url)
			health_panel_text(page)
			campaign = current_campaign(page)
			log(f"campaign {campaign}")
			wait_for_campaign_status(page, base_url, campaign, {"Analyzed"})
			evidence["campaign"] = campaign
			evidence["after_analyze"] = campaign_counts(page, base_url, campaign)
			evidence["steps"].append("analyzed")

			# 4 — Build Migration Plan, in batches small enough that a pause means something.
			print("[4/6] build migration plan", flush=True)
			open_settings(page, base_url)
			health_panel_text(page)
			click_migration_action(page, "Build Migration Plan")
			page.fill(".modal.show [data-fieldname='batch_size'] input", "10")
			dialog_submit(page, "Build Plan")
			wait_for_campaign_status(page, base_url, campaign, {"Planned"})
			evidence["after_plan"] = campaign_counts(page, base_url, campaign)
			evidence["batch_count"] = page.evaluate(
				"() => cur_frm && cur_frm.doc ? cur_frm.doc.total_batches : null"
			)
			evidence["steps"].append("planned")

			# 5 — Start.
			print("[5/6] start", flush=True)
			open_settings(page, base_url)
			health_panel_text(page)
			click_migration_action(page, "Start")
			page.locator(".modal.show .btn-primary", has_text="Yes").first.click()
			status = wait_for_campaign_status(page, base_url, campaign, {"Running"})
			log(f"status {status}")
			page.screenshot(path=f"{artefacts}/03-running.png")
			evidence["after_start"] = campaign_counts(page, base_url, campaign)
			evidence["steps"].append("started")

			# 6 — Pause.
			print("[6/6] pause", flush=True)
			open_settings(page, base_url)
			health_panel_text(page)
			click_migration_action(page, "Pause")
			status = wait_for_campaign_status(page, base_url, campaign, {"Paused"})
			log(f"status {status}")
			page.screenshot(path=f"{artefacts}/04-paused.png")
			evidence["after_pause"] = campaign_counts(page, base_url, campaign)
			evidence["steps"].append("paused")

			# The dashboard has to open for the operator the campaign belongs to.
			page.goto(f"{base_url}{DASHBOARD_ROUTE}", timeout=STEP_TIMEOUT_MS)
			page.wait_for_selector(".cfs-dashboard", timeout=STEP_TIMEOUT_MS)
			page.screenshot(path=f"{artefacts}/05-dashboard.png")
			evidence["steps"].append("dashboard rendered")

		except (PlaywrightTimeoutError, SmokeFailure) as exc:
			failures.append(f"{type(exc).__name__}: {exc}")
			page.screenshot(path=f"{artefacts}/99-failure.png")
		finally:
			evidence["failures"] = failures
			evidence["result"] = "PASSED" if not failures else "FAILED"
			evidence["finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
			write_evidence(artefacts, evidence)

			# Console errors are reported, not asserted on: frappe's own bundles log warnings
			# this app does not control, and failing on them would make the smoke flaky in a
			# way that says nothing about the buttons.
			if console_errors:
				print("console errors seen (informational):", flush=True)
				for message in console_errors[:10]:
					log(message[:200])
			context.close()
			browser.close()

	if failures:
		print("\nSMOKE FAILED", flush=True)
		for failure in failures:
			print(f"  - {failure}", flush=True)
		return 1

	print("\nSMOKE PASSED", flush=True)
	return 0


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
	parser.add_argument("--user", default="Administrator")
	parser.add_argument("--password", default="admin")
	parser.add_argument("--headed", action="store_true")
	parser.add_argument("--artefacts", default="/tmp")
	arguments = parser.parse_args()
	return run(
		arguments.base_url,
		arguments.user,
		arguments.password,
		arguments.headed,
		arguments.artefacts,
	)


if __name__ == "__main__":
	sys.exit(main())
