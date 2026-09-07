"""The three runtime patches, installed once per process and idempotently.

1. **`frappe.utils.response.download_private_file`** → `private.download_private_file_cloud`.
   `frappe/app.py` resolves this through the module attribute at call time and only after
   `validate_auth()`, so the replacement is reached and token clients are already
   authenticated. Proven on the installed frappe by `tests/test_serving_spike.py`.

2. **`frappe.utils.file_manager.get_file_path`** (A36) → a resolver that returns a
   materialized path for cloud-backed rows. Two live bypasses need it, and they need it
   differently:

   * erpnext's EDI readers call `frappe.utils.file_manager.get_file_path(...)`
     (`code_list_import.py:30`, `common_code.py:89`) — a module attribute, so patching the
     frappe symbol covers them;
   * India Compliance does `from frappe.utils.file_manager import get_file_path`
     (`gst_india/utils/__init__.py:26`) — an **import-bound copy** made when its module was
     first imported. Patching frappe's symbol does nothing for it, so the copy is patched
     too, guarded on IC actually being installed.

3. **The dev statics loader** (A24, "P3 spike decides"). `bench serve`'s
   `StaticDataMiddleware.get_directory_loader` raises `NotFound` on a miss
   (`middlewares.py:22-27`), and werkzeug's `SharedDataMiddleware.__call__` only falls
   through to the wrapped app when a loader *returns* no loader — an exception propagates
   as a 404 and the page renderer never runs. The instance's loaders are therefore wrapped
   to return "not handled" instead. Dev-only and guarded: production nginx already falls
   through (`try_files … @webserver`), so nothing is patched there.

Everything is reversible through :func:`uninstall`, which the tests use.
"""

import threading

import frappe

_LOCK = threading.Lock()

_state = {
	"installed": False,
	"download_private_file": None,
	"get_file_path": None,
	"ic_get_file_path": None,
	"dev_statics": None,
}


def ensure_installed():
	"""`before_request` / `before_job` hook. Cheap and idempotent after the first call."""
	if not _state["installed"]:
		with _LOCK:
			if not _state["installed"]:
				_install()

	ensure_india_compliance_patched()


def ensure_india_compliance_patched():
	"""A36 — the IC decision is per SITE, and `_state` is per PROCESS.

	A multi-site worker shares one interpreter, so if this were folded into the one-shot
	`_install()` the first site to serve a request would decide for every site after it: a
	site without India Compliance installed would leave IC's import-bound copy of
	`get_file_path` alone, and a site that HAS IC and arrives second would silently keep
	core's implementation — i.e. its GSTR ingest would open a local path that does not exist
	on an S3_ONLY site, which is the exact bypass A36 exists to close, failing quietly.

	So the question is asked again until the answer is yes. That is cheap
	(`get_installed_apps` is a cached read) and the answer is monotonic: patching is a
	no-op for sites that do not need it, because `get_file_path_cloud` hands anything that
	is not cloud-backed straight back to core.
	"""
	if _state["ic_get_file_path"] is not None:
		return
	with _LOCK:
		if _state["ic_get_file_path"] is not None:
			return
		_patch_india_compliance()


def _install():
	import frappe.utils.file_manager as file_manager_module
	import frappe.utils.response as response_module

	from cloud_file_storage.serving import private

	_state["download_private_file"] = response_module.download_private_file
	response_module.download_private_file = private.download_private_file_cloud

	_state["get_file_path"] = file_manager_module.get_file_path
	file_manager_module.get_file_path = get_file_path_cloud

	_patch_dev_statics()

	_state["installed"] = True


def uninstall():
	"""Put every patched symbol back. Used by tests and by an operator debugging a site."""
	with _LOCK:
		if not _state["installed"]:
			return

		import frappe.utils.file_manager as file_manager_module
		import frappe.utils.response as response_module

		response_module.download_private_file = _state["download_private_file"]
		file_manager_module.get_file_path = _state["get_file_path"]

		if _state["ic_get_file_path"] is not None:
			module, original = _state["ic_get_file_path"]
			module.get_file_path = original
			_state["ic_get_file_path"] = None

		if _state["dev_statics"] is not None:
			middleware, exports = _state["dev_statics"]
			middleware.exports = exports
			_state["dev_statics"] = None

		_state["download_private_file"] = None
		_state["get_file_path"] = None
		_state["installed"] = False


def is_installed() -> bool:
	return _state["installed"]


def original_download_private_file():
	"""Core's implementation — the captured one when patched, the live one when not."""
	if _state["download_private_file"] is not None:
		return _state["download_private_file"]

	import frappe.utils.response as response_module

	return response_module.download_private_file


def original_get_file_path():
	if _state["get_file_path"] is not None:
		return _state["get_file_path"]

	import frappe.utils.file_manager as file_manager_module

	return file_manager_module.get_file_path


# --- A36: the get_file_path bypass -------------------------------------------------------


def get_file_path_cloud(file_name):
	"""`frappe.utils.file_manager.get_file_path`, cloud-aware (A36).

	Core resolves the argument (a File name, a `file_name`, or a path) to a `file_url` and
	then to a filesystem path — which on an S3_ONLY site does not exist, so
	`frappe.get_file_json(get_file_path(path))` in IC's GSTR ingest opens nothing.
	Resolving the File row and returning `get_full_path()` instead materializes the bytes
	and registers the path for write-back (A4).
	"""
	original = original_get_file_path()

	if not file_name or "../" in str(file_name):
		return original(file_name)

	try:
		doc = _find_file_doc(file_name)
		if doc is None:
			return original(file_name)
		return doc.get_full_path()
	except frappe.DoesNotExistError:
		return original(file_name)


#: The only shapes for which the `file_url` arm is consulted (see :func:`_find_file_doc`).
CANONICAL_URL_PREFIXES = ("/files/", "/private/files/")


def _find_file_doc(file_name):
	"""The File row core's own query would pick, as a document.

	Core matches `name == x OR file_name == x` (`file_manager.py:353-358`) and takes the
	first row. A `file_url` is matched too, because callers pass one (erpnext's
	`code_list_import.py:30` passes `file_url` directly) — but ONLY when the argument is
	actually spelled like a canonical URL.

	**That narrowing is a security constraint, not tidiness.** Neither core's
	`get_file_path` nor this replacement performs a permission check, and this one
	materializes the bytes of whatever it resolves, private rows included. Every extra row
	the matcher can reach is therefore a row some caller could be made to read. Core's
	`name|file_name` matcher is the baseline; restricting the added `file_url` arm to
	`/files/…` and `/private/files/…` keeps erpnext's real caller working while leaving
	arbitrary caller-supplied text no more resolvable than it is in core.

	**Do not whitelist a caller that passes user-controlled text to
	`frappe.utils.file_manager.get_file_path`.** There is none today; the audit that keeps
	it that way is `grep -rn "get_file_path(" apps/`.
	"""
	file_table = frappe.qb.DocType("File")
	matcher = (file_table.name == file_name) | (file_table.file_name == file_name)
	if str(file_name).startswith(CANONICAL_URL_PREFIXES):
		matcher = matcher | (file_table.file_url == file_name)

	rows = (
		frappe.qb.from_(file_table)
		.select(file_table.name)
		.where(matcher)
		.where(file_table.is_folder == 0)
		.limit(1)
		.run()
	)
	if not rows:
		return None

	doc = frappe.get_doc("File", rows[0][0])
	# Only take over when there is something to take over: a plain local file goes back to
	# core so behaviour on a LOCAL_ONLY site stays byte-identical.
	resolver = getattr(doc, "_is_cloud_backed", None)
	if resolver is None or not resolver():
		return None
	return doc


def _patch_india_compliance():
	"""Patch IC's import-bound copy of `get_file_path`, when IC is installed."""
	try:
		if "india_compliance" not in frappe.get_installed_apps():
			return
	except Exception:  # noqa: BLE001 - no site context (e.g. a bare import) is not an error
		return

	try:
		from india_compliance.gst_india import utils as ic_utils
	except ImportError:
		return

	if getattr(ic_utils, "get_file_path", None) is None:
		return

	_state["ic_get_file_path"] = (ic_utils, ic_utils.get_file_path)
	ic_utils.get_file_path = get_file_path_cloud


# --- A24: the dev-server statics fall-through --------------------------------------------


def falling_through_loader(loader):
	"""Wrap a `StaticDataMiddleware` loader so a miss falls through instead of 404-ing.

	werkzeug's `SharedDataMiddleware.__call__` calls `self.app(...)` when the loader returns
	no file loader, but a raised `NotFound` propagates straight out as the response. Frappe's
	loader raises. Returning `(None, None)` is the shape werkzeug reads as "not handled".
	"""
	from werkzeug.exceptions import NotFound

	def wrapped(path):
		try:
			return loader(path)
		except NotFound:
			return None, None

	wrapped.cfs_falls_through = True
	return wrapped


def find_static_data_middleware(app, depth: int = 8):
	"""Walk a WSGI middleware chain looking for frappe's `StaticDataMiddleware`."""
	from frappe.middlewares import StaticDataMiddleware

	for _level in range(depth):
		if isinstance(app, StaticDataMiddleware):
			return app
		app = getattr(app, "app", None)
		if app is None:
			return None
	return None


def _patch_dev_statics():
	"""Dev-server only. Production nginx already falls through to the webserver."""
	if not getattr(frappe.local, "dev_server", 0):
		return

	# `import frappe.app` would bind `frappe` as a local name in this function and shadow
	# the module-level import above it.
	from frappe import app as frappe_app

	middleware = find_static_data_middleware(getattr(frappe_app, "application", None))
	if middleware is None:
		return
	if any(getattr(loader, "cfs_falls_through", False) for _prefix, loader in middleware.exports):
		return

	_state["dev_statics"] = (middleware, list(middleware.exports))
	middleware.exports = [(prefix, falling_through_loader(loader)) for prefix, loader in middleware.exports]
