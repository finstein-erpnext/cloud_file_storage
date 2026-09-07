"""Test harness for the backup domain: a fake backup bucket and a settings snapshot.

The fake replaces `backup.settings.get_backup_client`, i.e. the boto3 client itself, so the
real `upload_and_verify`, the real freshness assertion and the real lifecycle merge all run
— only the network is removed. Nothing in this module recomputes a checksum the way the
code under test does: `head_object` reports the digest of the bytes the bucket is actually
holding, so a fake that stored something else cannot agree with itself, and every
"verification bites" assertion here is a real one.
"""

import base64
import hashlib
import io
import os
import time
from unittest.mock import patch

import frappe
import frappe.tests.utils

from cloud_file_storage.backup.settings import BACKUP_SETTINGS_DOCTYPE

TEST_BACKUP_BUCKET = "cfs-unit-test-backups"


def no_such_lifecycle_error():
	"""The real `ClientError` S3 raises for a bucket with no lifecycle configuration.

	A bespoke exception class here would be caught by nothing the production code writes,
	so `fetch_current_policy`'s handler would look tested while never running.
	"""
	from botocore.exceptions import ClientError

	return ClientError(
		{"Error": {"Code": "NoSuchLifecycleConfiguration", "Message": "no lifecycle"}},
		"GetBucketLifecycleConfiguration",
	)


class InjectorRegistry(dict):
	"""The failure injectors, holding the values the fake actually reads.

	Rejects an unregistered key **at the point of use**, which is what makes registration
	load-bearing rather than descriptive: a test that reaches for an injector nobody
	implemented fails immediately and loudly, instead of setting an attribute the fake never
	consults and then passing for a reason that has nothing to do with what it claims.
	"""

	def __setitem__(self, key, value):
		if key not in self:
			raise KeyError(
				f"{key!r} is not a registered failure injector. Add it to "
				"FakeBackupBucket.FAILURE_INJECTORS and make an injection site read it, "
				"or the fake will silently ignore it."
			)
		super().__setitem__(key, value)


class FakeBackupBucket:
	"""An in-memory S3 bucket exposing only the calls the backup domain makes."""

	#: Every failure this fake can inject, mapped to its inert default. **This is the
	#: definition, not a description of one**: `__init__` creates the attributes from it, so
	#: an injector cannot exist without being registered here, and
	#: `test_every_failure_injector_the_fake_offers_is_exercised` reads this rather than
	#: guessing at attribute-name prefixes. The first version of that test guessed
	#: (`fail_`, `report_`, `store_`, `composite_`) and would have missed an injector named
	#: `raise_on_get` — a guard against stale restated lists, resting on one.
	FAILURE_INJECTORS = {
		#: The bucket silently keeps different bytes than it was sent.
		"store_instead": None,
		#: HEAD reports the digest of THESE bytes rather than of what is stored — a bucket
		#: whose metadata lies. Only a streamed re-GET catches it.
		"report_checksum_of": None,
		#: Report a multipart checksum-of-checksums (`…-4`).
		"composite_checksum": False,
		#: HEAD reports this ContentLength instead of the real one.
		"report_size": None,
		#: Raised by `upload_file`, before anything is stored.
		"fail_upload_with": None,
		#: Raised by `head_object`, AFTER the artifact is in the bucket.
		"fail_head_with": None,
		#: Raised by `get_object`, i.e. during the streamed re-GET — also after the artifact
		#: is in the bucket. Added when the gate pointed out that the re-GET transport test
		#: monkeypatched `get_object` directly and so sat outside this registry entirely.
		"fail_get_with": None,
	}

	def __init__(self):
		self.objects: dict[str, bytes] = {}
		self.uploads: list[dict] = []
		#: Every `delete_object` call. A5's rule is that this list stays EMPTY on a failed
		#: verification, so tests assert against it directly.
		self.deleted: list[str] = []
		self.lifecycle: dict | None = None
		self.put_lifecycle_calls: list[dict] = []
		self.versioning: dict = {"Status": "Enabled"}
		self.restore_requests: list[dict] = []
		self.presign_calls: list[dict] = []
		self.head_bucket_calls: list[str] = []

		# Failure injection. Every injection site below reads `self.injectors[...]`, so this
		# registry is the mechanism and not a description of one: an injector that is not
		# registered cannot be read, and `InjectorRegistry` refuses to let a test set one.
		self.injectors = InjectorRegistry(self.FAILURE_INJECTORS)

		self._patchers: list = []

	# --- lifecycle -----------------------------------------------------------------

	#: Every module that imported `get_backup_client` by name. `from … import f` binds a
	#: second reference, so patching only the defining module leaves the importers calling
	#: the real boto3 constructor — which is how the first draft of this harness produced a
	#: NoCredentialsError instead of a fake bucket.
	CLIENT_PATCH_TARGETS = (
		"cloud_file_storage.backup.settings.get_backup_client",
		"cloud_file_storage.backup.tasks.get_backup_client",
		"cloud_file_storage.backup.restore.get_backup_client",
	)

	def start(self):
		self._patchers = []
		for target in self.CLIENT_PATCH_TARGETS:
			patcher = patch(target, return_value=self)
			patcher.start()
			self._patchers.append(patcher)

	def stop(self):
		for patcher in reversed(getattr(self, "_patchers", [])):
			patcher.stop()
		self._patchers = []

	# --- S3 surface ----------------------------------------------------------------

	def upload_file(self, path, bucket, key, ExtraArgs=None, Config=None):
		if self.injectors["fail_upload_with"] is not None:
			raise self.injectors["fail_upload_with"]
		with open(path, "rb") as handle:
			content = handle.read()
		self.objects[key] = (
			self.injectors["store_instead"] if self.injectors["store_instead"] is not None else content
		)
		self.uploads.append(
			{
				"key": key,
				"bucket": bucket,
				"path": path,
				"extra_args": dict(ExtraArgs or {}),
				"config": Config,
				"sent_bytes": len(content),
			}
		)

	def head_object(self, Bucket, Key, ChecksumMode=None):
		if self.injectors["fail_head_with"] is not None:
			raise self.injectors["fail_head_with"]
		if Key not in self.objects:
			raise KeyError(Key)

		stored = self.objects[Key]
		checksum_source = (
			self.injectors["report_checksum_of"]
			if self.injectors["report_checksum_of"] is not None
			else stored
		)
		checksum = base64.b64encode(hashlib.sha256(checksum_source).digest()).decode()
		if self.injectors["composite_checksum"]:
			checksum = f"{checksum}-4"

		return {
			"ContentLength": self.injectors["report_size"]
			if self.injectors["report_size"] is not None
			else len(stored),
			"ChecksumSHA256": checksum,
			"ETag": f'"{hashlib.md5(stored).hexdigest()}"',
			"StorageClass": "STANDARD",
			"ChecksumMode": ChecksumMode,
		}

	def get_object(self, Bucket, Key):
		if self.injectors["fail_get_with"] is not None:
			raise self.injectors["fail_get_with"]
		if Key not in self.objects:
			raise KeyError(Key)
		return {"Body": io.BytesIO(self.objects[Key])}

	def delete_object(self, Bucket, Key):
		self.deleted.append(Key)
		self.objects.pop(Key, None)
		return {}

	def head_bucket(self, Bucket):
		self.head_bucket_calls.append(Bucket)
		return {}

	def get_bucket_lifecycle_configuration(self, Bucket):
		if self.lifecycle is None:
			raise no_such_lifecycle_error()
		return {"Rules": list(self.lifecycle.get("Rules") or [])}

	def put_bucket_lifecycle_configuration(self, Bucket, LifecycleConfiguration):
		self.put_lifecycle_calls.append({"bucket": Bucket, "policy": LifecycleConfiguration})
		self.lifecycle = LifecycleConfiguration
		return {}

	def get_bucket_versioning(self, Bucket):
		return dict(self.versioning)

	def restore_object(self, Bucket, Key, RestoreRequest=None):
		self.restore_requests.append({"key": Key, "request": RestoreRequest})
		return {"ResponseMetadata": {"HTTPStatusCode": 202}}

	def generate_presigned_url(self, operation, Params=None, ExpiresIn=None):
		self.presign_calls.append({"operation": operation, "params": Params, "ttl": ExpiresIn})
		return f"https://fake-backups.invalid/{(Params or {}).get('Key')}?X-Amz-Expires={ExpiresIn}"


#: Every Cloud Backup Settings field the suite may write. Snapshotted per class and
#: force-restored, so a run leaves the singleton exactly as it found it — the same contract
#: `tests/utils.MANAGED_SETTINGS_FIELDS` holds for Cloud Storage Settings.
MANAGED_BACKUP_SETTINGS_FIELDS = (
	"enabled",
	"backup_bucket",
	"backup_prefix",
	"use_storage_credentials",
	"region_name",
	"endpoint_url",
	"access_key",
	"sse_type",
	"kms_key_id",
	"frequency",
	"backup_hour",
	"backup_weekday",
	"include_site_config",
	"include_public_files",
	"include_private_files",
	"compress_files",
	"hot_retention_days",
	"archive_storage_class",
	"delete_after_days",
	"subdaily_retention_days",
	"noncurrent_retention_days",
	"abort_multipart_days",
	"lifecycle_applied_hash",
	"lifecycle_applied_on",
	"notify_email",
	"email_on_success",
	"log_retention_days",
	"last_backup_on",
	"last_backup_status",
)

MANAGED_BACKUP_SECRET_FIELD = "secret_key"

#: The state `set_backup_settings` starts every test from. Values match the shipped
#: defaults, so a test that changes nothing is testing the configuration operators get.
DEFAULT_BACKUP_SETTINGS = {
	"enabled": 1,
	"backup_bucket": TEST_BACKUP_BUCKET,
	"backup_prefix": "{site}/backups",
	"use_storage_credentials": 1,
	"region_name": "",
	"endpoint_url": "",
	"access_key": "",
	"sse_type": "Inherit",
	"kms_key_id": "",
	"frequency": "Daily",
	"backup_hour": 1,
	"backup_weekday": "Monday",
	"include_site_config": 1,
	"include_public_files": 0,
	"include_private_files": 0,
	"compress_files": 1,
	"hot_retention_days": 14,
	"archive_storage_class": "GLACIER",
	"delete_after_days": 365,
	"subdaily_retention_days": 7,
	"noncurrent_retention_days": 7,
	"abort_multipart_days": 7,
	"lifecycle_applied_hash": "",
	"lifecycle_applied_on": None,
	"notify_email": "",
	"email_on_success": 0,
	"log_retention_days": 90,
	"last_backup_on": None,
	"last_backup_status": "",
}


#: Marks a field that has no `tabSingles` row at all, which is a different state from a
#: row holding NULL and must be restored as such.
ABSENT = object()


#: Bounded retries for a `tabSingles` write that lost a deadlock.
DEADLOCK_RETRIES = 5


def retry_on_deadlock(operation, *, what: str):
	"""Run a `tabSingles` write, retrying if InnoDB picked it as the deadlock victim.

	A deadlock is not a failure of the statement — MariaDB rolls one transaction back and
	says "try again". Every write behind this is idempotent (set a value; delete rows that
	should not exist), so retrying is the correct response and changes no outcome.

	It is here because a cleanup that *cannot* put the singleton back leaves the site dirty
	for every test after it, and the suite is then reporting on a state no test chose — which
	is how a single lost lock turned into five to ten errored tests, most of them nowhere
	near the write that lost it.

	**This does not make two suites on one site safe** — PLAN §C forbids that and this does
	not change it. It makes the case survivable, which matters because an independent gate
	legitimately attests on a site its owner may also be touching; `tests.utils.set_mode`
	already anticipates exactly that in its own comment about this table.
	"""
	from frappe.exceptions import QueryDeadlockError, QueryTimeoutError

	for attempt in range(DEADLOCK_RETRIES):
		try:
			return operation()
		except (QueryDeadlockError, QueryTimeoutError):
			frappe.db.rollback()
			if attempt == DEADLOCK_RETRIES - 1:
				frappe.logger("cloud_file_storage").warning(f"gave up retrying after a deadlock: {what}")
				raise
			time.sleep(0.05 * (attempt + 1))
	return None


def snapshot_backup_settings() -> dict:
	"""The RAW stored value of every managed field, plus the credential.

	Raw, not `get_single_value`, because that casts — and the cast is not reversible for a
	Datetime: `cast("Datetime", None)` is `datetime.min`, `set_single_value` stores that as
	the string `0001-01-01T00:00:00`, and reading it back gives `None` again. A snapshot
	taken through the cast therefore alternates the stored bytes between runs, which is
	precisely what a "the suite left the site as it found it" check is supposed to catch —
	and it caught this one.
	"""
	from frappe.utils.password import get_decrypted_password

	stored = {
		row.field: row.value
		for row in frappe.db.sql(
			"select field, value from tabSingles where doctype = %s",
			(BACKUP_SETTINGS_DOCTYPE,),
			as_dict=True,
		)
	}
	snapshot = {fieldname: stored.get(fieldname, ABSENT) for fieldname in MANAGED_BACKUP_SETTINGS_FIELDS}
	try:
		snapshot[MANAGED_BACKUP_SECRET_FIELD] = get_decrypted_password(
			BACKUP_SETTINGS_DOCTYPE,
			BACKUP_SETTINGS_DOCTYPE,
			MANAGED_BACKUP_SECRET_FIELD,
			raise_exception=False,
		)
	except Exception:  # noqa: BLE001 - no stored credential is a normal state
		snapshot[MANAGED_BACKUP_SECRET_FIELD] = None
	return snapshot


def restore_backup_settings(snapshot: dict):
	from frappe.utils.password import remove_encrypted_password, set_encrypted_password

	frappe.db.rollback()
	values = {
		key: value
		for key, value in snapshot.items()
		if key != MANAGED_BACKUP_SECRET_FIELD and value is not ABSENT
	}
	if values:
		retry_on_deadlock(
			lambda: frappe.db.set_single_value(BACKUP_SETTINGS_DOCTYPE, values),
			what="restore backup settings",
		)
	# A field that had no row before the run must have no row after it. One statement, not
	# one per field, for the reason `tests.utils.set_mode` states about the same table: a
	# field-by-field loop over `tabSingles` is enough lock churn to deadlock against any
	# other transaction touching it, and this runs in every cleanup of every test.
	absent = [
		key for key, value in snapshot.items() if key != MANAGED_BACKUP_SECRET_FIELD and value is ABSENT
	]
	if absent:
		retry_on_deadlock(
			lambda: frappe.db.delete(
				"Singles", {"doctype": BACKUP_SETTINGS_DOCTYPE, "field": ("in", absent)}
			),
			what="drop absent backup settings rows",
		)

	secret = snapshot.get(MANAGED_BACKUP_SECRET_FIELD)
	try:
		if secret:
			set_encrypted_password(
				BACKUP_SETTINGS_DOCTYPE, BACKUP_SETTINGS_DOCTYPE, secret, MANAGED_BACKUP_SECRET_FIELD
			)
		else:
			remove_encrypted_password(
				BACKUP_SETTINGS_DOCTYPE, BACKUP_SETTINGS_DOCTYPE, MANAGED_BACKUP_SECRET_FIELD
			)
	except Exception:  # noqa: BLE001 - restoring the rest matters more
		frappe.logger("cloud_file_storage").warning("could not restore the backup credential")

	frappe.db.commit()
	frappe.clear_document_cache(BACKUP_SETTINGS_DOCTYPE)


def set_backup_settings(**fields):
	"""Write the backup singleton straight to the DB, bypassing `validate`.

	Bypassed for the same reason `tests.utils.set_mode` bypasses the storage validator: the
	validators are a separate concern with their own tests, and several of them probe a
	bucket that does not exist on this host.
	"""
	retry_on_deadlock(
		lambda: frappe.db.set_single_value(BACKUP_SETTINGS_DOCTYPE, {**DEFAULT_BACKUP_SETTINGS, **fields}),
		what="set backup settings",
	)
	frappe.clear_document_cache(BACKUP_SETTINGS_DOCTYPE)


def write_artifact(path: str, content: bytes, *, mtime: float | None = None) -> str:
	os.makedirs(os.path.dirname(path), exist_ok=True)
	with open(path, "wb") as handle:
		handle.write(content)
	if mtime is not None:
		os.utime(path, (mtime, mtime))
	return path


class FakeDump:
	"""Stands in for the `BackupGenerator` `new_backup` returns."""

	def __init__(
		self,
		*,
		backup_path_db=None,
		backup_path_conf=None,
		backup_path_files=None,
		backup_path_private_files=None,
		todays_date="20260816_010000",
	):
		self.backup_path_db = backup_path_db
		self.backup_path_conf = backup_path_conf
		self.backup_path_files = backup_path_files
		self.backup_path_private_files = backup_path_private_files
		self.todays_date = todays_date
		#: `{path: age_in_seconds}`, applied by `stamp()` when the fake `new_backup` is
		#: called — see the note in `BackupJobTestCase.make_dump`.
		self.ages: dict[str, float] = {}

	def stamp(self):
		"""Set each artifact's mtime relative to NOW, as the real generator would."""
		now = time.time()
		for path, age in self.ages.items():
			if path and os.path.exists(path):
				os.utime(path, (now - age, now - age))


class BackupTestCase(frappe.tests.utils.FrappeTestCase):
	"""Both settings singletons configured, a fake backup bucket, everything restored after.

	The storage singleton is configured too, not only the backup one: the backup domain
	inherits its credentials, its SSE mode and its multipart threshold from Cloud Storage
	Settings, so a test that left those at whatever the previous suite wrote would be
	verifying against an unknown threshold.
	"""

	#: Small enough that a "large artifact" in these tests is a megabyte, not 64 of them.
	MULTIPART_THRESHOLD_MB = 1

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from cloud_file_storage.tests.utils import restore_settings, snapshot_settings

		cls._storage_snapshot = snapshot_settings()
		cls._backup_snapshot = snapshot_backup_settings()
		cls.addClassCleanup(restore_settings, cls._storage_snapshot)
		cls.addClassCleanup(restore_backup_settings, cls._backup_snapshot)

	def setUp(self):
		super().setUp()
		from cloud_file_storage.tests.utils import set_mode

		set_mode("S3_ONLY", multipart_threshold_mb=self.MULTIPART_THRESHOLD_MB)
		set_backup_settings()

		self.bucket = FakeBackupBucket()
		self.bucket.start()
		self.addCleanup(self.bucket.stop)

		self._log_names: list[str] = []
		self._temp_paths: list[str] = []
		self.addCleanup(self._cleanup)

	#: Audit actions this suite can cause. Cleaned per test, because `download_url` writes one
	#: on every successful mint — including from the positive controls — and nothing else was
	#: removing them. Unbounded growth in a table other suites count rows in.
	AUDIT_ACTIONS = ("backup_presign_issued", "backup_lifecycle_applied")

	def _cleanup(self):
		for name in self._log_names:
			frappe.db.delete("Cloud Storage Backup Log", {"name": name})
		frappe.db.delete("Cloud Storage Audit Log", {"action": ("in", self.AUDIT_ACTIONS)})
		# Committed BEFORE the settings restore, which opens with a rollback: without this
		# the deletes above were rolled back and every test's log rows survived into the
		# next test. Found by `test_a_failed_verification_is_never_logged_success`.
		frappe.db.commit()

		for path in self._temp_paths:
			if os.path.exists(path):
				os.remove(path)
		# The same exact restore the class cleanup uses, so a per-test reset and the
		# end-of-class reset cannot leave the singleton in two different shapes.
		restore_backup_settings(self._backup_snapshot)

	# --- fixtures ------------------------------------------------------------------

	def temp_artifact(self, filename: str, content: bytes, *, mtime: float | None = None) -> str:
		path = frappe.get_site_path("private", "backups", filename)
		write_artifact(path, content, mtime=mtime)
		self._temp_paths.append(path)
		return path

	def track_log(self, name: str) -> str:
		self._log_names.append(name)
		return name

	def track_new_logs(self):
		"""Register every Backup Log row so the cleanup removes it."""
		for row in frappe.get_all("Cloud Storage Backup Log", pluck="name"):
			if row not in self._log_names:
				self._log_names.append(row)


def dump_all_singles() -> str:
	"""Every stored field of both settings singletons, for a between-runs comparison.

	`modified` is excluded and nothing else is: the gate's question is whether a test run
	left the site as it found it, and a field left out of this dump is a field a leak can
	hide in.
	"""
	import json

	payload = {}
	for doctype in ("Cloud Storage Settings", "Cloud Backup Settings"):
		rows = frappe.db.sql(
			"select field, value from tabSingles where doctype = %s order by field",
			(doctype,),
			as_dict=True,
		)
		payload[doctype] = {row.field: row.value for row in rows if row.field != "modified"}
	return json.dumps(payload, indent=1, sort_keys=True, default=str)
