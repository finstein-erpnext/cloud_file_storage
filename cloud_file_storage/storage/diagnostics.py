"""Test Connection — the one Desk button that talks to the bucket for its own sake.

What it proves, in order, and why each step is here rather than assumed:

* **HEAD bucket** — credentials resolve and the bucket exists. Cheapest possible failure.
* **PUT a probe object** — `head_bucket` succeeds for a read-only principal, so an IAM
  policy missing `s3:PutObject` looks healthy until the first attachment upload fails. The
  probe is the only way to find that at configuration time.
* **GET it back** — round-trip integrity, and the encryption the store actually applied.
* **SSE echo** — the store's answer, not the setting. Several S3-compatible providers
  accept a `ServerSideEncryption` header and ignore it; a settings page that reported the
  configured value would be reporting our own input back to us.
* **Clock skew** — SigV4 tolerates 15 minutes. A host whose clock has drifted produces
  `RequestTimeTooSkewed` on every call, and that error names the symptom, not the cause.
* **Bucket versioning** — PLAN §H item 1 adopts "30 days + attachment-bucket versioning
  ON" as the recovery window. Versioning off is what turns an overwrite into data loss, so
  it is reported here rather than discovered during a restore.

**The probe object is written and never deleted, deliberately.** docs/INVARIANTS.md invariant 4
reserves physical S3 deletes for deferred GC (grace + locking recount + tombstone), and a
second delete path in the codebase — even one that only ever touches a key no File row
points at — is exactly the thing that invariant exists to prevent. The key is fixed, so
repeated runs overwrite one small object rather than accumulating them, and the payload
says so.
"""

import datetime

import frappe
from frappe import _
from frappe.utils import cint

from cloud_file_storage.storage import engine, keys
from cloud_file_storage.storage.hashing import digest_bytes
from cloud_file_storage.storage.modes import get_settings

#: Fixed, so N runs leave 1 object. Dot-prefixed to sort out of the way of `pub/`/`prv/`.
PROBE_BASENAME = ".cfs-connection-probe"

#: What the probe writes. Small enough that a slow link does not make the button feel broken.
PROBE_BODY = b"cloud_file_storage connection probe\n"

#: SigV4 rejects a request signed more than 15 minutes from the store's clock. Warning at a
#: third of that leaves an operator time to fix NTP before uploads start failing.
CLOCK_SKEW_WARN_SECONDS = 300
CLOCK_SKEW_FAIL_SECONDS = 900

#: Every check this module can report, in run order. The Desk panel iterates this so a
#: check that was skipped is rendered as skipped rather than silently missing.
CHECK_NAMES = (
	"head_bucket",
	"put_object",
	"get_object",
	"sse_echo",
	"clock_skew",
	"versioning",
	"deprecated_hooks",
)


def probe_key(settings=None) -> str:
	"""`<prefix>/<site>/.cfs-connection-probe` — outside the pub/prv object namespace."""
	settings = settings or get_settings()
	prefix = keys.normalize_key_prefix(settings.key_prefix)
	segments = [segment for segment in (prefix, keys.site_segment(), PROBE_BASENAME) if segment]
	return "/".join(segments)


def _ok(check: str, message: str, **detail) -> dict:
	return {"check": check, "status": "ok", "message": message, **detail}


def _warn(check: str, message: str, **detail) -> dict:
	return {"check": check, "status": "warning", "message": message, **detail}


def _fail(check: str, message: str, **detail) -> dict:
	return {"check": check, "status": "failed", "message": message, **detail}


def test_connection(settings=None) -> dict:
	"""Run every probe, collecting results instead of stopping at the first failure.

	Stopping at the first failure would make an operator fix one thing, re-run, fix the
	next. The checks after a hard failure are reported as skipped rather than green — a
	check that could not run is not a check that passed.
	"""
	settings = settings or get_settings()
	checks: list[dict] = []

	if not settings.bucket:
		checks.append(_fail("head_bucket", _("No bucket is configured.")))
		for check in ("put_object", "get_object", "sse_echo", "clock_skew", "versioning"):
			checks.append(_warn(check, _("Skipped: no bucket is configured.")))
		_check_deprecated_hooks(checks)
		return _summarise(checks, settings)

	if _check_head_bucket(checks, settings):
		put_response = _check_put(checks, settings)
		_check_get(checks, settings)
		_check_sse(checks, settings, put_response)
		_check_clock_skew(checks, put_response)
		_check_versioning(checks, settings)
	else:
		for check in ("put_object", "get_object", "sse_echo", "clock_skew", "versioning"):
			checks.append(_warn(check, _("Skipped: the bucket could not be reached.")))

	_check_deprecated_hooks(checks)
	return _summarise(checks, settings)


def _summarise(checks: list[dict], settings) -> dict:
	statuses = {check["status"] for check in checks}
	if "failed" in statuses:
		overall = "failed"
	elif "warning" in statuses:
		overall = "warning"
	else:
		overall = "ok"

	return {
		"status": overall,
		"bucket": settings.bucket,
		"endpoint_url": settings.endpoint_url or "https://s3.amazonaws.com",
		"region": settings.region,
		"probe_key": probe_key(settings) if settings.bucket else None,
		"probe_object_retained": True,
		"probe_note": _(
			"The probe object is left in place on purpose: physical deletes belong to "
			"deferred garbage collection only. The key is fixed, so re-running this test "
			"overwrites it rather than adding another."
		),
		"checks": checks,
	}


def _check_head_bucket(checks: list[dict], settings) -> bool:
	try:
		engine.head_bucket(settings=settings)
	except Exception as exc:  # noqa: BLE001 - reported to the operator, never swallowed
		checks.append(_fail("head_bucket", _("Bucket not reachable: {0}").format(str(exc))))
		return False
	checks.append(_ok("head_bucket", _("Bucket {0} is reachable.").format(settings.bucket)))
	return True


def probe_descriptor(settings=None):
	"""A Cloud-Storage-Object-shaped stand-in for the probe, never persisted.

	The probe deliberately rides `engine.put_bytes`/`engine.get_bytes` rather than reaching
	for a boto3 client of its own. That is the code path an attachment upload takes, so the
	probe exercises the real ExtraArgs (no `ACL` key, checksummed PUT), the real breaker and
	the real typed errors — a hand-rolled `put_object` would prove that a *different* call
	works. No row is written: a Cloud Storage Object is the identity of bytes some File row
	depends on, and the probe backs nothing.
	"""
	settings = settings or get_settings()
	digest = digest_bytes(PROBE_BODY)
	return frappe._dict(
		{
			"name": None,
			"s3_key": probe_key(settings),
			"bucket": settings.bucket,
			"mime_type": "text/plain",
			"content_sha256": digest.sha256,
			"content_hash_md5": digest.md5,
			"file_size": digest.size,
		}
	)


def _check_put(checks: list[dict], settings) -> dict | None:
	try:
		response = engine.put_bytes(probe_descriptor(settings), PROBE_BODY, settings=settings)
	except Exception as exc:  # noqa: BLE001
		checks.append(
			_fail(
				"put_object",
				_("Cannot write to the bucket ({0}). Attachment uploads will fail.").format(str(exc)),
			)
		)
		return None

	checks.append(_ok("put_object", _("Wrote the probe object.")))
	return response


def _check_get(checks: list[dict], settings) -> None:
	try:
		body = engine.get_bytes(probe_descriptor(settings), settings=settings)
	except Exception as exc:  # noqa: BLE001
		checks.append(_fail("get_object", _("Cannot read back the probe object ({0}).").format(str(exc))))
		return

	if body != PROBE_BODY:
		checks.append(
			_fail(
				"get_object",
				_("The probe object read back {0} byte(s) instead of {1}.").format(
					len(body), len(PROBE_BODY)
				),
			)
		)
		return

	checks.append(_ok("get_object", _("Read the probe object back byte-for-byte.")))


def _check_sse(checks: list[dict], settings, put_response: dict | None) -> None:
	configured = (settings.sse_mode or "").strip()
	if not configured:
		checks.append(
			_ok(
				"sse_echo",
				_("No server-side encryption requested; the store encrypts at rest itself."),
			)
		)
		return

	if put_response is None:
		checks.append(_warn("sse_echo", _("Skipped: the probe object could not be written.")))
		return

	expected = "aws:kms" if configured == "SSE-KMS" else "AES256"
	applied = put_response.get("ServerSideEncryption")

	if applied == expected:
		checks.append(_ok("sse_echo", _("The store applied {0}.").format(applied)))
	elif not applied:
		checks.append(
			_warn(
				"sse_echo",
				_(
					"{0} was requested but the store did not report applying any encryption. "
					"Some S3-compatible providers accept the header and ignore it."
				).format(configured),
			)
		)
	else:
		checks.append(
			_warn(
				"sse_echo",
				_("{0} was requested but the store applied {1}.").format(configured, applied),
			)
		)


def _check_clock_skew(checks: list[dict], put_response: dict | None) -> None:
	remote = response_date(put_response)
	if remote is None:
		checks.append(_warn("clock_skew", _("Skipped: the store returned no Date header.")))
		return

	local = datetime.datetime.now(datetime.timezone.utc)
	skew = abs((local - remote).total_seconds())

	if skew >= CLOCK_SKEW_FAIL_SECONDS:
		checks.append(
			_fail(
				"clock_skew",
				_(
					"This host's clock is {0}s from the object store's. Signed requests are "
					"rejected beyond {1}s — fix NTP on this host."
				).format(int(skew), CLOCK_SKEW_FAIL_SECONDS),
				skew_seconds=int(skew),
			)
		)
	elif skew >= CLOCK_SKEW_WARN_SECONDS:
		checks.append(
			_warn(
				"clock_skew",
				_("This host's clock is {0}s from the object store's; signing fails at {1}s.").format(
					int(skew), CLOCK_SKEW_FAIL_SECONDS
				),
				skew_seconds=int(skew),
			)
		)
	else:
		checks.append(
			_ok(
				"clock_skew",
				_("Clocks agree to within {0}s.").format(int(skew)),
				skew_seconds=int(skew),
			)
		)


def response_date(put_response: dict | None) -> datetime.datetime | None:
	"""The store's own clock, from the HTTP `Date` header botocore preserves."""
	if not put_response:
		return None

	headers = (put_response.get("ResponseMetadata") or {}).get("HTTPHeaders") or {}
	raw = headers.get("date")
	if not raw:
		return None

	try:
		from email.utils import parsedate_to_datetime

		parsed = parsedate_to_datetime(raw)
	except (TypeError, ValueError):
		return None

	if parsed.tzinfo is None:
		parsed = parsed.replace(tzinfo=datetime.timezone.utc)
	return parsed


def _check_versioning(checks: list[dict], settings) -> None:
	"""A27, delegated to `backup.retention` so the rule has ONE implementation.

	P6 owns A27 — versioning enabled AND `NoncurrentVersionExpiration` at least the restore
	window — and P1's `api.compat.test_connection` called that function directly. At the
	P6/P7 merge that endpoint became a projection of `checks`, so taking P7's side of the
	conflict would have kept a step still named `versioning` while silently dropping the
	noncurrent-retention half and downgrading it from error to warning. The rule is run
	here instead, so both entry points get the full comparison.
	"""
	from cloud_file_storage.backup.retention import check_attachment_bucket_versioning

	step = check_attachment_bucket_versioning(settings)
	# The last three are excluded so a future key on the retention step can never
	# silently override the check's own status — which would turn a warning green.
	detail = {k: v for k, v in step.items() if k not in ("step", "ok", "error", "check", "status", "message")}

	if step.get("ok"):
		checks.append(_ok("versioning", _("Bucket versioning covers the restore window."), **detail))
		return

	checks.append(
		_warn(
			"versioning",
			step.get("error") or _("Versioning does not cover the restore window."),
			**detail,
		)
	)


def _check_deprecated_hooks(checks: list[dict]) -> None:
	from cloud_file_storage.api.compat import detect_deprecated_hooks

	findings = detect_deprecated_hooks(log=False)
	if findings:
		checks.append(
			_warn(
				"deprecated_hooks",
				" ".join(finding["message"] for finding in findings),
				findings=findings,
			)
		)
	else:
		checks.append(_ok("deprecated_hooks", _("No app defines the retired s3_key_generator hook.")))


def legacy_steps(payload: dict) -> list[dict]:
	"""The pre-P7 `{step, ok, error}` shape, derived from `checks`.

	`api.compat.test_connection` shipped that shape in P1 and site code may read it, so it
	is projected from the richer payload rather than produced by a second implementation.
	"""
	from cloud_file_storage.backup.retention import VERSIONING_STEP

	#: P1 shipped the A27 step under its own name and site code may read it. P7's `checks`
	#: call it `versioning`; the legacy projection is exactly the place to keep the old name
	#: rather than renaming a published field or emitting the same fact twice.
	LEGACY_NAMES = {"versioning": VERSIONING_STEP}

	steps = []
	for check in payload.get("checks") or []:
		step = {"step": LEGACY_NAMES.get(check["check"], check["check"]), "ok": check["status"] == "ok"}
		if check["status"] != "ok":
			step["error"] = check["message"]
		steps.append(step)
	return steps
