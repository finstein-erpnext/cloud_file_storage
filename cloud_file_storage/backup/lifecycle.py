"""Bucket lifecycle policy for the BACKUP bucket (A20, design §2.3).

Three rules govern everything in this module.

1. **Every generated rule carries `Filter.Prefix`.** An unfiltered rule applies to the
   whole bucket, so one forgotten filter turns "expire backups after a year" into "delete
   everything in this bucket after a year".
2. **Merge, never replace.** `put_bucket_lifecycle_configuration` replaces the entire
   configuration; an operator's own rules would vanish silently. Rules whose ID does not
   start with `cfs-` are carried through untouched, and the ones that would be dropped are
   named in the confirmation dialog before anything is written.
3. **Archive classes never touch the attachment bucket.** `assert_synchronous_retrieval`
   is the guard, and `apply_lifecycle_policy` refuses outright when the two buckets are the
   same one — a live attachment in GLACIER answers a download with 403 InvalidObjectState.
"""

import hashlib
import json

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from cloud_file_storage.backup import settings as backup_settings
from cloud_file_storage.backup.settings import (
	ALL_SLUGS,
	SUBDAILY_SLUGS,
	artifact_prefix,
	get_backup_settings,
)
from cloud_file_storage.storage import client as storage_client
from cloud_file_storage.storage.exceptions import CloudStorageConfigurationError

#: Minimum billed storage duration per archive class. Deleting earlier still costs the
#: full minimum, which is what makes a short retention over an archive transition a pure
#: loss rather than a saving.
GLACIER_MIN_DAYS = {"GLACIER_IR": 90, "GLACIER": 90, "DEEP_ARCHIVE": 180}

#: The live attachment bucket may only use these (PLAN §A, F6). Bound to the storage
#: layer's own tuple so the generator and the uploader cannot drift apart.
SYNC_RETRIEVAL_CLASSES = storage_client.SYNC_RETRIEVAL_STORAGE_CLASSES

#: Rules this app owns. Anything else in the bucket policy is the operator's.
RULE_ID_PREFIX = "cfs-"

#: Typed by the operator into the confirm dialog; checked on the server, never in JS only.
APPLY_CONFIRM_PHRASE = "APPLY LIFECYCLE"

#: S3 bills a 40 KB floor per archived object (8 KB of STANDARD-class metadata plus 32 KB
#: of archive-class index). Below this mean size the overhead dominates.
SMALL_OBJECT_THRESHOLD_BYTES = 128 * 1024
ARCHIVE_OVERHEAD_BYTES = 40 * 1024

#: Artifacts per day, per frequency, used by the volume economics rule.
ARTIFACTS_PER_DAY = {"hourly": 24, "every-6-hours": 4, "daily": 1, "weekly": 1 / 7}
SUBDAILY_VOLUME_WARN_ARTIFACTS = 500


def assert_prefix_is_scoped(settings=None) -> str:
	"""The expanded backup prefix, refusing an empty one. Returns `<prefix>/`.

	An empty prefix is not a narrow scope — it is the **absence** of one wearing the shape of
	one. `Filter: {"Prefix": ""}` matches every object in the bucket, so a `cfs-abort-mpu` or
	`cfs-noncurrent` rule generated from it applies to whatever else lives there. A20 requires
	a prefix on every generated rule and this is where that requirement is enforced, rather
	than in each of the four rule shapes, so a fifth rule added later inherits it.

	`validate_target` also requires a prefix, but only while `enabled` — and the lifecycle
	apply checks neither, so the guard cannot live there alone.
	"""
	settings = settings or get_backup_settings()
	base = backup_settings.expand_prefix(settings.backup_prefix)
	if not base:
		frappe.throw(
			_(
				"Set a {0} before generating a lifecycle policy. With an empty prefix the "
				"generated rules would carry {1}, which matches every object in {2} — "
				"including anything this app did not put there."
			).format(
				frappe.bold(_("Backup Prefix")),
				frappe.bold('Filter: {"Prefix": ""}'),
				frappe.bold(settings.backup_bucket or _("the bucket")),
			),
			title=_("Backup Prefix Required"),
			exc=frappe.ValidationError,
		)
	return f"{base}/"


def assert_every_rule_is_scoped(rules: list[dict]):
	"""Structural backstop: no generated rule leaves here without a non-empty prefix.

	The prefix guard above makes an empty `scoped_base` impossible, and this asserts the
	property the guard exists to protect — so a rule shape added later that forgets its
	`Filter` fails here rather than shipping unscoped and being found by a bucket audit.
	"""
	for rule in rules:
		prefix = (rule.get("Filter") or {}).get("Prefix")
		if not prefix:
			raise CloudStorageConfigurationError(
				f"generated lifecycle rule {rule.get('ID')!r} has no Filter.Prefix; "
				"an unscoped rule applies to the whole bucket (A20)"
			)


def assert_synchronous_retrieval(storage_class: str, context: str):
	"""Refuse an archive class where objects must be readable now.

	Called by `Cloud Storage Settings.validate()` for the attachment bucket's own storage
	class and by `apply_lifecycle_policy` before it will write a transition anywhere near
	it. Archive retrieval takes minutes to 48 hours; an attachment download cannot wait.
	"""
	value = (storage_class or "").strip()
	if value not in SYNC_RETRIEVAL_CLASSES:
		frappe.throw(
			_(
				"{0} cannot use the storage class {1}: objects there are not synchronously "
				"retrievable. Allowed: {2}. Archive classes are for backup artifacts only."
			).format(context, frappe.bold(value or _("(empty)")), ", ".join(SYNC_RETRIEVAL_CLASSES)),
			title=_("Archive Storage Class Refused"),
			exc=frappe.ValidationError,
		)


# ------------------------------------------------------------------ retention groups


def retention_groups(settings=None) -> list[dict]:
	"""One entry per generated prefix: `{slug, prefix, retention_days, per_day}`.

	Sub-daily prefixes get their own retention because they accumulate 24 (or 4) artifacts
	a day; keeping those for a year is three orders of magnitude more objects than keeping
	a year of dailies, which is precisely the case A20 asks the economics validator to see.
	"""
	settings = settings or get_backup_settings()
	subdaily = cint(settings.subdaily_retention_days)
	standard = cint(settings.delete_after_days)

	groups = []
	for slug in ALL_SLUGS:
		is_subdaily = slug in SUBDAILY_SLUGS
		groups.append(
			{
				"slug": slug,
				"prefix": artifact_prefix(settings, slug=slug) + "/",
				"retention_days": subdaily if is_subdaily else standard,
				"per_day": ARTIFACTS_PER_DAY.get(slug, 1),
				"subdaily": is_subdaily,
			}
		)
	return groups


def archives_group(settings, group: dict) -> bool:
	"""Whether an archive transition is generated for this prefix.

	Suppressed when the class is unset, when there is no hot window, or when the artifacts
	expire before the transition would ever fire. Generating a rule that cannot fire would
	make the applied policy claim an archival tier the bucket does not have.
	"""
	archive_class = (settings.archive_storage_class or "").strip()
	hot_days = cint(settings.hot_retention_days)
	if not archive_class or hot_days <= 0:
		return False
	retention = cint(group["retention_days"])
	return retention <= 0 or hot_days < retention


# ------------------------------------------------------------------ policy generation


def build_lifecycle_policy(settings=None) -> dict:
	"""The `LifecycleConfiguration` this app owns. Every rule is prefix-scoped."""
	settings = settings or get_backup_settings()
	archive_class = (settings.archive_storage_class or "").strip()
	hot_days = cint(settings.hot_retention_days)
	scoped_base = assert_prefix_is_scoped(settings)

	rules = []
	for group in retention_groups(settings):
		if archives_group(settings, group):
			rules.append(
				{
					"ID": f"{RULE_ID_PREFIX}archive-{group['slug']}",
					"Status": "Enabled",
					"Filter": {"Prefix": group["prefix"]},
					"Transitions": [{"Days": hot_days, "StorageClass": archive_class}],
				}
			)
		if cint(group["retention_days"]) > 0:
			rules.append(
				{
					"ID": f"{RULE_ID_PREFIX}expire-{group['slug']}",
					"Status": "Enabled",
					"Filter": {"Prefix": group["prefix"]},
					"Expiration": {"Days": cint(group["retention_days"])},
				}
			)

	if cint(settings.noncurrent_retention_days) > 0:
		rules.append(
			{
				"ID": f"{RULE_ID_PREFIX}noncurrent",
				"Status": "Enabled",
				"Filter": {"Prefix": scoped_base},
				"NoncurrentVersionExpiration": {"NoncurrentDays": cint(settings.noncurrent_retention_days)},
			}
		)

	if cint(settings.abort_multipart_days) > 0:
		rules.append(
			{
				"ID": f"{RULE_ID_PREFIX}abort-mpu",
				"Status": "Enabled",
				"Filter": {"Prefix": scoped_base},
				"AbortIncompleteMultipartUpload": {
					"DaysAfterInitiation": cint(settings.abort_multipart_days)
				},
			}
		)

	assert_every_rule_is_scoped(rules)
	return {"Rules": rules}


def policy_hash(policy: dict) -> str:
	return hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()


def is_generated_rule(rule: dict) -> bool:
	"""Ours iff the ID starts with `cfs-`. A rule with no ID is never ours.

	S3 generates an ID for a rule submitted without one, so an ID-less rule in a live
	bucket policy was written by something other than this app and is preserved.
	"""
	return str(rule.get("ID") or "").startswith(RULE_ID_PREFIX)


def merge_rules(current_rules: list[dict], generated_rules: list[dict]) -> dict:
	"""Merge the generated rules into the bucket's current ones (A20).

	Returns `{rules, preserved, dropped, replaced}`:

	* `preserved` — rules this app does not own, carried through byte-for-byte;
	* `dropped` — `cfs-` rules currently in the bucket that the new policy no longer
	  contains. These are what the confirm dialog has to list, because they are the only
	  rules the apply is removing;
	* `replaced` — `cfs-` rules present in both, i.e. updated in place.
	"""
	current_rules = list(current_rules or [])
	generated_rules = list(generated_rules or [])

	preserved = [rule for rule in current_rules if not is_generated_rule(rule)]
	current_ours = {rule["ID"]: rule for rule in current_rules if is_generated_rule(rule)}
	generated_ids = {rule["ID"] for rule in generated_rules}

	dropped = [rule for rule_id, rule in current_ours.items() if rule_id not in generated_ids]
	replaced = [rule for rule_id, rule in current_ours.items() if rule_id in generated_ids]

	return {
		"rules": preserved + generated_rules,
		"preserved": preserved,
		"dropped": dropped,
		"replaced": replaced,
	}


def overlapping_foreign_rules(preserved: list[dict], settings=None) -> list[dict]:
	"""Operator rules whose prefix overlaps ours.

	S3 applies the union of every matching rule and the earliest expiration wins, so a
	foreign `Expiration.Days: 1` on the bucket root silently overrides a year of retention.
	Reported, never edited — the rule is the operator's.
	"""
	settings = settings or get_backup_settings()
	base = backup_settings.expand_prefix(settings.backup_prefix)
	scoped_base = f"{base}/" if base else ""

	findings = []
	for rule in preserved:
		prefix = str(_rule_prefix(rule) or "")
		if prefix.startswith(scoped_base) or scoped_base.startswith(prefix):
			findings.append({"id": rule.get("ID"), "prefix": prefix, "rule": rule})
	return findings


def _rule_prefix(rule: dict) -> str | None:
	"""Prefix of a rule in either the current (`Filter`) or legacy (`Prefix`) shape."""
	if "Prefix" in rule:
		return rule.get("Prefix")
	rule_filter = rule.get("Filter") or {}
	if "Prefix" in rule_filter:
		return rule_filter.get("Prefix")
	return (rule_filter.get("And") or {}).get("Prefix")


# ------------------------------------------------------------------ economics


def collect_object_stats() -> dict:
	"""Mean artifact size, from this site's own successful backups.

	Measured rather than assumed: the 40 KB archive overhead matters at 12 KB a config
	artifact and is noise at 4 GB a dump, and only the site knows which it has.
	"""
	rows = frappe.get_all(
		backup_settings.BACKUP_LOG_DOCTYPE,
		filters={"status": "Success"},
		fields=["total_bytes", "sha256_manifest"],
		order_by="creation desc",
		limit=30,
	)
	total_bytes = 0
	artifacts = 0
	for row in rows:
		total_bytes += cint(row.total_bytes)
		try:
			artifacts += len(json.loads(row.sha256_manifest or "{}"))
		except ValueError:
			continue
	return {
		"samples": len(rows),
		"artifacts": artifacts,
		"total_bytes": total_bytes,
		"mean_bytes": int(total_bytes / artifacts) if artifacts else 0,
	}


def validate_economics(settings=None, object_stats: dict | None = None) -> list[dict]:
	"""Cost/retention contradictions in the configured policy.

	Returns `[{level, code, message, math}]`. `level == "error"` blocks the save and the
	apply; warnings are shown in the preview dialog and are the operator's call.
	"""
	settings = settings or get_backup_settings()
	object_stats = object_stats if object_stats is not None else collect_object_stats()

	archive_class = (settings.archive_storage_class or "").strip()
	hot_days = cint(settings.hot_retention_days)
	minimum = GLACIER_MIN_DAYS.get(archive_class, 0)
	findings: list[dict] = []

	for group in retention_groups(settings):
		retention = cint(group["retention_days"])
		archived = archives_group(settings, group)

		if archived and retention > 0 and hot_days + minimum > retention:
			findings.append(
				{
					"level": "error",
					"code": "archive_before_minimum",
					"message": _(
						"{0} artifacts move to {1} on day {2} but are deleted on day {3}, "
						"before {1}'s {4}-day minimum billing duration. You pay the full "
						"minimum for storage you deleted. Raise the retention or drop the "
						"archive transition."
					).format(group["slug"], archive_class, hot_days, retention, minimum),
					"math": f"{hot_days} + {minimum} = {hot_days + minimum} > {retention}",
				}
			)

		if archive_class and hot_days > 0 and 0 < retention <= hot_days:
			findings.append(
				{
					"level": "warn",
					"code": "archive_never_fires",
					"message": _(
						"No archive rule is generated for {0}: artifacts there expire on day "
						"{1}, before the day-{2} transition would fire. That is the cheaper "
						"outcome — archiving them would incur {3}'s {4}-day minimum."
					).format(group["slug"], retention, hot_days, archive_class or "-", minimum),
					"math": f"retention {retention} <= hot {hot_days}",
				}
			)

		if retention <= 0:
			findings.append(
				{
					"level": "warn",
					"code": "no_expiration",
					"message": _(
						"{0} artifacts are never deleted: no expiration rule is generated for "
						"{1}, so that prefix grows without bound."
					).format(group["slug"], group["prefix"]),
					"math": f"retention_days = {retention}",
				}
			)

	# A20: hourly frequency multiplied by the retention it is billed against.
	active_slug = backup_settings.frequency_slug(settings.frequency)
	if active_slug in SUBDAILY_SLUGS:
		per_day = ARTIFACTS_PER_DAY[active_slug]
		retention = cint(settings.subdaily_retention_days)
		runs = int(per_day * retention)
		artifacts_per_run = max(1, _artifacts_per_run(settings))
		total = runs * artifacts_per_run
		if total > SUBDAILY_VOLUME_WARN_ARTIFACTS:
			findings.append(
				{
					"level": "warn",
					"code": "subdaily_volume",
					"message": _(
						"{0} backups kept for {1} days accumulate about {2} artifacts under "
						"{3}. Each PUT, each lifecycle transition and each archived object's "
						"40 KB overhead is billed per object."
					).format(settings.frequency, retention, total, active_slug),
					"math": f"{per_day}/day x {retention}d x {artifacts_per_run} artifacts = {total}",
				}
			)
			if archive_class:
				findings.append(
					{
						"level": "warn",
						"code": "subdaily_archive_overhead",
						"message": _(
							"Those {0} artifacts also carry {1}'s per-object overhead of about "
							"40 KB each ({2} MB of pure overhead) once they transition."
						).format(
							total, archive_class, round(total * ARCHIVE_OVERHEAD_BYTES / 1024 / 1024, 1)
						),
						"math": f"{total} x 40KB = {round(total * ARCHIVE_OVERHEAD_BYTES / 1024 / 1024, 1)}MB",
					}
				)

	mean_bytes = cint(object_stats.get("mean_bytes"))
	if archive_class and mean_bytes and mean_bytes < SMALL_OBJECT_THRESHOLD_BYTES:
		findings.append(
			{
				"level": "warn",
				"code": "small_objects",
				"message": _(
					"Mean artifact size is {0} KB, below the 128 KB point where the 40 KB "
					"per-object archive overhead stops being noise: it adds about {1}% to "
					"what you store."
				).format(round(mean_bytes / 1024, 1), round(100 * ARCHIVE_OVERHEAD_BYTES / mean_bytes)),
				"math": f"40KB / {round(mean_bytes / 1024, 1)}KB = {round(100 * ARCHIVE_OVERHEAD_BYTES / mean_bytes)}%",
			}
		)

	if archive_class == "DEEP_ARCHIVE":
		findings.append(
			{
				"level": "warn",
				"code": "deep_archive_retrieval",
				"message": _(
					"DEEP_ARCHIVE retrieval takes 9-48 hours. A restore cannot start until the "
					"artifact is restored to STANDARD, so plan a recovery-time objective of at "
					"least two days for anything older than {0} days."
				).format(hot_days),
				"math": "standard retrieval ~ $0.02/GB + per-request charges",
			}
		)

	return findings


def _artifacts_per_run(settings) -> int:
	"""How many objects one run uploads, from the include_* checkboxes."""
	count = 1  # the database dump is always uploaded
	count += 1 if cint(settings.include_site_config) else 0
	count += 1 if cint(settings.include_public_files) else 0
	count += 1 if cint(settings.include_private_files) else 0
	return count


def economics_errors(findings: list[dict]) -> list[dict]:
	return [finding for finding in findings if finding.get("level") == "error"]


# ------------------------------------------------------------------ bucket I/O


def fetch_current_policy(client=None, settings=None) -> dict:
	"""The bucket's current lifecycle configuration; `{"Rules": []}` when it has none."""
	from botocore.exceptions import ClientError

	settings = settings or get_backup_settings()
	client = client or backup_settings.get_backup_client(settings)
	try:
		response = client.get_bucket_lifecycle_configuration(Bucket=settings.backup_bucket)
	except ClientError as exc:
		if exc.response.get("Error", {}).get("Code") in (
			"NoSuchLifecycleConfiguration",
			"NoSuchLifecycleConfigurationError",
		):
			return {"Rules": []}
		raise
	return {"Rules": response.get("Rules") or []}


@frappe.whitelist()
def preview_lifecycle_policy() -> dict:
	"""Dry run: the merged policy, what it drops, and the economics behind it.

	This is the apply path with the write removed, so what the dialog shows is what the
	apply writes — a preview computed by a second code path would eventually disagree with
	the thing it previews.
	"""
	frappe.only_for("System Manager")
	settings = get_backup_settings()
	backup_settings.assert_backup_bucket_isolated(settings, context="lifecycle preview")
	assert_prefix_is_scoped(settings)

	generated = build_lifecycle_policy(settings)
	findings = validate_economics(settings)

	current = {"Rules": []}
	current_error = None
	try:
		current = fetch_current_policy(settings=settings)
	except Exception as exc:  # noqa: BLE001 - a preview must render even offline
		current_error = str(exc)

	merged = merge_rules(current["Rules"], generated["Rules"])
	return {
		"bucket": settings.backup_bucket,
		"policy_json": {"Rules": merged["rules"]},
		"generated_rules": generated["Rules"],
		"current_bucket_policy": current,
		"current_policy_error": current_error,
		"preserved_rules": merged["preserved"],
		"dropped_rules": merged["dropped"],
		"replaced_rules": merged["replaced"],
		"overlapping_rules": overlapping_foreign_rules(merged["preserved"], settings),
		"warnings": findings,
		"economics_table": findings,
		"policy_hash": policy_hash({"Rules": merged["rules"]}),
		"confirm_phrase": APPLY_CONFIRM_PHRASE,
	}


@frappe.whitelist()
def apply_lifecycle_policy(confirm_phrase: str | None = None) -> dict:
	"""Write the merged policy to the backup bucket. Destructive: it schedules deletions.

	System Manager only, server-side type-to-confirm, audited. The confirmation is checked
	here and not only in the dialog, because the dialog is not what an API caller sees.
	"""
	frappe.only_for("System Manager")

	from cloud_file_storage.migration import audit

	settings = get_backup_settings()
	if not (settings.backup_bucket or "").strip():
		frappe.throw(_("Configure a backup bucket first."), title=_("Backup Not Configured"))

	# All three guards run before the phrase check so a misconfiguration is reported as one
	# rather than as a typo.
	backup_settings.assert_backup_bucket_isolated(settings, context="lifecycle apply")
	assert_prefix_is_scoped(settings)

	# Byte for byte, with no `.strip()` — see DECISIONS 2026-08-16 (security finding L-1). The
	# other type-to-confirm gate in this app (`migration.api.start_cleanup`) has always
	# compared exactly, and two remote-deletion gates with opposite normalisation rules are a
	# trap for whoever reads one and reasons about the other. A phrase the server normalises
	# is also a phrase the operator can get wrong and still be understood, which is the one
	# thing a confirmation must not be. Neither Desk dialog trims before posting, so what the
	# operator typed is what is compared.
	if confirm_phrase != APPLY_CONFIRM_PHRASE:
		frappe.throw(
			_("Type {0} to confirm applying the lifecycle policy to {1}.").format(
				frappe.bold(APPLY_CONFIRM_PHRASE), frappe.bold(settings.backup_bucket)
			),
			title=_("Confirmation Required"),
			exc=frappe.ValidationError,
		)

	findings = validate_economics(settings)
	errors = economics_errors(findings)
	if errors:
		frappe.throw(
			_("This lifecycle policy would destroy value: {0}").format(
				" ".join(finding["message"] for finding in errors)
			),
			title=_("Lifecycle Policy Refused"),
			exc=frappe.ValidationError,
		)

	client = backup_settings.get_backup_client(settings)
	current = fetch_current_policy(client, settings)
	generated = build_lifecycle_policy(settings)
	merged = merge_rules(current["Rules"], generated["Rules"])
	policy = {"Rules": merged["rules"]}

	client.put_bucket_lifecycle_configuration(Bucket=settings.backup_bucket, LifecycleConfiguration=policy)

	digest = policy_hash(policy)
	frappe.db.set_single_value(
		backup_settings.BACKUP_SETTINGS_DOCTYPE,
		{"lifecycle_applied_hash": digest, "lifecycle_applied_on": now_datetime()},
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)
	audit.record(
		"backup_lifecycle_applied",
		bucket=settings.backup_bucket,
		policy_hash=digest,
		rule_ids=[rule["ID"] for rule in merged["rules"] if rule.get("ID")],
		dropped_rule_ids=[rule.get("ID") for rule in merged["dropped"]],
		preserved_rule_ids=[rule.get("ID") for rule in merged["preserved"]],
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)
	frappe.clear_document_cache(backup_settings.BACKUP_SETTINGS_DOCTYPE)

	return {
		"bucket": settings.backup_bucket,
		"policy_hash": digest,
		"applied_rules": [rule.get("ID") for rule in merged["rules"]],
		"dropped_rules": merged["dropped"],
		"preserved_rules": merged["preserved"],
		"warnings": findings,
	}
