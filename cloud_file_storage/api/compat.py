"""Backwards-compatible HTTP surfaces.

`legacy_generate_file` is the remap target for every
`/api/method/frappe_s3_attachment.controller.generate_file?...` URL written by a 0.2.x
install (`override_whitelisted_methods` in hooks.py). Those URLs live in business fields,
emails and bookmarks; they are never rewritten in `tabFile.file_url` and they keep
resolving here forever (A10).

The key in such a URL is a *fork-era* key, so it is resolved two ways: the deprecated
`File.s3_object_key` locator (A13) and, for anything this runtime wrote, the object's own
`s3_key`.

**A24, the endpoint's authorization contract.** The fork granted access when *any* row
sharing the key was readable and this app's first port answered `DoesNotExistError` for an
unknown key but a permission error for a known one — two distinguishable outcomes, which
makes the endpoint a file-existence oracle for anyone who can guess a key. Every refusal
here is now the same `frappe.PermissionError`, whether the key is unknown, the row is
unreadable, or the object is gone; and the gate is core's own any-one-readable rule
(`find_file_by_url`/`is_downloadable`) applied across the rows sharing the key, narrowed to
one row when `fid` is supplied.
"""

import frappe
from frappe import _
from frappe.utils import cint

from cloud_file_storage.serving.disposition import disposition_for
from cloud_file_storage.storage import engine, objects
from cloud_file_storage.storage.modes import get_settings

DEFAULT_PRESIGN_TTL = 300

#: One message for every refusal. Reusing the string is not cosmetic: a caller must not be
#: able to tell "no such key" from "not yours" (A24).
_REFUSAL = "You don't have permission to access this file"

#: How many rows sharing one legacy key are considered. A fork-era key is per-file; a
#: pathological fan-out must not turn one request into an unbounded permission scan.
MAX_CANDIDATES = 20


@frappe.whitelist()
def legacy_generate_file(key: str | None = None, file_name: str | None = None):
	"""Redirect to a short-lived presigned GET for the object behind ``key``.

	The caller must pass a File row's read gate before any URL is issued. Guests are not
	allowed (no `allow_guest`), and every refusal is indistinguishable.
	"""
	file_doc = _readable_file_for_key(key, fid=frappe.form_dict.get("fid"))
	if file_doc is None:
		_refuse()

	cso = _resolve_cso(file_doc, key)
	if cso is None or not objects.is_servable(cso):
		# The row was readable, so this is not an oracle — but there is still nothing to
		# hand out, and the refusal stays the same shape as every other.
		_refuse()

	from frappe.core.doctype.access_log.access_log import make_access_log

	# The legacy endpoint audits identically to `/private/files/...` (design §11): one row,
	# and only once permission has been granted.
	make_access_log(doctype="File", document=file_doc.name, file_type=_extension(file_doc.file_name))

	settings = get_settings()
	signed_url = engine.presign_get(
		cso,
		ttl=cint(settings.private_presign_ttl) or DEFAULT_PRESIGN_TTL,
		disposition=disposition_for(file_doc.file_name, cso.mime_type, settings),
		filename=file_name or file_doc.file_name,
		content_type=cso.mime_type,
		settings=settings,
	)
	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = signed_url
	return


def _refuse():
	"""The single refusal. Never varies with what the caller asked for."""
	frappe.throw(_(_REFUSAL), frappe.PermissionError, title=_("Not Permitted"))


def _extension(file_name: str | None) -> str:
	import os

	return os.path.splitext(file_name or "")[-1][1:]


def _readable_file_for_key(key: str | None, *, fid: str | None = None):
	"""Core's any-one-readable gate, applied to the rows sharing a legacy key (A24).

	Returns the first File the current user may read, or None — with no way for the caller
	to tell "no candidates" from "no permission" out of the *response*.

	**Accepted residual (recorded in DECISIONS.md):** the two cases are still distinguishable
	by *timing*, because an unknown key does no work while a known one costs up to
	`MAX_CANDIDATES` document loads and permission checks. It is not closed here because the
	only ways to close it are worse: padding every refusal to the same cost means doing
	twenty permission checks for every unknown key (a free amplifier for anyone who wants
	one), and a constant-time gate cannot be built out of `has_permission`, which reads
	share rows, user permissions and the attached document. What the channel discloses is
	also bounded: the caller must already hold a valid fork-era key — an opaque per-file
	string this app never mints and never puts in a URL it writes — and learns only that
	*some* row references it, never whose or what.
	"""
	if not key:
		return None

	names = _candidate_file_names(key)
	if fid:
		names = [name for name in names if name == fid]

	for name in names[:MAX_CANDIDATES]:
		doc = frappe.get_doc("File", name)
		if doc.is_downloadable():
			return doc

	return None


def _candidate_file_names(key: str) -> list[str]:
	"""Every File row this fork-era key could denote, deprecated locator first."""
	names: list[str] = []

	if frappe.db.has_column("File", "s3_object_key"):
		names += frappe.get_all("File", filters={"s3_object_key": key}, pluck="name")

	cso_name = frappe.db.get_value("Cloud Storage Object", {"s3_key": key}, "name")
	if cso_name:
		names += [
			name
			for name in frappe.get_all("File", filters={"cloud_storage_object": cso_name}, pluck="name")
			if name not in names
		]

	return names


def _resolve_cso(file_doc, key: str | None):
	"""A35 — the A1 chain first, then the key itself for rows that predate any link."""
	resolver = getattr(file_doc, "_resolve_cso", None)
	if resolver:
		cso = resolver()
		if cso is not None:
			return cso

	cso_name = frappe.db.get_value("Cloud Storage Object", {"s3_key": key}, "name") if key else None
	return objects.get_cso(cso_name) if cso_name else None


# --- deprecated-hook detection (A24 / rename ADR R5) --------------------------------------

DEPRECATED_HOOKS = ("s3_key_generator",)


def detect_deprecated_hooks(log: bool = True) -> list[dict]:
	"""Report site code hooking surfaces this app deliberately does not honour.

	`s3_key_generator` let a 0.2.x site compute its own object keys from the file name. This
	app's keys are content-addressed and are the object's identity (PLAN.md §A "Identity"):
	a site-supplied key would break dedup, refcounting and GC at once. Running the hook and
	discarding its result would be worse than not running it, so it is not run — and this
	detector makes that loud instead of silent (ADR R5).

	Called from `after_migrate` and from `test_connection`; returns the findings so the
	Settings health panel can render them.
	"""
	findings = []
	for hook in DEPRECATED_HOOKS:
		implementations = frappe.get_hooks(hook) or []
		if not implementations:
			continue
		finding = {
			"hook": hook,
			"implementations": list(implementations),
			"message": _(
				"The {0} hook is not supported by Cloud File Storage. Object keys are "
				"content-addressed and are the object's identity, so a site-supplied key "
				"cannot be honoured. Remove the hook; see docs/adr/amendments-register.md A24."
			).format(hook),
		}
		findings.append(finding)
		if log:
			frappe.log_error(
				title=f"cloud_file_storage: deprecated hook {hook}",
				message=f"{finding['message']} Implementations: {', '.join(finding['implementations'])}",
			)

	return findings


@frappe.whitelist()
def test_connection():
	"""Operator-facing connectivity probe. System Manager only, never returns secrets.

	Kept at this endpoint because P1 shipped it here and site code may call it. The probes
	themselves live in `storage.diagnostics` — P7's Desk button calls the same function
	through `api.admin`, so there is one implementation and two entry points rather than two
	implementations that can drift. `steps` is the pre-P7 shape, projected from `checks`.
	"""
	frappe.only_for("System Manager")

	settings = get_settings()
	if not settings.bucket:
		frappe.throw(_("Configure a bucket first."), title=_("Cloud Storage Not Configured"))

	from cloud_file_storage.health import detect_public_private_residue
	from cloud_file_storage.storage import diagnostics

	payload = diagnostics.test_connection(settings=settings)
	return {
		**payload,
		"steps": diagnostics.legacy_steps(payload),
		"deprecated_hooks": detect_deprecated_hooks(log=False),
		# Not a bucket problem, but this is the operator's health surface and the condition
		# is an unauthenticated disclosure of private files by nginx (see health.py).
		"public_private_residue": detect_public_private_residue(log=False),
	}
