"""The P7 exit gate: every mission button, in every campaign state.

Three properties, and the reason each is asserted the way it is.

**1. Per-button state validation.** For each button the table below names the states the
frozen A9 machine allows. The test drives the campaign into all eleven states and calls the
endpoint in each one. In an invalid state the refusal must be *the state machine's* refusal,
identified by `_assert_status`'s own wording — not merely "an exception happened", which
would pass for a button that refused because a queue was missing or a document did not
exist. In a valid state the state refusal must be absent; anything else the call runs into
(no approval, no batches, wrong mode) is another gate's business and has its own tests.

**2. Type-to-confirm, server-side.** The phrase is checked in `migration/api`, which is what
makes it survive a caller that never loaded the dialog — `/api/method/...start_cleanup` is
reachable directly. The tests below call the endpoint the way such a caller would.

**3. api/admin.py decides nothing.** Parsed, not read: every function body is exactly a role
gate and a delegation. A button that grew its own precondition would be a precondition
`bench cfs-migrate-*` does not have.
"""

import ast
import inspect

import frappe
from frappe.utils import cint

from cloud_file_storage.api import admin
from cloud_file_storage.cloud_file_storage.doctype.cloud_migration_campaign.cloud_migration_campaign import (
	CAMPAIGN_STATUSES,
)
from cloud_file_storage.migration import api as migration_api
from cloud_file_storage.tests.migration_utils import CAMPAIGN_DOCTYPE, MigrationTestCase

#: The fragment `migration.api._assert_status` puts in its message. Matching on this rather
#: than on `frappe.ValidationError` is the difference between "the state machine refused"
#: and "something, somewhere, raised".
STATE_REFUSAL = "this action needs it to be one of"

#: Button → (admin endpoint, extra kwargs, states the shipped state machine allows).
#: Sourced from `migration/api.py`, which is the single validation path (A9) — the design
#: doc's table is source material and, where it differs, DECISIONS.md records why.
BUTTON_MATRIX = {
	"Analyze Storage": ("analyze_storage", {}, ("Draft", "Analyzed", "Stopped", "Failed")),
	"Build Migration Plan": ("build_migration_plan", {}, ("Analyzed", "Planned", "Stopped")),
	"Start": ("start_campaign", {}, ("Planned", "Paused", "Stopped", "Running")),
	"Pause": ("pause_campaign", {}, ("Running",)),
	"Resume": ("resume_campaign", {}, ("Paused",)),
	"Stop After Current Batch": ("stop_after_current_batch", {}, ("Running", "Paused")),
	"Retry Failed": ("retry_failed", {}, ("Running", "Paused", "Stopped", "Failed")),
	"Verify": ("start_verify", {}, ("Running", "Paused", "Stopped", "Failed")),
	"Delete Verified Local Copies (approve)": (
		"approve_cleanup",
		{},
		("Running", "Stopped", "Paused", "Completed"),
	),
	"Delete Verified Local Copies (start)": (
		"start_cleanup",
		{"confirm_phrase": migration_api.CLEANUP_CONFIRM_PHRASE},
		("Running", "Stopped", "Paused", "Completed", "Cleanup Running"),
	),
}


class AdminApiTestCase(MigrationTestCase):
	def set_status(self, campaign: str, status: str):
		frappe.db.set_value(
			CAMPAIGN_DOCTYPE,
			campaign,
			{"status": status, "control_flag": None},
			update_modified=False,
		)
		frappe.db.commit()

	def call(self, endpoint: str, campaign: str, **kwargs):
		return getattr(admin, endpoint)(campaign=campaign, **kwargs)

	def state_refusal(self, endpoint: str, campaign: str, **kwargs) -> str | None:
		"""The state machine's refusal message, or None if it did not refuse."""
		try:
			self.call(endpoint, campaign, **kwargs)
		except Exception as exc:  # noqa: BLE001 - the message is the assertion
			message = str(exc)
			return message if STATE_REFUSAL in message else None
		return None


class TestTheMatrixAgreesWithTheDesignTable(AdminApiTestCase):
	"""P7-M2. `BUTTON_MATRIX` is read off `migration/api.py` — the thing it checks.

	So a wrong gate copied into the oracle would pass, and the matrix would certify the
	implementation against itself. The independent source is design §3.3's button table,
	which was written before any of this code. It is **parsed**, not transcribed, so the
	oracle cannot drift from the document the way a copied list drifts from its source.

	The design table and the shipped machine genuinely differ in places, and those
	differences are real decisions (recorded in DECISIONS: `migration/api.py` is the single
	validation path, so where the two disagree the shipped gate wins). Each one therefore has
	to be **declared here with a reason**. That is what makes this bite: a gate quietly
	widened in the implementation and copied into the matrix produces an *undeclared*
	divergence and fails.
	"""

	DESIGN_DOC = "docs/design/delivery-platform.md"

	#: Button label in design §3.3 → the matrix key it corresponds to.
	DESIGN_TO_MATRIX = {
		"Start": "Start",
		"Pause": "Pause",
		"Resume": "Resume",
		"Stop After Current Batch": "Stop After Current Batch",
		"Retry Failed": "Retry Failed",
		"Verify": "Verify",
		"Analyze Storage": "Analyze Storage",
		"Delete Verified Local Copies": "Delete Verified Local Copies (approve)",
	}

	#: Every place the shipped machine differs from the design cell, and why. A divergence
	#: not listed here fails the test.
	DECLARED_DIVERGENCES = {
		"Start": (
			"design says Planned only; the shipped gate also accepts Paused/Stopped/Running "
			"because start_migration is the resume-and-redispatch path and is idempotent"
		),
		"Resume": (
			"design says Paused or Failed; the shipped gate accepts Paused only — a Failed "
			"campaign is restarted through start_migration, which re-runs the queue guard "
			"and the A29 preflight that resume deliberately skips"
		),
		"Stop After Current Batch": (
			"design says Running; the shipped gate also accepts Paused, so an operator who "
			"paused first can still stop without resuming to do it"
		),
		"Analyze Storage": (
			"the design cell is a cross-campaign condition (no campaign in Running/Cleanup "
			"Running), which the shipped code enforces separately via _assert_no_other_active; "
			"the per-campaign gate is Draft/Analyzed/Stopped/Failed"
		),
		"Delete Verified Local Copies": (
			"the design cell names Running plus a batch-level recompute; the shipped gate "
			"also accepts Stopped/Paused/Completed, and the recompute lives per object in "
			"cleanup.py where it cannot be bypassed by a campaign-level status"
		),
	}

	def design_cells(self) -> dict[str, str]:
		"""The Valid-states column of design §3.3, straight out of the file."""
		import pathlib as _pathlib

		root = _pathlib.Path(__file__).resolve().parents[2]
		text = (root / self.DESIGN_DOC).read_text(encoding="utf-8")
		start = text.index("| Button | Endpoint (whitelisted) | Role |")
		end = text.index("Client JS lives in", start)

		cells = {}
		for line in text[start:end].splitlines():
			if not line.startswith("|") or line.startswith("|---"):
				continue
			columns = [column.strip() for column in line.strip().strip("|").split("|")]
			if len(columns) >= 4 and columns[0] != "Button":
				cells[columns[0]] = columns[3]
		return cells

	def test_the_design_table_is_still_parseable(self):
		"""If the table moves or is reformatted, this fails loudly rather than silently
		yielding an empty oracle that agrees with everything."""
		cells = self.design_cells()
		self.assertGreaterEqual(len(cells), 12)
		for label in self.DESIGN_TO_MATRIX:
			self.assertIn(label, cells, f"design §3.3 no longer has a {label!r} row")

	def test_every_divergence_from_the_design_table_is_declared(self):
		cells = self.design_cells()

		for design_label, matrix_key in self.DESIGN_TO_MATRIX.items():
			with self.subTest(button=design_label):
				documented = {status for status in CAMPAIGN_STATUSES if status in cells[design_label]}
				shipped = set(BUTTON_MATRIX[matrix_key][2])

				if documented == shipped:
					self.assertNotIn(
						design_label,
						self.DECLARED_DIVERGENCES,
						f"{design_label} matches the design table; the declared divergence is stale",
					)
					continue

				self.assertIn(
					design_label,
					self.DECLARED_DIVERGENCES,
					f"{design_label} differs from design §3.3 (doc {sorted(documented)} vs "
					f"shipped {sorted(shipped)}) and the difference is not declared",
				)

	def test_no_button_operates_during_a_transitional_state(self):
		"""An A9 property no table states: `Analyzing` and `Stopping` are owned by workers.

		A button valid in either would be issuing an order to a campaign that is mid-move,
		which is what the control flags exist to avoid.
		"""
		for button, (_endpoint, _kwargs, valid) in BUTTON_MATRIX.items():
			for transitional in ("Analyzing", "Stopping"):
				with self.subTest(button=button, state=transitional):
					self.assertNotIn(transitional, valid)


class TestEveryButtonValidatesItsState(AdminApiTestCase):
	def test_every_frozen_state_is_covered_by_the_matrix(self):
		"""Guards the matrix itself: a state added to A9 must not silently go untested."""
		self.assertEqual(len(CAMPAIGN_STATUSES), 11)
		for _button, (_endpoint, _kwargs, valid) in BUTTON_MATRIX.items():
			for state in valid:
				self.assertIn(state, CAMPAIGN_STATUSES, f"{state} is not an A9 state")

	def test_each_button_is_refused_in_every_invalid_state(self):
		campaign = self.campaign(title="state matrix")
		for button, (endpoint, kwargs, valid) in BUTTON_MATRIX.items():
			for state in CAMPAIGN_STATUSES:
				if state in valid:
					continue
				with self.subTest(button=button, state=state):
					self.set_status(campaign, state)
					refusal = self.state_refusal(endpoint, campaign, **kwargs)
					self.assertIsNotNone(
						refusal,
						f"{button} was not refused by the state machine while the campaign was {state}",
					)
					self.assertEqual(
						frappe.db.get_value(CAMPAIGN_DOCTYPE, campaign, "status"),
						state,
						f"{button} changed the status despite being refused in {state}",
					)

	def test_each_button_passes_the_state_gate_in_every_valid_state(self):
		campaign = self.campaign(title="state matrix valid")
		for button, (endpoint, kwargs, valid) in BUTTON_MATRIX.items():
			for state in valid:
				with self.subTest(button=button, state=state):
					self.set_status(campaign, state)
					self.assertIsNone(
						self.state_refusal(endpoint, campaign, **kwargs),
						f"{button} was refused by the state machine in {state}, which is valid",
					)


class TestReconcileRefusesDuringACampaign(AdminApiTestCase):
	"""Reconcile has no campaign argument; its state gate is "nothing is in flight"."""

	def test_it_is_refused_while_any_campaign_is_active(self):
		from cloud_file_storage.cloud_file_storage.doctype.cloud_migration_campaign.cloud_migration_campaign import (
			ACTIVE_STATUSES,
		)

		campaign = self.campaign(title="reconcile gate")
		for state in ACTIVE_STATUSES:
			with self.subTest(state=state):
				self.set_status(campaign, state)
				with self.assertRaises(frappe.ValidationError) as caught:
					admin.reconcile()
				self.assertIn("Reconcile compares the bucket", str(caught.exception))

	def test_it_runs_once_no_campaign_is_active(self):
		campaign = self.campaign(title="reconcile ok")
		self.set_status(campaign, "Completed")
		result = admin.reconcile()
		self.assertIn("remote_objects", result)


class TestReportingIsNeverRefused(AdminApiTestCase):
	"""Export is read-only: an operator diagnosing a stuck campaign must not be locked out."""

	def test_the_report_exports_in_every_state(self):
		campaign = self.campaign(title="report in every state")
		for state in CAMPAIGN_STATUSES:
			with self.subTest(state=state):
				self.set_status(campaign, state)
				path = admin.export_migration_report(campaign=campaign)
				self.assertTrue(path)


class TestCleanupTypeToConfirm(AdminApiTestCase):
	"""The phrase is a server-side gate, not a dialog (PLAN §C security).

	Every fixture below is a campaign on which cleanup would otherwise **succeed**: approved,
	Running, in a mode that permits it. That matters more than it looks. An earlier version of
	this class left the site in LOCAL_ONLY, so `start_cleanup` threw "cleanup is not allowed
	in this mode" and every `assertRaises(ValidationError)` passed — including for a phrase
	comparison that had been deliberately loosened to accept `delete local files`. The
	assertions therefore match the confirm gate's own wording, and there is a positive control
	proving the correct phrase gets all the way through.
	"""

	#: The fragment only `_assert_confirm_phrase` produces.
	CONFIRM_REFUSAL = "exactly to confirm"

	def _ready_for_cleanup(self) -> str:
		from frappe.utils import now_datetime

		from cloud_file_storage.tests.utils import set_mode

		set_mode("S3_PRIMARY_LOCAL_FALLBACK")
		campaign = self.campaign(title="confirm phrase")
		frappe.db.set_value(
			CAMPAIGN_DOCTYPE,
			campaign,
			{
				"status": "Running",
				"cleanup_approved_by": "Administrator",
				"cleanup_approved_at": now_datetime(),
			},
			update_modified=False,
		)
		frappe.db.commit()
		return campaign

	def test_the_correct_phrase_gets_all_the_way_through(self):
		"""The positive control. Without it every refusal below could be another gate."""
		campaign = self._ready_for_cleanup()
		admin.start_cleanup(campaign=campaign, confirm_phrase=migration_api.CLEANUP_CONFIRM_PHRASE)
		self.assertEqual(frappe.db.get_value(CAMPAIGN_DOCTYPE, campaign, "status"), "Cleanup Running")

	def test_the_phrase_is_required(self):
		campaign = self._ready_for_cleanup()
		with self.assertRaises(frappe.ValidationError) as caught:
			admin.start_cleanup(campaign=campaign)
		self.assertIn(self.CONFIRM_REFUSAL, str(caught.exception))
		self.assertEqual(frappe.db.get_value(CAMPAIGN_DOCTYPE, campaign, "status"), "Running")

	def test_a_near_miss_is_refused_by_the_confirm_gate(self):
		campaign = self._ready_for_cleanup()
		for supplied in (
			"delete local files",
			"Delete Local Files",
			"DELETE LOCAL FILE",
			" DELETE LOCAL FILES",
			"DELETE LOCAL FILES ",
			"DELETE  LOCAL FILES",
			"DELETE_LOCAL_FILES",
			"",
			"yes",
		):
			with self.subTest(supplied=supplied):
				# Reset per iteration: one phrase that slipped through would otherwise move
				# the campaign and make every later assertion fail for that reason instead.
				self.set_status(campaign, "Running")
				with self.assertRaises(frappe.ValidationError) as caught:
					admin.start_cleanup(campaign=campaign, confirm_phrase=supplied)
				self.assertIn(
					self.CONFIRM_REFUSAL,
					str(caught.exception),
					"refused, but by some other gate — this campaign is otherwise ready",
				)
				self.assertEqual(
					frappe.db.get_value(CAMPAIGN_DOCTYPE, campaign, "status"),
					"Running",
					"a mistyped phrase still moved the campaign into cleanup",
				)

	def test_the_gate_lives_in_the_choke_point_not_the_button(self):
		"""So the CLI carries it too. Asserted against `migration.api`, not `api.admin`."""
		source = inspect.getsource(migration_api.start_cleanup.__wrapped__)
		self.assertIn("_assert_confirm_phrase(confirm_phrase)", source)

		wrapper = inspect.getsource(admin.start_cleanup.__wrapped__)
		self.assertNotIn(migration_api.CLEANUP_CONFIRM_PHRASE, wrapper)

	def test_the_cli_command_hands_the_phrase_through(self):
		from cloud_file_storage import commands

		options = {option.name for option in commands.migrate_cleanup.params}
		self.assertIn("confirm_phrase", options)

	def test_starting_a_cleanup_writes_an_audit_row(self):
		from cloud_file_storage.migration import audit

		campaign = self._ready_for_cleanup()
		before = audit.count("campaign_transition", campaign=campaign)

		admin.start_cleanup(campaign=campaign, confirm_phrase=migration_api.CLEANUP_CONFIRM_PHRASE)

		rows = frappe.get_all(
			audit.AUDIT_DOCTYPE,
			filters={"campaign": campaign, "action": "campaign_transition"},
			fields=["details"],
		)
		self.assertGreater(len(rows), before)
		self.assertTrue(
			any("start_cleanup" in (row.details or "") for row in rows),
			"no audit row names the cleanup start",
		)


class TestCampaignCreationCannotPresetDestruction(AdminApiTestCase):
	"""P7-H1. `create_campaign` is reachable by the operator role since P7 widened `_guard`.

	The hole was not that the operator could delete anything — they cannot approve or start a
	cleanup. It was that a campaign could be **created** pre-set to `Direct Delete`, so the
	System Manager who later approves and starts it approves a decision someone else took,
	without that choice appearing anywhere on the approval path. The approval stays real only
	if it covers everything destructive about the run.
	"""

	def test_cleanup_mode_is_refused_at_creation(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			admin.create_campaign(title="preset direct delete", cleanup_mode="Direct Delete")
		self.assertIn("cleanup_mode", str(caught.exception))

	def test_quarantine_ttl_is_refused_at_creation(self):
		"""Same shape one step later: how long a quarantined local copy survives."""
		with self.assertRaises(frappe.ValidationError) as caught:
			admin.create_campaign(title="preset ttl", quarantine_ttl_days=0)
		self.assertIn("quarantine_ttl_days", str(caught.exception))

	def test_a_refused_creation_leaves_no_campaign_behind(self):
		before = frappe.db.count(CAMPAIGN_DOCTYPE)
		with self.assertRaises(frappe.ValidationError):
			admin.create_campaign(title="never created", cleanup_mode="Direct Delete")
		self.assertEqual(frappe.db.count(CAMPAIGN_DOCTYPE), before)

	def test_a_campaign_created_normally_quarantines(self):
		"""The default is the safe one, and it is asserted rather than assumed."""
		campaign = self.campaign(title="default cleanup mode")
		self.assertEqual(frappe.db.get_value(CAMPAIGN_DOCTYPE, campaign, "cleanup_mode"), "Quarantine")
		self.assertEqual(cint(frappe.db.get_value(CAMPAIGN_DOCTYPE, campaign, "quarantine_ttl_days")), 14)

	def test_the_harmless_policy_fields_still_work(self):
		"""The fix must not have closed the whole door: scope and retry shaping stay settable."""
		campaign = admin.create_campaign(
			title="scoped campaign", include_public=0, max_attempts=3, dedupe_by_content_hash=0
		)
		self._campaigns.append(campaign)
		row = frappe.db.get_value(
			CAMPAIGN_DOCTYPE, campaign, ["include_public", "max_attempts"], as_dict=True
		)
		self.assertEqual(cint(row.include_public), 0)
		self.assertEqual(cint(row.max_attempts), 3)

	def test_direct_delete_is_still_reachable_where_it_belongs(self):
		"""Refusing it at creation is only correct because the manager-gated path survives."""
		from frappe.utils import now_datetime

		from cloud_file_storage.tests.utils import set_mode

		set_mode("S3_PRIMARY_LOCAL_FALLBACK")
		campaign = self.campaign(title="direct delete at the right moment")
		frappe.db.set_value(
			CAMPAIGN_DOCTYPE,
			campaign,
			{
				"status": "Running",
				"cleanup_approved_by": "Administrator",
				"cleanup_approved_at": now_datetime(),
			},
			update_modified=False,
		)
		frappe.db.commit()

		admin.start_cleanup(
			campaign=campaign,
			confirm_phrase=migration_api.CLEANUP_CONFIRM_PHRASE,
			direct_delete=True,
		)
		self.assertEqual(frappe.db.get_value(CAMPAIGN_DOCTYPE, campaign, "cleanup_mode"), "Direct Delete")

	def test_the_operator_role_holds_no_write_on_the_campaign_doctype(self):
		"""The other route to the same field. Refusing it in the API is only a fix if the
		document itself is not writable by that role."""
		meta = frappe.get_meta(CAMPAIGN_DOCTYPE)
		for perm in meta.permissions:
			if perm.role != "Cloud Storage Manager":
				continue
			with self.subTest(permlevel=perm.permlevel or 0):
				self.assertFalse(perm.write, "the operator role can write campaigns directly")


class TestReportExportPathIsBounded(AdminApiTestCase):
	"""M-3. `export_report` joins `campaign` into a filesystem path and is operator-reachable.

	The bound was always narrow — campaign names are `format:CFS-CAMP-{####}`, so a traversing
	value names no real campaign and the file would be header-only — but "the primitive is only
	good for creating an empty file somewhere writable" stops being true the moment the
	filename or the column set changes. Two independent guards: the campaign must exist, and
	the value is reduced to its basename before the join.
	"""

	def test_a_traversing_campaign_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			admin.export_migration_report(campaign="../../../../tmp/cfs-traversal")

	def test_an_absolute_path_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			admin.export_migration_report(campaign="/tmp/cfs-absolute")

	def test_a_campaign_that_does_not_exist_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			admin.export_migration_report(campaign="CFS-CAMP-9999")

	def test_nothing_is_written_outside_the_private_files_directory(self):
		"""The property, not the mechanism: whatever the input, the file lands in one place."""
		import os

		before = set(os.listdir("/tmp"))
		for hostile in ("../../../../tmp/cfs-escape", "/tmp/cfs-escape2", "..", "."):
			with self.subTest(campaign=hostile), self.assertRaises(frappe.ValidationError):
				admin.export_migration_report(campaign=hostile)
		self.assertEqual(
			{name for name in set(os.listdir("/tmp")) - before if "cfs-escape" in name},
			set(),
		)

	def test_a_real_campaign_still_exports(self):
		"""The positive control — refusing everything would pass every test above."""
		campaign = self.campaign(title="export still works")
		path = admin.export_migration_report(campaign=campaign)
		self.assertIn(campaign, path)
		self.assertTrue(path.endswith("-migration-report.csv"))

	def test_the_returned_path_is_described_correctly(self):
		"""N-3: `get_site_path` is site-relative, and this string is shown to the operator."""
		import inspect
		import os

		from cloud_file_storage.migration import report

		campaign = self.campaign(title="export path shape")
		path = report.export_campaign_report(campaign)
		self.assertFalse(os.path.isabs(path), "the path is absolute after all")
		self.assertIn("site-relative", inspect.getdoc(report.export_campaign_report))


class TestAdminIsOnlyWrappers(AdminApiTestCase):
	"""A9: `api/admin.py` contains only thin `frappe.only_for` wrappers."""

	CHOKE_POINTS = {"migration_api", "diagnostics", "health_module"}

	def _functions(self):
		tree = ast.parse(inspect.getsource(admin))
		return [node for node in tree.body if isinstance(node, ast.FunctionDef)]

	def test_every_endpoint_is_a_gate_and_a_delegation(self):
		for function in self._functions():
			with self.subTest(endpoint=function.name):
				body = [node for node in function.body if not isinstance(node, ast.Expr | ast.Pass)]
				gate = function.body[0]
				self.assertIsInstance(gate, ast.Expr)
				self.assertEqual(getattr(gate.value.func, "attr", None), "only_for")

				self.assertEqual(len(body), 1, f"{function.name} has logic beyond its delegation")
				self.assertIsInstance(body[0], ast.Return)
				self.assertIsInstance(body[0].value, ast.Call)

	def test_every_argument_forwarded_is_a_bare_parameter(self):
		"""P7-M1. A constant kwarg is a decision, and this module makes none.

		The reviewer's example is the one that matters: `start_cleanup(...,
		direct_delete=True)` hardcoded in the wrapper passed every other check here — a
		wrapper "deciding nothing" while deciding the most destructive thing in the app. So
		every argument must be a bare name that appears in the wrapper's own signature;
		nothing may be computed, defaulted or constant at this layer.
		"""
		for function in self._functions():
			parameters = {
				argument.arg for group in (function.args.args, function.args.kwonlyargs) for argument in group
			}
			if function.args.kwarg:
				parameters.add(function.args.kwarg.arg)

			call = function.body[-1].value
			with self.subTest(endpoint=function.name):
				for index, argument in enumerate(call.args):
					self.assertIsInstance(argument, ast.Name, f"positional arg {index} is not a bare name")
					self.assertIn(argument.id, parameters)

				for keyword in call.keywords:
					label = keyword.arg or "**"
					self.assertIsInstance(
						keyword.value,
						ast.Name,
						f"{function.name} passes a computed or constant value as {label}",
					)
					self.assertIn(keyword.value.id, parameters)

	def test_no_endpoint_carries_a_decorator_other_than_whitelist(self):
		"""P7-M1. `assertIn` allowed extras, so an imported `@precondition` passed."""
		for function in self._functions():
			with self.subTest(endpoint=function.name):
				self.assertEqual(
					[ast.unparse(node) for node in function.decorator_list],
					["frappe.whitelist()"],
				)

	def test_every_delegation_targets_a_choke_point(self):
		for function in self._functions():
			with self.subTest(endpoint=function.name):
				call = function.body[-1].value
				target = call.func
				self.assertIsInstance(target, ast.Attribute)
				self.assertIn(target.value.id, self.CHOKE_POINTS)

	def test_no_endpoint_decides_anything(self):
		"""No branch, no loop, no throw, no db access anywhere in the module's functions."""
		# BoolOp and IfExp are the two that read as "no logic" and are not: `batch_size or
		# 5000` and `x if y else z` both decide something (P7-M1).
		forbidden = (
			ast.If,
			ast.For,
			ast.While,
			ast.Try,
			ast.With,
			ast.Assign,
			ast.Compare,
			ast.BoolOp,
			ast.IfExp,
		)
		for function in self._functions():
			with self.subTest(endpoint=function.name):
				for node in ast.walk(function):
					self.assertNotIsInstance(node, forbidden)
				source = ast.unparse(function)
				for banned in ("frappe.throw", "frappe.db", "_assert", "get_value"):
					self.assertNotIn(banned, source)

	def test_every_endpoint_is_whitelisted(self):
		for function in self._functions():
			with self.subTest(endpoint=function.name):
				decorators = [ast.unparse(node) for node in function.decorator_list]
				self.assertIn("frappe.whitelist()", decorators)
				self.assertTrue(getattr(getattr(admin, function.name), "__wrapped__", None))

	def test_the_twelve_buttons_all_have_an_endpoint(self):
		"""PLAN §B P7: twelve mission buttons. Delete Verified Local Copies is two actions."""
		buttons = {
			"Test Connection": ("test_connection",),
			"Analyze Storage": ("analyze_storage",),
			"Build Migration Plan": ("build_migration_plan",),
			"Start": ("start_campaign",),
			"Pause": ("pause_campaign",),
			"Resume": ("resume_campaign",),
			"Stop After Current Batch": ("stop_after_current_batch",),
			"Retry Failed": ("retry_failed",),
			"Verify": ("start_verify",),
			"Reconcile": ("reconcile",),
			"Delete Verified Local Copies": ("approve_cleanup", "start_cleanup"),
			"Export Migration Report": ("export_migration_report",),
		}
		self.assertEqual(len(buttons), 12)
		for button, endpoints in buttons.items():
			for endpoint in endpoints:
				with self.subTest(button=button, endpoint=endpoint):
					self.assertTrue(hasattr(admin, endpoint))
					self.assertTrue(getattr(getattr(admin, endpoint), "__wrapped__", None))
