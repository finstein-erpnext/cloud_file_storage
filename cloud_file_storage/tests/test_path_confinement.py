"""Traversal confinement for the cache path builder, and the SQL identifier allow-list.

These exist because Semgrep's `frappe-security-file-traversal` and
`frappe-sql-format-injection` findings are **suppressed per-site** in this codebase. A
suppression is a claim that the construct is safe; these tests are what make that claim
checkable, so the suppression cannot quietly become wrong.

The cache case was **not** a false positive when first examined. `entry_path` was
`os.path.join(cache_root(), f"{file_name}__{cso_name}{ext}")`, which escaped the cache root for
any component containing `..` or a leading `/`. It was safe only because every caller happened
to pass a frappe docname — confinement living in the callers rather than in the function. It is
now enforced twice: components are flattened to basenames with `..` removed, and the resolved
path must sit under the resolved cache root.
"""

import os

import frappe

from cloud_file_storage.cache import materialize
from cloud_file_storage.storage.modes import get_settings
from cloud_file_storage.tests.migration_utils import MigrationTestCase
from cloud_file_storage.tests.utils import CloudStorageTestCase


class TestTheCachePathCannotEscapeItsRoot(CloudStorageTestCase):
	"""The confinement property the `frappe-security-file-traversal` suppressions rely on."""

	#: Relative, absolute, nested, Windows-separator and encoded-ish forms. Each is a distinct
	#: way out of a directory, and a builder that stops one may not stop another.
	ESCAPES = (
		"../../etc/passwd",
		"..",
		"a/../../../tmp/x",
		"/etc/passwd",
		"..\\..\\windows",
		"./../../root",
		"%2e%2e/%2e%2e/etc",
	)

	def test_no_traversal_component_escapes_the_cache_root(self):
		root = os.path.realpath(materialize.cache_root())
		for bad in self.ESCAPES:
			with self.subTest(component=bad):
				resolved = os.path.realpath(materialize.entry_path(bad, "CSO-1"))
				self.assertTrue(
					resolved == root or resolved.startswith(root + os.sep),
					f"{bad!r} resolved to {resolved!r}, outside the cache root {root!r}",
				)

	def test_a_traversal_in_the_object_name_escapes_no_further(self):
		"""Both components are attacker-shaped, not just the first."""
		root = os.path.realpath(materialize.cache_root())
		resolved = os.path.realpath(materialize.entry_path("ok", "../../../etc/shadow"))
		self.assertTrue(resolved.startswith(root + os.sep))

	def test_a_traversal_extension_escapes_no_further(self):
		"""`_extension` derives from the user's own file name, so it is attacker-shaped too."""
		root = os.path.realpath(materialize.cache_root())
		resolved = os.path.realpath(materialize.entry_path("ok", "CSO-1", "../../x"))
		self.assertTrue(resolved.startswith(root + os.sep))

	def test_an_ordinary_entry_is_untouched(self):
		"""The control. A builder that mangled every name would pass the tests above."""
		path = materialize.entry_path("abc123", "CSO-1", ".txt")
		self.assertEqual(os.path.basename(path), "abc123__CSO-1.txt")
		self.assertEqual(os.path.dirname(os.path.realpath(path)), os.path.realpath(materialize.cache_root()))

	def test_the_symlink_check_is_on_the_resolved_path(self):
		"""`realpath` is compared, so a symlinked cache root cannot be used to step outside."""
		root = materialize.cache_root()
		self.assertEqual(
			os.path.realpath(materialize.entry_path("x", "y")),
			os.path.join(os.path.realpath(root), "x__y"),
		)


class TestTheAnalyzerColumnListIsAClosedSet(CloudStorageTestCase):
	"""The property the `frappe-sql-format-injection` suppressions rely on.

	Those queries interpolate identifiers into the SQL text. That is only safe while the
	identifiers come from a closed literal set and can never be caller-supplied — so the closed
	set is asserted here rather than assumed by a reader of the suppression comment.
	"""

	def test_every_interpolated_column_is_a_real_file_column(self):
		from cloud_file_storage.migration import analyzer

		meta = frappe.get_meta("File")
		known = {f.fieldname for f in meta.fields} | {"name", "creation", "modified", "owner"}
		for column in analyzer._file_columns():
			with self.subTest(column=column):
				self.assertIn(
					column,
					known,
					f"{column!r} is interpolated into SQL but is not a File column; an "
					"identifier that is not a real column is either a typo or an injection",
				)

	def test_the_column_list_contains_no_sql_metacharacters(self):
		"""An identifier carrying a backtick, quote, space or semicolon could break out."""
		from cloud_file_storage.migration import analyzer

		for column in analyzer._file_columns():
			with self.subTest(column=column):
				self.assertRegex(
					column,
					r"^[A-Za-z_][A-Za-z0-9_]*$",
					f"{column!r} is not a bare identifier; interpolating it into SQL is unsafe",
				)


class TestCasObjectRefusesUnknownIdentifiers(CloudStorageTestCase):
	r"""F2 from the independent security review: the closed set must be enforced, not assumed.

	`cas_object` builds `SET \`col\`=%(col)s` from its `**values` keys. Every call site passes
	literal keywords, so there was no injection — but CPython permits a non-identifier string
	through `**{...}` unpacking, so the closed set was a **call-site convention** while the
	suppression comment claimed an enforced property. This is the test that makes the claim true.
	"""

	def test_an_unknown_field_is_refused_before_it_reaches_sql(self):
		from cloud_file_storage.migration import engine

		with self.assertRaises(ValueError) as caught:
			engine.cas_object("CFS-OBJ-0001", expected="Pending", to="Uploading", **{"bogus_col": 1})
		self.assertIn("bogus_col", str(caught.exception))

	def test_a_sql_injection_shaped_key_is_refused(self):
		"""The shape that would matter: a key carrying SQL rather than a column name."""
		from cloud_file_storage.migration import engine

		payload = "name`=name, `status"
		with self.assertRaises(ValueError) as caught:
			engine.cas_object("CFS-OBJ-0001", expected="Pending", to="Uploading", **{payload: "x"})
		self.assertIn(payload, str(caught.exception))

	def test_every_field_the_app_actually_passes_is_allowed(self):
		"""The control. An allow-list that refused everything would pass the two tests above."""
		from cloud_file_storage.migration import engine

		used = {
			"attempt_count",
			"cleaned_at",
			"cloud_storage_object",
			"conflict",
			"disk_path",
			"error_class",
			"file_url",
			"last_error",
			"legacy_bucket",
			"legacy_key",
			"md5",
			"next_retry_at",
			"quarantine_path",
			"sha256",
			"size_bytes",
			"skip_reason",
			"uploaded_at",
			"verified_at",
			"status",
		}
		missing = used - engine.ALLOWED_OBJECT_CAS_FIELDS
		self.assertFalse(missing, f"the allow-list would break real call sites: {sorted(missing)}")

	def test_every_allowed_field_is_a_real_column(self):
		"""An allow-listed identifier that is not a column is a typo waiting to reach SQL."""
		from cloud_file_storage.migration import engine

		meta = frappe.get_meta("Cloud Migration Object")
		known = {f.fieldname for f in meta.fields} | {"name", "status", "modified", "owner"}
		for field in sorted(engine.ALLOWED_OBJECT_CAS_FIELDS):
			with self.subTest(field=field):
				self.assertIn(field, known, f"{field!r} is allow-listed but is not a real column")


class TestCleanupRefusesPathsOutsideTheSite(CloudStorageTestCase):
	"""R4 from the independent security review: CLEANUP checked object state, never the path.

	Every gate in `migration/cleanup.py` asked about the *object* — verified remotely, refs
	linked, mode, freshness. None asked about the **path** it was about to unlink. `disk_path`
	and `quarantine_path` are ordinary columns on `Cloud Migration Object`, which grants `write`
	to System Manager and does not mark `disk_path` read-only, so the confinement lived in "the
	analyzer wrote this column" — which is nowhere, exactly as `entry_path` did before it was
	fixed.

	On a multi-site bench that crosses the **site boundary**: another site's attachments, or the
	bench's own app code. Three entry points reach a destructive call — the gate chain, the
	nightly purge scheduler, and restore — and each is checked separately here, because each
	reaches the filesystem without passing through the others.
	"""

	ESCAPES = ("/etc/passwd", "/home/user/v15/apps/frappe/frappe/__init__.py", "../../../etc/hosts")

	def test_a_path_outside_the_site_trees_is_refused(self):
		from cloud_file_storage.migration import cleanup

		for bad in self.ESCAPES:
			with self.subTest(path=bad):
				allowed, reason = cleanup.path_confinement_gate(frappe._dict(disk_path=bad))
				self.assertFalse(allowed, f"{bad!r} was accepted for deletion")
				self.assertIn("outside", reason)

	def test_a_quarantine_path_outside_the_quarantine_root_is_refused(self):
		from cloud_file_storage.migration import cleanup

		allowed, reason = cleanup.path_confinement_gate(
			frappe._dict(
				disk_path=frappe.get_site_path("private", "files", "ok.txt"),
				quarantine_path="/etc/passwd",
			)
		)
		self.assertFalse(allowed)
		self.assertIn("quarantine_path", reason)

	def test_a_legitimate_site_path_is_accepted(self):
		"""The control. A gate that refused everything would pass the two tests above."""
		from cloud_file_storage.migration import cleanup

		for good in (
			frappe.get_site_path("private", "files", "a.txt"),
			frappe.get_site_path("public", "files", "b.txt"),
		):
			with self.subTest(path=good):
				allowed, reason = cleanup.path_confinement_gate(frappe._dict(disk_path=good))
				self.assertTrue(allowed, f"{good!r} was refused: {reason}")

	def test_the_gate_runs_before_any_other_cleanup_gate(self):
		"""Ordering matters: it must refuse before a gate that touches the filesystem."""
		import inspect

		from cloud_file_storage.migration import cleanup

		source = inspect.getsource(cleanup.cleanup_object)
		self.assertLess(
			source.index("path_confinement_gate"),
			source.index("campaign_cleanup_gate"),
			"the path gate must be first in the chain",
		)


class TestTheCasAllowListIsWellFormedAndComplete(CloudStorageTestCase):
	"""R7 + R8: the allow-list needs the same shape assertion as its sibling, and its control
	must be *derived* from the call sites rather than snapshotted.

	R8 matters operationally: several `cas_object` call sites sit inside `except` handlers
	(`engine._open_conflict`, `_record_object_failure`), so a field missing from the allow-list
	turns a handled failure into an unhandled one **in a worker**. A hand-copied control would
	not catch that; deriving the set from the AST does.
	"""

	def test_every_allowed_identifier_is_a_bare_column_name(self):
		"""R7 — mirrors the assertion `_file_columns()` already carries."""
		from cloud_file_storage.migration import engine

		for field in sorted(engine.ALLOWED_OBJECT_CAS_FIELDS):
			with self.subTest(field=field):
				self.assertRegex(
					field,
					r"^[A-Za-z_][A-Za-z0-9_]*$",
					f"{field!r} is not a bare identifier; it is interpolated into SQL",
				)

	def test_the_allow_list_covers_every_keyword_any_call_site_passes(self):
		"""R8 — derived from the call sites by AST, not copied by hand."""
		import ast
		import io
		import os

		from cloud_file_storage.migration import engine

		used = set()
		for root, dirs, files in os.walk(os.path.dirname(os.path.dirname(engine.__file__))):
			dirs[:] = [d for d in dirs if d not in ("tests", "__pycache__")]
			for name in files:
				if not name.endswith(".py"):
					continue
				path = os.path.join(root, name)
				try:
					tree = ast.parse(open(path, encoding="utf8").read())
				except SyntaxError:
					continue
				for node in ast.walk(tree):
					if not isinstance(node, ast.Call):
						continue
					fn = getattr(node.func, "attr", getattr(node.func, "id", None))
					if fn != "cas_object":
						continue
					for kw in node.keywords:
						if kw.arg and kw.arg not in ("expected", "to"):
							used.add(kw.arg)

		missing = used - engine.ALLOWED_OBJECT_CAS_FIELDS
		self.assertFalse(
			missing,
			f"call sites pass field(s) the allow-list refuses: {sorted(missing)} -- several "
			"cas_object calls run inside except handlers, so this would raise from a worker's "
			"error path rather than failing visibly",
		)


class TestUnconfinedPathsAreNeverREAD(CloudStorageTestCase):
	"""The fourth path class, found by independent security review after the delete sinks closed.

	The confinement gate bound where `disk_path` is used to **destroy**. The same Desk-writable
	column is also used to **read**, and those reads *publish*: UPLOAD sends the bytes into the
	bucket as an ordinary, servable object. So an unconfined read is not corruption — it is
	exfiltration. `sites/<site>/site_config.json` carries the database password and the S3
	secret; uploading it would place both in the object store under a normal File.

	The reviewer ranked this at or above the delete, and that is the right ranking: destruction
	is loud and recoverable from backup, silent publication of a credential is neither.
	"""

	def test_the_site_config_path_is_refused_by_the_predicate(self):
		from cloud_file_storage.migration import analyzer

		secret = frappe.get_site_path("site_config.json")
		self.assertFalse(
			analyzer.confined_to_site_files(secret),
			"site_config.json is inside the site but OUTSIDE its file trees; accepting it would "
			"upload the DB password and the S3 secret into the bucket as a servable object",
		)

	def test_paths_outside_the_site_entirely_are_refused(self):
		from cloud_file_storage.migration import analyzer

		for bad in ("/etc/passwd", "/home/user/v15/sites/common_site_config.json", "../../etc/hosts"):
			with self.subTest(path=bad):
				self.assertFalse(analyzer.confined_to_site_files(bad))

	def test_legitimate_file_paths_are_accepted(self):
		"""The control: a predicate that refused everything would pass the two tests above."""
		from cloud_file_storage.migration import analyzer

		for good in (
			frappe.get_site_path("private", "files", "a.txt"),
			frappe.get_site_path("public", "files", "b.txt"),
		):
			with self.subTest(path=good):
				self.assertTrue(analyzer.confined_to_site_files(good), f"{good} was refused")

	def test_a_sibling_prefix_directory_cannot_masquerade(self):
		"""`.../files_evil` must not match `.../files` on a bare startswith."""
		from cloud_file_storage.migration import analyzer

		sibling = frappe.get_site_path("private", "files") + "_evil"
		self.assertFalse(analyzer.confined_to_site_files(sibling + "/x.txt"))

	def test_every_disk_path_read_sink_checks_confinement(self):
		"""Structural: each of the three sinks must call the predicate.

		Named individually rather than grepped as a group, so removing the check from ONE of
		them fails by name instead of leaving the other two to carry a green result.
		"""
		import inspect

		from cloud_file_storage.migration import conflicts, engine, thumbnails

		for label, func in (
			("engine.process_object_upload", engine.process_object_upload),
			("thumbnails._local_thumbnail_bytes", thumbnails._local_thumbnail_bytes),
			("conflicts.relink", conflicts.relink),
		):
			with self.subTest(sink=label):
				self.assertIn(
					"confined_to_site_files",
					inspect.getsource(func),
					f"{label} reads obj.disk_path without confining it -- those bytes are "
					"published, so this is an exfiltration path",
				)


class TestTheRefusalIsRecordedNotJustAvoided(MigrationTestCase):
	"""The assertion that bites: what the refusal RECORDS, not merely that no bytes moved.

	The first version of this guard called `_record_object_failure(obj, camp, error_class=...,
	details=...)`. That function takes `(obj, camp, exc)`. The call raised `TypeError` before it
	could record anything, the blanket per-object handler swallowed it, and the object went back
	to **Pending with backoff** — an attack input retried `max_attempts` times and finally filed
	as a `Warning` named "TypeError", indistinguishable in triage from an ordinary bug.

	The confinement never broke: the `TypeError` was raised before `digest_path`, so the path was
	never read. That is exactly why no existing test caught it — every test asserting *the bytes
	are not uploaded* stays green whether the guard refuses or the handler explodes. It is the
	same shape as the `FakeObjectStore` stub and the lying-HEAD test this delivery has already
	been bitten by twice: an outcome assertion that a second, wrong mechanism also satisfies.

	So this asserts the disposition instead. Mutating the refusal back to the broken call, or to
	a transient failure, fails here even though no byte moves in any of those worlds.
	"""

	def test_a_path_outside_the_site_fails_terminally_as_PathOutsideSite(self):
		import frappe

		from cloud_file_storage.migration import engine

		source = self.local_file(file_name="confinement-probe.txt")
		campaign = self.campaign()
		self.analyze(campaign)
		self.plan(campaign)
		obj = self.object_for(campaign, source.file_url)

		# The exfiltration target itself: the file holding the DB password and the S3 secret.
		outside = frappe.get_site_path("site_config.json")
		self.assertTrue(os.path.exists(outside), "probe requires a real out-of-tree file")
		frappe.db.set_value("Cloud Migration Object", obj.name, "disk_path", outside, update_modified=False)

		camp = frappe.get_doc("Cloud Migration Campaign", campaign)
		fresh = frappe.get_doc("Cloud Migration Object", obj.name)
		moved = engine.process_object_upload(fresh, camp, get_settings(), None)

		self.assertIsNone(moved, "a refused path must move no bytes")

		status = frappe.db.get_value("Cloud Migration Object", obj.name, "status")
		self.assertEqual(
			status,
			"Failed",
			"the object must end TERMINAL. A path outside the site will never become a path "
			f"inside it, so Pending-with-backoff (got {status!r}) is retrying an attack input "
			"and calling the result transient",
		)

		opened = self.conflicts_of(campaign, conflict_type="upload_failed")
		self.assertEqual(len(opened), 1, "exactly one conflict per (object, type)")
		self.assertEqual(
			opened[0]["severity"],
			"Blocker",
			"an operator-supplied path aimed outside the site is not a Warning",
		)
		# `error_class` is written onto the OBJECT row by `_open_conflict` (engine.py:899),
		# not onto the Conflict — assert it where it actually lands.
		self.assertEqual(
			frappe.db.get_value("Cloud Migration Object", obj.name, "error_class"),
			"PathOutsideSite",
			"the recorded class must name the refusal. 'TypeError' here means the guard held "
			"but its handler crashed, which is how this defect originally hid",
		)

	def test_no_object_row_is_left_uploading_after_a_refusal(self):
		"""A refusal that leaks the `Uploading` lease strands the row against every later pass."""
		import frappe

		from cloud_file_storage.migration import engine

		source = self.local_file(file_name="confinement-lease.txt")
		campaign = self.campaign()
		self.analyze(campaign)
		self.plan(campaign)
		obj = self.object_for(campaign, source.file_url)

		frappe.db.set_value(
			"Cloud Migration Object",
			obj.name,
			"disk_path",
			frappe.get_site_path("site_config.json"),
			update_modified=False,
		)
		engine.process_object_upload(
			frappe.get_doc("Cloud Migration Object", obj.name),
			frappe.get_doc("Cloud Migration Campaign", campaign),
			get_settings(),
			None,
		)

		self.assertNotEqual(
			frappe.db.get_value("Cloud Migration Object", obj.name, "status"),
			"Uploading",
			"the refusal returned while still holding the Uploading lease",
		)
