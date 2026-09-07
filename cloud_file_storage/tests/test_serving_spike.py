"""P3 spike — the private-serving mechanism, proved on the real frappe source.

PLAN.md §B makes this the phase's first deliverable: *"The interception target is not
frozen from documentation — the P3 spike must prove the full chain on each exact supported
revision before freeze."* So this module proves, as tests rather than prose:

1. **Where the dispatch happens.** `frappe/app.py` reaches `download_private_file` through
   a *module attribute*, at call time, so replacing the attribute on
   `frappe.utils.response` is actually reached. If a future frappe rewrote that line as a
   `from … import`, our patch would bind nothing and every private download would silently
   bypass the cloud — :func:`analyze_private_file_dispatch` is what notices.
2. **When it happens.** After `validate_auth()`, so a token/API client is a real user by
   then; and `init_request()` — which runs the `before_request` hooks — happens *before*
   `validate_auth()`, which is exactly why the interception is NOT a `before_request` hook
   (ADR-5). Both are read off the real source, not asserted from the design doc.
3. **What the gate is.** `find_file_by_url(path, name=form_dict.fid)` →
   `is_downloadable()` → `has_permission`, then exactly one `make_access_log` row. The
   characterization tests below drive **unpatched core** so the recorded behaviour is
   frappe's, not ours; `test_serving_private.py` then holds our implementation to it.

The declared compatibility matrix is `{v15.16.0, v15.93.0, version-15}` (PLAN §B, A34);
v15.93.0 is the ref actually installed here. The other refs are NOT left to CI on the
strength of a design document: any ref present in the bench's own frappe checkout is read
back with `git show <ref>:<path>` and analysed exactly like the installed one, which is why
:func:`analyze_private_file_dispatch` never touches `frappe.app` itself. On this bench that
makes the **floor ref provable locally** — v15.16.0 is a real tag here — so A34's claim is
evidence rather than an assumption. A ref that genuinely is not present skips **loudly**,
naming the ref and the command that would make it provable; it never silently passes.
"""

import ast
import inspect
import subprocess
import textwrap
from pathlib import Path

import frappe
import frappe.app
import frappe.utils.response
from frappe.core.doctype.file.utils import find_file_by_url
from frappe.tests.utils import FrappeTestCase
from werkzeug.exceptions import Forbidden

from cloud_file_storage.tests.request_utils import (
	access_log_count,
	as_user,
	http_request,
	make_api_credentials,
	make_user,
)
from cloud_file_storage.tests.utils import CloudStorageTestCase

DISPATCH_TARGET = ("frappe", "utils", "response", "download_private_file")

#: The compatibility matrix PLAN §B pins this spike to.
#: `test_the_declared_matrix_matches_the_plan` is what stops this list and the PLAN drifting
#: apart. Kept on one line on purpose: a name wrapped across two comment lines cannot be
#: found by grep, which is the same defect as naming it wrongly.
DECLARED_FRAPPE_REFS = ("v15.16.0", "v15.93.0", "version-15")

#: A34's floor: `find_file_by_url` and its `name=` (the `fid` query argument) first exist here.
FLOOR_REF = "v15.16.0"

#: The bench's own frappe checkout — where another ref's source can be read without a network.
FRAPPE_REPO = Path(frappe.__file__).resolve().parents[1]


def source_at_ref(ref: str, relative_path: str) -> str | None:
	"""`git show <ref>:<path>` out of the bench's frappe checkout. None when absent.

	This is what lets one bench prove the spike on a ref it does not have installed. It
	reads from git rather than the working tree deliberately: the working tree is the
	installed ref and may carry local bench patches, and the claim being made is about a
	*released* revision.
	"""
	try:
		completed = subprocess.run(
			["git", "-C", str(FRAPPE_REPO), "show", f"{ref}:{relative_path}"],
			capture_output=True,
			text=True,
			timeout=60,
		)
	except (OSError, subprocess.SubprocessError):
		return None
	return completed.stdout if completed.returncode == 0 else None


def analyze_download_private_file(source: str) -> dict:
	"""Read a frappe `utils/response.py` and report the SHAPE of its permission gate.

	`analyze_private_file_dispatch` proves our replacement is reached; this proves what it
	has to replace — the gate whose semantics `serving/private.py` reproduces. Returns
	``{"refuses_guest", "find_file_by_url_kwargs", "access_log_calls",
	"send_private_file_calls"}``.
	"""
	tree = ast.parse(textwrap.dedent(source))

	target = None
	for node in ast.walk(tree):
		if isinstance(node, ast.FunctionDef) and node.name == "download_private_file":
			target = node
			break
	if target is None:
		raise AssertionError("no download_private_file in the given source")

	result = {
		"refuses_guest": False,
		"find_file_by_url_kwargs": (),
		"access_log_calls": 0,
		"send_private_file_calls": 0,
	}

	for node in ast.walk(target):
		if isinstance(node, ast.Constant) and node.value == "Guest":
			result["refuses_guest"] = True
		if not isinstance(node, ast.Call):
			continue
		path = _dotted_name(node.func)
		if not path:
			continue
		if path[-1] == "find_file_by_url":
			result["find_file_by_url_kwargs"] = tuple(kw.arg for kw in node.keywords)
		elif path[-1] == "make_access_log":
			result["access_log_calls"] += 1
		elif path[-1] == "send_private_file":
			result["send_private_file_calls"] += 1

	return result


def function_arguments(source: str, function_name: str) -> list[str] | None:
	"""The positional parameter names of one function in a source string."""
	tree = ast.parse(textwrap.dedent(source))
	for node in ast.walk(tree):
		if isinstance(node, ast.FunctionDef) and node.name == function_name:
			return [argument.arg for argument in node.args.args]
	return None


def analyze_private_file_dispatch(source: str, function_name: str = "application") -> dict:
	"""Read a frappe `app.py` and report how `download_private_file` is reached.

	Returns ``{"attribute_path", "dispatch_lineno", "validate_auth_lineno",
	"init_request_lineno"}``. `attribute_path` is the dotted name the call is made
	through — ``("frappe", "utils", "response", "download_private_file")`` for a module
	attribute lookup, ``("download_private_file",)`` for a locally bound import, and
	``None`` when the call is absent altogether.
	"""
	tree = ast.parse(textwrap.dedent(source))

	target = None
	for node in ast.walk(tree):
		if isinstance(node, ast.FunctionDef) and node.name == function_name:
			target = node
			break
	if target is None:
		raise AssertionError(f"no function named {function_name!r} in the given source")

	result = {
		"attribute_path": None,
		"dispatch_lineno": None,
		"validate_auth_lineno": None,
		"init_request_lineno": None,
	}

	for node in ast.walk(target):
		if not isinstance(node, ast.Call):
			continue
		path = _dotted_name(node.func)
		if not path:
			continue
		if path[-1] == "download_private_file" and result["dispatch_lineno"] is None:
			result["attribute_path"] = path
			result["dispatch_lineno"] = node.lineno
		elif path[-1] == "validate_auth" and result["validate_auth_lineno"] is None:
			result["validate_auth_lineno"] = node.lineno
		elif path[-1] == "init_request" and result["init_request_lineno"] is None:
			result["init_request_lineno"] = node.lineno

	return result


def _dotted_name(node) -> tuple[str, ...] | None:
	parts: list[str] = []
	while isinstance(node, ast.Attribute):
		parts.append(node.attr)
		node = node.value
	if isinstance(node, ast.Name):
		parts.append(node.id)
		return tuple(reversed(parts))
	return None


class TestDispatchAnalyzer(FrappeTestCase):
	"""The analyzer itself, against synthetic sources.

	Without these, `test_dispatch_*` below would be a check that cannot fail for the reason
	it exists: an analyzer that returned `None` for everything would make an "is not a local
	import" assertion pass forever.
	"""

	MODULE_ATTRIBUTE_FORM = """
	def application(request):
		init_request(request)
		validate_auth()
		if request.path.startswith("/private/files/"):
			response = frappe.utils.response.download_private_file(request.path)
	"""

	LOCAL_IMPORT_FORM = """
	def application(request):
		init_request(request)
		validate_auth()
		if request.path.startswith("/private/files/"):
			response = download_private_file(request.path)
	"""

	AUTH_AFTER_DISPATCH_FORM = """
	def application(request):
		init_request(request)
		if request.path.startswith("/private/files/"):
			response = frappe.utils.response.download_private_file(request.path)
		validate_auth()
	"""

	NO_DISPATCH_FORM = """
	def application(request):
		init_request(request)
		validate_auth()
	"""

	def test_recognises_a_module_attribute_dispatch(self):
		result = analyze_private_file_dispatch(self.MODULE_ATTRIBUTE_FORM)
		self.assertEqual(result["attribute_path"], DISPATCH_TARGET)
		self.assertLess(result["validate_auth_lineno"], result["dispatch_lineno"])
		self.assertLess(result["init_request_lineno"], result["validate_auth_lineno"])

	def test_flags_a_locally_bound_import(self):
		"""The form that would silently defeat the patch must be distinguishable."""
		result = analyze_private_file_dispatch(self.LOCAL_IMPORT_FORM)
		self.assertEqual(result["attribute_path"], ("download_private_file",))
		self.assertNotEqual(result["attribute_path"], DISPATCH_TARGET)

	def test_flags_authentication_after_dispatch(self):
		result = analyze_private_file_dispatch(self.AUTH_AFTER_DISPATCH_FORM)
		self.assertGreater(result["validate_auth_lineno"], result["dispatch_lineno"])

	def test_reports_an_absent_dispatch(self):
		result = analyze_private_file_dispatch(self.NO_DISPATCH_FORM)
		self.assertIsNone(result["attribute_path"])
		self.assertIsNone(result["dispatch_lineno"])

	def test_raises_when_the_function_is_gone(self):
		with self.assertRaises(AssertionError):
			analyze_private_file_dispatch("def other(): pass")


class TestDispatchMechanismOnRealFrappe(FrappeTestCase):
	"""FROZEN P3 mechanism — asserted against the installed frappe, not a fixture."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.result = analyze_private_file_dispatch(inspect.getsource(frappe.app))

	def test_dispatch_is_a_module_attribute_lookup(self):
		"""ADR-5's premise: patching the module attribute is reached at call time."""
		self.assertEqual(
			self.result["attribute_path"],
			DISPATCH_TARGET,
			"frappe.app.application no longer calls download_private_file through the "
			"frappe.utils.response module attribute; the runtime patch would bind nothing.",
		)

	def test_dispatch_happens_after_authentication(self):
		"""Token/API clients are real users by the time the patched function runs."""
		self.assertIsNotNone(self.result["validate_auth_lineno"])
		self.assertLess(self.result["validate_auth_lineno"], self.result["dispatch_lineno"])

	def test_before_request_hooks_run_before_authentication(self):
		"""Why the interception is not a `before_request` hook (ADR-5).

		`before_request` is dispatched from inside `init_request`, which runs before
		`validate_auth()` — a token client is still Guest there, so a `before_request`
		interception would 403 every API download.
		"""
		self.assertIsNotNone(self.result["init_request_lineno"])
		self.assertLess(self.result["init_request_lineno"], self.result["validate_auth_lineno"])

		init_request_source = inspect.getsource(frappe.app.init_request)
		self.assertIn("before_request", init_request_source)

	def test_patch_target_is_still_a_module_level_function(self):
		self.assertTrue(callable(frappe.utils.response.download_private_file))
		self.assertEqual(frappe.utils.response.download_private_file.__module__, "frappe.utils.response")

	def test_find_file_by_url_accepts_the_fid_argument(self):
		"""A34: `find_file_by_url` + `name=` (the `fid` query arg) is the v15.16.0 floor.

		Read from the code object, not from :func:`inspect.signature`. On frappe v16 the
		function is annotated ``-> "File" | None`` (frappe/core/doctype/file/utils.py:429),
		which PEP 649 defers and which is invalid when evaluated: `inspect.signature` raises
		``TypeError: unsupported operand type(s) for |: 'str' and 'NoneType'`` under *every*
		`annotation_format` (VALUE, STRING and FORWARDREF all verified on CPython 3.14.7).

		Catching that TypeError and skipping would retire this gate behind a green test —
		the mechanism would keep working while the check that guards it stopped being able
		to fail. `co_varnames` never touches annotations, is identical on v15 and v16, and
		keeps the assertion exact: remove or rename `name` and this still fails.
		"""
		code = find_file_by_url.__code__
		positional = code.co_varnames[: code.co_argcount]
		self.assertEqual(list(positional), ["path", "name"])


class TestRefSourceReader(FrappeTestCase):
	"""The `git show` reader itself.

	Without these, a broken :func:`source_at_ref` — a wrong repo path, a git that is not on
	PATH — would return None for every ref, turn every cross-ref assertion below into a
	skip, and leave the matrix "green" while proving nothing. That is exactly the shape of
	check-that-cannot-fail this project keeps finding, so the reader is tested both ways.
	"""

	def test_it_reads_the_installed_checkout(self):
		source = source_at_ref("HEAD", "frappe/app.py")

		self.assertIsNotNone(source, f"could not read frappe/app.py out of {FRAPPE_REPO}")
		self.assertIn("def application(", source)

	def test_it_returns_none_for_a_ref_that_does_not_exist(self):
		self.assertIsNone(source_at_ref("cfs-no-such-ref", "frappe/app.py"))

	def test_it_returns_none_for_a_path_that_does_not_exist(self):
		self.assertIsNone(source_at_ref("HEAD", "frappe/no_such_module.py"))

	def test_the_analyzer_reads_the_same_mechanism_out_of_git_as_out_of_the_import(self):
		"""Ties the git-read path to the `inspect.getsource` path the frozen tests use."""
		from_git = analyze_private_file_dispatch(source_at_ref("HEAD", "frappe/app.py"))
		from_import = analyze_private_file_dispatch(inspect.getsource(frappe.app))

		self.assertEqual(from_git["attribute_path"], from_import["attribute_path"])
		self.assertEqual(from_git["attribute_path"], DISPATCH_TARGET)


class TestDispatchMechanismAcrossDeclaredRefs(FrappeTestCase):
	"""PLAN §B: "spike matrix green on all refs" — proved here, not deferred to CI.

	The installed ref is covered by `TestDispatchMechanismOnRealFrappe`. This class covers
	every OTHER declared ref that the bench's frappe checkout can produce, which on this
	bench includes the A34 floor. A ref that is not present skips with the command that
	would make it provable — never silently.
	"""

	def _source(self, ref: str, relative_path: str) -> str:
		source = source_at_ref(ref, relative_path)
		if source is None:
			self.skipTest(
				f"frappe ref {ref!r} is not in {FRAPPE_REPO}, so {relative_path} cannot be "
				f"analysed here. Make it provable with: "
				f"git -C {FRAPPE_REPO} fetch --tags origin {ref}. "
				"Until then this ref is covered only by the CI matrix."
			)
		return source

	def test_the_declared_matrix_matches_the_plan(self):
		"""If PLAN §B's matrix changes, this module has to change with it."""
		plan = (Path(__file__).resolve().parents[2] / "docs" / "PLAN.md").read_text(encoding="utf-8")

		self.assertIn("{" + ", ".join(DECLARED_FRAPPE_REFS) + "}", plan)
		self.assertIn(FLOOR_REF, DECLARED_FRAPPE_REFS)

	def test_every_declared_ref_dispatches_through_the_module_attribute(self):
		for ref in DECLARED_FRAPPE_REFS:
			with self.subTest(ref=ref):
				result = analyze_private_file_dispatch(self._source(ref, "frappe/app.py"))

				self.assertEqual(
					result["attribute_path"],
					DISPATCH_TARGET,
					f"frappe {ref} does not reach download_private_file through the "
					"frappe.utils.response module attribute; the runtime patch would bind "
					"nothing on that ref.",
				)
				self.assertLess(result["validate_auth_lineno"], result["dispatch_lineno"])
				self.assertLess(result["init_request_lineno"], result["validate_auth_lineno"])

	def test_the_floor_ref_takes_the_fid_argument(self):
		"""A34's actual premise: `find_file_by_url(path, name)` exists at the floor."""
		source = self._source(FLOOR_REF, "frappe/core/doctype/file/utils.py")

		self.assertEqual(function_arguments(source, "find_file_by_url"), ["path", "name"])

	def test_the_floor_ref_gate_is_the_gate_this_app_reproduces(self):
		"""The whole point of a floor: the semantics `serving/private.py` copies hold there.

		Compared against the installed ref rather than against a literal, so the assertion
		is "the gate did not change across the supported range" — which is the claim A34
		makes and the one that would actually break us.
		"""
		floor = analyze_download_private_file(self._source(FLOOR_REF, "frappe/utils/response.py"))
		installed = analyze_download_private_file(inspect.getsource(frappe.utils.response))

		self.assertTrue(floor["refuses_guest"])
		self.assertEqual(floor["find_file_by_url_kwargs"], ("name",))
		self.assertEqual(floor["access_log_calls"], 1, "exactly one access log is the contract")
		self.assertEqual(floor["send_private_file_calls"], 1)
		self.assertEqual(floor, installed, "the private-serving gate changed within the supported range")


class TestCorePrivateServingCharacterization(CloudStorageTestCase):
	"""What unpatched core actually does — the behaviour our patch must preserve.

	These drive `frappe.utils.response.download_private_file` itself, with a real request
	and a real `validate_auth()`, in LOCAL_ONLY mode so the bytes are genuinely on disk.
	"""

	MODE = "LOCAL_ONLY"

	def setUp(self):
		super().setUp()
		self.owner = make_user(self, "cfs-spike-owner@example.com")
		self.stranger = make_user(self, "cfs-spike-stranger@example.com")

	def _private_file(self, *, file_name="spike-private.txt", content=b"spike", owner=None):
		with as_user(owner or self.owner.name):
			doc = self.make_file(file_name=file_name, content=content, is_private=1)
		return doc

	def _url_sibling(self, source, owner: str):
		"""A second File row at the same `file_url`, genuinely owned by ``owner``.

		Inserted *as* that user rather than with an explicit `owner` field:
		`Document.set_user_and_timestamp` (document.py:598-602) overwrites `owner` with the
		session user on every insert — v15.93.0 has no `in_test` exemption — so passing it
		in the dict silently produces an Administrator-owned row and a permission test that
		proves nothing.
		"""
		with as_user(owner):
			doc = frappe.get_doc(
				{
					"doctype": "File",
					"file_name": source.file_name,
					"file_url": source.file_url,
					"is_private": source.is_private,
					"content_hash": source.content_hash,
					"file_size": source.file_size,
				}
			).insert(ignore_permissions=True)
		self.track(doc)
		self.assertEqual(frappe.db.get_value("File", doc.name, "owner"), owner)
		return doc

	def test_permitted_owner_gets_the_bytes_and_exactly_one_access_log(self):
		doc = self._private_file()

		with http_request(doc.file_url, user=self.owner.name):
			before = access_log_count(doc.name)
			response = frappe.utils.response.download_private_file(doc.file_url)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(access_log_count(doc.name) - before, 1)

	def test_guest_is_forbidden(self):
		doc = self._private_file(file_name="spike-guest.txt")

		with http_request(doc.file_url, user="Guest"):
			before = access_log_count(doc.name)
			with self.assertRaises(Forbidden):
				frappe.utils.response.download_private_file(doc.file_url)
			self.assertEqual(access_log_count(doc.name), before)

	def test_unpermitted_user_is_forbidden_and_writes_no_access_log(self):
		doc = self._private_file(file_name="spike-stranger.txt")

		with http_request(doc.file_url, user=self.stranger.name):
			before = access_log_count(doc.name)
			with self.assertRaises(Forbidden):
				frappe.utils.response.download_private_file(doc.file_url)
			self.assertEqual(access_log_count(doc.name), before)

	def test_a_missing_url_is_forbidden_not_not_found(self):
		"""Core does not distinguish "absent" from "not yours" — no existence oracle."""
		with http_request("/private/files/spike-does-not-exist.txt", user=self.stranger.name):
			with self.assertRaises(Forbidden):
				frappe.utils.response.download_private_file("/private/files/spike-does-not-exist.txt")

	def test_shared_file_url_is_readable_when_any_one_row_is(self):
		"""`find_file_by_url` loops every row sharing the URL and returns the first readable one."""
		mine = self._private_file(file_name="spike-shared.txt", content=b"shared-bytes")
		theirs = self._url_sibling(mine, self.stranger.name)
		# The stranger owns exactly one of the two rows at this URL.
		self.assertNotEqual(mine.name, theirs.name)

		with http_request(mine.file_url, user=self.stranger.name):
			resolved = find_file_by_url(mine.file_url)
		self.assertEqual(resolved.name, theirs.name)

	def test_fid_pins_the_row_and_does_not_fall_back_to_a_readable_sibling(self):
		mine = self._private_file(file_name="spike-fid.txt", content=b"fid-bytes")
		theirs = self._url_sibling(mine, self.stranger.name)

		with http_request(mine.file_url, user=self.stranger.name, query_string={"fid": theirs.name}):
			self.assertEqual(frappe.form_dict.fid, theirs.name)
			self.assertIsNotNone(find_file_by_url(mine.file_url, name=frappe.form_dict.fid))

		# Pinned to the row the stranger cannot read: no silent fallback to their own row.
		with http_request(mine.file_url, user=self.stranger.name, query_string={"fid": mine.name}):
			self.assertIsNone(find_file_by_url(mine.file_url, name=frappe.form_dict.fid))


class TestTokenAuthCharacterization(CloudStorageTestCase):
	"""Token/API auth resolves in `validate_auth()`, i.e. before the dispatch.

	The API key and secret are real rows; the assertion is on `frappe.session.user` after
	`validate_auth()`, a value that comes out of the database.
	"""

	MODE = "LOCAL_ONLY"

	def setUp(self):
		super().setUp()
		self.api_user = make_user(self, "cfs-spike-token@example.com")
		self.api_key, self.api_secret = make_api_credentials(self.api_user.name)

	def test_token_header_authenticates_the_user(self):
		with http_request(
			"/private/files/whatever.txt",
			user="Guest",
			headers={"Authorization": f"token {self.api_key}:{self.api_secret}"},
			run_validate_auth=True,
		):
			self.assertEqual(frappe.session.user, self.api_user.name)

	def test_a_wrong_secret_is_rejected(self):
		with self.assertRaises(frappe.AuthenticationError):
			with http_request(
				"/private/files/whatever.txt",
				user="Guest",
				headers={"Authorization": f"token {self.api_key}:not-the-secret"},
				run_validate_auth=True,
			):
				pass

	def test_token_authenticated_user_downloads_their_own_private_file(self):
		with as_user(self.api_user.name):
			doc = self.make_file(file_name="spike-token.txt", content=b"token-bytes", is_private=1)

		with http_request(
			doc.file_url,
			user="Guest",
			headers={"Authorization": f"token {self.api_key}:{self.api_secret}"},
			run_validate_auth=True,
		):
			self.assertEqual(frappe.session.user, self.api_user.name)
			before = access_log_count(doc.name)
			response = frappe.utils.response.download_private_file(doc.file_url)
			self.assertEqual(response.status_code, 200)
			self.assertEqual(access_log_count(doc.name) - before, 1)
