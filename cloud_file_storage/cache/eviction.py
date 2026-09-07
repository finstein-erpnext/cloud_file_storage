"""Bounded materialization cache: TTL + size eviction (daily, dispatched).

Three rules keep this from ever losing data:

* a **dirty** entry (local changes not yet written back) is never evicted;
* an entry whose bytes no longer match its sidecar is treated as dirty, because that
  difference IS an unflushed local edit — the explicit `dirty` flag is only set when a
  write-back upload fails, so it cannot be the only signal;
* an entry whose materialization filelock is held is never evicted, so a download in
  flight is never pulled out from under its reader.

The budget is enforced here *and* on the materialize path (`materialize.make_room_for`):
this sweep runs daily, and a busy site can otherwise grow the cache past
`cache_max_size_mb` between two sweeps and fill the disk.

Canonical `files/` copies are not cache entries and are never touched here — they belong
to mode semantics, not to the cache.
"""

import json
import os

import frappe
from filelock import FileLock as _StrongFileLock
from filelock import Timeout as _FileLockTimeout
from frappe.utils import add_to_date, cint, get_datetime, get_site_path, now_datetime
from frappe.utils import synchronization as frappe_synchronization

from cloud_file_storage.cache.materialize import (
	CACHE_DIRNAME,
	SIDECAR_SUFFIX,
	sidecar_path,
)
from cloud_file_storage.cache.materialize import (
	has_unflushed_changes as materialize_has_unflushed_changes,
)
from cloud_file_storage.storage.hashing import stat_signature
from cloud_file_storage.storage.modes import get_settings

MB = 1024 * 1024
#: How long to wait for an entry's filelock before deciding it is in use. Long enough to
#: lose a race, short enough that a sweep never stalls behind a large download.
LOCK_PROBE_TIMEOUT = 0.2


def probe_lock_path(lock_name: str) -> str:
	"""The lockfile `frappe.utils.synchronization.filelock(lock_name)` would use.

	The probe deliberately does NOT go through frappe's `filelock`: that helper calls
	`frappe.log_error` on every timeout, so a contended entry — an ordinary, expected
	outcome that just means "someone is using this, skip it" — would insert an Error Log
	document (with a full `get_traceback(with_context=True)`) per entry per sweep. On a busy
	site that is both a flood of false errors and real work on a maintenance path.

	Constructing the path here is only safe while it stays identical to frappe's, so the
	site-relative directory is taken from frappe's own module constant and two tests hold
	the two halves of that contract:
	`test_writeback.TestTheEvictionLockProbe.test_the_probe_targets_the_lockfile_frappe_locks`
	(the path is the one frappe's helper actually creates) and
	`…test_the_probe_does_not_hold_a_lock_frappe_would_consider_free` (holding it really does
	exclude frappe's acquisition). Agreeing on the path without excluding anything would be a
	check that passes while the property it names is untrue.
	"""
	return os.path.abspath(get_site_path(frappe_synchronization.LOCKS_DIR, f"{lock_name}.lock"))


def budget_bytes(settings=None) -> int:
	"""The configured cache budget in bytes.

	An explicit 0 means "keep nothing cached" and is honoured; only an unset field falls
	back to the A12 default.
	"""
	settings = settings or get_settings()
	configured = settings.cache_max_size_mb
	return (cint(configured) if configured not in (None, "") else 5120) * MB


def _file_name_of(entry_name: str) -> str:
	"""The File row an entry belongs to. Entries are named `{file}__{cso}{ext}`."""
	return entry_name.split("__", 1)[0]


def _has_drifted(path: str, sidecar: dict) -> bool:
	"""Whether the bytes on disk no longer match what was materialized.

	Delegates the provable case to `materialize.has_unflushed_changes` so eviction and
	materialization can never disagree about what "this entry holds an unflushed edit"
	means; one of them protecting bytes the other discards is the whole failure mode.

	The two differ on one point, deliberately: a sidecar missing either half of the
	signature is drift **here**, because eviction's question is "can I show this entry is
	safe to delete" and the answer is no. `materialize`'s question is the opposite one — "can
	I show these local bytes are an edit worth keeping over the object" — so it answers no
	to the same input.
	"""
	if stat_signature(path) is None:
		return False

	if sidecar.get("size") is None or sidecar.get("mtime") is None:
		return True

	return materialize_has_unflushed_changes(path, sidecar)


def _entries(root: str) -> list[dict]:
	entries = []
	for name in sorted(os.listdir(root)):
		if name.endswith((SIDECAR_SUFFIX, ".part", ".lock")):
			continue
		path = os.path.join(root, name)
		if not os.path.isfile(path):
			continue

		sidecar = {}
		try:
			with open(sidecar_path(path)) as handle:  # nosemgrep: frappe-security-file-traversal
				sidecar = json.load(handle)
		except (OSError, ValueError):
			sidecar = {}

		try:
			size = os.path.getsize(path)
		except OSError:
			continue

		entries.append(
			{
				"path": path,
				"name": name,
				"size": size,
				"dirty": bool(sidecar.get("dirty")) or _has_drifted(path, sidecar),
				"last_access": sidecar.get("last_access"),
			}
		)
	return entries


def _last_access(entry: dict):
	try:
		return get_datetime(entry["last_access"])
	except Exception:  # noqa: BLE001 - a missing/garbled sidecar sorts oldest-first
		return get_datetime("1970-01-01 00:00:00")


def _drop(entry: dict) -> int:
	try:
		os.remove(entry["path"])
	except OSError:
		return 0
	try:
		os.remove(sidecar_path(entry["path"]))
	except OSError:
		pass
	return entry["size"]


def _remove(entry: dict, *, holding_lock_for: str | None = None) -> tuple[int, bool]:
	"""Delete one entry unless its File's materialization lock is held by someone else.

	Returns ``(bytes_freed, contended)``. `contended` says the entry was skipped **because
	it is in use**, which is exactly the case in which the sweep under-frees; the caller
	reports it so an unmet budget is a visible number rather than a silence.

	`holding_lock_for` names a File whose lock the CALLER already holds — `materialize` calls
	the admission check from inside `filelock(cfs_mat_<file>)`. The lock is not reentrant, so
	without this a materialize could never evict its own File's stale entries: the probe
	would time out and the trim would silently under-free.
	"""
	owner = _file_name_of(entry["name"])
	if holding_lock_for and owner == holding_lock_for:
		return _drop(entry), False

	try:
		with _StrongFileLock(probe_lock_path(f"cfs_mat_{owner}"), timeout=LOCK_PROBE_TIMEOUT):
			return _drop(entry), False
	except _FileLockTimeout:
		# Someone is materializing or writing back this entry. Leave it for the next sweep.
		return 0, True


def _empty_result() -> dict:
	return {"trimmed": 0, "bytes_freed": 0, "contended": 0, "bytes_contended": 0, "over_budget": 0}


def trim_to(
	target_bytes: int, entries: list[dict] | None = None, *, holding_lock_for: str | None = None
) -> dict:
	"""Evict least-recently-used clean entries until the cache fits in ``target_bytes``.

	`entries` is the **whole** cache — dirty entries included. Only clean ones are evictable,
	but every entry occupies the budget, so measuring the total over the clean ones alone
	would let a cache that is mostly dirty (or mostly locked) report itself as under budget
	and free nothing while the disk fills. What cannot be freed is returned as
	`over_budget`/`contended` instead of being dropped on the floor.
	"""
	root = get_site_path(CACHE_DIRNAME)
	if not os.path.isdir(root):
		return _empty_result()

	if entries is None:
		entries = _entries(root)

	total = sum(entry["size"] for entry in entries)
	freed = 0
	trimmed = 0
	contended = 0
	bytes_contended = 0
	for entry in sorted((e for e in entries if not e["dirty"]), key=_last_access):
		if total <= target_bytes:
			break
		removed, was_contended = _remove(entry, holding_lock_for=holding_lock_for)
		if removed:
			total -= removed
			freed += removed
			trimmed += 1
		elif was_contended:
			contended += 1
			bytes_contended += entry["size"]

	return {
		"trimmed": trimmed,
		"bytes_freed": freed,
		"contended": contended,
		"bytes_contended": bytes_contended,
		"over_budget": max(0, total - target_bytes),
	}


def run_eviction():
	"""Scheduled entry point: expire by TTL, then trim to the size budget."""
	root = get_site_path(CACHE_DIRNAME)
	if not os.path.isdir(root):
		return {"expired": 0, **_empty_result()}

	settings = get_settings()
	ttl_hours = cint(settings.cache_ttl_hours) or 72
	max_bytes = budget_bytes(settings)

	entries = _entries(root)
	cutoff = add_to_date(now_datetime(), hours=-ttl_hours)

	freed = 0
	expired = 0
	contended = 0
	bytes_contended = 0
	survivors = []
	for entry in entries:
		# Dirty entries are never expired — they hold the only copy of an unflushed edit —
		# but they stay in `survivors` because they still occupy the budget.
		if not entry["dirty"] and _last_access(entry) < cutoff:
			removed, was_contended = _remove(entry)
			if removed:
				freed += removed
				expired += 1
				continue
			if was_contended:
				contended += 1
				bytes_contended += entry["size"]
		survivors.append(entry)

	trim = trim_to(max_bytes, survivors)
	freed += trim["bytes_freed"]
	contended += trim["contended"]
	bytes_contended += trim["bytes_contended"]

	result = {
		"expired": expired,
		"trimmed": trim["trimmed"],
		"bytes_freed": freed,
		"contended": contended,
		"bytes_contended": bytes_contended,
		"over_budget": trim["over_budget"],
	}
	if freed:
		frappe.logger("cloud_file_storage").info({"operation": "cache_eviction", **result})
	if result["over_budget"]:
		# The bound this app advertises was not met. Saying so is the difference between a
		# known-degraded cache and one silently growing past `cache_max_size_mb`.
		frappe.logger("cloud_file_storage").warning(
			{"operation": "cache_eviction_over_budget", "budget_bytes": max_bytes, **result}
		)
	return result


#: Where a cache entry goes when its bytes can neither be proven safe to drop nor proven
#: to be the object. A sibling of the cache directory, not a subdirectory of it: entries
#: here must not count towards the budget and must not be re-scanned by the next sweep.
REPAIR_QUARANTINE_DIRNAME = "cloud_storage_cache_quarantine"


def repair_quarantine_root() -> str:
	path = get_site_path(REPAIR_QUARANTINE_DIRNAME)
	os.makedirs(path, exist_ok=True)
	return path


def _cso_name_of(entry_name: str) -> str:
	"""The Cloud Storage Object an entry was materialized from.

	Entries are named `{file}__{cso}{ext}` and a CSO name is a 32-character uuid4 hex with
	no dot in it (`CloudStorageObject.autoname`), so the extension splits off cleanly.
	"""
	remainder = entry_name.split("__", 1)[1] if "__" in entry_name else ""
	return remainder.split(".", 1)[0]


def _needs_repair(sidecar: dict | None) -> bool:
	"""Whether this entry can NEVER be evicted as things stand.

	`_has_drifted` treats a sidecar that is missing, unreadable or missing either half of
	the `(mtime, size)` signature as drift — correctly, because eviction's question is "can
	I prove this is safe to delete" and the answer is no. But nothing ever answers it later
	either: the entry stays dirty for ever, counts towards `over_budget` for ever, and the
	sweep warns on every run with no remedy. That is the gap this repairs.
	"""
	if not sidecar:
		return True
	return sidecar.get("size") is None or sidecar.get("mtime") is None


def repair_unevictable_entries_dispatch():
	"""Daily dispatcher: the repair walks the whole cache and re-hashes, so it is not O(ms).

	Same reason as the GC dispatchers in `gc.py`: the `daily` frequency runs on `default`
	(300s), while this app's floor for maintenance work is the dedicated `cloud_migration`
	queue with `long` as its loud fallback (`background.py`). Registering the worker directly
	on `daily` put unbounded cache work on the ERP's shared default queue.
	"""
	from cloud_file_storage.background import enqueue_maintenance

	enqueue_maintenance("cloud_file_storage.cache.eviction.repair_unevictable_entries", timeout=1500)


def run_eviction_dispatch():
	"""Daily dispatcher: eviction stats the entire cache tree. See the note above."""
	from cloud_file_storage.background import enqueue_maintenance

	enqueue_maintenance("cloud_file_storage.cache.eviction.run_eviction", timeout=1500)


def repair_unevictable_entries() -> dict:
	"""Give permanently-dirty cache entries a way out (carried from P4, decided in P6).

	An entry whose sidecar is missing or garbled can never be evicted, so it occupies the
	budget for ever and `run_eviction` reports `cache_eviction_over_budget` on every run
	with nothing an operator can do about it. Two outcomes, and which one an entry gets is
	decided by evidence, never by convenience:

	* **the bytes still hash to the object's `content_sha256`** — then the entry is a
	  faithful copy of a verified remote object, nothing is at risk, and a clean sidecar is
	  re-derived. The entry becomes evictable again.
	* **the bytes differ, or the object is unknown** — then these are local changes that
	  were never written back, or corruption, and they may be the only copy. The entry is
	  **moved to a quarantine directory, never deleted**, and logged with its path so an
	  operator can recover it. Same rule as migration CLEANUP: bytes that cannot be shown
	  to be safe to lose are not lost (PLAN §C).

	Entries whose materialization lock is held are skipped, exactly as eviction skips them.
	"""
	from cloud_file_storage.cache.materialize import read_sidecar, write_sidecar
	from cloud_file_storage.storage.hashing import digest_path

	root = get_site_path(CACHE_DIRNAME)
	result = {"scanned": 0, "repaired": 0, "quarantined": 0, "contended": 0, "quarantine_paths": []}
	if not os.path.isdir(root):
		return result

	for name in sorted(os.listdir(root)):
		if name.endswith((SIDECAR_SUFFIX, ".part", ".lock")):
			continue
		path = os.path.join(root, name)
		if not os.path.isfile(path):
			continue
		if not _needs_repair(read_sidecar(path)):
			continue

		result["scanned"] += 1
		owner = _file_name_of(name)
		try:
			with _StrongFileLock(probe_lock_path(f"cfs_mat_{owner}"), timeout=LOCK_PROBE_TIMEOUT):
				outcome = _repair_one(path, name, digest_path, write_sidecar)
		except _FileLockTimeout:
			result["contended"] += 1
			continue

		if outcome.get("repaired"):
			result["repaired"] += 1
		elif outcome.get("quarantined"):
			result["quarantined"] += 1
			result["quarantine_paths"].append(outcome["quarantined"])

	if result["quarantined"]:
		frappe.log_error(
			title="cloud_file_storage: quarantined unrecoverable cache entries",
			message=(
				"These cache entries had no usable sidecar and their bytes do not match the "
				"object they were materialized from, so they may hold local changes that were "
				"never written back. They were MOVED, not deleted:\n" + "\n".join(result["quarantine_paths"])
			),
		)
	if result["repaired"] or result["quarantined"]:
		frappe.logger("cloud_file_storage").info({"operation": "cache_repair", **result})
	return result


def _repair_one(path: str, name: str, digest_path, write_sidecar) -> dict:
	"""Re-derive a sidecar, or quarantine. Never deletes."""
	cso_name = _cso_name_of(name)
	expected_sha256 = (
		frappe.db.get_value("Cloud Storage Object", cso_name, "content_sha256") if cso_name else None
	)

	if expected_sha256:
		try:
			digest = digest_path(path)
		except OSError:
			return {}
		if digest.sha256 == expected_sha256:
			write_sidecar(
				path,
				sha256=digest.sha256,
				size=digest.size,
				dirty=False,
				file=_file_name_of(name),
				cloud_storage_object=cso_name,
			)
			return {"repaired": True}

	return {"quarantined": _quarantine(path, name)}


def _quarantine(path: str, name: str) -> str | None:
	"""Move an entry out of the cache. Refuses to overwrite an existing quarantine file."""
	target = os.path.join(repair_quarantine_root(), f"{name}__{now_datetime():%Y%m%d%H%M%S}")
	if os.path.exists(target):
		return None
	try:
		os.rename(path, target)
	except OSError:
		return None
	try:
		os.remove(sidecar_path(path))
	except OSError:
		pass
	return target
