"""The triage queue and its four operator actions (design §7).

Every action is role-gated, audited, and re-derives its preconditions server-side — the
Desk buttons and the CLI both arrive here, so a validation that lived in the UI would be
one the CLI does not have.

`relink` is the only flow in this engine that renames a healthy local URL, and it does so
by **copying** bytes, never moving them: the File row that keeps the original URL keeps its
original bytes, and each other row gets its own physical copy under a fresh canonical name.
A move would leave whichever row lost the coin-toss pointing at bytes that are no longer
there.
"""

import os
import shutil

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from cloud_file_storage.migration import aliases, analyzer, audit, names

CONFLICT_DOCTYPE = "Cloud Migration Conflict"
OBJECT_DOCTYPE = "Cloud Migration Object"
REF_DOCTYPE = "Cloud Migration File Ref"


def _conflict(name: str):
	return frappe.get_doc(CONFLICT_DOCTYPE, name)


def _close(conflict, status: str, action: str, note: str | None = None):
	frappe.db.set_value(
		CONFLICT_DOCTYPE,
		conflict.name,
		{
			"status": status,
			"resolution_action": action,
			"resolved_by": frappe.session.user,
			"resolved_at": now_datetime(),
			"details": conflict.details
			if not note
			else frappe.as_json({"note": note, "previous": conflict.details}),
		},
		update_modified=False,
	)
	audit.record(
		"conflict_action",
		campaign=conflict.campaign,
		migration_object=conflict.migration_object,
		action=action,
		conflict=conflict.name,
		note=note,
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)


def retry(conflict_name: str) -> dict:
	"""Put the object back in the queue with a clean attempt counter."""
	frappe.only_for("System Manager")
	conflict = _conflict(conflict_name)

	if conflict.migration_object:
		frappe.db.set_value(
			OBJECT_DOCTYPE,
			conflict.migration_object,
			{
				"status": "Pending",
				"attempt_count": 0,
				"next_retry_at": None,
				"error_class": None,
				"last_error": None,
				"skip_reason": None,
			},
			update_modified=False,
		)
		reopen_batch(conflict.migration_object)

	_close(conflict, "Retrying", "retry")
	return {"status": "Retrying"}


def skip(conflict_name: str, note: str | None = None) -> dict:
	"""Leave the file local forever, and say so in the report."""
	frappe.only_for("System Manager")
	conflict = _conflict(conflict_name)

	if conflict.migration_object:
		frappe.db.set_value(
			OBJECT_DOCTYPE,
			conflict.migration_object,
			{"status": "Skipped", "skip_reason": "operator_skipped"},
			update_modified=False,
		)

	_close(conflict, "Skipped", "skip", note)
	return {"status": "Skipped"}


def mark_resolved(conflict_name: str, note: str | None = None) -> dict:
	frappe.only_for("System Manager")
	conflict = _conflict(conflict_name)
	_close(conflict, "Resolved", "mark_resolved", note)
	return {"status": "Resolved"}


def resolve_privacy(conflict_name: str, is_private: int) -> dict:
	"""A19 — the operator declares the true visibility, and only then may the object move.

	Both sides of the disagreement are made consistent: the File rows' `is_private` column
	and the URL prefix. If the URL has to change, it changes through
	`aliases.rewrite_file_url`, so the old URL keeps resolving and the parent field follows.
	"""
	frappe.only_for("System Manager")
	conflict = _conflict(conflict_name)
	if conflict.conflict_type != "ambiguous_privacy":
		frappe.throw(_("resolve_privacy only applies to an ambiguous_privacy conflict."))

	is_private = 1 if cint(is_private) else 0
	obj = frappe.get_doc(OBJECT_DOCTYPE, conflict.migration_object)
	refs = frappe.db.get_all(
		REF_DOCTYPE, filters={"migration_object": obj.name}, fields=["file", "file_url"], limit_page_length=0
	)

	for ref in refs:
		current = frappe.db.get_value("File", ref.file, ["file_url", "is_private"], as_dict=True)
		if not current:
			continue
		wanted_url = _reprefixed(current.file_url, is_private)
		if wanted_url != current.file_url:
			aliases.rewrite_file_url(
				ref.file, current.file_url, wanted_url, reason="privacy_flip", campaign=obj.campaign
			)
		if cint(current.is_private) != is_private:
			frappe.db.set_value("File", ref.file, "is_private", is_private, update_modified=False)

	new_url = _reprefixed(obj.file_url, is_private)
	frappe.db.set_value(
		OBJECT_DOCTYPE,
		obj.name,
		{
			"status": "Pending",
			"privacy_mismatch": 0,
			"is_private": is_private,
			"file_url": new_url,
			"disk_path": analyzer.local_disk_path(analyzer.normalize_url(new_url)),
			"attempt_count": 0,
		},
		update_modified=False,
	)
	reopen_batch(obj.name)
	_close(conflict, "Resolved", f"resolve_privacy(is_private={is_private})")
	return {"status": "Resolved", "is_private": is_private}


def _reprefixed(url: str | None, is_private: int) -> str | None:
	if not url:
		return url
	basename = url.rsplit("/", 1)[-1]
	return f"/private/files/{basename}" if is_private else f"/files/{basename}"


def relink(conflict_name: str, keep_file: str) -> dict:
	"""`conflicting_url` — one File keeps the URL, every other gets its own copy.

	The chosen row is left completely untouched: same URL, same bytes, same parent field.
	Each other row's bytes are **copied** to a new canonical name, its URL is rewritten
	through the alias/parent updater, and it becomes its own migration object so the normal
	pipeline uploads and verifies it. Nothing is deleted, and if the copy fails the original
	is still exactly where it was.
	"""
	frappe.only_for("System Manager")
	conflict = _conflict(conflict_name)
	if conflict.conflict_type != "conflicting_url":
		frappe.throw(_("relink only applies to a conflicting_url conflict."))

	obj = frappe.get_doc(OBJECT_DOCTYPE, conflict.migration_object)
	refs = frappe.db.get_all(
		REF_DOCTYPE,
		filters={"migration_object": obj.name},
		fields=["name", "file", "file_url", "content_hash"],
		limit_page_length=0,
	)
	if not any(ref.file == keep_file for ref in refs):
		frappe.throw(_("{0} is not a reference of this object.").format(keep_file))

	source_path = obj.disk_path
	if not source_path or not os.path.exists(source_path):
		frappe.throw(_("The original file is not on disk; nothing can be copied from it."))
	# `_split_off` does `shutil.copy2(source_path, dirname(source_path)/…)`, so an unconfined
	# path both READS an arbitrary file and WRITES a copy into an arbitrary directory — and the
	# `file_url` derived from it via `relpath` would be a non-canonical `../..` value on a real
	# File row, which is hard invariant 2. Reached from a whitelisted endpoint.
	if not analyzer.confined_to_site_files(source_path):
		frappe.throw(_("The original file is outside this site's file trees and will not be copied."))

	created = []
	for ref in refs:
		if ref.file == keep_file:
			continue
		created.append(_split_off(obj, ref, source_path))

	frappe.db.set_value(
		OBJECT_DOCTYPE,
		obj.name,
		{"status": "Pending", "classification": "healthy_unique", "attempt_count": 0},
		update_modified=False,
	)
	reopen_batch(obj.name)
	_close(conflict, "Resolved", f"relink(keep={keep_file})")
	return {"kept": keep_file, "split_off": created}


def _split_off(obj, ref, source_path: str) -> dict:
	"""Give one File row its own physical copy under a fresh canonical name."""
	is_private = obj.file_url.startswith("/private/files/")
	directory = os.path.dirname(source_path)
	stem, extension = os.path.splitext(os.path.basename(source_path))
	suffix = names.file_ref_name(obj.campaign, ref.file)[:10]
	new_basename = f"{stem}-{suffix}{extension}"
	new_path = os.path.join(directory, new_basename)

	if not os.path.exists(new_path):
		# copy2, never move: the row that keeps the URL must keep its bytes.
		shutil.copy2(source_path, new_path)

	new_url = analyzer.url_for_disk_path(new_path, private=is_private)
	aliases.rewrite_file_url(ref.file, ref.file_url, new_url, reason="relink", campaign=obj.campaign)
	aliases.mark_ref_rewritten(obj.campaign, ref.file)

	identity_kind = "local_private" if is_private else "local_public"
	url_hash = names.identity_hash(f"{identity_kind}::{new_url}")
	new_object = names.object_name(obj.campaign, url_hash)

	if not frappe.db.exists(OBJECT_DOCTYPE, new_object):
		doc = frappe.new_doc(OBJECT_DOCTYPE)
		doc.update(
			{
				"campaign": obj.campaign,
				"url_hash": url_hash,
				"identity_kind": identity_kind,
				"file_url": new_url,
				"classification": "healthy_unique",
				"status": "Pending",
				"is_private": 1 if is_private else 0,
				"on_disk": 1,
				"disk_path": new_path,
				"disk_size": os.path.getsize(new_path),
				"ref_count": 1,
				"primary_file": ref.file,
			}
		)
		doc.insert(ignore_permissions=True)

	frappe.db.set_value(
		REF_DOCTYPE,
		ref.name,
		{"migration_object": new_object, "file_url": new_url, "url_rewritten": 1},
		update_modified=False,
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)
	return {"file": ref.file, "url": new_url, "object": new_object}


def reopen_batch(migration_object: str):
	"""Make sure a reopened object has somewhere to be picked up from.

	Two cases, and the second is the one that bites: an object that was already batched only
	needs its batch to accept work again, but an object that was in **Conflict at plan time
	was never batched at all** — the planner only takes Pending objects. Returning it to
	Pending without giving it a batch leaves it invisible to the dispatcher for ever, and the
	campaign can never complete because it still counts as in flight.
	"""
	campaign, batch = frappe.db.get_value(OBJECT_DOCTYPE, migration_object, ["campaign", "batch"])
	if not batch:
		from cloud_file_storage.migration import planner

		planner.plan_unbatched(campaign)
		return
	status = frappe.db.get_value("Cloud Migration Batch", batch, "status")
	if status in ("Failed", "Verified", "Cleaned", "Uploaded"):
		frappe.db.set_value(
			"Cloud Migration Batch",
			batch,
			{"status": "Pending", "attempts": 0, "last_error": None},
			update_modified=False,
		)


def open_conflicts(campaign: str, *, severity: str | None = None) -> list[dict]:
	filters = {"campaign": campaign, "status": "Open"}
	if severity:
		filters["severity"] = severity
	return frappe.db.get_all(
		CONFLICT_DOCTYPE,
		filters=filters,
		fields=["name", "conflict_type", "severity", "migration_object", "details"],
		order_by="severity asc, creation asc",
		limit_page_length=0,
	)


def blocker_count(campaign: str) -> int:
	return frappe.db.count(CONFLICT_DOCTYPE, {"campaign": campaign, "status": "Open", "severity": "Blocker"})
