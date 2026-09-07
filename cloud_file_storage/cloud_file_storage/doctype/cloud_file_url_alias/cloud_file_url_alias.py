# Copyright (c) 2026, Finstein and contributors
# For license information, please see license.txt

import hashlib

import frappe
from frappe import _
from frappe.model.document import Document

#: Index on the SHA1 column, created by sync from `unique: 1` and re-asserted by the patch.
URL_HASH_INDEX = "url_hash"


def hash_url(url: str) -> str:
	"""SHA1 of the URL — the indexable stand-in for a 500-char column (A11)."""
	return hashlib.sha1((url or "").encode()).hexdigest()


class CloudFileURLAlias(Document):
	def before_naming(self):
		# `autoname: field:url_hash` resolves the name in `set_new_name`, which runs before
		# `validate` (`document.py:304`), so the hash has to exist by now.
		self.url_hash = hash_url((self.old_url or "").strip())

	def validate(self):
		self.old_url = (self.old_url or "").strip()
		self.new_url = (self.new_url or "").strip()

		if not self.old_url or not self.new_url:
			frappe.throw(_("An alias needs both an old and a new URL."), title=_("Incomplete Alias"))

		if self.old_url == self.new_url:
			frappe.throw(_("An alias cannot point a URL at itself."), title=_("Invalid Alias"))

		# Invariant 2: the destination is a canonical URL and nothing else. An alias is the
		# ONLY sanctioned way a stored URL changes, so it must never launder a non-canonical
		# one back into the site.
		if not self.new_url.startswith(("/files/", "/private/files/")):
			frappe.throw(
				_("An alias must point at a canonical /files/ or /private/files/ URL, not {0}.").format(
					self.new_url
				),
				title=_("Invalid Alias"),
			)

		self.url_hash = hash_url(self.old_url)
