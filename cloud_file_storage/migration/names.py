"""A33 — deterministic, sha1-derived names for the two high-volume migration tables.

`Cloud Migration Object.name = sha1(campaign‖url_hash)` and
`Cloud Migration File Ref.name = sha1(campaign‖file)`.

Two properties follow, and both are load-bearing rather than cosmetic:

* **Idempotent upserts by construction.** The analyzer re-runs whole pages after a crash.
  With a random `hash` autoname a re-run inserts a second row for the same identity and the
  ref counts silently double; with this scheme the second insert collides on the primary
  key and `INSERT ... ON DUPLICATE KEY UPDATE` / `INSERT IGNORE` does exactly the right
  thing without a separate lookup.
* **No redundant unique index.** migration-engine.md §2.3/§2.4 specify unique
  `(campaign, url_hash)` and `(campaign, file)` constraints. Those predate A33 and are
  exactly what the primary key now enforces, so they are deliberately not created: at
  1.2M objects plus 1.2M refs a redundant unique index is index bytes F4 has to account
  for, buying a guarantee the PK already gives. Every lookup that would have used them
  computes the name instead — which is also one index lookup cheaper.

The separator is a NUL byte so no campaign name and identity pair can be re-spelled into
another one ("CAMP-1" + "23" and "CAMP-12" + "3" are different keys here).
"""

import hashlib

SEPARATOR = b"\0"


def _sha1(*parts: str) -> str:
	digest = hashlib.sha1(usedforsecurity=False)
	digest.update(SEPARATOR.join((part or "").encode() for part in parts))
	return digest.hexdigest()


def identity_hash(identity: str) -> str:
	"""`url_hash` — the sha1 of an object's identity string (`kind::normalized_url`)."""
	return _sha1(identity)


def object_name(campaign: str, url_hash: str) -> str:
	"""A33 — `Cloud Migration Object.name`."""
	return _sha1(campaign, url_hash)


def file_ref_name(campaign: str, file: str) -> str:
	"""A33 — `Cloud Migration File Ref.name`."""
	return _sha1(campaign, file)
