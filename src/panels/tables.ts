import { displayValue, escapeHtml } from "./html.js";

export type Row = Record<string, unknown>;

export type TableSnapshot = {
  table: string;
  kind?: string;
  primaryKey: string[];
  columns?: string[];
  rowCap?: number;
  rows: Row[];
};

export type RowChange = {
  table: string;
  kind: "insert" | "update" | "delete";
  key: string;
  row: Row;
  previous?: Row;
};

export type TablesResponse = {
  schema: string | null;
  schemas: string[];
  rowCap: number;
  tables: TableSnapshot[];
  message?: string;
};

function canonical(value: unknown): string {
  if (value === null) {
    return "null";
  }
  if (Array.isArray(value)) {
    return `[${value.map(canonical).join(",")}]`;
  }
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>).sort(([left], [right]) =>
      left.localeCompare(right),
    );
    return `{${entries.map(([key, item]) => `${JSON.stringify(key)}:${canonical(item)}`).join(",")}}`;
  }
  return JSON.stringify(value) ?? String(value);
}

function rowKey(row: Row, primaryKey: string[]): string {
  return primaryKey.length === 0
    ? canonical(row)
    : canonical(primaryKey.map((column) => row[column] ?? null));
}

export function compareRows(previous: TableSnapshot, current: TableSnapshot): RowChange[] {
  const primaryKey = current.primaryKey;
  if (primaryKey.length > 0) {
    const before = new Map(previous.rows.map((row) => [rowKey(row, primaryKey), row]));
    const after = new Map(current.rows.map((row) => [rowKey(row, primaryKey), row]));
    const keys = [...new Set([...before.keys(), ...after.keys()])].sort();
    const changes: RowChange[] = [];
    for (const key of keys) {
      const oldRow = before.get(key);
      const newRow = after.get(key);
      if (oldRow === undefined && newRow !== undefined) {
        changes.push({ table: current.table, kind: "insert", key, row: newRow });
      } else if (newRow === undefined && oldRow !== undefined) {
        changes.push({ table: current.table, kind: "delete", key, row: oldRow });
      } else if (oldRow !== undefined && newRow !== undefined && canonical(oldRow) !== canonical(newRow)) {
        changes.push({ table: current.table, kind: "update", key, row: newRow, previous: oldRow });
      }
    }
    return changes;
  }

  const beforeCounts = new Map<string, { row: Row; count: number }>();
  const afterCounts = new Map<string, { row: Row; count: number }>();
  for (const row of previous.rows) {
    const key = canonical(row);
    const entry = beforeCounts.get(key);
    beforeCounts.set(key, { row, count: (entry?.count ?? 0) + 1 });
  }
  for (const row of current.rows) {
    const key = canonical(row);
    const entry = afterCounts.get(key);
    afterCounts.set(key, { row, count: (entry?.count ?? 0) + 1 });
  }

  const keys = [...new Set([...beforeCounts.keys(), ...afterCounts.keys()])].sort();
  const changes: RowChange[] = [];
  for (const key of keys) {
    const before = beforeCounts.get(key);
    const after = afterCounts.get(key);
    const oldCount = before?.count ?? 0;
    const newCount = after?.count ?? 0;
    for (let count = oldCount; count < newCount; count += 1) {
      const row = after?.row;
      if (row !== undefined) {
        changes.push({ table: current.table, kind: "insert", key, row });
      }
    }
    for (let count = newCount; count < oldCount; count += 1) {
      const row = before?.row;
      if (row !== undefined) {
        changes.push({ table: current.table, kind: "delete", key, row });
      }
    }
  }
  return changes;
}

function renderRow(row: Row, columns: string[], kind: RowChange["kind"] | ""): string {
  const marker = kind === "" ? "" : kind;
  const cells = columns.map((column) => `<td>${escapeHtml(displayValue(row[column] ?? null))}</td>`).join("");
  return `<tr data-change="${marker}" class="${marker}"><th>${marker}</th>${cells}</tr>`;
}

export function renderTableSnapshot(
  current: TableSnapshot,
  previous?: TableSnapshot | null,
): string {
  const changes = previous === undefined || previous === null ? [] : compareRows(previous, current);
  const remaining = [...changes];
  const columns = current.columns ?? Object.keys(current.rows[0] ?? {});
  const currentRows = current.rows.map((row) => {
    const index = remaining.findIndex(
      (change) => change.kind !== "delete" && canonical(change.row) === canonical(row),
    );
    const kind = index < 0 ? "" : remaining.splice(index, 1)[0]?.kind ?? "";
    return renderRow(row, columns, kind);
  });
  const removedRows = remaining
    .filter((change) => change.kind === "delete")
    .map((change) => renderRow(change.row, columns, "delete"));
  const header = columns.map((column) => `<th>${escapeHtml(column)}</th>`).join("");
  const cap = current.rowCap ?? 100;
  const emptyNote = (current.kind === "r" || current.kind === "p") && current.rows.length === 0 && removedRows.length === 0
    ? " This table has no committed rows in the latest poll. Check that the table name and the committed data match what you expect."
    : "";
  return `<section class="table-card"><h2>${escapeHtml(current.table)}</h2><p>Showing up to ${cap} rows. Only these rows are polled.${emptyNote}</p><div class="table-scroll" tabindex="0"><table><thead><tr><th>Change</th>${header}</tr></thead><tbody>${currentRows.join("")}${removedRows.join("")}</tbody></table></div></section>`;
}

export function renderTables(current: TablesResponse, previous?: TablesResponse | null): string {
  if (current.schemas.length === 0) {
    return `<p>${escapeHtml(current.message ?? "No schema is available in this database.")}</p>`;
  }
  const oldTables = new Map((previous?.tables ?? []).map((table) => [table.table, table]));
  const cards = current.tables
    .map((table) => renderTableSnapshot(table, oldTables.get(table.table)))
    .join("");
  return `<p>Rows use committed data. The page polls every second.</p>${cards || "<p>No tables or views exist in this schema.</p>"}`;
}
