"""URL rewrites and the parent-field consistency that has to come with them (design §7).

ADR-M12: healthy legacy URLs are **never** rewritten. This module is reached only by the
three cases where a URL genuinely cannot stay as it is — a conflict relink, a legacy
adoption whose URL 404s after the rename, and a privacy fix — and every one of them goes
through :func:`rewrite_file_url`, which does four things atomically or none of them:

1. records the `Cloud File URL Alias` so the old URL keeps resolving (302, A11);
2. rewrites `tabFile.file_url` (and `thumbnail_url` when it shared the stem);
3. rewrites the **parent business field**, because `file_url` values live in Attach /
   Attach Image fields and in ordinary Data columns that integrations read
   (`gst_return_log.py:87,104`, `stock_ledger.py:308-318`) — a rewritten File row with a
   stale parent field is a broken document, not a migrated one;
4. writes the audit row.

Nothing here uses raw SQL against `tabFile`: `frappe.db.set_value` is the sanctioned write
path outside the three audited raw sites (docs/INVARIANTS.md invariant 5), and the guarded-equality
semantics design §7 asks for are obtained by reading the current value under `for_update`
before writing it.
"""

import frappe

from cloud_file_storage.migration import audit
from cloud_file_storage.serving import aliases as serving_aliases

REF_DOCTYPE = "Cloud Migration File Ref"

#: Field types that legitimately hold a `file_url`. Anything else with a matching value is
#: left alone — a coincidence in a free-text field is not ours to rewrite.
ATTACH_FIELDTYPES = ("Attach", "Attach Image")

#: The `Cloud File URL Alias.reason` Select, shipped by P3. Callers pass one of these and
#: nothing else: the Select is validated on insert, so a reason this app invented would turn
#: a URL rewrite into a `ValidationError` in the middle of the rewrite.
ALIAS_REASONS = ("manual", "conflict", "relink", "adoption", "privacy_flip")


def rewrite_file_url(
	file: str,
	old_url: str,
	new_url: str,
	*,
	reason: str,
	campaign: str | None = None,
	update_parent: bool = True,
) -> dict:
	"""Move one File row to a new URL, keeping every consumer of the old one working."""
	if not file or not old_url or not new_url or old_url == new_url:
		return {"rewritten": False}

	if not new_url.startswith(("/files/", "/private/files/")):
		frappe.throw(f"Refusing to rewrite {file} to a non-canonical URL: {new_url}")
	if reason not in ALIAS_REASONS:
		frappe.throw(f"{reason!r} is not a Cloud File URL Alias reason: {ALIAS_REASONS}")

	current = frappe.db.get_value(
		"File",
		file,
		["file_url", "thumbnail_url", "attached_to_doctype", "attached_to_name", "attached_to_field"],
		as_dict=True,
	)
	if not current:
		return {"rewritten": False}
	if current.file_url != old_url:
		# Guarded equality: the runtime moved this row after we decided to. Its decision is
		# newer than ours, so ours is dropped.
		return {"rewritten": False, "reason": "file_url changed under us"}

	alias = serving_aliases.ensure_alias(
		old_url, new_url, file=file, reason=reason, migration_campaign=campaign
	)

	values = {"file_url": new_url}
	if current.thumbnail_url and current.thumbnail_url.startswith(_stem(old_url)):
		values["thumbnail_url"] = current.thumbnail_url.replace(_stem(old_url), _stem(new_url), 1)

	frappe.db.set_value("File", file, values, update_modified=False)

	parents = (
		_update_parent_field(current, old_url, new_url)
		if update_parent and current.attached_to_doctype
		else []
	)

	audit.record(
		"url_rewrite",
		campaign=campaign,
		file=file,
		old_url=old_url,
		new_url=new_url,
		reason=reason,
		alias=alias,
		parents_updated=parents,
	)

	from cloud_file_storage.core_hooks import invalidate_public_404

	invalidate_public_404(old_url, new_url, values.get("thumbnail_url"))
	return {"rewritten": True, "alias": alias, "parents_updated": parents}


def _stem(url: str) -> str:
	return url.rsplit(".", 1)[0] if "." in url.rsplit("/", 1)[-1] else url


def _update_parent_field(current, old_url: str, new_url: str) -> list[dict]:
	"""Rewrite the Attach/Attach Image field(s) on the parent that hold the old URL.

	Validated through Meta rather than written blind: a File row's `attached_to_field` can
	name a field that no longer exists, or one whose type is not an attachment at all, and
	writing a URL into either would corrupt a business document to fix a storage detail.
	"""
	updated: list[dict] = []
	doctype, name = current.attached_to_doctype, current.attached_to_name
	if not doctype or not name or not frappe.db.exists(doctype, name):
		return updated

	try:
		meta = frappe.get_meta(doctype)
	except Exception:  # noqa: BLE001 - a vanished doctype is not a reason to fail a rewrite
		return updated

	candidates = []
	if current.attached_to_field:
		candidates.append(current.attached_to_field)
	if meta.get("image_field") and meta.get("image_field") not in candidates:
		candidates.append(meta.get("image_field"))

	for fieldname in candidates:
		field = meta.get_field(fieldname)
		if not field or field.fieldtype not in ATTACH_FIELDTYPES:
			continue
		# `for_update` gives the guarded-equality semantics design §7 specifies without a
		# second raw-SQL site: the row is locked, re-read, and written only if unchanged.
		value = frappe.db.get_value(doctype, name, fieldname, for_update=True)
		if value != old_url:
			continue
		frappe.db.set_value(doctype, name, fieldname, new_url, update_modified=False)
		updated.append({"doctype": doctype, "name": name, "field": fieldname})

	return updated


def mark_ref_rewritten(campaign: str, file: str):
	ref = frappe.db.get_value(REF_DOCTYPE, {"campaign": campaign, "file": file}, "name")
	if ref:
		frappe.db.set_value(REF_DOCTYPE, ref, "url_rewritten", 1, update_modified=False)
