"""A real werkzeug request inside a bench test, for the serving suites.

`bench run-tests` has no HTTP layer: `frappe.local.request` is unset, so anything that
reads `frappe.form_dict`, request headers or `frappe.session` the way the request path does
cannot be exercised at all. :func:`http_request` builds a genuine
`werkzeug.wrappers.Request` and installs it, so the code under test sees the same objects a
real request would, and restores everything afterwards.

What is deliberately NOT faked: the permission gate, `find_file_by_url`, `make_access_log`,
and — when ``run_validate_auth=True`` — `frappe.auth.validate_auth` itself, which resolves
the API key/secret against the database and sets the session user. The one stand-in is
`frappe.local.login_manager`, which `validate_api_key_secret` reads to decide whether the
request is still unauthenticated; a real `LoginManager()` would resume a session out of
Redis and clobber the test's user context. It reports `Guest`, which is what an
unauthenticated request genuinely has at that point, and it cannot fabricate the assertion
that matters (the resolved `frappe.session.user` comes from `tabUser`).
"""

import secrets
from contextlib import contextmanager
from urllib.parse import urlencode

import frappe
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from cloud_file_storage.tests.permission_utils import system_manager_floor_relaxed

_UNSET = object()


@contextmanager
def http_request(
	path: str,
	*,
	user: str = "Administrator",
	headers: dict | None = None,
	query_string: dict | None = None,
	method: str = "GET",
	run_validate_auth: bool = False,
):
	"""Install a real request (and optionally run the real `validate_auth`)."""
	previous_request = getattr(frappe.local, "request", _UNSET)
	previous_form_dict = getattr(frappe.local, "form_dict", _UNSET)
	previous_login_manager = getattr(frappe.local, "login_manager", _UNSET)
	previous_response = getattr(frappe.local, "response", _UNSET)
	previous_user = frappe.session.user

	builder = EnvironBuilder(
		path=path,
		method=method,
		headers=headers or {},
		query_string=urlencode(query_string) if query_string else None,
	)
	request = Request(builder.get_environ())

	frappe.local.request = request
	frappe.local.response = frappe._dict({"docs": []})
	frappe.local.login_manager = frappe._dict(user="Guest")
	# `set_user` resets `local.form_dict` (frappe/__init__.py:642), so the query arguments
	# are installed after it — the same ordering the real request path uses, which is why
	# `validate_api_key_secret` saves and restores `form_dict` around its own `set_user`.
	frappe.set_user(user)
	frappe.local.form_dict = frappe._dict(request.args.to_dict())

	try:
		if run_validate_auth:
			from frappe.auth import validate_auth

			validate_auth()
		yield request
	finally:
		frappe.set_user(previous_user)
		_restore(frappe.local, "request", previous_request)
		_restore(frappe.local, "form_dict", previous_form_dict)
		_restore(frappe.local, "login_manager", previous_login_manager)
		_restore(frappe.local, "response", previous_response)


def _restore(target, attribute, value):
	if value is _UNSET:
		if hasattr(target, attribute):
			delattr(target, attribute)
	else:
		setattr(target, attribute, value)


@contextmanager
def as_user(user: str):
	previous = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(previous)


def make_user(test_case, email: str, roles: tuple[str, ...] = ()) -> "frappe.Document":
	"""A real enabled User, removed again when the test finishes."""
	if frappe.db.exists("User", email):
		with system_manager_floor_relaxed():
			frappe.delete_doc("User", email, force=True, ignore_permissions=True)

	user = frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": email.split("@")[0],
			"send_welcome_email": 0,
			"enabled": 1,
			"roles": [{"role": role} for role in roles],
		}
	).insert(ignore_permissions=True)

	test_case.addCleanup(_delete_user, email)
	return user


def _delete_user(email: str):
	frappe.set_user("Administrator")
	if frappe.db.exists("User", email):
		with system_manager_floor_relaxed():
			frappe.delete_doc("User", email, force=True, ignore_permissions=True)


def make_api_credentials(user: str) -> tuple[str, str]:
	"""Give a User a real api_key/api_secret pair, as `validate_auth` expects to find them."""
	from frappe.utils.password import set_encrypted_password

	api_key = f"cfskey{secrets.token_hex(8)}"
	api_secret = f"cfssecret{secrets.token_hex(8)}"
	frappe.db.set_value("User", user, "api_key", api_key)
	set_encrypted_password("User", user, api_secret, "api_secret")
	return api_key, api_secret


def access_log_count(file_name: str) -> int:
	"""Access Log rows recorded against one File row (contract #10)."""
	return frappe.db.count("Access Log", {"export_from": "File", "reference_document": file_name})
