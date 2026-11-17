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
    return "Check the database password, then start the page again.";
  }
  if (status.databaseStatus !== "connected") {
    return status.message ?? "PostgreSQL is unavailable. Check that the server is running and that CMSC127_DATABASE_URL is right.";
  }
  return "Connected to PostgreSQL.";
}

function formatTime(value: string | undefined): string {
  if (value === undefined) {
    return "unknown";
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

export function renderStatus(status: StatusData): string {
  if (status.databaseStatus !== "connected") {
    return `<p class="error-message">${escapeHtml(statusMessage(status))}</p>`;
  }
  return `<dl><dt>Server</dt><dd>${escapeHtml(status.server)}</dd><dt>Database</dt><dd>${escapeHtml(status.database ?? "unknown")}</dd><dt>Role</dt><dd>${escapeHtml(status.role ?? "unknown")}</dd><dt>Last poll</dt><dd>${escapeHtml(formatTime(status.lastPoll))}</dd></dl>`;
}
