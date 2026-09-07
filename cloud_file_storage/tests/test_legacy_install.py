"""The external-install bootstrap (A21), and the guard that has to stay closed.

The end-to-end proof of the *whole* bootstrap is not here: it is a real
`bench cfs-adopt-legacy-install` followed by a real `bench migrate` on a site seeded from
`tests/legacy_fixture.py`, recorded in `docs/evidence/p8-legacy-install-rehearsal.md`.

**An earlier version of this docstring justified that with "the bootstrap commits its own
transaction by design", and applied it to the whole module. That was true of exactly two
functions and false of the rest** — `bootstrap()` and `finish()` commit; `map_settings`,
`harvest_settings`, `adopt_module_def`, `adopt_ignored_doctypes` and `_is_untouched` do not.
A true-sounding reason applied past its scope is why the credential fix — the one defect in
this phase that can destroy an operator's S3 secret — sat for a whole phase on a line no unit
test reached, and why every gate accepted that (P8 reviewer, H-1).

So the rule here is the narrower one: **anything that does not commit is driven for real**,
with the fork's side stubbed at `_legacy_single_values` and `get_decrypted_password` and
nothing else. `harvest_settings` really saves the real settings document through the real
`validate()`, and the tests that care about persistence reload it and save it **again**,
because that second save is where both the credential deletion and the L-12 pairing error
actually land. What is left to the rehearsal is only the two committing entry points and the
schema state a migrate produces.
"""

import ast
import contextlib
import inspect
import json
from unittest.mock import patch

import frappe
from frappe.utils import cint

from cloud_file_storage import legacy_install
from cloud_file_storage.tests.utils import CloudStorageTestCase

PATCHES_TXT = "cloud_file_storage/patches.txt"
SETTINGS_DOCTYPE = legacy_install.SETTINGS_DOCTYPE


class TestTheGuardIsClosedOnANormalSite(CloudStorageTestCase):
	"""This site has never been a legacy install, so every probe must say so — and be able
	to say otherwise, which is the half that makes the first half mean anything."""

	def test_no_residue_is_detected(self):
		self.assertEqual(
			legacy_install.legacy_residue(),
			{
				"installed_apps_entry": False,
				"module_def": False,
				"doctype_row": False,
				"settings_values": False,
			},
		)
		self.assertFalse(legacy_install.is_legacy_install())

	def test_the_probe_opens_when_the_fork_left_settings_behind(self):
		"""The mutation: one `tabSingles` row is enough to make this a legacy site.

		Without this, `test_no_residue_is_detected` would pass just as happily against a
		probe that returned False unconditionally.
		"""
		frappe.qb.into("Singles").columns("doctype", "field", "value").insert(
			(legacy_install.LEGACY_SETTINGS_DOCTYPE, "bucket_name", "left-behind")
		).run()
		try:
			self.assertTrue(legacy_install.legacy_residue()["settings_values"])
			self.assertTrue(legacy_install.is_legacy_install())
		finally:
			frappe.db.delete("Singles", {"doctype": legacy_install.LEGACY_SETTINGS_DOCTYPE})

		self.assertFalse(legacy_install.is_legacy_install(), "the fixture outlived its test")

	def test_bootstrap_is_a_no_op_and_writes_nothing(self):
		before = frappe.db.get_global("installed_apps")
		errors_before = frappe.db.count("Error Log")

		report = legacy_install.bootstrap()

		self.assertFalse(report["legacy_install"])
		self.assertNotIn("settings_preview", report, "a no-op must not even map the settings")
		self.assertEqual(frappe.db.get_global("installed_apps"), before)
		self.assertEqual(frappe.db.count("Error Log"), errors_before)


class TestInstalledAppsAdoption(CloudStorageTestCase):
	"""The one thing that must happen before `bench migrate` can run at all."""

	def _with_apps(self, apps, dry_run=False):
		written = {}

		def set_global(key, value):
			written[key] = value

		with (
			patch.object(frappe.db, "get_global", return_value=json.dumps(apps)),
			patch.object(frappe.db, "set_global", set_global),
			patch.object(legacy_install, "_invalidate_installed_apps_cache"),
		):
			report = legacy_install.adopt_installed_apps(dry_run=dry_run)
		return report, written

	def test_the_fork_slot_is_taken_in_place(self):
		"""Order is hook order. Appending instead of substituting would reorder both."""
		report, written = self._with_apps(["frappe", "frappe_s3_attachment", "erpnext"])
		self.assertTrue(report["changed"])
		self.assertEqual(report["installed_apps"], ["frappe", "cloud_file_storage", "erpnext"])
		self.assertEqual(json.loads(written["installed_apps"]), report["installed_apps"])

	def test_an_app_already_installed_alongside_the_fork_is_not_duplicated(self):
		report, _written = self._with_apps(["frappe", "frappe_s3_attachment", "cloud_file_storage"])
		self.assertEqual(report["installed_apps"], ["frappe", "cloud_file_storage"])

	def test_a_site_without_the_fork_is_left_alone(self):
		report, written = self._with_apps(["frappe", "cloud_file_storage"])
		self.assertFalse(report["changed"])
		self.assertEqual(written, {})

	def test_a_dry_run_writes_nothing(self):
		report, written = self._with_apps(["frappe", "frappe_s3_attachment"], dry_run=True)
		self.assertTrue(report["changed"])
		self.assertTrue(report["dry_run"])
		self.assertEqual(written, {}, "a dry run touched the installed_apps global")


class TestSettingsMapping(CloudStorageTestCase):
	FORK_VALUES = {
		"bucket_name": "legacy-fork-bucket",
		"region_name": "ap-south-1",
		"folder_name": "attachments",
		"access_key": "AKIALEGACYFORKKEY",
		"signed_url_expiry_time": "900",
		"delete_file_from_cloud": "1",
	}

	def _map(self, fork_values=None, ours=None):
		ours = ours or {}
		with (
			patch.object(
				legacy_install, "_legacy_single_values", return_value=fork_values or self.FORK_VALUES
			),
			patch.object(legacy_install, "_our_single_value", side_effect=lambda field: ours.get(field)),
			patch(
				"frappe.utils.password.get_decrypted_password",
				side_effect=lambda dt, *a, **k: (
					"s3cret" if dt == legacy_install.LEGACY_SETTINGS_DOCTYPE else None
				),
			),
		):
			return legacy_install.map_settings()

	def test_the_fork_fields_land_on_their_a12_names(self):
		adopted, _report = self._map()
		self.assertEqual(adopted["bucket"], "legacy-fork-bucket")
		self.assertEqual(adopted["region"], "ap-south-1")
		self.assertEqual(adopted["key_prefix"], "attachments")
		self.assertEqual(adopted["access_key_id"], "AKIALEGACYFORKKEY")
		self.assertEqual(adopted["private_presign_ttl"], 900, "Int fields are cast at the boundary")

	def test_adopting_a_key_turns_off_the_default_credential_chain(self):
		"""Otherwise the runtime holds a key it never reads — the first rehearsal's defect."""
		adopted, _report = self._map()
		self.assertEqual(adopted["use_default_credential_chain"], 0, "a Check field is an int, not '0'")

	def test_a_field_the_operator_has_set_is_not_overwritten(self):
		adopted, report = self._map(ours={"bucket": "the-operator-chose-this"})
		self.assertNotIn("bucket", adopted)
		self.assertIn("bucket", report["skipped_already_set"])

	def test_a_fork_field_with_no_home_in_the_frozen_schema_is_reported_not_dropped(self):
		_adopted, report = self._map()
		self.assertIn("delete_file_from_cloud", report["unmapped"])

	def test_the_report_never_carries_a_value(self):
		"""It is printed by the bench command and written to an Error Log."""
		_adopted, report = self._map()
		serialised = json.dumps(report)
		self.assertNotIn("AKIALEGACYFORKKEY", serialised)
		self.assertNotIn("s3cret", serialised)
		self.assertNotIn("legacy-fork-bucket", serialised)
		self.assertTrue(report["secret_available"])


class TestTheDefaultVersusDecisionRule(CloudStorageTestCase):
	"""`_is_untouched` is what decides whether the fork's value wins, so it is asserted
	against the real settings meta rather than a description of it."""

	def test_a_field_still_at_its_shipped_default_counts_as_untouched(self):
		default = legacy_install._schema_default("private_presign_ttl")
		self.assertEqual(str(default), "300", "the A12 default moved; this test is now vacuous")
		with patch.object(legacy_install, "_our_single_value", return_value=default):
			self.assertTrue(legacy_install._is_untouched("private_presign_ttl"))

	def test_a_field_the_operator_moved_off_the_default_counts_as_decided(self):
		with patch.object(legacy_install, "_our_single_value", return_value="42"):
			self.assertFalse(legacy_install._is_untouched("private_presign_ttl"))

	def test_an_empty_field_counts_as_untouched(self):
		with patch.object(legacy_install, "_our_single_value", return_value=None):
			self.assertTrue(legacy_install._is_untouched("bucket"))


class TestAdoptionIsAdditive(CloudStorageTestCase):
	"""Hard invariant 1, for the one module that runs before anything else on a legacy site.

	The checker is **borrowed, not rewritten**. An earlier version of this class kept its own
	list of deletion-ish names, and that list was weaker than one this repository already owns:
	it omitted `quarantine` — which invariant 1 names in the same breath as deletion — and
	`rename`/`replace`/`move`, and it did no receiver analysis, so `os.remove` and
	`str.replace` were indistinguishable to it. `test_migration_safety_lint.find_deletion_calls`
	does all of that and is itself exercised against deliberately-bad synthetic modules. Two
	checkers for one invariant is how the weaker one ends up guarding the newer code.
	"""

	def test_the_module_makes_no_deletion_call(self):
		from cloud_file_storage.tests.test_migration_safety_lint import find_deletion_calls

		found = find_deletion_calls(ast.parse(inspect.getsource(legacy_install)))
		self.assertEqual(found, [], f"legacy_install can delete: {found}")

	def test_the_borrowed_checker_still_bites(self):
		"""Anti-vacuity: a checker that returned [] for everything would pass the test above."""
		from cloud_file_storage.tests.test_migration_safety_lint import find_deletion_calls

		bad = ast.parse("import os\ndef go(path):\n\tos.remove(path)\n")
		self.assertTrue(find_deletion_calls(bad), "the borrowed checker no longer detects os.remove")

	def test_no_object_is_adopted_without_a_bucket_to_name(self):
		with patch(
			"cloud_file_storage.storage.modes.get_settings",
			return_value=frappe._dict({"bucket": ""}),
		):
			report = legacy_install.adopt_fork_objects()
		self.assertEqual(report["adopted"], 0)
		self.assertEqual(report["reason"], "no bucket configured")


class TestThePatchesAreRegisteredInTheOrderTheyDependOn(CloudStorageTestCase):
	def patches_txt(self) -> str:
		from pathlib import Path

		import cloud_file_storage

		return (Path(cloud_file_storage.__file__).parent / "patches.txt").read_text(encoding="utf-8")

	def sections(self) -> dict[str, list[str]]:
		sections, current = {}, None
		for line in self.patches_txt().splitlines():
			line = line.strip()
			if line.startswith("["):
				current = line.strip("[]")
				sections[current] = []
			elif line and current:
				sections[current].append(line)
		return sections

	def test_the_bootstrap_patch_runs_before_the_model_sync(self):
		"""`sync_all` iterates installed_apps; until the patch fixes it, none of our
		doctypes are created at all."""
		self.assertIn(
			"cloud_file_storage.patches.v1_0_0.adopt_legacy_fork_install",
			self.sections()["pre_model_sync"],
		)

	def test_the_object_patch_runs_after_the_column_it_writes_exists(self):
		"""It sets `File.cloud_storage_object`, which `v0_3_0.create_cloud_storage_object_link`
		creates, and reads `File.s3_object_key`, which `v0_2_0.backfill_s3_object_key` fills."""
		post = self.sections()["post_model_sync"]
		link = post.index("cloud_file_storage.patches.v0_3_0.create_cloud_storage_object_link")
		backfill = post.index("cloud_file_storage.patches.v0_2_0.backfill_s3_object_key")
		objects = post.index("cloud_file_storage.patches.v1_0_0.link_legacy_fork_objects")
		self.assertGreater(objects, link)
		self.assertGreater(objects, backfill)


class TestAdoptingAKeyWithoutItsSecret(CloudStorageTestCase):
	"""L-12 — the fork's access key survives, its `__Auth` row does not.

	`validate_credentials` requires an access key and a secret to arrive together. Switching
	`use_default_credential_chain` off beside a key whose secret cannot be retrieved makes
	`settings.save()` throw **inside the `[post_model_sync]` patch**, which fails the entire
	`bench migrate` on an adoption that was otherwise fine — with a credential-pairing message
	that never mentions adoption.

	The second test is the one that matters: it drives the real `harvest_settings`, which
	really saves the real settings document through the real `validate()`. A test that only
	inspected the mapping would pass against a fix that got the flag right and the save wrong.
	"""

	FORK_WITH_KEY_BUT_NO_SECRET = {
		"bucket_name": "legacy-fork-bucket",
		"access_key": "AKIALEGACYFORKKEY",
	}

	def setUp(self):
		super().setUp()
		# The preconditions the finding names, forced rather than assumed: no credential of our
		# own, no access key of our own, and the chain still at its shipped default. The class
		# cleanup inherited from CloudStorageTestCase restores all of it.
		from frappe.utils.password import remove_encrypted_password

		remove_encrypted_password(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE, "secret_access_key")
		frappe.db.set_single_value(
			SETTINGS_DOCTYPE,
			{"access_key_id": "", "secret_access_key": "", "use_default_credential_chain": 1},
			update_modified=False,
		)
		frappe.db.commit()

	def _fork_holds(self, values):
		"""The fork's Singles as given, and no retrievable secret anywhere."""
		return patch.multiple(
			legacy_install,
			_legacy_single_values=lambda: dict(values),
		)

	def test_the_credential_chain_is_left_on_when_no_secret_can_be_adopted(self):
		with self._fork_holds(self.FORK_WITH_KEY_BUT_NO_SECRET):
			adopted, report = legacy_install.map_settings()

		self.assertEqual(adopted.get("access_key_id"), "AKIALEGACYFORKKEY")
		self.assertNotIn(
			"use_default_credential_chain",
			adopted,
			"the chain was switched off beside a key with no secret — validate_credentials "
			"will throw inside the patch and fail the migrate",
		)
		self.assertTrue(report["credential_chain_left_on_without_secret"])
		self.assertFalse(report["secret_available"])

	def test_the_settings_the_harvest_leaves_behind_can_still_be_saved(self):
		"""The real assertion, and it has to be the NEXT save rather than the harvest's own.

		Measured rather than assumed: `settings.update({"use_default_credential_chain": "0"})`
		puts a **truthy string** on the document, so `validate_credentials`' early return fires
		and the harvest's own save succeeds even with the bug present. The pairing error
		arrives on the next load-and-save, once that value has round-tripped through the DB as
		integer 0 — which in a real migrate is `after_migrate`, or the operator's first Desk
		save, or any later patch touching these settings. So this test does what those do:
		harvest, then reload and save, and require that to work.
		"""
		errors_before = frappe.db.count("Error Log")

		with self._fork_holds(self.FORK_WITH_KEY_BUT_NO_SECRET):
			legacy_install.harvest_settings()

		self.assertEqual(frappe.db.get_single_value(SETTINGS_DOCTYPE, "access_key_id"), "AKIALEGACYFORKKEY")

		frappe.clear_document_cache(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE)
		settings = frappe.get_single(SETTINGS_DOCTYPE)
		settings.flags.ignore_mandatory = True
		settings.save(ignore_permissions=True)  # must not raise the credential-pairing error

		self.assertEqual(
			int(frappe.db.get_single_value(SETTINGS_DOCTYPE, "use_default_credential_chain") or 0),
			1,
			"the site was left with a key, no secret, and the chain off — every later save throws",
		)
		# Silent is worse than loud here: the operator has to know the adopted key is unusable.
		self.assertGreater(frappe.db.count("Error Log"), errors_before)

	def test_a_key_with_its_secret_still_switches_the_chain_off(self):
		"""The control. Without it, the fix above could be "never switch the chain off"."""
		with self._fork_holds({**self.FORK_WITH_KEY_BUT_NO_SECRET, "secret_key": "unused"}):
			with patch(
				"frappe.utils.password.get_decrypted_password",
				side_effect=lambda dt, *a, **k: (
					"s3cret" if dt == legacy_install.LEGACY_SETTINGS_DOCTYPE else None
				),
			):
				adopted, report = legacy_install.map_settings()

		self.assertEqual(adopted.get("use_default_credential_chain"), 0)
		self.assertFalse(report["credential_chain_left_on_without_secret"])
		self.assertTrue(report["secret_available"])

	def test_a_secret_of_our_own_does_NOT_permit_the_switch(self):
		"""Reviewer L-9 — and this assertion changed direction, deliberately.

		It used to assert that a secret this app already holds satisfies the pairing. It does
		not, in the one case it can arise: if this app had a key *and* a secret, the fork's key
		would have been skipped as already-decided and this branch would not run — so `ours`
		being set here means the operator has a secret and **no** key, and pairing it with the
		fork's key produces a credential that authenticates as neither. That site works today on
		the IAM chain; switching the chain off would break every S3 operation on it with
		`SignatureDoesNotMatch`. So the chain is left alone and the operator is told which of the
		two credentials they meant.
		"""
		from frappe.utils.password import set_encrypted_password

		set_encrypted_password(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE, "already-ours", "secret_access_key")
		frappe.db.commit()

		with self._fork_holds(self.FORK_WITH_KEY_BUT_NO_SECRET):
			adopted, report = legacy_install.map_settings()

		self.assertEqual(adopted.get("access_key_id"), "AKIALEGACYFORKKEY")
		self.assertNotIn(
			"use_default_credential_chain",
			adopted,
			"the fork's key was paired with a secret that does not belong to it",
		)
		self.assertTrue(report["credential_pair_would_be_mismatched"])
		self.assertTrue(report["credential_chain_left_on_without_secret"])
		self.assertFalse(report["secret_available"], "our own secret must not be overwritten")

	def test_the_mismatch_is_reported_distinctly_from_a_missing_secret(self):
		"""The two ways to reach the warning need different advice, so they get different text."""
		from frappe.utils.password import set_encrypted_password

		errors_before = frappe.db.count("Error Log")
		set_encrypted_password(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE, "already-ours", "secret_access_key")
		frappe.db.commit()

		with self._fork_holds(self.FORK_WITH_KEY_BUT_NO_SECRET):
			legacy_install.harvest_settings()

		self.assertGreater(frappe.db.count("Error Log"), errors_before)
		latest = frappe.get_all("Error Log", fields=["error"], order_by="creation desc", limit_page_length=1)
		self.assertIn("did not come from the fork", str(latest))
		self.assertNotIn("no retrievable", str(latest), "the missing-secret advice was given instead")


class TestBootstrapWritesNoSettings(CloudStorageTestCase):
	"""H-1(a) — the pre-sync stage must not touch this app's settings.

	The whole reason `harvest_settings` moved out of `bootstrap()` is that writing settings
	values before `sync_all` leaves the Single partially populated and suppresses every schema
	default, which killed a real migrate. Nothing held that afterwards: moving the call back
	failed no test. This holds it two ways — behaviourally, and structurally, because the
	behavioural half depends on preconditions a future harness change could quietly remove.
	"""

	def setUp(self):
		super().setUp()
		# A target field this site has not decided, so a regression would have something to
		# write. Restored by the class cleanup inherited from CloudStorageTestCase.
		frappe.db.set_single_value(SETTINGS_DOCTYPE, {"key_prefix": ""}, update_modified=False)
		frappe.db.commit()

		frappe.qb.into("Singles").columns("doctype", "field", "value").insert(
			(legacy_install.LEGACY_SETTINGS_DOCTYPE, "folder_name", "attachments-from-the-fork")
		).run()
		self.addCleanup(frappe.db.commit)
		self.addCleanup(
			lambda: frappe.db.delete("Singles", {"doctype": legacy_install.LEGACY_SETTINGS_DOCTYPE})
		)
		frappe.db.commit()

	def test_the_bootstrap_writes_no_settings_value(self):
		self.assertTrue(legacy_install.is_legacy_install(), "the fixture did not take")
		before = dict(frappe.db.get_singles_dict(SETTINGS_DOCTYPE))

		report = legacy_install.bootstrap()

		self.assertTrue(report["legacy_install"], "the bootstrap did not see the residue")
		self.assertEqual(
			frappe.db.get_singles_dict(SETTINGS_DOCTYPE),
			before,
			"the pre-sync stage wrote a settings value — on a first migrate that partially "
			"populates the Single and suppresses every schema default",
		)

	def test_the_bootstrap_only_previews_the_settings(self):
		"""The structural half: `harvest_settings` is not reachable from `bootstrap`'s body."""
		called = {
			node.func.id
			for node in ast.walk(ast.parse(inspect.getsource(legacy_install.bootstrap)))
			if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
		}
		self.assertIn("map_settings", called, "the preview is gone; this check is now vacuous")
		self.assertNotIn("harvest_settings", called)


class TestTheAdoptedCredentialSurvives(CloudStorageTestCase):
	"""H-1(b) — the rehearsal's §5, at unit scope and re-runnable.

	`_save_passwords` (`frappe/model/base_document.py:1126-1146`) calls
	`remove_encrypted_password` on any Password field it finds empty. Writing `__Auth` directly
	and leaving the document's own field blank therefore survives adoption and is destroyed by
	the **next** save of those settings — `after_migrate`, a Desk save, any later patch. The
	independent test-engineer reproduced exactly that on a live adopted site: one ordinary save
	took `__Auth` from `[secret_access_key]` to `[]`.

	**Driven with no stubs at all.** The fork's Singles rows and its `__Auth` secret are written
	for real, so `_legacy_single_values` and `get_decrypted_password` run as they do in a
	migrate; only the fork's *data* is synthetic. An earlier version patched both readers, which
	tested the same statements through a narrower door.

	The fork here deliberately has **no access key**. With one, the credential-chain flag goes to
	0 and a document whose secret was written around it fails `validate_credentials` on the spot
	— a refusal, not a deletion, which would hide the mechanism this class exists to pin. With
	the chain left at 1 the document is valid either way, so the only thing that can lose the
	secret is `_save_passwords` deleting a field it finds empty.
	"""

	FORK_SECRET = "fork-secret-value"
	#: `folder_name` -> `key_prefix`, and `setUp` clears `key_prefix` first, because the fork
	#: field has to map to something this site has NOT already decided. `bucket_name` was the
	#: obvious choice and the wrong one: the suite's harness sets `bucket`, so the mapping
	#: skipped it, `adopted` came back empty, and the save that destroys the credential would
	#: not have run. The assertion added for reviewer L-7 caught that on its first execution.
	FORK = {"folder_name": "attachments-from-the-fork"}

	def setUp(self):
		super().setUp()
		from frappe.utils.password import remove_encrypted_password, set_encrypted_password

		remove_encrypted_password(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE, "secret_access_key")
		frappe.db.set_single_value(
			SETTINGS_DOCTYPE,
			{
				"access_key_id": "",
				"secret_access_key": "",
				"use_default_credential_chain": 1,
				"key_prefix": "",
			},
			update_modified=False,
		)

		legacy = legacy_install.LEGACY_SETTINGS_DOCTYPE
		frappe.db.delete("Singles", {"doctype": legacy})
		for field, value in self.FORK.items():
			frappe.qb.into("Singles").columns("doctype", "field", "value").insert(
				(legacy, field, value)
			).run()
		set_encrypted_password(legacy, legacy, self.FORK_SECRET, legacy_install.LEGACY_SECRET_FIELD)
		frappe.db.commit()

		self.addCleanup(frappe.db.commit)
		self.addCleanup(remove_encrypted_password, legacy, legacy, legacy_install.LEGACY_SECRET_FIELD)
		self.addCleanup(frappe.db.delete, "Singles", {"doctype": legacy})

	def test_the_secret_survives_the_next_save_of_the_settings(self):
		from frappe.utils.password import get_decrypted_password

		report = legacy_install.harvest_settings()
		self.assertTrue(report["secret_adopted"], "the fork's secret was not adopted at all")
		# The destroying save only happens under `if adopted:` (legacy_install.py). A fixture
		# that adopted nothing would skip it and this test would stop guarding the credential
		# path — checked rather than documented, because a precondition that is checked cannot
		# rot (reviewer L-7). Measured note: at this revision the class still goes red under the
		# credential mutation with an EMPTY fork, because the test's own second save destroys
		# what the buggy path wrote. The assertion pins the intent anyway; the margin it
		# protects is one refactor wide.
		self.assertTrue(report["adopted"], "the fixture adopted no mapped field")
		# `raise_exception=False` on purpose: under the credential regression the row is already
		# gone by here — `harvest_settings`' own save destroys it, because `adopted` is non-empty
		# and `_save_passwords` deletes a Password field it finds blank. Letting that surface as
		# a named assertion beats an unhandled "Password not found" three frames deep.
		self.assertEqual(
			get_decrypted_password(
				SETTINGS_DOCTYPE, SETTINGS_DOCTYPE, "secret_access_key", raise_exception=False
			),
			self.FORK_SECRET,
			"the adopted credential did not survive the harvest's own save",
		)

		# The save that destroys it when the credential is written around the document. This
		# line IS the test: without it the assertion above passes against the buggy version too.
		frappe.clear_document_cache(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE)
		settings = frappe.get_single(SETTINGS_DOCTYPE)
		settings.flags.ignore_mandatory = True
		settings.save(ignore_permissions=True)

		self.assertEqual(
			get_decrypted_password(
				SETTINGS_DOCTYPE, SETTINGS_DOCTYPE, "secret_access_key", raise_exception=False
			),
			self.FORK_SECRET,
			"the next save of these settings deleted the adopted credential — every cloud "
			"read on an adopted site would fail from here",
		)

	def test_the_document_carries_the_placeholder_rather_than_a_blank(self):
		"""Why it survives: `_save_passwords` only deletes what it finds empty."""
		legacy_install.harvest_settings()
		stored = frappe.db.get_singles_dict(SETTINGS_DOCTYPE).get("secret_access_key")
		self.assertTrue(stored, "the Password field is blank on the stored document")
		self.assertNotIn(self.FORK_SECRET, str(stored), "the raw secret was written in clear")


class TestFinishIsTheSecondSave(CloudStorageTestCase):
	"""L-12, driven where it actually lands: inside `finish()`.

	`finish()` calls `harvest_settings()` and then `adopt_ignored_doctypes()`, and the second
	one does a fresh `get_single()` + `save()` whenever the fork's ignored list adds a row —
	which the rehearsal shows happening (`adopted: ['Blog Post']`). That reload reads the Check
	field back as int 0, so before the fix the pairing error landed *inside the
	`[post_model_sync]` patch* and `bench migrate` exited non-zero.

	Four things are stubbed and no more: the three reads that look for the fork's child table,
	which does not exist on a site the fork never owned, and `adopt_fork_objects` — which would
	otherwise scan `tabFile` and mint Cloud Storage Objects on the suite's own site for a test
	that is about neither. Everything the L-12 defect runs through is real: the real settings
	document, the real `validate()`, the real second save. (An earlier version of this docstring
	claimed "and only those" while the fourth patch sat ten lines below it — reviewer L-6, and a
	prose-vs-code slip inside a test added to close a prose-vs-code finding.)
	"""

	FORK_WITH_KEY_BUT_NO_SECRET = {"bucket_name": "b", "access_key": "AKIALEGACYFORKKEY"}

	#: `adopt_ignored_doctypes` carries a fork row across only when the DocType it names still
	#: exists on the site (`legacy_install.py:595`) -- it has to, because `doctype_name` is a
	#: reqd Link to DocType. So the row this class pretends the fork listed must name one that
	#: does, or nothing is adopted, the second `save()` never happens, and the L-12 premise
	#: below is never armed. This was the literal "Blog Post" until frappe moved Blog out of
	#: core into a separate app for v16 (frappe `64db88228f`, in `v16.0.0-rc.1`), so the name is
	#: now probed rather than assumed. `Blog Post` leads the list, so a v15 site still picks it
	#: and this class exercises exactly what it exercised before.
	FORK_IGNORED_ROW_CANDIDATES = ("Blog Post", "Web Page", "Note", "ToDo")

	def setUp(self):
		super().setUp()
		from frappe.utils.password import remove_encrypted_password

		self.FORK_IGNORED_ROW = self._pick_fork_ignored_row()
		remove_encrypted_password(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE, "secret_access_key")

	def _pick_fork_ignored_row(self) -> str:
		"""A DocType this site has that the settings do not already ignore.

		Probed, not named: the product guards on `frappe.db.exists("DocType", ...)`, so the
		only row that can arm the second save is one core still ships. It raises rather than
		skips -- a silently unarmed L-12 test is the exact failure mode this class exists to
		prevent, and a skip would hide it as effectively as the wrong constant did.
		"""
		seeded = {
			row.doctype_name for row in (frappe.get_single(SETTINGS_DOCTYPE).ignored_doctypes or [])
		}
		for name in self.FORK_IGNORED_ROW_CANDIDATES:
			if name not in seeded and frappe.db.exists("DocType", name):
				return name
		raise AssertionError(
			f"none of {self.FORK_IGNORED_ROW_CANDIDATES} is an adoptable DocType on this site, "
			"so this class cannot arm the second save it is named for"
		)
		frappe.db.set_single_value(
			SETTINGS_DOCTYPE,
			{"access_key_id": "", "secret_access_key": "", "use_default_credential_chain": 1},
			update_modified=False,
		)
		frappe.db.commit()

		# The row must be ABSENT for the second save to happen, and `restore_settings` does not
		# reach the child table — so it is removed here and again on the way out rather than
		# asserted away. A previous run of this class would otherwise disarm it.
		self._drop_fork_ignored_row()
		self.addCleanup(frappe.db.commit)
		self.addCleanup(self._drop_fork_ignored_row)

	def _drop_fork_ignored_row(self):
		frappe.db.delete(
			"Cloud Storage Ignored DocType",
			{"parent": SETTINGS_DOCTYPE, "doctype_name": self.FORK_IGNORED_ROW},
		)
		frappe.db.commit()
		frappe.clear_document_cache(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE)

	@contextlib.contextmanager
	def _counting_settings_saves(self):
		"""Count real `Document.save()` calls on the settings Single, wrapping the real one."""
		from frappe.model.document import Document

		counter = {"count": 0}
		original = Document.save

		def counting_save(self, *args, **kwargs):
			if getattr(self, "doctype", None) == SETTINGS_DOCTYPE:
				counter["count"] += 1
			return original(self, *args, **kwargs)

		with patch.object(Document, "save", counting_save):
			yield counter

	def _count_settings_saves(self):
		"""Start counting for the duration of the test; the count is read after `finish()`."""
		manager = self._counting_settings_saves()
		counter = manager.__enter__()
		self.addCleanup(manager.__exit__, None, None, None)
		return counter

	def _finish_with(self, fork_values):
		real_exists, real_table_exists, real_get_all = (
			frappe.db.exists,
			frappe.db.table_exists,
			frappe.db.get_all,
		)
		legacy_child = legacy_install.LEGACY_IGNORED_DOCTYPE

		def exists(doctype, *args, **kwargs):
			if doctype == "DocType" and args and args[0] == legacy_child:
				return legacy_child
			return real_exists(doctype, *args, **kwargs)

		def table_exists(doctype, *args, **kwargs):
			return True if doctype == legacy_child else real_table_exists(doctype, *args, **kwargs)

		def get_all(doctype, *args, **kwargs):
			if doctype == legacy_child:
				return [self.FORK_IGNORED_ROW]
			return real_get_all(doctype, *args, **kwargs)

		with (
			patch.object(legacy_install, "_legacy_single_values", return_value=dict(fork_values)),
			patch.object(legacy_install, "adopt_fork_objects", return_value={"adopted": 0}),
			patch.object(frappe.db, "exists", exists),
			patch.object(frappe.db, "table_exists", table_exists),
			patch.object(frappe.db, "get_all", get_all),
		):
			return legacy_install.finish()

	def test_finish_completes_when_the_fork_key_has_no_secret(self):
		"""The migrate-equivalent path. Before the fix this raised inside the patch.

		The second save is **asserted**, not assumed (reviewer N3). This test is named for it
		and was one assertion short of being independent of the sibling that establishes it:
		removing the `save()` from `adopt_ignored_doctypes` failed only that sibling, leaving
		this one green while no longer testing the condition in its own name.
		"""
		saves = self._count_settings_saves()
		report = self._finish_with(self.FORK_WITH_KEY_BUT_NO_SECRET)
		self.assertGreaterEqual(
			saves["count"],
			2,
			"`finish()` saved the settings once, so the reload that reads the Check field back "
			"as int 0 never happened and this test is not exercising L-12 at all",
		)

		self.assertTrue(report["settings"]["credential_chain_left_on_without_secret"])
		self.assertEqual(report["ignored_doctypes"]["adopted"], [self.FORK_IGNORED_ROW])
		self.assertEqual(frappe.db.get_single_value(SETTINGS_DOCTYPE, "access_key_id"), "AKIALEGACYFORKKEY")
		self.assertEqual(
			cint(frappe.db.get_single_value(SETTINGS_DOCTYPE, "use_default_credential_chain")),
			1,
			"the chain was switched off beside a key with no secret",
		)

	def test_the_ignored_row_is_adopted_and_ours_are_kept(self):
		"""Additive in both directions: the fork's row arrives, the seeded trio stays."""
		before = {row.doctype_name for row in (frappe.get_single(SETTINGS_DOCTYPE).ignored_doctypes or [])}
		self._finish_with(self.FORK_WITH_KEY_BUT_NO_SECRET)

		frappe.clear_document_cache(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE)
		after = {row.doctype_name for row in (frappe.get_single(SETTINGS_DOCTYPE).ignored_doctypes or [])}
		self.assertEqual(after, before | {self.FORK_IGNORED_ROW})


class TestTheForkModuleDefIsLeftAlone(CloudStorageTestCase):
	"""M-5 — the bootstrap must NOT repoint `Module Def.app_name`, and here is what that buys.

	These tests replace three that asserted the repoint happened. The repoint was dropped
	because its justification was checkably false at both ends of the supported frappe range
	while its cost was real: `frappe/installer.py` selects the modules an uninstall destroys by
	exactly that column, so a repointed fork module becomes an uninstall target and takes its
	`tabSingles` values and its `__Auth` secret with it.

	The second test is the one worth having: it runs the **selection an uninstall performs**
	rather than asserting the column's value, so it fails if anything anywhere starts writing
	that column, not only if this function does.
	"""

	def setUp(self):
		super().setUp()
		# Row-level insert on purpose: under this bench's `developer_mode`, `ModuleDef.on_update`
		# would try to mkdir a module folder inside an app that is not on the bench — which is
		# the very condition being simulated.
		now = frappe.utils.now()
		frappe.qb.into("Module Def").columns(
			"name",
			"creation",
			"modified",
			"modified_by",
			"owner",
			"docstatus",
			"idx",
			"module_name",
			"app_name",
			"custom",
		).insert(
			(
				legacy_install.LEGACY_MODULE,
				now,
				now,
				"Administrator",
				"Administrator",
				0,
				0,
				legacy_install.LEGACY_MODULE,
				legacy_install.LEGACY_APP,
				0,
			)
		).run()
		self.addCleanup(frappe.db.commit)
		self.addCleanup(frappe.db.delete, "Module Def", {"name": legacy_install.LEGACY_MODULE})
		frappe.db.commit()

	def test_the_fork_app_name_is_reported_and_not_written(self):
		report = legacy_install.inspect_module_def()

		self.assertTrue(report["found"])
		self.assertFalse(report["changed"])
		self.assertEqual(report["app_name"], legacy_install.LEGACY_APP)
		self.assertEqual(
			frappe.db.get_value("Module Def", legacy_install.LEGACY_MODULE, "app_name"),
			legacy_install.LEGACY_APP,
			"the bootstrap repointed the fork's Module Def — that arms `bench uninstall-app` "
			"against the fork's own Single and its __Auth secret",
		)

	def test_an_uninstall_of_this_app_would_not_select_the_fork_module(self):
		"""The consequence, run rather than described.

		`frappe/installer.py` chooses what to destroy with exactly this query. Asserting the
		query's result rather than the column's value means this test also catches anything
		*else* that starts writing that column.
		"""
		legacy_install.bootstrap()

		targets = frappe.get_all("Module Def", filters={"app_name": legacy_install.APP}, pluck="name")
		self.assertNotIn(
			legacy_install.LEGACY_MODULE,
			targets,
			"uninstalling this app would take the fork's S3 File Attachment Single with it",
		)

	def test_a_site_with_no_fork_module_def_reports_nothing_found(self):
		frappe.db.delete("Module Def", {"name": legacy_install.LEGACY_MODULE})
		frappe.db.commit()
		report = legacy_install.inspect_module_def()
		self.assertFalse(report["found"])
		self.assertFalse(report["changed"])


class TestTheFixtureAndTheBootstrapAgree(CloudStorageTestCase):
	"""L-5 — nothing imported the fixture, so nothing noticed if it drifted.

	The rehearsal's whole value is that the fixture is the fork's real residue. If a fork field
	were renamed on one side only, the rehearsal would keep passing while exercising a mapping
	that no longer matches anything, and the drift would be invisible.
	"""

	def test_the_fixture_uses_the_names_the_bootstrap_looks_for(self):
		from cloud_file_storage.tests import legacy_fixture

		for attribute in (
			"LEGACY_APP",
			"LEGACY_MODULE",
			"LEGACY_SETTINGS_DOCTYPE",
			"LEGACY_IGNORED_DOCTYPE",
		):
			with self.subTest(attribute=attribute):
				self.assertEqual(
					getattr(legacy_fixture, attribute),
					getattr(legacy_install, attribute),
					"the fixture and the bootstrap disagree about the fork's own names",
				)

	def test_every_fork_field_the_fixture_seeds_is_one_the_bootstrap_knows(self):
		from cloud_file_storage.tests import legacy_fixture

		known = {legacy for legacy, _ours in legacy_install.SETTINGS_MAP}
		known |= set(legacy_install.UNMAPPED_LEGACY_FIELDS)
		known.add(legacy_install.LEGACY_SECRET_FIELD)

		unknown = sorted(set(legacy_fixture.LEGACY_SETTINGS) - known)
		self.assertEqual(
			unknown,
			[],
			"the fixture seeds fork fields the bootstrap neither maps nor reports as unmapped, "
			"so the rehearsal silently proves nothing about them",
		)

	def test_the_fixture_seeds_something_the_mapping_actually_carries(self):
		"""Anti-vacuity: the check above passes trivially against an empty fixture."""
		from cloud_file_storage.tests import legacy_fixture

		mapped = {legacy for legacy, _ours in legacy_install.SETTINGS_MAP}
		self.assertTrue(mapped & set(legacy_fixture.LEGACY_SETTINGS))


#: Distinguishes "no request_cache existed" from "it existed and was empty" — the same
#: falsy-value trap that caused the defect this class documents.
_UNSET_CACHE = object()


class TestTheInstalledAppsCacheIsInvalidated(CloudStorageTestCase):
	"""The reviewer's optional suggestion, taken: the one function whose failure is invisible.

	`adopt_installed_apps` itself is tested with `set_global` captured rather than written —
	a deliberate limit, because a test that dies between rewriting the site's real
	`installed_apps` and restoring it leaves the site's app list wrong, which is worse than the
	coverage it buys. `_invalidate_installed_apps_cache` needs no such write, and it is the part
	whose failure mode does not announce itself: `get_installed_apps` is `@request_cache`, so a
	missed invalidation is a stale memo read later in the same process, far from the cause.
	"""

	def test_the_request_cache_memo_is_dropped(self):
		"""**This test poisoned the whole process before CI caught it.**

		The first version did `frappe.local.request_cache = getattr(..., None) or {}`. An empty
		`defaultdict` is **falsy**, so that `or {}` silently swapped frappe's `defaultdict(dict)`
		for a plain dict — and `frappe/utils/caching.py:48` only installs the defaultdict
		`if not hasattr(frappe.local, "request_cache")`, so it never recovers. Every later
		`@request_cache` call then raises `KeyError` on the *write* at line 60, and CI reported
		120+ errors in unrelated `setUpClass` methods across the suite.

		It passed locally purely on test ordering. So: preserve the container's type, and restore
		whatever was there in a cleanup rather than leaving the process altered.
		"""
		from collections import defaultdict

		previous = getattr(frappe.local, "request_cache", _UNSET_CACHE)

		def restore():
			if previous is _UNSET_CACHE:
				if hasattr(frappe.local, "request_cache"):
					del frappe.local.request_cache
			else:
				frappe.local.request_cache = previous

		self.addCleanup(restore)

		# defaultdict, not {} — the type frappe's decorator requires for its write path.
		frappe.local.request_cache = defaultdict(dict)
		frappe.local.request_cache["a-memo-that-must-not-survive"] = ["frappe", "frappe_s3_attachment"]

		legacy_install._invalidate_installed_apps_cache()

		self.assertEqual(
			dict(getattr(frappe.local, "request_cache", {}) or {}),
			{},
			"a stale `get_installed_apps` memo survived the swap — the rest of the bootstrap "
			"would keep reading the app it just removed",
		)

	def test_the_cache_container_type_is_never_downgraded_to_a_plain_dict(self):
		"""The regression guard for the defect CI found, stated as its own property.

		`frappe/utils/caching.py:48` installs `defaultdict(dict)` **only**
		`if not hasattr(frappe.local, "request_cache")`. So once anything replaces that container
		with a plain `dict`, frappe never restores it, and line 60's write —
		`request_cache[func][args_key] = return_val` — raises `KeyError` for the life of the
		process. CI reported **120+ errors in unrelated setUpClass methods** from exactly that.

		The idiom that caused it was `getattr(frappe.local, "request_cache", None) or {}`: an
		**empty defaultdict is falsy**, so the `or` fired on a perfectly good container. This
		asserts the container survives both the production invalidation and the falsy-empty case,
		and that `get_installed_apps` still works afterwards — the observable the users of that
		cache actually depend on.
		"""
		from collections import defaultdict

		previous = getattr(frappe.local, "request_cache", _UNSET_CACHE)

		def restore():
			if previous is _UNSET_CACHE:
				if hasattr(frappe.local, "request_cache"):
					del frappe.local.request_cache
			else:
				frappe.local.request_cache = previous

		self.addCleanup(restore)

		frappe.local.request_cache = defaultdict(dict)
		legacy_install._invalidate_installed_apps_cache()

		self.assertIsInstance(
			frappe.local.request_cache,
			defaultdict,
			"the request cache was downgraded to a plain dict; frappe only installs its "
			"defaultdict when the attribute is ABSENT, so this poisons every later "
			"@request_cache call in the process",
		)
		# The empty container is falsy -- the trap itself. Pin that truth so a future
		# `or {}` idiom is visibly wrong rather than subtly wrong.
		self.assertFalse(
			frappe.local.request_cache,
			"precondition: the invalidated cache is empty, and therefore FALSY -- which is why "
			"`getattr(...) or {}` silently replaced it",
		)
		self.assertTrue(
			frappe.get_installed_apps(),
			"`get_installed_apps` raised or returned nothing after invalidation -- the memo "
			"write path is broken, which is the CI failure this test exists to prevent",
		)

	def _restore_doc_events_hooks(self):
		"""Snapshot/restore, because these tests delete a cache the rest of the process shares."""
		previous = getattr(frappe.local, "doc_events_hooks", _UNSET_CACHE)

		def restore():
			if previous is _UNSET_CACHE:
				if hasattr(frappe.local, "doc_events_hooks"):
					del frappe.local.doc_events_hooks
			else:
				frappe.local.doc_events_hooks = previous

		self.addCleanup(restore)

	# ---- the two frappe readers, reproduced exactly -------------------------------------
	#
	# The bench runs one frappe. Asserting only against it is what let this defect ship: the
	# installed reader is the *forgiving* one. Both shapes are reproduced here from frappe's
	# own source so the floor is tested on this bench rather than only in CI.

	@staticmethod
	def _floor_reader(ns):
		"""frappe <= v15.85.0 — `frappe/__init__.py: if not hasattr(local, "doc_events_hooks")`.

		The declared floor (v15.16.0) is this shape. `None` satisfies `hasattr`, so a cache
		"invalidated" by assignment is never rebuilt and every reader gets `None`.
		"""
		if not hasattr(ns, "doc_events_hooks"):
			ns.doc_events_hooks = {"File": {"after_insert": ["rebuilt"]}}
		return ns.doc_events_hooks

	@staticmethod
	def _modern_reader(ns):
		"""frappe >= v15.88.0 — `if not getattr(local, "doc_events_hooks", None)`."""
		if not getattr(ns, "doc_events_hooks", None):
			ns.doc_events_hooks = {"File": {"after_insert": ["rebuilt"]}}
		return ns.doc_events_hooks

	class _Namespace:
		"""Stand-in for `frappe.local` — attribute get/set/del, nothing else."""

	def test_the_none_assignment_reproduces_the_floor_failure(self):
		"""Requirement 1: the OLD implementation must actually fail, or the fix proves nothing.

		This is the defect itself, reproduced without frappe: assign `None`, then read through
		the floor's `hasattr` reader. `hasattr` is satisfied, the rebuild never runs, and the
		caller receives `None` — which is precisely what made frappe's dispatch raise
		`AttributeError: 'NoneType' object has no attribute 'get'` 414 times on v15.16.0.
		"""
		ns = self._Namespace()
		ns.doc_events_hooks = {"stale": True}

		ns.doc_events_hooks = None  # the old implementation, verbatim

		self.assertIsNone(
			self._floor_reader(ns),
			"the floor reader rebuilt after a None assignment — then this defect never existed "
			"and this test is not reproducing it",
		)

	def test_the_deletion_lets_the_floor_reader_rebuild(self):
		"""Requirement 2 + 5: the fix works against the DECLARED FLOOR, not just this bench."""
		ns = self._Namespace()
		ns.doc_events_hooks = {"stale": True}

		if hasattr(ns, "doc_events_hooks"):  # the shipped invalidation, verbatim
			del ns.doc_events_hooks

		rebuilt = self._floor_reader(ns)
		self.assertIsInstance(rebuilt, dict, "the floor reader did not rebuild after deletion")
		self.assertIn("File", rebuilt)

	def test_the_deletion_lets_the_modern_reader_rebuild(self):
		"""Requirement 5's other half: the fix must not regress the bench's own frappe."""
		ns = self._Namespace()
		ns.doc_events_hooks = {"stale": True}

		if hasattr(ns, "doc_events_hooks"):
			del ns.doc_events_hooks

		rebuilt = self._modern_reader(ns)
		self.assertIsInstance(rebuilt, dict)
		self.assertIn("File", rebuilt)

	def test_the_none_assignment_is_survivable_only_on_the_modern_reader(self):
		"""Why this was invisible here: the bench's reader forgives what the floor's does not.

		Pins the asymmetry itself, so the record is checkable rather than asserted. If frappe
		ever makes the modern reader strict too, this fails and the comment needs revisiting.
		"""
		modern_ns, floor_ns = self._Namespace(), self._Namespace()
		modern_ns.doc_events_hooks = None
		floor_ns.doc_events_hooks = None

		self.assertIsInstance(self._modern_reader(modern_ns), dict, "modern reader should rebuild")
		self.assertIsNone(self._floor_reader(floor_ns), "floor reader should NOT rebuild")

	# ---- the shipped function, against the installed frappe -----------------------------

	def test_the_attribute_is_absent_after_invalidation_never_none(self):
		"""Requirement 6's target: the one assertion a `= None` regression cannot satisfy.

		Stated as absence rather than as a value, because absence is the only form that is
		correct on **both** readers. The previous version of this test asserted
		`assertIsNone(frappe.local.doc_events_hooks)` — it did not merely miss the defect, it
		**required** it, which is why the defect survived every local run and four review rounds.
		"""
		self._restore_doc_events_hooks()
		frappe.local.doc_events_hooks = {"stale": True}

		legacy_install._invalidate_installed_apps_cache()

		self.assertFalse(
			hasattr(frappe.local, "doc_events_hooks"),
			"`doc_events_hooks` still exists after invalidation. If it was set to None, every "
			"frappe below v15.88.0 will hand None to `get_doc_hooks()` and every document "
			"insert will raise AttributeError in frappe's own dispatch",
		)

	def test_repeated_invalidation_is_safe(self):
		"""Requirement 3 — `del` on an absent attribute raises, so the guard has to hold."""
		self._restore_doc_events_hooks()
		frappe.local.doc_events_hooks = {"stale": True}

		legacy_install._invalidate_installed_apps_cache()
		legacy_install._invalidate_installed_apps_cache()
		legacy_install._invalidate_installed_apps_cache()

		self.assertFalse(hasattr(frappe.local, "doc_events_hooks"))

	def test_invalidation_with_the_attribute_already_absent_is_safe(self):
		"""Requirement 4 — the bootstrap's first call runs before anything populates it."""
		self._restore_doc_events_hooks()
		if hasattr(frappe.local, "doc_events_hooks"):
			del frappe.local.doc_events_hooks

		legacy_install._invalidate_installed_apps_cache()

		self.assertFalse(hasattr(frappe.local, "doc_events_hooks"))

	def test_the_installed_frappe_rebuilds_a_usable_map_after_invalidation(self):
		"""The end-to-end observable: frappe's real reader, not a reproduction of it.

		Guards the direction the reproductions cannot — that the shipped invalidation leaves
		the process in a state frappe itself can recover from.
		"""
		self._restore_doc_events_hooks()
		frappe.local.doc_events_hooks = {"stale": True}

		legacy_install._invalidate_installed_apps_cache()
		hooks = frappe.get_doc_hooks()

		self.assertIsInstance(hooks, dict, "frappe.get_doc_hooks() did not return a mapping")
		self.assertNotIn("stale", hooks, "the stale map survived invalidation")
		# The observable the 414 CI errors came from: frappe's dispatch does
		# `doc_events.get(doctype, {}).get(method, [])`, which needs a mapping, not None.
		self.assertEqual(hooks.get("__definitely_not_a_doctype__", {}).get("on_update", []), [])


class TestTheForkFileRowsAreLinked(CloudStorageTestCase):
	"""N2 — the object half of `legacy_install.py`, which H-1 did not reach.

	H-1 closed the settings and credential half. This is the other half of the same module and
	it was equally unheld: removing the `File.cloud_storage_object` write inside
	`adopt_fork_objects` left the entire 1355-test suite green, at two separate SHAs. Nothing
	asserted that adoption links anything — the only tests naming that column were about patch
	ordering, and the one test driving this function drove its refusal path.

	**The link assertion is the whole test**, exactly as the second save is for the credential
	one. Without the link the objects are still minted and every legacy private URL 403s:
	`api/compat.legacy_generate_file` resolves the File row *by* `cloud_storage_object`, so an
	adopted site would serve nothing — which is the one thing this function exists to prevent.
	"""

	MODE = "LOCAL_ONLY"

	NEW_GEN_KEY = "attachments/2021/07/14/Sales Invoice/2f9a1c_invoice.txt"
	OLD_GEN_KEY = "attachments/2019/03/02/Purchase Invoice/91bd77_bill.txt"

	def setUp(self):
		super().setUp()
		self.addCleanup(self._drop_audit_rows)

	def _drop_audit_rows(self):
		for name in self.fork_files:
			frappe.db.delete("Cloud Storage Audit Log", {"file": name})
		frappe.db.commit()

	fork_files: list[str] = []

	def _fork_row(self, *, file_name, key, is_private=1):
		"""A File row shaped like one the fork left behind: a key, and no object link."""
		doc = self.make_file(file_name=file_name, content=b"legacy fork bytes", is_private=is_private)
		frappe.db.set_value(
			"File",
			doc.name,
			{"s3_object_key": key, "cloud_storage_object": None, "content_hash": None},
			update_modified=False,
		)
		frappe.db.commit()
		self.fork_files.append(doc.name)
		return doc.name

	def test_every_fork_row_is_linked_to_the_object_minted_for_its_key(self):
		self.fork_files = []
		private_row = self._fork_row(file_name="n2-invoice.txt", key=self.NEW_GEN_KEY)
		public_row = self._fork_row(file_name="n2-bill.txt", key=self.OLD_GEN_KEY, is_private=0)

		report = legacy_install.adopt_fork_objects()
		self.assertEqual(report["adopted"], 2, report)

		for row, key, visibility in (
			(private_row, self.NEW_GEN_KEY, "private"),
			(public_row, self.OLD_GEN_KEY, "public"),
		):
			with self.subTest(key=key):
				cso_name = frappe.db.get_value("File", row, "cloud_storage_object")
				self.assertTrue(
					cso_name,
					"adoption minted an object and left the File row unlinked — every legacy "
					"private URL on an adopted site would 403, because compat resolves the row "
					"by this column",
				)
				cso = frappe.get_doc("Cloud Storage Object", cso_name)
				self.assertEqual(cso.s3_key, key, "the row was linked to the wrong object")
				self.assertEqual(cso.status, "legacy_unverified")
				self.assertEqual(cso.visibility, visibility)
				self.assertIsNone(cso.content_sha256, "adoption fabricated a hash it never read")

	def test_a_row_the_fork_never_touched_is_left_alone(self):
		"""The control: a green above could otherwise mean "links everything"."""
		self.fork_files = []
		fork_row = self._fork_row(file_name="n2-fork.txt", key=self.NEW_GEN_KEY)
		local_only = self.make_file(file_name="n2-untouched.txt", content=b"not a cloud file")

		legacy_install.adopt_fork_objects()

		self.assertTrue(frappe.db.get_value("File", fork_row, "cloud_storage_object"))
		self.assertFalse(
			frappe.db.get_value("File", local_only.name, "cloud_storage_object"),
			"a File with no fork key was adopted",
		)

	def test_a_second_pass_adopts_nothing_and_relinks_nothing(self):
		"""Idempotent by construction: the linked row leaves the candidate set."""
		self.fork_files = []
		row = self._fork_row(file_name="n2-again.txt", key=self.NEW_GEN_KEY)

		legacy_install.adopt_fork_objects()
		first = frappe.db.get_value("File", row, "cloud_storage_object")

		second = legacy_install.adopt_fork_objects()
		self.assertEqual(second["adopted"], 0)
		self.assertEqual(frappe.db.get_value("File", row, "cloud_storage_object"), first)


class TestTheAdoptionLoopCannotSpin(CloudStorageTestCase):
	"""L-13 — the termination guard itself, which nothing held.

	The guard exists because removing the `File.cloud_storage_object` write does not make
	`adopt_fork_objects` fail; it makes it loop forever, since the candidate set only shrinks
	because of that write. The link write is tested now, so a *code* regression is caught
	upstream — but the guard is what stands between an operator and a hung `bench migrate` when
	the write fails for an environmental reason instead: a column renamed by another app, a
	permission oddity, a trigger. That is not a code path any other test reaches.

	**The obvious test for this hangs the suite.** Stub the write, call the function, assert the
	reason — and if the guard ever regresses, CI wedges instead of going red. A failing test
	tells you something; a wedged job tells you nothing and blocks everything behind it. So this
	is bounded in both directions: `BATCH_SIZE` is 1 so a second pass is reached immediately,
	and the candidate read itself raises on the third call. The guard must stop the loop before
	that; if it does not, the test fails loudly with `TookTooManyPasses` rather than spinning.
	"""

	MODE = "LOCAL_ONLY"
	KEY = "attachments/2021/07/14/Sales Invoice/l13_invoice.txt"

	class TookTooManyPasses(AssertionError):
		"""Raised by the bounded reader, so a regressed guard fails instead of hanging."""

	#: Hard ceiling on reads of `tabFile` inside the bounded scenario, independent of whether the
	#: interceptor still recognises the candidate query. Two fork rows at `BATCH_SIZE` 1 need
	#: three reads on the healthy path; anything approaching this is a loop that is not stopping.
	MAX_FILE_READS = 8

	def setUp(self):
		super().setUp()
		self.fork_rows = []
		for index in (1, 2):
			doc = self.make_file(file_name=f"l13-{index}.txt", content=b"legacy fork bytes")
			frappe.db.set_value(
				"File",
				doc.name,
				{"s3_object_key": f"{self.KEY}.{index}", "cloud_storage_object": None},
				update_modified=False,
			)
			self.fork_rows.append(doc.name)
		frappe.db.commit()
		self.addCleanup(frappe.db.commit)
		self.addCleanup(self._drop_audit_rows)

	def _drop_audit_rows(self):
		for name in self.fork_rows:
			frappe.db.delete("Cloud Storage Audit Log", {"file": name})

	@contextlib.contextmanager
	def _link_write_broken(self, *, max_passes=2):
		"""The environmental failure: the link write silently does nothing.

		`frappe.get_all` is wrapped so the candidate read cannot be reached more times than the
		guard should allow. Only the File-link `set_value` is neutralised; every other write —
		including the object mint — runs for real, because the point is a loop that keeps
		minting while the set never shrinks.
		"""
		real_set_value, real_get_all = frappe.db.set_value, frappe.get_all
		passes = {"count": 0}
		reads = {"count": 0}

		def set_value(doctype, name, fieldname, *args, **kwargs):
			if doctype == "File" and isinstance(fieldname, dict) and "cloud_storage_object" in fieldname:
				return None
			return real_set_value(doctype, name, fieldname, *args, **kwargs)

		def get_all(doctype, *args, **kwargs):
			if doctype == "File":
				# Backstop first, and unconditional: ANY read of `tabFile` counts against a hard
				# ceiling. The recogniser below can be defeated by respelling the call it looks
				# for; this cannot, so the worst case is a test that fails for a slightly less
				# specific reason rather than a suite that wedges.
				reads["count"] += 1
				if reads["count"] > self.MAX_FILE_READS:
					raise self.TookTooManyPasses(
						f"`tabFile` was read {reads['count']} times, past the hard ceiling of "
						f"{self.MAX_FILE_READS}: whatever the loop is doing, it is not stopping"
					)
				# Keyed on the filters, not on `order_by`. The first version matched
				# `order_by == "name"`, and the test-engineer broke it by respelling that to
				# `"name asc"` in the code under test — semantically identical, the kind of thing
				# any refactor writes — which un-matched the interceptor and wedged the suite for
				# 150s until it was killed. The filters name the column this loop exists for, so
				# they are what identifies it.
				if "s3_object_key" in str(kwargs.get("filters") or args):
					passes["count"] += 1
					if passes["count"] > max_passes:
						raise self.TookTooManyPasses(
							f"the candidate read was reached {passes['count']} times: the guard "
							"did not stop the loop, and on a real site this is a hung bench "
							"migrate"
						)
			return real_get_all(doctype, *args, **kwargs)

		with (
			patch.object(legacy_install, "BATCH_SIZE", 1),
			patch.object(frappe.db, "set_value", set_value),
			patch.object(frappe, "get_all", get_all),
		):
			yield passes

	def test_a_link_write_that_does_nothing_stops_the_loop_instead_of_spinning(self):
		errors_before = frappe.db.count("Error Log")

		with self._link_write_broken() as passes:
			report = legacy_install.adopt_fork_objects()

		self.assertEqual(report["reason"], "adoption made no progress")
		self.assertEqual(passes["count"], 2, "the guard fired at the wrong pass")
		self.assertGreater(frappe.db.count("Error Log"), errors_before, "adoption stopped silently")

	def test_the_pass_ceiling_stops_a_loop_the_intersection_guard_cannot_see(self):
		"""G5 — the ceiling, which the two tests above provably never reach.

		The intersection guard fires when a pass repeats the previous pass's rows. That is the
		common shape, and it is why `test_a_link_write_that_does_nothing_stops_the_loop_instead_of_spinning` stops at
		pass 2 — below the ceiling, so that test says nothing about whether the ceiling works.
		Reverting `for _pass in range(max_passes)` to `while True` leaves the whole suite green
		without this test.

		**Defeating the guard without touching it:** rotate the candidate order so consecutive
		passes never share a row. `{A}` then `{B}` then `{A}` intersects nothing pairwise, so the
		guard is silent and only the ceiling is left standing. That is not a contrived shape — a
		non-deterministic `order_by` over a set that is not draining produces it.

		**Bounded, like its siblings, but by a different mechanism.** The stub that rotates the
		rows is also what counts them, so if the ceiling regresses this raises rather than hangs.
		And if the loop ever stops calling `frappe.get_all` — the G5 axis — the rotation simply
		does not happen, the rows repeat, the intersection guard fires, and this test FAILS on
		the reason string. No path through it wedges.
		"""
		real_set_value, real_get_all = frappe.db.set_value, frappe.get_all
		reads = {"count": 0}
		# Well above the ceiling the code should impose (2 candidates at BATCH_SIZE 1 -> 3), so
		# this only fires if nothing else stopped the loop.
		hard_stop = 12

		def set_value(doctype, name, fieldname, *args, **kwargs):
			if doctype == "File" and isinstance(fieldname, dict) and "cloud_storage_object" in fieldname:
				return None
			return real_set_value(doctype, name, fieldname, *args, **kwargs)

		def get_all(doctype, *args, **kwargs):
			if doctype == "File" and "s3_object_key" in str(kwargs.get("filters") or args):
				reads["count"] += 1
				if reads["count"] > hard_stop:
					raise self.TookTooManyPasses(
						f"the candidate read was reached {reads['count']} times with the "
						"intersection guard silent: the pass ceiling did not stop the loop, and "
						"on a real site this is a hung bench migrate"
					)
				# Rotate, so pass N and pass N+1 never share a row and the guard stays silent.
				# The batch limit is dropped first: rotating a one-row page is a no-op, so the
				# whole candidate set is read and a different single row handed back each pass.
				unlimited = dict(kwargs)
				unlimited["limit_page_length"] = 0
				every = real_get_all(doctype, *args, **unlimited)
				if not every:
					return every
				offset = reads["count"] % len(every)
				return [every[offset]]
			return real_get_all(doctype, *args, **kwargs)

		errors_before = frappe.db.count("Error Log")
		with (
			patch.object(legacy_install, "BATCH_SIZE", 1),
			patch.object(frappe.db, "set_value", set_value),
			patch.object(frappe, "get_all", get_all),
		):
			report = legacy_install.adopt_fork_objects()

		self.assertEqual(
			report["reason"],
			"adoption exceeded its pass ceiling",
			"the ceiling did not stop the loop -- if this says 'adoption made no progress' the "
			"rotation failed to defeat the intersection guard and the ceiling is still untested",
		)
		self.assertGreater(
			frappe.db.count("Error Log"), errors_before, "the ceiling stopped adoption silently"
		)

	def _ceiling_fires_at(self, extra_rows: int, batch_size: int = 1) -> int:
		"""Drive the ceiling with `2 + extra_rows` candidates and return the pass it fired on.

		Same rotation as the test above — consecutive passes never share a row, so the
		intersection guard stays silent and the ceiling is the only thing that can stop the
		loop. The stub counts and also bounds: `hard_stop` is generous relative to any
		legitimate ceiling for these row counts, so a regressed ceiling raises rather than hangs.
		"""
		for index in range(extra_rows):
			doc = self.make_file(file_name=f"l13-scale-{index}.txt", content=b"legacy fork bytes")
			frappe.db.set_value(
				"File",
				doc.name,
				{"s3_object_key": f"{self.KEY}.scale.{index}", "cloud_storage_object": None},
				update_modified=False,
			)
			self.fork_rows.append(doc.name)
		frappe.db.commit()

		real_set_value, real_get_all = frappe.db.set_value, frappe.get_all
		reads = {"count": 0}
		hard_stop = 2 + extra_rows + legacy_install.ADOPTION_PASS_SLACK + 6

		def set_value(doctype, name, fieldname, *args, **kwargs):
			if doctype == "File" and isinstance(fieldname, dict) and "cloud_storage_object" in fieldname:
				return None
			return real_set_value(doctype, name, fieldname, *args, **kwargs)

		def get_all(doctype, *args, **kwargs):
			if doctype == "File" and "s3_object_key" in str(kwargs.get("filters") or args):
				reads["count"] += 1
				if reads["count"] > hard_stop:
					raise self.TookTooManyPasses(
						f"the candidate read was reached {reads['count']} times for "
						f"{2 + extra_rows} candidates: the ceiling did not stop the loop"
					)
				unlimited = dict(kwargs)
				unlimited["limit_page_length"] = 0
				every = real_get_all(doctype, *args, **unlimited)
				if not every:
					return every
				return [every[reads["count"] % len(every)]]
			return real_get_all(doctype, *args, **kwargs)

		with (
			patch.object(legacy_install, "BATCH_SIZE", batch_size),
			patch.object(frappe.db, "set_value", set_value),
			patch.object(frappe, "get_all", get_all),
		):
			report = legacy_install.adopt_fork_objects()
		self.assertEqual(report["reason"], "adoption exceeded its pass ceiling")
		return reads["count"]

	def test_the_ceiling_scales_with_the_work_rather_than_being_a_round_number(self):
		"""The ceiling must move with the candidate count, not be a constant.

		**This test exists because its first version did not test its own name.** It asserted
		`ADOPTION_PASS_SLACK == 2` and that the guard fired early — neither of which varies the
		candidate count, the only way to observe scaling. The release auditor proved the gap by
		mutation: hardcoding `max_passes = 3` left all 46 tests green while **refusing any real
		adoption of more than ~2 batches of fork rows** (>10,000 at the shipped `BATCH_SIZE`),
		stopping with "adoption exceeded its pass ceiling" and leaving the rest unadopted. That
		is exactly the failure the name promises to prevent.

		So the property is measured directly: run the same broken-link scenario at two different
		candidate counts and require the ceiling to fire later for the larger one, at the
		arithmetic the code claims. A constant ceiling fails both equalities; a ceiling that
		ignores the batch size fails the second.
		"""
		small = self._ceiling_fires_at(extra_rows=0)
		self.assertEqual(
			small,
			2 + legacy_install.ADOPTION_PASS_SLACK,
			"with 2 candidates at BATCH_SIZE 1 the ceiling must be 2 passes plus the slack",
		)

		larger = self._ceiling_fires_at(extra_rows=4)
		self.assertEqual(
			larger,
			6 + legacy_install.ADOPTION_PASS_SLACK,
			"with 6 candidates the ceiling must be 6 passes plus the slack -- if this equals "
			"the 2-candidate figure the ceiling is a constant and will refuse a real migration",
		)
		self.assertGreater(
			larger, small, "the ceiling did not move with the amount of work; it is not scaling"
		)

		# The third data point exists because the first two cannot see the batch-size term:
		# both run at BATCH_SIZE 1, and `ceil(N/1) == N`, so dropping the division entirely is
		# invisible to them. Found by mutation in the release audit. At the shipped BATCH_SIZE
		# of 5000 that term IS the safety property -- 1.2M fork rows give a correct ceiling of
		# 242 passes against a mutant's 1,200,002, each one a full indexed scan.
		# Same 6 candidates as above, now drained 2 per pass: ceil(6/2) + slack.
		batched = self._ceiling_fires_at(extra_rows=0, batch_size=2)
		self.assertEqual(
			batched,
			3 + legacy_install.ADOPTION_PASS_SLACK,
			"with 6 candidates at BATCH_SIZE 2 the ceiling must be ceil(6/2) plus the slack -- "
			"if this equals the BATCH_SIZE 1 figure the ceiling ignores the batch size, which "
			"at shipped values turns a 242-pass ceiling into a 1.2M-pass one",
		)
		self.assertLess(
			batched,
			larger,
			"draining more rows per pass must lower the ceiling; it did not, so the batch-size "
			"term is not being applied",
		)

	def test_the_healthy_path_does_not_trip_the_guard(self):
		"""The control. A guard that fired on every run would pass the test above.

		`adopted == 2` is a claim about the **site-wide** candidate set, not about this class's
		rows: `adopt_fork_objects` scans every File with a fork key and no link. It holds because
		`CloudStorageTestCase._cleanup` removes every `make_file` row and every `TEST_BUCKET`
		object after each test, so no neighbouring class can inflate it — and it would be wrong
		on a site that already carried unlinked fork rows, which a pristine install never does.
		Recorded so the next person does not learn it from a confusing failure.
		"""
		with patch.object(legacy_install, "BATCH_SIZE", 1):
			report = legacy_install.adopt_fork_objects()

		self.assertNotIn("reason", report, report)
		self.assertEqual(report["adopted"], 2)
		for name in self.fork_rows:
			self.assertTrue(frappe.db.get_value("File", name, "cloud_storage_object"))
