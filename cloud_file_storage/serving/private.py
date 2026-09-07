"""Cloud-aware `download_private_file` — the private serving path.

Installed by `runtime_patches.ensure_installed()` as an attribute on
`frappe.utils.response`, because `frappe/app.py` reaches the function through exactly that
module attribute at call time, **after** `validate_auth()`. Both facts are asserted against
the installed frappe by `tests/test_serving_spike.py::TestDispatchMechanismOnRealFrappe`
rather than taken from a design document — that spike is what froze this mechanism.

The gate is core's, unchanged and in core's order:

    Guest? → Forbidden
    find_file_by_url(path, name=form_dict.fid)  → is_downloadable() → has_permission
    not found → the same gate against the row whose `thumbnail_url` is this path (A19:
                a thumbnail is a rendering of its source, so it is served off the source
                row and never by the public renderer)
    still not found → Forbidden    (never NotFound: no existence oracle)
    make_access_log(...)           → EXACTLY ONE row, whichever branch serves
    local copy present             → send_private_file() directly (A24: no second log)
    otherwise                      → 302 to a short-TTL, disposition-pinned presigned GET

The object is resolved through the A1 chain across URL-sharing rows (A35), not through
`file.cloud_storage_object` alone.
"""

import os

import frappe
from frappe import _
from frappe.utils import cint
from werkzeug.exceptions import Forbidden, NotFound
from werkzeug.utils import redirect
from werkzeug.wrappers import Response

from cloud_file_storage.serving import aliases
from cloud_file_storage.serving.disposition import disposition_for
from cloud_file_storage.storage import engine, objects
from cloud_file_storage.storage.exceptions import CloudStorageError, CloudStorageTransportError
from cloud_file_storage.storage.modes import OperationMode, get_mode, get_settings

#: How long a client should wait before retrying a 503. Long enough that a retry storm
#: cannot make an outage worse, short enough that recovery is noticed.
RETRY_AFTER_SECONDS = 30


def download_private_file_cloud(path: str) -> Response:
	"""The patched `frappe.utils.response.download_private_file`."""
	from frappe.core.doctype.access_log.access_log import make_access_log
	from frappe.core.doctype.file.utils import find_file_by_url
	from frappe.utils.response import send_private_file

	original = get_original()

	if not is_active():
		# Not our site, or LOCAL_ONLY: byte-identical core behaviour, including X-Accel.
		return original(path)

	if frappe.session.user == "Guest":
		raise Forbidden(_("You don't have permission to access this file"))

	file = find_file_by_url(path, name=frappe.form_dict.fid)

	if not file:
		# A19/PLAN §A — a thumbnail has no File row of its own (core writes only
		# `thumbnail_url` onto its source), so a private file's thumbnail can only be served
		# off its SOURCE row, behind that row's gate. The public renderer must refuse it, and
		# nginx cannot see it: without this branch the URL is dead, which is precisely what
		# invites someone to "fix" it by pointing it back at `/files/`.
		thumbnail_source = _readable_thumbnail_source(path)
		if thumbnail_source is not None:
			return _serve_thumbnail(thumbnail_source, path)

		# A11 — the alias is consulted at the miss, BEFORE the Forbidden outcome, and the
		# permission gate is re-run against whatever it resolves to.
		aliased = _alias_redirect(path)
		if aliased is not None:
			return aliased
		raise Forbidden(_("You don't have permission to access this file"))

	# Contract #10, and the reason the local branch below calls `send_private_file` rather
	# than delegating to core's `download_private_file`: core would log a second row.
	#
	# Logged HERE, before servability is known, so a 503 or a NotFound below still leaves a
	# row. That is deliberate and it is core's own order (`utils/response.py:272`): the log
	# records *an authorised attempt to read this file*, which is the auditable event, and it
	# is the only placement under which "exactly one row per request" holds on every branch —
	# a log moved after the outcome would need repeating on four of them. Recorded in
	# DECISIONS.md.
	make_access_log(doctype="File", document=file.name, file_type=os.path.splitext(path)[-1][1:])

	local_path = _local_path(file)
	if local_path and os.path.exists(local_path):
		return send_private_file(path.split("/private", 1)[1])

	return _serve_from_cloud(file, path)


def _serve_from_cloud(file, path: str) -> Response:
	return _serve_object(_resolve_cso(file), file.file_name or os.path.basename(path))


def _readable_thumbnail_source(path: str):
	"""The File whose `thumbnail_url` is this path, if the caller may read that File.

	Mirrors `find_file_by_url` (`file/utils.py:430-443`) deliberately: core's
	any-one-readable rule across the matching rows, and one `None` for both "no such
	thumbnail" and "not yours", so this branch cannot be turned into an existence oracle
	(A24). `fid` is not honoured — it pins a row at a `file_url`, and means nothing here.
	"""
	rows = frappe.get_all("File", filters={"thumbnail_url": str(path), "is_folder": 0}, fields="*")
	for row in rows:
		file = frappe.get_doc(doctype="File", **row)
		if file.is_downloadable():
			return file
	return None


def _serve_thumbnail(file, path: str) -> Response:
	"""Serve the derived object behind a permitted row's `thumbnail_url`.

	Same contract as the primary path in every respect that matters: exactly one access log
	(against the source row — the thumbnail has no row of its own), the local copy streamed
	directly when there is one, and the disposition pinned from the THUMBNAIL's own name.
	"""
	from frappe.core.doctype.access_log.access_log import make_access_log
	from frappe.utils.response import send_private_file

	from cloud_file_storage.serving import thumbnails

	make_access_log(doctype="File", document=file.name, file_type=os.path.splitext(path)[-1][1:])

	local_path = thumbnails.local_thumbnail_path(path)
	if local_path and os.path.exists(local_path):
		# The argument is derived from the path that was just proved to exist, not from the
		# URL. `local_thumbnail_path` flattens to `os.path.basename`, while the primary
		# branch's `path.split("/private", 1)[1]` keeps any subdirectory — for a
		# `thumbnail_url` with a directory component the two disagree, and the disagreement
		# would show up as `send_private_file` raising NotFound *after* the existence check
		# passed, i.e. a 404 where the presigned fallback should have run. Core-shaped
		# thumbnail names have no directory component, so this is a coupling being removed
		# rather than a bug being fixed; the point is that the check and the send can no
		# longer be made to disagree by a later change to either.
		return send_private_file(os.path.relpath(local_path, frappe.get_site_path("private")))

	return _serve_object(thumbnails.thumbnail_cso_for(file), os.path.basename(path))


def _serve_object(cso, filename: str) -> Response:
	if cso is None:
		# The caller is permitted, so this is not an oracle: there is genuinely nothing to
		# serve — no local copy and no object.
		raise NotFound

	if not objects.is_servable(cso):
		if cso.status in ("deleted", "orphaned"):
			raise NotFound
		# pending_upload / failed: the bytes exist somewhere (the write path never reports
		# success without durability) but not yet in the bucket. The repair job is what
		# fixes this, so the honest answer is "try again", never 404.
		return _unavailable(f"object {cso.name} is {cso.status}")

	settings = get_settings()
	try:
		signed_url = engine.presign_get(
			cso,
			ttl=cint(settings.private_presign_ttl) or engine.DEFAULT_PRIVATE_PRESIGN_TTL,
			disposition=disposition_for(filename, cso.mime_type, settings),
			filename=filename,
			content_type=cso.mime_type,
			settings=settings,
		)
	except CloudStorageTransportError as exc:
		# A30: the breaker is open, or the endpoint is unreachable. Never a 404 — a 404
		# invites a caller to conclude the file is gone (contract #5's failure mode, one
		# HTTP layer up).
		return _unavailable(str(exc))
	except CloudStorageError:
		raise

	return _redirect(signed_url)


def _alias_redirect(path: str) -> Response | None:
	"""Follow an alias, re-running the permission gate on the resolved target (A11)."""
	from frappe.core.doctype.file.utils import find_file_by_url

	target = aliases.resolve(path)
	if not target:
		return None

	# The gate runs again, on the target, with no `fid`: a `fid` pins a row at the OLD URL
	# and means nothing here. An unreadable target returns None, so the caller falls through
	# to the same Forbidden an unknown URL gets — the alias never reveals that a file exists.
	if not find_file_by_url(target):
		return None

	return _redirect(target, code=aliases.ALIAS_REDIRECT_CODE)


def _redirect(location: str, code: int = 302) -> Response:
	response = redirect(location, code=code)
	# The Location header of a private redirect is a bearer capability for its TTL; a cached
	# 302 would hand it to the next user of a shared cache (PLAN.md §C).
	response.headers["Cache-Control"] = "no-store"
	return response


def _unavailable(reason: str) -> Response:
	frappe.logger("cloud_file_storage").warning(f"private serving unavailable: {reason}")
	response = Response(
		_("This file is temporarily unavailable. Please try again shortly."),
		status=503,
		content_type="text/plain; charset=utf-8",
	)
	response.headers["Retry-After"] = str(RETRY_AFTER_SECONDS)
	response.headers["Cache-Control"] = "no-store"
	return response


def _resolve_cso(file):
	"""A35 — resolve through the A1 chain, not `file.cloud_storage_object` alone."""
	resolver = getattr(file, "_resolve_cso", None)
	if resolver:
		return resolver()
	return objects.get_cso(file.get("cloud_storage_object"))


def _local_path(file) -> str | None:
	resolver = getattr(file, "_canonical_local_path", None)
	return resolver() if resolver else None


def is_active() -> bool:
	"""Whether this site wants cloud-aware private serving at all.

	Multi-site worker processes share one interpreter, so the patched function runs for
	sites that never installed the app. Both checks are cached reads.
	"""
	try:
		if "cloud_file_storage" not in frappe.get_installed_apps():
			return False
		return get_mode() is not OperationMode.LOCAL_ONLY
	except Exception:  # noqa: BLE001 - never let a settings read break file downloads
		frappe.logger("cloud_file_storage").warning("could not determine cloud serving state; using core")
		return False


def get_original():
	"""The frappe implementation captured at patch time (or the live one when unpatched)."""
	from cloud_file_storage.serving import runtime_patches

	return runtime_patches.original_download_private_file()
