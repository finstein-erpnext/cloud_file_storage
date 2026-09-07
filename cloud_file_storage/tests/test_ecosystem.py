"""L3 ecosystem tests — erpnext and India Compliance driving the real runtime (F5).

PLAN §B, the P4 row: `T-RIV`, `T-ITEMIMG`, `T-IC-LIVE` (including
`upload-gstr-after-cleanup`). What makes these worth running is that **the app code is
real**: `erpnext.stock.stock_ledger.create_json_gz_file`, `get_reposting_data`,
`india_compliance`'s `GSTReturnLog.update_json_for`/`download_file` and its import-bound
`get_file_path` are imported and called, not re-implemented here. A re-implementation
would keep passing after the upstream call changed shape, which is the whole failure mode
this gate exists to catch.

Only the bucket is faked (`FakeObjectStore`, as everywhere else in this suite) so the run
is deterministic and offline; the L3 CI job additionally runs the MinIO suite against a
real bucket in the same container.

Every class runs in **S3_ONLY**: it is the mode with no local copy to fall back on, so a
resolution or write-back gap shows up as a failure instead of being masked by bytes that
happen to still be on disk.

On a site without erpnext/India Compliance these skip **loudly**, naming the missing app.
`.github/helper/check_ecosystem_completeness.sh` fails the L3 job if that happens there,
so "the ecosystem job is green" can never mean "the ecosystem job did nothing" (A22).
"""

import gzip
import json
import os
import unittest
from pathlib import Path

import frappe
from frappe.utils import cint
from frappe.utils.file_manager import get_content_hash

from cloud_file_storage.cache import materialize, writeback
from cloud_file_storage.serving import runtime_patches
from cloud_file_storage.tests.utils import CloudStorageTestCase

#: Apps every class in this module needs. hrms is installed by the L3 job as a
#: coexistence check (it has no file-API call site of its own — `grep -rn
#: "get_full_path\\|save_file(\\|get_file_path" apps/hrms` is empty), so it is not
#: required here.
ECOSYSTEM_APPS = ("erpnext", "india_compliance")

#: A 1x1 PNG. Small enough to inline, real enough for PIL to open in `make_thumbnail`.
PNG_1X1 = bytes.fromhex(
	"89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
	"de0000000c4944415408d763f8cfc0000003010100b5c0f9a10000000049454e"
	"44ae426082"
)


def missing_ecosystem_apps() -> list[str]:
	return [app for app in ECOSYSTEM_APPS if app not in frappe.get_installed_apps()]


class EcosystemTestCase(CloudStorageTestCase):
	MODE = "S3_ONLY"

	@classmethod
	def setUpClass(cls):
		missing = missing_ecosystem_apps()
		if missing:
			raise unittest.SkipTest(
				f"ecosystem test needs {', '.join(missing)} installed on this site — "
				"run it on the L3 site (PLAN §B P4 / ACCEPTANCE_GATES F5)"
			)
		super().setUpClass()

	def setUp(self):
		super().setUp()
		# `_state` in runtime_patches is per process. Leaving the patch installed would make
		# `test_serving_patches.TestIndiaComplianceImportBoundPatch` — which asserts that a
		# fake IC module *gets* patched — silently pass over an already-decided state.
		self.addCleanup(runtime_patches.uninstall)


class TestRepostItemValuation(EcosystemTestCase):
	"""T-RIV — erpnext's write-through: `get_full_path()` + `open(path, "wb")`.

	`stock_ledger.create_json_gz_file` (v15.93.0, lines 408-425) is the only place in
	erpnext that mutates an attachment's bytes in place. It also carries the fork sentinel
	`"/frappe_s3_attachment." in file_doc.file_url`, whose True branch **deletes the File**.
	Our `file_url` is canonical, so that branch must stay unreachable.
	"""

	def _riv(self):
		"""A Repost Item Valuation row, without erpnext's stock validations.

		`validate()` reaches for a Company through the Warehouse, which would drag the whole
		chart-of-accounts setup into a test about file bytes. The reposting-data attachment
		code path does not read any of those fields — it uses `doctype`, `name` and
		`reposting_data_file`.
		"""
		doc = frappe.get_doc(
			{
				"doctype": "Repost Item Valuation",
				"based_on": "Transaction",
				"voucher_type": "Stock Entry",
				"voucher_no": "CFS-ECOSYSTEM-SE",
				"posting_date": "2026-01-01",
				"posting_time": "10:00:00",
				"status": "Queued",
			}
		)
		doc.flags.ignore_validate = True
		doc.flags.ignore_mandatory = True
		doc.insert(ignore_permissions=True)
		self.addCleanup(frappe.db.delete, "Repost Item Valuation", {"name": doc.name})
		return doc

	@staticmethod
	def _payload(marker: str) -> dict:
		return {
			"items_to_be_repost": [{"item_code": marker, "warehouse": "W", "posting_date": "2026-01-01"}],
			"distinct_item_and_warehouse": {},
			"affected_transactions": [],
		}

	def _attached_file(self, riv):
		from erpnext.stock.stock_ledger import get_reposting_file_name

		name = get_reposting_file_name(riv.doctype, riv.name)
		self.assertTrue(name, "erpnext could not find the File row it just attached")
		doc = frappe.get_doc("File", name)
		self.track(doc)
		return doc

	def test_the_first_write_produces_a_canonical_cloud_backed_attachment(self):
		from erpnext.stock.stock_ledger import create_json_gz_file, get_reposting_data

		riv = self._riv()
		file_url = create_json_gz_file(self._payload("ITEM-A"), riv)

		self.assertTrue(
			file_url.startswith("/private/files/"),
			f"erpnext's reposting attachment must keep a canonical private URL, got {file_url!r}",
		)
		file_doc = self._attached_file(riv)
		self.assertEqual(file_doc.file_url, file_url)

		cso = self.cso_of(file_doc)
		self.assertIsNotNone(cso, "the reposting attachment was never linked to a cloud object")
		self.assertTrue(self.store.contains(cso), "the reposting bytes never reached the bucket")

		local = self.local_path(file_doc)
		self.assertFalse(
			local and os.path.exists(local),
			"S3_ONLY kept a local copy, so nothing below proves the cloud path was used",
		)

		# erpnext's own reader, through `File.get_content()`.
		self.assertEqual(
			get_reposting_data(file_url).items_to_be_repost[0]["item_code"],
			"ITEM-A",
			"erpnext could not read back the reposting data it just wrote",
		)

	def test_the_fork_sentinel_never_matches_a_canonical_url(self):
		"""The True branch of that sentinel calls `file_doc.delete()` and starts over.

		Two things have to hold, and they fail differently: the sentinel must still be in
		erpnext (if upstream drops it, this test should stop claiming to guard anything),
		and our URL must not match it.
		"""
		import erpnext.stock.stock_ledger as stock_ledger
		from erpnext.stock.stock_ledger import create_json_gz_file

		source = Path(stock_ledger.__file__).read_text(encoding="utf-8")
		self.assertIn(
			'"/frappe_s3_attachment." in file_doc.file_url',
			source,
			"erpnext no longer carries the fork sentinel; this guard needs rewriting",
		)

		riv = self._riv()
		create_json_gz_file(self._payload("ITEM-SENTINEL"), riv)
		file_doc = self._attached_file(riv)
		self.assertNotIn("/frappe_s3_attachment.", file_doc.file_url)

		riv.reposting_data_file = file_doc.file_url
		create_json_gz_file(self._payload("ITEM-SENTINEL-2"), riv, file_doc.name)

		self.assertTrue(
			frappe.db.exists("File", file_doc.name),
			"erpnext took the fork branch and deleted the attachment",
		)

	def test_a_rewrite_through_get_full_path_reaches_the_object_store(self):
		from erpnext.stock.stock_ledger import create_json_gz_file, get_reposting_data

		riv = self._riv()
		file_url = create_json_gz_file(self._payload("ITEM-V1"), riv)
		riv.reposting_data_file = file_url
		file_doc = self._attached_file(riv)
		original_cso = file_doc.cloud_storage_object
		self.assertTrue(original_cso, "precondition: the first write must have linked an object")
		frappe.db.commit()

		create_json_gz_file(self._payload("ITEM-V2"), riv, file_doc.name)
		writeback.flush_request()

		repointed = frappe.db.get_value("File", file_doc.name, "cloud_storage_object")
		self.assertNotEqual(repointed, original_cso, "write-back never repointed the File row")

		self.assertEqual(
			get_reposting_data(file_url).items_to_be_repost[0]["item_code"],
			"ITEM-V2",
			"erpnext read back the pre-rewrite data",
		)
		stored = self.store.objects[frappe.get_doc("Cloud Storage Object", repointed).s3_key]
		self.assertEqual(json.loads(gzip.decompress(stored))["items_to_be_repost"][0]["item_code"], "ITEM-V2")

	def _write_gz(self, path, marker):
		with open(path, "wb") as handle:
			handle.write(gzip.compress(frappe.safe_encode(frappe.as_json(self._payload(marker)))))

	def _riv_with_attachment(self, marker):
		from erpnext.stock.stock_ledger import create_json_gz_file

		riv = self._riv()
		file_url = create_json_gz_file(self._payload(marker), riv)
		riv.reposting_data_file = file_url
		file_doc = self._attached_file(riv)
		frappe.db.commit()
		return file_url, frappe.get_doc("File", file_doc.name)

	def test_a_second_hand_out_does_not_overwrite_the_bytes_just_written(self):
		"""A4: erpnext hands the path out once per reposting chunk against the same File.

		The markers differ in length on purpose: the entry's size then differs from its
		sidecar, which is precisely the state in which a re-download would clobber bytes that
		exist nowhere else yet.
		"""
		file_url, reloaded = self._riv_with_attachment("SHORT")

		first_path = reloaded.get_full_path()
		self._write_gz(first_path, "A-MUCH-LONGER-ITEM-CODE-THAN-THE-FIRST-ONE")
		written_size = os.path.getsize(first_path)
		self.assertNotEqual(
			written_size,
			materialize.read_sidecar(first_path)["size"],
			"the rewrite must change the entry's size, or this test cannot see a clobber",
		)

		second_path = reloaded.get_full_path()

		self.assertEqual(second_path, first_path, "the second hand-out moved the file")
		with open(second_path, "rb") as handle:
			self.assertEqual(
				json.loads(gzip.decompress(handle.read()))["items_to_be_repost"][0]["item_code"],
				"A-MUCH-LONGER-ITEM-CODE-THAN-THE-FIRST-ONE",
				"the second get_full_path() re-downloaded over the caller's unflushed bytes",
			)

	def test_a_second_hand_out_does_not_disarm_the_pending_write_back(self):
		"""The same-length case, where nothing is clobbered but everything is still lost.

		Write-back compares the current `(mtime, size)` against the one recorded at hand-out.
		If the second hand-out re-baselines to the post-write state, `_sync_back` sees no
		change, the edit never reaches the bucket, and the only trace is a cache entry that
		eviction will eventually drop.
		"""
		from erpnext.stock.stock_ledger import get_reposting_data

		file_url, reloaded = self._riv_with_attachment("ITEM-ONE")

		first_path = reloaded.get_full_path()
		self._write_gz(first_path, "ITEM-TWO")
		self.assertEqual(
			os.path.getsize(first_path),
			materialize.read_sidecar(first_path)["size"],
			"same-length precondition: this test is about the case a size check cannot see",
		)

		reloaded.get_full_path()
		writeback.flush_request()

		self.assertEqual(
			get_reposting_data(file_url).items_to_be_repost[0]["item_code"],
			"ITEM-TWO",
			"the second get_full_path() disarmed write-back and the edit never left the disk",
		)


class TestItemImage(EcosystemTestCase):
	"""T-ITEMIMG — an Item image round-trip, the commonest Attach Image in erpnext."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls._fixtures = []
		for doctype, values in (
			("UOM", {"uom_name": "CFS Ecosystem Unit"}),
			("Item Group", {"item_group_name": "CFS Ecosystem Group", "is_group": 0}),
		):
			existing = frappe.db.exists(doctype, values.get("uom_name") or values.get("item_group_name"))
			if not existing:
				doc = frappe.get_doc({"doctype": doctype, **values}).insert(ignore_permissions=True)
				cls._fixtures.append((doctype, doc.name))
		frappe.db.commit()
		cls.addClassCleanup(cls._drop_fixtures)

	@classmethod
	def _drop_fixtures(cls):
		for doctype, name in reversed(cls._fixtures):
			frappe.db.delete(doctype, {"name": name})
		frappe.db.commit()

	@classmethod
	def _hsn_code(cls) -> str:
		"""India Compliance makes `gst_hsn_code` mandatory on every Item it validates.

		The digit count is a GST Settings value, so the code is looked up by length rather
		than hard-coded: a site configured for 8 digits would reject a 6-digit literal.
		"""
		digits = cint(frappe.db.get_single_value("GST Settings", "min_hsn_digits")) or 6
		# `"_" * digits` is a LIKE pattern, not a literal: `_` is SQL's single-character
		# wildcard, so `"______"` means "any name exactly six characters long". India
		# Compliance rejects a wrong-length code with a different error than a missing one,
		# which would read here as an unrelated failure.
		existing = frappe.get_all(
			"GST HSN Code", filters={"name": ("like", "_" * digits)}, limit=1, pluck="name"
		)
		if existing:
			return existing[0]

		code = frappe.get_doc(
			{"doctype": "GST HSN Code", "hsn_code": "9" * digits, "description": "CFS ecosystem"}
		).insert(ignore_permissions=True)
		cls._fixtures.append(("GST HSN Code", code.name))
		return code.name

	def _item(self):
		doc = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": f"CFS-ECO-{frappe.generate_hash(length=8)}",
				"item_group": "CFS Ecosystem Group",
				"stock_uom": "CFS Ecosystem Unit",
				"gst_hsn_code": self._hsn_code(),
				"is_stock_item": 0,
			}
		).insert(ignore_permissions=True)
		self.addCleanup(frappe.db.delete, "Item", {"name": doc.name})
		return doc

	def _attach_image(self, item, *, file_name="cfs-item.png", content=PNG_1X1):
		file_doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": file_name,
				"content": content,
				"is_private": 0,
				"attached_to_doctype": "Item",
				"attached_to_name": item.name,
				"attached_to_field": "image",
			}
		).insert(ignore_permissions=True)
		self.track(file_doc)
		# What frappe's upload handler does after the File row exists.
		item.db_set("image", file_doc.file_url)
		return file_doc

	def test_an_item_image_round_trips_through_the_object_store(self):
		item = self._item()
		file_doc = self._attach_image(item)

		self.assertTrue(
			file_doc.file_url.startswith("/files/"),
			f"a public Item image must keep a canonical public URL, got {file_doc.file_url!r}",
		)
		self.assertEqual(frappe.db.get_value("Item", item.name, "image"), file_doc.file_url)

		cso = self.cso_of(file_doc)
		self.assertIsNotNone(cso, "the Item image was never linked to a cloud object")
		local = self.local_path(file_doc)
		self.assertFalse(
			local and os.path.exists(local),
			"S3_ONLY kept a local copy, so the read below would not prove anything",
		)

		reloaded = frappe.get_doc("File", file_doc.name)
		self.assertEqual(reloaded.get_content(encodings=[]), PNG_1X1)
		self.assertEqual(reloaded.content_hash, get_content_hash(PNG_1X1))

	def test_an_item_image_can_still_produce_a_thumbnail(self):
		"""Core reads the source straight off local disk, which is empty under S3_ONLY."""
		item = self._item()
		file_doc = self._attach_image(item, file_name="cfs-item-thumb.png")

		reloaded = frappe.get_doc("File", file_doc.name)
		thumbnail_url = reloaded.make_thumbnail(set_as_thumbnail=True, width=16, height=16)

		self.assertTrue(thumbnail_url, "no thumbnail was produced for a cloud-backed Item image")
		self.assertEqual(
			frappe.db.get_value("File", file_doc.name, "thumbnail_url"),
			thumbnail_url,
			"the thumbnail URL was not persisted on the File row",
		)

	def test_replacing_the_image_repoints_the_item_at_the_new_bytes(self):
		item = self._item()
		first = self._attach_image(item, file_name="cfs-item-old.png")
		second_bytes = PNG_1X1 + b"\x00"
		second = self._attach_image(item, file_name="cfs-item-new.png", content=second_bytes)

		self.assertNotEqual(first.file_url, second.file_url)
		self.assertEqual(frappe.db.get_value("Item", item.name, "image"), second.file_url)
		self.assertEqual(frappe.get_doc("File", second.name).get_content(encodings=[]), second_bytes)
		# The first object is still readable: replacing a field never deletes bytes.
		self.assertEqual(frappe.get_doc("File", first.name).get_content(encodings=[]), PNG_1X1)


class TestIndiaComplianceLive(EcosystemTestCase):
	"""T-IC-LIVE — GST Return Log CRUD + download, e-Waybill `save_file`, A36."""

	def _log(self):
		doc = frappe.get_doc(
			{
				"doctype": "GST Return Log",
				"return_type": "GSTR1",
				"return_period": "042026",
				"gstin": f"24AAUPV7468F{frappe.generate_hash(length=3).upper()}",
			}
		).insert(ignore_permissions=True)
		self.addCleanup(frappe.db.delete, "GST Return Log", {"name": doc.name})
		return doc

	def _track_attachment(self, log, field):
		from india_compliance.gst_india.doctype.gst_return_log.gst_return_log import get_file_doc

		file_doc = get_file_doc(log.doctype, log.name, field)
		self.assertIsNotNone(file_doc, f"India Compliance did not attach a File for {field}")
		self.track(file_doc)
		return file_doc

	def test_gst_return_log_create_read_update_and_download(self):
		"""IC's own create/read/update/download, unmodified.

		`update_json_for` on an existing field calls `file.save_file(content=…,
		overwrite=True)` and then reads `file.file_url` — with **no** `.save()` after it
		(gst_return_log.py:103). That is the exact shape A2 exists for.
		"""
		log = self._log()

		log.update_json_for("books", {"invoices": ["A"]})
		file_doc = self._track_attachment(log, "books")
		self.assertTrue(
			file_doc.file_url.startswith("/private/files/"),
			f"a GST Return Log attachment must be private and canonical, got {file_doc.file_url!r}",
		)
		self.assertIsNotNone(self.cso_of(file_doc), "the GSTR attachment was never linked to an object")
		self.assertEqual(log.get_json_for("books")["invoices"], ["A"])

		original_cso = frappe.db.get_value("File", file_doc.name, "cloud_storage_object")
		log.update_json_for("books", {"invoices": ["A", "B"]})

		self.assertEqual(
			frappe.db.get_value("GST Return Log", log.name, "books"),
			frappe.db.get_value("File", file_doc.name, "file_url"),
			"the parent field and the File row disagree after the overwrite",
		)
		self.assertNotEqual(
			frappe.db.get_value("File", file_doc.name, "cloud_storage_object"),
			original_cso,
			"the overwrite was never persisted (A2): the File row still points at the old object",
		)
		self.assertEqual(log.get_json_for("books")["invoices"], ["A", "B"])

		# `download_file` is IC's whitelisted endpoint; it is the only caller in the whole
		# ecosystem of `get_content(encodings=[])`, the compatibility vanilla v15 lacks.
		from india_compliance.gst_india.doctype.gst_return_log.gst_return_log import download_file

		previous_form_dict, previous_response = frappe.local.form_dict, frappe.local.response
		frappe.local.response = frappe._dict()
		frappe.local.form_dict = frappe._dict(
			doctype=log.doctype, name=log.name, file_field="books", file_name="books.json.gz"
		)
		try:
			download_file()
			content = frappe.local.response["filecontent"]
			self.assertEqual(frappe.local.response["type"], "download")
		finally:
			frappe.local.form_dict, frappe.local.response = previous_form_dict, previous_response

		self.assertIsInstance(content, bytes, "get_content(encodings=[]) decoded the gzip stream")
		self.assertEqual(json.loads(gzip.decompress(content).decode())["invoices"], ["A", "B"])

	def test_get_content_with_an_empty_encoding_list_returns_raw_bytes(self):
		"""Contract #2 against a real gzip payload: `encodings=[]` must never decode."""
		log = self._log()
		log.update_json_for("books", {"invoices": ["RAW"]})
		file_doc = self._track_attachment(log, "books")

		reloaded = frappe.get_doc("File", file_doc.name)
		raw = reloaded.get_content(encodings=[])

		self.assertIsInstance(raw, bytes)
		self.assertEqual(json.loads(gzip.decompress(raw).decode())["invoices"], ["RAW"])

	def test_the_e_waybill_save_file_path(self):
		"""`e_waybill.py:741` — `save_file(name, pdf, doctype, docname, is_private=1)`.

		The legacy `write_file` convention: `file_manager.save_file` calls the hook before
		any File row exists, so the CSO can only be found through the `frappe.local` stash
		(A1's fourth arm).
		"""
		from frappe.utils.file_manager import save_file

		log = self._log()
		pdf = b"%PDF-1.4\ncfs ecosystem e-waybill\n%%EOF\n"

		file_doc = save_file("e-waybill-cfs.pdf", pdf, log.doctype, log.name, is_private=1)
		self.track(file_doc)

		self.assertTrue(
			file_doc.file_url.startswith("/private/files/"),
			f"the e-Waybill PDF must stay private and canonical, got {file_doc.file_url!r}",
		)
		self.assertIsNotNone(self.cso_of(file_doc), "the e-Waybill PDF was never linked to an object")
		self.assertEqual(file_doc.content_hash, get_content_hash(pdf))
		self.assertEqual(frappe.get_doc("File", file_doc.name).get_content(encodings=[]), pdf)

	def test_the_import_bound_get_file_path_is_patched_on_the_real_module(self):
		"""A36's second half, against the installed India Compliance rather than a stub."""
		import india_compliance.gst_india.utils as ic_utils
		from frappe.utils.file_manager import get_file_path as core_get_file_path

		# The precondition below is order-dependent: `runtime_patches._state` is per PROCESS,
		# so "is IC patched right now" depends on what ran before. It holds today because
		# `EcosystemTestCase.setUp` registers `runtime_patches.uninstall` as a cleanup for
		# every test in this module, and it is asserted rather than assumed so a module that
		# leaves the patch installed fails HERE, naming the cause, instead of making the
		# `assertIs` below pass for the wrong reason. A `runtime_patches.uninstall()` on this
		# line would make it unconditional; it is left out only because this session could not
		# re-run the suite to verify the change.
		self.assertIsNot(
			ic_utils.get_file_path,
			runtime_patches.get_file_path_cloud,
			"precondition: IC must start out holding its own import-bound copy",
		)

		runtime_patches.ensure_installed()

		self.assertIs(
			ic_utils.get_file_path,
			runtime_patches.get_file_path_cloud,
			"India Compliance's import-bound copy was not patched (A36)",
		)

		runtime_patches.uninstall()
		self.assertIs(ic_utils.get_file_path, core_get_file_path)

	def test_upload_gstr_after_cleanup(self):
		"""The A36 bypass: IC ingests a GSTR JSON through `get_file_path`, not `get_content`.

		After migration CLEANUP there are no local bytes at all, so core's implementation
		hands `frappe.get_file_json` a path that does not exist. The whole ingest fails with
		a `FileNotFoundError` that looks like data loss.
		"""
		import india_compliance.gst_india.utils as ic_utils

		payload = {"data": {"rtnprd": "042026", "docdata": {"b2b": []}}}
		file_doc = self.make_file(file_name="gstr2b-cfs.json", content=json.dumps(payload), is_private=1)
		frappe.db.commit()

		# What CLEANUP leaves behind: the object, the File row, and nothing on local disk.
		local = self.local_path(file_doc)
		self.assertFalse(local and os.path.exists(local), "precondition: no local copy under S3_ONLY")
		materialize.forget(file_doc.name)

		runtime_patches.ensure_installed()
		self.assertIs(ic_utils.get_file_path, runtime_patches.get_file_path_cloud)

		ingested = ic_utils.get_json_from_file(file_doc.file_url)

		self.assertEqual(ingested["data"]["rtnprd"], "042026")

	def test_upload_gstr_after_cleanup_through_the_reconciliation_tool(self):
		"""The same bypass through IC's real entry point rather than the helper."""
		payload = {"data": {"rtnprd": "052026", "docdata": {"b2b": []}}}
		file_doc = self.make_file(file_name="gstr2b-tool-cfs.json", content=json.dumps(payload), is_private=1)
		frappe.db.commit()
		materialize.forget(file_doc.name)

		runtime_patches.ensure_installed()

		tool = frappe.get_doc("Purchase Reconciliation Tool")
		from india_compliance.gst_india.utils.gstr_utils import ReturnType

		period = tool.get_return_period_from_file(ReturnType.GSTR2B.value, file_doc.file_url)

		# `get_return_period_from_file` swallows every exception and returns None, so the
		# assertion has to be on the value: a broken A36 patch reads as `None` here.
		self.assertEqual(period, "052026", "IC could not read the GSTR file after cleanup")
