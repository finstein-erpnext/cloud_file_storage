# Copyright (c) 2026, Finstein and Contributors
# See license.txt

import unittest
from unittest.mock import patch

import frappe

from cloud_file_storage.cloud_file_storage.doctype.cloud_storage_settings.cloud_storage_settings import (
	SYNC_RETRIEVAL_STORAGE_CLASSES,
	CloudStorageSettings,
)


def _conf(**overrides):
	"""Swap frappe.conf wholesale so the validators can be exercised without a site."""
	return patch.object(frappe, "conf", frappe._dict(overrides))


class TestCloudStorageSettings(unittest.TestCase):
	def _make_settings_doc(
		self,
		*,
		use_default_credential_chain=0,
		access_key_id="",
		secret_access_key="",
		endpoint_url="",
		storage_class="STANDARD",
		ignored_doctypes=None,
	):
		doc = frappe._dict(
			{
				"use_default_credential_chain": use_default_credential_chain,
				"access_key_id": access_key_id,
				"endpoint_url": endpoint_url,
				"storage_class": storage_class,
				"ignored_doctypes": ignored_doctypes or [],
			}
		)
		doc.get_password = lambda fieldname, raise_exception=False: (
			secret_access_key if fieldname == "secret_access_key" else ""
		)
		return doc

	def test_validate_credentials_requires_access_key_and_secret_together(self):
		doc = self._make_settings_doc(access_key_id="AKIA123", secret_access_key="")
		with self.assertRaises(frappe.ValidationError):
			CloudStorageSettings.validate_credentials(doc)

		doc = self._make_settings_doc(access_key_id="", secret_access_key="secret123")
		with self.assertRaises(frappe.ValidationError):
			CloudStorageSettings.validate_credentials(doc)

	def test_validate_credentials_accepts_both_or_neither(self):
		doc = self._make_settings_doc(access_key_id="", secret_access_key="")
		CloudStorageSettings.validate_credentials(doc)

		doc = self._make_settings_doc(access_key_id="AKIA123", secret_access_key="secret123")
		CloudStorageSettings.validate_credentials(doc)

	def test_validate_credentials_skipped_on_default_credential_chain(self):
		doc = self._make_settings_doc(use_default_credential_chain=1, access_key_id="AKIA123")
		CloudStorageSettings.validate_credentials(doc)

	def test_validate_endpoint_url_accepts_valid_https_url(self):
		doc = self._make_settings_doc(endpoint_url="https://fsn1.your-objectstorage.com")
		with _conf(allow_tests=False):
			CloudStorageSettings.validate_endpoint_url(doc)

	def test_validate_endpoint_url_rejects_non_https_or_malformed(self):
		doc = self._make_settings_doc(endpoint_url="http://fsn1.your-objectstorage.com")
		with _conf(allow_tests=False):
			with self.assertRaises(frappe.ValidationError):
				CloudStorageSettings.validate_endpoint_url(doc)

			doc = self._make_settings_doc(endpoint_url="not-a-url")
			with self.assertRaises(frappe.ValidationError):
				CloudStorageSettings.validate_endpoint_url(doc)

	def test_validate_endpoint_url_allows_http_when_allow_tests(self):
		"""Local MinIO runs over http; this replaces the fork's inject-settings workaround."""
		doc = self._make_settings_doc(endpoint_url="http://127.0.0.1:9000")
		with _conf(allow_tests=True):
			CloudStorageSettings.validate_endpoint_url(doc)

	def test_validate_endpoint_url_rejects_query_and_fragment(self):
		with _conf(allow_tests=False):
			doc = self._make_settings_doc(endpoint_url="https://fsn1.your-objectstorage.com?x=1")
			with self.assertRaises(frappe.ValidationError):
				CloudStorageSettings.validate_endpoint_url(doc)

			doc = self._make_settings_doc(endpoint_url="https://fsn1.your-objectstorage.com#frag")
			with self.assertRaises(frappe.ValidationError):
				CloudStorageSettings.validate_endpoint_url(doc)

	def test_validate_storage_class_accepts_sync_retrieval_classes(self):
		for storage_class in SYNC_RETRIEVAL_STORAGE_CLASSES:
			with self.subTest(storage_class=storage_class):
				doc = self._make_settings_doc(storage_class=storage_class)
				CloudStorageSettings.validate_storage_class(doc)

	def test_validate_storage_class_rejects_archive_classes(self):
		"""Live objects must stay synchronously retrievable (FD-5)."""
		for storage_class in ("GLACIER", "DEEP_ARCHIVE", "GLACIER_IR", "", None):
			with self.subTest(storage_class=storage_class):
				doc = self._make_settings_doc(storage_class=storage_class)
				with self.assertRaises(frappe.ValidationError):
					CloudStorageSettings.validate_storage_class(doc)

	def test_validate_ignored_doctypes_rejects_duplicates(self):
		doc = self._make_settings_doc(
			ignored_doctypes=[
				frappe._dict({"doctype_name": "Data Import"}),
				frappe._dict({"doctype_name": "Data Import"}),
			]
		)
		with self.assertRaises(frappe.ValidationError):
			CloudStorageSettings.validate_ignored_doctypes(doc)

	def test_validate_ignored_doctypes_accepts_distinct_rows(self):
		doc = self._make_settings_doc(
			ignored_doctypes=[
				frappe._dict({"doctype_name": "Data Import"}),
				frappe._dict({"doctype_name": "Prepared Report"}),
				frappe._dict({"doctype_name": None}),
			]
		)
		CloudStorageSettings.validate_ignored_doctypes(doc)
