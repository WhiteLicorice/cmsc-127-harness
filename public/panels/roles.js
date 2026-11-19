import { escapeHtml } from "./html.js";
export function describeRolePrivileges(entry) {
    return entry.privileges.length === 0 ? "no table privileges" : entry.privileges.join(", ");
}
export function renderRoles(entries) {
    if (entries.length === 0) {
        return "<p>No tables or roles exist in this schema.</p>";
    }
    const rows = entries.map((entry) => `<tr><td>${escapeHtml(entry.role)}</td><td>${escapeHtml(entry.table)}</td><td>${escapeHtml(describeRolePrivileges(entry))}</td></tr>`).join("");
    return `<div class="table-scroll" tabindex="0"><table><thead><tr><th>Role</th><th>Table</th><th>Privileges</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}
