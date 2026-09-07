// Live migration ops. The one thing this page must not do is invent a number.
//
// Why a Page and not Number Cards (design §3.4 / ADR 0015): cards poll a count and cannot
// render a batch stream or per-phase progress. `publish_realtime` rides the queue Redis, so
// the coordinator throttles to one event per batch — a Page is the only surface that can
// subscribe to those events at all.
//
// Realtime is an enhancement, never the source of truth: socketio can be down, and a
// dashboard that then shows a frozen number without saying so is worse than one that shows
// nothing. So the page polls `get_campaign_snapshot` every 30s regardless, and the header
// says when the numbers were last refreshed.

frappe.pages['cloud-migration-dashboard'].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __('Cloud Migration Dashboard'),
		single_column: true,
	});

	frappe.cloud_migration_dashboard = new CloudMigrationDashboard(page);
};

frappe.pages['cloud-migration-dashboard'].on_page_show = function () {
	frappe.cloud_migration_dashboard && frappe.cloud_migration_dashboard.refresh();
};

const POLL_INTERVAL_MS = 30000;

class CloudMigrationDashboard {
	constructor(page) {
		this.page = page;
		this.campaign = null;
		this.snapshot = null;
		this.last_refreshed = null;

		this.make_controls();
		this.make_body();
		this.bind_realtime();
		this.start_polling();
		this.load_campaigns();
	}

	make_controls() {
		this.campaign_field = this.page.add_field({
			fieldtype: 'Link',
			fieldname: 'campaign',
			label: __('Campaign'),
			options: 'Cloud Migration Campaign',
			change: () => {
				this.campaign = this.campaign_field.get_value();
				this.refresh();
			},
		});

		this.page.set_primary_action(__('Refresh'), () => this.refresh(), 'refresh');
		this.page.add_menu_item(__('Open Conflicts'), () =>
			frappe.set_route('List', 'Cloud Migration Conflict', { status: 'Open' })
		);
		this.page.add_menu_item(__('Cloud Storage Settings'), () =>
			frappe.set_route('Form', 'Cloud Storage Settings')
		);
	}

	make_body() {
		this.body = $('<div class="cfs-dashboard"></div>').appendTo(this.page.main);
	}

	bind_realtime() {
		frappe.realtime.on('cfs_migration_progress', (data) => {
			if (!data || !this.campaign || data.name !== this.campaign) return;
			this.snapshot = data;
			this.last_refreshed = new Date();
			this.render();
		});
	}

	start_polling() {
		// Cleared by the framework when the page is destroyed; the guard keeps a hidden page
		// from issuing calls nobody is looking at.
		this.timer = setInterval(() => {
			if (this.campaign && frappe.get_route()[1] === 'cloud-migration-dashboard') this.refresh();
		}, POLL_INTERVAL_MS);
	}

	load_campaigns() {
		frappe.db
			.get_list('Cloud Migration Campaign', {
				fields: ['name', 'status'],
				order_by: 'modified desc',
				limit: 1,
			})
			.then((rows) => {
				if (rows && rows.length) {
					this.campaign_field.set_value(rows[0].name);
				} else {
					this.render_empty();
				}
			});
	}

	refresh() {
		if (!this.campaign) return this.render_empty();
		frappe
			.call({
				method: 'cloud_file_storage.api.admin.get_campaign_snapshot',
				args: { campaign: this.campaign },
			})
			.then((response) => {
				const payload = response.message || {};
				this.snapshot = payload.snapshot || {};
				this.convergence = payload.convergence || {};
				this.summary = payload.summary || {};
				this.last_refreshed = new Date();
				this.render();
			});
	}

	render_empty() {
		this.body.html(
			`<div class="text-muted" style="padding: 2rem 0">${__(
				'No migration campaign yet. Start one from Cloud Storage Settings.'
			)}</div>`
		);
	}

	render() {
		const snapshot = this.snapshot || {};
		const stamp = this.last_refreshed ? this.last_refreshed.toLocaleTimeString() : '-';

		this.body.html(`
			<div class="cfs-dashboard-header" style="margin-bottom: 1rem">
				<h4>${frappe.utils.escape_html(snapshot.title || this.campaign)}</h4>
				<div class="text-muted">
					${__('Status')}: <b>${frappe.utils.escape_html(snapshot.status || '-')}</b> &middot;
					${__('Phase')}: ${frappe.utils.escape_html(snapshot.active_phase || '-')} &middot;
					${__('Numbers as of')} ${stamp}
				</div>
			</div>
			${this.progress_html(snapshot)}
			${this.pipeline_html(snapshot)}
			${this.conflict_html(snapshot)}
		`);
	}

	progress_html(snapshot) {
		const pct = Math.min(100, Math.max(0, snapshot.progress_pct || 0));
		return `
			<div class="progress" style="height: 12px">
				<div class="progress-bar" role="progressbar" style="width: ${pct}%"></div>
			</div>
			<div class="text-muted" style="margin: .25rem 0 1.5rem">
				${pct}% &middot; ${__('remaining')} ${snapshot.remaining || 0} /
				${snapshot.total_objects || 0} ${__('objects')}
			</div>
		`;
	}

	pipeline_html(snapshot) {
		const phases = [
			[__('Pending'), snapshot.objects_pending],
			[__('Uploading'), snapshot.objects_uploading],
			[__('Uploaded'), snapshot.objects_uploaded],
			[__('Verified'), snapshot.objects_verified],
			[__('Cleaned'), snapshot.objects_cleaned],
			[__('Skipped'), snapshot.objects_skipped],
			[__('Adopted'), snapshot.objects_adopted],
			[__('Deduplicated'), snapshot.objects_dedup_reused],
			[__('Failed'), snapshot.objects_failed],
			[__('Conflict'), snapshot.objects_conflict],
		];
		const cells = phases
			.map(
				([label, value]) => `
					<div class="col-sm-3" style="margin-bottom: .75rem">
						<div class="text-muted small">${label}</div>
						<div class="h5">${frappe.utils.escape_html(String(value || 0))}</div>
					</div>`
			)
			.join('');
		return `<div class="row">${cells}</div>`;
	}

	conflict_html(snapshot) {
		if (!snapshot.open_blockers) return '';
		return `
			<div class="alert alert-warning" style="margin-top: 1rem">
				${snapshot.open_blockers} ${__(
					'Blocker conflict(s) are open. Cleanup cannot be approved until they are resolved or skipped.'
				)}
			</div>
		`;
	}
}
