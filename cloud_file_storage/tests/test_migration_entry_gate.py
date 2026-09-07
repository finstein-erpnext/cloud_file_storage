"""P5 entry gate (PLAN.md §B) — is the frozen A7 CSO interface enough to migrate with?

PLAN.md §B makes this the *entry* criterion for P5, not an exit one: before the migration
engine is built, the P2↔P5 contract has to be shown sufficient, and a CSO created the way
migration will create one has to be shown servable through the P3 paths. Two things follow
from that framing:

* every test here calls the A7 surface **only** — `objects.ensure_cso`,
  `objects.adopt_references`, `objects.set_cso_status`, `engine.verify` — because a gate
  that reaches around the interface proves nothing about the interface;
* where the interface turns out **not** to be sufficient, the gap is asserted rather than
  papered over. :class:`TestAdoptionCannotBeExpressedByEnsureCso` is that assertion, and
  `objects.adopt_legacy_cso` is the recorded, in-choke-point resolution (A15/A18, and
  DECISIONS.md 2026-08-15).
"""

import inspect
import os

import frappe

from cloud_file_storage.cloud_file_storage.doctype.cloud_storage_object.cloud_storage_object import (
	PRESENT_STATUSES,
	SERVABLE_STATUSES,
	STATUSES,
)
from cloud_file_storage.storage import engine, objects
from cloud_file_storage.storage.hashing import digest_path
from cloud_file_storage.tests.request_utils import access_log_count, http_request, make_user
from cloud_file_storage.tests.utils import CloudStorageTestCase, set_mode

#: A7 verbatim: "Statuses (lowercase): pending_upload uploaded verified failed orphaned
#: pending_delete deleted legacy_unverified. Columns: content_sha256 / content_hash_md5 /
#: file_size / s3_key. Public API: ensure_cso(), adopt_references(), set_cso_status(),
#: engine.verify()."
A7_STATUSES = (
	"pending_upload",
	"uploaded",
	"verified",
	"failed",
	"orphaned",
	"pending_delete",
	"deleted",
	"legacy_unverified",
)
A7_COLUMNS = ("content_sha256", "content_hash_md5", "file_size", "s3_key")

#: `FakeObjectStore` replaces `engine.verify` with a plain HEAD echo, which would make an
#: "engine.verify bites" assertion a test of the fake. Captured at import time — before any
#: patching — so the integrity test below drives the real implementation against the fake
#: bucket's HEAD.
REAL_VERIFY = engine.verify


class TestA7InterfaceConformance(CloudStorageTestCase):
	"""The frozen surface exists, in the frozen shape, with the frozen vocabulary."""

	MODE = "LOCAL_ONLY"

	def test_the_status_set_is_exactly_the_eight_frozen_lowercase_names(self):
		self.assertEqual(tuple(STATUSES), A7_STATUSES)
		for status in STATUSES:
			self.assertEqual(status, status.lower(), f"{status!r} is not lowercase")

	def test_the_doctype_select_offers_exactly_those_statuses(self):
		"""The schema and the module constant cannot drift apart unnoticed."""
		options = frappe.get_meta("Cloud Storage Object").get_field("status").options
		self.assertEqual(tuple(options.split("\n")), A7_STATUSES)

	def test_the_four_frozen_columns_exist_with_migration_usable_types(self):
		meta = frappe.get_meta("Cloud Storage Object")
		for column in A7_COLUMNS:
			self.assertIsNotNone(meta.get_field(column), f"A7 column {column} is missing")

		# A24: byte counters are Long Int everywhere. A 100GB corpus has objects an INT(11)
		# cannot hold, and migration writes this column for every object it uploads.
		self.assertEqual(meta.get_field("file_size").fieldtype, "Long Int")

	def test_the_four_public_functions_exist_and_take_what_migration_passes(self):
		for name in ("ensure_cso", "adopt_references", "set_cso_status"):
			self.assertTrue(callable(getattr(objects, name, None)), f"objects.{name} is missing")
		self.assertTrue(callable(getattr(engine, "verify", None)))

		ensure = inspect.signature(objects.ensure_cso).parameters
		# UPLOAD passes every one of these; a missing one is a contract break, not a detail.
		for parameter in (
			"content_sha256",
			"content_hash_md5",
			"file_size",
			"visibility",
			"mime_type",
			"source_path",
			"migration_batch",
		):
			self.assertIn(parameter, ensure, f"ensure_cso lost the {parameter} parameter")

	def test_present_and_servable_sets_are_subsets_of_the_frozen_statuses(self):
		self.assertTrue(set(PRESENT_STATUSES) <= set(A7_STATUSES))
		self.assertTrue(set(SERVABLE_STATUSES) <= set(A7_STATUSES))
		# Adoption's whole point (A15): an adopted row is servable without being verified.
		self.assertIn("legacy_unverified", SERVABLE_STATUSES)
		self.assertNotIn("legacy_unverified", ("verified",))


class MigrationShapedUpload(CloudStorageTestCase):
	"""Base case that produces a CSO through exactly the calls UPLOAD will make."""

	MODE = "LOCAL_ONLY"

	def local_file(self, *, file_name, content, is_private=1):
		"""A File row with bytes on local disk and no cloud object — the pre-migration state."""
		doc = self.make_file(file_name=file_name, content=content, is_private=is_private)
		self.assertIsNone(
			frappe.db.get_value("File", doc.name, "cloud_storage_object"),
			"LOCAL_ONLY must not have created a cloud object; this fixture would prove nothing",
		)
		return doc

	def migrate(self, doc, *, batch="CFS-CAMP-0001-B1"):
		"""ensure_cso → put_path → uploaded → engine.verify → verified. A7 calls only."""
		path = doc.get_full_path()
		digest = digest_path(path)
		is_private = frappe.db.get_value("File", doc.name, "is_private")

		cso = objects.ensure_cso(
			content_sha256=digest.sha256,
			content_hash_md5=digest.md5,
			file_size=digest.size,
			visibility="private" if is_private else "public",
			mime_type="text/plain",
			source_path=path,
			migration_batch=batch,
		)
		self.assertTrue(objects.needs_upload(cso), "a fresh migration object must want uploading")

		engine.put_path(cso, path)
		objects.set_cso_status(cso.name, "uploaded")

		cso.reload()
		engine.verify(cso)
		objects.set_cso_status(cso.name, "verified")
		cso.reload()
		return cso


class TestMigrationCreatedCsoIsServable(MigrationShapedUpload):
	"""PLAN §B's second entry criterion, on both serving paths and both read APIs."""

	def setUp(self):
		super().setUp()
		self.owner = make_user(self, "cfs-p5-entry-owner@example.com")

	def test_a_private_migrated_object_serves_through_the_p3_private_path(self):
		from cloud_file_storage.serving import private

		doc = self.local_file(file_name="p5-entry-private.txt", content=b"entry gate bytes")
		cso = self.migrate(doc)

		linked = objects.adopt_references(cso.name, [doc.name])
		self.assertEqual(linked, 1)

		set_mode("S3_ONLY")
		# The private path serves a surviving local copy directly (A24), so the cloud claim
		# has to be made in the state CLEANUP leaves behind: object present, local copy gone.
		os.remove(doc._canonical_local_path())

		before = access_log_count(doc.name)
		with http_request(doc.file_url, user="Administrator"):
			response = private.download_private_file_cloud(doc.file_url)

		self.assertEqual(response.status_code, 302)
		self.assertIn(cso.s3_key, response.headers["Location"])
		self.assertEqual(access_log_count(doc.name) - before, 1)

	def test_a_public_migrated_object_serves_through_the_p3_renderer(self):
		from cloud_file_storage.serving.public import PublicFileRenderer

		doc = self.local_file(file_name="p5-entry-public.txt", content=b"public entry", is_private=0)
		cso = self.migrate(doc)
		objects.adopt_references(cso.name, [doc.name])

		set_mode("S3_ONLY")
		os.remove(doc._canonical_local_path())

		with http_request(doc.file_url):
			renderer = PublicFileRenderer(doc.file_url.lstrip("/"))
			self.assertTrue(renderer.can_render(), "the renderer declined a migrated public object")
			response = renderer.render()

		self.assertEqual(response.status_code, 302)
		self.assertIn(cso.s3_key, response.headers["Location"])

	def test_get_content_reads_the_migrated_object_after_the_local_copy_is_gone(self):
		"""The read API contract (#3/#5) has to survive migration, not just serving."""
		content = b"content read after migration"
		doc = self.local_file(file_name="p5-entry-read.txt", content=content)
		cso = self.migrate(doc)
		objects.adopt_references(cso.name, [doc.name])

		set_mode("S3_ONLY")
		path = doc._canonical_local_path()
		self.assertTrue(os.path.exists(path))
		os.remove(path)  # test hygiene: CLEANUP is not what this gate is about

		fresh = frappe.get_doc("File", doc.name)
		# No-arg is exact v15 behaviour (decoded); `encodings=[]` is the India Compliance
		# raw-bytes superset (contract #3).
		self.assertEqual(fresh.get_content(), content.decode())
		self.assertEqual(fresh.get_content(encodings=[]), content)

	def test_the_migration_object_is_reachable_without_a_link_through_the_a1_chain(self):
		"""A1: core creates File rows from `file_url` alone, before any link exists."""
		doc = self.local_file(file_name="p5-entry-chain.txt", content=b"chain bytes")
		cso = self.migrate(doc)
		objects.adopt_references(cso.name, [doc.name])

		sibling = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "p5-entry-chain.txt",
				"file_url": doc.file_url,
				"is_private": 1,
			}
		).insert(ignore_permissions=True)
		self.track(sibling)

		set_mode("S3_ONLY")
		self.assertIsNone(frappe.db.get_value("File", sibling.name, "cloud_storage_object"))
		resolved = frappe.get_doc("File", sibling.name)._resolve_cso()
		self.assertIsNotNone(resolved, "the URL-sharing row could not reach the migrated object")
		self.assertEqual(resolved.name, cso.name)


class TestA7CoversTheMigrationStateMachine(MigrationShapedUpload):
	"""Every CSO transition the engine needs is expressible through `set_cso_status`."""

	def test_upload_failure_and_retry_round_trip(self):
		doc = self.local_file(file_name="p5-entry-retry.txt", content=b"retry bytes")
		path = doc.get_full_path()
		digest = digest_path(path)
		cso = objects.ensure_cso(
			content_sha256=digest.sha256,
			content_hash_md5=digest.md5,
			file_size=digest.size,
			visibility="private",
			source_path=path,
			migration_batch="CFS-CAMP-0001-B1",
		)

		objects.record_failure(cso.name, "injected transport failure")
		self.assertEqual(frappe.db.get_value(objects.CSO_DOCTYPE, cso.name, "status"), "failed")

		objects.set_cso_status(cso.name, "pending_upload")
		cso.reload()
		self.assertTrue(objects.needs_upload(cso))

		engine.put_path(cso, path)
		objects.set_cso_status(cso.name, "uploaded")
		objects.set_cso_status(cso.name, "verified")
		self.assertEqual(frappe.db.get_value(objects.CSO_DOCTYPE, cso.name, "status"), "verified")

	def test_a_dedup_hit_is_findable_through_the_frozen_identity_index(self):
		"""UPLOAD's dedup check is `find_by_identity`, not a hand-rolled SELECT."""
		first = self.local_file(file_name="p5-entry-dedup-a.txt", content=b"identical bytes")
		second = self.local_file(file_name="p5-entry-dedup-b.txt", content=b"identical bytes")

		cso = self.migrate(first)
		digest = digest_path(second.get_full_path())
		self.assertEqual(digest.sha256, cso.content_sha256)

		hit = objects.find_by_identity(digest.sha256, "private", cso.bucket)
		self.assertEqual(hit, cso.name)

		linked = objects.adopt_references(cso.name, [first.name, second.name])
		self.assertEqual(linked, 2)
		self.assertEqual(objects.live_reference_count(cso.name), 2)

	def test_verify_refuses_bytes_that_do_not_match_what_was_recorded(self):
		"""`engine.verify` is the independent check VERIFY depends on — it must bite."""
		from cloud_file_storage.storage.exceptions import CloudStorageIntegrityError

		doc = self.local_file(file_name="p5-entry-tamper.txt", content=b"original bytes")
		cso = self.migrate(doc)

		self.store.objects[cso.s3_key] = b"tampered bytes of a different length"
		with self.assertRaises(CloudStorageIntegrityError):
			REAL_VERIFY(cso)

		# …and passes on untampered bytes, so the assertion above is about the tampering
		# rather than about `REAL_VERIFY` raising on everything.
		self.store.objects[cso.s3_key] = b"original bytes"
		self.assertEqual(REAL_VERIFY(cso)["ContentLength"], len(b"original bytes"))


class TestAdoptionCannotBeExpressedByEnsureCso(CloudStorageTestCase):
	"""The entry gate's one negative finding, asserted rather than described.

	A15/A18 adoption must produce a CSO that points at an object **already in the bucket at
	a key this app did not choose** (`File.s3_object_key`, written by the 0.2.x fork), whose
	SHA256 is unknown until someone re-reads 100GB. `ensure_cso` can express neither: its
	key is derived from the content hash, and a content hash is exactly what an adopted row
	does not have.

	The resolution is recorded in DECISIONS.md and lives in the same choke-point module
	(`storage/objects.adopt_legacy_cso`), so docs/INVARIANTS.md invariant 4 — migration never mutates
	a CSO outside `storage.objects` — still holds.
	"""

	MODE = "LOCAL_ONLY"

	def test_ensure_cso_cannot_take_a_legacy_key(self):
		self.assertNotIn("s3_key", inspect.signature(objects.ensure_cso).parameters)

	def test_ensure_cso_rejects_the_missing_hash_an_adopted_row_starts_with(self):
		with self.assertRaises(ValueError):
			objects.ensure_cso(
				content_sha256=None,
				content_hash_md5=None,
				file_size=17,
				visibility="private",
			)

	def test_adopt_legacy_cso_expresses_what_ensure_cso_cannot(self):
		legacy_key = "attachments/2021/07/14/Sales Invoice/abcd_report.pdf"
		cso = objects.adopt_legacy_cso(
			s3_key=legacy_key,
			file_size=4096,
			visibility="private",
			content_hash_md5=None,
			mime_type="application/pdf",
			migration_batch="CFS-CAMP-0001-B1",
		)
		self.addCleanup(frappe.db.delete, objects.CSO_DOCTYPE, {"name": cso.name})

		self.assertEqual(cso.s3_key, legacy_key)
		self.assertEqual(cso.status, "legacy_unverified")
		self.assertIsNone(cso.content_sha256)
		# A15: an adopted row is never `verified` without a hash check, but it IS servable.
		self.assertIn(cso.status, SERVABLE_STATUSES)
		self.assertFalse(objects.needs_upload(cso), "adoption must never trigger a re-upload")

	def test_adopting_the_same_legacy_key_twice_returns_one_row(self):
		legacy_key = "attachments/2021/07/14/Sales Invoice/idem_report.pdf"
		first = objects.adopt_legacy_cso(s3_key=legacy_key, file_size=10, visibility="private")
		second = objects.adopt_legacy_cso(s3_key=legacy_key, file_size=10, visibility="private")
		self.addCleanup(frappe.db.delete, objects.CSO_DOCTYPE, {"name": first.name})

		self.assertEqual(first.name, second.name)

	def test_a_promoted_adoption_carries_a_real_hash(self):
		"""A15: promotion to `verified` happens only with hashes, through `set_cso_status`."""
		cso = objects.adopt_legacy_cso(
			s3_key="attachments/2021/07/14/Sales Invoice/promote.pdf",
			file_size=11,
			visibility="private",
		)
		self.addCleanup(frappe.db.delete, objects.CSO_DOCTYPE, {"name": cso.name})

		digest = frappe._dict(sha256="a" * 64, md5="b" * 32)
		objects.set_cso_status(
			cso.name,
			"verified",
			content_sha256=digest.sha256,
			content_hash_md5=digest.md5,
		)
		row = frappe.db.get_value(objects.CSO_DOCTYPE, cso.name, ["status", "content_sha256"], as_dict=True)
		self.assertEqual(row.status, "verified")
		self.assertEqual(row.content_sha256, digest.sha256)
