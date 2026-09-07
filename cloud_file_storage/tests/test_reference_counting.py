"""A6 — the pre-delete recount must be a locking read, and must count every reader.

Two separate properties, both load-bearing for "GC never deletes referenced bytes":

* **how** the count is read — `FOR UPDATE`, not a plain `SELECT COUNT(*)`. Locking the CSO
  row does not lock or serialise the File rows being counted, and under InnoDB REPEATABLE
  READ a plain read answers from the transaction's original read view, which for a sweep is
  established by the candidate query before any lock was taken;
* **which** rows are counted — the A1 chain lets a row with no `cloud_storage_object` of its
  own still resolve to an object through a content-hash sibling, and a link-only count
  values such a reader at zero.
"""

from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, now_datetime

from cloud_file_storage import gc
from cloud_file_storage.storage import objects
from cloud_file_storage.storage.hashing import digest_bytes
from cloud_file_storage.tests.utils import CSO_DOCTYPE, CloudStorageTestCase


class TestTheRecountIsLocking(CloudStorageTestCase):
	def test_it_issues_for_update_against_the_file_rows_it_counts(self):
		"""Fails against `frappe.db.count`, which has no `for_update` parameter at all."""
		doc = self.make_file(file_name="lock-shape.txt", content="lock shape")

		statements = []
		real_sql = frappe.db.sql

		def spy(query, *args, **kwargs):
			statements.append(str(query))
			return real_sql(query, *args, **kwargs)

		with patch.object(frappe.db, "sql", side_effect=spy):
			objects.live_reference_count(doc.cloud_storage_object)

		counting = [q for q in statements if "tabFile" in q and "COUNT" in q.upper()]
		self.assertTrue(counting, "the recount never touched tabFile")
		for query in counting:
			with self.subTest(query=query[:80]):
				self.assertIn("FOR UPDATE", query.upper())

	def test_migration_reference_count_locks_too(self):
		"""P5 inherits this count; it must not be handed a stale one either."""
		import inspect

		source = inspect.getsource(objects.migration_reference_count)
		self.assertIn(".for_update()", source)

	def test_only_the_cosmetic_counter_reads_without_a_lock(self):
		"""Every caller whose answer decides whether bytes die keeps the locking read."""
		doc = self.make_file(file_name="lock-callers.txt", content="lock callers")
		cso_name = doc.cloud_storage_object

		def locking_queries():
			statements = []
			real_sql = frappe.db.sql

			def spy(query, *args, **kwargs):
				statements.append(str(query))
				return real_sql(query, *args, **kwargs)

			return statements, spy

		statements, spy = locking_queries()
		with patch.object(frappe.db, "sql", side_effect=spy):
			objects.refresh_reference_count(cso_name)
		counting = [q for q in statements if "tabFile" in q and "COUNT" in q.upper()]
		self.assertTrue(counting)
		self.assertFalse(
			any("FOR UPDATE" in q.upper() for q in counting),
			"the cosmetic counter should not range-lock tabFile on every write",
		)

		statements, spy = locking_queries()
		with patch.object(frappe.db, "sql", side_effect=spy):
			objects.release_reference(cso_name, excluding_file=doc.name)
		counting = [q for q in statements if "tabFile" in q and "COUNT" in q.upper()]
		self.assertTrue(counting)
		self.assertTrue(
			all("FOR UPDATE" in q.upper() for q in counting),
			"the decision path must still read under a lock",
		)

	def test_a_reference_committed_after_the_read_view_opened_is_still_seen(self):
		"""The concrete loss path: adopted onto a pending_delete object mid-sweep."""
		doc = self.make_file(file_name="lock-race.txt", content="lock race")
		cso_name = doc.cloud_storage_object
		frappe.db.commit()

		# Drop the link so the object looks unreferenced, exactly as it would be to a sweep.
		frappe.db.set_value("File", doc.name, "cloud_storage_object", None, update_modified=False)
		frappe.db.commit()

		# Open this transaction's read view with a plain read — this is what `gc.py`'s
		# candidate query does before any lock is taken.
		frappe.get_all(CSO_DOCTYPE, filters={"name": cso_name}, pluck="name")

		# Another connection commits a reference onto the object.
		# `FrappeTestCase.secondary_connection` first exists in frappe **v15.50.0**
		# (`frappe/tests/utils.py`); the declared floor is **v15.16.0**. Skipping LOUDLY rather
		# than silently, and naming the version, because A22 forbids a silent skip — a control
		# that vanishes quietly reads as green. There is no substitute to write here: the test
		# needs a genuine second database connection to observe cross-transaction visibility,
		# and hand-rolling one would be testing our connection helper, not the locking.
		if not hasattr(self, "secondary_connection"):
			self.skipTest(
				"frappe.tests.utils.FrappeTestCase.secondary_connection does not exist before "
				"frappe v15.50.0, so cross-connection visibility cannot be observed on this "
				"ref. Covered on every ref at or above v15.50.0 by the same CI matrix."
			)

		with self.secondary_connection():
			frappe.db.set_value("File", doc.name, "cloud_storage_object", cso_name, update_modified=False)
			frappe.db.commit()

		self.assertEqual(
			objects.live_reference_count(cso_name),
			1,
			"the locking recount must see a reference committed after the read view opened",
		)


class TestTheRecountSeesEveryReader(CloudStorageTestCase):
	def test_a_row_resolving_through_a_sibling_counts_as_a_reference(self):
		"""R3 — the A1 chain resolves link-less rows; GC must not value them at zero."""
		doc = self.make_file(file_name="sibling-count.txt", content="counted through a sibling")
		cso_name = doc.cloud_storage_object

		unlinked = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "sibling-count-copy.txt",
				"file_url": "/private/files/sibling-count-copy.txt",
				"is_private": 1,
				"content_hash": doc.content_hash,
			}
		)
		unlinked.flags.ignore_duplicate_entry_error = True
		unlinked.insert(ignore_permissions=True)
		self.track(unlinked)
		frappe.db.set_value("File", unlinked.name, "cloud_storage_object", None, update_modified=False)

		# It resolves to the object through the content-hash sibling...
		self.assertEqual(frappe.get_doc("File", unlinked.name)._resolve_cso().name, cso_name)
		# ...so it has to count as a reference.
		self.assertEqual(objects.live_reference_count(cso_name), 2)

	def test_such_a_reader_prevents_the_object_being_scheduled(self):
		doc = self.make_file(file_name="sibling-keeps.txt", content="a sibling keeps me alive")
		cso_name = doc.cloud_storage_object

		unlinked = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "sibling-keeps-copy.txt",
				"file_url": "/private/files/sibling-keeps-copy.txt",
				"is_private": 1,
				"content_hash": doc.content_hash,
			}
		)
		unlinked.flags.ignore_duplicate_entry_error = True
		unlinked.insert(ignore_permissions=True)
		self.track(unlinked)
		frappe.db.set_value("File", unlinked.name, "cloud_storage_object", None, update_modified=False)

		scheduled = objects.release_reference(cso_name, excluding_file=doc.name)

		self.assertFalse(scheduled)
		self.assertNotEqual(frappe.db.get_value(CSO_DOCTYPE, cso_name, "status"), "pending_delete")

	def test_a_url_sharing_reader_with_no_hash_of_its_own_still_counts(self):
		"""NEW-1: the A1 chain has two arms and the recount has to mirror both.

		A link-less row sharing `file_url` with the linked row resolves through it, whatever
		its own `content_hash` says — and `patches/v0_2_0/backfill_s3_object_key` nulls
		exactly that column on fork-era rows.
		"""
		doc = self.make_file(file_name="url-arm.txt", content="kept alive by a url sharer")
		cso_name = doc.cloud_storage_object

		sharer = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "url-arm.txt",
				"file_url": doc.file_url,
				"is_private": 1,
				"content_hash": doc.content_hash,
			}
		)
		sharer.flags.ignore_duplicate_entry_error = True
		sharer.insert(ignore_permissions=True)
		self.track(sharer)
		# No link of its own and no hash: invisible to the content-hash arm.
		frappe.db.set_value(
			"File",
			sharer.name,
			{"cloud_storage_object": None, "content_hash": None},
			update_modified=False,
		)

		self.assertEqual(frappe.get_doc("File", sharer.name)._resolve_cso().name, cso_name)
		self.assertEqual(objects.unlinked_sibling_count(cso_name, exclude_file=doc.name), 1)

	def test_a_url_sharing_reader_survives_a_full_gc_sweep(self):
		"""The loss path end to end: delete the linked row, sweep, bytes must remain."""
		doc = self.make_file(file_name="url-arm-gc.txt", content="do not collect me")
		cso_name = doc.cloud_storage_object
		key = frappe.db.get_value(CSO_DOCTYPE, cso_name, "s3_key")

		sharer = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "url-arm-gc.txt",
				"file_url": doc.file_url,
				"is_private": 1,
				"content_hash": doc.content_hash,
			}
		)
		sharer.flags.ignore_duplicate_entry_error = True
		sharer.insert(ignore_permissions=True)
		self.track(sharer)
		frappe.db.set_value(
			"File",
			sharer.name,
			{"cloud_storage_object": None, "content_hash": None},
			update_modified=False,
		)

		frappe.delete_doc("File", doc.name, ignore_permissions=True, force=True)
		frappe.db.set_value(
			CSO_DOCTYPE,
			cso_name,
			"deletion_scheduled_at",
			add_to_date(now_datetime(), days=-1),
			update_modified=False,
		)

		gc.run_deferred_object_gc()

		self.assertNotIn(key, self.store.deleted, "GC deleted bytes a live row still resolves to")
		self.assertIn(key, self.store.objects)
		self.assertEqual(frappe.get_doc("File", sharer.name).get_content(), "do not collect me")

	def test_hashless_rows_are_never_counted_as_readers(self):
		"""A NULL filter compiles to IS NULL; counting those would match half the site."""
		digest = digest_bytes(b"hashless probe")
		cso = objects.ensure_cso(
			content_sha256=digest.sha256,
			content_hash_md5=digest.md5,
			file_size=digest.size,
			visibility="private",
		)
		frappe.db.set_value(CSO_DOCTYPE, cso.name, "content_hash_md5", None, update_modified=False)

		self.assertEqual(objects.unlinked_sibling_count(cso.name), 0)

	def test_visibility_is_part_of_the_match(self):
		"""A public twin is a different object and must not prop up a private one."""
		private = self.make_file(file_name="vis-private.txt", content="visibility matters", is_private=1)
		cso_name = private.cloud_storage_object

		# Same bytes, public: a genuinely different object, then unlinked so only its
		# content_hash could ever match.
		public_twin = self.make_file(file_name="vis-public.txt", content="visibility matters", is_private=0)
		self.assertEqual(public_twin.content_hash, private.content_hash)
		frappe.db.set_value("File", public_twin.name, "cloud_storage_object", None, update_modified=False)

		self.assertEqual(objects.unlinked_sibling_count(cso_name, exclude_file=private.name), 0)


class TestAdoptionCarriesTheHash(CloudStorageTestCase):
	"""Core's `_delete_file_on_disk` gate counts rows sharing `content_hash` (file.py:511-526)."""

	def test_a_relinked_sibling_gets_the_objects_hash(self):
		doc = self.make_file(file_name="adopt-hash.json", content='{"v": 1}')
		original_cso = doc.cloud_storage_object

		sibling = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "adopt-hash.json",
				"file_url": doc.file_url,
				"is_private": 1,
				"content_hash": doc.content_hash,
			}
		)
		sibling.flags.ignore_duplicate_entry_error = True
		sibling.insert(ignore_permissions=True)
		self.track(sibling)
		objects.adopt_references(original_cso, [sibling.name])

		# Overwrite: the row's object changes, so the sibling's hash must follow it.
		frappe.get_doc("File", doc.name).save_file(content='{"v": 2}', overwrite=True)

		new_cso = frappe.db.get_value("File", doc.name, "cloud_storage_object")
		sibling_row = frappe.db.get_value(
			"File", sibling.name, ["cloud_storage_object", "content_hash"], as_dict=True
		)
		self.assertEqual(sibling_row.cloud_storage_object, new_cso)
		self.assertEqual(
			sibling_row.content_hash,
			frappe.db.get_value(CSO_DOCTYPE, new_cso, "content_hash_md5"),
		)

	def test_core_s_refcount_gate_sees_the_sharer_after_a_relink(self):
		"""The point of carrying the hash: the deleting row must be routed thumbnail-only."""
		doc = self.make_file(file_name="gate-share.json", content='{"v": 1}')
		sibling = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "gate-share.json",
				"file_url": doc.file_url,
				"is_private": 1,
				"content_hash": doc.content_hash,
			}
		)
		sibling.flags.ignore_duplicate_entry_error = True
		sibling.insert(ignore_permissions=True)
		self.track(sibling)
		objects.adopt_references(doc.cloud_storage_object, [sibling.name])

		frappe.get_doc("File", doc.name).save_file(content='{"v": 2}', overwrite=True)

		reloaded = frappe.get_doc("File", doc.name)
		sharers = frappe.get_all(
			"File",
			filters={"content_hash": reloaded.content_hash, "name": ("!=", reloaded.name)},
			pluck="name",
		)
		self.assertIn(sibling.name, sharers)


class TestTheLockingSplitIsPinnedStructurally(CloudStorageTestCase):
	"""A6 must fail on the edit, not only on the behaviour.

	`for_update` being a parameter means the locking read is now a per-call-site decision.
	Every site that can lead to `engine.delete` takes the lock today, but that is a
	convention until something fails when it is broken — the same shape as the P1
	contract-completeness gate that reported green for an all-skipped run.

	The walk covers **all three** count functions, not just the outer one, and treats any
	`for_update` argument that is not a literal `True` as something the author must have
	declared below. A gate that recognises only the one spelling we happened to write is not
	enforcement; a direct call to `unlinked_sibling_count(..., for_update=False)`, or a
	computed `for_update=flag`, would otherwise walk straight past it.
	"""

	#: The three functions that answer "how many references does this object have".
	COUNT_FUNCTIONS = {
		"live_reference_count",
		"unlinked_sibling_count",
		"migration_reference_count",
	}

	#: Who may call each of them. A new caller anywhere has to be added here deliberately,
	#: with a decision about whether it locks.
	EXPECTED_CALLERS = {
		"live_reference_count": {
			"_collect_one",  # gc.py — the only path that reaches engine.delete
			"schedule_deletion",  # objects.py — puts an object into pending_delete
			"release_reference",  # objects.py — decides whether this was the last reference
			"refresh_reference_count",  # objects.py — the denormalized list-view counter
			"_find_unreferenced",  # migration/reconcile.py — a line in a drift REPORT (P5)
		},
		"unlinked_sibling_count": {"live_reference_count"},
		"migration_reference_count": {"live_reference_count"},
	}

	#: The call sites allowed to ask for a non-locking read, and why. Every entry is a place
	#: whose answer does NOT decide whether bytes die — that is the whole membership rule.
	#: (enclosing function, callee) -> justification.
	DECLARED_NON_LOCKING = {
		("refresh_reference_count", "live_reference_count"): (
			"the denormalized list-view counter, documented as never authoritative"
		),
		("_find_unreferenced", "live_reference_count"): (
			"migration/reconcile.py writes a line in a drift report; nothing acts on it "
			"automatically, and the runtime's GC takes its own locking recount before it "
			"deletes anything. Locking here would hold row locks on tabFile across a sweep "
			"of every object in the bucket, in one transaction, on a live site."
		),
	}

	#: The ONLY sites allowed to forward their own `for_update` parameter onward, and only
	#: as the bare name — an arbitrary expression is not propagation, it is a decision.
	DECLARED_PROPAGATION = {
		("live_reference_count", "unlinked_sibling_count"),
		("live_reference_count", "migration_reference_count"),
	}

	#: Kept as a set rather than a single name (P5 added the second entry). The property the
	#: gate enforces is unchanged — every non-locking site is declared, and every declared
	#: site still exists — and the two-way comparison below is what stops the table from
	#: quietly outliving the code it describes.
	NON_LOCKING_CALLERS = {"refresh_reference_count", "_find_unreferenced"}

	def _call_sites(self):
		"""Every call to any of the three count functions in the shipped package.

		Each site is classified by what it does with `for_update`:
		`absent` (defaults to locking), `literal_true`, `literal_false`,
		`propagates` (forwards the caller's own parameter by name), or `non_literal`.
		"""
		import ast
		from pathlib import Path

		import cloud_file_storage

		package = Path(cloud_file_storage.__file__).resolve().parent
		sites = []

		def callee_name(func_node):
			if isinstance(func_node, ast.Name):
				return func_node.id
			if isinstance(func_node, ast.Attribute):
				return func_node.attr
			return None

		def classify(node):
			for keyword in node.keywords:
				if keyword.arg != "for_update":
					continue
				value = keyword.value
				if isinstance(value, ast.Constant):
					return "literal_true" if value.value is True else "literal_false"
				if isinstance(value, ast.Name) and value.id == "for_update":
					return "propagates"
				return "non_literal"
			return "absent"

		def visit(node, enclosing, relative):
			if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
				enclosing = node.name
			if isinstance(node, ast.Call) and callee_name(node.func) in self.COUNT_FUNCTIONS:
				sites.append(
					{
						"caller": enclosing,
						"callee": callee_name(node.func),
						"for_update": classify(node),
						"where": f"{relative}:{node.lineno}",
					}
				)
			for child in ast.iter_child_nodes(node):
				visit(child, enclosing, relative)

		for path in package.rglob("*.py"):
			if "test" in path.name:
				continue
			relative = path.relative_to(package).as_posix()
			tree = ast.parse(path.read_text(encoding="utf-8"))
			for child in ast.iter_child_nodes(tree):
				visit(child, None, relative)

		return sites

	def test_the_set_of_callers_is_exactly_the_reviewed_one(self):
		"""Covers all three functions: a direct caller of any of them must be reviewed."""
		for callee, expected in self.EXPECTED_CALLERS.items():
			with self.subTest(callee=callee):
				callers = {site["caller"] for site in self._call_sites() if site["callee"] == callee}
				self.assertEqual(
					callers,
					expected,
					f"a new caller of {callee} must be reviewed for whether it locks",
				)

	def test_every_call_site_is_locking_or_declared(self):
		"""The catch-all: anything that is not plainly locking has to be declared above.

		This is what closes the bypasses — a literal False, a forwarded parameter somewhere
		it was not intended, or any computed expression all land here rather than being
		quietly unrecognised.
		"""
		offenders = []
		for site in self._call_sites():
			pair = (site["caller"], site["callee"])
			kind = site["for_update"]
			if kind in ("absent", "literal_true"):
				continue
			if kind == "literal_false" and pair in self.DECLARED_NON_LOCKING:
				continue
			if kind == "propagates" and pair in self.DECLARED_PROPAGATION:
				continue
			offenders.append(f"{site['where']} {site['caller']} -> {site['callee']} ({kind})")

		self.assertEqual(
			offenders,
			[],
			"undeclared non-locking or non-literal for_update; declare it with a justification "
			f"or make it lock: {offenders}",
		)

	def test_the_sites_that_skip_the_lock_are_exactly_the_declared_ones(self):
		"""Both directions: nothing undeclared skips, and nothing declared has disappeared."""
		skipping = {
			(site["caller"], site["callee"])
			for site in self._call_sites()
			if site["for_update"] == "literal_false"
		}
		self.assertEqual(
			skipping,
			set(self.DECLARED_NON_LOCKING),
			"the set of non-locking call sites no longer matches the reviewed table",
		)

	def test_and_none_of_those_sites_decides_whether_bytes_die(self):
		skipping = [site for site in self._call_sites() if site["for_update"] == "literal_false"]
		self.assertTrue(skipping, "no non-locking site at all — this check would be vacuous")
		for site in skipping:
			self.assertIn(site["caller"], self.NON_LOCKING_CALLERS)
			self.assertIn((site["caller"], site["callee"]), self.DECLARED_NON_LOCKING)
			self.assertNotIn(
				site["caller"],
				{"_collect_one", "schedule_deletion", "release_reference"},
				f"{site['caller']} decides whether bytes die and must not skip the lock",
			)

	def test_every_decision_path_takes_the_lock(self):
		"""`_collect_one`, `schedule_deletion` and `release_reference` all decide whether
		bytes die, so none of them may skip it or compute the flag."""
		decision_paths = self.EXPECTED_CALLERS["live_reference_count"] - self.NON_LOCKING_CALLERS
		offenders = [
			site["where"]
			for site in self._call_sites()
			if site["caller"] in decision_paths and site["for_update"] not in ("absent", "literal_true")
		]
		self.assertEqual(offenders, [], f"a decision path stopped locking: {offenders}")


class TestTheSweepItselfCountsUnderALock(CloudStorageTestCase):
	"""The dynamic half: the real path to `engine.delete`, not a proxy for it."""

	def test_every_count_issued_by_a_collecting_sweep_is_locking(self):
		doc = self.make_file(file_name="sweep-lock.txt", content="collect me under a lock")
		cso_name = doc.cloud_storage_object
		key = frappe.db.get_value(CSO_DOCTYPE, cso_name, "s3_key")
		frappe.delete_doc("File", doc.name, ignore_permissions=True, force=True)
		frappe.db.set_value(
			CSO_DOCTYPE,
			cso_name,
			"deletion_scheduled_at",
			add_to_date(now_datetime(), days=-1),
			update_modified=False,
		)

		statements = []
		real_sql = frappe.db.sql

		def spy(query, *args, **kwargs):
			statements.append(str(query))
			return real_sql(query, *args, **kwargs)

		with patch.object(frappe.db, "sql", side_effect=spy):
			gc.run_deferred_object_gc()

		# The sweep must actually have reached the delete, or the assertion below is empty.
		self.assertEqual(frappe.db.get_value(CSO_DOCTYPE, cso_name, "status"), "deleted")
		self.assertIn(key, self.store.deleted)

		counting = [q for q in statements if "tabFile" in q and "COUNT" in q.upper()]
		self.assertTrue(counting, "the sweep never counted tabFile references")
		for query in counting:
			with self.subTest(query=query[:80]):
				self.assertIn("FOR UPDATE", query.upper())
