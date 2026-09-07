// Copyright (c) 2026, Finstein and contributors
// For license information, please see license.txt
//
// The twelve mission buttons. Every one of them posts to `cloud_file_storage.api.admin`,
// which is a role gate and a delegation into `migration/api` — the same function
// `bench cfs-migrate-*` calls (A9). Nothing here decides whether an action is allowed.
//
// That includes the type-to-confirm on Delete Verified Local Copies. The dialog collects the
// phrase and posts it **unvalidated**: the comparison lives in `migration.api.start_cleanup`
// because `/api/method/...start_cleanup` is reachable without ever loading this form, and a
// confirmation the caller can skip is not a confirmation. A client-side check here would
// only make it look guarded.

frappe.ui.form.on('Cloud Storage Settings', {
	refresh(frm) {
		frm.trigger('render_storage_health');
		frm.trigger('add_mission_buttons');
	},

	// ------------------------------------------------------------------ storage buttons

	test_connection(frm) {
		frappe.call({
			method: 'cloud_file_storage.api.admin.test_connection',
			freeze: true,
			freeze_message: __('Probing the object store…'),
			callback: (response) => render_connection(frm, response.message),
		});
	},

	refresh_storage_health(frm) {
		frm.trigger('render_storage_health');
	},

	reconcile(frm) {
		frappe.confirm(
			__(
				'Reconcile walks the whole bucket and compares it against the object rows. It changes nothing. Continue?'
			),
			() =>
				frappe.call({
					method: 'cloud_file_storage.api.admin.reconcile',
					freeze: true,
					freeze_message: __('Reconciling…'),
					callback: (response) => {
						const counters = response.message || {};
						frappe.msgprint({
							title: __('Reconcile Complete'),
							indicator: 'blue',
							message: `
								<ul>
									<li>${__('Remote objects')}: ${counters.remote_objects || 0}</li>
									<li>${__('Orphan remote keys')}: ${counters.orphan_remote || 0}</li>
									<li>${__('Missing remote')}: ${counters.missing_remote || 0}</li>
									<li>${__('Size mismatches')}: ${counters.size_mismatch || 0}</li>
									<li>${__('Unreferenced objects')}: ${counters.unreferenced_cso || 0}</li>
								</ul>`,
						});
					},
				})
		);
	},

	render_storage_health(frm) {
		frappe.call({
			method: 'cloud_file_storage.api.admin.get_storage_health',
			callback: (response) => {
				const health = response.message || {};
				frm.__cfs_health = health;
				render_health(frm, health);
				render_cache(frm, health);
				render_migration(frm, health);
				frm.trigger('add_mission_buttons');
			},
		});
	},

	// ----------------------------------------------------------------- mission buttons

	add_mission_buttons(frm) {
		frm.clear_custom_buttons();

		const campaign = active_campaign(frm);
		const migration = __('Migration');

		frm.add_custom_button(__('Analyze Storage'), () => analyze(frm), migration);
		frm.add_custom_button(__('Build Migration Plan'), () => plan(frm, campaign), migration);
		frm.add_custom_button(__('Start'), () => start(frm, campaign), migration);
		frm.add_custom_button(__('Pause'), () => simple(frm, 'pause_campaign', campaign), migration);
		frm.add_custom_button(__('Resume'), () => simple(frm, 'resume_campaign', campaign), migration);
		frm.add_custom_button(
			__('Stop After Current Batch'),
			() => stop_after_batch(frm, campaign),
			migration
		);
		frm.add_custom_button(__('Retry Failed'), () => retry_failed(frm, campaign), migration);
		frm.add_custom_button(__('Verify'), () => simple(frm, 'start_verify', campaign), migration);
		frm.add_custom_button(
			__('Export Migration Report'),
			() => export_report(frm, campaign),
			migration
		);
		frm.add_custom_button(
			__('Delete Verified Local Copies'),
			() => delete_verified_local_copies(frm, campaign),
			migration
		);
	},
});

// ------------------------------------------------------------------------------ helpers

function active_campaign(frm) {
	const migration = (frm.__cfs_health || {}).migration || {};
	const active = migration.active_campaign || migration.latest_campaign;
	return active ? active.name : null;
}

function require_campaign(campaign) {
	if (campaign) return true;
	frappe.msgprint({
		title: __('No Campaign'),
		indicator: 'orange',
		message: __('Analyze Storage first — that is what creates the campaign these act on.'),
	});
	return false;
}

// Every endpoint re-validates state server-side, so a stale form is refused rather than
// obeyed. When that happens the panel is reloaded so the operator sees the real state.
function call(frm, method, args, done) {
	return frappe.call({
		method: `cloud_file_storage.api.admin.${method}`,
		args: args || {},
		freeze: true,
		freeze_message: __('Working…'),
		callback: (response) => {
			frm.trigger('render_storage_health');
			done && done(response.message);
		},
		error: () => frm.trigger('render_storage_health'),
	});
}

function simple(frm, method, campaign) {
	if (!require_campaign(campaign)) return;
	call(frm, method, { campaign });
}

function analyze(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __('Analyze Storage'),
		fields: [
			{
				fieldname: 'title',
				fieldtype: 'Data',
				label: __('Campaign Title'),
				reqd: 1,
				default: __('Migration {0}', [frappe.datetime.now_date()]),
			},
			{
				fieldname: 'existing',
				fieldtype: 'Link',
				options: 'Cloud Migration Campaign',
				label: __('Or re-analyze an existing campaign'),
			},
		],
		primary_action_label: __('Analyze'),
		primary_action: (values) => {
			dialog.hide();
			if (values.existing) return call(frm, 'analyze_storage', { campaign: values.existing });
			call(frm, 'create_campaign', { title: values.title }, (campaign) =>
				call(frm, 'analyze_storage', { campaign })
			);
		},
	});
	dialog.show();
}

function plan(frm, campaign) {
	if (!require_campaign(campaign)) return;
	const dialog = new frappe.ui.Dialog({
		title: __('Build Migration Plan'),
		fields: [
			{
				fieldname: 'batch_size',
				fieldtype: 'Int',
				label: __('Batch Size'),
				default: 1000,
				description: __('Objects per batch. 1,000 keeps the job count bounded.'),
			},
		],
		primary_action_label: __('Build Plan'),
		primary_action: (values) => {
			dialog.hide();
			call(frm, 'build_migration_plan', { campaign, batch_size: values.batch_size });
		},
	});
	dialog.show();
}

function start(frm, campaign) {
	if (!require_campaign(campaign)) return;
	frappe.confirm(
		__('Start transferring objects for campaign {0}?', [campaign]),
		() => call(frm, 'start_campaign', { campaign })
	);
}

function stop_after_batch(frm, campaign) {
	if (!require_campaign(campaign)) return;
	frappe.confirm(
		__('Stop after the current batch commits? In-flight work is finished, not abandoned.'),
		() => call(frm, 'stop_after_current_batch', { campaign })
	);
}

function retry_failed(frm, campaign) {
	if (!require_campaign(campaign)) return;
	call(frm, 'retry_failed', { campaign }, (result) => {
		const payload = result || {};
		frappe.msgprint({
			title: __('Retry Failed'),
			indicator: 'blue',
			message: __(
				'{0} object(s) requeued. {1} have spent their attempts and are in the conflict queue.',
				[payload.retried || 0, payload.exhausted || 0]
			),
		});
	});
}

function export_report(frm, campaign) {
	if (!require_campaign(campaign)) return;
	call(frm, 'export_migration_report', { campaign }, (path) => {
		frappe.msgprint({
			title: __('Report Exported'),
			indicator: 'green',
			message: frappe.utils.escape_html(path || ''),
		});
	});
}

// A9: two audited actions, in this order. Approving deletes nothing; the job re-reads both
// the approval and every per-object gate before it moves a single file.
function delete_verified_local_copies(frm, campaign) {
	if (!require_campaign(campaign)) return;

	frappe.prompt(
		[
			{
				fieldname: 'note',
				fieldtype: 'Small Text',
				label: __('Approval note'),
				description: __('Recorded on the campaign with who approved and when.'),
			},
			{
				fieldname: 'confirm_phrase',
				fieldtype: 'Data',
				label: __('Type DELETE LOCAL FILES to confirm'),
				reqd: 1,
				description: __(
					'Checked on the server. Local copies are quarantined only after a fresh remote verification and a local re-stat of every object.'
				),
			},
		],
		(values) => {
			call(frm, 'approve_cleanup', { campaign, note: values.note }, () =>
				call(frm, 'start_cleanup', {
					campaign,
					confirm_phrase: values.confirm_phrase,
				})
			);
		},
		__('Delete Verified Local Copies'),
		__('Approve and Start')
	);
}

// ----------------------------------------------------------------------------- rendering

function indicator_for(check) {
	// A27 sets severity 'error' on a finding that is a hard warning rather than a broken
	// connection. Without this a real "the bucket cannot honour the restore window" finding
	// renders identically to "Skipped: the bucket could not be reached" — a positive finding
	// and the ABSENCE of a check on one colour. `_summarise` still derives the overall status
	// from `status`, so a working bucket is never reported as failed.
	if (check && check.severity === 'error') return 'red';
	const status = check && check.status ? check.status : check;
	return { ok: 'green', warning: 'orange', failed: 'red' }[status] || 'gray';
}

function render_connection(frm, payload) {
	const wrapper = frm.get_field('connection_status_html');
	if (!wrapper || !payload) return;

	const rows = (payload.checks || [])
		.map(
			(check) => `
				<tr>
					<td style="width: 12rem"><b>${frappe.utils.escape_html(check.check)}</b></td>
					<td><span class="indicator ${indicator_for(check)}">${frappe.utils.escape_html(
						check.message
					)}</span></td>
				</tr>`
		)
		.join('');

	wrapper.$wrapper.html(`
		<div class="frappe-card" style="padding: 1rem">
			<div class="indicator ${indicator_for(payload.status)}">
				${frappe.utils.escape_html(payload.bucket || '')} @
				${frappe.utils.escape_html(payload.endpoint_url || '')}
			</div>
			<table class="table table-borderless" style="margin-top: .75rem">${rows}</table>
			<div class="text-muted small">${frappe.utils.escape_html(payload.probe_note || '')}</div>
		</div>
	`);
}

function bytes(value) {
	return frappe.form.formatters.FileSize(value || 0);
}

// PLAN §A: the panel must distinguish cloud-managed permanent attachment bytes from bounded
// temporary/local operational bytes. They are rendered as separate cards with their own
// totals, and no total spans both — a single "storage used" figure is exactly the number
// that would let operational bytes read as migrated ones.
function render_health(frm, health) {
	const wrapper = frm.get_field('storage_health_html');
	if (!wrapper) return;

	const cloud = health.cloud_managed || {};
	const local = health.local_operational || {};
	const unmigrated = health.local_unmigrated || {};

	const warnings = (health.warnings || [])
		.map(
			(warning) =>
				`<div class="indicator orange">${frappe.utils.escape_html(warning.message)}</div>`
		)
		.join('');

	wrapper.$wrapper.html(`
		<div class="row">
			<div class="col-sm-4">
				<div class="frappe-card" style="padding: 1rem">
					<div class="text-muted small">${frappe.utils.escape_html(cloud.label || '')}</div>
					<div class="h4">${bytes(cloud.attachment_bytes)}</div>
					<div class="small">
						${cloud.attachment_objects || 0} ${__('objects')} &middot;
						${bytes(cloud.verified_bytes)} ${__('verified')}<br>
						${cloud.derived_objects || 0} ${__('thumbnails')} (${bytes(cloud.derived_bytes)})<br>
						${cloud.linked_files || 0} ${__('File rows linked')}
					</div>
				</div>
			</div>
			<div class="col-sm-4">
				<div class="frappe-card" style="padding: 1rem">
					<div class="text-muted small">${frappe.utils.escape_html(local.label || '')}</div>
					<div class="h4">${bytes(local.operational_bytes)}</div>
					<div class="small">
						${local.operational_files || 0} ${__('operational files')}<br>
						${__('cache')} ${bytes(local.cache_bytes)} / ${bytes(local.cache_budget_bytes)}<br>
						${__('quarantined')} ${local.quarantined_objects || 0} (${bytes(
							local.quarantine_bytes
						)})<br>
						${__('local by design')}: ${frappe.utils.escape_html(
							(local.ignored_doctypes || []).join(', ')
						)}
					</div>
				</div>
			</div>
			<div class="col-sm-4">
				<div class="frappe-card" style="padding: 1rem">
					<div class="text-muted small">${frappe.utils.escape_html(unmigrated.label || '')}</div>
					<div class="h4">${bytes(unmigrated.bytes)}</div>
					<div class="small">${unmigrated.files || 0} ${__('files')}</div>
				</div>
			</div>
		</div>
		<div style="margin-top: .75rem">
			<span class="indicator blue">${__('Mode')}: ${frappe.utils.escape_html(health.mode || '')}</span>
			${warnings}
		</div>
		${backup_html(health.backup)}
	`);
}

// Backup findings are DETECTED in `backup/health.backup_health` (P6) and only rendered here.
// They are a separate block with no number in it, deliberately: `tarballs_after_cutover` is a
// statement about local bytes, and folding it in next to a byte total is exactly how a reader
// ends up adding operational bytes to cloud-managed ones. The messages are printed verbatim —
// paraphrasing them here would make this panel a second answer to the same question.
function backup_indicator(level) {
	return { error: 'red', warn: 'orange', info: 'blue' }[level] || 'gray';
}

function backup_html(backup) {
	if (!backup || !backup.available) {
		return `<div class="text-muted small" style="margin-top: .75rem">${frappe.utils.escape_html(
			(backup && backup.reason) || __('Backup health is unavailable.')
		)}</div>`;
	}

	if (!backup.visible) {
		return `<div class="text-muted small" style="margin-top: .75rem">${frappe.utils.escape_html(
			backup.reason || ''
		)}</div>`;
	}

	const findings = (backup.findings || [])
		.map(
			(finding) =>
				`<div class="indicator ${backup_indicator(finding.level)}">${frappe.utils.escape_html(
					finding.message
				)}</div>`
		)
		.join('');

	const last = backup.last_backup_on
		? `${__('Last backup')}: ${frappe.utils.escape_html(
				String(backup.last_backup_on)
			)} (${frappe.utils.escape_html(String(backup.last_backup_status || ''))})`
		: __('No cloud backup has completed yet.');

	return `
		<div style="margin-top: 1rem">
			<div class="text-muted small">${frappe.utils.escape_html(backup.label || __('Backup'))}</div>
			<div class="small">${last}</div>
			${findings || `<div class="indicator green">${__('No backup findings.')}</div>`}
		</div>
	`;
}

function render_cache(frm, health) {
	const wrapper = frm.get_field('cache_stats_html');
	if (!wrapper) return;
	const local = health.local_operational || {};
	wrapper.$wrapper.html(`
		<div class="text-muted small">
			${__('Cache')}: ${bytes(local.cache_bytes)} ${__('of')} ${bytes(local.cache_budget_bytes)}
			&middot; ${local.cache_dirty_entries || 0} ${__('entries awaiting write-back')}
			&middot; ${frappe.utils.escape_html(local.cache_directory || '')}
		</div>
	`);
}

function render_migration(frm, health) {
	const wrapper = frm.get_field('migration_html');
	if (!wrapper) return;

	const migration = health.migration || {};
	const campaign = migration.active_campaign || migration.latest_campaign;

	if (!campaign) {
		wrapper.$wrapper.html(
			`<div class="text-muted">${__('No migration campaign yet.')}</div>`
		);
		return;
	}

	wrapper.$wrapper.html(`
		<div class="frappe-card" style="padding: 1rem">
			<div>
				<a href="/app/cloud-migration-campaign/${encodeURIComponent(campaign.name)}">
					${frappe.utils.escape_html(campaign.title || campaign.name)}
				</a>
				<span class="indicator blue">${frappe.utils.escape_html(campaign.status || '')}</span>
			</div>
			<div class="small text-muted">
				${__('Open conflicts')}: ${migration.open_conflicts || 0}
				(${migration.open_blockers || 0} ${__('blockers')})
			</div>
			<div style="margin-top: .5rem">
				<a href="/app/cloud-migration-dashboard">${__('Open the migration dashboard')}</a>
			</div>
		</div>
	`);
}
