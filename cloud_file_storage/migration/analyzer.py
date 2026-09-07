"""SCAN_DB / SCAN_FS / CLASSIFY — the analysis snapshot *is* the migration plan (ADR-M1).

Two of the three sanctioned raw-SQL sites against `tabFile` live here (docs/INVARIANTS.md invariant
5): the keyset scan in :func:`scan_db_page` and the classification reads in
:func:`_classify_conflicting_url`. Both are SELECTs — this module never writes to
`tabFile`. Raw SQL against the *migration* tables is not restricted by that invariant and
is used freely, because a per-row `get_doc` over 1.2M rows is not a migration, it is an
outage.

Three properties the implementation is built around:

* **Every page is a checkpoint.** One transaction per page, then a commit that also writes
  the cursor. A crash re-runs at most one page, and re-running a page is a no-op because
  A33 names make the inserts collide on the primary key.
* **No hashing here** (ADR-M11). Classification needs the DB's `content_hash`, existence
  and size; hashing 100GB is UPLOAD's single streamed pass.
* **Counts are derived, never accumulated.** A24 rules `ref_count` out of the incremental
  upsert and into a `GROUP BY` in CLASSIFY, because an incremental counter double-counts
  every page a crash makes us re-run — silently, and in the direction that looks healthy.
"""

import os
import urllib.parse
from datetime import datetime

import frappe
from frappe.utils import cint, now_datetime

from cloud_file_storage.migration import audit, names
from cloud_file_storage.storage import modes

CAMPAIGN_DOCTYPE = "Cloud Migration Campaign"
OBJECT_DOCTYPE = "Cloud Migration Object"
REF_DOCTYPE = "Cloud Migration File Ref"
CONFLICT_DOCTYPE = "Cloud Migration Conflict"

#: Rows per keyset page. 2000 File rows is a few hundred KB of result set and ~600 pages at
#: production scale — small enough that re-running one after a crash costs nothing.
SCAN_PAGE_SIZE = 2000

#: Filesystem entries buffered before one `executemany` UPDATE during SCAN_FS.
FS_BUFFER_SIZE = 1000

#: The fork's private endpoint. The rename left these URLs in `tabFile.file_url` on purpose
#: (A10) — they still resolve through `api.compat.legacy_generate_file`.
LEGACY_ENDPOINTS = (
	"/api/method/frappe_s3_attachment.controller.generate_file",
	"/api/method/cloud_file_storage.api.compat.legacy_generate_file",
)

PUBLIC_PREFIX = "/files/"
PRIVATE_PREFIX = "/private/files/"


# --------------------------------------------------------------------------- preflight


def ensure_analysis_indexes():
	"""Design §3.1 — the indexes a 1.2M-row scan and every later phase depend on.

	Delegates to `install.ensure_file_indexes` rather than repeating the DDL: the same
	`tabFile.content_hash(32)` index is what keeps the A6 locking recount from escalating to
	a table scan, so there must be exactly one definition of it. Idempotent, and it commits
	before its DDL (A24), which is why it runs here and not inside a locked transition.
	"""
	from cloud_file_storage.install import ensure_file_indexes, ensure_s3_object_key_index

	ensure_file_indexes()
	if frappe.db.has_column("File", "s3_object_key"):
		ensure_s3_object_key_index()


# ---------------------------------------------------------------------- URL normalisation


def normalize_url(file_url: str | None) -> str:
	"""Strip an own-site absolute prefix, percent-decode and trim.

	Core writes relative URLs, but `file_url` is a free-text column and sites do end up with
	`https://this-site/files/x.pdf` in it. Those are the same physical file as `/files/x.pdf`
	and must land on the same identity, or the migration uploads the bytes twice and links
	half the rows.
	"""
	url = (file_url or "").strip()
	if not url:
		return ""

	if url.startswith("http://") or url.startswith("https://"):
		parsed = urllib.parse.urlsplit(url)
		if _is_own_site(parsed.netloc):
			url = parsed.path + (f"?{parsed.query}" if parsed.query else "")
		else:
			return url

	return urllib.parse.unquote(url)


def _is_own_site(netloc: str) -> bool:
	host = (netloc or "").split("@")[-1].split(":")[0].lower()
	if not host:
		return False
	site = (frappe.local.site or "").lower()
	candidates = {site}
	host_name = frappe.get_conf().get("host_name") or ""
	if host_name:
		candidates.add(urllib.parse.urlsplit(host_name).netloc.split(":")[0].lower() or host_name.lower())
	return host in candidates


def legacy_key_from_url(url: str) -> str | None:
	"""The `key` query argument of a fork `generate_file` URL, if this is one."""
	path, _, query = url.partition("?")
	if path not in LEGACY_ENDPOINTS:
		return None
	key = urllib.parse.parse_qs(query).get("key") or []
	return key[0] if key and key[0] else None


def classify_identity(row) -> tuple[str, str]:
	"""`(identity_kind, identity)` for one File row — the physical-object unit (ADR-M3).

	The URL decides, and `s3_object_key` is consulted only for rows whose URL is *not*
	canonical. migration-engine.md §3.2 phrases the rule as "legacy endpoint **or**
	`s3_object_key` set", which would reclassify a healthy local file as an already-remote
	adoption the moment a stale locator column survived on it — and adoption moves no bytes,
	so that file would never be uploaded and would simply be missing under S3_ONLY. The URL
	is what serving resolves and what business fields store, so the URL wins.
	"""
	url = normalize_url(row.get("file_url"))
	legacy_key = (row.get("s3_object_key") or "").strip()

	if url.startswith(PRIVATE_PREFIX):
		return "local_private", f"local_private::{url}"
	if url.startswith(PUBLIC_PREFIX):
		return "local_public", f"local_public::{url}"

	key_from_url = legacy_key_from_url(url)
	if key_from_url:
		return "legacy_fork_key", f"s3legacy::{key_from_url}"
	if legacy_key:
		# A raw bucket URL (the fork's public path) or an unrecognised shape, with the key
		# still recorded. Multiple URL spellings collapse onto one key — the point of using
		# the key as identity at all.
		return "legacy_fork_key", f"s3legacy::{legacy_key}"
	if url.startswith("http://") or url.startswith("https://"):
		return "remote_https", f"remote_https::{url}"

	return "corrupt_metadata", f"corrupt::{row['name']}"


def site_file_roots() -> tuple[str, ...]:
	"""The two trees a File's bytes may legitimately live in."""
	return (frappe.get_site_path("public", "files"), frappe.get_site_path("private", "files"))


def confined_to_site_files(path: str | None) -> bool:
	"""Whether `path` resolves inside this site's file trees.

	**`disk_path` is a Desk-writable column** on `Cloud Migration Object` (System Manager holds
	`write`, and the field is not read-only), so any code that opens, copies or unlinks it is
	reading an operator-supplied path. Independent security review found the destroy sinks first
	(CLEANUP) and then the **read** sinks, which are worse: an unconfined read does not damage
	the host, it *publishes* it — `site_config.json` carries the DB password and the S3 secret,
	and UPLOAD would put those bytes in the bucket as an ordinary, servable object.

	Both sides are resolved, so a symlink cannot step outside, and the `+ os.sep` guard stops a
	sibling prefix (`.../files_evil`) from matching.
	"""
	return confined_under(path, site_file_roots())


def confined_under(path: str | None, roots: tuple[str, ...]) -> bool:
	"""The one implementation of "is this path inside one of these roots".

	Kept single deliberately. Two byte-identical copies existed — this one and
	`cleanup._confined_under` — which is the drift risk that matters for a security predicate:
	hardening one (a null-byte guard, `commonpath`, a `normpath` step) would silently leave the
	other on the old semantics, and the other guarded the sinks that *destroy*.
	"""
	if not path:
		return False
	candidate = os.path.realpath(path)
	for root in roots:
		resolved = os.path.realpath(root)
		if candidate == resolved or candidate.startswith(resolved + os.sep):
			return True
	return False


def local_disk_path(url: str) -> str | None:
	"""The absolute path a canonical URL denotes, or None when it is not canonical.

	Refuses anything that escapes the site's file trees: `file_url` is operator- and
	integration-writable, and this path is later handed to `os.rename` by CLEANUP.
	"""
	if url.startswith(PRIVATE_PREFIX):
		root = frappe.get_site_path("private", "files")
		relative = url[len(PRIVATE_PREFIX) :]
	elif url.startswith(PUBLIC_PREFIX):
		root = frappe.get_site_path("public", "files")
		relative = url[len(PUBLIC_PREFIX) :]
	else:
		return None

	if not relative:
		return None

	candidate = os.path.realpath(os.path.join(root, relative))
	if candidate != os.path.realpath(root) and not candidate.startswith(os.path.realpath(root) + os.sep):
		return None
	return candidate


def url_for_disk_path(path: str, *, private: bool) -> str:
	root = frappe.get_site_path("private" if private else "public", "files")
	relative = os.path.relpath(path, root).replace(os.sep, "/")
	return (PRIVATE_PREFIX if private else PUBLIC_PREFIX) + relative


# ------------------------------------------------------------------------------ SCAN_DB


def _campaign(campaign: str):
	return frappe.get_doc(CAMPAIGN_DOCTYPE, campaign)


def _set_campaign(campaign: str, values: dict):
	frappe.db.set_value(CAMPAIGN_DOCTYPE, campaign, values, update_modified=False)


def _file_columns() -> list[str]:
	columns = [
		"name",
		"file_name",
		"file_url",
		"file_size",
		"is_private",
		"is_folder",
		"content_hash",
		"attached_to_doctype",
		"attached_to_name",
		"attached_to_field",
		"thumbnail_url",
	]
	# A13 — the analyzer depends on the deprecated locator and guards on the column.
	if frappe.db.has_column("File", "s3_object_key"):
		columns.append("s3_object_key")
	return columns


def scan_db(campaign: str) -> dict:
	"""Keyset-scan `tabFile` into object and ref rows. Resumable from `scan_cursor_file`."""
	camp = _campaign(campaign)
	_set_campaign(campaign, {"active_phase": "SCAN_DB"})
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)

	seen = 0
	while True:
		processed = scan_db_page(camp)
		if not processed:
			break
		seen += processed

	return {"rows": seen}


def scan_db_page(camp) -> int:
	"""One page: read, group, upsert, commit. The commit *is* the checkpoint."""
	cursor = camp.scan_cursor_file or ""
	columns = ", ".join(f"`{column}`" for column in _file_columns())

	rows = frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- interpolates only closed-set identifiers (module constants / _file_columns()); every value is parameter-bound
		f"""
		SELECT {columns}
		FROM `tabFile`
		WHERE `name` > %(cursor)s
		ORDER BY `name`
		LIMIT %(page_size)s
		""",
		{"cursor": cursor, "page_size": SCAN_PAGE_SIZE},
		as_dict=True,
	)
	if not rows:
		return 0

	ignored = modes.ignored_doctypes()
	include_public = cint(camp.include_public)
	include_private = cint(camp.include_private)

	objects: dict[str, dict] = {}
	refs: list[dict] = []
	stamp = now_datetime()

	for row in rows:
		if cint(row.is_folder):
			continue

		identity_kind, identity = classify_identity(row)
		url = normalize_url(row.file_url)

		if identity_kind == "local_public" and not include_public:
			continue
		if identity_kind == "local_private" and not include_private:
			continue

		url_hash = names.identity_hash(identity)
		object_name = names.object_name(camp.name, url_hash)

		if url_hash not in objects:
			objects[url_hash] = _new_object_row(camp, row, identity_kind, url, url_hash, object_name, stamp)

		refs.append(
			{
				"name": names.file_ref_name(camp.name, row.name),
				"campaign": camp.name,
				"migration_object": object_name,
				"file": row.name,
				"file_name": row.file_name,
				"file_url": row.file_url,
				"content_hash": row.content_hash,
				"is_private": cint(row.is_private),
				"attached_to_doctype": row.attached_to_doctype,
				"attached_to_name": row.attached_to_name,
				"attached_to_field": row.attached_to_field,
				"ignored_parent": 1 if row.attached_to_doctype in ignored else 0,
				"linked": 0,
				"url_rewritten": 0,
				"creation": stamp,
				"modified": stamp,
			}
		)

		thumbnail = _thumbnail_object_row(camp, row, object_name, stamp)
		if thumbnail and thumbnail["url_hash"] not in objects:
			objects[thumbnail["url_hash"]] = thumbnail

	_bulk_upsert(OBJECT_DOCTYPE, list(objects.values()))
	_bulk_upsert(REF_DOCTYPE, refs)

	last = rows[-1]["name"]
	_set_campaign(camp.name, {"scan_cursor_file": last})
	camp.scan_cursor_file = last
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)
	return len(rows)


def _new_object_row(camp, row, identity_kind, url, url_hash, object_name, stamp) -> dict:
	is_private_column = cint(row.is_private)
	url_is_private = url.startswith(PRIVATE_PREFIX)
	local = identity_kind in ("local_public", "local_private")

	legacy_key = None
	if identity_kind == "legacy_fork_key":
		legacy_key = legacy_key_from_url(url) or (row.get("s3_object_key") or "").strip()

	return {
		"name": object_name,
		"campaign": camp.name,
		"url_hash": url_hash,
		"identity_kind": identity_kind,
		"file_url": row.file_url,
		"status": "Pending",
		"is_private": 1 if (url_is_private if local else is_private_column) else 0,
		# A19: the column and the URL prefix disagree, so nobody knows what visibility these
		# bytes were meant to have. Only meaningful for a local URL — a remote row has no
		# prefix to disagree with.
		"privacy_mismatch": 1 if (local and bool(is_private_column) != url_is_private) else 0,
		"disk_path": local_disk_path(url) if local else None,
		"size_bytes": cint(row.file_size),
		"legacy_key": legacy_key,
		"legacy_bucket": None,
		"is_thumbnail": 0,
		"creation": stamp,
		"modified": stamp,
	}


def _thumbnail_object_row(camp, row, parent_object, stamp) -> dict | None:
	"""A thumbnail is a physical object of its own, owned by its source (PLAN §A).

	It gets a row so the derived-object migration has something to drive, and so a local
	thumbnail is never quarantined before its source is verified and its availability is
	guaranteed.
	"""
	thumbnail_url = normalize_url(row.get("thumbnail_url"))
	if not thumbnail_url or not thumbnail_url.startswith((PUBLIC_PREFIX, PRIVATE_PREFIX)):
		return None

	identity = f"thumbnail::{thumbnail_url}"
	url_hash = names.identity_hash(identity)
	return {
		"name": names.object_name(camp.name, url_hash),
		"campaign": camp.name,
		"url_hash": url_hash,
		"identity_kind": "local_private" if thumbnail_url.startswith(PRIVATE_PREFIX) else "local_public",
		"file_url": row.thumbnail_url,
		"status": "Pending",
		"is_private": 1 if thumbnail_url.startswith(PRIVATE_PREFIX) else 0,
		"privacy_mismatch": 0,
		"disk_path": local_disk_path(thumbnail_url),
		"size_bytes": 0,
		"legacy_key": None,
		"legacy_bucket": None,
		"is_thumbnail": 1,
		"parent_object": parent_object,
		"thumbnail_disk_path": local_disk_path(thumbnail_url),
		"creation": stamp,
		"modified": stamp,
	}


def _bulk_upsert(doctype: str, rows: list[dict]):
	"""`INSERT IGNORE` keyed on the A33 name, so a replayed page changes nothing.

	Uses `frappe.db.bulk_insert` (query builder, `.ignore()` on MariaDB) rather than a
	hand-written statement. Deliberately *insert-only*: a re-run must not overwrite state a
	later phase has already written onto an object — the identity fields it would rewrite
	are derived from the same File row and cannot have changed, while `status`,
	`attempt_count` and the hashes very much can.
	"""
	if not rows:
		return

	fields = sorted({key for row in rows for key in row})
	defaults = {"owner": "Administrator", "modified_by": "Administrator", "docstatus": 0, "idx": 0}
	fields = list(fields) + list(defaults)

	values = [tuple(row.get(field, defaults.get(field)) for field in fields) for row in rows]
	frappe.db.bulk_insert(doctype, fields, values, ignore_duplicates=True)


# ------------------------------------------------------------------------------ SCAN_FS


def scan_fs(campaign: str) -> dict:
	"""Streaming walk of both file trees, matching disk against the scanned identities.

	Restarts from zero after a crash by design (ADR): the walk is a pure idempotent upsert
	and ~1.2M `stat` calls, and a sortable cursor over an unordered million-entry directory
	would cost more memory than the restart costs time.
	"""
	camp = _campaign(campaign)
	_set_campaign(campaign, {"active_phase": "SCAN_FS", "fs_entries_seen": 0})
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)

	from cloud_file_storage.migration import cleanup

	quarantine_root = os.path.realpath(cleanup.quarantine_root())
	seen = 0
	buffer: list[dict] = []

	for private in (False, True):
		if private and not cint(camp.include_private):
			continue
		if not private and not cint(camp.include_public):
			continue

		root = frappe.get_site_path("private" if private else "public", "files")
		if not os.path.isdir(root):
			continue

		for path in _walk(root, skip=quarantine_root):
			try:
				stat = os.stat(path)
			except OSError:
				# Vanished between the scandir and the stat. Nothing to record: SCAN_DB
				# already knows about the File row, and CLASSIFY will call it
				# `missing_physical` rather than inventing a size for it.
				continue

			url = url_for_disk_path(path, private=private)
			identity_kind = "local_private" if private else "local_public"
			url_hash = names.identity_hash(f"{identity_kind}::{url}")
			buffer.append(
				{
					"name": names.object_name(campaign, url_hash),
					"url_hash": url_hash,
					"url": url,
					# `realpath`, so SCAN_FS and `local_disk_path` write the same spelling of
					# the same file. `get_site_path` returns a path relative to the bench, and
					# two spellings of one path end up in quarantine paths and audit rows as
					# if they were two different files.
					"disk_path": os.path.realpath(path),
					"disk_size": stat.st_size,
					"disk_mtime": datetime.fromtimestamp(stat.st_mtime),
					"is_private": 1 if private else 0,
				}
			)
			seen += 1

			if len(buffer) >= FS_BUFFER_SIZE:
				_record_orphans(campaign, _flush_fs_buffer(campaign, buffer))
				_set_campaign(campaign, {"fs_entries_seen": seen})
				frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)
				buffer = []

	_record_orphans(campaign, _flush_fs_buffer(campaign, buffer))
	_set_campaign(campaign, {"fs_entries_seen": seen})
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)
	return {"entries": seen}


def _walk(root: str, *, skip: str):
	"""Recursive `os.scandir`, never following a symlink out of the tree."""
	stack = [root]
	while stack:
		current = stack.pop()
		if os.path.realpath(current) == skip:
			continue
		try:
			entries = list(os.scandir(current))
		except OSError:
			continue
		for entry in entries:
			try:
				if entry.is_dir(follow_symlinks=False):
					stack.append(entry.path)
				elif entry.is_file(follow_symlinks=False):
					yield entry.path
			except OSError:
				continue


def _flush_fs_buffer(campaign: str, buffer: list[dict]) -> list[dict]:
	"""Stamp the disk facts onto the matching objects; return the entries that matched none.

	The `INSERT … ON DUPLICATE KEY UPDATE` is filtered to rows that already exist, so only
	the UPDATE branch can ever fire: a file with no File row is an **orphan**, and inserting
	a half-built object row for it here would smuggle it into the work queue instead of the
	report.
	"""
	if not buffer:
		return []

	present = set(
		frappe.db.get_all(
			OBJECT_DOCTYPE,
			filters={"name": ("in", [entry["name"] for entry in buffer])},
			pluck="name",
			limit_page_length=0,
		)
	)
	matched = [entry for entry in buffer if entry["name"] in present]

	if matched:
		placeholders = ", ".join(["(%s, %s, %s, %s, %s, %s)"] * len(matched))
		values: list = []
		for entry in matched:
			values += [
				entry["name"],
				campaign,
				entry["url_hash"],
				entry["disk_size"],
				entry["disk_mtime"],
				entry["disk_path"],
			]
		frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- interpolates only closed-set identifiers (module constants / _file_columns()); every value is parameter-bound
			f"""
			INSERT INTO `tab{OBJECT_DOCTYPE}` (name, campaign, url_hash, disk_size, disk_mtime, disk_path)
			VALUES {placeholders}
			ON DUPLICATE KEY UPDATE
				on_disk = 1,
				disk_size = VALUES(disk_size),
				disk_mtime = VALUES(disk_mtime),
				disk_path = VALUES(disk_path)
			""",
			values,
		)

	return [entry for entry in buffer if entry["name"] not in present]


def _record_orphans(campaign: str, orphans: list[dict]):
	"""Bytes on disk that no File row claims. Reported, never auto-processed."""
	if not orphans:
		return

	stamp = now_datetime()
	rows = [
		{
			"name": orphan["name"],
			"campaign": campaign,
			"url_hash": orphan["url_hash"],
			"identity_kind": "orphan_disk",
			"file_url": orphan["url"],
			"classification": "orphan_physical",
			# Never auto-processed: an orphan is a report line, not work. Uploading bytes no
			# File row references would create objects nothing can ever reach, and deleting
			# them is not this engine's decision to make.
			"status": "Skipped",
			"skip_reason": "orphan_physical",
			"on_disk": 1,
			"disk_path": orphan["disk_path"],
			"disk_size": orphan["disk_size"],
			"disk_mtime": orphan["disk_mtime"],
			"is_private": orphan["is_private"],
			"creation": stamp,
			"modified": stamp,
		}
		for orphan in orphans
	]
	_bulk_upsert(OBJECT_DOCTYPE, rows)


# ----------------------------------------------------------------------------- CLASSIFY

#: Ordered, each step committed and recorded on the campaign so CLASSIFY resumes where it
#: stopped. Order matters: `ref_counts` feeds everything, and the conflict steps must run
#: before `healthy_unique` claims what is left.
CLASSIFY_STEPS = (
	"ref_counts",
	"ignored_scope",
	"missing_physical",
	"conflicting_url",
	"privacy_mismatch",
	"remote_and_legacy",
	"corrupt_metadata",
	"shared_content",
	"dup_filename",
	"shared_url",
	"healthy_unique",
	"reconcile_counters",
)


def classify(campaign: str) -> dict:
	camp = _campaign(campaign)
	_set_campaign(campaign, {"active_phase": "CLASSIFY"})
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)

	done = camp.classify_step or ""
	start = CLASSIFY_STEPS.index(done) + 1 if done in CLASSIFY_STEPS else 0

	for step in CLASSIFY_STEPS[start:]:
		globals()[f"_classify_{step}"](campaign)
		_set_campaign(campaign, {"classify_step": step})
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)

	return {"steps": len(CLASSIFY_STEPS) - start}


def _classify_ref_counts(campaign: str):
	"""A24 — `ref_count` is derived by GROUP BY, never accumulated during the scan.

	Also derives `ignored_ref_count` (A17) and the consensus `db_content_hash`: NULL when
	the refs disagree, which is exactly the signal `conflicting_url` keys off.
	"""
	frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- interpolates only closed-set identifiers (module constants / _file_columns()); every value is parameter-bound
		f"""
		UPDATE `tab{OBJECT_DOCTYPE}` o
		JOIN (
			SELECT migration_object,
			       COUNT(*) AS refs,
			       SUM(ignored_parent) AS ignored_refs,
			       COUNT(DISTINCT content_hash) AS hashes,
			       MIN(content_hash) AS one_hash
			FROM `tab{REF_DOCTYPE}`
			WHERE campaign = %(campaign)s
			GROUP BY migration_object
		) r ON r.migration_object = o.name
		SET o.ref_count = r.refs,
		    o.ignored_ref_count = r.ignored_refs,
		    o.db_content_hash = CASE WHEN r.hashes = 1 THEN r.one_hash ELSE NULL END
		WHERE o.campaign = %(campaign)s
		""",
		{"campaign": campaign},
	)
	frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- interpolates only closed-set identifiers (module constants / _file_columns()); every value is parameter-bound
		f"""
		UPDATE `tab{OBJECT_DOCTYPE}` o
		JOIN (
			SELECT migration_object, MIN(file) AS first_file
			FROM `tab{REF_DOCTYPE}`
			WHERE campaign = %(campaign)s
			GROUP BY migration_object
		) r ON r.migration_object = o.name
		SET o.primary_file = r.first_file
		WHERE o.campaign = %(campaign)s AND (o.primary_file IS NULL OR o.primary_file = '')
		""",
		{"campaign": campaign},
	)


def _classify_ignored_scope(campaign: str):
	"""A17 — LOCAL_OPERATIONAL parents.

	An object every one of whose refs hangs off Data Import / Prepared Report / Package
	Import is `Skipped`: those bytes are consumed through local paths and are never claimed
	as migrated cloud attachments. An object with a *mix* is not skipped — the other refs
	are real attachments — but it keeps `ignored_ref_count > 0`, and CLEANUP hard-refuses
	any object carrying one.
	"""
	frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- interpolates only closed-set identifiers (module constants / _file_columns()); every value is parameter-bound
		f"""
		UPDATE `tab{OBJECT_DOCTYPE}`
		SET status = 'Skipped', skip_reason = 'ignored_doctype'
		WHERE campaign = %(campaign)s
		  AND status = 'Pending'
		  AND ref_count > 0
		  AND ignored_ref_count = ref_count
		""",
		{"campaign": campaign},
	)


def _classify_missing_physical(campaign: str):
	"""A local URL with nothing behind it. Blocker — except for thumbnails.

	A missing thumbnail is derived data whose source is still there, and PLAN §A says to
	regenerate it from the verified source rather than to halt the campaign over it. Halting
	would also be the worse failure in practice: thumbnails go missing routinely (a privacy
	flip relocates one, core's `delete_file` removes one from a path it wrote elsewhere), and
	a Blocker per stale preview would bury the conflicts that matter.
	"""
	frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- interpolates only closed-set identifiers (module constants / _file_columns()); every value is parameter-bound
		f"""
		UPDATE `tab{OBJECT_DOCTYPE}`
		SET skip_reason = 'thumbnail_regenerate'
		WHERE campaign = %(campaign)s AND is_thumbnail = 1 AND on_disk = 0 AND status = 'Pending'
		""",
		{"campaign": campaign},
	)

	rows = frappe.db.get_all(
		OBJECT_DOCTYPE,
		filters={
			"campaign": campaign,
			"identity_kind": ("in", ("local_public", "local_private")),
			"on_disk": 0,
			"status": "Pending",
			"is_thumbnail": 0,
		},
		fields=["name", "file_url", "ref_count"],
		limit_page_length=0,
	)
	for row in rows:
		_flag(
			campaign,
			row.name,
			classification="missing_physical",
			conflict_type="missing_physical",
			severity="Blocker",
			details={"file_url": row.file_url, "ref_count": row.ref_count},
		)


def _classify_conflicting_url(campaign: str):
	"""One URL, two byte expectations. Nothing can pick the bytes automatically."""
	rows = frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- interpolates only closed-set identifiers (module constants / _file_columns()); every value is parameter-bound
		f"""
		SELECT migration_object, COUNT(DISTINCT content_hash) AS hashes,
		       GROUP_CONCAT(DISTINCT content_hash) AS hash_list
		FROM `tab{REF_DOCTYPE}`
		WHERE campaign = %(campaign)s AND content_hash IS NOT NULL AND content_hash != ''
		GROUP BY migration_object
		HAVING hashes > 1
		""",
		{"campaign": campaign},
		as_dict=True,
	)
	for row in rows:
		_flag(
			campaign,
			row.migration_object,
			classification="conflicting_url",
			conflict_type="conflicting_url",
			severity="Blocker",
			details={"content_hashes": (row.hash_list or "").split(",")},
		)


def _classify_privacy_mismatch(campaign: str):
	"""A19 — Blocker. The operator decides the true visibility before anything is uploaded."""
	rows = frappe.db.get_all(
		OBJECT_DOCTYPE,
		filters={"campaign": campaign, "privacy_mismatch": 1, "status": "Pending"},
		fields=["name", "file_url", "is_private"],
		limit_page_length=0,
	)
	for row in rows:
		_flag(
			campaign,
			row.name,
			classification=None,
			conflict_type="ambiguous_privacy",
			severity="Blocker",
			details={"file_url": row.file_url, "url_says_private": bool(row.is_private)},
		)


def _classify_remote_and_legacy(campaign: str):
	camp = _campaign(campaign)

	frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- interpolates only closed-set identifiers (module constants / _file_columns()); every value is parameter-bound
		f"""
		UPDATE `tab{OBJECT_DOCTYPE}`
		SET classification = 'legacy_fork'
		WHERE campaign = %(campaign)s AND identity_kind = 'legacy_fork_key'
		""",
		{"campaign": campaign},
	)
	if not cint(camp.adopt_legacy_fork_rows):
		frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- interpolates only closed-set identifiers (module constants / _file_columns()); every value is parameter-bound
			f"""
			UPDATE `tab{OBJECT_DOCTYPE}`
			SET status = 'Skipped', skip_reason = 'adoption_disabled'
			WHERE campaign = %(campaign)s AND identity_kind = 'legacy_fork_key' AND status = 'Pending'
			""",
			{"campaign": campaign},
		)

	frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- interpolates only closed-set identifiers (module constants / _file_columns()); every value is parameter-bound
		f"""
		UPDATE `tab{OBJECT_DOCTYPE}`
		SET classification = 'remote_url'
		WHERE campaign = %(campaign)s AND identity_kind = 'remote_https'
		""",
		{"campaign": campaign},
	)

	# A18 — remote rows are adopted only when the URL points at the bucket this site is
	# configured for. Anything else is somebody else's storage and stays untouched.
	from cloud_file_storage.migration import adoption

	for row in frappe.db.get_all(
		OBJECT_DOCTYPE,
		filters={"campaign": campaign, "identity_kind": "remote_https", "status": "Pending"},
		fields=["name", "file_url"],
		limit_page_length=0,
	):
		parsed = adoption.parse_remote_url(row.file_url) if cint(camp.adopt_remote_https_rows) else None
		if parsed:
			frappe.db.set_value(
				OBJECT_DOCTYPE,
				row.name,
				{"legacy_bucket": parsed["bucket"], "legacy_key": parsed["key"]},
				update_modified=False,
			)
		else:
			frappe.db.set_value(
				OBJECT_DOCTYPE,
				row.name,
				{"status": "Skipped", "skip_reason": "remote_not_ours"},
				update_modified=False,
			)


def _classify_corrupt_metadata(campaign: str):
	rows = frappe.db.get_all(
		OBJECT_DOCTYPE,
		filters={"campaign": campaign, "identity_kind": "corrupt_metadata"},
		fields=["name", "file_url"],
		limit_page_length=0,
	)
	for row in rows:
		_flag(
			campaign,
			row.name,
			classification="corrupt_metadata",
			conflict_type="corrupt_metadata",
			severity="Warning",
			details={"file_url": row.file_url},
		)


def _classify_shared_content(campaign: str):
	"""Identical bytes behind different URLs — UPLOAD's dedup path reuses one object."""
	frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- interpolates only closed-set identifiers (module constants / _file_columns()); every value is parameter-bound
		f"""
		UPDATE `tab{OBJECT_DOCTYPE}` o
		JOIN (
			SELECT db_content_hash, disk_size
			FROM `tab{OBJECT_DOCTYPE}`
			WHERE campaign = %(campaign)s
			  AND db_content_hash IS NOT NULL AND db_content_hash != ''
			  AND on_disk = 1
			GROUP BY db_content_hash, disk_size
			HAVING COUNT(*) > 1
		) d ON d.db_content_hash = o.db_content_hash AND d.disk_size = o.disk_size
		SET o.classification = 'shared_content'
		WHERE o.campaign = %(campaign)s
		  AND o.status = 'Pending'
		  AND (o.classification IS NULL OR o.classification = '')
		""",
		{"campaign": campaign},
	)


def _classify_dup_filename(campaign: str):
	"""Informational only. A filename is not identity — flagging it must never block work."""
	frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- interpolates only closed-set identifiers (module constants / _file_columns()); every value is parameter-bound
		f"""
		UPDATE `tab{OBJECT_DOCTYPE}` o
		JOIN `tab{REF_DOCTYPE}` r ON r.migration_object = o.name
		JOIN (
			SELECT file_name
			FROM `tab{REF_DOCTYPE}`
			WHERE campaign = %(campaign)s AND file_name IS NOT NULL AND file_name != ''
			GROUP BY file_name
			HAVING COUNT(DISTINCT migration_object) > 1
		) d ON d.file_name = r.file_name
		SET o.dup_filename = 1
		WHERE o.campaign = %(campaign)s
		""",
		{"campaign": campaign},
	)


def _classify_shared_url(campaign: str):
	frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- interpolates only closed-set identifiers (module constants / _file_columns()); every value is parameter-bound
		f"""
		UPDATE `tab{OBJECT_DOCTYPE}`
		SET classification = 'shared_url'
		WHERE campaign = %(campaign)s
		  AND ref_count > 1
		  AND (classification IS NULL OR classification = '')
		""",
		{"campaign": campaign},
	)


def _classify_healthy_unique(campaign: str):
	frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- interpolates only closed-set identifiers (module constants / _file_columns()); every value is parameter-bound
		f"""
		UPDATE `tab{OBJECT_DOCTYPE}`
		SET classification = 'healthy_unique'
		WHERE campaign = %(campaign)s
		  AND identity_kind IN ('local_public', 'local_private')
		  AND on_disk = 1
		  AND (classification IS NULL OR classification = '')
		""",
		{"campaign": campaign},
	)


def _classify_reconcile_counters(campaign: str):
	refresh_counters(campaign)


def _flag(campaign, object_name, *, classification, conflict_type, severity, details):
	"""Set the classification, halt the object and open exactly one conflict row.

	"Exactly one" matters: CLASSIFY is resumable and its steps can re-run, and a duplicate
	Blocker per re-run turns the triage queue into noise nobody reads.
	"""
	values = {"status": "Conflict"}
	if classification:
		values["classification"] = classification

	existing = frappe.db.get_value(
		CONFLICT_DOCTYPE,
		{"campaign": campaign, "migration_object": object_name, "conflict_type": conflict_type},
		"name",
	)
	if not existing:
		conflict = frappe.new_doc(CONFLICT_DOCTYPE)
		conflict.update(
			{
				"campaign": campaign,
				"migration_object": object_name,
				"conflict_type": conflict_type,
				"status": "Open",
				"severity": severity,
				"details": frappe.as_json(details),
			}
		)
		conflict.insert(ignore_permissions=True)
		existing = conflict.name

	values["conflict"] = existing
	frappe.db.set_value(OBJECT_DOCTYPE, object_name, values, update_modified=False)
	return existing


# ----------------------------------------------------------------------------- counters


#: `Cloud Migration Object.status` → the campaign counter it feeds (ADR-M13).
STATUS_COUNTERS = {
	"Pending": "objects_pending",
	"Uploading": "objects_uploading",
	"Uploaded": "objects_uploaded",
	"Verifying": "objects_uploading",
	"Verified": "objects_verified",
	"CleanupEligible": "objects_verified",
	"Quarantined": "objects_cleaned",
	"CleanedUp": "objects_cleaned",
	"Adopted": "objects_adopted",
	"DedupReused": "objects_dedup_reused",
	"Failed": "objects_failed",
	"Conflict": "objects_conflict",
	"Skipped": "objects_skipped",
}


def refresh_counters(campaign: str) -> dict:
	"""One indexed GROUP BY, replacing every counter (ADR-M13).

	Recomputed rather than incremented: a crashed transaction leaves an increment behind and
	the drift never self-heals. This runs at every batch boundary, so the dashboard is at
	worst one batch stale and never wrong in a way that hides work.
	"""
	rows = frappe.db.sql(  # nosemgrep: frappe-sql-format-injection -- interpolates only closed-set identifiers (module constants / _file_columns()); every value is parameter-bound
		f"""
		SELECT status, COUNT(*) AS objects, COALESCE(SUM(size_bytes), 0) AS bytes
		FROM `tab{OBJECT_DOCTYPE}`
		WHERE campaign = %(campaign)s
		GROUP BY status
		""",
		{"campaign": campaign},
		as_dict=True,
	)

	values = dict.fromkeys(set(STATUS_COUNTERS.values()), 0)
	total_objects = 0
	bytes_total = 0
	bytes_uploaded = 0
	bytes_verified = 0

	for row in rows:
		counter = STATUS_COUNTERS.get(row.status)
		if counter:
			values[counter] += cint(row.objects)
		total_objects += cint(row.objects)
		bytes_total += cint(row.bytes)
		if row.status in ("Uploaded", "Verifying", "Verified", "CleanupEligible", "Quarantined", "CleanedUp"):
			bytes_uploaded += cint(row.bytes)
		if row.status in ("Verified", "CleanupEligible", "Quarantined", "CleanedUp"):
			bytes_verified += cint(row.bytes)

	values.update(
		{
			"total_objects": total_objects,
			"total_files": frappe.db.count(REF_DOCTYPE, {"campaign": campaign}),
			"bytes_total": bytes_total,
			"bytes_uploaded": bytes_uploaded,
			"bytes_verified": bytes_verified,
		}
	)
	_set_campaign(campaign, values)
	return values


# --------------------------------------------------------------------------- entrypoint


def run_analysis(campaign: str) -> dict:
	"""The analyzer job: preflight indexes, then the three phases, then `Analyzed`.

	Resumable at phase granularity — a crash inside SCAN_DB resumes from the cursor, inside
	CLASSIFY from `classify_step`, and SCAN_FS restarts (it is a pure upsert).
	"""
	ensure_analysis_indexes()

	camp = _campaign(campaign)
	if camp.status not in ("Draft", "Analyzing", "Analyzed"):
		frappe.throw(f"Campaign {campaign} is {camp.status}; analysis runs from Draft.")

	_set_campaign(campaign, {"status": "Analyzing", "started_at": camp.started_at or now_datetime()})
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)
	audit.record("campaign_transition", campaign=campaign, to="Analyzing", phase="analysis")

	resume_from = camp.active_phase or ""
	if resume_from not in ("SCAN_FS", "CLASSIFY"):
		scan_db(campaign)
	if resume_from != "CLASSIFY":
		scan_fs(campaign)
	classify(campaign)

	_set_campaign(campaign, {"status": "Analyzed", "active_phase": None})
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- hard invariant 7 (docs/INVARIANTS.md:38): hook-side and worker mutations commit their own transaction (A2/A3)
	audit.record("campaign_transition", campaign=campaign, to="Analyzed")
	return refresh_counters(campaign)
