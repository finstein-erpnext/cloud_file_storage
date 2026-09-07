"""Background dispatch — everything this app enqueues targets `cloud_migration`.

Migration, repair and GC work must not compete with the ERP's own `short`/`default`/`long`
queues (F3). The queue is a deployment prerequisite documented in
`docs/runbooks/deployment.md`; when it is absent we fall back to `long` **loudly**, because
a silent fallback is exactly how a migration ends up starving interactive work.
"""

import frappe
from frappe.utils.background_jobs import enqueue, get_queue_list

CLOUD_MIGRATION_QUEUE = "cloud_migration"
FALLBACK_QUEUE = "long"


def migration_queue() -> str:
	"""The configured queue, or `long` with a loud warning."""
	try:
		available = get_queue_list()
	except Exception:  # noqa: BLE001 - a Redis hiccup must not stop a maintenance job
		available = []

	if CLOUD_MIGRATION_QUEUE in available:
		return CLOUD_MIGRATION_QUEUE

	frappe.logger("cloud_file_storage").warning(
		f"queue {CLOUD_MIGRATION_QUEUE!r} is not configured on this bench; falling back to "
		f"{FALLBACK_QUEUE!r}. Add it to common_site_config.json 'workers' and restart the "
		"supervisor (see docs/runbooks/deployment.md)."
	)
	return FALLBACK_QUEUE


def enqueue_maintenance(method: str, **kwargs):
	"""Enqueue app maintenance work on the dedicated queue."""
	return enqueue(method, queue=migration_queue(), **kwargs)
