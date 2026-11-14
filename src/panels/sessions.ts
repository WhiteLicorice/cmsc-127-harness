import { escapeHtml } from "./html.js";

export type SessionRow = {
  pid: number;
  role: string;
  applicationName: string;
  state: string | null;
  openTransaction: boolean;
  blockingPids: number[];
};

export function describeSession(session: SessionRow): string {
  const wait = session.blockingPids.length > 0
    ? `waits for ${session.blockingPids.join(", ")}`
    : "not waiting";
  const transaction = session.openTransaction ? "open transaction" : "no open transaction";
  return `PID ${session.pid} (${session.role}) ${wait}; ${transaction}.`;
}

export function renderSessions(sessions: SessionRow[]): string {
  if (sessions.length === 0) {
    return "<p>No other open sessions are visible.</p>";
  }
  const rows = sessions.map((session) => {
    const wait = session.blockingPids.length > 0 ? session.blockingPids.join(", ") : "not waiting";
    return `<tr><td>${session.pid}</td><td>${escapeHtml(session.role)}</td><td>${escapeHtml(session.applicationName)}</td><td>${escapeHtml(session.state ?? "unknown")}</td><td>${session.openTransaction ? "yes" : "no"}</td><td>${escapeHtml(wait)}</td></tr>`;
  }).join("");
  return `<div class="table-scroll" tabindex="0"><table><thead><tr><th>PID</th><th>Role</th><th>Application</th><th>State</th><th>Open transaction</th><th>Waits for</th></tr></thead><tbody>${rows}</tbody></table></div><p>PostgreSQL limits which sessions a role can see.</p>`;
}
