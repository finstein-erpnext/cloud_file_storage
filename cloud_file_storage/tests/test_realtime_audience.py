"""N-1 — who receives the migration snapshot.

The finding was that `publish_realtime(..., user="Administrator")` is the narrowest possible
audience, so for every non-Administrator operator the dashboard's realtime binding never fired
and the Page ran on its 30s poll alone. The existing tests asserted that a binding *exists* and
that the event name matches; nothing asserted who could hear it, so they were green for a
feature inert for its intended users.

**The obvious fix is worse than the bug.** With neither `user` nor `room`, frappe resolves the
room to `get_site_room()` — `"all"` — and campaign internals reach every Desk user. So these
tests assert the **audience**, not that an event fires: a test written as "a subscriber
receives it" passes identically for a broadcast, which would be the same defect one layer up.
`test_a_broadcast_would_fail_this_suite` is the control that proves it.
"""

from unittest.mock import patch

import frappe

from cloud_file_storage.migration import report
from cloud_file_storage.tests.desk_utils import (
	CLOUD_STORAGE_MANAGER,
	SYSTEM_MANAGER,
	drop_users,
	ensure_user,
)
from cloud_file_storage.tests.migration_utils import MigrationTestCase


class RealtimeTestCase(MigrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.operator = ensure_user("cfs-rt-operator@example.com", (CLOUD_STORAGE_MANAGER,))
		cls.manager = ensure_user("cfs-rt-manager@example.com", (SYSTEM_MANAGER,))
		cls.outsider = ensure_user("cfs-rt-outsider@example.com", ())

	@classmethod
	def tearDownClass(cls):
		drop_users(cls.operator, cls.manager, cls.outsider)
		super().tearDownClass()

	def publish(self, campaign: str) -> list[dict]:
		"""Run one publish, returning every `publish_realtime` call it made."""
		calls: list[dict] = []

		def spy(event, message=None, **kwargs):
			calls.append({"event": event, "message": message, **kwargs})

		report.reset_publish_throttle()
		with patch.object(frappe, "publish_realtime", spy):
			report.publish_campaign_snapshot(campaign, force=True)
		return calls


class TestTheAudienceIsTheDashboardsAudience(RealtimeTestCase):
	def test_both_operating_roles_receive_the_snapshot(self):
		campaign = self.campaign(title="realtime audience")
		recipients = {call.get("user") for call in self.publish(campaign)}

		self.assertIn(self.manager, recipients)
		self.assertIn(self.operator, recipients)

	def test_a_user_with_neither_role_does_not(self):
		"""The other half. An audience that includes everyone is not an audience."""
		campaign = self.campaign(title="realtime outsider")
		recipients = {call.get("user") for call in self.publish(campaign)}

		self.assertNotIn(self.outsider, recipients)
		self.assertNotIn("Guest", recipients)

	def test_administrator_still_receives_it(self):
		"""It holds every role implicitly and may carry no Has Role row, so a role query
		alone would drop the one user the previous implementation did reach."""
		campaign = self.campaign(title="realtime admin")
		self.assertIn("Administrator", {call.get("user") for call in self.publish(campaign)})

	def test_a_disabled_user_is_dropped(self):
		campaign = self.campaign(title="realtime disabled")
		frappe.db.set_value("User", self.operator, "enabled", 0)
		frappe.db.commit()
		self.addCleanup(frappe.db.commit)
		self.addCleanup(frappe.db.set_value, "User", self.operator, "enabled", 1)

		self.assertNotIn(self.operator, {call.get("user") for call in self.publish(campaign)})

	def test_a_website_user_holding_the_role_is_not_in_the_audience(self):
		"""L-5. The docstring says "may open the dashboard", so the query has to mean that.

		A Website User with Cloud Storage Manager holds the role and cannot open a Desk Page
		at all, so including them sends campaign snapshots to somebody who can never act on
		them — the audience being wrong in the safe direction is still the audience being
		wrong.
		"""
		website_user = ensure_user("cfs-rt-website@example.com", (CLOUD_STORAGE_MANAGER,))
		# Dropped rather than restored: a restore whose failure is silent leaves a Cloud
		# Storage Manager permanently a Website User, and `drop_users` verifies removal.
		self.addCleanup(drop_users, website_user)
		frappe.db.set_value("User", website_user, "user_type", "Website User")
		frappe.db.commit()

		self.assertNotIn(website_user, report.dashboard_audience())

		campaign = self.campaign(title="realtime website user")
		self.assertNotIn(website_user, {call.get("user") for call in self.publish(campaign)})

	def test_a_system_user_holding_the_role_is(self):
		"""The positive control for the filter above."""
		self.assertIn(self.operator, report.dashboard_audience())

	def test_the_audience_matches_the_roles_the_page_admits(self):
		"""Bound to the Page's own DocPerm rows, so the two cannot drift apart."""
		page_roles = {row.role for row in frappe.get_doc("Page", "cloud-migration-dashboard").roles}
		self.assertEqual(set(report.DASHBOARD_ROLES), page_roles)


class TestNothingIsBroadcast(RealtimeTestCase):
	def assert_every_publish_is_targeted(self, calls):
		"""The audience assertion itself, factored out so a control can **catch** it.

		Deliberately not wrapped in `subTest`: a failure inside a subTest context is recorded
		on the result rather than raised, so `assertRaises` in the control below would never
		see it and the control would report a failure of its own instead of catching one.
		That is the difference between a control that runs the real assertion and one that
		only appears to.
		"""
		self.assertTrue(calls, "nothing was published at all")
		for call in calls:
			self.assertTrue(call.get("user"), f"a publish went out with no user: {call}")
			self.assertIsNone(call.get("room"), f"a publish named a room directly: {call}")

	def test_every_publish_names_a_single_user(self):
		campaign = self.campaign(title="realtime targeted")
		self.assert_every_publish_is_targeted(self.publish(campaign))

	def test_no_publish_targets_the_site_room(self):
		"""`get_site_room()` is the literal string 'all' and reaches every Desk user."""
		from frappe.realtime import get_site_room

		campaign = self.campaign(title="realtime not all")
		for call in self.publish(campaign):
			with self.subTest(call=call):
				self.assertNotEqual(call.get("room"), get_site_room())
				self.assertNotEqual(call.get("user"), get_site_room())

	def test_a_broadcast_makes_the_real_assertion_fail(self):
		"""M-7. The control **runs** the real assertion against a broadcast.

		The first version of this defined a local `broadcast()` and then asserted, inline,
		that a check *of this shape* would notice it — its own copy of the assertion in
		`test_every_publish_names_a_single_user`, so the real one was never invoked. It
		simulated the mutation instead of performing it, while its docstring claimed to prove
		the suite notices. Loosening the real assertion would have left this green.

		This version patches the implementation to broadcast and calls the real test method,
		so it fails the moment that assertion stops catching a broadcast. Verified by
		weakening the real assertion and watching this go red.
		"""

		def broadcast(campaign_name, *, force=False):
			# What a well-meaning fix for "nobody receives this" looks like. frappe resolves
			# a publish with neither `user` nor `room` to the site room, `"all"`.
			frappe.publish_realtime(report.REALTIME_EVENT, report.campaign_snapshot(campaign_name))

		with patch.object(report, "publish_campaign_snapshot", broadcast):
			with self.assertRaises(AssertionError):
				self.test_every_publish_names_a_single_user()

	def test_a_room_broadcast_also_makes_it_fail(self):
		"""The other broadcast shape: an explicit `room="all"` rather than an omitted one.

		Worth its own case because a loosened assertion of the form
		`user or room` would accept this while still rejecting the bare form.
		"""
		from frappe.realtime import get_site_room

		def broadcast(campaign_name, *, force=False):
			frappe.publish_realtime(
				report.REALTIME_EVENT, report.campaign_snapshot(campaign_name), room=get_site_room()
			)

		with patch.object(report, "publish_campaign_snapshot", broadcast):
			with self.assertRaises(AssertionError):
				self.test_every_publish_names_a_single_user()


class TestOneBadRecipientDoesNotSilenceTheRest(RealtimeTestCase):
	"""N-5. The loop was wrapped in a single `try`, so a failure on recipient 3 dropped 4..N.

	The existing hiccup test used `side_effect` on every call, so it never covered the partial
	case — every recipient failed and "the batch survived" was true either way. This fails one
	recipient and asserts the others still received it.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.operator = ensure_user("cfs-rt-partial@example.com", (CLOUD_STORAGE_MANAGER,))
		cls.manager = ensure_user("cfs-rt-partial2@example.com", (SYSTEM_MANAGER,))

	@classmethod
	def tearDownClass(cls):
		drop_users(cls.operator, cls.manager)
		super().tearDownClass()

	def test_the_recipients_after_a_failing_one_still_receive_it(self):
		campaign = self.campaign(title="realtime partial failure")
		audience = report.dashboard_audience()
		self.assertGreaterEqual(len(audience), 3, "need several recipients for this to mean anything")
		victim = audience[len(audience) // 2]

		delivered: list[str] = []

		def flaky(event, message=None, **kwargs):
			if kwargs.get("user") == victim:
				raise RuntimeError("socketio refused this one")
			delivered.append(kwargs.get("user"))

		report.reset_publish_throttle()
		with patch.object(frappe, "publish_realtime", flaky):
			self.assertIsNotNone(report.publish_campaign_snapshot(campaign, force=True))

		self.assertNotIn(victim, delivered)
		self.assertEqual(
			sorted(delivered),
			sorted(user for user in audience if user != victim),
			"a failure on one recipient dropped the ones after it",
		)


class TestTheSnapshotStillPublishes(RealtimeTestCase):
	def test_the_event_name_is_the_one_the_page_binds(self):
		campaign = self.campaign(title="realtime event name")
		calls = self.publish(campaign)
		self.assertTrue(all(call["event"] == report.REALTIME_EVENT for call in calls))

	def test_the_payload_is_the_campaign_snapshot(self):
		campaign = self.campaign(title="realtime payload")
		calls = self.publish(campaign)
		self.assertTrue(calls)
		self.assertEqual(calls[0]["message"]["name"], campaign)

	def test_a_realtime_failure_never_breaks_the_batch(self):
		"""A migration must not fail because socketio did."""
		campaign = self.campaign(title="realtime hiccup")
		report.reset_publish_throttle()
		with patch.object(frappe, "publish_realtime", side_effect=RuntimeError("redis down")):
			self.assertIsNotNone(report.publish_campaign_snapshot(campaign, force=True))
