"""The happy path end to end, plus the properties that make it safe.

This is the suite that proves the pieces fit: analyze → plan → upload → verify → link, with
the local file untouched throughout and the File row pointing at a verified object at the
end.
"""

import os

import frappe

from cloud_file_storage.migration import analyzer, engine, report, verify
from cloud_file_storage.storage import objects
from cloud_file_storage.tests.migration_utils import MigrationTestCase
from cloud_file_storage.tests.utils import set_mode


class TestMigrationHappyPath(MigrationTestCase):
	def test_a_private_file_is_uploaded_verified_and_linked(self):
		doc = self.local_file(file_name="pipeline-private.txt", content=b"pipeline bytes")
		campaign = self.campaign()

		self.migrate(campaign)

		obj = self.object_for(campaign, doc.file_url)
		self.assertEqual(obj.status, "Verified")
		self.assertEqual(obj.classification, "healthy_unique")
		self.assertEqual(obj.ref_count, 1)

		cso_name = self.link_of(doc)
		self.assertEqual(cso_name, obj.cloud_storage_object)
		self.assertEqual(frappe.db.get_value(objects.CSO_DOCTYPE, cso_name, "status"), "verified")

		# Nothing local moved: UPLOAD and VERIFY have no deletion path at all.
		self.assertTrue(self.local_exists(doc))

	def test_the_uploaded_bytes_are_the_files_bytes(self):
		content = b"the exact bytes that must arrive"
		doc = self.local_file(file_name="pipeline-bytes.txt", content=content)
		campaign = self.campaign()

		self.migrate(campaign)

		cso = objects.get_cso(self.link_of(doc))
		self.assertEqual(self.store.objects[cso.s3_key], content)

	def test_two_file_rows_sharing_one_url_are_one_object_with_two_refs(self):
		doc = self.local_file(file_name="pipeline-shared.txt", content=b"shared url bytes")
		sibling = self.url_sibling(doc, file_name="pipeline-shared-copy.txt")
		campaign = self.campaign()

		self.migrate(campaign)

		obj = self.object_for(campaign, doc.file_url)
		self.assertEqual(obj.ref_count, 2, "the shared URL must be ONE physical object")
		self.assertEqual(obj.classification, "shared_url")
		self.assertEqual(self.link_of(doc), obj.cloud_storage_object)
		self.assertEqual(self.link_of(sibling), obj.cloud_storage_object)

	def test_identical_bytes_behind_two_urls_reuse_one_cloud_object(self):
		first = self.local_file(file_name="pipeline-dup-a.txt", content=b"identical payload")
		second = self.standalone_copy(first, file_name="pipeline-dup-b.txt")
		campaign = self.campaign()

		self.migrate(campaign)

		first_obj = self.object_for(campaign, first.file_url)
		second_obj = self.object_for(campaign, second.file_url)
		self.assertNotEqual(first_obj.name, second_obj.name, "two URLs are two migration objects")
		self.assertEqual(
			first_obj.cloud_storage_object,
			second_obj.cloud_storage_object,
			"identical bytes must converge on one Cloud Storage Object",
		)
		cso = first_obj.cloud_storage_object
		self.assertEqual(frappe.db.count("File", {"cloud_storage_object": cso}), 2)
		# A14 — both migration objects of this still-running campaign count as live
		# references, so GC cannot delete the bytes out from under the campaign.
		self.assertEqual(objects.migration_reference_count(cso, for_update=False), 2)
		self.assertEqual(objects.live_reference_count(cso), 4)

	def test_convergence_reaches_one_for_a_clean_corpus(self):
		docs = [
			self.local_file(file_name=f"pipeline-conv-{index}.txt", content=f"payload {index}".encode())
			for index in range(5)
		]
		campaign = self.campaign()

		self.migrate(campaign)

		convergence = self.fixture_convergence(campaign, docs)
		self.assertEqual(convergence["in_scope"], 5)
		self.assertEqual(convergence["verified"], 5)

	def test_the_job_count_is_bounded_by_the_batch_count(self):
		"""F3 — roughly two jobs per batch, never one per file."""
		for index in range(6):
			self.local_file(file_name=f"pipeline-jobs-{index}.txt", content=f"job payload {index}".encode())
		campaign = self.campaign(batch_size=2)

		self.migrate(campaign)

		batches = frappe.db.count("Cloud Migration Batch", {"campaign": campaign})
		objects_planned = frappe.db.count(
			"Cloud Migration Object", {"campaign": campaign, "batch": ("is", "set")}
		)
		self.assertGreaterEqual(batches, 3)
		self.assertGreater(objects_planned, batches, "the fixture has to have more objects than batches")

		jobs = len(self.queue.calls)
		# The F3 claim is structural: job count tracks BATCHES, not objects. Two per batch
		# (one upload, one verify) plus headroom for a retry — and, more importantly, the
		# only methods ever enqueued are the three batch entry points, so there is no shape
		# of input that could produce a job per file.
		self.assertLessEqual(jobs, 2 * batches + 2, f"{jobs} jobs for {batches} batches")
		self.assertEqual(
			{str(call["method"]).rsplit(".", 1)[-1] for call in self.queue.calls},
			{"run_upload_batch", "run_verify_batch"},
		)
		self.assertTrue(all(call["queue"] == engine.MIGRATION_QUEUE for call in self.queue.calls))

	def test_every_batch_job_carries_a_deterministic_job_id(self):
		self.local_file(file_name="pipeline-jobid.txt")
		campaign = self.campaign()
		self.migrate(campaign)

		self.assertTrue(self.queue.job_ids, "no jobs were enqueued at all")
		for job_id in self.queue.job_ids:
			self.assertTrue(job_id.startswith(f"cfs::{campaign}::"), job_id)

		# Ids repeat across re-dispatch, and that is the point: RQ's `deduplicate` keys on
		# the id, so a batch that is already queued or running is not queued a second time.
		batches = frappe.db.count("Cloud Migration Batch", {"campaign": campaign})
		self.assertLessEqual(len(set(self.queue.job_ids)), 3 * batches)


class TestAnalyzerClassification(MigrationTestCase):
	def test_a_file_whose_bytes_are_gone_is_a_blocker_not_an_upload(self):
		doc = self.local_file(file_name="analyze-missing.txt", content=b"about to vanish")
		os.remove(doc._canonical_local_path())
		campaign = self.campaign()

		self.analyze(campaign)

		obj = self.object_for(campaign, doc.file_url)
		self.assertEqual(obj.classification, "missing_physical")
		self.assertEqual(obj.status, "Conflict")
		conflicts = self.conflicts_of(campaign, conflict_type="missing_physical")
		self.assertTrue(any(row.migration_object == obj.name for row in conflicts))
		self.assertEqual([row.severity for row in conflicts if row.migration_object == obj.name], ["Blocker"])

	def test_one_url_with_two_byte_expectations_halts_rather_than_guessing(self):
		doc = self.local_file(file_name="analyze-conflict.txt", content=b"first expectation")
		sibling = self.url_sibling(doc, file_name="analyze-conflict-2.txt")
		frappe.db.set_value("File", sibling.name, "content_hash", "f" * 32, update_modified=False)
		frappe.db.set_value("File", doc.name, "content_hash", "a" * 32, update_modified=False)
		frappe.db.commit()
		campaign = self.campaign()

		self.analyze(campaign)

		obj = self.object_for(campaign, doc.file_url)
		self.assertEqual(obj.classification, "conflicting_url")
		self.assertEqual(obj.status, "Conflict")
		self.assertIsNone(obj.db_content_hash, "a disputed hash must not be recorded as consensus")

	def test_a_privacy_mismatch_is_a_blocker_and_is_never_uploaded(self):
		"""A19 — the operator decides the true visibility before any byte moves."""
		doc = self.local_file(file_name="analyze-privacy.txt", content=b"whose bytes are these")
		# The column now disagrees with the URL prefix, which is the A19 shape.
		frappe.db.set_value("File", doc.name, "is_private", 0, update_modified=False)
		frappe.db.commit()
		campaign = self.campaign()

		self.analyze(campaign)
		self.plan(campaign)
		set_mode("S3_PRIMARY_LOCAL_FALLBACK")
		from cloud_file_storage.migration import api

		api.start_migration(campaign, skip_preflight=True)
		self.drain(campaign)

		obj = self.object_for(campaign, doc.file_url)
		self.assertEqual(obj.privacy_mismatch, 1)
		self.assertEqual(obj.status, "Conflict")
		self.assertIsNone(obj.cloud_storage_object, "an ambiguous-privacy object must not be uploaded")
		self.assertTrue(self.conflicts_of(campaign, conflict_type="ambiguous_privacy"))

	def test_an_attachment_of_an_ignored_doctype_is_skipped(self):
		"""A17 — LOCAL_OPERATIONAL bytes are never claimed as migrated cloud attachments."""
		doc = self.local_file(
			file_name="analyze-ignored.txt",
			content=b"data import payload",
			attached_to_doctype="Data Import",
			attached_to_name="DI-TEST-0001",
		)
		campaign = self.campaign()

		self.analyze(campaign)

		obj = self.object_for(campaign, doc.file_url)
		self.assertEqual(obj.status, "Skipped")
		self.assertEqual(obj.skip_reason, "ignored_doctype")
		self.assertEqual(obj.ignored_ref_count, 1)

	def test_a_mixed_object_is_not_skipped_but_stays_marked(self):
		"""A17's harder half: one ignored ref among real ones blocks CLEANUP, not upload."""
		doc = self.local_file(file_name="analyze-mixed.txt", content=b"shared with an import")
		sibling = self.url_sibling(doc, file_name="analyze-mixed-2.txt")
		frappe.db.set_value(
			"File",
			sibling.name,
			{"attached_to_doctype": "Data Import", "attached_to_name": "DI-TEST-0002"},
			update_modified=False,
		)
		frappe.db.commit()
		campaign = self.campaign()

		self.analyze(campaign)

		obj = self.object_for(campaign, doc.file_url)
		self.assertEqual(obj.status, "Pending", "a real attachment shares this URL; it must migrate")
		self.assertEqual(obj.ref_count, 2)
		self.assertEqual(obj.ignored_ref_count, 1)

	def test_a_file_on_disk_that_no_row_claims_is_reported_not_uploaded(self):
		orphan = frappe.get_site_path("private", "files", "cfs-orphan-probe.bin")
		with open(orphan, "wb") as handle:
			handle.write(b"nobody references me")
		self.addCleanup(lambda: os.path.exists(orphan) and os.remove(orphan))
		campaign = self.campaign()

		self.analyze(campaign)

		obj = self.object_for(campaign, "/private/files/cfs-orphan-probe.bin")
		self.assertIsNotNone(obj, "the orphan was not recorded at all")
		self.assertEqual(obj.classification, "orphan_physical")
		self.assertEqual(obj.status, "Skipped")

	def test_re_running_the_analysis_changes_nothing(self):
		"""A33 — a replayed page collides on the primary key instead of duplicating."""
		doc = self.local_file(file_name="analyze-idempotent.txt", content=b"run me twice")
		campaign = self.campaign()

		self.analyze(campaign)
		first = (
			frappe.db.count("Cloud Migration Object", {"campaign": campaign}),
			frappe.db.count("Cloud Migration File Ref", {"campaign": campaign}),
		)
		obj_before = self.object_for(campaign, doc.file_url)

		frappe.db.set_value(
			"Cloud Migration Campaign", campaign, {"scan_cursor_file": "", "classify_step": ""}
		)
		frappe.db.commit()
		self.analyze(campaign)

		second = (
			frappe.db.count("Cloud Migration Object", {"campaign": campaign}),
			frappe.db.count("Cloud Migration File Ref", {"campaign": campaign}),
		)
		self.assertEqual(first, second)
		self.assertEqual(self.object_for(campaign, doc.file_url).ref_count, obj_before.ref_count)


class TestAnalyzerIdentity(MigrationTestCase):
	def test_an_absolute_own_site_url_normalises_onto_the_relative_one(self):
		relative = "/private/files/normalise-me.txt"
		absolute = f"https://{frappe.local.site}{relative}"
		self.assertEqual(analyzer.normalize_url(absolute), relative)
		self.assertEqual(
			analyzer.classify_identity(frappe._dict({"name": "F1", "file_url": absolute})),
			analyzer.classify_identity(frappe._dict({"name": "F2", "file_url": relative})),
		)

	def test_a_url_pointing_at_someone_elses_host_stays_remote(self):
		url = "https://example.invalid/files/theirs.txt"
		kind, _identity = analyzer.classify_identity(frappe._dict({"name": "F3", "file_url": url}))
		self.assertEqual(kind, "remote_https")

	def test_a_canonical_url_wins_over_a_stale_locator_column(self):
		"""The deviation from design §3.2, asserted rather than only argued in a docstring."""
		row = frappe._dict(
			{"name": "F4", "file_url": "/files/healthy.txt", "s3_object_key": "old/fork/key.txt"}
		)
		kind, identity = analyzer.classify_identity(row)
		self.assertEqual(kind, "local_public")
		self.assertEqual(identity, "local_public::/files/healthy.txt")

	def test_a_fork_endpoint_url_is_identified_by_its_key(self):
		row = frappe._dict(
			{
				"name": "F5",
				"file_url": "/api/method/frappe_s3_attachment.controller.generate_file?key=a/b/c.pdf",
				"s3_object_key": "a/b/c.pdf",
			}
		)
		kind, identity = analyzer.classify_identity(row)
		self.assertEqual(kind, "legacy_fork_key")
		self.assertEqual(identity, "s3legacy::a/b/c.pdf")

	def test_a_path_that_escapes_the_file_tree_resolves_to_nothing(self):
		self.assertIsNone(analyzer.local_disk_path("/private/files/../../../etc/passwd"))
		self.assertIsNotNone(analyzer.local_disk_path("/private/files/ordinary.txt"))


class TestVerifyLinkGuard(MigrationTestCase):
	"""A26 — the link never clobbers a newer runtime decision."""

	def test_a_row_the_runtime_relinked_is_not_stolen(self):
		doc = self.local_file(file_name="guard-relinked.txt", content=b"guard bytes")
		campaign = self.campaign()
		self.analyze(campaign)
		self.plan(campaign)
		set_mode("S3_PRIMARY_LOCAL_FALLBACK")

		# A different object gets there first — what DUAL_WRITE does while we are working.
		# Set BEFORE the campaign starts: `start_migration` dispatches immediately, and with
		# the inline queue that means the whole pipeline would already have run.
		other = objects.ensure_cso(
			content_sha256="b" * 64,
			content_hash_md5="c" * 32,
			file_size=1,
			visibility="private",
		)
		frappe.db.set_value("File", doc.name, "cloud_storage_object", other.name, update_modified=False)
		frappe.db.commit()

		from cloud_file_storage.migration import api

		api.start_migration(campaign, skip_preflight=True)
		self.drain(campaign)

		self.assertEqual(self.link_of(doc), other.name, "the migration overwrote a newer runtime link")
		obj = self.object_for(campaign, doc.file_url)
		self.assertEqual(obj.skip_reason, "superseded_by_runtime")
		ref = frappe.db.get_value(
			"Cloud Migration File Ref", {"migration_object": obj.name, "file": doc.name}, "*", as_dict=True
		)
		self.assertEqual(ref.linked, 0)
		self.assertEqual(ref.skip_reason, "superseded_by_runtime")
		self.assertIn("link_skipped", self.audit_actions(campaign))

	def test_a_row_whose_url_moved_is_not_linked(self):
		doc = self.local_file(file_name="guard-moved.txt", content=b"moved bytes")
		campaign = self.campaign()
		self.analyze(campaign)
		self.plan(campaign)
		set_mode("S3_PRIMARY_LOCAL_FALLBACK")

		frappe.db.set_value(
			"File", doc.name, "file_url", "/private/files/guard-moved-elsewhere.txt", update_modified=False
		)
		frappe.db.commit()

		from cloud_file_storage.migration import api

		api.start_migration(campaign, skip_preflight=True)
		self.drain(campaign)
		self.assertIsNone(self.link_of(doc), "a row whose URL moved under us must not be linked")

	def test_a_row_whose_bytes_were_replaced_is_not_linked(self):
		doc = self.local_file(file_name="guard-rehashed.txt", content=b"original content")
		campaign = self.campaign()
		self.analyze(campaign)
		self.plan(campaign)
		set_mode("S3_PRIMARY_LOCAL_FALLBACK")

		frappe.db.set_value("File", doc.name, "content_hash", "9" * 32, update_modified=False)
		frappe.db.commit()

		from cloud_file_storage.migration import api

		api.start_migration(campaign, skip_preflight=True)
		self.drain(campaign)
		self.assertIsNone(self.link_of(doc), "a row whose content_hash changed must not be linked")

	def test_an_already_correctly_linked_ref_is_not_mistaken_for_superseded(self):
		"""The ROW_COUNT trap: MariaDB reports *changed* rows, and a correct row changes none."""
		doc = self.local_file(file_name="guard-idempotent.txt", content=b"link me twice")
		campaign = self.campaign()
		self.migrate(campaign)

		obj = frappe.db.get_value(
			"Cloud Migration Object",
			engine.object_name_for(campaign, f"local_private::{doc.file_url}"),
			"*",
			as_dict=True,
		)
		cso = objects.get_cso(obj.cloud_storage_object)

		result = verify.link_refs(obj, cso, campaign=campaign)
		self.assertEqual(result["row_count"], 0, "nothing changed, so MariaDB reports zero")
		self.assertEqual(result["linked"], 1, "…but the ref IS linked, and must be read back as such")
		self.assertEqual(result["superseded"], 0)

	def test_the_content_hash_of_a_hashless_row_is_backfilled(self):
		"""Contract #15 — restores core dedup on rows the 0.2.x fork nulled."""
		doc = self.local_file(file_name="guard-backfill.txt", content=b"hash me")
		frappe.db.set_value("File", doc.name, "content_hash", None, update_modified=False)
		frappe.db.commit()
		campaign = self.campaign()

		self.migrate(campaign)

		cso = objects.get_cso(self.link_of(doc))
		self.assertEqual(frappe.db.get_value("File", doc.name, "content_hash"), cso.content_hash_md5)


class TestCounters(MigrationTestCase):
	def test_counters_are_recomputed_rather_than_accumulated(self):
		for index in range(3):
			self.local_file(file_name=f"counter-{index}.txt", content=f"counter {index}".encode())
		campaign = self.campaign()
		self.migrate(campaign)

		frappe.db.set_value(
			"Cloud Migration Campaign", campaign, {"objects_verified": 999}, update_modified=False
		)
		frappe.db.commit()

		analyzer.refresh_counters(campaign)
		snapshot = report.campaign_snapshot(campaign)
		self.assertNotEqual(snapshot["objects_verified"], 999, "drift did not self-heal")
		self.assertGreaterEqual(snapshot["objects_verified"], 3)
