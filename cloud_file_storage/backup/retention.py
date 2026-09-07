"""A27 — the restore window is only real if the bytes are still there.

`restore_window_days` is a promise: "a file deleted up to N days ago can be brought back".
Two things have to hold for that promise to be keepable, and neither is automatic:

* the deferred GC must not have physically deleted the object yet, so
  `object_delete_grace_days >= restore_window_days`;
* an object deleted *outside* this app — an operator with bucket access, a lifecycle rule,
  a rogue script — is only recoverable if the bucket is versioned and the noncurrent
  version has not yet expired, so versioning must be on with
  `NoncurrentVersionExpiration >= restore_window_days`.

The first is validated on save. The second cannot be validated on save (it is a property of
someone else's bucket), so it is a hard-warning step in `test_connection`.
"""

import frappe
from frappe import _
from frappe.utils import cint

VERSIONING_STEP = "attachment_bucket_versioning"


def assert_grace_covers_restore_window(settings):
	"""`object_delete_grace_days >= restore_window_days`, checked on save (A27).

	Shipped default is 30/30 (supersession S1 raised the grace from A12's 7 for exactly
	this reason), so a fresh install satisfies it; this refuses the edit that would break it.
	"""
	grace = cint(settings.object_delete_grace_days)
	window = cint(settings.restore_window_days)
	if window <= 0 or grace >= window:
		return

	frappe.throw(
		_(
			"{0} ({1} days) is shorter than {2} ({3} days). The grace period is the delay "
			"before an unreferenced object is physically deleted, so a shorter grace means "
			"the object is gone before the restore window it promises has expired. Raise the "
			"grace to at least {3} days, or shorten the restore window."
		).format(
			frappe.bold(_("Object Delete Grace Days")),
			grace,
			frappe.bold(_("Restore Window Days")),
			window,
		),
		title=_("Restore Window Not Covered"),
		exc=frappe.ValidationError,
	)


def check_attachment_bucket_versioning(settings=None) -> dict:
	"""Whether the attachment bucket can actually honour the restore window (A27).

	Returns a `test_connection` step: `{step, ok, severity, ...}`. `ok` is False — a hard
	warning, surfaced red — when versioning is off or the noncurrent retention is shorter
	than the window. It is not a `throw`: this app does not own the bucket configuration,
	and refusing to save Settings because someone else's bucket lacks versioning would
	simply lock the operator out of the form that tells them why.
	"""
	from cloud_file_storage.storage import engine
	from cloud_file_storage.storage.modes import get_settings

	settings = settings or get_settings()
	window = cint(settings.restore_window_days)
	step = {"step": VERSIONING_STEP, "restore_window_days": window, "severity": "error"}

	if not (settings.bucket or "").strip():
		return {**step, "ok": False, "error": "no attachment bucket configured"}

	# Both probes go through `storage.engine`, which owns the bounded request-path timeouts
	# (A30) and the breaker. `test_connection` is something an operator waits on, so a hung
	# bucket must degrade in seconds, not hold the request open.
	try:
		versioning = engine.get_bucket_versioning(settings=settings)
	except Exception as exc:  # noqa: BLE001 - the point of the probe is to report it
		return {**step, "ok": False, "error": str(exc)}

	status = versioning.get("Status")
	if status != "Enabled":
		# Held in a local rather than inline in the dict: `string-concat-in-list` blocked CI on
		# this message, reading the wrapped text as mis-typed elements. Stated narrowly because
		# it has to be -- the rule fired at these three sites and not at
		# structurally similar ones elsewhere (`health.py`, `storage/diagnostics.py` nest the
		# same `_()` concatenation in dict literals and are clean). The trigger is narrower
		# than "inside a dict or list literal" and is NOT characterised here -- four attempts
		# to state a general rule for it were each falsified by running the tool. What is
		# measured: hoisting the message out of the literal removes the finding. The scan
		# figures live in docs/security/semgrep-adjudication.md rather than here -- a number
		# restated in a comment is a pointer nothing re-checks.
		# The literal stays inside `_()`, which is what keeps babel able to extract it;
		# hoisting the *string* instead of the *call* would silently drop the msgid.
		message = _(
			"Versioning is not enabled on the attachment bucket {0}. Restore Window Days is "
			"{1}, but without versioning an object deleted outside this app is gone "
			"immediately and cannot be restored at all."
		).format(settings.bucket, window)
		return {
			**step,
			"ok": False,
			"versioning": status or "Disabled",
			"error": message,
		}

	noncurrent_days = None
	try:
		configuration = engine.get_bucket_lifecycle(settings=settings)
	except Exception as exc:  # noqa: BLE001 - reported, never swallowed
		return {**step, "ok": False, "versioning": status, "error": str(exc)}

	# Two rules can both match an object and the EARLIEST expiry is the one that fires, so
	# the minimum is the real noncurrent window, not the first or the largest.
	for rule in configuration.get("Rules") or []:
		if rule.get("Status") != "Enabled":
			continue
		days = cint((rule.get("NoncurrentVersionExpiration") or {}).get("NoncurrentDays"))
		if days:
			noncurrent_days = days if noncurrent_days is None else min(noncurrent_days, days)

	# No NoncurrentVersionExpiration at all means noncurrent versions are kept for ever,
	# which covers any window.
	if noncurrent_days is not None and noncurrent_days < window:
		# Hoisted for the same reason as the versioning message above.
		message = _(
			"The attachment bucket expires noncurrent versions after {0} days, which is "
			"shorter than the {1}-day restore window. Raise NoncurrentVersionExpiration to "
			"at least {1} days."
		).format(noncurrent_days, window)
		return {
			**step,
			"ok": False,
			"versioning": status,
			"noncurrent_days": noncurrent_days,
			"error": message,
		}

	return {
		**step,
		"ok": True,
		"versioning": status,
		"noncurrent_days": noncurrent_days,
		"severity": "info",
	}
