"""Anti-vacuity for the security gate: prove the Semgrep rules still fire on unsafe code.

123 `# nosemgrep` annotations now sit in this codebase, each asserting that a construct is safe.
If a rule silently stopped firing — an upstream ruleset change, a config drift, a broken clone —
every one of those assertions would go unchecked and the gate would report green **for having
detected nothing**. That is the same vacuity this project has found four separate ways in its own
test suite, applied to the security gate itself.

The unsafe examples are written to a **temporary directory at test time** rather than committed.
A committed unsafe fixture would be flagged by the repository's own Semgrep job, so the choice is
between excluding a directory from the scan — which the release contract forbids — and not having
the file in the tree at all. The second is strictly better: nothing is excluded, and the control
still runs.

**It cannot skip in CI.** `CFS_SEMGREP_RULES` is set by the workflow; when that variable is set,
a missing rules directory or a missing binary raises in `setUpClass` rather than skipping — an
independent review found the original guard would have skipped the whole class in CI, because the
linter workflow clones the rules into a *different* job's workspace. A skipped security control
reads as green, which is the vacuity this file exists to prevent, one level up. Only a local run
with the variable unset may skip, and it says so.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

RULES_DIR = os.environ.get("CFS_SEMGREP_RULES", "/tmp/frappe-semgrep-rules/rules")

UNSAFE = """
import frappe


def unsafe_arbitrary_path_read(user_supplied_path):
	with open(user_supplied_path) as handle:
		return handle.read()


def unsafe_user_controlled_sql(table_name, order_by):
	return frappe.db.sql(f"SELECT * FROM `tab{table_name}` ORDER BY {order_by}")


@frappe.whitelist()
def unsafe_request_reachable_impersonation(target_user):
	frappe.set_user(target_user)
	return frappe.session.user
"""

SAFE = """
import os

import frappe

ALLOWED = {"name", "file_name"}


def safe_confined_read(root, entry):
	path = os.path.join(root, os.path.basename(entry).replace("..", ""))
	if not os.path.realpath(path).startswith(os.path.realpath(root) + os.sep):
		raise ValueError("outside root")
	with open(path) as handle:  # nosemgrep: frappe-security-file-traversal
		return handle.read()


def safe_closed_set_sql(key, file_name):
	if key not in ALLOWED:
		raise KeyError(key)
	return frappe.db.sql(  # nosemgrep: frappe-sql-format-injection
		f"SELECT `{key}` FROM `tabFile` WHERE file_name = %(file_name)s",
		{"file_name": file_name},
	)
"""


def _semgrep():
	return shutil.which("semgrep") or shutil.which("semgrep", path=os.path.dirname(os.sys.executable))


#: Set by CI so the control cannot silently skip there. Independent security review found the
#: original guard skipped whenever the default `/tmp` clone was absent -- and the linter workflow
#: clones the rules into a DIFFERENT job's workspace, so in CI the class would have skipped
#: entirely and read as green. A skip that reads as green is the exact vacuity this file exists
#: to prevent, one level up.
_RULES_ENV = "CFS_SEMGREP_RULES"
_RULES_EXPLICIT = os.environ.get(_RULES_ENV)


class TestTheSecurityRulesStillFire(unittest.TestCase):
	"""If these stop firing, every nosemgrep in the tree is unverified."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if _RULES_EXPLICIT:
			# Explicitly configured: a missing path or missing binary is a FAILURE, never a skip.
			if not os.path.isdir(_RULES_EXPLICIT):
				raise AssertionError(
					f"{_RULES_ENV}={_RULES_EXPLICIT!r} is set but is not a directory; the security "
					"anti-vacuity control cannot run and must not be reported as passing"
				)
			if not _semgrep():
				raise AssertionError(
					f"{_RULES_ENV} is set but the semgrep binary is not on PATH; the security "
					"anti-vacuity control cannot run and must not be reported as passing"
				)
		elif not (_semgrep() and os.path.isdir(RULES_DIR)):
			raise unittest.SkipTest(
				f"semgrep or the frappe rules ({RULES_DIR}) unavailable locally, and {_RULES_ENV} "
				"is unset -- set it in CI so this control cannot skip there"
			)

	def _scan(self, source: str) -> set[str]:
		with tempfile.TemporaryDirectory() as tmp:
			path = os.path.join(tmp, "fixture.py")
			with open(path, "w") as handle:
				handle.write(source)
			out = subprocess.run(
				[_semgrep(), "--config", RULES_DIR, "--metrics=off", "--json", path],
				capture_output=True,
				text=True,
				timeout=300,
			)
			# returncode 0 = no findings, 1 = findings. Anything else is a crashed scan, which
			# would otherwise make the NEGATIVE control pass for having found nothing.
			if out.returncode not in (0, 1):
				raise AssertionError(f"semgrep exited {out.returncode}: {(out.stderr or '')[:300]}")
			payload = json.loads(out.stdout or "{}")
			return {r["check_id"].split(".")[-1] for r in payload.get("results", [])}

	def test_arbitrary_path_traversal_is_detected(self):
		self.assertIn(
			"frappe-security-file-traversal",
			self._scan(UNSAFE),
			"the traversal rule is not loaded or no longer fires at all; it matches ANY open(), "
			"so this proves the rule is live rather than that it discriminates -- and if it is "
			"not live, every file-traversal suppression in this codebase is unverified",
		)

	def test_user_controlled_sql_interpolation_is_detected(self):
		self.assertIn(
			"frappe-sql-format-injection",
			self._scan(UNSAFE),
			"the SQL rule stopped firing on caller-supplied identifiers; every "
			"sql-format-injection suppression is now unverified",
		)

	def test_request_reachable_impersonation_is_detected(self):
		self.assertIn(
			"frappe-setuser",
			self._scan(UNSAFE),
			"the set_user rule is not loaded or no longer fires at all; it matches ANY "
			"frappe.set_user(), so the human analysis in the adjudication is the real control "
			"and this only proves the rule is live",
		)

	def test_the_approved_safe_patterns_are_not_blockers(self):
		"""The negative control: the safe shapes must not leave an unexplained finding.

		Without this, a rule that fired on *everything* would satisfy the three tests above.
		"""
		found = self._scan(SAFE)
		for rule in ("frappe-security-file-traversal", "frappe-sql-format-injection"):
			self.assertNotIn(
				rule,
				found,
				f"{rule} fired on the approved safe pattern despite its narrow suppression; "
				"either the annotation no longer attaches or the pattern is not actually safe",
			)
