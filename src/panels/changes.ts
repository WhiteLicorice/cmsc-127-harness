import { escapeHtml } from "./html.js";
import { compareRows, type TableSnapshot } from "./tables.js";

export type ChangeFeedEvent = {
  time: string;
  table: string;
  kind: "insert" | "update" | "delete";
  key: string;
};

export function buildFeed(
  previous: TableSnapshot[],
  current: TableSnapshot[],
  observedAt: string,
): ChangeFeedEvent[] {
  const oldTables = new Map(previous.map((table) => [table.table, table]));
  const newTables = new Map(current.map((table) => [table.table, table]));
  const names = [...new Set([...oldTables.keys(), ...newTables.keys()])].sort();
  const events: ChangeFeedEvent[] = [];
  for (const name of names) {
    const oldTable = oldTables.get(name) ?? { table: name, primaryKey: [], rows: [] };
    const newTable = newTables.get(name) ?? { table: name, primaryKey: oldTable.primaryKey, rows: [] };
    for (const change of compareRows(oldTable, newTable)) {
      events.push({ time: observedAt, table: name, kind: change.kind, key: change.key });
    }
  }
  return orderFeed(events);
}

export function orderFeed(events: ChangeFeedEvent[]): ChangeFeedEvent[] {
  return [...events].sort(
    (left, right) => left.time.localeCompare(right.time) || left.table.localeCompare(right.table) || left.key.localeCompare(right.key),
  );
}

export function renderChangeFeed(events: ChangeFeedEvent[]): string {
  if (events.length === 0) {
    return "<p>No changes observed yet. The page checks once per second.</p>";
  }
  const rows = orderFeed(events)
    .map((event) => `<tr><td>${escapeHtml(new Date(event.time).toLocaleTimeString())}</td><td>${escapeHtml(event.table)}</td><td>${escapeHtml(event.kind)}</td><td><code>${escapeHtml(event.key)}</code></td></tr>`)
    .join("");
  return `<p>Changes come from the one-second table polls. The feed keeps the last 100 observations.</p><div class="table-scroll"><table><thead><tr><th>Time</th><th>Table</th><th>Change</th><th>Key</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}
