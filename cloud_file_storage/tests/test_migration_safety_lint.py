"""Gate F6 — the two structural safety properties, proved by reading the source.

* **UPLOAD and VERIFY contain no local deletion path at all** (PLAN §C, docs/INVARIANTS.md invariant
  1). Not "no deletion happens at runtime" — no deletion *call exists*, so no future edit
  can reach one without failing this test.
* **Raw SQL against `tabFile` / the Cloud Storage Object table lives at exactly three
  sanctioned audited sites** (invariant 5): the analyzer's SELECTs, the A26 guarded VERIFY
  link, and the external-install compat patch.

Both checks are AST-based rather than textual, for opposite reasons. A grep for `os.remove`
matches this docstring and misses `from os import remove as drop`; a grep for `tabFile`
matches every prose mention in a docstring. The checkers below are exercised against
deliberately-bad synthetic modules further down, so a check that stopped biting fails here
rather than passing quietly.
"""

import ast
import os
import re
import unittest

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Modules that make up UPLOAD and VERIFY. Every one of them is on the path between "the
#: bytes are local" and "the bytes are also remote", and none of them may be able to delete.
NO_DELETION_MODULES = (
	"migration/engine.py",
	"migration/verify.py",
	"migration/adoption.py",
	"migration/thumbnails.py",
	"migration/analyzer.py",
	"migration/planner.py",
)

#: Names that can only mean "remove something", whatever they are called on. `str` has no
#: `unlink` and no `rmtree`, so these need no receiver analysis.
UNAMBIGUOUS_DELETION_NAMES = frozenset(
	{"unlink", "rmtree", "rmdir", "removedirs", "delete_doc", "delete_object", "delete_objects"}
)

#: Names that mean "remove something" only when called on a module that removes things.
#: `str.replace`, `str.remove` on a list and `list.remove` are all ordinary code, so these
#: are matched together with the root of the attribute chain.
RECEIVER_SENSITIVE_NAMES = frozenset({"remove", "rename", "replace", "move", "delete", "truncate"})

#: Roots whose members do remove things: the filesystem modules, frappe's own delete APIs
#: and the storage engine (whose `delete` is the physical S3 delete, reachable only from GC).
DANGEROUS_ROOTS = frozenset(
	{"os", "shutil", "pathlib", "Path", "frappe", "engine", "storage_engine", "client", "s3", "bucket"}
)

DELETION_NAMES = UNAMBIGUOUS_DELETION_NAMES | RECEIVER_SENSITIVE_NAMES

#: The three sites docs/INVARIANTS.md invariant 5 sanctions, relative to the package root.
SANCTIONED_SQL_SITES = frozenset(
	{
		"migration/analyzer.py",
		"migration/verify.py",
		"patches/v0_2_0/backfill_s3_object_key.py",
	}
)

GUARDED_TABLES = ("tabFile", "tabCloud Storage Object")

WRITE_VERBS = ("insert into", "update ", "delete from", "replace into", "truncate")


def _module_paths() -> list[str]:
	paths = []
	for root, _dirs, files in os.walk(APP_ROOT):
		if f"{os.sep}tests" in root or "__pycache__" in root:
			continue
		for name in files:
			if name.endswith(".py"):
				paths.append(os.path.join(root, name))
	return sorted(paths)


def _relative(path: str) -> str:
	return os.path.relpath(path, APP_ROOT).replace(os.sep, "/")


def _parse(path: str) -> ast.Module:
	with open(path) as handle:
		return ast.parse(handle.read(), filename=path)


def _attribute_root(node: ast.AST) -> str | None:
	"""The leftmost name of a pure attribute chain (`os.path.join` → `os`), else None.

	Returning None for anything else is what lets `os.path.relpath(...).replace("/", "_")`
	through while still catching `os.replace(a, b)`: the first is a method on the *result* of
	a call, so the chain is not pure and there is no module root to be dangerous.
	"""
	while isinstance(node, ast.Attribute):
		node = node.value
	return node.id if isinstance(node, ast.Name) else None


def find_deletion_calls(tree: ast.Module) -> list[str]:
	"""Every call or import that could remove or move something."""
	found: list[str] = []
	aliased: set[str] = set()

	for node in ast.walk(tree):
		if isinstance(node, ast.ImportFrom) and node.module in ("os", "shutil", "pathlib"):
			for alias in node.names:
				if alias.name in DELETION_NAMES:
					aliased.add(alias.asname or alias.name)
					found.append(f"line {node.lineno}: from {node.module} import {alias.name}")

	for node in ast.walk(tree):
		if not isinstance(node, ast.Call):
			continue

		target = node.func
		if isinstance(target, ast.Name):
			if target.id in aliased or target.id in UNAMBIGUOUS_DELETION_NAMES:
				found.append(f"line {node.lineno}: call to {target.id}()")
			continue

		if not isinstance(target, ast.Attribute):
			continue

		if target.attr in UNAMBIGUOUS_DELETION_NAMES:
			found.append(f"line {node.lineno}: call to {target.attr}()")
			continue

		if target.attr in RECEIVER_SENSITIVE_NAMES:
			root = _attribute_root(target)
			if root in DANGEROUS_ROOTS:
				found.append(f"line {node.lineno}: call to {root}.….{target.attr}()")

	return found


#: Emitted in place of an f-string slot the checker cannot resolve to a literal. A statement
#: carrying it is reported rather than cleared: the interpolated token is exactly what the
#: guard is about, and "I could not read it" must not be recorded as "it is safe".
UNRESOLVED = "<unresolved-interpolation>"


def _string_arguments(node: ast.Call, constants: dict[str, str]) -> list[str]:
	"""The literal text of a `sql()` call's query, however it was spelled.

	Three forms are read, and the third is the one that mattered: a plain string, the
	`query=` keyword form, and an f-string. For an f-string the **interpolated slots are
	resolved** against module-level string constants — ``f"UPDATE `tab{OBJECT_DOCTYPE}`"`` is
	perfectly readable when `OBJECT_DOCTYPE` is a module constant — and anything that cannot
	be resolved becomes :data:`UNRESOLVED`, which makes the statement reportable.

	The previous version joined only the `ast.Constant` parts and threw the slots away, so
	``f"SELECT COUNT(*) FROM `{table}`"`` read as `SELECT COUNT(*) FROM ``` — the checker
	discarding the one token it exists to guard.
	"""
	candidates = list(node.args[:1])
	candidates += [kw.value for kw in node.keywords if kw.arg in ("query", "sql")]

	texts: list[str] = []
	for argument in candidates:
		if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
			texts.append(argument.value)
		elif isinstance(argument, ast.JoinedStr):
			texts.append(_render_joined_string(argument, constants))
		elif isinstance(argument, ast.Name):
			texts.append(f"<name:{argument.id}>")
		else:
			texts.append(UNRESOLVED)
	return texts


def _render_joined_string(node: ast.JoinedStr, constants: dict[str, str]) -> str:
	parts: list[str] = []
	for value in node.values:
		if isinstance(value, ast.Constant) and isinstance(value.value, str):
			parts.append(value.value)
		elif isinstance(value, ast.FormattedValue):
			inner = value.value
			if isinstance(inner, ast.Name) and inner.id in constants:
				parts.append(constants[inner.id])
			else:
				parts.append(UNRESOLVED)
		else:
			parts.append(UNRESOLVED)
	return "".join(parts)


def names_guarded_table(text: str) -> bool:
	"""Whether a rendered statement names `tabFile` or the Cloud Storage Object table."""
	return any(table in text for table in GUARDED_TABLES)


def has_unreadable_table_name(text: str) -> bool:
	"""Whether an unresolved slot sits **in a table-name position**.

	The distinction matters, and getting it wrong in either direction is bad: reporting every
	unresolved interpolation flags `VALUES {placeholders}`, which says nothing about which
	table is written and would push honest bulk-insert code onto the sanctioned list;
	reporting none of them is the blind spot that let a COUNT against the Cloud Storage
	Object table through. A slot inside backticks is an identifier — that is the one the
	guard cannot afford not to read.
	"""
	for span in re.findall(r"`([^`]*)`", text):
		if UNRESOLVED in span:
			return True
	return False


def find_guarded_table_sql(tree: ast.Module) -> list[str]:
	"""Raw SQL statements that name a guarded table, or whose table name cannot be read.

	Resolves the direct form, the module-constant form (`frappe.db.sql(LINK_SQL, …)`) and
	f-strings whose slots are module-level constants. A statement whose **table name** cannot
	be resolved is reported as though it named a guarded table, because the checker cannot
	show that it does not — the conservative direction for a rule whose whole job is that no
	fourth site exists.
	"""
	constants: dict[str, str] = {}
	for node in ast.walk(tree):
		if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
			if isinstance(node.value.value, str):
				for target in node.targets:
					if isinstance(target, ast.Name):
						constants[target.id] = node.value.value

	statements: list[str] = []
	for node in ast.walk(tree):
		if not isinstance(node, ast.Call):
			continue
		func = node.func
		if not (isinstance(func, ast.Attribute) and func.attr in ("sql", "multisql", "sql_list")):
			continue

		for text in _string_arguments(node, constants):
			if text.startswith("<name:"):
				text = constants.get(text[6:-1], "")
			if names_guarded_table(text) or has_unreadable_table_name(text):
				statements.append(text)

	return statements


class TestNoDeletionInUploadOrVerify(unittest.TestCase):
	"""PLAN §C / F6 — the modules that move bytes upward cannot remove anything."""

	def test_the_upload_and_verify_modules_contain_no_deletion_call(self):
		offences = {}
		for relative in NO_DELETION_MODULES:
			path = os.path.join(APP_ROOT, relative)
			self.assertTrue(os.path.exists(path), f"{relative} does not exist — has it been renamed?")
			found = find_deletion_calls(_parse(path))
			if found:
				offences[relative] = found
		self.assertEqual(offences, {}, f"deletion calls in UPLOAD/VERIFY modules: {offences}")

	def test_cleanup_is_the_module_that_does_delete(self):
		"""The counterpart assertion: without it the check above could pass by testing nothing."""
		path = os.path.join(APP_ROOT, "migration/cleanup.py")
		found = find_deletion_calls(_parse(path))
		self.assertTrue(found, "cleanup.py has no deletion call at all — the F6 check is vacuous")

	def test_the_checker_catches_a_plain_os_remove(self):
		tree = ast.parse("import os\n\ndef f(path):\n    os.remove(path)\n")
		self.assertTrue(find_deletion_calls(tree))

	def test_the_checker_catches_an_aliased_import(self):
		tree = ast.parse("from os import rename\n\ndef f(a, b):\n    rename(a, b)\n")
		self.assertTrue(find_deletion_calls(tree))

	def test_the_checker_ignores_a_mention_in_a_docstring(self):
		tree = ast.parse('"""This module never calls os.remove or shutil.move."""\n\nx = 1\n')
		self.assertEqual(find_deletion_calls(tree), [])

	def test_the_checker_ignores_string_and_list_methods_of_the_same_name(self):
		"""`str.replace` and `list.remove` are ordinary code and must not read as deletions."""
		tree = ast.parse(
			"import os\n\n"
			"def f(path, items):\n"
			"    text = os.path.relpath(path, '/').replace(os.sep, '/')\n"
			"    items.remove(text)\n"
			"    return text\n"
		)
		self.assertEqual(find_deletion_calls(tree), [])

	def test_the_checker_still_catches_os_replace(self):
		"""…while `os.replace` is a destructive rename and must still be caught."""
		tree = ast.parse("import os\n\ndef f(a, b):\n    os.replace(a, b)\n")
		self.assertTrue(find_deletion_calls(tree))


class TestSanctionedRawSql(unittest.TestCase):
	"""Invariant 5 — three audited sites, and no fourth."""

	def test_only_the_sanctioned_modules_carry_raw_sql_against_the_guarded_tables(self):
		offenders = {}
		for path in _module_paths():
			relative = _relative(path)
			statements = find_guarded_table_sql(_parse(path))
			if statements and relative not in SANCTIONED_SQL_SITES:
				offenders[relative] = statements
		self.assertEqual(offenders, {}, f"unsanctioned raw SQL against tabFile/CSO: {offenders}")

	def test_each_sanctioned_site_actually_contains_the_sql_it_is_sanctioned_for(self):
		"""An allowlist that names a site with no SQL is an allowlist nobody is maintaining."""
		for relative in SANCTIONED_SQL_SITES:
			path = os.path.join(APP_ROOT, relative)
			self.assertTrue(
				find_guarded_table_sql(_parse(path)),
				f"{relative} is on the sanctioned list but has no raw SQL against the guarded tables",
			)

	def test_the_analyzer_only_reads(self):
		"""Two of the three sites are SELECTs; the analyzer never writes to `tabFile`."""
		path = os.path.join(APP_ROOT, "migration/analyzer.py")
		# Only statements that genuinely name a guarded table: a conservatively-reported
		# unresolved slot says nothing about whether `tabFile` is being written.
		for statement in [s for s in find_guarded_table_sql(_parse(path)) if names_guarded_table(s)]:
			lowered = " ".join(statement.lower().split())
			for verb in WRITE_VERBS:
				self.assertNotIn(verb, lowered, f"the analyzer writes to a guarded table: {statement[:120]}")

	def test_the_verify_link_carries_every_a26_guard(self):
		from cloud_file_storage.migration import verify

		statement = " ".join(verify.LINK_SQL.lower().split())
		self.assertIn("update `tabfile` f", statement)
		self.assertIn("f.cloud_storage_object is null or f.cloud_storage_object = %(cso)s", statement)
		self.assertIn("f.file_url = r.file_url", statement)
		self.assertIn("f.content_hash is null or f.content_hash =", statement)

	def test_the_checker_catches_an_unsanctioned_update(self):
		tree = ast.parse('import frappe\n\ndef f():\n    frappe.db.sql("UPDATE `tabFile` SET x=1")\n')
		self.assertTrue(find_guarded_table_sql(tree))

	def test_an_unresolvable_interpolation_is_reported_not_cleared(self):
		"""The blind spot that let a COUNT against the CSO table through.

		``f"SELECT COUNT(*) FROM `{table}`"`` used to render as `SELECT COUNT(*) FROM ``,
		with the interpolated token — the one thing the guard is about — discarded. A slot the
		checker cannot resolve now makes the statement reportable.
		"""
		tree = ast.parse(
			"import frappe\n\n"
			"def f(doctype):\n"
			'    table = "tab" + doctype\n'
			'    frappe.db.sql(f"SELECT COUNT(*) FROM `{table}`")\n'
		)
		self.assertTrue(find_guarded_table_sql(tree))

	def test_an_f_string_over_a_module_constant_is_resolved(self):
		"""…while a slot it CAN read is read, so the engine's own migration SQL stays clean."""
		safe = ast.parse(
			'import frappe\n\nOBJECT_DOCTYPE = "Cloud Migration Object"\n\n'
			'def f():\n    frappe.db.sql(f"UPDATE `tab{OBJECT_DOCTYPE}` SET x=1")\n'
		)
		self.assertEqual(find_guarded_table_sql(safe), [])

		guarded = ast.parse(
			'import frappe\n\nDT = "File"\n\ndef f():\n    frappe.db.sql(f"UPDATE `tab{DT}` SET x=1")\n'
		)
		self.assertTrue(find_guarded_table_sql(guarded))

	def test_the_checker_catches_the_query_keyword_form(self):
		"""`frappe.db.sql(query=…)` is the same statement and must not slip past."""
		tree = ast.parse('import frappe\n\ndef f():\n    frappe.db.sql(query="UPDATE `tabFile` SET x=1")\n')
		self.assertTrue(find_guarded_table_sql(tree))

	def test_the_checker_resolves_sql_held_in_a_module_constant(self):
		tree = ast.parse(
			'import frappe\n\nQ = "UPDATE `tabFile` SET x=1"\n\ndef f():\n    frappe.db.sql(Q)\n'
		)
		self.assertTrue(find_guarded_table_sql(tree))

	def test_the_checker_ignores_migration_table_sql(self):
		tree = ast.parse(
			'import frappe\n\ndef f():\n    frappe.db.sql("UPDATE `tabCloud Migration Object` SET x=1")\n'
		)
		self.assertEqual(find_guarded_table_sql(tree), [])


class TestNoAclAnywhere(unittest.TestCase):
	"""F6 — no `ACL` key in any S3 call, asserted across the whole package."""

	def test_no_module_passes_an_acl_key(self):
		offenders = []
		for path in _module_paths():
			tree = _parse(path)
			for node in ast.walk(tree):
				if isinstance(node, ast.Constant) and node.value == "ACL":
					offenders.append(f"{_relative(path)}:{node.lineno}")
				if isinstance(node, ast.keyword) and node.arg == "ACL":
					offenders.append(f"{_relative(path)}:{node.value.lineno}")
		self.assertEqual(offenders, [], f"an ACL key reached an S3 call: {offenders}")


class TestEveryManualCommitIsAccountedFor(unittest.TestCase):
	"""R9 — the review record for manual commits, enforced instead of narrated.

	86 `frappe.db.commit()` calls carry a `# nosemgrep: frappe-manual-commit` annotation naming
	the invariant that requires them. **13 do not**, and the reason is a property of the rule
	rather than of the code: `frappe_correctness.yml` has `pattern-not-inside: try/except`, so a
	commit inside a `try` is structurally invisible to Semgrep and an annotation there suppresses
	nothing. The 1:1 hygiene sweep therefore removed them — correctly for the tool, and at the
	cost of the review record.

	The independent reviewer's call, adopted: **do not add thirteen comments.** Prose at thirteen
	hot-path sites is thirteen more things to drift, and this delivery has watched prose drift
	away from the tree four separate times. Record it once, enforce it once.

	So: every `frappe.db.commit()` in shipping code must be **either** annotated **or** lexically
	inside a `try`. A new unannotated commit outside a `try` cannot appear without failing here.
	"""

	def test_every_shipping_commit_is_annotated_or_inside_a_try(self):
		import ast
		import io
		import os

		import cloud_file_storage

		root = os.path.dirname(cloud_file_storage.__file__)
		offenders = []
		for dirpath, dirnames, filenames in os.walk(root):
			# Mirror the RULE's own exclusions (frappe_correctness.yml:213-216 excludes
			# `**/patches/**` and `**/demo/**`). Without this the two controls oscillate over a
			# single line: this test demands an annotation there, the rule covers no finding
			# there, so the 1:1 suppression-hygiene sweep strips the annotation as dead, and this
			# test fails again. Both controls must agree about the same directories or neither is
			# stable. Independent security review caught this before it could cycle once.
			dirnames[:] = [d for d in dirnames if d not in ("tests", "__pycache__", "patches", "demo")]
			for name in filenames:
				if not name.endswith(".py"):
					continue
				path = os.path.join(dirpath, name)
				source = open(path, encoding="utf8").read()
				lines = source.split("\n")
				try:
					tree = ast.parse(source)
				except SyntaxError:
					continue

				inside_try = set()
				for node in ast.walk(tree):
					if isinstance(node, ast.Try):
						for child in ast.walk(node):
							if hasattr(child, "lineno"):
								inside_try.add(child.lineno)

				for node in ast.walk(tree):
					if not isinstance(node, ast.Call):
						continue
					func = node.func
					if getattr(func, "attr", None) != "commit":
						continue
					line = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
					if "nosemgrep" in line or node.lineno in inside_try:
						continue
					offenders.append(f"{os.path.relpath(path, root)}:{node.lineno}")

		self.assertFalse(
			offenders,
			"manual commit(s) neither annotated nor inside a try/except: "
			f"{sorted(offenders)}. Annotate with the invariant that requires the commit "
			"(hard invariant 7 for hook/worker paths; durability-before-enqueue for endpoints), "
			"or explain why no commit is needed here.",
		)
