"""`Cloud File URL Alias` — the only sanctioned way a stored URL stops resolving (A11).

Healthy legacy URLs are never mass-renamed (PLAN.md §A). When a URL genuinely has to
change — a conflict, a relink, a legacy adoption — the old one keeps working through an
alias, and both serving paths consult it *at the miss*, before deciding Forbidden or
NotFound.

Redirects are **302** and never 301/307: a 301 is cached by browsers and proxies
indefinitely, so a later correction could not be delivered, and a 307 would preserve the
method on a URL that only ever answers GET.
"""

import frappe

from cloud_file_storage.cloud_file_storage.doctype.cloud_file_url_alias.cloud_file_url_alias import (
	hash_url,
)

ALIAS_DOCTYPE = "Cloud File URL Alias"

#: A11 — never 301 (permanently cached, uncorrectable) and never 307 (method-preserving).
ALIAS_REDIRECT_CODE = 302

#: Bound on `old_url → new_url → …` hops, so a cycle cannot spin the request thread.
MAX_HOPS = 4


def resolve(old_url: str | None) -> str | None:
	"""The canonical URL this one now points at, following chained aliases.

	Returns None when there is no alias, or when following one would loop.
	"""
	if not old_url:
		return None

	seen = {old_url}
	current = old_url
	for _hop in range(MAX_HOPS):
		new_url = frappe.db.get_value(ALIAS_DOCTYPE, {"url_hash": hash_url(current)}, "new_url")
		if not new_url or new_url in seen:
			break
		seen.add(new_url)
		current = new_url

	return current if current != old_url else None


def ensure_alias(
	old_url: str,
	new_url: str,
	*,
	file: str | None = None,
	reason: str = "manual",
	note: str | None = None,
	migration_campaign: str | None = None,
) -> str | None:
	"""Idempotently record `old_url → new_url`. Returns the alias name.

	Deterministic naming (`autoname: field:url_hash`) makes a re-run an update rather than a
	duplicate, which is what lets a migration campaign re-drive a batch safely.
	"""
	if not old_url or not new_url or old_url == new_url:
		return None

	name = hash_url(old_url)
	values = {
		"new_url": new_url,
		"file": file,
		"reason": reason,
		"note": note,
		"migration_campaign": migration_campaign,
	}

	if frappe.db.exists(ALIAS_DOCTYPE, name):
		doc = frappe.get_doc(ALIAS_DOCTYPE, name)
		doc.update(values)
		doc.save(ignore_permissions=True)
		return doc.name

	doc = frappe.new_doc(ALIAS_DOCTYPE)
	doc.update({"old_url": old_url, **values})
	doc.insert(ignore_permissions=True)
	return doc.name
