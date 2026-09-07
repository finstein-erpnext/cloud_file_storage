"""The Workspace, the dashboard Page and the two form scripts.

The trap this suite is written around: asserting that a file exists proves nothing about
whether the Desk can use it. A workspace whose `content` names a Number Card that was never
created renders an empty box; a form script that calls an endpoint which does not exist
fails only when a human clicks. So every reference these fixtures make is resolved against
the thing it names — cards against `Number Card`, links against installed DocTypes,
shortcuts against DocTypes and Pages, and every `cloud_file_storage.api.admin.<x>` string in
the scripts against a whitelisted function.
"""

import json
import os
import re

import frappe

from cloud_file_storage.api import admin
from cloud_file_storage.tests.utils import CloudStorageTestCase

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_ROOT = os.path.join(APP_ROOT, "cloud_file_storage")

WORKSPACE = "Cloud Storage"
DASHBOARD_PAGE = "cloud-migration-dashboard"

SCRIPTS = {
	"settings": os.path.join(MODULE_ROOT, "doctype", "cloud_storage_settings", "cloud_storage_settings.js"),
	"campaign": os.path.join(
		MODULE_ROOT, "doctype", "cloud_migration_campaign", "cloud_migration_campaign.js"
	),
	"dashboard": os.path.join(
		MODULE_ROOT, "page", "cloud_migration_dashboard", "cloud_migration_dashboard.js"
	),
}

ENDPOINT_PATTERN = re.compile(r"cloud_file_storage\.api\.admin\.(\w+)")


def script(name: str) -> str:
	with open(SCRIPTS[name]) as handle:
		return handle.read()


class TestWorkspace(CloudStorageTestCase):
	def workspace(self):
		return frappe.get_doc("Workspace", WORKSPACE)

	def test_it_is_installed(self):
		self.assertTrue(frappe.db.exists("Workspace", WORKSPACE))

	def test_every_link_points_at_an_installed_doctype(self):
		for link in self.workspace().links:
			if link.type != "Link":
				continue
			with self.subTest(link=link.label):
				self.assertEqual(link.link_type, "DocType")
				self.assertTrue(
					frappe.db.exists("DocType", link.link_to),
					f"{link.link_to} is not installed",
				)

	def test_every_shortcut_resolves(self):
		for shortcut in self.workspace().shortcuts:
			with self.subTest(shortcut=shortcut.label):
				self.assertTrue(
					frappe.db.exists(shortcut.type, shortcut.link_to),
					f"{shortcut.type} {shortcut.link_to} does not exist",
				)

	def test_every_number_card_the_content_names_exists(self):
		"""The failure this catches renders as an empty box, not as an error."""
		content = json.loads(self.workspace().content)
		named = [block["data"]["number_card_name"] for block in content if block["type"] == "number_card"]
		self.assertTrue(named, "the workspace declares no number cards")
		for card in named:
			with self.subTest(card=card):
				self.assertTrue(frappe.db.exists("Number Card", card), f"{card} was never created")

	def test_every_card_block_names_a_card_break_that_exists(self):
		content = json.loads(self.workspace().content)
		declared = {link.label for link in self.workspace().links if link.type == "Card Break"}
		for block in content:
			if block["type"] != "card":
				continue
			with self.subTest(card=block["data"]["card_name"]):
				self.assertIn(block["data"]["card_name"], declared)

	def test_the_seeded_cards_count_the_thing_they_are_labelled_with(self):
		"""A card labelled Verified that counts every object is worse than no card."""
		expectations = {
			"Cloud Objects Verified": ("Cloud Storage Object", {"status": "verified"}),
			"Cloud Objects Awaiting Upload": ("Cloud Storage Object", {"status": "pending_upload"}),
			"Cloud Objects Failed": ("Cloud Storage Object", {"status": "failed"}),
			"Open Migration Conflicts": ("Cloud Migration Conflict", {"status": "Open"}),
		}
		for name, (doctype, filters) in expectations.items():
			with self.subTest(card=name):
				card = frappe.get_doc("Number Card", name)
				self.assertEqual(card.document_type, doctype)
				self.assertEqual(card.function, "Count")
				self.assertEqual(json.loads(card.filters_json), filters)


class TestDashboardPage(CloudStorageTestCase):
	def test_the_page_is_installed(self):
		self.assertTrue(frappe.db.exists("Page", DASHBOARD_PAGE))

	def test_both_operating_roles_can_open_it(self):
		roles = {row.role for row in frappe.get_doc("Page", DASHBOARD_PAGE).roles}
		self.assertEqual(roles, {"System Manager", "Cloud Storage Manager"})

	def test_the_client_script_exists_next_to_it(self):
		self.assertTrue(os.path.exists(SCRIPTS["dashboard"]))

	def test_it_polls_as_well_as_subscribing(self):
		"""Realtime is an enhancement; socketio being down must not freeze the numbers."""
		source = script("dashboard")
		self.assertIn("frappe.realtime.on('cfs_migration_progress'", source)
		self.assertIn("setInterval", source)
		self.assertIn("get_campaign_snapshot", source)

	def test_the_realtime_event_name_matches_what_the_engine_publishes(self):
		from cloud_file_storage.migration import report

		self.assertIn(f"'{report.REALTIME_EVENT}'", script("dashboard"))


class TestFormScripts(CloudStorageTestCase):
	def test_every_endpoint_the_scripts_call_is_whitelisted(self):
		for name in SCRIPTS:
			for endpoint in sorted(set(ENDPOINT_PATTERN.findall(script(name)))):
				with self.subTest(script=name, endpoint=endpoint):
					function = getattr(admin, endpoint, None)
					self.assertIsNotNone(function, f"api.admin has no {endpoint}")
					self.assertTrue(
						getattr(function, "__wrapped__", None),
						f"{endpoint} is not whitelisted",
					)

	#: The two that are Button *fields* on the form (their label lives in the doctype JSON,
	#: and clicking fires the same-named form event), against the ten the script adds.
	FIELD_BUTTONS = {"Test Connection": "test_connection", "Reconcile": "reconcile"}
	CUSTOM_BUTTONS = (
		"Analyze Storage",
		"Build Migration Plan",
		"Start",
		"Pause",
		"Resume",
		"Stop After Current Batch",
		"Retry Failed",
		"Verify",
		"Delete Verified Local Copies",
		"Export Migration Report",
	)

	def test_the_settings_form_offers_all_twelve_buttons(self):
		self.assertEqual(len(self.FIELD_BUTTONS) + len(self.CUSTOM_BUTTONS), 12)

		meta = frappe.get_meta("Cloud Storage Settings")
		source = script("settings")

		for label, fieldname in self.FIELD_BUTTONS.items():
			with self.subTest(button=label):
				field = meta.get_field(fieldname)
				self.assertIsNotNone(field, f"{fieldname} is not on the form")
				self.assertEqual(field.fieldtype, "Button")
				self.assertEqual(field.label, label)
				# A Button field does nothing without a handler of the same name.
				self.assertRegex(source, rf"\n\t{fieldname}\(frm\) \{{")

		for label in self.CUSTOM_BUTTONS:
			with self.subTest(button=label):
				# The call is wrapped across lines when its arguments are long, so the label
				# is matched with the whitespace the formatter may have inserted.
				self.assertRegex(source, rf"add_custom_button\(\s*__\('{re.escape(label)}'\)")

	def test_the_form_fields_the_scripts_render_into_exist(self):
		meta = frappe.get_meta("Cloud Storage Settings")
		for fieldname in (
			"connection_status_html",
			"storage_health_html",
			"cache_stats_html",
			"migration_html",
			"test_connection",
			"refresh_storage_health",
			"reconcile",
		):
			with self.subTest(fieldname=fieldname):
				self.assertIsNotNone(meta.get_field(fieldname), f"{fieldname} is not on the form")

	def test_the_panel_fields_store_nothing(self):
		"""Button and HTML are no_value_fields, which is why A12's data schema is unchanged."""
		from frappe.model import no_value_fields

		meta = frappe.get_meta("Cloud Storage Settings")
		for fieldname in (
			"connection_status_html",
			"storage_health_html",
			"cache_stats_html",
			"migration_html",
			"test_connection",
			"refresh_storage_health",
			"reconcile",
		):
			with self.subTest(fieldname=fieldname):
				self.assertIn(meta.get_field(fieldname).fieldtype, no_value_fields)

	def test_the_confirm_phrase_is_posted_not_compared_in_the_client(self):
		"""A client-side comparison would make the gate look enforced where it is not."""
		for name in ("settings", "campaign"):
			source = script(name)
			with self.subTest(script=name):
				self.assertIn("confirm_phrase: values.confirm_phrase", source)
				self.assertNotIn("=== 'DELETE LOCAL FILES'", source)
				self.assertNotIn('=== "DELETE LOCAL FILES"', source)

	def test_delete_verified_local_copies_calls_both_audited_actions(self):
		"""A9: approve_cleanup then start_cleanup, in that order.

		Scoped to the handler's own body. A first pass compared positions in the whole file
		and was measuring the header comment, which mentions `start_cleanup` before any code
		runs — a check that would have kept passing if the calls were swapped.
		"""
		for name in ("settings", "campaign"):
			with self.subTest(script=name):
				body = _function_body(script(name), "delete_verified_local_copies")
				self.assertIn("approve_cleanup", body)
				self.assertIn("start_cleanup", body)
				self.assertLess(body.index("approve_cleanup"), body.index("start_cleanup"))

	def test_no_script_calls_the_migration_api_directly(self):
		"""Buttons go through api.admin, which is where the role gate is."""
		for name in SCRIPTS:
			with self.subTest(script=name):
				self.assertNotIn("cloud_file_storage.migration.api", script(name))

	def test_the_dead_fork_endpoint_is_gone(self):
		"""P1 left `cloud_file_storage.controller.migrate_existing_files` on the form."""
		self.assertNotIn("controller.migrate_existing_files", script("settings"))
		self.assertFalse(
			os.path.exists(os.path.join(APP_ROOT, "controller.py")),
			"the fork controller module is back",
		)


def _function_body(source: str, name: str) -> str:
	"""The text of `function <name>(...) { ... }`, brace-matched."""
	start = source.index(f"function {name}(")
	depth = 0
	for index in range(start, len(source)):
		if source[index] == "{":
			depth += 1
		elif source[index] == "}":
			depth -= 1
			if depth == 0:
				return source[start : index + 1]
	raise AssertionError(f"unbalanced braces in {name}")


class TestPlaywrightSmokeScript(CloudStorageTestCase):
	"""Guards the smoke script from rotting. It does **not** stand in for running it.

	The smoke needs a served site, a worker on `cloud_migration` and a bucket; a python test
	that quietly skipped without them would be a claim of browser coverage that nothing
	backs. So this asserts only what can be asserted offline — that the selectors and
	endpoints it drives still exist — and `docs/runbooks/desk-smoke.md` says how to run it.
	"""

	SMOKE = os.path.join(APP_ROOT, "tests", "playwright", "smoke.py")

	def source(self) -> str:
		with open(self.SMOKE) as handle:
			return handle.read()

	def test_the_script_exists_and_parses(self):
		import ast as ast_module

		self.assertTrue(os.path.exists(self.SMOKE))
		ast_module.parse(self.source())

	def test_it_drives_fields_that_are_on_the_form(self):
		source = self.source()
		meta = frappe.get_meta("Cloud Storage Settings")
		for fieldname in ("storage_health_html", "connection_status_html", "test_connection"):
			with self.subTest(fieldname=fieldname):
				self.assertIn(f"'{fieldname}'", source)
				self.assertIsNotNone(meta.get_field(fieldname))

	def test_it_clicks_buttons_the_settings_script_actually_adds(self):
		source = self.source()
		settings = script("settings")
		for label in ("Analyze Storage", "Build Migration Plan", "Start", "Pause"):
			with self.subTest(button=label):
				self.assertIn(f'"{label}"', source)
				self.assertRegex(settings, rf"add_custom_button\(\s*__\('{re.escape(label)}'\)")

	def test_it_never_reaches_past_the_browser(self):
		"""A smoke that called the endpoint directly would not be testing the button."""
		source = self.source()
		for banned in ("import frappe", "api.admin.", "frappe.db"):
			with self.subTest(banned=banned):
				self.assertNotIn(banned, source)

	def test_the_runbook_exists_and_names_the_prerequisites(self):
		runbook = os.path.join(os.path.dirname(APP_ROOT), "docs", "runbooks", "desk-smoke.md")
		self.assertTrue(os.path.exists(runbook))
		with open(runbook) as handle:
			text = handle.read()
		for topic in ("cloud_migration", "100 File rows", "locale", "Paused"):
			with self.subTest(topic=topic):
				self.assertIn(topic, text)


class TestBackupFindingsAreRenderedNotRecomputed(CloudStorageTestCase):
	"""The P6 ⇄ P7 seam, from the client side.

	Detection lives in `backup/health` (P6); this panel renders it. The behaviour is tested
	in `test_backup_health_panel.py`; what is checked here is that the *script* prints the
	messages it is given and keeps them out of the byte cards.
	"""

	def test_the_panel_renders_a_backup_block(self):
		source = script("settings")
		self.assertIn("function backup_html(backup)", source)
		self.assertIn("health.backup", source)

	def test_it_prints_the_messages_verbatim(self):
		"""No client-side rewording: the panel must not become a second source of truth.

		This asserted `assertIn("escape_html(finding.message)", body)` — a substring check for
		one named interpolation, which passes while any *other* interpolation goes unwrapped
		and would have passed against the tree that shipped L-4. It read as an escaping check
		and was not one. Escaping is now covered properly and for every interpolation by
		`tests/test_desk_escaping.py`; what belongs here is the property this test is named
		for, so it asserts that the message reaches the DOM **unaltered** — the interpolation
		carrying `finding.message` must be exactly the escape call, with nothing prepended,
		appended or substituted.
		"""
		import re

		body = _function_body(script("settings"), "backup_html")

		# Looking at the interpolation *expression* is not enough, and this test learned that
		# the hard way: prefixing the output with a literal `Backup: ` leaves the expression
		# untouched, so an expression-level assertion stays green while the panel has started
		# editorialising. The property is about the rendered element, so the check reads the
		# markup between the surrounding tags.
		index = body.index("finding.message")
		opening = body.rindex(">", 0, index)
		closing = body.index("<", index)
		rendered = body[opening + 1 : closing]

		self.assertRegex(
			rendered.strip(),
			r"^\$\{frappe\.utils\.escape_html\(\s*finding\.message\s*\)\}$",
			f"the finding message is decorated or reworded before rendering: {rendered.strip()!r}",
		)

	def test_it_hardcodes_no_finding_text_of_its_own(self):
		body = _function_body(script("settings"), "backup_html")
		for invented in ("stale", "tarball", "lifecycle", "hours ago", "drift"):
			with self.subTest(invented=invented):
				self.assertNotIn(invented, body.lower())

	def test_the_backup_block_carries_no_byte_total(self):
		"""`tarballs_after_cutover` is about local bytes — the finding most likely to be
		folded in beside a byte figure by someone tidying the panel."""
		body = _function_body(script("settings"), "backup_html")
		self.assertNotIn("bytes(", body)

	def test_the_withheld_case_is_rendered_rather_than_blank(self):
		body = _function_body(script("settings"), "backup_html")
		self.assertIn("backup.visible", body)
		self.assertIn("backup.reason", body)
