// Copyright (c) 2026, Finstein and contributors
// For license information, please see license.txt

frappe.ui.form.on("Cloud Backup Settings", {
	refresh(frm) {
		frm.add_custom_button(__("Preview Lifecycle Policy"), () => preview_lifecycle(frm));
		frm.add_custom_button(__("Apply Lifecycle Policy"), () => preview_lifecycle(frm, true));
		frm.add_custom_button(__("Backup Now"), () => backup_now(frm));
	},
	lifecycle_preview(frm) {
		preview_lifecycle(frm);
	},
	apply_lifecycle(frm) {
		preview_lifecycle(frm, true);
	},
	backup_now(frm) {
		backup_now(frm);
	},
});

function backup_now(frm) {
	frappe.call({
		method: "cloud_file_storage.backup.tasks.backup_now",
		freeze: true,
		callback: () => frappe.show_alert({ message: __("Backup enqueued"), indicator: "blue" }),
	});
}

function rule_list(rules) {
	if (!rules || !rules.length) {
		return `<div class="text-muted">${__("None")}</div>`;
	}
	return (
		"<ul>" +
		rules
			.map((rule) => `<li><code>${frappe.utils.escape_html(rule.ID || __("(no id)"))}</code></li>`)
			.join("") +
		"</ul>"
	);
}

// The dialog always shows the preview first, including when the operator asked to apply:
// A20 requires the rules that would be DROPPED to be listed before anything is written,
// and the confirmation phrase is checked again on the server.
function preview_lifecycle(frm, apply) {
	frappe.call({
		method: "cloud_file_storage.backup.lifecycle.preview_lifecycle_policy",
		freeze: true,
		callback: (response) => {
			const preview = response.message;
			const warnings = (preview.warnings || [])
				.map(
					(warning) =>
						`<li><b>${frappe.utils.escape_html(warning.level)}</b>: ${frappe.utils.escape_html(
							warning.message
						)} <code>${frappe.utils.escape_html(warning.math || "")}</code></li>`
				)
				.join("");

			const fields = [
				{
					fieldtype: "HTML",
					options: `
						<h5>${__("Rules this app will write")}</h5>${rule_list(preview.generated_rules)}
						<h5>${__("Rules preserved (not managed by this app)")}</h5>${rule_list(preview.preserved_rules)}
						<h5 class="text-danger">${__("Rules that will be DROPPED")}</h5>${rule_list(preview.dropped_rules)}
						<h5>${__("Economics")}</h5><ul>${warnings || "<li>" + __("No findings") + "</li>"}</ul>
						<pre style="max-height: 240px; overflow: auto">${frappe.utils.escape_html(
							JSON.stringify(preview.policy_json, null, 2)
						)}</pre>`,
				},
			];

			if (apply) {
				fields.push({
					fieldtype: "Data",
					fieldname: "confirm_phrase",
					reqd: 1,
					label: __("Type {0} to confirm", [preview.confirm_phrase]),
					description: __("Applying schedules deletions on bucket {0}.", [preview.bucket]),
				});
			}

			const dialog = new frappe.ui.Dialog({
				title: apply ? __("Apply Lifecycle Policy") : __("Lifecycle Policy Preview"),
				size: "large",
				fields: fields,
				primary_action_label: apply ? __("Apply") : __("Close"),
				primary_action: (values) => {
					if (!apply) {
						dialog.hide();
						return;
					}
					frappe.call({
						method: "cloud_file_storage.backup.lifecycle.apply_lifecycle_policy",
						args: { confirm_phrase: values.confirm_phrase },
						freeze: true,
						callback: () => {
							dialog.hide();
							frm.reload_doc();
							frappe.show_alert({ message: __("Lifecycle policy applied"), indicator: "green" });
						},
					});
				},
			});
			dialog.show();
		},
	});
}
