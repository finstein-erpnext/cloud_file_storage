"""Three-way diff: bucket ⇄ Cloud Storage Object ⇄ File (design §10).

Four drift classes, and what each one means:

* **orphan_remote** — a key in the bucket with no Cloud Storage Object. Storage cost, and
  possibly a campaign that died between the PUT and its commit. Reported, never deleted:
  this module has no delete path, and physical deletes live only in deferred GC.
* **missing_remote** — a `verified` object whose key is *not* in the bucket. This is the
  critical one: something believes those bytes are safe. It raises an Error Log.
* **size_mismatch** — the object and the key disagree about length.
* **unreferenced_cso** — an object no File row and no live campaign points at. A GC
  candidate, handed to the runtime's own lifecycle rather than acted on here.

Resumable per page: the `ContinuationToken` is persisted on the `Cloud Reconcile Run` row
before the page is processed, so a crash re-reads at most one page of 1000 keys. Drift
detail goes to a JSONL file rather than to rows, because the volume is unbounded.
"""

import json
import os

import frappe
from frappe.utils import cint, now_datetime

from cloud_file_storage.storage import engine as storage_engine
from cloud_file_storage.storage import keys, objects
from cloud_file_storage.storage.modes import get_settings

RUN_DOCTYPE = "Cloud Reconcile Run"
CSO_DOCTYPE = "Cloud Storage Object"
PAGE_SIZE = 1000


def run_reconcile(bucket: str | None = None, prefix: str | None = None, *, run: str | None = None) -> dict:
	"""Walk the bucket, diff it against the object table, and write the drift report."""
	settings = get_settings()
	bucket = bucket or settings.bucket

	doc = frappe.get_doc(RUN_DOCTYPE, run) if run else _new_run(bucket, prefix)
	report_path = _report_path(doc.name)
	counters = {
		"remote_objects": cint(doc.remote_objects),
		"cso_rows": 0,
		"orphan_remote": cint(doc.orphan_remote),
		"missing_remote": 0,
		"size_mismatch": cint(doc.size_mismatch),
		"unreferenced_cso": 0,
	}

	token = doc.continuation_token or None
	try:
		with open(report_path, "a") as report:  # nosemgrep: frappe-security-file-traversal
			while True:
				response = storage_engine.list_objects(
					prefix=prefix, continuation_token=token, page_size=PAGE_SIZE, settings=settings
				)
				contents = response.get("Contents") or []
				counters["remote_objects"] += len(contents)
				_diff_page(contents, counters, report, bucket)

				token = response.get("NextContinuationToken")
				frappe.db.set_value(
					RUN_DOCTYPE,
					doc.name,
					{"continuation_token": token, **_persistable(counters)},
					update_modified=False,
				)
				frappe.db.commit()

				if not response.get("IsTruncated") or not token:
					break

			counters["cso_rows"] = frappe.db.count(CSO_DOCTYPE, {"bucket": bucket})
			counters["missing_remote"] = _find_missing_remote(bucket, prefix, report)
			counters["unreferenced_cso"] = _find_unreferenced(bucket, report)
	except Exception as exc:  # noqa: BLE001 - a failed sweep is recorded, not swallowed
		frappe.db.set_value(
			RUN_DOCTYPE,
			doc.name,
			{"status": "Failed", "last_error": str(exc)[:500], "completed_at": now_datetime()},
			update_modified=False,
		)
		frappe.db.commit()
		raise

	frappe.db.set_value(
		RUN_DOCTYPE,
		doc.name,
		{
			"status": "Completed",
			"completed_at": now_datetime(),
			"continuation_token": None,
			"report_file": report_path,
			**_persistable(counters),
		},
		update_modified=False,
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)

	if counters["missing_remote"]:
		frappe.log_error(
			title="Cloud storage reconcile: objects missing from the bucket",
			message=(
				f"{counters['missing_remote']} Cloud Storage Object row(s) are `verified` but their "
				f"key is not in {bucket}. See {report_path}."
			),
		)

	return {"run": doc.name, "report": report_path, **counters}


def _new_run(bucket: str, prefix: str | None):
	doc = frappe.new_doc(RUN_DOCTYPE)
	doc.update({"status": "Running", "bucket": bucket, "prefix": prefix, "started_at": now_datetime()})
	doc.insert(ignore_permissions=True)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)
	return doc


def _report_path(run: str) -> str:
	directory = frappe.get_site_path("private", "files")
	os.makedirs(directory, exist_ok=True)
	return os.path.join(directory, f"{run}-drift.jsonl")


def _persistable(counters: dict) -> dict:
	return {key: cint(value) for key, value in counters.items()}


def _diff_page(contents: list[dict], counters: dict, report, bucket: str):
	"""Match one page of bucket keys against the object table with a single indexed IN."""
	if not contents:
		return

	by_key = {item["Key"]: item for item in contents}
	rows = frappe.db.get_all(
		CSO_DOCTYPE,
		filters={"s3_key": ("in", list(by_key)), "bucket": bucket},
		fields=["name", "s3_key", "file_size", "status"],
		limit_page_length=0,
	)
	known = {row.s3_key: row for row in rows}

	for key, item in by_key.items():
		row = known.get(key)
		if row is None:
			counters["orphan_remote"] += 1
			_write(
				report,
				{
					"class": "orphan_remote",
					"key": key,
					"size": item.get("Size"),
					"ours": bool(keys.parse_object_key(key)),
				},
			)
			continue
		if cint(row.file_size) and cint(item.get("Size")) != cint(row.file_size):
			counters["size_mismatch"] += 1
			_write(
				report,
				{
					"class": "size_mismatch",
					"key": key,
					"cloud_storage_object": row.name,
					"remote_size": item.get("Size"),
					"recorded_size": cint(row.file_size),
				},
			)


def _find_missing_remote(bucket: str, prefix: str | None, report) -> int:
	"""Every `verified` object whose key the bucket does not have. The critical class."""
	missing = 0
	cursor = ""
	while True:
		page = frappe.db.get_all(
			CSO_DOCTYPE,
			filters={"bucket": bucket, "status": "verified", "name": (">", cursor)},
			fields=["name", "s3_key", "file_size"],
			order_by="name asc",
			limit_page_length=PAGE_SIZE,
		)
		if not page:
			break
		for row in page:
			if prefix and not (row.s3_key or "").startswith(prefix):
				continue
			cso = frappe.db.get_value(
				CSO_DOCTYPE, row.name, ["name", "bucket", "s3_key", "mime_type"], as_dict=True
			)
			if not storage_engine.exists(cso):
				missing += 1
				_write(
					report,
					{"class": "missing_remote", "key": row.s3_key, "cloud_storage_object": row.name},
				)
		cursor = page[-1].name
	return missing


def _find_unreferenced(bucket: str, report) -> int:
	"""Objects nothing points at. Reported for the runtime's GC, never deleted here."""
	unreferenced = 0
	cursor = ""
	while True:
		page = frappe.db.get_all(
			CSO_DOCTYPE,
			filters={
				"bucket": bucket,
				"status": ("in", ("uploaded", "verified", "legacy_unverified")),
				"name": (">", cursor),
			},
			fields=["name", "s3_key"],
			order_by="name asc",
			limit_page_length=PAGE_SIZE,
		)
		if not page:
			break
		for row in page:
			if objects.live_reference_count(row.name, for_update=False):
				continue
			unreferenced += 1
			_write(report, {"class": "unreferenced_cso", "key": row.s3_key, "cloud_storage_object": row.name})
		cursor = page[-1].name
	return unreferenced


def _write(report, payload: dict):
	report.write(json.dumps(payload, default=str, sort_keys=True) + "\n")


def scheduled_reconcile():
	"""Weekly drift check. Opt-in: it lists the whole bucket, which is not free."""
	settings = get_settings()
	if not settings.bucket:
		return {"skipped": "no bucket configured"}
	return run_reconcile()
