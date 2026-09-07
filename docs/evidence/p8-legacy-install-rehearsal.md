# P8 evidence — the legacy-install bootstrap, proven by a real `bench migrate`

A21's acceptance is "a REAL `bench migrate` on a synthetic legacy site", and this is the
record of it. Everything below was executed on this bench on 2026-08-16 against
`cfs-p8-legacy5.local`, a site built for the run and seeded by
`cloud_file_storage/tests/legacy_fixture.py` with the DB state a `frappe_s3_attachment`
0.2.x install leaves behind. Runs 3 and 4 produced identical output; run 4
repeated it after the candidate query was rewritten from raw SQL to `frappe.get_all`, and run
5 after the L-12 credential-chain fix (both DECISIONS 2026-08-16). Those are the only changes
between them, and the observable result did not move. The rehearsal script is `p8_legacy_rehearsal.sh` (job scratch,
reproduced step by step below); the fixture, the bootstrap and the two patches are committed.

Five sites were used in all. `cfs-p8-legacy.local` and `cfs-p8-legacy2.local` are what found
the three defects recorded at the end; 3, 4 and 5 are the clean runs. They are all left in place
rather than dropped — deleting a site is a SAFETY_BOUNDARY action under this project's autonomy
guard, so each rehearsal takes a fresh site name instead.

## Reproducing it — the precondition an earlier version of this document omitted

```
docs/evidence/p8-legacy-rehearsal.sh <a-site-name-that-does-not-exist-yet>
```

The script and its three helpers are committed beside this file, so the run is repeatable
rather than described. **It creates the site with `developer_mode 0`, and that is required,
not incidental.** This bench's `common_site_config.json` sets `developer_mode: 1`, and with it
on the seeding dies at the very first step: `ModuleDef.on_update` calls
`create_modules_folder()` whenever `not self.custom and frappe.conf.get("developer_mode")`
(`frappe/core/doctype/module_def/module_def.py:29-35`), which resolves
`get_app_path("frappe_s3_attachment")` for an app that is not on the bench.
`frappe.flags.in_patch` does not cover that guard — it is the bypass for
`DocType.check_developer_mode`, which is a different one.

All rehearsal sites carry the site-level override. This document did not say so until the
independent test-engineer reproduced the run and hit it, which is the whole argument for
committing the harness: an evidence document nobody else can follow is not evidence.

**The committed harness was then executed from the repository**, unmodified, against a fresh
`cfs-p8-legacy7.local`: seed `exit=0`, pre-bootstrap migrate `exit=1` with
`ModuleNotFoundError: No module named 'frappe_s3_attachment'` (the red control), bootstrap
`exit=0`, migrate `exit=0` with 2 objects adopted and `secret_adopted: True`, second migrate a
no-op, and the credential surviving an ordinary settings save. So the reproduction instructions
above are not a description of what was once done — they are what produced this paragraph.

## What the fixture builds

Taken from the fork's own doctype JSON at the last commit before the P1 rename (`6eb9acc^`)
and from its `controller.py` upload path:

| Residue | Detail |
|---|---|
| `Module Def` | `Frappe S3 Attachment`, `app_name = frappe_s3_attachment` — the package that is no longer on the bench |
| DocTypes | `S3 File Attachment` (Single) and `S3 Ignored DocType Row` (child), `custom = 0` |
| Settings | bucket/region/endpoint/folder/access key/expiry in `tabSingles`, plus a real encrypted `secret_key` in `__Auth` |
| Ignored list | `Data Import` (which this app also seeds) and `Blog Post` (which it does not) — the second is what makes adoption of the list observable |
| Custom Field | `File-s3_object_key`, owned by the fork's module |
| File rows | one later-generation row (key in `s3_object_key`), one earlier-generation row (key overloaded into `content_hash`), one plain local file the fork never touched |
| Patch Log | `frappe_s3_attachment.patches.v0_0_1.migrate_existing_files` |
| `installed_apps` | `["frappe", "frappe_s3_attachment"]` |

## The run

### 1. RED — migrate fails before the bootstrap

```
$ bench --site cfs-p8-legacy3.local migrate
migrate exit=1
ModuleNotFoundError: No module named 'frappe_s3_attachment'
```

This is the state A21 describes: `frappe/migrate.py:110`, `frappe/modules/patch_handler.py:89`
and `frappe/model/sync.py:43` all iterate `frappe.get_installed_apps()` **unfiltered**, so the
first thing migrate does on such a site is raise. No patch of ours can fix it, because no patch
of ours runs. This is also the run that makes every green below mean something.

### 2. The dry run changes nothing

`bench --site … cfs-adopt-legacy-install --dry-run` reported the same plan it later executed,
and a full state probe before and after was byte-identical: `installed_apps` still
`["frappe", "frappe_s3_attachment"]`, `Module Def.app_name` `frappe_s3_attachment` (which is
also where it stays), our
`tabSingles` still empty, our credential still unset.

### 3. The bootstrap, then the real migrate

```
$ bench --site cfs-p8-legacy3.local cfs-adopt-legacy-install
$ bench --site cfs-p8-legacy3.local migrate
migrate exit=0
cloud_file_storage: adopted a frappe_s3_attachment install — {...}
cloud_file_storage: linked the legacy fork objects — {'settings': {'adopted':
  ['access_key_id', 'bucket', 'endpoint_url', 'key_prefix', 'private_presign_ttl', 'region',
   'use_default_credential_chain'], 'skipped_already_set': [], 'unmapped':
  ['delete_file_from_cloud', 'timeout_for_migration_job'], 'secret_adopted': True},
  'ignored_doctypes': {'adopted': ['Blog Post']}, 'objects': {'candidates': 2, 'adopted': 2}}
```

### 4. State after adoption

| Property | Value | Why it matters |
|---|---|---|
| `installed_apps` | `['frappe', 'cloud_file_storage']` | the fork's slot taken in place, order preserved |
| `Module Def.app_name` | `frappe_s3_attachment`, **unchanged** | the bootstrap reports this column and does not write it (reviewer M-5); repointing it would have made the fork's module a `bench uninstall-app` target |
| Settings | `bucket=legacy-fork-bucket`, `region=ap-south-1`, `key_prefix=attachments`, `access_key_id=AKIALEGACYFORKKEY`, `private_presign_ttl=900`, `use_default_credential_chain=0` | the fork's values, including the ones that sit on top of a shipped default |
| Credential | `secret_access_key` set; `tabSingles` holds the `************************` placeholder | adopted through the document, so an ordinary save does not destroy it |
| Ignored doctypes | `['Blog Post', 'Prepared Report', 'Package Import', 'Data Import']` | the fork's list merged with ours; neither side lost a row |
| Cloud Storage Objects | 2, both `legacy_unverified`, both with `content_sha256 = NULL` | A15: no hash, therefore no claim of verification |
| `tabFile.file_url` | unchanged, still `/api/method/frappe_s3_attachment.controller.generate_file?key=…` on both rows | A10: URLs are never mass-rewritten |
| Old-generation row | `content_hash` moved to `s3_object_key` by `v0_2_0`, then linked | both fork generations are picked up |
| Local control file | `/files/untouched.txt`, no CSO, `content_hash` intact | adoption is additive |
| Fork `Patch Log` rows | 1, unchanged; our own | 12 | no history is rewritten or forged |
| Fork `tabSingles` values | all still present | nothing of the operator's was moved |
| Fork DocType rows | `S3 File Attachment` and `S3 Ignored DocType Row` both still exist | frappe's orphan reaper skips them (`DoesNotExistError`, not `ImportError`) |

### 5. Idempotency and the save that used to be fatal

A second `bench migrate` exited 0 and changed nothing. Then, as the Desk would:

```
$ settings = frappe.get_single("Cloud Storage Settings"); settings.save()
{"secret_before_save": true, "secret_after_save": true, "unchanged": true,
 "singles_placeholder": "************************"}
```

## What the rehearsal found that review had not

1. **A partially populated Single suppresses every schema default.** The first revision wrote
   the harvested values into `tabSingles` during `[pre_model_sync]`. `Document.load_from_db`
   only falls back to `new_doc` — and therefore to the shipped defaults — when it finds *no*
   rows for the doctype, so six harvested fields were enough to leave `storage_class` empty and
   kill the next `settings.save()` in the same migrate. Fixed by harvesting post-model-sync
   through the ORM.
2. **"Already has a value" cannot tell a default from a decision.** Once the DocType exists,
   every field carries its default, so the fork's `signed_url_expiry_time` was dropped and
   `use_default_credential_chain` stayed `1` while an access key was adopted — a runtime
   holding a key it would never read. Fixed by `_is_untouched`: empty, or still equal to the
   shipped default.
3. **`set_encrypted_password` alone loses the credential on the next save.** `__Auth` held the
   secret but the document's own field was blank, and `_save_passwords`
   (`frappe/model/base_document.py:1126-1146`) calls `remove_encrypted_password` on any empty
   Password field. The adopted credential would have been destroyed by the next save of those
   settings by anyone. Fixed by adopting the secret *through* the document.

All three passed a code read. None would have been found without running the migrate.

## The case this run does NOT cover, and how it is covered instead

The fixture's fork holds an access key **and** a retrievable secret, which is the ordinary
case. The one where it holds a key whose `__Auth` row is gone — security finding L-12 — is not
reachable from a rehearsal without seeding a deliberately broken fixture, so it is covered by
`test_legacy_install.TestAdoptingAKeyWithoutItsSecret` instead, including the assertion that
matters: after the harvest, the settings document is **reloaded and saved again**, because that
second save is where the credential-pairing error actually fires. Reverting the fix makes that
test raise the real `ValidationError: Set both Access Key ID and Secret Access Key…`.

## What this run does not prove

- **No S3 endpoint was involved.** `adopt_fork_objects` deliberately makes no S3 call — a
  migrate that needs the bucket reachable is a migrate that fails during an outage — so the
  objects it mints are `legacy_unverified` with no hash. That those keys really exist in a real
  bucket, and that their bytes hash to what the objects later claim, is
  `migration.adoption.promote_adopted_object`'s job and is covered by the migration suite and
  the MinIO job, not here.
- **Scale.** Three File rows, not 1.2M. The adoption loop is the same batched shape as
  `patches/v0_2_0/backfill_s3_object_key` (5,000 rows, commit, re-read the remainder), but its
  behaviour at 1.2M rows is projected from that shape, not measured.
- **A site with Custom DocPerms, workflows or a customised File form.** The fixture is the
  fork's own residue and nothing else.
