"""Shared fixtures for the migration suites.

Two things every migration test needs and neither of which is worth repeating:

* a **pre-migration site** — File rows whose bytes are on local disk with no Cloud Storage
  Object, which is what LOCAL_ONLY produces;
* an **inline queue** — `frappe.enqueue` executed in-process, with every call recorded. The
  recording is not a convenience: "bounded RQ job count, no job explosion" (F3) is an
  assertion about how many jobs a campaign enqueues, and it is made here at unit scale and
  again against real RQ in the rehearsal.

Everything a test creates is removed afterwards, including the campaign's snapshot rows and
its audit trail, so a suite leaves the site as it found it.
"""

import os
from contextlib import contextmanager
from unittest.mock import patch

import frappe

from cloud_file_storage.migration import analyzer, api, engine, planner, report, verify
from cloud_file_storage.tests.utils import CloudStorageTestCase, set_mode

CAMPAIGN_DOCTYPE = "Cloud Migration Campaign"
OBJECT_DOCTYPE = "Cloud Migration Object"
REF_DOCTYPE = "Cloud Migration File Ref"
BATCH_DOCTYPE = "Cloud Migration Batch"
CONFLICT_DOCTYPE = "Cloud Migration Conflict"
AUDIT_DOCTYPE = "Cloud Storage Audit Log"


class RecordingQueue:
	"""Runs enqueued work in-process and remembers every call.

	`frappe.enqueue` in a test would otherwise reach the real RQ, where `cloud_migration` is
	not a configured queue on this bench — and mocking it away without recording would lose
	the only evidence a unit test can offer about job counts and queue targeting.
	"""

	def __init__(self):
		self.calls: list[dict] = []
		self._patcher = None

	def start(self):
		self._patcher = patch("frappe.enqueue", self)
		self._patcher.start()

	def stop(self):
		if self._patcher:
			self._patcher.stop()
			self._patcher = None

	def __call__(self, method, **kwargs):
		queue = kwargs.pop("queue", None)
		job_id = kwargs.pop("job_id", None)
		for reserved in ("timeout", "deduplicate", "now", "enqueue_after_commit", "at_front", "is_async"):
			kwargs.pop(reserved, None)
		self.calls.append({"method": method, "queue": queue, "job_id": job_id, "kwargs": dict(kwargs)})
		return frappe.call(method, **kwargs)

	@property
	def job_ids(self) -> list[str]:
		return [call["job_id"] for call in self.calls]

	def methods(self, suffix: str) -> list[dict]:
		return [call for call in self.calls if str(call["method"]).endswith(suffix)]


class MigrationTestCase(CloudStorageTestCase):
	"""A campaign, a recording queue, and a site that starts out un-migrated."""

	MODE = "LOCAL_ONLY"

	def setUp(self):
		super().setUp()
		self.queue = RecordingQueue()
		self.queue.start()
		self.addCleanup(self.queue.stop)

		self._campaigns: list[str] = []
		self.addCleanup(self._drop_campaigns)

		report.reset_publish_throttle()
		# The engine refuses to enqueue without the dedicated queue unless the campaign was
		# explicitly started with the fallback. Tests are about the engine, not the bench's
		# worker configuration, so the availability probe is answered True here and the
		# refusal itself is tested directly in `test_migration_queue_guard`.
		queue_patch = patch(
			"cloud_file_storage.migration.engine.migration_queue_available", return_value=True
		)
		queue_patch.start()
		self.addCleanup(queue_patch.stop)

	# --- fixtures ------------------------------------------------------------------

	def local_file(self, *, file_name, content=b"migration bytes", is_private=1, **extra):
		"""A File row with bytes on disk and no cloud object — the pre-migration state."""
		doc = self.make_file(file_name=file_name, content=content, is_private=is_private, **extra)
		self.assertIsNone(
			frappe.db.get_value("File", doc.name, "cloud_storage_object"),
			"the fixture already has a cloud object; it would prove nothing about migrating",
		)
		return doc

	def standalone_copy(self, source, *, file_name):
		"""A File row with its OWN url holding byte-identical content.

		Written directly rather than through `save_file`, because core dedups on
		`content_hash` + `is_private` and would hand back the first row's `file_url` — which
		is the behaviour that makes "two URLs, identical bytes" impossible to build any other
		way, and exactly the case UPLOAD's dedup path exists for.
		"""
		content = open(source._canonical_local_path(), "rb").read()
		url = f"/private/files/{file_name}" if source.is_private else f"/files/{file_name}"
		path = frappe.get_site_path("private" if source.is_private else "public", "files", file_name)
		with open(path, "wb") as handle:
			handle.write(content)
		self.addCleanup(lambda: os.path.exists(path) and os.remove(path))

		doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": file_name,
				"file_url": url,
				"is_private": source.is_private,
			}
		)
		# Core's own escape hatch. Without it `validate_duplicate_entry` rewrites `file_url` to
		# the first row's, and the fixture silently becomes a shared-URL case instead of the
		# two-URLs-one-content case under test.
		doc.flags.ignore_duplicate_entry_error = True
		doc.insert(ignore_permissions=True)

		# **There are TWO rewrite sites and only one of them has a flag.** `save_file` dedups
		# again on the insert path, gated by `ignore_existing_file_check` — a *parameter*, and
		# `before_insert` calls `save_file()` without it, so that door cannot be shut from here.
		#
		# What spares the fixture on a modern frappe is a nesting that does not exist at the
		# declared floor:
		#
		#   v15.93.0   if file_doc.exists_on_disk():
		#                  if self.exists_on_disk():
		#                      if not self.file_url:      <- caller's URL preserved
		#   v15.16.0   if file_doc.exists_on_disk():
		#                  self.file_url = duplicate_file.file_url   <- clobbered, always
		#
		# So on v15.16.0 the fixture's URL was overwritten and the assertion below failed. The
		# fixture had been relying on a core implementation detail it never named. Repair the
		# row rather than depend on either shape; the bytes are already on disk above, so the
		# row is coherent either way.
		if doc.file_url != url:
			frappe.db.set_value("File", doc.name, "file_url", url, update_modified=False)
			doc.reload()

		self.assertEqual(doc.file_url, url, "the fixture's own URL could not be restored")
		self.track(doc)
		return doc

	def url_sibling(self, source, *, file_name=None):
		"""A second File row sharing one `file_url` — the supported core pattern (ADR-M3)."""
		doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": file_name or source.file_name,
				"file_url": source.file_url,
				"is_private": source.is_private,
			}
		).insert(ignore_permissions=True)
		self.track(doc)
		return doc

	def campaign(self, **policy) -> str:
		"""A campaign, with the destructive fields applied after creation.

		`create_campaign` refuses `cleanup_mode` and `quarantine_ttl_days` (P7-H1): they
		decide whether local bytes survive, and the endpoint is reachable by the operator
		role, so a campaign could otherwise arrive pre-set to Direct Delete for a System
		Manager to approve without ever being shown the choice. In production the mode is
		chosen at `start_cleanup(direct_delete=True)`, which is manager-gated and audited.
		A fixture that needs the field set writes it directly here, which is what the
		Campaign form does for a System Manager — and keeps these tests exercising cleanup
		rather than the creation gate, which has its own tests.
		"""
		reserved = {field: policy.pop(field) for field in api.RESERVED_POLICY_FIELDS if field in policy}
		name = api.create_campaign(policy.pop("title", "P5 test campaign"), **policy)
		self._campaigns.append(name)

		if reserved:
			frappe.db.set_value(CAMPAIGN_DOCTYPE, name, reserved, update_modified=False)
			frappe.db.commit()
		return name

	# --- driving the pipeline ------------------------------------------------------

	def analyze(self, campaign: str) -> dict:
		return analyzer.run_analysis(campaign)

	def plan(self, campaign: str, **kwargs) -> dict:
		return planner.plan_campaign(campaign, **kwargs)

	def object_for(self, campaign: str, url: str, *, kind: str | None = None):
		"""The migration object row for a URL, by A33 name — no searching, no ambiguity."""
		if kind is None:
			kind = "local_private" if url.startswith("/private/files/") else "local_public"
		name = engine.object_name_for(campaign, f"{kind}::{url}")
		return frappe.db.get_value(OBJECT_DOCTYPE, name, "*", as_dict=True)

	def drain(self, campaign: str, *, max_rounds: int = 40) -> int:
		"""Run batches until the campaign stops producing work. Returns the rounds used."""
		for rounds in range(1, max_rounds + 1):
			dispatched = engine.dispatch_batches(campaign)
			if not dispatched:
				return rounds
		self.fail(f"campaign {campaign} did not settle in {max_rounds} rounds")
		return max_rounds

	def upload_only(self, campaign: str, *, mode: str = "S3_PRIMARY_LOCAL_FALLBACK") -> int:
		"""Run the UPLOAD pass and stop there.

		`start_migration` dispatches immediately and the inline queue then chains straight
		through verification, so a test that wants to tamper with the bucket *between* upload
		and verify has to drive the two passes itself. `chain_next` is suppressed for the same
		reason.
		"""
		set_mode(mode)
		frappe.db.set_value(
			CAMPAIGN_DOCTYPE,
			campaign,
			{"status": "Running", "active_phase": "UPLOAD", "control_flag": None},
			update_modified=False,
		)
		frappe.db.commit()

		ran = 0
		with patch("cloud_file_storage.migration.engine.chain_next", return_value=0):
			for batch in frappe.db.get_all(
				BATCH_DOCTYPE,
				filters={"campaign": campaign, "status": "Pending"},
				pluck="name",
				order_by="batch_no asc",
				limit_page_length=0,
			):
				if engine.cas_batch(batch, expected="Pending", to="UploadDispatched"):
					engine.run_upload_batch(campaign, batch)
					ran += 1
		return ran

	def verify_only(self, campaign: str) -> int:
		"""Run the VERIFY pass over whatever is ready for it."""
		ran = 0
		with patch("cloud_file_storage.migration.engine.chain_next", return_value=0):
			for batch in frappe.db.get_all(
				BATCH_DOCTYPE,
				filters={"campaign": campaign, "status": "Uploaded"},
				pluck="name",
				order_by="batch_no asc",
				limit_page_length=0,
			):
				if engine.cas_batch(batch, expected="Uploaded", to="VerifyDispatched"):
					verify.run_verify_batch(campaign, batch)
					ran += 1
		return ran

	def migrate(self, campaign: str, *, mode: str = "S3_PRIMARY_LOCAL_FALLBACK") -> dict:
		"""analyze → plan → start → drain, the whole happy path."""
		self.analyze(campaign)
		self.plan(campaign)
		set_mode(mode)
		api.start_migration(campaign, skip_preflight=True)
		self.drain(campaign)
		return report.convergence(campaign)

	def fixture_convergence(self, campaign: str, file_docs) -> dict:
		"""Convergence over the objects THIS test made.

		The scratch site carries File rows from every other suite that has ever run on it, so
		a campaign-wide ratio here would measure that residue rather than the code under
		test. The campaign-wide figure is what the 100k rehearsal reports, on a corpus it
		controls.
		"""
		verified = 0
		for doc in file_docs:
			obj = self.object_for(campaign, doc.file_url)
			self.assertIsNotNone(obj, f"no migration object for {doc.file_url}")
			if obj.status in ("Verified", "CleanupEligible", "Quarantined", "CleanedUp"):
				verified += 1
		return {"in_scope": len(file_docs), "verified": verified}

	def statuses(self, campaign: str) -> dict:
		rows = frappe.db.get_all(
			OBJECT_DOCTYPE, filters={"campaign": campaign}, fields=["status", "name"], limit_page_length=0
		)
		counts: dict[str, int] = {}
		for row in rows:
			counts[row.status] = counts.get(row.status, 0) + 1
		return counts

	def conflicts_of(self, campaign: str, *, conflict_type: str | None = None) -> list[dict]:
		filters = {"campaign": campaign}
		if conflict_type:
			filters["conflict_type"] = conflict_type
		return frappe.db.get_all(
			CONFLICT_DOCTYPE,
			filters=filters,
			fields=["name", "conflict_type", "severity", "status", "migration_object", "details"],
			limit_page_length=0,
		)

	def audit_actions(self, campaign: str) -> list[str]:
		return frappe.db.get_all(
			AUDIT_DOCTYPE, filters={"campaign": campaign}, pluck="action", limit_page_length=0
		)

	def link_of(self, file_doc) -> str | None:
		return frappe.db.get_value("File", file_doc.name, "cloud_storage_object")

	def local_exists(self, file_doc) -> bool:
		path = file_doc._canonical_local_path()
		return bool(path and os.path.exists(path))

	# --- teardown ------------------------------------------------------------------

	def tearDown(self):
		"""Remove the rows that hold a Link to a File before the File rows go.

		`addCleanup` is LIFO, so anything registered in `setUp` runs *after* the base case
		deletes the test's File rows — and an audit row or an alias pointing at a deleted
		File makes frappe refuse the delete with `LinkExistsError`. Both tables are written
		by this engine on paths that have no campaign of their own (a bare
		`aliases.rewrite_file_url`), so campaign-scoped cleanup does not reach them.
		"""
		frappe.set_user("Administrator")
		for name in frappe.get_all("Cloud File URL Alias", pluck="name"):
			frappe.delete_doc("Cloud File URL Alias", name, force=True, ignore_permissions=True)
		frappe.db.delete(AUDIT_DOCTYPE)
		frappe.db.commit()
		super().tearDown()

	def _drop_campaigns(self):
		frappe.set_user("Administrator")
		for campaign in self._campaigns:
			for doctype in (REF_DOCTYPE, OBJECT_DOCTYPE, BATCH_DOCTYPE, CONFLICT_DOCTYPE, AUDIT_DOCTYPE):
				frappe.db.delete(doctype, {"campaign": campaign})
			frappe.db.delete(CAMPAIGN_DOCTYPE, {"name": campaign})
		frappe.db.delete(AUDIT_DOCTYPE, {"campaign": ("is", "not set")})
		frappe.db.commit()
		self._campaigns = []


@contextmanager
def approved_cleanup(campaign: str, *, note: str = "test approval"):
	"""Approve cleanup without going through the blocker check, for cleanup-focused tests."""
	from frappe.utils import now_datetime

	frappe.db.set_value(
		CAMPAIGN_DOCTYPE,
		campaign,
		{
			"cleanup_approved_by": "Administrator",
			"cleanup_approved_at": now_datetime(),
			"cleanup_approval_note": note,
		},
		update_modified=False,
	)
	frappe.db.commit()
	try:
		yield
	finally:
		frappe.db.set_value(
			CAMPAIGN_DOCTYPE,
			campaign,
			{"cleanup_approved_by": None, "cleanup_approved_at": None},
			update_modified=False,
		)
		frappe.db.commit()


def object_is_fully_linked(migration_object: str) -> bool:
	return verify.object_is_fully_linked(migration_object)
