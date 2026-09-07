"""T-ADOPT — legacy fork rows and remote bucket URLs adopted in place (A15, A18).

The two properties that make adoption safe rather than merely convenient:

* **no byte moves.** The object stays at the key the 0.2.x fork chose; what is created is
  the Cloud Storage Object that lets serving, GC and reference counting see it at all.
* **no honest-looking lie.** An adopted row is `legacy_unverified` and cannot be anything
  else, because nobody has hashed those bytes. It is servable — it was already the serving
  truth — and it becomes `verified` only after a streamed re-GET that actually hashes it.
"""

import inspect
from unittest.mock import patch

import frappe

from cloud_file_storage.cloud_file_storage.doctype.cloud_storage_object.cloud_storage_object import (
	SERVABLE_STATUSES,
)
from cloud_file_storage.migration import adoption, api, engine
from cloud_file_storage.storage import objects
from cloud_file_storage.tests.migration_utils import MigrationTestCase
from cloud_file_storage.tests.utils import TEST_BUCKET, set_mode

OBJECT_DOCTYPE = "Cloud Migration Object"
#: `.bin`, not `.pdf`: core sniffs a `.pdf` upload with pypdf and refuses bytes that
#: are not a real document, which has nothing to do with adoption.
LEGACY_KEY = "attachments/2021/07/14/Sales Invoice/8f2c_report.bin"


class AdoptionTestCase(MigrationTestCase):
	def legacy_row(self, *, key=LEGACY_KEY, content=b"bytes the fork uploaded", url=None):
		"""A File row the way 0.2.x left it: fork URL, key in `s3_object_key`, no local file.

		Built by inserting an ordinary row and then writing the fork's URL straight into the
		column, because core's `validate_file_on_disk` refuses a
		`/api/method/frappe_s3_attachment...` URL outright today. That refusal is the reason
		these rows can only be *found*, never re-created: the fork wrote them through a code
		path that no longer exists, and every real adoption target is a row in exactly this
		state.
		"""
		import os

		doc = self.make_file(file_name=key.rsplit("/", 1)[-1], content=content, is_private=1)
		local = doc._canonical_local_path()
		if local and os.path.exists(local):
			os.remove(local)  # the fork left no local copy

		frappe.db.set_value(
			"File",
			doc.name,
			{
				"file_url": url or f"/api/method/frappe_s3_attachment.controller.generate_file?key={key}",
				"s3_object_key": key,
				"content_hash": None,
			},
			update_modified=False,
		)
		frappe.db.commit()

		# The bytes are already in the bucket, at the fork's key.
		self.store.objects[key] = content
		return frappe.get_doc("File", doc.name)


class TestLegacyForkAdoption(AdoptionTestCase):
	def test_a_fork_row_is_classified_as_legacy_and_keyed_by_its_object_key(self):
		self.legacy_row()
		campaign = self.campaign()
		self.analyze(campaign)

		name = engine.object_name_for(campaign, f"s3legacy::{LEGACY_KEY}")
		obj = frappe.db.get_value(OBJECT_DOCTYPE, name, "*", as_dict=True)
		self.assertIsNotNone(obj, "the fork row produced no migration object")
		self.assertEqual(obj.identity_kind, "legacy_fork_key")
		self.assertEqual(obj.classification, "legacy_fork")
		self.assertEqual(obj.legacy_key, LEGACY_KEY)

	def test_adoption_creates_a_legacy_unverified_object_without_re_uploading(self):
		doc = self.legacy_row()
		campaign = self.campaign()
		self.analyze(campaign)
		self.plan(campaign)
		set_mode("S3_PRIMARY_LOCAL_FALLBACK")

		before = list(self.store.put_calls)
		api.start_migration(campaign, skip_preflight=True)
		self.drain(campaign)

		name = engine.object_name_for(campaign, f"s3legacy::{LEGACY_KEY}")
		obj = frappe.db.get_value(OBJECT_DOCTYPE, name, "*", as_dict=True)
		self.assertEqual(self.store.put_calls, before, "adoption re-uploaded bytes")

		cso = objects.get_cso(obj.cloud_storage_object)
		self.assertIsNotNone(cso, "no cloud object was created for the adopted row")
		self.assertEqual(cso.s3_key, LEGACY_KEY, "adoption moved the object to a new key")
		self.assertIn(cso.status, SERVABLE_STATUSES)
		self.assertEqual(self.link_of(doc), cso.name, "the File row was not linked")

	def test_an_adopted_object_is_never_verified_without_a_hash(self):
		"""A15 — the honesty rule, asserted on the shipped row rather than on the docstring.

		VERIFY runs over adopted objects (their File rows still need the guarded link), and
		what it can check about them is existence, size and — when the ETag is usable at all
		— an MD5. None of that is a hash of the bytes, so the status must survive the pass
		unchanged.
		"""
		self.legacy_row()
		campaign = self.campaign()
		self.migrate(campaign)

		name = engine.object_name_for(campaign, f"s3legacy::{LEGACY_KEY}")
		cso = objects.get_cso(frappe.db.get_value(OBJECT_DOCTYPE, name, "cloud_storage_object"))
		self.assertEqual(cso.status, "legacy_unverified")
		self.assertIsNone(cso.content_sha256)

		# …and promotion, which really does hash, is what changes it.
		self.assertTrue(adoption.promote_adopted_object(cso.name))
		cso.reload()
		self.assertEqual(cso.status, "verified")
		self.assertTrue(cso.content_sha256)

	def test_an_adopted_object_that_was_never_promoted_is_not_cleanup_eligible(self):
		"""The consequence of the rule above: unhashed bytes protect their local copy."""
		from cloud_file_storage.migration import cleanup

		self.legacy_row()
		campaign = self.campaign()
		self.migrate(campaign)

		name = engine.object_name_for(campaign, f"s3legacy::{LEGACY_KEY}")
		row = frappe.db.get_value(OBJECT_DOCTYPE, name, "*", as_dict=True)
		allowed, reason = cleanup.object_cleanup_gate(row)
		self.assertFalse(allowed)
		self.assertIn("legacy_unverified", reason)

	def test_a_pointer_to_nothing_is_a_blocker(self):
		self.legacy_row(key="attachments/2021/07/14/gone.bin")
		self.store.objects.pop("attachments/2021/07/14/gone.bin")
		campaign = self.campaign()
		self.migrate(campaign)

		conflicts = self.conflicts_of(campaign, conflict_type="adoption_failed")
		self.assertTrue(conflicts, "an adoption pointing at nothing was not flagged")
		self.assertEqual({row.severity for row in conflicts}, {"Blocker"})

	def test_adoption_can_be_turned_off(self):
		self.legacy_row()
		campaign = self.campaign(adopt_legacy_fork_rows=0)
		self.analyze(campaign)

		name = engine.object_name_for(campaign, f"s3legacy::{LEGACY_KEY}")
		obj = frappe.db.get_value(OBJECT_DOCTYPE, name, "*", as_dict=True)
		self.assertEqual(obj.status, "Skipped")
		self.assertEqual(obj.skip_reason, "adoption_disabled")


class TestAdoptionHonestyIsStructural(AdoptionTestCase):
	"""ADR supersession S2 — A15's honesty rule is enforced by construction, not convention.

	The reason `adopt_legacy_cso` exists as its own function rather than as an override
	parameter on `ensure_cso` is that an override would let any caller write a status the
	bytes have not earned. These tests assert the property that choice buys: **there is no
	call to this constructor that produces anything other than `legacy_unverified`**, and the
	only route onward re-reads every byte.
	"""

	def test_the_constructor_takes_no_argument_that_could_set_a_status(self):
		"""If a `status` parameter were ever added, this fails — which is the point."""
		parameters = inspect.signature(objects.adopt_legacy_cso).parameters
		self.assertNotIn("status", parameters)
		for name in parameters:
			self.assertNotIn(
				"status",
				name,
				f"{name} looks like a status override; A15 must not be caller-settable",
			)

	def test_no_combination_of_arguments_produces_anything_but_legacy_unverified(self):
		"""Including the one that looks most like a verified object: full hashes and a size."""
		cases = {
			"bare": {},
			"with_md5": {"content_hash_md5": "b" * 32},
			"with_sha256": {"content_sha256": "c" * 64},
			"fully_hashed": {"content_sha256": "d" * 64, "content_hash_md5": "e" * 32},
			"public": {"visibility": "public"},
		}
		for label, extra in cases.items():
			with self.subTest(case=label):
				key = f"attachments/honesty/{label}.bin"
				cso = objects.adopt_legacy_cso(
					s3_key=key,
					file_size=12,
					visibility=extra.pop("visibility", "private"),
					**extra,
				)
				self.addCleanup(frappe.db.delete, objects.CSO_DOCTYPE, {"name": cso.name})
				self.assertEqual(
					cso.status,
					"legacy_unverified",
					f"{label} produced {cso.status}; adoption may only ever write legacy_unverified",
				)
				self.assertEqual(
					frappe.db.get_value(objects.CSO_DOCTYPE, cso.name, "status"),
					"legacy_unverified",
					"the persisted row disagrees with the returned document",
				)

	def test_the_code_writes_exactly_one_status_and_it_is_the_honest_one(self):
		"""A structural read of the function, so a second write path cannot appear unnoticed.

		The docstring is stripped first: it legitimately names `verified` when explaining
		which function *does* promote, so a check that could not tell prose from code would
		be a check about how the function is described.
		"""
		import ast
		import re
		import textwrap

		source = inspect.getsource(objects.adopt_legacy_cso)
		# Through the AST, not through `__doc__`: CPython 3.13 (gh-81283) made the *compiler*
		# strip the common leading whitespace from docstrings, so on py3.13+ `__doc__` no longer
		# matches the tab-indented text in the file and this `replace` removed nothing at all --
		# leaving the docstring, which legitimately names `verified`, inside `body`. The parser
		# was not changed, so the AST carries the literal as written on every version.
		raw_docstring = ast.get_docstring(ast.parse(textwrap.dedent(source)).body[0], clean=False)
		body = source.replace(raw_docstring or "", "")
		# Quoted literals only, and matched whole: a substring search would find "verified"
		# inside "legacy_unverified" and fail on the very status the function is allowed to
		# write.
		statuses = sorted(set(re.findall(r'"(pending_upload|uploaded|verified)"', body)))
		self.assertEqual(
			statuses,
			[],
			f"adopt_legacy_cso mentions {statuses}; it may only ever write legacy_unverified",
		)
		self.assertIn('"status": "legacy_unverified"', body)

	def test_promotion_is_the_only_route_to_verified_and_it_reads_the_bytes(self):
		content = b"bytes that have to be read before anyone calls them verified"
		cso = objects.adopt_legacy_cso(
			s3_key="attachments/honesty/promote.bin", file_size=len(content), visibility="private"
		)
		self.addCleanup(frappe.db.delete, objects.CSO_DOCTYPE, {"name": cso.name})
		self.store.objects["attachments/honesty/promote.bin"] = content

		reads_before = len(self.store.put_calls)
		# Wrapped around the function as it is *currently installed* on the module: the
		# fake store replaced that attribute in setUp, so patching the fake's own method
		# would spy on something nothing calls.
		from cloud_file_storage.storage import engine as storage_engine

		with patch.object(storage_engine, "get_bytes", wraps=storage_engine.get_bytes) as spy:
			self.assertTrue(adoption.promote_adopted_object(cso.name))
			spy.assert_called()

		row = frappe.db.get_value(objects.CSO_DOCTYPE, cso.name, ["status", "content_sha256"], as_dict=True)
		self.assertEqual(row.status, "verified")
		from cloud_file_storage.storage.hashing import digest_bytes

		self.assertEqual(row.content_sha256, digest_bytes(content).sha256)
		self.assertEqual(len(self.store.put_calls), reads_before, "promotion re-uploaded bytes")

	def test_an_adopted_object_cannot_be_jumped_straight_to_verified_without_hashes(self):
		"""The choke point's own state machine is the backstop behind the constructor."""
		cso = objects.adopt_legacy_cso(
			s3_key="attachments/honesty/nohash.bin", file_size=3, visibility="private"
		)
		self.addCleanup(frappe.db.delete, objects.CSO_DOCTYPE, {"name": cso.name})

		objects.set_cso_status(cso.name, "verified")
		doc = frappe.get_doc(objects.CSO_DOCTYPE, cso.name)
		with self.assertRaises(frappe.ValidationError):
			# `validate_identity` refuses a non-legacy_unverified row with no hashes, so a
			# row promoted without them cannot survive its next save.
			doc.save(ignore_permissions=True)


class TestEtagBackfillRule(AdoptionTestCase):
	"""A15 — the ETag becomes `content_hash` only where it provably is the MD5."""

	def test_a_single_part_etag_without_sse_is_taken_as_the_md5(self):
		head = {"ETag": '"5d41402abc4b2a76b9719d911017c592"', "ContentLength": 5}
		self.assertEqual(adoption.etag_md5(head, None), "5d41402abc4b2a76b9719d911017c592")

	def test_a_multipart_etag_is_refused(self):
		head = {"ETag": '"5d41402abc4b2a76b9719d911017c592-4"'}
		self.assertIsNone(adoption.etag_md5(head, None), "a composite ETag was written as an MD5")

	def test_an_aes256_encrypted_object_still_yields_its_md5(self):
		head = {"ETag": '"5d41402abc4b2a76b9719d911017c592"', "ServerSideEncryption": "AES256"}
		self.assertIsNotNone(adoption.etag_md5(head, None))

	def test_a_kms_encrypted_object_does_not(self):
		head = {"ETag": '"5d41402abc4b2a76b9719d911017c592"', "ServerSideEncryption": "aws:kms"}
		self.assertIsNone(adoption.etag_md5(head, None))

	def test_a_customer_key_encrypted_object_does_not(self):
		head = {
			"ETag": '"5d41402abc4b2a76b9719d911017c592"',
			"SSECustomerAlgorithm": "AES256",
		}
		self.assertIsNone(adoption.etag_md5(head, None))


class TestPromotion(AdoptionTestCase):
	"""A15 — the only route from `legacy_unverified` to `verified`."""

	def test_promotion_hashes_the_bytes_and_records_them(self):
		content = b"adopted bytes to be hashed"
		cso = objects.adopt_legacy_cso(
			s3_key="attachments/promote/me.bin", file_size=len(content), visibility="private"
		)
		self.store.objects["attachments/promote/me.bin"] = content

		self.assertTrue(adoption.promote_adopted_object(cso.name))

		row = frappe.db.get_value(
			objects.CSO_DOCTYPE, cso.name, ["status", "content_sha256", "content_hash_md5"], as_dict=True
		)
		self.assertEqual(row.status, "verified")
		from cloud_file_storage.storage.hashing import digest_bytes

		digest = digest_bytes(content)
		self.assertEqual(row.content_sha256, digest.sha256)
		self.assertEqual(row.content_hash_md5, digest.md5)

	def test_promotion_refuses_when_the_bytes_do_not_match_the_recorded_etag(self):
		cso = objects.adopt_legacy_cso(
			s3_key="attachments/promote/liar.bin",
			file_size=4,
			visibility="private",
			content_hash_md5="0" * 32,
		)
		self.store.objects["attachments/promote/liar.bin"] = b"real"

		self.assertFalse(adoption.promote_adopted_object(cso.name))
		self.assertNotEqual(
			frappe.db.get_value(objects.CSO_DOCTYPE, cso.name, "status"),
			"verified",
			"an object whose bytes disagree with its ETag was promoted",
		)

	def test_promotion_refuses_when_the_size_drifted(self):
		cso = objects.adopt_legacy_cso(
			s3_key="attachments/promote/short.bin", file_size=999, visibility="private"
		)
		self.store.objects["attachments/promote/short.bin"] = b"tiny"

		self.assertFalse(adoption.promote_adopted_object(cso.name))
		self.assertNotEqual(frappe.db.get_value(objects.CSO_DOCTYPE, cso.name, "status"), "verified")


class TestRemoteUrlAdoption(AdoptionTestCase):
	"""A18 — only URLs that name the configured bucket are adopted."""

	def setUp(self):
		super().setUp()
		set_mode("LOCAL_ONLY", bucket=TEST_BUCKET, endpoint_url="")

	def test_a_virtual_hosted_url_for_our_bucket_parses(self):
		parsed = adoption.parse_remote_url(
			f"https://{TEST_BUCKET}.s3.eu-west-1.amazonaws.com/attachments/a/b.bin"
		)
		self.assertEqual(parsed, {"bucket": TEST_BUCKET, "key": "attachments/a/b.bin"})

	def test_a_path_style_url_for_our_bucket_parses(self):
		parsed = adoption.parse_remote_url(
			f"https://s3.eu-west-1.amazonaws.com/{TEST_BUCKET}/attachments/a/b.bin"
		)
		self.assertEqual(parsed, {"bucket": TEST_BUCKET, "key": "attachments/a/b.bin"})

	def test_someone_elses_bucket_is_not_adopted(self):
		self.assertIsNone(
			adoption.parse_remote_url("https://another-bucket.s3.amazonaws.com/attachments/a/b.bin")
		)
		self.assertIsNone(adoption.parse_remote_url("https://cdn.example.invalid/files/a/b.pdf"))

	def test_a_configured_endpoint_host_is_recognised(self):
		set_mode("LOCAL_ONLY", bucket=TEST_BUCKET, endpoint_url="http://127.0.0.1:9000")
		parsed = adoption.parse_remote_url(f"http://127.0.0.1:9000/{TEST_BUCKET}/attachments/a/b.bin")
		self.assertEqual(parsed, {"bucket": TEST_BUCKET, "key": "attachments/a/b.bin"})

	def test_a_remote_row_we_do_not_own_is_skipped_rather_than_claimed(self):
		doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "theirs.bin",
				"file_url": "https://someone-else.s3.amazonaws.com/theirs.bin",
				"is_private": 0,
			}
		)
		doc.flags.ignore_duplicate_entry_error = True
		doc.insert(ignore_permissions=True)
		self.track(doc)

		campaign = self.campaign()
		self.analyze(campaign)

		name = engine.object_name_for(
			campaign, "remote_https::https://someone-else.s3.amazonaws.com/theirs.bin"
		)
		obj = frappe.db.get_value(OBJECT_DOCTYPE, name, "*", as_dict=True)
		self.assertEqual(obj.status, "Skipped")
		self.assertEqual(obj.skip_reason, "remote_not_ours")
		self.assertIsNone(obj.cloud_storage_object)
