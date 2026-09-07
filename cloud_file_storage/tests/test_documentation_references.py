"""Every test this project points AT has to exist (P4 gate finding F5).

Four times now a *reference to* a check has drifted from the check itself, while the check
stayed sound:

* `probe_lock_path`'s docstring named `..._the_same_lockfile_...` for a method spelled
  `..._the_lockfile_...`, which made a real, working guard invisible to a name-based audit
  and got it escalated as a missing test;
* a DECISIONS entry cited `test_a_dirty_entry_still_counts_against_the_budget`, a name from
  a second session's since-removed tests, for a method called
  `test_dirty_entries_count_towards_the_budget`;
* the correction to that entry then cited class `TestEvictionBudget`, which does not exist;
* `test_serving_spike` wrapped a test name across two comment lines, so no grep could find
  it.

The guards are not what rots — the pointers are. So the pointers get a guard of their own:
a name in a docstring, a comment, DECISIONS.md or PROGRESS.md must resolve to a real test
method, and a `Class.method` citation must resolve to that method **on that class**.

Needs no site: everything here is `ast` and text.
"""

import ast
import re
import unittest
from pathlib import Path

import cloud_file_storage

APP_ROOT = Path(cloud_file_storage.__file__).resolve().parent.parent
PACKAGE = APP_ROOT / "cloud_file_storage"

#: Files that may point at a test.
SCANNED = [APP_ROOT / "docs" / "DECISIONS.md", APP_ROOT / "docs" / "PROGRESS.md"]

#: Evidence scripts, added in P8. They pin source lines by literal string, so they go stale in
#: exactly the way this file exists to catch — and did: the P6 mutation script anchored on the
#: lifecycle confirm-phrase guard, security finding L-1 rewrote that line, and by the script's
#: own convention the mutation quietly started reporting NOT APPLIED. Nothing noticed, because
#: this scanner read `.md` and `.py` and the stale pointer was one file type over.
SCANNED += sorted((APP_ROOT / "docs" / "evidence").glob("*.sh"))

#: The evidence helpers are Python but live outside the package, so the `PACKAGE.rglob("*.py")`
#: sweep below reaches neither their citations nor their test definitions. Added for the same
#: reason as the shell scripts: a stale pointer one file type over is still a stale pointer.
SCANNED += sorted((APP_ROOT / "docs" / "evidence").glob("*.py"))

#: A bare `test_…` token only counts as a test-method citation when it has at least this
#: many underscores. Real method names in this suite are sentences; the threshold is what
#: keeps `test_connection` (a Settings method), `test_site` (the CI site) and `test_records`
#: out without needing an ever-growing list of exceptions.
MIN_UNDERSCORES = 4

BARE = re.compile(r"\btest_[a-z0-9_]+\b")
QUALIFIED = re.compile(r"\b(Test[A-Za-z0-9_]*)\.(test_[a-z0-9_]+)\b")

#: Names quoted deliberately as a former or wrong spelling, with the reason. Registering one
#: is the only way to mention a name that does not resolve — and the registration is itself
#: checked both ways below, so this cannot become a dumping ground.
HISTORICAL_NAMES = {
	"test_a_dirty_entry_still_counts_against_the_budget": (
		"quoted in DECISIONS as the stale citation that finding F5 corrected; it belonged to "
		"a second session's tests, which were never merged"
	),
	"test_the_probe_targets_the_same_lockfile_frappe_locks": (
		"quoted in DECISIONS as the misspelling that made a working guard unfindable"
	),
	"test_a_broadcast_would_fail_this_suite": (
		"the N-1 control as first written, quoted by the control that replaced it and by "
		"DECISIONS: it simulated the broadcast mutation inline instead of invoking the real "
		"assertion, so weakening that assertion would have left it green. Replaced by "
		"`test_a_broadcast_makes_the_real_assertion_fail`"
	),
	"test_an_existing_secret_of_our_own_also_permits_the_switch": (
		"quoted in DECISIONS as the assertion that changed direction under reviewer L-9: it "
		"blessed pairing the fork's access key with a secret this app already held, which "
		"authenticates as neither. Replaced by "
		"`test_a_secret_of_our_own_does_NOT_permit_the_switch`"
	),
	"test_a_dump_older_than_the_job_is_refused_early": (
		"not a citation: the name P6's mutation script RENAMES a guard to, to prove the backup "
		"completeness gate fails on a renamed exit-criterion test "
		"(docs/evidence/p6-mutation-evidence.sh). It must NOT exist in this tree"
	),
	"test_surrounding_whitespace_in_the_phrase_is_tolerated": (
		"quoted in DECISIONS as the assertion that changed direction under security finding "
		"L-1: it asserted that the lifecycle confirm phrase tolerated surrounding whitespace, "
		"and the gate is now byte-for-byte like the cleanup one. Replaced by "
		"`test_a_near_miss_is_refused_by_the_confirm_gate`"
	),
	"test_a_guard_nobody_listed": (
		"not a citation: the name of a synthetic test written into a temporary workspace by "
		"`test_backup_gate.TestEveryMarkedGuardIsWatched`, to prove the F-2 walk fails on a "
		"`@refusal_guard` the gate does not name. It must NOT exist in this tree"
	),
	"test_the_health_panel_carries_the_finding": (
		"the ninth instance of the recorded pattern, quoted by the two tests that replaced "
		"it: its name promised the health panel and its body read a key on "
		"`compat.test_connection`'s payload, so the panel's own warning looked covered when "
		"it was not. Renamed to `test_the_connection_report_carries_the_finding`; the panel "
		"is now covered by `test_storage_health.TestPanelWarnings`"
	),
}


def _existing_tests() -> dict[str, set[str]]:
	"""Every test method in the app, as ``{method_name: {owning class, …}}``."""
	found: dict[str, set[str]] = {}
	for path in PACKAGE.rglob("*.py"):
		if "test" not in path.name:
			continue
		try:
			tree = ast.parse(path.read_text(encoding="utf-8"))
		except SyntaxError:  # pragma: no cover - a broken module fails its own run first
			continue
		for node in ast.walk(tree):
			if not isinstance(node, ast.ClassDef):
				continue
			for child in node.body:
				if isinstance(child, ast.FunctionDef) and child.name.startswith("test_"):
					found.setdefault(child.name, set()).add(node.name)
	return found


def _scanned_files() -> list[Path]:
	return [p for p in SCANNED if p.exists()] + sorted(PACKAGE.rglob("*.py"))


class TestEveryCitedTestExists(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.present = _existing_tests()
		cls.files = _scanned_files()

	def test_the_scan_actually_finds_something(self):
		"""A citation checker over an empty corpus passes vacuously and proves nothing."""
		self.assertGreater(len(self.present), 100, "no test methods were parsed out of the app")
		self.assertGreater(len(self.files), 10, "nothing was scanned for citations")

		citations = {
			name
			for path in self.files
			for name in BARE.findall(path.read_text(encoding="utf-8"))
			if name.count("_") >= MIN_UNDERSCORES
		}
		self.assertGreater(len(citations), 20, "no test-name citations were found to check")

	def test_every_cited_test_name_resolves(self):
		unresolved = {}
		for path in self.files:
			text = path.read_text(encoding="utf-8")
			for name in BARE.findall(text):
				if name.count("_") < MIN_UNDERSCORES:
					continue
				if name in self.present or name in HISTORICAL_NAMES:
					continue
				unresolved.setdefault(name, set()).add(path.relative_to(APP_ROOT).as_posix())

		self.assertEqual(
			unresolved,
			{},
			"these documents point at tests that do not exist "
			"(fix the name, or register it in HISTORICAL_NAMES with a reason): "
			+ "; ".join(f"{n} <- {sorted(w)}" for n, w in sorted(unresolved.items())),
		)

	def test_every_qualified_citation_names_the_right_class(self):
		"""`Class.method` has to be true of the class, not just of the method.

		The correction to finding F5 named class `TestEvictionBudget` against a method that is
		really on `TestEvictionReportsWhatItCouldNotFree`: the method existed, the class did
		not, so a method-only check would have waved it through.

		(This docstring was itself the guard's first catch — the original wrapped that
		`Class.method` citation across two lines, which is the fourth defect listed above.)
		"""
		wrong = {}
		for path in self.files:
			text = path.read_text(encoding="utf-8")
			for class_name, method in QUALIFIED.findall(text):
				owners = self.present.get(method)
				if owners is None:
					continue  # covered by the bare-name test, with a better message
				if class_name not in owners:
					wrong.setdefault(f"{class_name}.{method}", set()).add(
						f"{path.relative_to(APP_ROOT).as_posix()} (actually on {sorted(owners)})"
					)

		self.assertEqual(wrong, {}, f"qualified citations naming the wrong class: {wrong}")

	def test_the_historical_allowlist_cannot_rot(self):
		"""Both directions, or the allowlist becomes the place stale names go to hide.

		An entry that is no longer quoted anywhere is dead weight; an entry that resolves to
		a real method is suppressing a citation that should have been checked.
		"""
		self.assertTrue(HISTORICAL_NAMES, "the allowlist is empty; drop it or the tests around it")

		corpus = "\n".join(path.read_text(encoding="utf-8") for path in self.files)
		for name, reason in HISTORICAL_NAMES.items():
			self.assertTrue(reason.strip(), f"{name} is allowlisted without a reason")
			self.assertIn(name, corpus, f"{name} is allowlisted but no longer cited anywhere")
			self.assertNotIn(
				name,
				self.present,
				f"{name} resolves to a real test; remove it from HISTORICAL_NAMES so it is checked",
			)


class TestEveryMutationAnchorStillResolves(unittest.TestCase):
	"""L-8 — the class M-3 exposed, rather than the instance it fixed.

	A mutation script pins a source line by literal string: `s.replace("<that exact line>", …)`.
	When the line moves, the mutation silently stops applying — and by these scripts' own
	convention that is reported as NOT APPLIED, not as a failure. It happened: P8's L-1 fix
	rewrote the lifecycle confirm-phrase guard, and the mutation proving that guard bites
	quietly retired. It was re-anchored by hand, which fixes the instance and leaves the class
	open — the next such edit does the same thing again.

	This walks every `run_mutation` in every evidence script, resolves the file variable from
	the script's own assignments, and asserts the anchor literal is still present in that file.

	The anchors are Python string expressions rather than plain literals — several are built
	with `chr(34)` and `chr(9)*5` to survive two layers of quoting — so they are parsed and
	evaluated as expressions instead of pattern-matched.
	"""

	SCRIPTS = sorted((APP_ROOT / "docs" / "evidence").glob("*.sh"))

	#: `run_mutation "<label>" "$FILEVAR" '<python snippet>' "<test module>"`
	CALL = re.compile(
		r'^run_mutation\s+"(?P<label>[^"]*)"\s+"\$(?P<var>\w+)"\s*\\?\s*\n?\s*\'(?P<snippet>.*?)\'\s',
		re.MULTILINE | re.DOTALL,
	)
	ASSIGNMENT = re.compile(r"^(?P<name>[A-Z_]+)=(?P<value>[^\s\"'$]+)\s*$", re.MULTILINE)

	@staticmethod
	def _evaluate(node) -> str:
		"""Evaluate the anchor expression: literals, `+`, `*` and `chr(n)`."""
		if isinstance(node, ast.Constant):
			return node.value if isinstance(node.value, str) else str(node.value)
		if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
			return TestEveryMutationAnchorStillResolves._evaluate(
				node.left
			) + TestEveryMutationAnchorStillResolves._evaluate(node.right)
		if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
			left, right = node.left, node.right
			if isinstance(right, ast.Constant) and isinstance(right.value, int):
				return TestEveryMutationAnchorStillResolves._evaluate(left) * right.value
			if isinstance(left, ast.Constant) and isinstance(left.value, int):
				return TestEveryMutationAnchorStillResolves._evaluate(right) * left.value
		if (
			isinstance(node, ast.Call)
			and isinstance(node.func, ast.Name)
			and node.func.id == "chr"
			and len(node.args) == 1
			and isinstance(node.args[0], ast.Constant)
		):
			return chr(node.args[0].value)
		raise ValueError(ast.dump(node))

	def anchors(self, script) -> list[tuple[str, str, str]]:
		"""`(label, path, anchor)` for every `s.replace(...)` in the script."""
		text = script.read_text(encoding="utf-8")
		paths = {m.group("name"): m.group("value") for m in self.ASSIGNMENT.finditer(text)}

		found = []
		for call in self.CALL.finditer(text):
			path = paths.get(call.group("var"))
			if not path or not path.endswith((".py", ".json", ".yml", ".sh")):
				continue
			snippet = call.group("snippet")
			try:
				tree = ast.parse(snippet)
			except SyntaxError:
				continue
			for node in ast.walk(tree):
				if (
					isinstance(node, ast.Call)
					and isinstance(node.func, ast.Attribute)
					and node.func.attr == "replace"
					and node.args
				):
					try:
						found.append((call.group("label"), path, self._evaluate(node.args[0])))
					except ValueError:
						continue
		return found

	def test_every_mutation_anchor_is_still_present_in_the_file_it_names(self):
		stale = []
		checked = 0
		for script in self.SCRIPTS:
			for label, path, anchor in self.anchors(script):
				target = APP_ROOT / path
				if not target.exists():
					stale.append(f"{script.name}: {label} -> {path} does not exist")
					continue
				checked += 1
				if anchor not in target.read_text(encoding="utf-8"):
					stale.append(f"{script.name}: {label!r} no longer matches anything in {path}")

		self.assertGreater(checked, 40, "no anchors were parsed; this check would be vacuous")
		self.assertEqual(
			stale,
			[],
			"these mutations would report NOT APPLIED rather than failing — re-anchor them, or "
			"the evidence they produced no longer reproduces:\n  " + "\n  ".join(stale),
		)

	def test_the_anchor_evaluator_handles_the_shapes_the_scripts_use(self):
		"""Anti-vacuity: a parser that silently returned nothing would pass the test above."""
		cases = {
			's = s.replace("plain", "x")': "plain",
			's = s.replace(chr(34) + "quoted" + chr(34), "x")': '"quoted"',
			's = s.replace(chr(9)*2 + "indented", "x")': "\t\tindented",
		}
		for snippet, expected in cases.items():
			with self.subTest(snippet=snippet):
				node = ast.parse(snippet).body[0].value
				self.assertEqual(self._evaluate(node.args[0]), expected)
