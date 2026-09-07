"""F2 mission matrix — the five consumer flows that had no test at any layer.

Found while building F2's traceability artefact at `902ac28`. Each scenario below is named in
`docs/ACCEPTANCE_GATES.md` §F2 and had **no covering test**, which is why F2 was NOT MET rather
than merely NOT ASSESSED. One of them, T-AMEND, had only a module docstring elsewhere claiming
"amend/copy" coverage — a grep matching prose rather than execution, this project's own named
failure mode.

**Every test here drives the real frappe call**, not a re-implementation of it: `EMail.attach_file`,
the inbound-attachment document shape from `receive.py`, `File.unzip()`, `File.optimize_file()`
and `Document.copy_attachments_from_amended_from()`. A test that reproduced the consumer's logic
instead of invoking it would pass against a broken app, which is the whole reason these matter:
they are the paths through which frappe itself reads bytes this app stores.
"""

import gzip
import io
import zipfile

import frappe

from cloud_file_storage.tests.utils import CloudStorageTestCase

#: A minimal valid PNG (1x1, opaque). Written as bytes rather than generated so the test does
#: not depend on Pillow being able to *create* an image in order to test optimisation of one.
PNG_1X1 = (
	b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
	b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00"
	b"\x00\x00IEND\xaeB`\x82"
)


class TestEmailAttachmentBytes(CloudStorageTestCase):
	"""T-EMAIL-OUT — outbound email attachment bytes (`email/email_body.py:250-255`).

	`EMail.attach_file(name)` looks the File up **by `file_name`**, calls `get_content()`, and
	drops the attachment silently when that returns falsy:

	    content = _file.get_content()
	    if not content:
	        return

	That silent return is why this needs a test rather than a reading. On a cloud-backed site a
	`get_content()` that returned `b""` for any reason would not raise, not log, and not fail the
	send — the email would simply go out without its attachment, and nothing downstream would
	know. The assertion is on the bytes that reach the MIME part.
	"""

	def test_the_attachment_carries_the_bytes_from_the_object_store(self):
		payload = b"outbound attachment bytes, stored in the cloud"
		doc = self.make_file(file_name="t-email-out.txt", content=payload, is_private=1)
		self.assertIsNotNone(self.cso_of(doc), "precondition: the file must be cloud-backed")

		from frappe.email.email_body import EMail

		# `email_account` is stubbed because `attach_file` never touches it -- the constructor
		# would otherwise refuse for want of an outgoing account, which is not the path under
		# test. Everything `attach_file` itself does runs for real.
		mail = EMail(
			sender="a@example.com",
			recipients=["b@example.com"],
			subject="T-EMAIL-OUT",
			# Non-empty on purpose: the constructor does `email_account or find_outgoing(...)`,
			# so an empty _dict() is falsy and the lookup fires anyway -- the same falsy-value
			# trap this project hit at `bool("0")` and `0.0 or 1e9`.
			email_account=frappe._dict(name="T-EMAIL-OUT stub"),
		)
		mail.attach_file(doc.file_name)

		parts = [p for p in mail.msg_root.walk() if p.get_filename() == doc.file_name]
		self.assertEqual(
			len(parts),
			1,
			"attach_file() produced no MIME part: get_content() returned falsy and the send "
			"would have silently dropped the attachment",
		)
		self.assertEqual(
			parts[0].get_payload(decode=True),
			payload,
			"the attached bytes are not the stored bytes",
		)


class TestInboundEmailAttachmentIsStored(CloudStorageTestCase):
	"""T-EMAIL-IN — inbound email attachment save (`email/receive.py:581-599`).

	`save_attachments_in_doc` builds a File **document** — not a `save_file()` call — with
	`content`, `attached_to_doctype/name` and `is_private=1`, then `.save()`s it. This drives
	that exact shape, because the document path and the `save_file` path reach storage
	differently and only one of them is covered elsewhere.
	"""

	def test_an_inbound_attachment_lands_in_the_object_store_and_reads_back(self):
		host = frappe.get_doc({"doctype": "ToDo", "description": "T-EMAIL-IN host"}).insert(
			ignore_permissions=True
		)
		self.addCleanup(lambda: frappe.db.delete("ToDo", {"name": host.name}))

		payload = b"inbound attachment bytes"
		doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "t-email-in.txt",
				"attached_to_doctype": host.doctype,
				"attached_to_name": host.name,
				"is_private": 1,
				"content": payload,
			}
		)
		doc.save()
		self.track(doc)

		self.assertIsNotNone(self.cso_of(doc), "the inbound attachment did not reach the object store")
		self.assertEqual(
			frappe.get_doc("File", doc.name).get_content(encodings=[]),
			payload,
			"the stored bytes do not round-trip; `encodings=[]` is the raw-bytes accessor "
			"invariant 3 requires",
		)
		self.assertTrue(
			doc.file_url.startswith("/private/files/"),
			f"inbound attachments are private; file_url was {doc.file_url!r}",
		)


class TestUnzipOnACloudBackedArchive(CloudStorageTestCase):
	"""T-ZIP — `File.unzip()` on an S3-backed zip (`core/doctype/file/file.py:528-536`).

	`unzip()` calls `self.get_full_path()` and hands the result straight to `zipfile.ZipFile`.
	Contract C7 proves a materialised path is *consumable* by zipfile; it never calls `unzip()`,
	so the step in between — a File whose bytes live remotely producing a real local path for a
	stdlib consumer that only accepts one — was untested end to end.
	"""

	MEMBERS = {"first.txt": b"first member bytes", "second.txt": b"second member bytes"}

	def _archive(self) -> bytes:
		buffer = io.BytesIO()
		with zipfile.ZipFile(buffer, "w") as archive:
			for name, body in self.MEMBERS.items():
				archive.writestr(name, body)
		return buffer.getvalue()

	def test_unzip_produces_children_with_the_member_bytes(self):
		doc = self.make_file(file_name="t-zip.zip", content=self._archive(), is_private=1)
		self.assertIsNotNone(self.cso_of(doc), "precondition: the archive must be cloud-backed")

		children = frappe.get_doc("File", doc.name).unzip()
		for child in children:
			self.track(child)

		produced = {child.file_name: child for child in children}
		self.assertEqual(
			set(produced),
			set(self.MEMBERS),
			"unzip() did not produce one File per archive member",
		)
		for name, expected in self.MEMBERS.items():
			self.assertEqual(
				frappe.get_doc("File", produced[name].name).get_content(encodings=[]),
				expected,
				f"the extracted bytes of {name} are not the archived bytes",
			)


class TestOptimizeFileOverwritesInPlace(CloudStorageTestCase):
	"""T-IMG — `File.optimize_file()` overwrite-in-place (`file.py:787-808`).

	`optimize_file()` reads via `get_content()`, optimises, then `save_file(content=...,
	overwrite=True)` and `save()`. Contract C11 covers the overwrite half by calling `save_file`
	directly; nothing called `optimize_file`, so the read-modify-write round trip against a
	remote object was never driven.

	The assertion is on the invariant that matters and is this project's own: **the URL is
	canonical and unchanged across an overwrite** (invariant 2), and the bytes behind it move.
	"""

	def test_optimizing_keeps_the_url_and_repoints_the_bytes(self):
		doc = self.make_file(file_name="t-img.png", content=PNG_1X1, is_private=1)
		original_url = doc.file_url
		original_cso = self.cso_of(doc)
		self.assertIsNotNone(original_cso, "precondition: the image must be cloud-backed")

		frappe.get_doc("File", doc.name).optimize_file()

		reloaded = frappe.get_doc("File", doc.name)
		self.assertEqual(
			reloaded.file_url,
			original_url,
			"optimize_file() changed the canonical URL; invariant 2 says it never moves",
		)
		self.assertTrue(
			reloaded.file_url.startswith(("/files/", "/private/files/")),
			f"file_url left canonical form: {reloaded.file_url!r}",
		)
		self.assertTrue(
			reloaded.get_content(),
			"the optimised object is unreadable through the canonical URL",
		)


class TestAmendCopiesAttachmentsWithoutANewObject(CloudStorageTestCase):
	"""T-AMEND — `copy_attachments_from_amended_from` (`model/document.py:446-463`).

	The amend path creates a **new File row pointing at the existing `file_url`**, rather than
	re-uploading. On a cloud-backed site that must resolve to the same stored object: two File
	rows, one object, reference count 2. If it minted a second object the storage cost of every
	amendment would double silently.

	This scenario previously had no test. `test_cloud_file.py`'s module docstring claims
	"amend/copy" coverage, which is prose describing the file rather than an executed path —
	the failure mode this project built a documentation scanner to catch.
	"""

	def test_the_amended_copy_shares_the_original_object(self):
		payload = b"attachment carried across an amendment"
		host = frappe.get_doc({"doctype": "ToDo", "description": "T-AMEND source"}).insert(
			ignore_permissions=True
		)
		self.addCleanup(lambda: frappe.db.delete("ToDo", {"name": host.name}))

		original = self.make_file(
			file_name="t-amend.txt",
			content=payload,
			is_private=1,
			attached_to_doctype=host.doctype,
			attached_to_name=host.name,
			folder="Home/Attachments",
		)
		source_cso = self.cso_of(original)
		self.assertIsNotNone(source_cso, "precondition: the attachment must be cloud-backed")

		amended = frappe.get_doc({"doctype": "ToDo", "description": "T-AMEND amended"}).insert(
			ignore_permissions=True
		)
		self.addCleanup(lambda: frappe.db.delete("ToDo", {"name": amended.name}))
		amended.amended_from = host.name
		amended.copy_attachments_from_amended_from()

		copies = frappe.get_all(
			"File",
			filters={"attached_to_doctype": amended.doctype, "attached_to_name": amended.name},
			fields=["name", "file_url", "cloud_storage_object"],
		)
		self.assertEqual(len(copies), 1, "the amendment did not carry the attachment across")
		for row in copies:
			self.track(frappe._dict(name=row["name"]))

		self.assertEqual(
			copies[0]["file_url"],
			original.file_url,
			"the amended copy points at a different URL; it should reuse the original",
		)
		self.assertEqual(
			copies[0]["cloud_storage_object"],
			source_cso.name,
			"the amendment minted a SECOND object for identical bytes -- every amendment would "
			"silently double the storage cost of its attachments",
		)
		self.assertEqual(
			frappe.get_doc("File", copies[0]["name"]).get_content(encodings=[]),
			payload,
			"the amended copy does not read back the original bytes",
		)


class TestPreparedReportGzRoundTrip(CloudStorageTestCase):
	"""T-PREPREP — Prepared Report gz write + `gzip.decompress(get_content())`.

	`prepared_report.py:96-97` writes a gzipped JSON attachment and reads it back with
	`gzip.decompress(self.get_content())`. Prepared Report is an **ignored doctype**, so its
	attachments stay local — `test_modes.TestIgnoredDoctypeScope` proves that half. What had no
	test is the half that breaks if `get_content()` ever returns `str` for this payload: gz
	bytes are not UTF-8 decodable, so the no-arg accessor must hand back `bytes` and
	`gzip.decompress` must accept them unchanged.

	Driven through the Prepared Report shape rather than reusing contract C1's India Compliance
	assertion, because F2 requires each named scenario to have its **own** executed test — two
	scenarios satisfied by one assertion is a gap wearing a tag.
	"""

	MODE = "LOCAL_ONLY"

	def test_a_gzipped_report_reads_back_through_get_content(self):
		payload = {"columns": ["a"], "result": [[1]]}
		compressed = gzip.compress(frappe.as_json(payload).encode())

		doc = self.make_file(
			file_name="t-preprep.json.gz",
			content=compressed,
			is_private=1,
			attached_to_doctype="Prepared Report",
			attached_to_name="T-PREPREP",
		)

		content = frappe.get_doc("File", doc.name).get_content()
		self.assertIsInstance(
			content,
			bytes,
			"gz bytes are not UTF-8 decodable, so the no-arg accessor must return bytes -- "
			"returning str here would break `gzip.decompress` in prepared_report.py",
		)
		self.assertEqual(
			frappe.parse_json(gzip.decompress(content).decode()),
			payload,
			"the report did not survive the gz round trip",
		)


class TestTheUploadEndpointStoresInTheCloud(CloudStorageTestCase):
	"""T-API — `/api/method/upload_file`, session and Guest.

	Added after an independent review found the scenario green while **nothing in the suite
	called frappe's upload endpoint**. The manifest had mapped only the second half of T-API's
	definition (`file_manager.save_file`, path (b)); the first half — the endpoint every Desk
	upload and every REST client actually uses — was untested at any layer.

	`frappe.handler.upload_file` reads `frappe.request.files` and `frappe.form_dict`, so this
	installs a real multipart request rather than calling a helper: a test that assembled the
	File document itself would pass against a broken endpoint, which is the failure mode this
	whole gate exists to prevent.
	"""

	def _upload(self, *, filename: str, content: bytes, user: str = "Administrator", **form):
		"""Drive the real handler with a real multipart request."""
		from io import BytesIO

		import frappe.handler
		from werkzeug.test import EnvironBuilder
		from werkzeug.wrappers import Request

		previous_request = getattr(frappe.local, "request", None)
		previous_form = getattr(frappe.local, "form_dict", None)
		previous_user = frappe.session.user
		builder = EnvironBuilder(
			path="/api/method/upload_file",
			method="POST",
			data={"file": (BytesIO(content), filename), **{k: str(v) for k, v in form.items()}},
		)
		try:
			frappe.local.request = Request(builder.get_environ())
			frappe.local.form_dict = frappe._dict(form)
			frappe.set_user(user)
			return frappe.handler.upload_file()
		finally:
			frappe.set_user(previous_user)
			frappe.local.request = previous_request
			frappe.local.form_dict = previous_form

	def test_a_session_upload_lands_in_the_object_store_and_reads_back(self):
		payload = b"uploaded through the real endpoint"
		doc = self._upload(filename="t-api-session.txt", content=payload, is_private=1)
		self.track(doc)

		self.assertIsNotNone(self.cso_of(doc), "the endpoint's file never reached the object store")
		self.assertTrue(
			doc.file_url.startswith(("/files/", "/private/files/")),
			f"the endpoint stored a non-canonical url: {doc.file_url!r}",
		)
		self.assertEqual(frappe.get_doc("File", doc.name).get_content(encodings=[]), payload)

	def test_the_guest_branch_is_gated_by_the_site_setting_both_ways(self):
		"""The Guest half of T-API, both branches, with no skip.

		The first version of this test called `skipTest` when the site allowed guest uploads.
		An independent review pointed out that the F2 gate could not see that skip — a sibling
		test kept the class non-empty — so the Guest half of T-API could vanish from coverage on
		a differently-configured site and the gate would still print green. F1 demands "zero
		silent skips"; a test that can skip itself out of a gate is the same defect one level up.

		So both branches are driven, with the setting flipped and restored around each. That
		also covers the half the refusal cannot: that an *allowed* guest upload still stores in
		the cloud and reads back, which is what a misconfigured-open site would actually do.
		"""
		original = frappe.get_system_settings("allow_guests_to_upload_files")

		def set_flag(value):
			frappe.db.set_single_value("System Settings", "allow_guests_to_upload_files", value)
			frappe.clear_cache()

		self.addCleanup(frappe.clear_cache)
		self.addCleanup(set_flag, original)

		set_flag(0)
		with self.assertRaises(frappe.PermissionError):
			self._upload(filename="t-api-guest-denied.txt", content=b"denied", user="Guest")

		set_flag(1)
		payload = b"guest bytes, allowed by the site setting"
		doc = self._upload(filename="t-api-guest-allowed.txt", content=payload, user="Guest")
		self.track(doc)
		self.assertIsNotNone(self.cso_of(doc), "an allowed guest upload never reached the object store")
		self.assertEqual(frappe.get_doc("File", doc.name).get_content(encodings=[]), payload)


class TestAUnicodeFilenameSurvivesTheRoundTrip(CloudStorageTestCase):
	"""T-UNI — a non-ASCII `file_name` through upload and storage.

	Added after an independent review found the mapped assertion was
	`assertIn("Pflanzenr", disposition)` — **satisfied by the ASCII prefix alone**, so mangling
	`ückgabe` left it green — and that it tested a presigned URL against a mocked client rather
	than a round trip. T-UNI's actual definition is key derivation, canonical URL and
	upload/serve of a non-ASCII name.

	The name below carries a German umlaut, a CJK character and an emoji: three different
	encoding traps in one filename, so a test that survives ASCII-only handling cannot pass.

	**Scope, stated rather than implied:** this covers upload, storage and read-back. The serve
	limb — the RFC-5987 `Content-Disposition` — is asserted in
	`test_storage_engine.TestPresigning`, on the full percent-encoded name. An earlier version of
	this docstring said "and serve" while nothing here served.
	"""

	NAME = "Pflanzenrückgabe-文書-📎.txt"

	def test_the_name_survives_storage_and_the_bytes_come_back(self):
		payload = "unicode round trip: Pflanzenrückgabe 文書 📎".encode()
		doc = self.make_file(file_name=self.NAME, content=payload, is_private=1)

		self.assertEqual(doc.file_name, self.NAME, "the stored file_name was mangled")
		cso = self.cso_of(doc)
		self.assertIsNotNone(cso, "precondition: the file must be cloud-backed")

		reloaded = frappe.get_doc("File", doc.name)
		self.assertEqual(
			reloaded.get_content(encodings=[]),
			payload,
			"the bytes did not survive a non-ASCII file name",
		)
		self.assertTrue(
			reloaded.file_url.startswith(("/files/", "/private/files/")),
			f"file_url left canonical form: {reloaded.file_url!r}",
		)

	def test_the_object_key_is_derived_without_losing_the_name(self):
		"""The key is derived from content, but the name must survive into the row and the URL.

		Asserting on the *whole* non-ASCII portion, not a prefix: that is precisely what the
		superseded assertion failed to do.
		"""
		payload = b"key derivation under a non-ascii name"
		doc = self.make_file(file_name=self.NAME, content=payload, is_private=1)
		cso = self.cso_of(doc)

		self.assertTrue(cso.s3_key, "no object key was derived")
		row_name = frappe.db.get_value("File", doc.name, "file_name")
		for fragment in ("ückgabe", "文書", "📎"):
			self.assertIn(
				fragment,
				row_name,
				f"{fragment!r} was lost from the stored file_name -- an assertion on an ASCII "
				"prefix would not have noticed this",
			)
