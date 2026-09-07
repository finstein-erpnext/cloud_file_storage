"""Batch assignment (design §4).

Re-runnable: it only touches objects with no batch, so a crash mid-plan is repaired by
running it again, and a campaign that gains objects later can be re-planned without
disturbing the batches already in flight.
"""

import frappe
from frappe.query_builder.functions import Max
from frappe.utils import cint

from cloud_file_storage.migration import audit

CAMPAIGN_DOCTYPE = "Cloud Migration Campaign"
BATCH_DOCTYPE = "Cloud Migration Batch"
OBJECT_DOCTYPE = "Cloud Migration Object"


def plan_campaign(campaign: str, *, batch_size: int | None = None) -> dict:
	"""Assign every unbatched Pending object to a batch, sources before thumbnails.

	**Ordering.** Design §4 asks for `(is_private, disk_path)` so a batch reads one area of
	the disk at a time. That ordering cannot be produced without sorting an unindexed column
	on every page, which at 1.2M rows turns an O(n) plan into an O(n²/batch_size) one — and
	the locality it buys is small at an 83KB average, where the work is request-rate bound
	rather than IO bound (design §12). So the keyset runs on the primary key, and the one
	ordering property that is *correctness* rather than performance is kept explicitly:
	thumbnails are planned after everything else, because a thumbnail's derived object is
	keyed off its source's content hash and cannot be built before the source is uploaded.
	"""
	created, assigned = plan_unbatched(campaign, batch_size=batch_size)

	frappe.db.set_value(
		CAMPAIGN_DOCTYPE, campaign, {"status": "Planned", "active_phase": None}, update_modified=False
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)
	audit.record("campaign_transition", campaign=campaign, to="Planned", batches=created, objects=assigned)
	return {"batches": created, "objects": assigned}


def plan_unbatched(campaign: str, *, batch_size: int | None = None) -> tuple[int, int]:
	"""Batch every unbatched Pending object. Returns `(batches, objects)`.

	Split out of :func:`plan_campaign` because it is also needed **while a campaign is
	running**: an object that was in Conflict at plan time has no batch, so when an operator
	resolves the conflict and it returns to Pending there is nothing for the dispatcher to
	pick it up in — it would sit Pending for ever, and the campaign could never complete.
	The conflict actions call this; unlike `plan_campaign` it does not touch the campaign's
	status, which would send a Running campaign back to Planned.
	"""
	camp = frappe.get_doc(CAMPAIGN_DOCTYPE, campaign)
	size = cint(batch_size or camp.batch_size) or 1000

	# `max(batch_no)` as a string is rejected by v16's SELECT parser
	# (frappe/database/query.py:1881-1897); the query builder means one expression that is
	# correct on both branches.
	batch = frappe.qb.DocType(BATCH_DOCTYPE)
	highest = (frappe.qb.from_(batch).select(Max(batch.batch_no)).where(batch.campaign == campaign)).run()
	next_no = cint((highest[0][0] if highest and highest[0] else 0) or 0)
	created = 0
	assigned = 0

	for is_thumbnail in (0, 1):
		cursor = ""
		while True:
			page = frappe.db.get_all(
				OBJECT_DOCTYPE,
				filters={
					"campaign": campaign,
					"status": "Pending",
					"batch": ("is", "not set"),
					"is_thumbnail": is_thumbnail,
					"name": (">", cursor),
				},
				fields=["name", "size_bytes"],
				order_by="name asc",
				limit_page_length=size,
			)
			if not page:
				break

			next_no += 1
			batch = frappe.new_doc(BATCH_DOCTYPE)
			batch.update(
				{
					"campaign": campaign,
					"batch_no": next_no,
					"status": "Pending",
					"phase": "UPLOAD",
					"object_count": len(page),
					"bytes_total": sum(cint(row.size_bytes) for row in page),
				}
			)
			batch.insert(ignore_permissions=True)

			frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- interpolates only closed-set identifiers (module constants / _file_columns()); every value is parameter-bound
				f"UPDATE `tab{OBJECT_DOCTYPE}` SET batch=%(batch)s WHERE name IN %(names)s",
				{"batch": batch.name, "names": tuple(row.name for row in page)},
			)
			frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)

			created += 1
			assigned += len(page)
			cursor = page[-1].name

	return created, assigned


def unplanned_object_count(campaign: str) -> int:
	return frappe.db.count(
		OBJECT_DOCTYPE, {"campaign": campaign, "status": "Pending", "batch": ("is", "not set")}
	)
