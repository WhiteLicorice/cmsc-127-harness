import { escapeHtml } from "./html.js";

export type RolePrivilege = {
  role: string;
  table: string;
  privileges: string[];
};

export function describeRolePrivileges(entry: RolePrivilege): string {
  return entry.privileges.length === 0 ? "no table privileges" : entry.privileges.join(", ");
}

export function renderRoles(entries: RolePrivilege[]): string {
  if (entries.length === 0) {
    return "<p>No tables or user roles exist in this schema.</p>";
  }
  const rows = entries.map((entry) =>
    `<tr><td>${escapeHtml(entry.role)}</td><td>${escapeHtml(entry.table)}</td><td>${escapeHtml(describeRolePrivileges(entry))}</td></tr>`,
  ).join("");
  return `<div class="table-scroll"><table><thead><tr><th>Role</th><th>Table</th><th>Privileges</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}
