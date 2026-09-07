"""Adopting a site that is still running the `frappe_s3_attachment` 0.2.x fork (A21).

An external 0.2.x site cannot simply `bench migrate` into this app. `installed_apps` names
a module that no longer exists on the bench, and **migrate reads that list unfiltered**:
`frappe/migrate.py:110` (`before_migrate` hooks), `frappe/modules/patch_handler.py:89`
(patch discovery) and `frappe/model/sync.py:43` (`sync_all`) all iterate
`frappe.get_installed_apps()` without `_ensure_on_bench=True`, so the first thing migrate
does on such a site is raise `ModuleNotFoundError`. A patch cannot fix that: the patch
never runs.

So the bootstrap is two moves, and this module is the single implementation of both:

1. **Out of band, before migrate** — `bench cfs-adopt-legacy-install --site <site>`
   (`commands.adopt_legacy_install`). Hook *loading* uses the filtered form
   (`frappe/__init__.py:1584`), so an ordinary `frappe.init()` + `connect()` still works on
   a site migrate cannot touch. That is the window this command runs in.
2. **`[pre_model_sync]` + `[post_model_sync]` patches** — `patches/v1_0_0/`. They call the
   same functions, so a site that reached this app another way (the fork uninstalled first,
   then `bench install-app cloud_file_storage`) is adopted by the patches alone.

**Everything here is additive.** Nothing local is deleted, nothing remote is deleted, no
fork row is dropped, and no value this app already holds is overwritten. Adoption writes
`legacy_unverified` Cloud Storage Objects through `storage.objects.adopt_legacy_cso` and
never fabricates `verified` (A15) — promotion is a streamed re-GET, and it is the migration
engine's job, not migrate's.

**What this deliberately does NOT do.**

* It does not rewrite `tabFile.file_url` (A10). The `override_whitelisted_methods` remap
  keeps every `/api/method/frappe_s3_attachment.controller.generate_file?key=…` URL alive
  forever, and those URLs also live in business fields, emails and bookmarks where no patch
  can reach them. Canonicalisation is the adoption campaign's job.
* It does not rewrite `tabPatch Log`. The rename research (docs/research §6 step 19)
  prescribes rewriting the `frappe_s3_attachment.patches.` prefix, which is correct for a
  *rename in place* — the app keeps its patch history. This is a greenfield adoption
  (ADR 0008): our patches are new modules with new names, none of which collide with a fork
  entry, so rewriting the prefix would only forge history and could mark one of our patches
  as already run. The fork's rows stay as the inert history they are.
* It does not touch the fork's `Module Def` at all, does not delete its DocTypes, and
  **nothing else deletes them either**. An earlier revision repointed `Module Def.app_name` at
  this app; that was dropped after the claimed benefit turned out not to exist at either end
  of the supported frappe range while the cost — arming `bench uninstall-app` against the
  fork's own data — was real. `inspect_module_def` carries the whole finding.
  `remove_orphan_doctypes` (`frappe/model/sync.py:143-176`) does not reap
  them: it treats `ImportError` as the orphan signal and this path raises
  `frappe.DoesNotExistError`, which its bare `except Exception: continue` skips. The fork's
  DocType rows, its `tabSingles` values and its `__Auth` secret all survive adoption — the
  rehearsal evidence records all three still present afterwards. The secret is harvested
  early anyway, because relying on somebody else's ordering to preserve an operator's
  credential is not a thing worth being clever about.
"""

import json

import frappe
from frappe.utils import cint

APP = "cloud_file_storage"
SETTINGS_DOCTYPE = "Cloud Storage Settings"
IGNORED_DOCTYPE = "Cloud Storage Ignored DocType"

LEGACY_APP = "frappe_s3_attachment"
LEGACY_MODULE = "Frappe S3 Attachment"
LEGACY_SETTINGS_DOCTYPE = "S3 File Attachment"
LEGACY_IGNORED_DOCTYPE = "S3 Ignored DocType Row"

#: Fork field -> A12 field. Values are copied, never moved: the fork's `tabSingles` rows are
#: left exactly as they are so an operator can still read the configuration they had.
#:
#: `folder_name` -> `key_prefix` is the one mapping worth explaining. The fork prefixed
#: every key with it (`controller.py` key builder), which is precisely what `key_prefix`
#: means here; carrying it over is what keeps a legacy key and a newly minted key inside the
#: same bucket namespace.
SETTINGS_MAP = (
	("bucket_name", "bucket"),
	("region_name", "region"),
	("endpoint_url", "endpoint_url"),
	("folder_name", "key_prefix"),
	("access_key", "access_key_id"),
	("signed_url_expiry_time", "private_presign_ttl"),
)

LEGACY_SECRET_FIELD = "secret_key"
SECRET_FIELD = "secret_access_key"

#: Fork fields with no home in the frozen A12 schema, reported so the operator can see they
#: were considered rather than missed. `delete_file_from_cloud` is the fork's
#: delete-immediately switch — this app defers every physical delete to GC and there is no
#: setting that can turn that off; the other two died with the fork's bulk-upload endpoint
#: (patches/v0_3_0/drop_fork_migration_settings.py).
UNMAPPED_LEGACY_FIELDS = ("delete_file_from_cloud", "timeout_for_migration_job", "migrate_existing_files")

#: How many File rows one adoption pass claims before committing. Same shape and size as
#: `patches/v0_2_0/backfill_s3_object_key.py`: each batch leaves the candidate set, so the
#: next pass reads the remainder without an offset.
BATCH_SIZE = 5000

# Slack passes allowed on top of the arithmetic minimum, for rows that arrive while the
# loop runs. Small on purpose: this is a fault ceiling, not a work budget.
ADOPTION_PASS_SLACK = 2


def _adoption_candidate_filters() -> list[list[str]]:
	"""The one definition of "a fork File row still waiting to be adopted".

	**The pass ceiling and the loop it bounds must ask the same question.** They were two
	copies of this list, and the ceiling is only correct while the copies agree — nothing
	enforced that. No test could see the difference either: every test neuters the link
	write, so no row ever becomes linked and the two agree by construction. The case that
	tells them apart is an ordinary one — a re-run on a mostly-adopted site, where the
	count would scale with the *adopted* population instead of the work remaining.

	Found by mutation in the release audit, and fixed by deleting the second copy rather
	than by testing for drift: a fourth data point would protect one drift out of a class,
	where this leaves nothing that can drift. Same move as the pass ceiling itself —
	change kind rather than add a layer.

	Returned fresh on each call, because frappe mutates filter lists it is handed.
	"""
	return [
		["s3_object_key", "is", "set"],
		["cloud_storage_object", "is", "not set"],
	]


# --- detection -----------------------------------------------------------------------------


def legacy_residue() -> dict:
	"""What of the fork is still on this site. Four small indexed reads, no `tabFile`.

	This is the guard both patches run first, so it has to be cheap on the 1.2M-row site
	that was never a legacy install at all — on which every value below is False and the
	patches return immediately.
	"""
	return {
		"installed_apps_entry": LEGACY_APP in _global_installed_apps(),
		"module_def": bool(frappe.db.exists("Module Def", LEGACY_MODULE)),
		"doctype_row": bool(frappe.db.exists("DocType", LEGACY_SETTINGS_DOCTYPE)),
		"settings_values": bool(frappe.db.get_singles_dict(LEGACY_SETTINGS_DOCTYPE)),
	}


def is_legacy_install() -> bool:
	"""True when any fork residue remains.

	Deliberately still True after a successful adoption: the fork's `tabSingles` values are
	never deleted, so this stays the stable signal that lets the post-sync patch fire even
	though the pre-sync one has already cleared `installed_apps`.
	"""
	return any(legacy_residue().values())


def _global_installed_apps() -> list[str]:
	raw = frappe.db.get_global("installed_apps")
	if not raw:
		return []
	try:
		apps = json.loads(raw)
	except (TypeError, ValueError):
		return []
	return apps if isinstance(apps, list) else []


# --- stage 1: what has to be true before `bench migrate` can run at all ---------------------


def adopt_installed_apps(dry_run: bool = False) -> dict:
	"""Swap the vanished fork for this app in the `installed_apps` global.

	The fork's slot is taken rather than appended, so app order (and therefore hook order)
	is preserved. Mirrors `frappe/installer.py:347` for the write and `:363` for the cache
	invalidation — `frappe.get_installed_apps` is `@request_cache`, so skipping the second
	half would leave this process reading the old list it just replaced.
	"""
	apps = _global_installed_apps()
	if LEGACY_APP not in apps:
		return {"changed": False, "reason": "no legacy entry", "installed_apps": apps}

	updated: list[str] = []
	for app in apps:
		if app != LEGACY_APP:
			if app not in updated:
				updated.append(app)
			continue
		if APP not in apps and APP not in updated:
			updated.append(APP)

	if dry_run:
		return {"changed": True, "dry_run": True, "installed_apps": updated}

	frappe.db.set_global("installed_apps", json.dumps(updated))
	_invalidate_installed_apps_cache()
	return {"changed": True, "installed_apps": updated}


def _invalidate_installed_apps_cache():
	"""Mirror `frappe/installer.py:363-364`, plus the layer installer.py never has to think about.

	`get_installed_apps` is `@request_cache` (`frappe/__init__.py:1540`), memoised into
	`frappe.local.request_cache`. `add_to_installed_apps` gets away with ignoring that because
	it appends to the very list it just read; this function *replaces* the list, so the memo
	has to go too or the rest of the bootstrap keeps reading the app it just removed.
	"""
	from frappe.defaults import _clear_cache

	_clear_cache("__global")

	# **Deleted, never assigned `None`** — and that is a compatibility requirement, not a style
	# preference. frappe's reader for this cache changed shape mid-v15:
	#
	#   <= v15.85.0   `if not hasattr(local, "doc_events_hooks")`        -- None counts as PRESENT
	#   >= v15.88.0   `if not getattr(local, "doc_events_hooks", None)`  -- None is falsy, rebuilds
	#
	# frappe is self-consistent at both: `installer.py` only started assigning `None` at v15.88.0,
	# the same release that made the reader tolerate it. This function mirrored that newer idiom
	# while `docs/supported-versions.md` declares a **v15.16.0** floor, so on every ref below
	# 15.88 the assignment made `get_doc_hooks()` return `None` permanently, and the next document
	# insert died inside frappe's own dispatch with
	# `AttributeError: 'NoneType' object has no attribute 'get'`.
	#
	# The F1 reference matrix caught it: 414 errors on v15.16.0 against a green v15.93.0. It was
	# invisible on this bench because the bench runs 15.93 — accidentally compatible with the
	# newest frappe rather than correctly compatible across the declared range.
	#
	# Deleting satisfies BOTH readers, so it is correct for the whole range rather than for the
	# version that happens to be installed.
	if hasattr(frappe.local, "doc_events_hooks"):
		del frappe.local.doc_events_hooks

	if hasattr(frappe.local, "request_cache"):
		frappe.local.request_cache.clear()


def inspect_module_def() -> dict:
	"""Report the fork's `Module Def`. **Writes nothing** — and that is a ruling, not an omission.

	A21 says the bootstrap should "fix apps.txt/installed_apps/Module Def". An earlier revision
	took that literally and repointed `app_name` at this app. It was dropped (M-5), because the
	benefit was checkably absent at **both** ends of the supported range while the cost was
	real:

	* **The claimed benefit.** The justification was that a stale `app_name` makes
	  `get_doctype_app_map()` hand a vanished app name to `frappe.get_hooks(app_name=…)` in
	  `frappe/api/__init__.py`, turning any REST request naming a fork doctype into a framework
	  500. Checked at v15.93.0, the revision this bench runs: the call is there, and it is
	  inside `with suppress(Exception)` with the response already built — so it cannot 500.
	  Checked at v15.16.0, the declared floor: `get_doctype_app_map` is not referenced from
	  that module at all — the code path does not exist. There is no supported version where
	  the benefit exists.
	* **The cost.** `frappe/installer.py` selects the modules an uninstall destroys by exactly
	  this column. Repointing it meant `bench uninstall-app cloud_file_storage` on an adopted
	  site would also destroy the fork's `S3 File Attachment` Single, its `tabSingles` values
	  and its `__Auth` secret — the very data this module promises survives. It also made
	  `frappe/config/__init__.py` report the fork's module as belonging to this app.

	A cost with no benefit anywhere in the supported range is not a trade.

	**What is genuinely true about that column, and was true before the repoint too:** it is
	not what makes a module resolvable. `get_module_app` reads `frappe.local.module_app`, a map
	built from the `modules.txt` of every app **on the bench** (`frappe/__init__.py:1649-1690`).
	A module whose app is gone is absent from it whatever `Module Def` says, so the fork's
	DocTypes are unopenable through the ORM either way — which is why every read in
	`map_settings` is meta-free, and why `remove_orphan_doctypes` leaves them alone (it treats
	`ImportError` as the orphan signal; this path raises `DoesNotExistError`).
	"""
	if not frappe.db.exists("Module Def", LEGACY_MODULE):
		return {"changed": False, "found": False}

	return {
		"changed": False,
		"found": True,
		"app_name": frappe.db.get_value("Module Def", LEGACY_MODULE, "app_name"),
		"reason": "left as it is: repointing app_name arms uninstall-app against the fork's data",
	}


def map_settings() -> tuple[dict, dict]:
	"""Read-only: what the fork holds, translated into A12 field names.

	Split out from the write so the bench command can show an operator exactly what adoption
	will carry over — and what it will leave alone — before anything is touched. Returns
	`(values, report)`; the report names fields only, never values, because it is printed and
	logged and one of those values is an access key id.
	"""
	from frappe.utils.password import get_decrypted_password

	legacy = _legacy_single_values()
	adopted: dict[str, str] = {}
	skipped: list[str] = []

	for legacy_field, our_field in SETTINGS_MAP:
		value = legacy.get(legacy_field)
		if value in (None, ""):
			continue
		if not _is_untouched(our_field):
			skipped.append(our_field)
			continue
		adopted[our_field] = _typed(our_field, value)

	secret = get_decrypted_password(
		LEGACY_SETTINGS_DOCTYPE, LEGACY_SETTINGS_DOCTYPE, LEGACY_SECRET_FIELD, raise_exception=False
	)
	ours = get_decrypted_password(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE, SECRET_FIELD, raise_exception=False)

	#: Will the saved document carry the secret that goes WITH the key being adopted? Only if
	#: the fork's own secret is what lands there. Looked up before the credential-chain decision
	#: below, because the two are one decision (security finding L-12).
	#:
	#: An earlier revision also accepted "this app already holds a secret", which is wrong in
	#: the one case it can arise (reviewer L-9). If this app had both a key and a secret, the
	#: fork's key would have been skipped as already-decided and the branch below never runs; so
	#: `ours` being set here means the operator has a secret and **no** key — and pairing the
	#: fork's key with it produces a credential that authenticates as neither. That site is
	#: working today on the IAM chain, and switching the chain off would break every S3
	#: operation on it with `SignatureDoesNotMatch`. Leaving the chain alone keeps it working.
	fork_secret_lands_with_the_key = bool(secret) and not ours

	# The fork had no credential-chain switch; it always used explicit keys. Adopting one
	# without clearing the default-chain flag would hand the runtime a key it never reads —
	# which is exactly what happened on the first rehearsal, because `use_default_credential_
	# chain` defaults to 1 and the old "already has a value" test could not tell a default
	# from a decision.
	#
	# **But the flag and the secret are one decision, not two** (L-12). `validate_credentials`
	# requires an access key and a secret to arrive together, so switching the chain off beside
	# a key whose secret cannot be retrieved — the fork's `__Auth` row deleted, or never
	# written — leaves the settings in a state that cannot be saved again.
	#
	# Worth being exact about *when* that bites, because it was not where it looked. Before
	# `_typed` existed this function wrote the **string** `"0"`, which is truthy, so
	# `validate_credentials`' `if self.use_default_credential_chain: return` fired and the
	# harvest's own save skipped validation entirely; the error arrived on the NEXT
	# load-and-save, once the value had round-tripped through the DB as integer 0. On a
	# realistic fork site that next save is five lines later — `finish()` calls
	# `adopt_ignored_doctypes()`, which reloads and saves whenever the fork's list adds a row —
	# so `bench migrate` failed inside the `[post_model_sync]` patch. Both halves are now
	# closed: values are typed at the boundary (`_typed`), and the flag is not set at all
	# unless a secret will be there.
	#
	# So the chain stays on unless a secret will actually be there, and the operator is told,
	# loudly, that the key that was adopted is not in use.
	credential_chain_left_on_without_secret = False
	credential_pair_would_be_mismatched = False
	if adopted.get("access_key_id") and _is_untouched("use_default_credential_chain"):
		if fork_secret_lands_with_the_key:
			adopted["use_default_credential_chain"] = _typed("use_default_credential_chain", 0)
		else:
			credential_chain_left_on_without_secret = True
			credential_pair_would_be_mismatched = bool(ours)

	report = {
		"adopted": sorted(adopted),
		"skipped_already_set": sorted(skipped),
		"secret_available": bool(secret) and not ours,
		"credential_chain_left_on_without_secret": credential_chain_left_on_without_secret,
		"credential_pair_would_be_mismatched": credential_pair_would_be_mismatched,
		"unmapped": [field for field in UNMAPPED_LEGACY_FIELDS if legacy.get(field) not in (None, "")],
	}
	return adopted, report


def harvest_settings() -> dict:
	"""Copy the fork's configuration into `Cloud Storage Settings` through the ORM.

	**Post-sync on purpose, and the reason is worth keeping.** An earlier revision wrote these
	values straight into `tabSingles` before `sync_all`, which works — a Single's values live
	there whether or not its DocType exists yet. What it also does is leave the Single
	*partially* populated, and `Document.load_from_db` only falls back to `new_doc` (and
	therefore to the schema's defaults) when it finds **no** rows at all
	(`frappe/model/document.py`). Six harvested fields were enough to suppress every default,
	and the first `settings.save()` in the same migrate died on an empty `storage_class`. The
	real migrate on the synthetic legacy site is what surfaced that; the fix is to write the
	document the way every other caller does, once it exists.

	Nothing already set on this app's settings is overwritten — an operator who has begun
	configuring them owns them.
	"""
	from frappe.utils.password import get_decrypted_password

	if not frappe.db.exists("DocType", SETTINGS_DOCTYPE):
		return {"adopted": [], "reason": "settings doctype not synced yet"}

	adopted, report = map_settings()
	if report["secret_available"]:
		# Through the document, not through `set_encrypted_password`. `_save_passwords`
		# (`frappe/model/base_document.py:1126-1146`) writes `__Auth` *and* the `*****`
		# placeholder that stands in for the value on the stored document — and, crucially,
		# `remove_encrypted_password`s any Password field it finds empty. Writing `__Auth`
		# directly leaves the document's own field blank, so the next save of these settings
		# by anyone, including `after_migrate`, would delete the credential this just adopted.
		adopted[SECRET_FIELD] = get_decrypted_password(
			LEGACY_SETTINGS_DOCTYPE, LEGACY_SETTINGS_DOCTYPE, LEGACY_SECRET_FIELD, raise_exception=False
		)

	if report["credential_chain_left_on_without_secret"]:
		_warn_unusable_access_key(mismatched=report["credential_pair_would_be_mismatched"])

	if adopted:
		settings = frappe.get_single(SETTINGS_DOCTYPE)
		settings.update(adopted)
		settings.flags.ignore_mandatory = True
		settings.save(ignore_permissions=True)

	report["secret_adopted"] = report.pop("secret_available")
	return report


def _warn_unusable_access_key(*, mismatched: bool = False):
	"""An access key was adopted that no secret goes with. Loud, because silent is worse here.

	Adoption succeeds in this case rather than failing the migrate (L-12), so the site comes up
	on the IAM default credential chain holding an access key id it cannot use. That is the
	right outcome — a working migrate and a recoverable configuration beats a migrate that dies
	on a credential-pairing error — but it is not a state anyone should have to infer.

	Two ways to arrive here, and the operator's next move differs, so the message does too:
	the fork had no retrievable secret at all, or this app already holds a secret of its own
	that does **not** belong to the fork's key (reviewer L-9).
	"""
	if mismatched:
		frappe.log_error(
			title="cloud_file_storage: adopted an access key that does not match the stored secret",
			message=(
				"The frappe_s3_attachment settings carried an Access Key ID, and this app "
				"already holds a Secret Access Key that did not come from the fork. Those two "
				"do not belong together, so Cloud Storage Settings was left on the default "
				"credential chain rather than being switched to a pair that would authenticate "
				"as neither.\n\n"
				"ACTION: decide which credential this site should use. Either enter the secret "
				"that belongs to the adopted Access Key ID and clear 'Use Default Credential "
				"Chain', or clear the Access Key ID and keep using the instance credential "
				"chain. Nothing is broken as it stands; the adopted key is simply not in use.\n\n"
				"See docs/runbooks/deployment.md."
			),
		)
		return

	frappe.log_error(
		title="cloud_file_storage: adopted an access key with no secret",
		message=(
			"The frappe_s3_attachment settings carried an Access Key ID but no retrievable "
			"Secret Key, so Cloud Storage Settings was left on the default credential chain "
			"rather than being switched to static keys — which would have failed this migrate "
			"on the credential-pairing validation.\n\n"
			"ACTION: either enter the Secret Access Key beside the adopted Access Key ID and "
			"clear 'Use Default Credential Chain', or clear the Access Key ID and keep using "
			"the instance credential chain. Until then the adopted key is not in use.\n\n"
			"See docs/runbooks/deployment.md."
		),
	)


def _typed(fieldname: str, value):
	"""Coerce a harvested value to the type its field actually holds.

	**This closes a class, not an instance.** Everything on both sides of this module arrives
	as a string — `tabSingles` stores strings, and so did the fork — and Frappe's Check and Int
	fields want integers. Handing a Check field the string `"0"` produces a value that is
	`False` in the database and `True` in Python, which is exactly the shape of security
	finding L-12; it is also the same falsy-value trap as the A29 defect P5 found, where
	`age or 1e9` turned the freshest possible backup into the oldest. Two instances of one
	trap in this codebase is enough. Casting once, here, at the point where fork data becomes
	our data, means no caller downstream has to remember which fields are strings.

	Before `sync_all` there is no meta to ask, and no document to save either — the bench
	command only previews at that point — so the value passes through unchanged and the report
	is still accurate about *which* fields would be adopted.
	"""
	try:
		field = frappe.get_meta(SETTINGS_DOCTYPE).get_field(fieldname)
	except frappe.DoesNotExistError:
		return value
	if field is None:
		return value
	if field.fieldtype in ("Check", "Int"):
		return cint(value)
	return value


def _legacy_single_values() -> dict:
	return frappe.db.get_singles_dict(LEGACY_SETTINGS_DOCTYPE)


def _is_untouched(fieldname: str) -> bool:
	"""True when nothing on this site has decided that field yet.

	"Has a value" is the wrong test once the DocType exists, because by then every field
	carries its schema default and a default is not a decision. So untouched means empty **or
	still equal to the shipped default**. The cost is explicit and small: an operator who
	deliberately set a field to exactly its default has that one overwritten by the fork's
	value. The alternative cost is silently dropping the operator's real bucket configuration,
	which is what the first rehearsal did.
	"""
	current = _our_single_value(fieldname)
	if current is None:
		return True
	default = _schema_default(fieldname)
	if default is None:
		return False
	# Compared through `_typed` rather than `str()`. The old spelling was `str(current) ==
	# str(default)`, which is the same string/typed confusion as L-12 seen from the other side:
	# it makes `0` and `"0"` equal, which happens to be right, and `1` and `"1.0"` unequal,
	# which is not. Whatever the field's type is, both sides are put into it before comparing.
	return _typed(fieldname, current) == _typed(fieldname, default)


def _schema_default(fieldname: str):
	"""The shipped default for a settings field, or None before `sync_all` has run."""
	try:
		field = frappe.get_meta(SETTINGS_DOCTYPE).get_field(fieldname)
	except frappe.DoesNotExistError:
		return None
	return field.default if field else None


def _our_single_value(fieldname: str):
	"""This app's current value for a settings field, read without needing its DocType.

	`get_singles_dict` goes straight at `tabSingles` through the query builder
	(`database.py:715-736`), so `map_settings` also answers on a site whose `sync_all` has not
	run yet — which is what lets the bench command preview the harvest before migrate.
	"""
	value = frappe.db.get_singles_dict(SETTINGS_DOCTYPE).get(fieldname)
	return None if value in (None, "") else value


def bootstrap(dry_run: bool = False) -> dict:
	"""The whole pre-migrate stage: the bench command's body and the pre-sync patch's body.

	Deliberately narrow. The only things that *must* happen before `sync_all` are the ones
	that decide whether it runs at all and against which app; everything else waits for
	`finish()`, where this app's own schema exists and the ORM is available.
	"""
	residue = legacy_residue()
	if not any(residue.values()):
		return {"legacy_install": False, "residue": residue}

	report = {
		"legacy_install": True,
		"residue": residue,
		"installed_apps": adopt_installed_apps(dry_run=dry_run),
		"module_def": inspect_module_def(),
		# Preview only. The settings themselves are adopted post-sync, where the document can
		# be saved through the ORM with its own defaults and validation — see harvest_settings.
		"settings_preview": map_settings()[1],
	}
	if not dry_run:
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)
		_log_bootstrap(report)
	return report


def _log_bootstrap(report: dict):
	"""One Error Log row, because a silent adoption is one nobody can audit afterwards.

	`frappe.log_error` is the only durable, always-present surface at `[pre_model_sync]`
	time — `Cloud Storage Audit Log` may not have a table yet on a first migrate. The
	post-sync stage writes real audit rows.
	"""
	frappe.log_error(
		title="cloud_file_storage: adopted a frappe_s3_attachment install",
		message=json.dumps(report, indent=2, default=str),
	)


# --- stage 2: what needs this app's own tables ---------------------------------------------


def adopt_ignored_doctypes() -> dict:
	"""Carry the fork's ignored-doctype list into the frozen settings child table.

	Additive in both directions: a row this app already has is not duplicated, and the
	LOCAL_OPERATIONAL trio `after_install`/`after_migrate` seeds (ADR 0022) is not removed
	because the fork did not list it.
	"""
	if not (
		frappe.db.exists("DocType", LEGACY_IGNORED_DOCTYPE) and frappe.db.table_exists(LEGACY_IGNORED_DOCTYPE)
	):
		# The DocType row is still there in the real flow (`post_model_sync` patches run
		# before `remove_orphan_doctypes`), so this is the "adopted late, by hand" case.
		return {"adopted": [], "reason": "no legacy child table"}

	legacy_rows = frappe.db.get_all(
		LEGACY_IGNORED_DOCTYPE,
		filters={"parenttype": LEGACY_SETTINGS_DOCTYPE, "parentfield": "ignored_doctypes"},
		pluck="doctype_name",
	)
	wanted = [name for name in dict.fromkeys(legacy_rows) if name]
	if not wanted:
		return {"adopted": []}

	settings = frappe.get_single(SETTINGS_DOCTYPE)
	present = {row.doctype_name for row in (settings.ignored_doctypes or [])}
	missing = [name for name in wanted if name not in present and frappe.db.exists("DocType", name)]
	if not missing:
		return {"adopted": [], "already_present": sorted(present & set(wanted))}

	for name in missing:
		settings.append("ignored_doctypes", {"doctype_name": name})
	settings.flags.ignore_mandatory = True
	settings.save(ignore_permissions=True)
	return {"adopted": missing}


def adopt_fork_objects(limit: int | None = None) -> dict:
	"""Mint a `legacy_unverified` Cloud Storage Object per fork key, and link its File row.

	    This is what makes a freshly adopted site *serve* its old attachments. Without a Cloud
	    Storage Object, `api/compat.legacy_generate_file` resolves nothing and refuses — every
	    private fork URL on the site would 403 until an adoption campaign had run.

	Three properties worth being explicit about:

	    * **No S3 call.** A `bench migrate` that needs the bucket to be reachable is a migrate
	      that fails during an S3 outage. Size comes from `tabFile.file_size` and there is no
	      hash at all, which is exactly what `legacy_unverified` means (A15). The real HEAD, the
	      streamed re-GET and the promotion to `verified` belong to
	      `migration.adoption.promote_adopted_object`.
	    * **No status fabrication.** `storage.objects.adopt_legacy_cso` hardcodes
	      `legacy_unverified` and takes no argument that could write anything else (S2), so this
	      caller could not lie about verification even if it wanted to.
	    * **Nothing is deleted and no URL is rewritten** (A10, invariant 1). The File keeps its
	      fork `file_url`; it gains a link.

	The candidate read goes through `frappe.get_all`, not raw SQL. Invariant 5 puts raw SQL
	against `tabFile` at exactly three audited sites, and "compat-patch rewrites" names
	`patches/v0_2_0/backfill_s3_object_key.py` — not a fourth file that would like to join
	that family. `is set` / `is not set` compile to the `ifnull(col, '') != ''` predicate the
	raw version spelled out, over the same `s3_object_key` index. The batching is that
	patch's shape: each batch leaves the candidate set, so the next pass reads the remainder
	without an offset.
	"""
	from cloud_file_storage.migration import audit
	from cloud_file_storage.storage import objects
	from cloud_file_storage.storage.modes import get_settings

	if not frappe.db.has_column("File", "s3_object_key"):
		return {"candidates": 0, "adopted": 0, "reason": "no legacy locator column"}
	if not frappe.db.has_column("File", "cloud_storage_object"):
		# `patches/v0_3_0.create_cloud_storage_object_link` runs earlier in `patches.txt` and
		# creates it, so this is the "called by hand, out of order" case.
		return {"candidates": 0, "adopted": 0, "reason": "no cloud_storage_object column"}

	settings = get_settings()
	if not (settings.bucket or "").strip():
		# Nothing to point an object at. Loud rather than silent: the operator has to set the
		# bucket and re-run, and the alternative is a site full of objects naming "".
		frappe.log_error(
			title="cloud_file_storage: legacy objects not adopted",
			message=(
				"Cloud Storage Settings has no bucket, so the fork's File rows could not be "
				"linked to Cloud Storage Objects. Set the bucket and run "
				"`bench --site <site> execute "
				"cloud_file_storage.legacy_install.adopt_fork_objects`."
			),
		)
		return {"candidates": 0, "adopted": 0, "reason": "no bucket configured"}

	adopted = 0
	scanned = 0
	previous_pass: set[str] = set()

	batch_size = cint(limit) or BATCH_SIZE
	# **Termination is a property of this loop, not of a test's ability to intercept a call.**
	# The intersection guard below reports *why* adoption stopped; this ceiling is what
	# guarantees it stops at all. The distinction matters: an earlier version relied on a
	# test-side bound patched onto `frappe.get_all`, and a one-token change to `frappe.get_list`
	# — which does not delegate to `get_all` — walked straight past it and hung the suite for
	# 150 seconds. A cap here cannot be bypassed by changing which query helper is called.
	#
	# One pass drains at most `batch_size` rows, so a healthy run needs at most
	# ceil(candidates / batch_size) passes; anything beyond that plus slack means the link
	# write is not happening, which is a fault rather than a slow run.
	candidate_count = frappe.db.count("File", filters=_adoption_candidate_filters())
	max_passes = -(-cint(candidate_count) // batch_size) + ADOPTION_PASS_SLACK

	for _pass in range(max_passes):
		rows = frappe.get_all(
			"File",
			filters=_adoption_candidate_filters(),
			fields=["name", "s3_object_key", "is_private", "file_size", "file_name"],
			limit_page_length=batch_size,
			order_by="name",
		)
		if not rows:
			break

		# **Termination is a property of the link write, so it is checked rather than assumed.**
		# This loop has no offset: it re-reads "rows with a fork key and no object link" until
		# that set is empty, which works only because each pass writes the link that removes its
		# rows from the set. If that write ever stopped happening the loop would spin forever,
		# and a `bench migrate` that hangs is worse than one that fails. Found by mutation —
		# removing the link write turned a test run into an infinite loop rather than a red.
		names = {row.name for row in rows}
		if names & previous_pass:
			frappe.log_error(
				title="cloud_file_storage: legacy object adoption made no progress",
				message=(
					f"A pass over the fork's File rows returned {len(names)} row(s) that the "
					"previous pass had already processed, which means the "
					"`File.cloud_storage_object` link is not being written and the candidate set "
					"is not shrinking. Adoption stopped rather than looping. Objects minted so "
					f"far: {adopted}."
				),
			)
			return {"candidates": scanned, "adopted": adopted, "reason": "adoption made no progress"}
		previous_pass = names

		scanned += len(rows)
		for row in rows:
			cso = objects.adopt_legacy_cso(
				s3_key=row.s3_object_key,
				file_size=cint(row.file_size),
				visibility="private" if cint(row.is_private) else "public",
				bucket=settings.bucket,
				settings=settings,
			)
			frappe.db.set_value("File", row.name, {"cloud_storage_object": cso.name}, update_modified=False)
			audit.record(
				"remote_adopt",
				file=row.name,
				cloud_storage_object=cso.name,
				actor=audit.SYSTEM_ACTOR,
				source="legacy_install",
				key=row.s3_object_key,
				status=cso.status,
			)
			adopted += 1

		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)
		if limit:
			break
	else:
		# The `for` exhausted its range without breaking: more passes than the candidate
		# count can justify. The intersection guard above catches the common shape (a pass
		# repeating the previous pass's rows); this catches every other way the set can fail
		# to drain, including ones nobody has named.
		frappe.log_error(
			title="cloud_file_storage: legacy object adoption hit its pass ceiling",
			message=(
				f"Adoption ran {max_passes} pass(es) over the fork's File rows without the "
				f"candidate set draining (ceiling derived from {candidate_count} candidate(s) "
				f"at {batch_size} per pass). The `File.cloud_storage_object` link is not being "
				f"written. Adoption stopped rather than looping. Objects minted so far: {adopted}."
			),
		)
		return {
			"candidates": scanned,
			"adopted": adopted,
			"reason": "adoption exceeded its pass ceiling",
		}

	return {"candidates": scanned, "adopted": adopted}


def finish() -> dict:
	"""The whole post-sync stage: the post-sync patch's body."""
	report = {
		# Settings first: `adopt_fork_objects` needs the bucket this step carries over.
		"settings": harvest_settings(),
		"ignored_doctypes": adopt_ignored_doctypes(),
		"objects": adopt_fork_objects(),
	}
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)
	return report
