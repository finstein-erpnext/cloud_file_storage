"""F2 — the mission matrix, mapped scenario-by-scenario to the test that executes it.

`docs/ACCEPTANCE_GATES.md` §F2 names 43 CFS-10 scenarios and requires each to have "a named
green T-test at its layer". Until this file existed F2 was the only gate with **no mechanical
check** — F1, F5, backup and MinIO each ship a gate script, F2 shipped prose.

**Why a manifest of test ids rather than a docstring-tag grep.** Every earlier attempt to count
this gate's coverage by grepping for `T-` tags was wrong, in both directions: it matched
`T-ID` inside `AKIA-SECRET-ID`, and it counted `T-AMEND` because a *module docstring* mentioned
"amend/copy" while no test drove `amended_from`. A tag is a claim; a test id checked against a
junit is a measurement. Mapping exact ids also makes it impossible for one scenario to be
satisfied by another's name, and impossible for a tagged-but-unexecuted test to count.

Each value is `module.Class` or `module.Class.method` under `cloud_file_storage.tests`. The gate
(`.github/helper/check_mission_matrix.sh`) fails if any scenario is absent, unmapped, skipped,
failed, errored, or mapped to something that did not execute.
"""

#: scenario id -> the test that drives it. One scenario, one entry, no sharing.
MISSION_MATRIX = {
	# --- serving -------------------------------------------------------------------------
	"T-PUB": "test_serving_public.TestPublicRendererResolution",
	"T-PRIV": "test_serving_private.TestPrivateServingRedirect",
	"T-PERM": "test_serving_private.TestPrivateServingPermissions",
	"T-PRIVFLIP": "test_serving_public.TestPrivacyFlipServing",
	"T-HTML": "test_disposition.TestPolicyB",
	"T-MODEGATE": "test_serving_private.TestPrivateServingModes",
	"T-ALIAS": "test_serving_private.TestPrivateServingAliases",
	"T-OUTAGE": "test_serving_public.TestPublicRendererOutage",
	# --- identity, dedup, deletion -------------------------------------------------------
	"T-DUP1": "test_core_hooks.TestCanonicalUrlAndHash.test_a_name_collision_with_different_bytes_gets_a_distinct_url",
	"T-DUP2": "test_core_hooks.TestCanonicalUrlAndHash.test_identical_bytes_keep_one_object_with_two_references",
	"T-DUP3": "test_minio_integration.TestMinioIntegration.test_two_identical_uploads_share_one_object",
	"T-DEL1": "test_reference_counting.TestTheRecountSeesEveryReader.test_a_url_sharing_reader_survives_a_full_gc_sweep",
	"T-DEL2": "test_gc.TestDeferredCollection.test_after_the_grace_window_the_object_is_deleted_and_tombstoned",
	# --- frappe consumer flows -----------------------------------------------------------
	"T-AMEND": "test_frappe_consumer_flows.TestAmendCopiesAttachmentsWithoutANewObject",
	"T-ATTACH": "test_cloud_file.TestUrlOnlyFileCreation.test_attach_files_to_document_style_row_adopts_the_object",
	"T-ATTIMG": "test_thumbnails.TestMakeThumbnail.test_a_cloud_only_image_still_produces_a_thumbnail",
	"T-EMAIL-OUT": "test_frappe_consumer_flows.TestEmailAttachmentBytes",
	"T-EMAIL-IN": "test_frappe_consumer_flows.TestInboundEmailAttachmentIsStored",
	"T-DATAIMP": "test_modes.TestIgnoredDoctypeScope.test_the_operational_trio_is_ignored",
	"T-PREPREP": "test_frappe_consumer_flows.TestPreparedReportGzRoundTrip",
	"T-ZIP": "test_frappe_consumer_flows.TestUnzipOnACloudBackedArchive",
	"T-IMG": "test_frappe_consumer_flows.TestOptimizeFileOverwritesInPlace",
	"T-UNI": "test_frappe_consumer_flows.TestAUnicodeFilenameSurvivesTheRoundTrip",
	"T-LARGE": "test_storage_engine.TestUploads.test_a_put_above_the_multipart_threshold_uses_the_managed_transfer",
	"T-API": "test_frappe_consumer_flows.TestTheUploadEndpointStoresInTheCloud",
	# --- ecosystem -----------------------------------------------------------------------
	"T-ITEMIMG": "test_ecosystem.TestItemImage",
	"T-RIV": "test_ecosystem.TestRepostItemValuation",
	"T-IC-LIVE": "test_ecosystem.TestIndiaComplianceLive.test_gst_return_log_create_read_update_and_download",
	"T-IC-RAW": "contract.test_file_compat_contract.TestC1C2GetContentSignature",
	"T-IC-FLOW": "contract.test_file_compat_contract.TestC18BusinessFieldPointers",
	# --- migration resilience ------------------------------------------------------------
	"T-RESTART": "test_migration_resilience.TestQueueLoss",
	"T-INTR": "test_migration_resilience.TestInterruptedWorker",
	"T-RETRY": "test_migration_resilience.TestRetryAndBackoff",
	"T-CHKSUM": "test_migration_resilience.TestChecksum",
	"T-PAUSE": "test_migration_resilience.TestPauseAndStop",
	"T-GATE": "test_migration_cleanup.TestCleanupGates",
	"T-RECON": "test_migration_reconcile.TestReconcileClasses",
	"T-RELINK": "test_migration_conflicts.TestRelink",
	"T-ADOPT": "test_migration_adoption.TestLegacyForkAdoption",
	"T-CLI": "test_migration_cli.TestEveryCommandDrivesTheApi",
	# --- legacy, backup, compat ----------------------------------------------------------
	"T-LEGACY": "test_compat.TestLegacyGenerateFile",
	"T-BACKUP": "test_backup.TestBackupVerification",
	"T-RENAME": "test_compat.TestDeprecatedHookDetector",
}
