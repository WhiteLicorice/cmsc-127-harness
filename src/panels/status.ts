import { escapeHtml } from "./html.js";

export type StatusData = {
  server: string;
  databaseStatus: string;
  database?: string;
  role?: string;
  lastPoll?: string;
  message?: string;
  error?: string;
};

export function statusMessage(status: StatusData): string {
  const error = `${status.message ?? ""} ${status.error ?? ""}`.toLowerCase();
  if (error.includes("password") || error.includes("authentication")) {
    return "Check the database password and start the harness again.";
  }
  if (status.databaseStatus !== "connected") {
    return status.message ?? "PostgreSQL is unavailable. Check the database server and connection settings.";
  }
  return "Connected to PostgreSQL.";
}

export function renderStatus(status: StatusData): string {
  if (status.databaseStatus !== "connected") {
    return `<p class="error-message">${escapeHtml(statusMessage(status))}</p>`;
  }
  return `<dl><dt>Server</dt><dd>${escapeHtml(status.server)}</dd><dt>Database</dt><dd>${escapeHtml(status.database ?? "unknown")}</dd><dt>Role</dt><dd>${escapeHtml(status.role ?? "unknown")}</dd><dt>Last poll</dt><dd>${escapeHtml(status.lastPoll ?? "unknown")}</dd></dl>`;
}
