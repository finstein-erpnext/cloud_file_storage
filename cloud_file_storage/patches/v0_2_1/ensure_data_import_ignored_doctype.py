"""Ensure the operational DocTypes are present on **Cloud Storage Settings** *Ignored DocTypes*.

Older databases may lack the default child rows (for example upgrades from releases
that only seeded them on ``after_install``). Without them, **Data Import** attachments
would be sent to cloud storage once controller-side forcing of that name was removed.

This patch delegates to ``ensure_default_ignored_doctypes`` (idempotent).
"""

from cloud_file_storage.install import ensure_default_ignored_doctypes


def execute():
	ensure_default_ignored_doctypes()
