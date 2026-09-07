"""Mandatory CI must fail legibly rather than stall, and must never wait for a human.

**The incident this comes from.** At candidate `bf0d88a` two mandatory matrix jobs —
`Server (frappe v15.93.0)` and `Server (frappe version-15)` — ran 55.7 and 56.1 minutes against a
legitimate 5.7-12.9, produced no output and no failure, and were cancelled; a re-run of `v15.93.0`
stalled another 37.9.

**The mechanism was never established, and the first diagnosis was wrong.** The stall was initially
attributed to `apt-get install` blocking on its confirmation prompt for want of `-y`. The retained
logs refute that: the stall is inside `apt update`, `apt-get install` never executed (no "Reading
package lists" follows it), and the logs contain neither a confirmation prompt nor a
lock-contention message. Those two hypotheses are excluded; the candidate field is not exhausted —
a network or mirror stall inside `apt update` remains unexcluded and fits every symptom. The last output is at 08:23:15 and the job is killed at 08:59:46 —
thirty-six minutes of silence with no diagnostic. The mirror `Ign:` retries that looked suspicious
appear in the passing jobs too.

So this module guards two independent things, and it is worth being precise about which does what:

* **`timeout-minutes` bounds the observed failure.** Whatever the cause, a job that stops producing
  output now dies in minutes instead of consuming GitHub's six-hour default. This is the guard that
  would have caught the actual incident.
* **Non-interactive apt hardens a real but *separate* risk.** `apt-get install` without `-y` can
  block on "Do you want to continue?" with no stdin to answer it. That did not happen here — but it
  is a latent hang in a mandatory gate, found while investigating, and worth closing on its own.

Both guards are driven with known-bad input below, because a check that cannot fail is worse than
no check.
"""

import os
import re
import unittest

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORKFLOW_DIR = os.path.join(REPO_ROOT, ".github", "workflows")

#: Every workflow here is mandatory: the repo carries no `continue-on-error`, which `ci.yml` states
#: is exactly what "mandatory" means for F1/F5.
#:
#: `-qq` is deliberately NOT accepted although it implies `-y`. apt-get(8) says never to use it
#: without a no-action modifier such as `-d`/`-s`, so accepting it would green-light a form the
#: tool's own manual warns against.
_ASSUME_YES = ("-y", "--yes", "--assume-yes")

#: apt invoked with a subcommand that changes the system, and therefore may prompt.
#:
#: The subcommand is matched *anywhere* after the binary rather than threaded through a pattern for
#: the intervening flags. An earlier form used `(?:[-\w=]+\s+)*`, whose character class has no `:`,
#: so `apt-get -o Acquire::Retries=3 install foo` did not match **at all** — the line was skipped
#: before the assume-yes check ran, and a bare install carrying `-o` options went unreported. That
#: blind spot was live: hardening `apt-get update` with `-o Acquire::…` options is exactly the
#: change that would put such options on an install line next.
_APT_SUBCOMMAND = re.compile(r"\bapt(?:-get)?\b(?P<rest>.*?)\b(install|remove|purge|upgrade|dist-upgrade)\b")

#: Shell separators. A line is split on these so each command is judged on its own flags; before
#: this, `apt-get install -y a && apt-get install b` passed because the assume-yes search scanned
#: the whole line and found the first command's `-y`.
_SEPARATORS = re.compile(r"&&|\|\||;|\|")

#: `bash $GITHUB_WORKSPACE/.github/helper/x.sh`, `bash .github/helper/x.sh`, `./github/helper/x.sh`
#: The optional quote is load-bearing: the real call inside `install.sh` is
#: `bash "$GITHUB_WORKSPACE/.github/helper/install_ecosystem_apps.sh"`, and a pattern that did not
#: allow it matched nothing and went silent — the exact failure mode the file-side test below
#: exists to catch, and did catch on its first run.
_HELPER_REF = re.compile(
	r"""(?:bash|sh|source|\.)\s+["']?(?:\$\{?GITHUB_WORKSPACE\}?/|\./)?(\.github/helper/[\w.-]+\.sh)"""
)


def _workflow_files():
	return [
		os.path.join(WORKFLOW_DIR, n)
		for n in sorted(os.listdir(WORKFLOW_DIR))
		if n.endswith((".yml", ".yaml"))
	]


def _run_steps(path):
	with open(path) as handle:
		doc = yaml.safe_load(handle)
	for job_name, job in (doc.get("jobs") or {}).items():
		for step in job.get("steps") or []:
			if isinstance(step, dict) and step.get("run"):
				yield job_name, step.get("name") or "<unnamed>", step["run"]


def _assumes_yes(command):
	"""Does this single command carry an assume-yes flag?

	Accepts the three long forms and a bundled short cluster containing `y` (`-yq`, `-qy`). Does
	NOT accept `-qq`, which implies `-y` but which apt-get(8) says never to use without a no-action
	modifier — so treating it as satisfying non-interactivity would green-light a form the tool's
	own manual warns against.
	"""
	for flag in _ASSUME_YES:
		if re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", command):
			return True
	# Bundled short flags: `-yq`, `-qy`. `-qq` contains no `y` and is therefore not accepted.
	return any("y" in token[1:] for token in re.findall(r"(?<![\w-])-[a-zA-Z]{2,}(?![\w-])", command))


def interactive_apt_commands(script):
	"""Commands invoking apt in a way that can stop and wait for a human."""
	offenders = []
	for raw in script.splitlines():
		line = raw.strip()
		if not line or line.startswith("#"):
			continue
		for command in _SEPARATORS.split(line):
			command = command.strip()
			if not command or not _APT_SUBCOMMAND.search(command):
				continue
			if not _assumes_yes(command):
				offenders.append(line if len(_SEPARATORS.split(line)) == 1 else command)
	return offenders


def misordered_sudo_env(script):
	"""`VAR=x sudo cmd` — sudo's env_reset strips it, so it reads as protection and provides none."""
	bad = []
	for raw in script.splitlines():
		line = raw.strip()
		if line.startswith("#"):
			continue
		if re.search(r"\b[A-Z_]+=[^\s]+\s+sudo\b", line):
			bad.append(line)
	return bad


def helper_scripts_referenced():
	"""(origin, job, step, relative path, absolute path or None if unresolvable).

	**Transitive.** Helpers invoke helpers: `install.sh` calls `install_ecosystem_apps.sh`, which no
	workflow references directly. Following only the workflow-level invocations left that second
	script unscanned — the guard would have gone quiet on an apt install added there, which is the
	same structural gap one level deeper. The file-side test below is what surfaced it.
	"""
	refs = []
	seen = set()
	frontier = []

	for path in _workflow_files():
		for job, step, script in _run_steps(path):
			for rel in _HELPER_REF.findall(script):
				frontier.append((os.path.basename(path), job, step, rel))

	while frontier:
		origin, job, step, rel = frontier.pop()
		if rel in seen:
			continue
		seen.add(rel)
		abs_path = os.path.join(REPO_ROOT, rel)
		resolved = abs_path if os.path.isfile(abs_path) else None
		refs.append((origin, job, step, rel, resolved))
		if resolved:
			with open(resolved) as handle:
				for nested in _HELPER_REF.findall(handle.read()):
					if nested not in seen:
						frontier.append((rel, job, f"{step} -> {os.path.basename(rel)}", nested))
	return refs


class TestNoMandatoryCICommandCanBlockOnAPrompt(unittest.TestCase):
	def test_every_inline_apt_invocation_assumes_yes(self):
		offenders = [
			f"{os.path.basename(p)} :: {job} :: {step} :: {line}"
			for p in _workflow_files()
			for job, step, script in _run_steps(p)
			for line in interactive_apt_commands(script)
		]

		self.assertEqual(
			offenders, [], "can block on an interactive confirmation:\n  " + "\n  ".join(offenders)
		)

	def test_helper_scripts_invoked_by_ci_are_scanned_and_clean(self):
		"""The gap that mattered: most of the work happens in `.github/helper/*.sh`, not inline.

		Scanning only `run:` blocks made the guard's claim true by what the helpers happened to
		contain rather than by anything it verified.
		"""
		refs = helper_scripts_referenced()

		self.assertTrue(refs, "expected mandatory CI to invoke at least one helper script")

		unresolvable = [f"{w}::{j}::{s} -> {rel}" for w, j, s, rel, ab in refs if ab is None]
		self.assertEqual(
			unresolvable, [], f"helper reference could not be resolved, so it went unscanned: {unresolvable}"
		)

		offenders = []
		for _w, _j, _s, rel, ab in refs:
			with open(ab) as handle:
				for line in interactive_apt_commands(handle.read()):
					offenders.append(f"{rel} :: {line}")

		self.assertEqual(offenders, [], "helper script can block on a prompt:\n  " + "\n  ".join(offenders))

	def test_every_helper_script_in_the_tree_is_actually_reached(self):
		"""Asked from the *file* side, which is the property that matters.

		Reference-side scanning answers "did we find the invocations we recognise?", and goes quiet
		on a form it does not recognise — a bare `"$GITHUB_WORKSPACE/.github/helper/x.sh"` on an
		executable script never becomes a reference, so it never reaches the unresolvable check that
		makes this guard loud. Asking whether every helper on disk is reached instead catches both
		an unrecognised invocation form and a helper that was added but never wired up.
		"""
		helper_dir = os.path.join(REPO_ROOT, ".github", "helper")
		on_disk = {n for n in os.listdir(helper_dir) if n.endswith(".sh")}
		reached = {os.path.basename(rel) for _w, _j, _s, rel, ab in helper_scripts_referenced() if ab}

		self.assertTrue(on_disk, "expected helper scripts in .github/helper/")
		self.assertEqual(
			on_disk - reached,
			set(),
			f"these helper scripts are never reached by a resolved CI reference, so nothing scans "
			f"them: {sorted(on_disk - reached)}",
		)

	def test_the_mariadb_step_is_explicitly_noninteractive_and_correctly_ordered(self):
		seen = 0
		for path in _workflow_files():
			for _job, step, script in _run_steps(path):
				if "mariadb-client" not in script:
					continue
				seen += 1
				self.assertRegex(
					script,
					r"sudo\s+DEBIAN_FRONTEND=noninteractive\s+apt-get\s+install\s+(?:[-\w=]+\s+)*-y\b",
					f"{step}: needs `sudo DEBIAN_FRONTEND=noninteractive apt-get install -y` — the "
					"variable AFTER sudo, or env_reset strips it",
				)
		self.assertGreaterEqual(seen, 3, "expected the matrix, minio and ecosystem install sites")

	def test_no_environment_assignment_precedes_sudo(self):
		bad = [
			f"{os.path.basename(p)} :: {job} :: {line}"
			for p in _workflow_files()
			for job, _step, script in _run_steps(p)
			for line in misordered_sudo_env(script)
		]

		self.assertEqual(bad, [], "sudo's env_reset discards these assignments:\n  " + "\n  ".join(bad))


class TestEveryMandatoryJobIsBounded(unittest.TestCase):
	"""A gate that can stall is worse than one that fails: it consumes hours and reads as slowness."""

	def test_every_job_declares_a_timeout(self):
		missing = []
		for path in _workflow_files():
			with open(path) as handle:
				doc = yaml.safe_load(handle)
			for job_name, job in (doc.get("jobs") or {}).items():
				if not job.get("timeout-minutes"):
					missing.append(f"{os.path.basename(path)}::{job_name}")

		self.assertEqual(
			missing, [], f"these jobs would run to GitHub's six-hour default on a stall: {missing}"
		)

	def test_timeouts_are_bounded_well_below_the_platform_default(self):
		"""Generous enough for runner variance, tight enough that a real regression still fails."""
		for path in _workflow_files():
			with open(path) as handle:
				doc = yaml.safe_load(handle)
			for job_name, job in (doc.get("jobs") or {}).items():
				mins = job.get("timeout-minutes")
				# Asserted, not assumed: a missing value would otherwise raise TypeError here and
				# report as an ERROR rather than as the failure it is. The neighbouring test owns
				# the "declared at all" rule; this one must still fail cleanly if it is absent.
				self.assertIsNotNone(mins, f"{job_name}: no timeout-minutes declared")
				self.assertLessEqual(mins, 60, f"{job_name}: {mins} min is not a meaningful bound")
				self.assertGreaterEqual(mins, 10, f"{job_name}: {mins} min risks failing a healthy run")

	def test_no_workflow_declares_continue_on_error(self):
		soft = []
		for path in _workflow_files():
			with open(path) as handle:
				doc = yaml.safe_load(handle)
			for job_name, job in (doc.get("jobs") or {}).items():
				if job.get("continue-on-error"):
					soft.append(f"{os.path.basename(path)}::{job_name}")
				for step in job.get("steps") or []:
					if isinstance(step, dict) and step.get("continue-on-error"):
						soft.append(f"{os.path.basename(path)}::{job_name}::{step.get('name')}")

		self.assertEqual(soft, [], f"mandatory CI must not soften failures: {soft}")


class TestTheGuardsThemselvesCanFail(unittest.TestCase):
	"""Known-bad input, because a check that cannot fail is worse than no check."""

	def test_it_flags_a_bare_install(self):
		self.assertEqual(
			interactive_apt_commands("sudo apt-get install mariadb-client"),
			["sudo apt-get install mariadb-client"],
		)

	def test_it_flags_the_short_apt_form(self):
		self.assertTrue(interactive_apt_commands("sudo apt install foo"))

	def test_debian_frontend_alone_is_not_enough(self):
		"""It suppresses debconf dialogs, not apt's own confirmation."""
		self.assertTrue(interactive_apt_commands("sudo DEBIAN_FRONTEND=noninteractive apt-get install foo"))

	def test_qq_is_not_accepted_as_assume_yes(self):
		"""It implies -y, but apt-get(8) says never to use it without a no-action modifier."""
		self.assertTrue(interactive_apt_commands("sudo apt-get -qq install foo"))

	def test_it_accepts_the_fixed_form(self):
		self.assertEqual(
			interactive_apt_commands("sudo DEBIAN_FRONTEND=noninteractive apt-get install -y mariadb-client"),
			[],
		)

	def test_an_option_containing_a_colon_does_not_hide_a_bare_install(self):
		"""MEDIUM-13. The blind spot the `apt-get update` hardening walks toward.

		The old inter-flag class `[-\\w=]` had no `:`, so a line carrying `-o Acquire::…` did not
		match the pattern at all and was skipped before the assume-yes check ever ran.
		"""
		self.assertTrue(
			interactive_apt_commands("sudo apt-get -o Acquire::Retries=3 install foo"),
			"an -o option must not stop the guard seeing a bare install",
		)

	def test_an_option_containing_a_colon_still_passes_when_yes_is_present(self):
		self.assertEqual(interactive_apt_commands("sudo apt-get -o Acquire::Retries=3 install -y foo"), [])

	def test_the_hardened_update_line_is_not_a_false_positive(self):
		"""`update` changes no packages and cannot prompt, options or not."""
		self.assertEqual(
			interactive_apt_commands(
				"sudo apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=15 update"
			),
			[],
		)

	def test_bundled_short_flags_containing_y_are_accepted(self):
		"""LOW-22. `-yq` and `-qy` are valid non-interactive forms; flagging them cries wolf."""
		self.assertEqual(interactive_apt_commands("sudo apt-get install -yq foo"), [])
		self.assertEqual(interactive_apt_commands("sudo apt-get install -qy foo"), [])

	def test_each_command_on_a_compound_line_is_judged_separately(self):
		"""LOW-23. Scanning the whole line let the first command's -y cover the second's absence."""
		offenders = interactive_apt_commands("sudo apt-get install -y a && sudo apt-get install b")

		self.assertTrue(offenders)
		self.assertIn("install b", offenders[0])

	def test_it_accepts_the_flag_before_the_subcommand(self):
		self.assertEqual(interactive_apt_commands("sudo apt-get -y install foo"), [])

	def test_it_ignores_comments_and_unrelated_lines(self):
		self.assertEqual(interactive_apt_commands("# apt-get install foo\npip install bar"), [])

	def test_it_does_not_mistake_a_substring_for_the_flag(self):
		self.assertTrue(interactive_apt_commands("sudo apt-get install --dry-run foo"))

	def test_the_sudo_ordering_check_rejects_the_stripped_form(self):
		self.assertEqual(
			misordered_sudo_env("DEBIAN_FRONTEND=noninteractive sudo apt-get install -y foo"),
			["DEBIAN_FRONTEND=noninteractive sudo apt-get install -y foo"],
		)

	def test_the_sudo_ordering_check_accepts_the_correct_form(self):
		self.assertEqual(
			misordered_sudo_env("sudo DEBIAN_FRONTEND=noninteractive apt-get install -y foo"), []
		)

	def test_an_apt_install_added_inside_a_helper_script_is_detected(self):
		"""The structural gap: `install.sh` is where the work is, and it was never scanned.

		Simulates the regression directly — a bare install appended to the helper's real content.
		"""
		helper = os.path.join(REPO_ROOT, ".github", "helper", "install.sh")
		self.assertTrue(os.path.isfile(helper), "install.sh is the helper the workflows invoke")

		with open(helper) as handle:
			real = handle.read()

		self.assertEqual(interactive_apt_commands(real), [], "install.sh is clean today")
		self.assertTrue(
			interactive_apt_commands(real + "\nsudo apt-get install redis-tools\n"),
			"an apt install added to install.sh must be detected",
		)

	def test_an_unresolvable_helper_reference_is_a_failure_not_a_silent_skip(self):
		script = "bash $GITHUB_WORKSPACE/.github/helper/does-not-exist.sh"
		found = _HELPER_REF.findall(script)

		self.assertEqual(found, [".github/helper/does-not-exist.sh"])
		self.assertFalse(os.path.isfile(os.path.join(REPO_ROOT, found[0])))


if __name__ == "__main__":
	unittest.main()
