// Copyright (c) 2026, Finstein and contributors
// For license information, please see license.txt
//
// The same mission buttons, bound to the campaign in front of you rather than to whichever
// campaign the Settings panel resolved as active. Same endpoints, same server-side gates —
// this file adds no precondition of its own, it only decides what to *show*.
//
// Showing a button that the server will refuse is not a bug worth fixing here: every
// endpoint re-validates state (a form can be minutes stale), and the refusal names the
// state, which is more useful than a button that silently disappeared. So visibility
// follows the frozen A9 machine as a hint, and the server has the last word.

const OPERATOR_ACTIONS = [
	{ label: __('Analyze Storage'), method: 'analyze_storage', states: ['Draft', 'Analyzed', 'Stopped', 'Failed'] },
	{ label: __('Build Migration Plan'), method: 'build_migration_plan', states: ['Analyzed', 'Planned', 'Stopped'] },
	{ label: __('Start'), method: 'start_campaign', states: ['Planned', 'Paused', 'Stopped', 'Running'] },
	{ label: __('Pause'), method: 'pause_campaign', states: ['Running'] },
	{ label: __('Resume'), method: 'resume_campaign', states: ['Paused'] },
	{ label: __('Stop After Current Batch'), method: 'stop_after_current_batch', states: ['Running', 'Paused'] },
	{ label: __('Retry Failed'), method: 'retry_failed', states: ['Running', 'Paused', 'Stopped', 'Failed'] },
	{ label: __('Verify'), method: 'start_verify', states: ['Running', 'Paused', 'Stopped', 'Failed'] },
];

frappe.ui.form.on('Cloud Migration Campaign', {
	refresh(frm) {
		if (frm.is_new()) return;

		frm.clear_custom_buttons();
		const group = __('Actions');

		OPERATOR_ACTIONS.filter((action) => action.states.includes(frm.doc.status)).forEach(
			(action) => {
				frm.add_custom_button(action.label, () => run(frm, action.method), group);
			}
		);

		frm.add_custom_button(
			__('Export Migration Report'),
			() =>
				frappe.call({
					method: 'cloud_file_storage.api.admin.export_migration_report',
					args: { campaign: frm.doc.name },
					freeze: true,
					callback: (response) =>
						frappe.msgprint({
							title: __('Report Exported'),
							indicator: 'green',
							message: frappe.utils.escape_html(response.message || ''),
						}),
				}),
			group
		);

		if (['Running', 'Stopped', 'Paused', 'Completed'].includes(frm.doc.status)) {
			frm.add_custom_button(
				__('Delete Verified Local Copies'),
				() => delete_verified_local_copies(frm),
				group
			);
		}

		frm.add_custom_button(__('Open Dashboard'), () =>
			frappe.set_route('cloud-migration-dashboard')
		);

		render_progress(frm);
	},
});

function run(frm, method) {
	frappe.call({
		method: `cloud_file_storage.api.admin.${method}`,
		args: { campaign: frm.doc.name },
		freeze: true,
		freeze_message: __('Working…'),
		callback: () => frm.reload_doc(),
		// A refusal means this form was stale. Reloading is the honest response: it shows
		// the state the server actually refused against.
		error: () => frm.reload_doc(),
	});
}

// The phrase is posted exactly as typed. `migration.api.start_cleanup` compares it, so the
// gate holds for the CLI and for a direct API call as well as for this dialog.
function delete_verified_local_copies(frm) {
	frappe.prompt(
		[
			{
				fieldname: 'note',
				fieldtype: 'Small Text',
				label: __('Approval note'),
			},
			{
				fieldname: 'confirm_phrase',
				fieldtype: 'Data',
				label: __('Type DELETE LOCAL FILES to confirm'),
				reqd: 1,
				description: __('Checked on the server, not here.'),
			},
		],
		(values) => {
			frappe.call({
				method: 'cloud_file_storage.api.admin.approve_cleanup',
				args: { campaign: frm.doc.name, note: values.note },
				freeze: true,
				callback: () =>
					frappe.call({
						method: 'cloud_file_storage.api.admin.start_cleanup',
						args: {
							campaign: frm.doc.name,
							confirm_phrase: values.confirm_phrase,
						},
						freeze: true,
						callback: () => frm.reload_doc(),
						error: () => frm.reload_doc(),
					}),
			});
		},
		__('Delete Verified Local Copies'),
		__('Approve and Start')
	);
}

function render_progress(frm) {
	const total = frm.doc.total_objects || 0;
	if (!total) return;

	const terminal =
		(frm.doc.objects_verified || 0) +
		(frm.doc.objects_cleaned || 0) +
		(frm.doc.objects_skipped || 0) +
		(frm.doc.objects_adopted || 0) +
		(frm.doc.objects_dedup_reused || 0);
	const pct = Math.min(100, Math.round((1000 * terminal) / total) / 10);

	frm.dashboard.add_progress(__('Migration Progress'), [
		{ title: __('Done'), width: `${pct}%`, progress_class: 'progress-bar-success' },
	]);
}
