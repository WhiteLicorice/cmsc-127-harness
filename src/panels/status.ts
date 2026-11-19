import { CONNECT_PROMPT, TOO_OLD_MESSAGE } from "../hosted.js";
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

// The hosted page adds a Connect control and refuses a server that is older
// than the oldest release it can read. The local page never draws either.
export type StatusView = {
  hosted: boolean;
  connected: boolean;
  tooOld?: boolean;
};

const LOCAL_VIEW: StatusView = { hosted: false, connected: true };

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

export function renderStatus(status: StatusData, view: StatusView = LOCAL_VIEW): string {
  if (view.hosted && view.tooOld === true) {
    return `<p class="error-message">${escapeHtml(TOO_OLD_MESSAGE)}</p>`;
  }
  if (view.hosted && !view.connected) {
    return `<p>${escapeHtml(CONNECT_PROMPT)}</p><p><button type="button" data-action="connect">Connect</button></p>`;
  }
  if (status.databaseStatus !== "connected") {
    return `<p class="error-message">${escapeHtml(statusMessage(status))}</p>`;
  }
  return `<dl><dt>Server</dt><dd>${escapeHtml(status.server)}</dd><dt>Database</dt><dd>${escapeHtml(status.database ?? "unknown")}</dd><dt>Role</dt><dd>${escapeHtml(status.role ?? "unknown")}</dd><dt>Last poll</dt><dd>${escapeHtml(formatTime(status.lastPoll))}</dd></dl>`;
}
