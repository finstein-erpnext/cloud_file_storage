"""T-RECON — the three-way diff, and A29/F4's capacity preflight.

Reconcile's four drift classes each mean something different operationally, so each is
seeded and asserted separately. The one that matters most is **missing_remote**: a
`verified` object whose bytes are not in the bucket means something already believes those
bytes are safe, and it is the only class that raises an alert.

Reconcile has no delete path at all, which is asserted structurally rather than described:
physical deletes live only in deferred GC.
"""

import ast
import json
import os
from unittest.mock import patch

import frappe
from frappe.utils import now_datetime

from cloud_file_storage.migration import preflight, reconcile
from cloud_file_storage.storage import objects
from cloud_file_storage.tests.migration_utils import MigrationTestCase
from cloud_file_storage.tests.utils import TEST_BUCKET

RUN_DOCTYPE = "Cloud Reconcile Run"


class ReconcileTestCase(MigrationTestCase):
	def setUp(self):
		super().setUp()
		self._reports: list[str] = []
		self.addCleanup(self._drop_runs)

	def _drop_runs(self):
		frappe.set_user("Administrator")
		for path in self._reports:
			if os.path.exists(path):
				os.remove(path)
		frappe.db.delete(RUN_DOCTYPE)
		frappe.db.commit()

	def reconcile(self, **kwargs) -> dict:
		result = reconcile.run_reconcile(**kwargs)
		self._reports.append(result["report"])
		return result

	def drift(self, result: dict, drift_class: str) -> list[dict]:
		with open(result["report"]) as handle:
			return [
				json.loads(line)
				for line in handle
				if line.strip() and json.loads(line)["class"] == drift_class
			]


class TestReconcileClasses(ReconcileTestCase):
	def test_a_key_with_no_object_row_is_an_orphan(self):
		self.store.objects["cfs-autonomous.local/prv/aa/bb/orphaned-key"] = b"nobody's object"

		result = self.reconcile()

		orphans = self.drift(result, "orphan_remote")
		self.assertTrue(any(row["key"].endswith("orphaned-key") for row in orphans))
		self.assertGreaterEqual(result["orphan_remote"], 1)

	def test_a_verified_object_whose_bytes_are_gone_is_missing_remote(self):
		doc = self.local_file(file_name="recon-missing.txt", content=b"about to vanish")
		campaign = self.campaign()
		self.migrate(campaign)
		cso = objects.get_cso(self.link_of(doc))
		self.store.objects.pop(cso.s3_key)

		result = self.reconcile()

		missing = self.drift(result, "missing_remote")
		self.assertTrue(any(row["cloud_storage_object"] == cso.name for row in missing))
		self.assertGreaterEqual(result["missing_remote"], 1)

	def test_a_missing_remote_object_raises_an_error_log(self):
		doc = self.local_file(file_name="recon-alert.txt", content=b"alert me")
		campaign = self.campaign()
		self.migrate(campaign)
		cso = objects.get_cso(self.link_of(doc))
		self.store.objects.pop(cso.s3_key)

		before = frappe.db.count("Error Log")
		self.reconcile()
		self.assertGreater(frappe.db.count("Error Log"), before, "no alert was raised")

	def test_a_size_disagreement_is_reported(self):
		doc = self.local_file(file_name="recon-size.txt", content=b"the recorded size")
		campaign = self.campaign()
		self.migrate(campaign)
		cso = objects.get_cso(self.link_of(doc))
		self.store.objects[cso.s3_key] = b"a payload of an entirely different length"

		result = self.reconcile()

		mismatches = self.drift(result, "size_mismatch")
		self.assertTrue(any(row["cloud_storage_object"] == cso.name for row in mismatches))

	def test_an_object_nothing_references_is_reported_but_not_deleted(self):
		cso = objects.ensure_cso(
			content_sha256="d" * 64, content_hash_md5="e" * 32, file_size=3, visibility="private"
		)
		objects.set_cso_status(cso.name, "uploaded")
		self.store.objects[cso.s3_key] = b"abc"

		result = self.reconcile()

		unreferenced = self.drift(result, "unreferenced_cso")
		self.assertTrue(any(row["cloud_storage_object"] == cso.name for row in unreferenced))
		self.assertIn(cso.s3_key, self.store.objects, "reconcile deleted an object")
		self.assertEqual(self.store.deleted, [], "reconcile issued a delete")

	def test_a_healthy_bucket_reports_no_drift_of_any_class(self):
		"""The counterpart: without it every assertion above could be reporting noise."""
		doc = self.local_file(file_name="recon-clean.txt", content=b"perfectly consistent")
		campaign = self.campaign()
		self.migrate(campaign)

		result = self.reconcile()

		cso = objects.get_cso(self.link_of(doc))
		for drift_class in ("missing_remote", "size_mismatch"):
			offending = [
				row for row in self.drift(result, drift_class) if row.get("cloud_storage_object") == cso.name
			]
			self.assertEqual(offending, [], f"a healthy object was reported as {drift_class}")


class TestReconcileMechanics(ReconcileTestCase):
	def test_the_run_is_recorded_with_its_counters(self):
		result = self.reconcile()
		row = frappe.db.get_value(
			RUN_DOCTYPE, result["run"], ["status", "bucket", "remote_objects", "report_file"], as_dict=True
		)
		self.assertEqual(row.status, "Completed")
		self.assertEqual(row.bucket, TEST_BUCKET)
		self.assertTrue(row.report_file)

	def test_paging_carries_a_continuation_token_and_clears_it_at_the_end(self):
		for index in range(5):
			self.store.objects[f"cfs-autonomous.local/prv/aa/bb/page-{index}"] = b"x"

		original = reconcile.PAGE_SIZE
		reconcile.PAGE_SIZE = 2
		try:
			result = self.reconcile()
		finally:
			reconcile.PAGE_SIZE = original

		self.assertGreaterEqual(result["remote_objects"], 5)
		self.assertIsNone(frappe.db.get_value(RUN_DOCTYPE, result["run"], "continuation_token"))

	def test_reconcile_contains_no_deletion_call(self):
		"""Design §10 — reported, never deleted. Asserted on the source, not on a run."""
		from cloud_file_storage.tests.test_migration_safety_lint import APP_ROOT, find_deletion_calls

		path = os.path.join(APP_ROOT, "migration/reconcile.py")
		with open(path) as handle:
			tree = ast.parse(handle.read(), filename=path)
		self.assertEqual(find_deletion_calls(tree), [])

	def test_a_scheduled_run_without_a_bucket_does_nothing(self):
		from cloud_file_storage.tests.utils import set_mode

		set_mode("LOCAL_ONLY", bucket="")
		self.assertEqual(reconcile.scheduled_reconcile(), {"skipped": "no bucket configured"})


class TestCapacityPreflight(MigrationTestCase):
	"""A29 / gate F4 — measured, projected, and refused below the thresholds."""

	def test_the_measurement_reads_real_numbers_out_of_the_database(self):
		self.local_file(file_name="preflight-measure.txt", content=b"measure me")
		campaign = self.campaign()
		self.migrate(campaign)

		measured = preflight.measure(campaign)

		objects_table = measured["tables"]["Cloud Migration Object"]
		self.assertGreater(objects_table["rows"], 0)
		self.assertGreater(objects_table["total_bytes"], 0, "information_schema reported no bytes")
		self.assertGreater(objects_table["bytes_per_row"], 0)
		self.assertGreaterEqual(objects_table["index_bytes"], 0)
		self.assertGreater(measured["disk"]["disk_free_bytes"], 0)
		# `evaluate` reads `db_disk_free_bytes`, not `disk_free_bytes`. Asserting only the
		# latter left the key the check depends on unpinned: renaming or dropping it gives
		# `cint(None) == 0`, every production-scale start is refused, and the suite stays
		# green. Measured, not supposed -- with this line absent the rename is caught by
		# nothing; with it, this test fails.
		self.assertGreater(measured["disk"]["db_disk_free_bytes"], 0)
		self.assertIn("innodb_file_per_table", measured["disk"])
		self.assertGreater(measured["process"]["peak_rss_bytes"], 0)
		self.assertEqual(measured["campaign"]["name"], campaign)

	def test_the_free_space_comparison_is_decided_at_its_boundary(self):
		"""The one place the disk arithmetic is exercised with numbers that could go either way.

		The healthy-path test injects 1 EiB and the refusal test forces a 1e12 factor, so
		neither observes `free >= required` where the answer is in doubt -- and nothing else
		asserted the shipped `DISK_SAFETY_FACTOR` at all. Pure unit: no database, no host, so
		the verdict cannot come from the machine this runs on.
		"""
		projected = {"total_bytes": 1000}
		measured = {"disk": {"db_disk_free_bytes": 2000}, "backup": {}, "redis": {}}

		def disk_check(free):
			measured["disk"]["db_disk_free_bytes"] = free
			checks = preflight.evaluate(measured, projected)
			return next(c for c in checks if c["check"] == "database_free_space")

		exact = disk_check(2000)
		self.assertTrue(exact["passed"], "free == required must pass; the check is `>=`")
		self.assertEqual(
			exact["required_bytes"],
			2000,
			"required is projected x DISK_SAFETY_FACTOR; 2000 pins the shipped factor at 2.0",
		)
		self.assertFalse(disk_check(1999)["passed"], "one byte short must fail")
		self.assertTrue(disk_check(2001)["passed"])

	def test_every_table_a_campaign_grows_is_measured(self):
		"""F4 is only as honest as `MEASURED_TABLES`.

		`Cloud Storage Object` was missing, and migration creates one per unique content —
		a fifth of the total footprint on the rehearsal corpus. A projection that leaves out
		a table understates the free space the gate demands, and understating is the
		direction in which a capacity gate passes a start it should refuse.
		"""
		self.assertIn("Cloud Storage Object", preflight.MEASURED_TABLES)
		for doctype in ("Cloud Migration Object", "Cloud Migration File Ref", "Cloud Storage Audit Log"):
			self.assertIn(doctype, preflight.MEASURED_TABLES)

		measured = preflight.measure()
		for doctype in preflight.MEASURED_TABLES:
			self.assertIn(doctype, measured["tables"], f"{doctype} was not measured")

	def test_the_projection_uses_each_tables_own_measured_ratio(self):
		"""Assuming one row per object understates whichever table carries more than one.

		Every measured value here is deliberately **above** the rehearsal calibration, so the
		floor added for P5-H1 cannot be what the assertions are reading — this test is about
		the ratios, and the floor has its own tests.
		"""
		measured = {
			"tables": {
				"Cloud Migration Object": {
					"rows": 100,
					"bytes_per_row": 9000.0,
					"index_bytes_per_row": 4000.0,
				},
				"Cloud Migration File Ref": {
					"rows": 200,
					"bytes_per_row": 9000.0,
					"index_bytes_per_row": 4000.0,
				},
				"Cloud Migration Conflict": {
					"rows": 10,
					"bytes_per_row": 9000.0,
					"index_bytes_per_row": 4000.0,
				},
				"Cloud Storage Audit Log": {
					"rows": 300,
					"bytes_per_row": 9000.0,
					"index_bytes_per_row": 4000.0,
				},
				"Cloud Storage Object": {"rows": 150, "bytes_per_row": 9000.0, "index_bytes_per_row": 4000.0},
			}
		}
		projected = preflight.project(measured, target_rows=1000)

		self.assertEqual(projected["Cloud Storage Audit Log"]["rows"], 3000, "the audit log was not 3:1")
		self.assertEqual(projected["Cloud Storage Object"]["rows"], 1500)
		self.assertEqual(projected["Cloud Migration File Ref"]["rows"], 2000)
		self.assertEqual(projected["audit_rows_per_object"], 3.0)
		self.assertEqual(projected["cloud_objects_per_object"], 1.5)
		# 1000 + 2000 + 100 + 3000 + 1500 rows at 9000 bytes each.
		self.assertEqual(projected["total_bytes"], 7600 * 9000)

	def test_a_table_left_out_of_the_projection_would_understate_it(self):
		"""The counterpart, so the inclusion test above is not merely a spelling check."""
		self.local_file(file_name="preflight-omit.txt", content=b"give the tables some rows")
		campaign = self.campaign()
		self.migrate(campaign)

		measured = preflight.measure(campaign)
		full = preflight.project(measured, target_rows=1_200_000)

		without_cso = {"tables": {k: v for k, v in measured["tables"].items() if k != "Cloud Storage Object"}}
		partial = preflight.project(without_cso, target_rows=1_200_000)
		self.assertLess(
			partial["total_bytes"],
			full["total_bytes"],
			"dropping a measured table did not change the projection — it is not being summed",
		)

	def test_a_campaign_measured_before_upload_still_projects_the_full_cost(self):
		"""P5-H1 — the gate runs at the one moment it cannot measure itself.

		`assert_startable` fires at campaign **start**: `Cloud Storage Object` has ~0 rows and
		the audit log has no `file_link` or `local_quarantine` rows, so a projection derived
		only from the live database gives those two tables a ratio of ~0 and drops roughly
		3.8KB of the measured 8.8KB per object. The gate would demand about half the space a
		campaign needs — a capacity gate passing a start it should refuse.
		"""
		empty = {
			"tables": {
				doctype: {"rows": 0, "bytes_per_row": 0.0, "index_bytes_per_row": 0.0}
				for doctype in preflight.MEASURED_TABLES
			}
		}
		projected = preflight.project(empty, target_rows=1_200_000)

		# The rehearsal measured 10.61 GB; a start-time projection must not fall below it.
		self.assertGreater(projected["total_bytes"], 10_000_000_000)
		for doctype in ("Cloud Storage Object", "Cloud Storage Audit Log"):
			self.assertGreater(
				projected[doctype]["total_bytes"],
				0,
				f"{doctype} contributed nothing to a start-time projection",
			)
		self.assertAlmostEqual(projected["Cloud Storage Audit Log"]["rows_per_object"], 2.0, delta=0.01)

	def test_a_site_whose_own_costs_exceed_the_calibration_wins(self):
		"""`max`, not "use the calibration": a heavier site must not be talked down to it."""
		heavy = {
			"tables": {
				doctype: {"rows": 100, "bytes_per_row": 99_999.0, "index_bytes_per_row": 9_999.0}
				for doctype in preflight.MEASURED_TABLES
			}
		}
		heavy["tables"]["Cloud Migration Object"]["rows"] = 100
		projected = preflight.project(heavy, target_rows=1000)
		self.assertEqual(projected["Cloud Migration Object"]["total_bytes"], int(99_999.0 * 1000))

	def test_the_calibration_covers_every_measured_table(self):
		"""A table added to MEASURED_TABLES without a calibration would project ~0 at start."""
		for doctype in preflight.MEASURED_TABLES:
			self.assertIn(doctype, preflight.CALIBRATION, f"{doctype} has no rehearsal calibration")
			entry = preflight.CALIBRATION[doctype]
			self.assertGreater(entry["bytes_per_row"], 0)

	def test_the_projection_scales_the_measured_per_row_cost(self):
		self.local_file(file_name="preflight-project.txt", content=b"project me")
		campaign = self.campaign()
		self.migrate(campaign)

		measured = preflight.measure(campaign)
		small = preflight.project(measured, target_rows=1000)
		large = preflight.project(measured, target_rows=1_000_000)

		self.assertEqual(large["target_rows"], 1_000_000)
		self.assertGreater(large["total_bytes"], small["total_bytes"])
		self.assertAlmostEqual(large["total_bytes"] / max(1, small["total_bytes"]), 1000, delta=50)
		self.assertGreater(large["refs_per_object"], 0)

	def test_a_failed_threshold_is_reported_with_its_numbers(self):
		campaign = self.campaign()
		self.analyze(campaign)

		# An impossible safety factor: whatever the site's free space, the projection cannot
		# fit. The point is that the check reports the numbers rather than only a verdict.
		report = preflight.run_preflight(campaign, disk_safety_factor=1e12)

		disk_check = next(check for check in report["checks"] if check["check"] == "database_free_space")
		self.assertFalse(disk_check["passed"])
		self.assertGreater(disk_check["required_bytes"], disk_check["free_bytes"])
		self.assertEqual(
			frappe.db.get_value("Cloud Migration Campaign", campaign, "preflight_status"), "Failed"
		)

	def test_a_backup_taken_seconds_ago_passes(self):
		"""`age_hours` is 0.0 for a fresh backup, and 0.0 is falsy.

		Written after the rehearsal refused a production-scale campaign one minute after a
		successful `bench backup`: an `or` default turned the freshest possible backup into
		the oldest possible one.
		"""
		measured = {"backup": {"present": True, "age_hours": 0.0}, "disk": {}, "redis": {}}
		projected = {"total_bytes": 0}
		checks = preflight.evaluate(measured, projected, max_backup_age_hours=24)
		backup_check = next(check for check in checks if check["check"] == "recent_database_backup")
		self.assertTrue(backup_check["passed"], "a backup taken seconds ago was called stale")

	def test_a_missing_backup_still_fails(self):
		measured = {"backup": {"present": False}, "disk": {}, "redis": {}}
		checks = preflight.evaluate(measured, {"total_bytes": 0})
		backup_check = next(check for check in checks if check["check"] == "recent_database_backup")
		self.assertFalse(backup_check["passed"])

	def test_a_stale_backup_fails_the_backup_check(self):
		campaign = self.campaign()
		self.analyze(campaign)
		report = preflight.run_preflight(campaign, max_backup_age_hours=0)
		backup_check = next(check for check in report["checks"] if check["check"] == "recent_database_backup")
		self.assertFalse(backup_check["passed"])

	def test_the_report_is_recorded_on_the_campaign(self):
		campaign = self.campaign()
		self.analyze(campaign)
		preflight.run_preflight(campaign)

		row = frappe.db.get_value(
			"Cloud Migration Campaign",
			campaign,
			["preflight_status", "preflight_at", "preflight_report"],
			as_dict=True,
		)
		self.assertIn(row.preflight_status, ("Passed", "Failed"))
		self.assertIsNotNone(row.preflight_at)
		stored = json.loads(row.preflight_report)
		self.assertIn("measured", stored)
		self.assertIn("projected", stored)
		self.assertIn("checks", stored)

	def test_a_small_campaign_is_not_production_scale(self):
		campaign = self.campaign()
		self.analyze(campaign)
		self.assertFalse(preflight.is_production_scale(campaign))
		# …and is therefore not refused, only measured.
		preflight.assert_startable(campaign)

	def test_a_production_scale_campaign_is_refused_below_the_thresholds(self):
		"""A29 — the refusal itself, driven through the real code path.

		The failing threshold is **injected**, exactly as the counterpart below injects the
		passing ones. It used to be left to the machine: the assertion relied on this site
		naturally failing `recent_database_backup` (no dump on disk) or
		`migration_queue_reachable` (no `cloud_migration` queue). Both are things a correctly
		prepared bench has — the deployment runbook asks for that queue, and any run of the
		backup suite leaves a real dump in `private/backups` for the next 24 hours — and on
		such a bench all four checks passed and this refusal could not fire at all. A test
		whose ability to go red depends on the machine being misconfigured is not testing the
		refusal. Found on the P8 release bench, which was the first to have both.
		"""
		campaign = self.campaign()
		self.analyze(campaign)

		frappe.db.set_value(
			"Cloud Migration Campaign",
			campaign,
			{"preflight_status": "Failed", "preflight_at": None},
			update_modified=False,
		)
		frappe.db.commit()

		with patch.object(preflight, "PRODUCTION_SCALE_OBJECTS", 1):
			self.assertTrue(preflight.is_production_scale(campaign))

			# One threshold, deliberately unmet. Everything else measures for real.
			with patch.object(preflight, "latest_backup", return_value={"present": False}):
				with self.assertRaises(frappe.ValidationError) as caught:
					preflight.assert_startable(campaign, skip_preflight=True)

		self.assertIn("recent_database_backup", str(caught.exception))

	def test_a_recorded_pass_does_not_authorise_a_later_start(self):
		"""A29 — the thresholds are measured at the start, never read back from the campaign.

		The hole this closes: `assert_startable` trusted a `preflight_status == "Passed"`
		recorded within the last day, so a production-scale campaign could start **without
		measuring anything** — onto free space the recording campaign had itself consumed,
		with the backup requirement ageing out on exactly the same 24-hour clock the cache
		used. A cached verdict is not a measurement, and A29 asks for a measurement.

		Isolation, because this test moves machinery the tests after it depend on: the scale
		threshold is patched rather than assigned, so it is restored even if an assertion
		raises, and the freshness failure is injected at `latest_backup` rather than by
		renaming the site's `private/backups` directory — a process that died mid-test would
		otherwise leave the site without its backups folder.

		Finding credited to the `P5-impl-2` session (DECISIONS 2026-08-15).
		"""
		campaign = self.campaign()
		self.analyze(campaign)

		# The exact state the old code short-circuited on: a fresh, genuine pass.
		frappe.db.set_value(
			"Cloud Migration Campaign",
			campaign,
			{"preflight_status": "Passed", "preflight_at": now_datetime()},
			update_modified=False,
		)
		frappe.db.commit()

		with patch.object(preflight, "PRODUCTION_SCALE_OBJECTS", 1):
			self.assertTrue(preflight.is_production_scale(campaign))

			# …and the machine no longer meets a threshold it met when that pass was recorded.
			with patch.object(preflight, "latest_backup", return_value={"present": False}):
				with patch.object(preflight, "run_preflight", wraps=preflight.run_preflight) as measured:
					with self.assertRaises(frappe.ValidationError):
						preflight.assert_startable(campaign, skip_preflight=True)
					measured.assert_called_once()

		self.assertEqual(
			frappe.db.get_value("Cloud Migration Campaign", campaign, "preflight_status"),
			"Failed",
			"the start was authorised off the cached verdict and left a stale one behind",
		)

	def test_a_healthy_production_scale_start_is_allowed_and_re_measured(self):
		"""The counterpart: re-measuring must not mean refusing everything.

		Without this, the test above would pass just as well against an `assert_startable`
		that always throws, which is not what A29 asks for.
		"""
		campaign = self.campaign()
		self.analyze(campaign)
		frappe.db.set_value(
			"Cloud Migration Campaign",
			campaign,
			{"preflight_status": "Not Run", "preflight_at": None},
			update_modified=False,
		)
		frappe.db.commit()

		# Two measurements are injected because this unit site legitimately fails them: it
		# has no recent dump and no `cloud_migration` queue (that queue is a deployment
		# prerequisite, exercised for real on the rehearsal site). Everything else — the
		# table sizes, the projection, the free-space arithmetic and the whole
		# `assert_startable` path — runs for real.
		fresh = {"present": True, "age_hours": 0.0, "path": "/synthetic/backup-database.sql.gz"}
		reachable = {"queue_depth": 0, "redis_used_memory_bytes": 1, "redis_peak_memory_bytes": 1}
		# A third measurement is injected for exactly the reason the two above are: this test
		# asserts the *healthy* path, and "healthy" includes having room for the projection.
		# Leaving free space to the host made the host decide the verdict -- it passed on a
		# workstation and failed on every CI runner, where a production-scale projection of
		# ~81 GiB against a 2.0 safety factor needs more disk than a runner has. Free space is
		# now ample by construction. The arithmetic still *runs*, but at 1 EiB it cannot
		# discriminate -- it is `test_the_free_space_comparison_is_decided_at_its_boundary`
		# that exercises `free >= required` with numbers which could land either way, and
		# `test_the_measurement_reads_real_numbers_out_of_the_database` that pins the key this
		# injection supplies by name. `innodb_file_per_table` is kept real by wrapping the
		# live call.
		# The refusal side is covered independently by
		# `test_a_failed_threshold_is_reported_with_its_numbers`, which forces an impossible
		# safety factor, so nothing here can make the check unable to fail.
		ample = dict(preflight.disk_metrics(), db_disk_free_bytes=1 << 60, disk_free_bytes=1 << 60)
		with patch.object(preflight, "PRODUCTION_SCALE_OBJECTS", 1):
			with patch.object(preflight, "latest_backup", return_value=fresh):
				with patch.object(preflight, "redis_metrics", return_value=reachable):
					with patch.object(preflight, "disk_metrics", return_value=ample):
						preflight.assert_startable(campaign, skip_preflight=True)

		row = frappe.db.get_value(
			"Cloud Migration Campaign", campaign, ["preflight_status", "preflight_at"], as_dict=True
		)
		self.assertEqual(row.preflight_status, "Passed")
		self.assertIsNotNone(row.preflight_at, "the start did not record a measurement")

	def test_skip_preflight_skips_only_a_small_campaign(self):
		"""The flag exists for the rehearsal and for a campaign an operator has reasoned about.

		Asserted by what it *does* rather than by where it appears in the source: on a small
		campaign it suppresses the measurement, and on a production-scale one it cannot —
		the size check runs first, which is what the refusal test above then observes.
		"""
		campaign = self.campaign()
		self.analyze(campaign)

		preflight.assert_startable(campaign, skip_preflight=True)
		self.assertIsNone(
			frappe.db.get_value("Cloud Migration Campaign", campaign, "preflight_at"),
			"skip_preflight did not skip on a small campaign",
		)

		preflight.assert_startable(campaign, skip_preflight=False)
		self.assertIsNotNone(
			frappe.db.get_value("Cloud Migration Campaign", campaign, "preflight_at"),
			"a small campaign was not measured when the flag was off",
		)
