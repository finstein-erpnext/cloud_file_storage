"""T-CLI — every bench command drives the same `migration/api` function a button would.

That is the whole property being tested, and it is a structural one: a validation living in
the CLI is one the Desk does not have, and vice versa. So each command is invoked through
click for real and the api function is spied on, rather than the command's *effect* being
re-asserted — an effect test would pass just as well for a command that re-implemented the
logic locally.

The commands are also asserted to fail cleanly on a wrong campaign state, because the state
machine's refusals are exactly what a single validation path buys.
"""

import json
from unittest.mock import patch

import click
import frappe
from click.testing import CliRunner

from cloud_file_storage import commands
from cloud_file_storage.migration import api
from cloud_file_storage.tests.migration_utils import MigrationTestCase


class CliTestCase(MigrationTestCase):
	def run_command(self, command, args, *, spy_on=None, returns=None):
		"""Invoke a bench command for real, with the site already connected.

		`_connect`/`_finish` are neutralised because the test process is already inside a
		frappe context — a second `frappe.init`/`destroy` would tear down the connection the
		suite is running on. Everything between them is the real command body.
		"""
		captured = {}

		def spy(*call_args, **call_kwargs):
			captured["args"] = call_args
			captured["kwargs"] = call_kwargs
			return returns if returns is not None else {"ok": True}

		context = frappe._dict({"sites": [frappe.local.site], "profile": False})
		patches = [
			patch.object(commands, "_connect", lambda ctx: frappe.local.site),
			patch.object(commands, "_finish", lambda: None),
		]
		if spy_on:
			patches.append(patch.object(api, spy_on, spy))

		for patcher in patches:
			patcher.start()
		try:
			result = CliRunner().invoke(command, args, obj=context, catch_exceptions=False)
		finally:
			for patcher in reversed(patches):
				patcher.stop()

		return result, captured


class TestEveryCommandDrivesTheApi(CliTestCase):
	def test_analyze_creates_a_campaign_and_starts_the_analysis(self):
		created = {}

		def fake_create(title, **policy):
			created["title"] = title
			return "CFS-CAMP-9001"

		with patch.object(api, "create_campaign", fake_create):
			result, captured = self.run_command(
				commands.migrate_analyze, ["--new", "from the CLI"], spy_on="start_analysis"
			)

		self.assertEqual(result.exit_code, 0, result.output)
		self.assertEqual(created["title"], "from the CLI")
		self.assertEqual(captured["args"], ("CFS-CAMP-9001",))

	def test_analyze_requires_a_campaign_or_a_title(self):
		# click turns a UsageError into exit code 2 plus a message, which is what an operator
		# actually sees; asserting the exception type would test click's internals instead.
		result, _captured = self.run_command(commands.migrate_analyze, [])
		self.assertEqual(result.exit_code, 2, result.output)
		self.assertIn("--campaign", result.output)

	def test_plan_forwards_the_batch_size(self):
		result, captured = self.run_command(
			commands.migrate_plan, ["--campaign", "CFS-CAMP-9001", "--batch-size", "250"], spy_on="plan"
		)
		self.assertEqual(result.exit_code, 0, result.output)
		self.assertEqual(captured["args"], ("CFS-CAMP-9001",))
		self.assertEqual(captured["kwargs"]["batch_size"], 250)

	def test_start_forwards_every_policy_flag(self):
		result, captured = self.run_command(
			commands.migrate_start,
			[
				"--campaign",
				"CFS-CAMP-9001",
				"--parallelism",
				"4",
				"--bandwidth-mbps",
				"25",
				"--force-fallback-queue",
			],
			spy_on="start_migration",
		)
		self.assertEqual(result.exit_code, 0, result.output)
		self.assertEqual(captured["kwargs"]["parallelism"], 4)
		self.assertEqual(captured["kwargs"]["bandwidth_mbps"], 25)
		self.assertTrue(captured["kwargs"]["force_fallback_queue"])

	def test_pause_resume_and_stop_each_call_their_own_transition(self):
		for command, spy_on in (
			(commands.migrate_pause, "pause"),
			(commands.migrate_resume, "resume"),
			(commands.migrate_stop, "stop"),
		):
			with self.subTest(command=command.name):
				result, captured = self.run_command(command, ["--campaign", "CFS-CAMP-9001"], spy_on=spy_on)
				self.assertEqual(result.exit_code, 0, result.output)
				self.assertEqual(captured["args"], ("CFS-CAMP-9001",))

	def test_approve_cleanup_and_cleanup_are_two_separate_commands(self):
		result, captured = self.run_command(
			commands.migrate_approve_cleanup,
			["--campaign", "CFS-CAMP-9001", "--note", "reviewed"],
			spy_on="approve_cleanup",
		)
		self.assertEqual(result.exit_code, 0, result.output)
		self.assertEqual(captured["kwargs"]["note"], "reviewed")

		result, captured = self.run_command(
			commands.migrate_cleanup,
			["--campaign", "CFS-CAMP-9001", "--confirm-phrase", "DELETE LOCAL FILES", "--direct-delete"],
			spy_on="start_cleanup",
		)
		self.assertEqual(result.exit_code, 0, result.output)
		self.assertTrue(captured["kwargs"]["direct_delete"])
		# The CLI hands the phrase straight through: the gate lives in migration/api, so the
		# command cannot be the place that decides whether it was typed.
		self.assertEqual(captured["kwargs"]["confirm_phrase"], "DELETE LOCAL FILES")

	def test_report_export_and_reconcile_and_preflight_and_purge(self):
		for command, args, spy_on in (
			(commands.migrate_report, ["--campaign", "C", "--format", "jsonl"], "export_report"),
			(commands.migrate_reconcile, ["--prefix", "attachments/"], "reconcile"),
			(commands.migrate_preflight, ["--campaign", "C", "--target-rows", "500"], "run_preflight"),
			(commands.migrate_purge, ["--campaign", "C", "--older-than-days", "10"], "purge_snapshot"),
		):
			with self.subTest(command=command.name):
				result, captured = self.run_command(command, args, spy_on=spy_on, returns="ok")
				self.assertEqual(result.exit_code, 0, result.output)
				self.assertIn("args", captured, f"{command.name} did not reach the api")

	def test_status_renders_the_counter_snapshot(self):
		snapshot = {
			"snapshot": {
				"name": "CFS-CAMP-9001",
				"status": "Running",
				"active_phase": "UPLOAD",
				"progress_pct": 42.0,
				"objects_verified": 7,
				"open_blockers": 1,
			},
			"convergence": {"ratio": 0.9993, "in_scope": 1000},
			"summary": {},
		}
		result, _captured = self.run_command(
			commands.migrate_status, ["--campaign", "CFS-CAMP-9001"], spy_on="status", returns=snapshot
		)
		self.assertEqual(result.exit_code, 0, result.output)
		self.assertIn("CFS-CAMP-9001", result.output)
		self.assertIn("objects_verified", result.output)
		self.assertIn("0.9993", result.output)
		self.assertIn("open blockers", result.output)

	def test_every_registered_command_is_exported(self):
		"""A command frappe cannot discover is a command that does not exist."""
		names = {command.name for command in commands.commands}
		self.assertIn("cfs-migrate-analyze", names)
		self.assertIn("cfs-migrate-approve-cleanup", names)
		self.assertIn("cfs-migrate-cleanup", names)
		self.assertEqual(len(names), len(commands.commands), "two commands share a name")


class TestCommandsFailCleanlyOnState(CliTestCase):
	"""The refusals a single validation path buys — reached through the CLI, not around it."""

	def test_planning_a_draft_campaign_is_refused(self):
		campaign = self.campaign()
		with self.assertRaises(frappe.ValidationError):
			self.run_command(commands.migrate_plan, ["--campaign", campaign])

	def test_starting_an_unanalyzed_campaign_is_refused(self):
		campaign = self.campaign()
		with self.assertRaises(frappe.ValidationError):
			self.run_command(commands.migrate_start, ["--campaign", campaign])

	def test_cleanup_without_approval_is_refused(self):
		campaign = self.campaign()
		self.analyze(campaign)
		with self.assertRaises(frappe.ValidationError):
			self.run_command(commands.migrate_cleanup, ["--campaign", campaign])

	def test_a_real_report_export_round_trips(self):
		"""One end-to-end command, so the spies above are not the only evidence."""
		doc = self.local_file(file_name="cli-report.txt", content=b"report me")
		campaign = self.campaign()
		self.migrate(campaign)

		result, _captured = self.run_command(
			commands.migrate_report, ["--campaign", campaign, "--format", "jsonl"]
		)
		path = result.output.strip().splitlines()[-1]

		with open(path) as handle:
			rows = [json.loads(line) for line in handle if line.strip()]
		import os

		self.addCleanup(lambda: os.path.exists(path) and os.remove(path))

		self.assertEqual(
			len(rows),
			frappe.db.count("Cloud Migration Object", {"campaign": campaign}),
			"the export row count does not match the object count",
		)
		self.assertTrue(any(row["file_url"] == doc.file_url for row in rows))
