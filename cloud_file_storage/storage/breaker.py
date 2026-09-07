"""Redis-cached circuit breaker (A30).

An S3 outage must degrade file operations per mode — it must never degrade the ERP. After
N consecutive transport failures the breaker opens and the request path stops waiting on
timeouts at all: DUAL_WRITE and S3_PRIMARY_LOCAL_FALLBACK go straight to local, S3_ONLY
fails fast with a typed error.

Absence and permission errors do not count: they are answers, not outages
(:func:`cloud_file_storage.storage.exceptions.is_transport_failure`).
"""

import time

import frappe

from cloud_file_storage.storage.exceptions import CloudStorageUnavailableError

FAILURE_KEY = "cfs:breaker:consecutive_failures"
OPEN_UNTIL_KEY = "cfs:breaker:open_until"

#: Consecutive transport failures before the breaker opens.
FAILURE_THRESHOLD = 5
#: How long it stays open before one probe is allowed through.
COOLDOWN_SECONDS = 60
#: Counter TTL: unrelated failures days apart must not add up to an outage.
FAILURE_TTL_SECONDS = 300


def _cache():
	try:
		return frappe.cache()
	except Exception:  # noqa: BLE001 - a cache outage must not break file operations
		return None


def is_open() -> bool:
	"""Whether S3 is presumed down. Fails closed-as-in-usable if Redis is unreachable."""
	cache = _cache()
	if cache is None:
		return False
	try:
		# `expires=True` is load-bearing: without it `get_value` memoises the answer —
		# including a `None` miss — in `frappe.local.cache` for the life of the process, so
		# a worker that consulted a closed breaker once would never see it open again.
		open_until = cache.get_value(OPEN_UNTIL_KEY, expires=True)
	except Exception:  # noqa: BLE001
		return False
	if not open_until:
		return False
	return float(open_until) > time.time()


def assert_closed(operation: str = "S3 operation"):
	if is_open():
		raise CloudStorageUnavailableError(
			f"{operation} skipped: object storage circuit breaker is open (A30)"
		)


def record_success():
	cache = _cache()
	if cache is None:
		return
	try:
		cache.delete_value(FAILURE_KEY)
		cache.delete_value(OPEN_UNTIL_KEY)
	except Exception:  # noqa: BLE001
		pass


def record_failure() -> int:
	"""Count one transport failure; open the breaker at the threshold. Returns the count."""
	cache = _cache()
	if cache is None:
		return 0
	try:
		failures = cache.incr(cache.make_key(FAILURE_KEY))
		cache.expire(cache.make_key(FAILURE_KEY), FAILURE_TTL_SECONDS)
		if failures >= FAILURE_THRESHOLD:
			cache.set_value(OPEN_UNTIL_KEY, time.time() + COOLDOWN_SECONDS, expires_in_sec=COOLDOWN_SECONDS)
			frappe.logger("cloud_file_storage").error(
				f"object storage circuit breaker opened after {failures} consecutive transport failures"
			)
		return int(failures)
	except Exception:  # noqa: BLE001
		return 0


def reset():
	"""Explicit close, for operators and tests."""
	record_success()
