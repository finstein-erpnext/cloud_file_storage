"""T-GATE — nothing local is deleted until every gate holds, re-checked at deletion time.

The shape of every test here is the same: satisfy all the gates but one, run CLEANUP, and
assert the local file is still exactly where it was. That is docs/INVARIANTS.md invariant 1 stated as
a test rather than as a comment.

The A32 quarantine layout and the DB-keyed TTL purge are here too, because both are about
the same thing: a quarantined file is somebody's only remaining local copy until the purge,
and anything that could overwrite or prematurely remove it is a second data loss.
"""

import os
from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, cint, now_datetime

from cloud_file_storage.migration import api, audit, cleanup, engine
from cloud_file_storage.storage import objects
from cloud_file_storage.tests.migration_utils import MigrationTestCase, approved_cleanup
from cloud_file_storage.tests.utils import set_mode

CAMPAIGN_DOCTYPE = "Cloud Migration Campaign"
OBJECT_DOCTYPE = "Cloud Migration Object"


class CleanupTestCase(MigrationTestCase):
	def migrated(self, *, file_name, content=b"cleanup bytes", mode="S3_ONLY", **policy):
		"""One verified, linked object with its local copy still in place."""
		doc = self.local_file(file_name=file_name, content=content)
		campaign = self.campaign(**policy)
		self.migrate(campaign, mode=mode)

		obj = self.object_for(campaign, doc.file_url)
		self.assertEqual(obj.status, "Verified", "the fixture did not reach a cleanable state")
		self.assertTrue(self.local_exists(doc))
		return doc, campaign, obj

	def run_cleanup(self, campaign: str) -> dict:
		camp = frappe.get_doc(CAMPAIGN_DOCTYPE, campaign)
		results = {"cleaned": 0, "blocked": 0}
		for batch in frappe.db.get_all(
			"Cloud Migration Batch", filters={"campaign": campaign}, pluck="name", limit_page_length=0
		):
			for obj in engine.iter_batch_objects(batch, cleanup.CLEANABLE_STATUSES):
				if cleanup.cleanup_object(obj, camp):
					results["cleaned"] += 1
				else:
					results["blocked"] += 1
		return results

	def assertBlocked(self, doc, campaign, obj, *, reason_contains: str):
		result = self.run_cleanup(campaign)
		self.assertEqual(result["cleaned"], 0, "a gate did not hold and the file was moved")
		self.assertTrue(self.local_exists(doc), "the local file was removed despite a failed gate")
		conflicts = self.conflicts_of(campaign, conflict_type="cleanup_blocked")
		self.assertTrue(conflicts, "nothing recorded why cleanup was refused")
		self.assertIn(
			reason_contains,
			" ".join(row.details or "" for row in conflicts),
			f"the recorded reason does not mention {reason_contains!r}",
		)


class TestCleanupGates(CleanupTestCase):
	def test_the_happy_path_quarantines_and_audits(self):
		doc, campaign, obj = self.migrated(file_name="cleanup-happy.txt")
		with approved_cleanup(campaign):
			result = self.run_cleanup(campaign)

		self.assertEqual(result["cleaned"], 1)
		self.assertFalse(self.local_exists(doc), "the verified local copy should have moved")

		row = frappe.db.get_value(
			OBJECT_DOCTYPE, obj.name, ["status", "quarantine_path", "cleaned_at"], as_dict=True
		)
		self.assertEqual(row.status, "Quarantined")
		self.assertTrue(os.path.exists(row.quarantine_path), "the bytes are gone, not quarantined")
		self.assertIsNotNone(row.cleaned_at)
		self.assertIn("local_quarantine", self.audit_actions(campaign))

	def test_cleanup_without_approval_deletes_nothing(self):
		doc, campaign, obj = self.migrated(file_name="cleanup-unapproved.txt")
		self.assertBlocked(doc, campaign, obj, reason_contains="cleanup has not been approved")

	def test_the_batch_job_refuses_outright_without_approval(self):
		"""The campaign-level gate, re-read inside the job rather than trusted from the button."""
		doc, campaign, obj = self.migrated(file_name="cleanup-job-unapproved.txt")
		batch = obj.batch
		frappe.db.set_value(
			"Cloud Migration Batch", batch, "status", "CleanupDispatched", update_modified=False
		)
		frappe.db.commit()

		result = cleanup.run_cleanup_batch(campaign, batch)
		self.assertIn("refused", result)
		self.assertTrue(self.local_exists(doc))
		self.assertIn("cleanup_refused", self.audit_actions(campaign))

	def test_cleanup_is_refused_in_a_mode_that_promises_a_local_copy(self):
		"""ADR-M16 — DUAL_WRITE says the bytes are in both places; cleanup would make that false."""
		doc, campaign, obj = self.migrated(file_name="cleanup-mode.txt", mode="S3_PRIMARY_LOCAL_FALLBACK")
		set_mode("DUAL_WRITE")
		with approved_cleanup(campaign):
			camp = frappe.get_doc(CAMPAIGN_DOCTYPE, campaign)
			allowed, reason = cleanup.campaign_cleanup_gate(camp)

		self.assertFalse(allowed)
		self.assertIn("DUAL_WRITE", reason)
		self.assertTrue(self.local_exists(doc))

	def test_a_ref_on_an_ignored_doctype_is_a_hard_refusal(self):
		"""A17 — LOCAL_OPERATIONAL bytes are consumed through local paths."""
		doc = self.local_file(file_name="cleanup-ignored.txt", content=b"import payload")
		sibling = self.url_sibling(doc, file_name="cleanup-ignored-2.txt")
		frappe.db.set_value(
			"File",
			sibling.name,
			{"attached_to_doctype": "Data Import", "attached_to_name": "DI-CLEAN-0001"},
			update_modified=False,
		)
		frappe.db.commit()

		campaign = self.campaign()
		self.migrate(campaign, mode="S3_ONLY")
		obj = self.object_for(campaign, doc.file_url)
		self.assertEqual(cint(obj.ignored_ref_count), 1, "the fixture did not produce an ignored ref")

		with approved_cleanup(campaign):
			self.assertBlocked(doc, campaign, obj, reason_contains="LOCAL_OPERATIONAL")

	def test_a_superseded_ref_bars_the_object_from_cleanup(self):
		"""A26 — the runtime won, so the local file is not ours to move."""
		doc, campaign, obj = self.migrated(file_name="cleanup-superseded.txt")
		frappe.db.set_value(
			OBJECT_DOCTYPE, obj.name, "skip_reason", "superseded_by_runtime", update_modified=False
		)
		frappe.db.commit()

		with approved_cleanup(campaign):
			self.assertBlocked(doc, campaign, obj, reason_contains="superseded")

	def test_an_unlinked_ref_bars_the_object_from_cleanup(self):
		doc, campaign, obj = self.migrated(file_name="cleanup-unlinked.txt")
		ref = frappe.db.get_value("Cloud Migration File Ref", {"migration_object": obj.name}, "name")
		frappe.db.set_value("Cloud Migration File Ref", ref, "linked", 0, update_modified=False)
		frappe.db.commit()

		with approved_cleanup(campaign):
			self.assertBlocked(doc, campaign, obj, reason_contains="link")

	def test_a_remote_object_that_vanished_since_verify_blocks_the_deletion(self):
		"""The fresh pre-delete check — the whole point of re-verifying at deletion time."""
		doc, campaign, obj = self.migrated(file_name="cleanup-vanished.txt")
		cso = objects.get_cso(obj.cloud_storage_object)
		self.store.objects.pop(cso.s3_key)

		with approved_cleanup(campaign):
			self.assertBlocked(doc, campaign, obj, reason_contains="pre-delete verification failed")

	def test_remote_bytes_that_changed_since_verify_block_the_deletion(self):
		doc, campaign, obj = self.migrated(file_name="cleanup-changed.txt")
		cso = objects.get_cso(obj.cloud_storage_object)
		self.store.objects[cso.s3_key] = b"different bytes entirely"

		with approved_cleanup(campaign):
			self.assertBlocked(doc, campaign, obj, reason_contains="pre-delete verification failed")


class TestLocalRecheck(CleanupTestCase):
	"""A4 — re-stat, and on drift re-hash the LOCAL file before touching it."""

	def test_a_same_size_overwrite_is_caught_by_the_re_hash(self):
		"""The case a stat comparison misses, and the one that would destroy the new bytes."""
		doc, campaign, obj = self.migrated(file_name="a4-samesize.txt", content=b"aaaaaaaaaaaaaaaa")

		path = doc._canonical_local_path()
		with open(path, "wb") as handle:
			handle.write(b"bbbbbbbbbbbbbbbb")  # same length, different content
		os.utime(path, (0, 0))

		with approved_cleanup(campaign):
			self.assertBlocked(doc, campaign, obj, reason_contains="local content drifted")

	def test_a_size_change_is_caught_by_the_cheap_check(self):
		doc, campaign, obj = self.migrated(file_name="a4-size.txt", content=b"original length")
		with open(doc._canonical_local_path(), "wb") as handle:
			handle.write(b"a considerably longer replacement")

		with approved_cleanup(campaign):
			self.assertBlocked(doc, campaign, obj, reason_contains="local size drifted")

	def test_an_untouched_file_passes_the_recheck(self):
		"""The counterpart: without it the drift assertions could hold for the wrong reason."""
		doc, campaign, obj = self.migrated(file_name="a4-untouched.txt")
		row = frappe.db.get_value(OBJECT_DOCTYPE, obj.name, "*", as_dict=True)
		allowed, reason = cleanup.local_recheck(row)
		self.assertTrue(allowed, reason)


class TestQuarantineLayout(CleanupTestCase):
	"""A32 — collision-free paths, no overwrite, no copy-then-delete fallback."""

	def test_the_path_is_campaign_and_object_scoped(self):
		path = cleanup.quarantine_path("CFS-CAMP-0001", "abc123")
		self.assertTrue(path.endswith(os.path.join("CFS-CAMP-0001", "abc123")))
		self.assertIn(cleanup.QUARANTINE_DIRNAME, path)

	def test_two_files_with_the_same_basename_cannot_collide(self):
		"""The failure a `<trash>/<basename>` layout has: `a/report.pdf` and `b/report.pdf`."""
		first = cleanup.quarantine_path("CFS-CAMP-0001", "object-one")
		second = cleanup.quarantine_path("CFS-CAMP-0001", "object-two")
		self.assertNotEqual(first, second)

	def test_quarantine_is_outside_the_served_file_trees(self):
		root = os.path.realpath(cleanup.quarantine_root())
		public = os.path.realpath(frappe.get_site_path("public"))
		private_files = os.path.realpath(frappe.get_site_path("private", "files"))
		self.assertFalse(root.startswith(public + os.sep), "quarantined bytes are behind nginx")
		self.assertFalse(root.startswith(private_files + os.sep))

	def test_an_occupied_target_refuses_rather_than_overwrites(self):
		doc, campaign, obj = self.migrated(file_name="a32-occupied.txt")
		target = cleanup.quarantine_path(campaign, obj.name)
		os.makedirs(os.path.dirname(target), exist_ok=True)
		with open(target, "wb") as handle:
			handle.write(b"somebody else's only remaining copy")
		self.addCleanup(lambda: os.path.exists(target) and os.remove(target))

		with approved_cleanup(campaign):
			self.assertBlocked(doc, campaign, obj, reason_contains="already exists")

		with open(target, "rb") as handle:
			self.assertEqual(handle.read(), b"somebody else's only remaining copy")

	def test_a_cross_filesystem_rename_blocks_instead_of_falling_back_to_copy(self):
		doc, campaign, obj = self.migrated(file_name="a32-exdev.txt")
		with (
			approved_cleanup(campaign),
			patch("os.rename", side_effect=OSError(18, "Invalid cross-device link")),
		):
			self.assertBlocked(doc, campaign, obj, reason_contains="rename failed")

	def test_a_quarantined_file_can_be_restored(self):
		doc, campaign, obj = self.migrated(file_name="a32-restore.txt", content=b"put me back")
		with approved_cleanup(campaign):
			self.run_cleanup(campaign)
		self.assertFalse(self.local_exists(doc))

		self.assertTrue(cleanup.restore_from_quarantine(obj.name))
		self.assertTrue(self.local_exists(doc))
		with open(doc._canonical_local_path(), "rb") as handle:
			self.assertEqual(handle.read(), b"put me back")

	def test_a_restore_refuses_to_overwrite_a_newer_file(self):
		doc, campaign, obj = self.migrated(file_name="a32-restore-guard.txt")
		with approved_cleanup(campaign):
			self.run_cleanup(campaign)

		path = doc._canonical_local_path()
		with open(path, "wb") as handle:
			handle.write(b"a newer file the site created after the quarantine")

		self.assertFalse(cleanup.restore_from_quarantine(obj.name))
		with open(path, "rb") as handle:
			self.assertEqual(handle.read(), b"a newer file the site created after the quarantine")


class TestQuarantinePurge(CleanupTestCase):
	"""A32 — the purge is keyed on `cleaned_at`, never on filesystem mtime."""

	def test_a_fresh_quarantine_is_not_purged(self):
		doc, campaign, obj = self.migrated(file_name="purge-fresh.txt")
		with approved_cleanup(campaign):
			self.run_cleanup(campaign)
		target = frappe.db.get_value(OBJECT_DOCTYPE, obj.name, "quarantine_path")

		cleanup.purge_expired_quarantine()

		self.assertTrue(os.path.exists(target), "a fresh quarantine was purged")
		self.assertEqual(frappe.db.get_value(OBJECT_DOCTYPE, obj.name, "status"), "Quarantined")

	def test_an_expired_quarantine_is_purged_and_audited(self):
		doc, campaign, obj = self.migrated(file_name="purge-expired.txt")
		with approved_cleanup(campaign):
			self.run_cleanup(campaign)
		target = frappe.db.get_value(OBJECT_DOCTYPE, obj.name, "quarantine_path")

		ttl = cint(frappe.db.get_value(CAMPAIGN_DOCTYPE, campaign, "quarantine_ttl_days"))
		frappe.db.set_value(
			OBJECT_DOCTYPE,
			obj.name,
			"cleaned_at",
			add_to_date(now_datetime(), days=-(ttl + 1)),
			update_modified=False,
		)
		frappe.db.commit()

		cleanup.purge_expired_quarantine()

		self.assertFalse(os.path.exists(target))
		self.assertEqual(frappe.db.get_value(OBJECT_DOCTYPE, obj.name, "status"), "CleanedUp")
		self.assertIn("quarantine_purge", self.audit_actions(campaign))

	def test_a_purge_re_verifies_the_remote_object_and_refuses_when_it_is_gone(self):
		"""docs/INVARIANTS.md invariant 1 — re-verified at DELETION time, not at quarantine time.

		The purge runs unattended from the scheduler up to `quarantine_ttl_days` (default 14)
		after the quarantine, and it destroys the last local copy. Fourteen days is long
		enough for a bucket to lose an object, so the gates that justified the quarantine are
		not evidence about now.
		"""
		doc, campaign, obj = self.migrated(file_name="purge-reverify.txt", content=b"still here?")
		with approved_cleanup(campaign):
			self.run_cleanup(campaign)
		target = frappe.db.get_value(OBJECT_DOCTYPE, obj.name, "quarantine_path")
		self.assertTrue(os.path.exists(target))

		# The bucket loses the object during the quarantine window.
		cso = objects.get_cso(obj.cloud_storage_object)
		self.store.objects.pop(cso.s3_key)

		self._expire(campaign, obj.name)
		cleanup.purge_expired_quarantine()

		self.assertTrue(
			os.path.exists(target),
			"the last local copy was destroyed while the remote object was missing",
		)
		self.assertEqual(frappe.db.get_value(OBJECT_DOCTYPE, obj.name, "status"), "Quarantined")
		refusals = [
			row
			for row in frappe.db.get_all(
				"Cloud Storage Audit Log",
				filters={"campaign": campaign, "action": "cleanup_refused"},
				fields=["details"],
				limit_page_length=0,
			)
			if "quarantine_purge" in (row.details or "")
		]
		self.assertTrue(refusals, "the refusal was not audited")

	def test_a_purge_refuses_when_the_remote_bytes_changed(self):
		doc, campaign, obj = self.migrated(file_name="purge-changed.txt", content=b"original")
		with approved_cleanup(campaign):
			self.run_cleanup(campaign)
		target = frappe.db.get_value(OBJECT_DOCTYPE, obj.name, "quarantine_path")

		cso = objects.get_cso(obj.cloud_storage_object)
		self.store.objects[cso.s3_key] = b"something else entirely"

		self._expire(campaign, obj.name)
		cleanup.purge_expired_quarantine()

		self.assertTrue(os.path.exists(target))
		self.assertEqual(frappe.db.get_value(OBJECT_DOCTYPE, obj.name, "status"), "Quarantined")

	def test_a_purge_proceeds_when_the_object_is_still_there(self):
		"""The counterpart: the two refusals above must not be a purge that never fires."""
		doc, campaign, obj = self.migrated(file_name="purge-healthy.txt", content=b"safely remote")
		with approved_cleanup(campaign):
			self.run_cleanup(campaign)
		target = frappe.db.get_value(OBJECT_DOCTYPE, obj.name, "quarantine_path")

		self._expire(campaign, obj.name)
		cleanup.purge_expired_quarantine()

		self.assertFalse(os.path.exists(target), "an expired, verified quarantine was not purged")
		self.assertEqual(frappe.db.get_value(OBJECT_DOCTYPE, obj.name, "status"), "CleanedUp")

	def _expire(self, campaign: str, migration_object: str):
		ttl = cint(frappe.db.get_value(CAMPAIGN_DOCTYPE, campaign, "quarantine_ttl_days"))
		frappe.db.set_value(
			OBJECT_DOCTYPE,
			migration_object,
			"cleaned_at",
			add_to_date(now_datetime(), days=-(ttl + 1)),
			update_modified=False,
		)
		frappe.db.commit()

	def test_an_old_files_mtime_does_not_make_it_expired(self):
		"""`rename` preserves mtime, so an mtime-keyed purge deletes a fresh quarantine."""
		doc, campaign, obj = self.migrated(file_name="purge-old-mtime.txt")
		path = doc._canonical_local_path()
		os.utime(path, (0, 0))  # a very old attachment, quarantined a second ago

		with approved_cleanup(campaign):
			self.run_cleanup(campaign)
		target = frappe.db.get_value(OBJECT_DOCTYPE, obj.name, "quarantine_path")
		self.assertLess(os.stat(target).st_mtime, 86400, "the fixture's mtime is not old")

		cleanup.purge_expired_quarantine()
		self.assertTrue(os.path.exists(target), "the purge is reading filesystem mtime")


class TestDirectDelete(CleanupTestCase):
	"""Direct Delete is opt-in and runs the same gates."""

	def test_direct_delete_removes_the_file_and_audits_it(self):
		doc, campaign, obj = self.migrated(file_name="direct-delete.txt", cleanup_mode="Direct Delete")
		with approved_cleanup(campaign):
			result = self.run_cleanup(campaign)

		self.assertEqual(result["cleaned"], 1)
		self.assertFalse(self.local_exists(doc))
		self.assertEqual(frappe.db.get_value(OBJECT_DOCTYPE, obj.name, "status"), "CleanedUp")
		self.assertIn("local_delete", self.audit_actions(campaign))

	def test_direct_delete_still_refuses_on_a_failed_gate(self):
		doc, campaign, obj = self.migrated(file_name="direct-delete-gate.txt", cleanup_mode="Direct Delete")
		cso = objects.get_cso(obj.cloud_storage_object)
		self.store.objects.pop(cso.s3_key)

		with approved_cleanup(campaign):
			self.assertBlocked(doc, campaign, obj, reason_contains="pre-delete verification failed")


class TestApprovalIsSeparateAndAudited(CleanupTestCase):
	"""A9 — approve_cleanup and start_cleanup are two actions, both audited."""

	def test_approval_alone_deletes_nothing(self):
		doc, campaign, obj = self.migrated(file_name="approve-only.txt")
		frappe.db.delete("Cloud Migration Conflict", {"campaign": campaign})
		frappe.db.commit()

		api.approve_cleanup(campaign, note="reviewed")

		row = frappe.db.get_value(
			CAMPAIGN_DOCTYPE, campaign, ["cleanup_approved_by", "cleanup_approved_at"], as_dict=True
		)
		self.assertEqual(row.cleanup_approved_by, "Administrator")
		self.assertIsNotNone(row.cleanup_approved_at)
		self.assertTrue(self.local_exists(doc), "approving is not deleting")

	def test_approval_is_refused_while_blockers_are_open(self):
		doc, campaign, obj = self.migrated(file_name="approve-blocked.txt")
		conflict = frappe.new_doc("Cloud Migration Conflict")
		conflict.update(
			{
				"campaign": campaign,
				"migration_object": obj.name,
				"conflict_type": "checksum_mismatch",
				"status": "Open",
				"severity": "Blocker",
			}
		)
		conflict.insert(ignore_permissions=True)
		frappe.db.commit()

		with self.assertRaises(frappe.ValidationError):
			api.approve_cleanup(campaign)
		self.assertIsNone(frappe.db.get_value(CAMPAIGN_DOCTYPE, campaign, "cleanup_approved_by"))

	def test_start_cleanup_is_refused_without_approval(self):
		# The confirm phrase is supplied so the refusal can only come from the missing
		# approval — otherwise this would pass on P7's type-to-confirm gate instead and
		# stop testing the thing its name claims.
		doc, campaign, obj = self.migrated(file_name="start-unapproved.txt")
		with self.assertRaises(frappe.ValidationError):
			api.start_cleanup(campaign, confirm_phrase=api.CLEANUP_CONFIRM_PHRASE)

	def test_start_cleanup_is_refused_in_a_local_copy_mode(self):
		doc, campaign, obj = self.migrated(file_name="start-mode.txt", mode="S3_PRIMARY_LOCAL_FALLBACK")
		set_mode("DUAL_WRITE")
		with approved_cleanup(campaign), self.assertRaises(frappe.ValidationError):
			api.start_cleanup(campaign, confirm_phrase=api.CLEANUP_CONFIRM_PHRASE)
		self.assertTrue(self.local_exists(doc))


class TestAuditLogIsAppendOnly(MigrationTestCase):
	def test_an_audit_row_cannot_be_edited(self):
		campaign = self.campaign()
		name = audit.record("local_quarantine", campaign=campaign, note="original")
		doc = frappe.get_doc(audit.AUDIT_DOCTYPE, name)
		doc.details = '{"note": "rewritten"}'
		with self.assertRaises(frappe.ValidationError):
			doc.save(ignore_permissions=True)

	def test_no_role_may_write_or_delete_audit_rows(self):
		meta = frappe.get_meta(audit.AUDIT_DOCTYPE)
		for perm in meta.permissions:
			self.assertFalse(perm.write, f"{perm.role} may write audit rows")
			self.assertFalse(perm.delete, f"{perm.role} may delete audit rows")
