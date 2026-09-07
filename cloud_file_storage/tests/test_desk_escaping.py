"""Every server-supplied value interpolated into Desk HTML is HTML-escaped.

**Why this exists.** The Playwright smoke could not be re-run on this bench, and the security
review's judgement was that the smoke is an evidence gap rather than a security one — every
control it would exercise is server-side and reachable without a browser, and `/api/method/` is
the adversary's path. It named exactly one class a browser covers that unit tests do not:
**rendered-output escaping**. This assertion covers that class deterministically, on every run,
without depending on a Desk that boots. L-4 was a real unescaped interpolation that shipped in
this phase, so the class is not hypothetical.

**It enumerates rather than sampling.** `assertIn("escape_html", source)` and occurrence counts
both pass against the exact tree that shipped L-4, which is what makes them proxies rather than
checks. Every `${...}` in every template literal is found, classified, and must land in one of
the rules below. **An interpolation that matches no rule fails the test** — silently skipping
what the matcher cannot classify would be the same defect one level down.

**What escapes this check** (stated rather than implied — it is a source matcher, not a
renderer, and the edges are real):

* backticks inside `//` and `/* */` comments and inside quoted strings are skipped, so a stray
  one cannot desynchronise the scan — the security review found that hole and its failure
  direction was a **silent pass**. What is still not handled is a backtick inside a *regular
  expression literal*, which JavaScript allows and which this scanner treats as a quote
  boundary only by accident of the surrounding characters; none of the three scripts contains
  one;
* a value passed through an intermediate variable before interpolation is classified on the
  variable, not on its origin — `HTML_FRAGMENT` and `LOCAL` below are exactly that hole, held
  closed by the small size of the allow-lists and by naming each member;
* HTML assembled by string concatenation (`"<b>" + x + "</b>"`) rather than a template literal
  is not seen at all;
* `.html()` called with a value built elsewhere entirely — for example server-rendered markup —
  is not seen;
* the numeric rule asserts that a named field *is* numeric on the server. That is a claim about
  the doctypes, checked by `test_every_numeric_field_is_really_numeric` below rather than
  assumed, but a field that changes type without that test being updated would slip through.

Nested template literals inside an interpolation **are** descended into, so an unescaped value
inside `${cond ? `<b>${x}</b>` : ''}` is reported.
"""

import os
import re
import shutil
import tempfile
import unittest

import cloud_file_storage

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(cloud_file_storage.__file__)))
MODULE = os.path.join(APP_ROOT, "cloud_file_storage", "cloud_file_storage")

SCRIPTS = {
	"settings": os.path.join(MODULE, "doctype", "cloud_storage_settings", "cloud_storage_settings.js"),
	"campaign": os.path.join(MODULE, "doctype", "cloud_migration_campaign", "cloud_migration_campaign.js"),
	"dashboard": os.path.join(MODULE, "page", "cloud_migration_dashboard", "cloud_migration_dashboard.js"),
}

#: Server fields interpolated as bare numbers. The claim is that each is numeric on the server,
#: which `test_every_numeric_field_is_really_numeric` checks against the doctype JSON rather
#: than taking on trust. A string field added here would be caught there.
NUMERIC_FIELDS = frozenset(
	{
		"attachment_bytes",
		"attachment_objects",
		"cache_budget_bytes",
		"cache_bytes",
		"cache_dirty_entries",
		"derived_bytes",
		"derived_objects",
		"linked_files",
		"missing_remote",
		"objects_adopted",
		"objects_cleaned",
		"objects_conflict",
		"objects_dedup_reused",
		"objects_failed",
		"objects_pending",
		"objects_skipped",
		"objects_uploaded",
		"objects_uploading",
		"objects_verified",
		"open_blockers",
		"open_conflicts",
		"operational_bytes",
		"operational_files",
		"orphan_remote",
		"quarantine_bytes",
		"quarantined_objects",
		"remaining",
		"remote_objects",
		"size_mismatch",
		"total_objects",
		"unreferenced_cso",
		"verified_bytes",
		"bytes",
		"files",
	}
)

#: Locals holding HTML this module already built and escaped on the way in. Each is a hole in
#: the matcher (the value is classified here, not at its origin), kept honest by being short
#: and by every producer being one of the `*_html` functions in the same file.
HTML_FRAGMENTS = frozenset({"rows", "cells", "findings", "last", "warnings"})

#: Locals carrying no server data at all.
CLIENT_LOCALS = frozenset({"pct", "stamp", "label", "method"})

#: Functions returning a fixed CSS class from a literal map, never caller data.
CLASS_HELPERS = frozenset({"indicator_for", "backup_indicator"})

#: Functions returning HTML they escaped themselves.
HTML_HELPERS = re.compile(r"^(this\.)?[a-z_]*html\(")

NUMERIC_FORMATTER = re.compile(r"^bytes\(")
FIELD_ACCESS = re.compile(r"^[A-Za-z_][\w.]*(\s*\|\|\s*0)?$")
TRANSLATION = re.compile(r"^__\(")
ESCAPED = re.compile(r"escape_html\(")
URL_ENCODED = re.compile(r"^encodeURIComponent\(")


def interpolations(source: str, base_line: int = 1):
	"""Every `${...}` inside a template literal, descending into nested literals.

	Backticks inside `//` and `/* */` comments and inside `'`/`"` strings are skipped. Without
	that, a single stray backtick in a comment flips `in_template` and desynchronises the scan
	from that point on — and the failure direction is **missed interpolations**, which is a
	silent pass in the check that stands in for a browser-level guarantee. The total-count
	floor catches gross desynchronisation and would not catch a subtle one.
	"""
	found, index, length, in_template = [], 0, len(source), False
	while index < length:
		char = source[index]
		if char == "\\":
			index += 2
			continue
		if not in_template and char == "/" and index + 1 < length:
			if source[index + 1] == "/":
				newline = source.find("\n", index)
				index = length if newline == -1 else newline
				continue
			if source[index + 1] == "*":
				end = source.find("*/", index + 2)
				index = length if end == -1 else end + 2
				continue
		if not in_template and char in "'\"":
			quote, cursor = char, index + 1
			while cursor < length:
				if source[cursor] == "\\":
					cursor += 2
					continue
				if source[cursor] == quote or source[cursor] == "\n":
					break
				cursor += 1
			index = cursor + 1
			continue
		if char == "`":
			in_template = not in_template
			index += 1
			continue
		if in_template and char == "$" and index + 1 < length and source[index + 1] == "{":
			depth, cursor = 1, index + 2
			while cursor < length and depth:
				if source[cursor] == "{":
					depth += 1
				elif source[cursor] == "}":
					depth -= 1
				cursor += 1
			expression = source[index + 2 : cursor - 1]
			line = base_line + source[:index].count("\n")
			found.append((line, " ".join(expression.split())))
			if "`" in expression:
				found.extend(interpolations(expression, line))
			index = cursor
			continue
		index += 1
	return found


def classify(expression: str) -> str | None:
	"""The rule an interpolation satisfies, or None — which is a failure, never a skip."""
	if ESCAPED.search(expression):
		return "ESCAPED"
	if TRANSLATION.match(expression):
		return "TRANSLATION"
	if NUMERIC_FORMATTER.match(expression):
		return "NUMERIC_FORMATTER"
	if URL_ENCODED.match(expression):
		return "URL_ENCODED"
	if HTML_HELPERS.match(expression):
		return "HTML_HELPER"
	bare = expression.split("(")[0].strip()
	if bare in CLASS_HELPERS:
		return "CLASS_HELPER"
	if expression in HTML_FRAGMENTS:
		return "HTML_FRAGMENT"
	if expression in CLIENT_LOCALS:
		return "CLIENT_LOCAL"
	if FIELD_ACCESS.match(expression):
		field = expression.split("||")[0].strip().split(".")[-1]
		if field in NUMERIC_FIELDS:
			return "NUMERIC_FIELD"
	if expression.startswith("findings ||"):
		return "HTML_FRAGMENT"
	return None


def unclassified(path: str):
	with open(path) as handle:
		source = handle.read()
	return [(line, expression) for line, expression in interpolations(source) if classify(expression) is None]


class TestEveryInterpolationIsClassified(unittest.TestCase):
	def test_no_script_interpolates_an_unclassified_value(self):
		for name, path in SCRIPTS.items():
			with self.subTest(script=name):
				offenders = unclassified(path)
				self.assertEqual(
					offenders,
					[],
					f"{os.path.basename(path)} interpolates values this check cannot classify as "
					f"escaped or safe: {offenders}",
				)

	def test_the_scanner_actually_finds_interpolations(self):
		"""Without this, an empty result would read as a clean pass.

		Per-file floor is 1 rather than an arbitrary number: `cloud_migration_campaign.js`
		genuinely has only two interpolations, because it drives dialogs and `msgprint` rather
		than building panels. The total floor is what makes the check meaningful.
		"""
		total = 0
		for name, path in SCRIPTS.items():
			with open(path) as handle:
				found = interpolations(handle.read())
			total += len(found)
			with self.subTest(script=name):
				self.assertGreaterEqual(len(found), 1, "the scanner found nothing in this file")
		self.assertGreater(total, 50, f"the scanner found only {total} interpolations in total")

	def test_it_descends_into_nested_template_literals(self):
		nested = "const x = `<i>${cond ? `<b>${danger}</b>` : ''}</i>`;"
		self.assertIn("danger", [expression for _line, expression in interpolations(nested)])


class TestTheCheckCatchesRealUnescapedValues(unittest.TestCase):
	"""C-20: mutate the artifact under test and run the real assertion against it."""

	def temp_copy(self, path: str, old: str, new: str) -> str:
		with open(path) as handle:
			source = handle.read()
		self.assertIn(old, source, "the line this control mutates is no longer present")
		directory = tempfile.mkdtemp()
		self.addCleanup(shutil.rmtree, directory)
		target = os.path.join(directory, os.path.basename(path))
		with open(target, "w") as handle:
			handle.write(source.replace(old, new))
		return target

	def test_unwrapping_a_currently_escaped_value_is_reported_by_name(self):
		"""A new regression. Fails naming the specific interpolation, not merely failing."""
		path = self.temp_copy(
			SCRIPTS["settings"],
			"${frappe.utils.escape_html(payload.bucket || '')} @",
			"${payload.bucket || ''} @",
		)
		offenders = unclassified(path)
		self.assertIn(
			"payload.bucket || ''",
			[expression for _line, expression in offenders],
			f"unwrapping a known-escaped value was not reported; got {offenders}",
		)

	def test_it_flags_l4s_original_unescaped_form(self):
		"""The real defect this phase shipped, restored verbatim.

		L-4 was `${(local.ignored_doctypes || []).join(', ')}` — a server-supplied list joined
		straight into the panel. A check that cannot catch the one real instance we have is not
		a check, so this asserts against that exact expression rather than a stand-in.
		"""
		with open(SCRIPTS["settings"]) as handle:
			shipped = handle.read()
		wrapped = re.search(
			r"\$\{frappe\.utils\.escape_html\(\s*\(local\.ignored_doctypes \|\| \[\]\)\.join\("
			r"', '\)\s*\)\}",
			shipped,
		)
		self.assertIsNotNone(wrapped, "the L-4 line is no longer in its fixed form")
		path = self.temp_copy(
			SCRIPTS["settings"],
			wrapped.group(0),
			"${(local.ignored_doctypes || []).join(', ')}",
		)
		offenders = unclassified(path)
		self.assertIn(
			"(local.ignored_doctypes || []).join(', ')",
			[expression for _line, expression in offenders],
			f"the check would not have caught L-4; got {offenders}",
		)

	def test_a_stray_backtick_in_a_comment_does_not_hide_a_later_interpolation(self):
		"""The false-pass mode the security review found, closed and then proved closed.

		A backtick in a `//` comment used to flip the scanner's `in_template` state and
		desynchronise every subsequent interpolation — silently, and in the direction of
		missing them. Here a stray backtick is placed in a comment *above* a deliberately
		unescaped interpolation, and the check must still name that interpolation. Written as
		a mutation of the real file run through the real assertion, not a synthetic string.
		"""
		with open(SCRIPTS["settings"]) as handle:
			shipped = handle.read()
		wrapped = re.search(
			r"\$\{frappe\.utils\.escape_html\(payload\.bucket \|\| ''\)\}",
			shipped,
		)
		self.assertIsNotNone(wrapped, "the line this control mutates has moved")

		path = self.temp_copy(
			SCRIPTS["settings"],
			wrapped.group(0),
			"${payload.bucket || ''}",
		)
		with open(path) as handle:
			mutated = handle.read()
		# A stray backtick in a comment, before the unescaped value.
		mutated = mutated.replace(
			"function render_connection(frm, payload) {",
			"// a stray ` backtick in a comment\nfunction render_connection(frm, payload) {",
			1,
		)
		with open(path, "w") as handle:
			handle.write(mutated)

		self.assertIn(
			"payload.bucket || ''",
			[expression for _line, expression in unclassified(path)],
			"a backtick in a comment desynchronised the scan and hid an unescaped value",
		)

	def test_a_backtick_in_a_quoted_string_does_not_desynchronise_it_either(self):
		"""The other spelling of the same hole."""
		source = "const a = 'has a ` backtick'; const b = `<i>${danger}</i>`;"
		self.assertEqual([expression for _line, expression in interpolations(source)], ["danger"])

	def test_the_shipped_tree_passes(self):
		"""The positive control: a matcher that flags everything proves nothing."""
		for name, path in SCRIPTS.items():
			with self.subTest(script=name):
				self.assertEqual(unclassified(path), [])


class TestTheNumericClaimIsChecked(unittest.TestCase):
	"""`NUMERIC_FIELD` asserts a server field is numeric. That claim is verified, not assumed."""

	NUMERIC_TYPES = {"Int", "Long Int", "Float", "Currency", "Percent"}

	def test_every_numeric_field_is_really_numeric(self):
		import json

		import frappe

		checked = 0
		for doctype in ("Cloud Migration Campaign", "Cloud Storage Object"):
			path = os.path.join(MODULE, "doctype", frappe.scrub(doctype), f"{frappe.scrub(doctype)}.json")
			with open(path) as handle:
				fields = json.load(handle)["fields"]
			for field in fields:
				if field["fieldname"] in NUMERIC_FIELDS:
					checked += 1
					with self.subTest(doctype=doctype, field=field["fieldname"]):
						self.assertIn(field["fieldtype"], self.NUMERIC_TYPES)
		self.assertGreater(checked, 8, "the numeric claim was not actually checked against anything")
