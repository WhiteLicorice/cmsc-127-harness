import { renderChangeFeed, type ChangeFeedEvent } from "./panels/changes.js";
import { renderChecks, type RunnerResult } from "./panels/checks.js";
import { renderRoles, type RolePrivilege } from "./panels/roles.js";
import { renderSchema, type SchemaInput } from "./panels/schema.js";
import { renderSessions, type SessionRow } from "./panels/sessions.js";
import { renderStatus, type StatusData } from "./panels/status.js";
import { renderTables, type TablesResponse } from "./panels/tables.js";

export type PanelId = "tables" | "changes" | "schema" | "sessions" | "checks" | "roles" | "status";

export type PanelDefinition = {
  id: PanelId;
  title: string;
  endpoint: (schema: string) => string;
  render: (data: unknown, previous?: unknown) => string;
};

export const panelRegistry: readonly PanelDefinition[] = [
  { id: "tables", title: "Tables", endpoint: (schema) => `/api/tables?schema=${encodeURIComponent(schema)}`, render: (data, previous) => renderTables(data as TablesResponse, previous as TablesResponse | null) },
  { id: "changes", title: "Changes", endpoint: () => "/api/changes", render: (data) => renderChangeFeed(data as ChangeFeedEvent[]) },
  { id: "schema", title: "Schema", endpoint: (schema) => `/api/schema?schema=${encodeURIComponent(schema)}`, render: (data) => renderSchema(data as SchemaInput) },
  { id: "sessions", title: "Sessions", endpoint: () => "/api/sessions", render: (data) => renderSessions((data as { sessions: SessionRow[] }).sessions) },
  { id: "checks", title: "Checks", endpoint: () => "/api/checks", render: (data) => renderChecks(data as RunnerResult) },
  { id: "roles", title: "Roles", endpoint: (schema) => `/api/roles?schema=${encodeURIComponent(schema)}`, render: (data) => renderRoles((data as { privileges: RolePrivilege[] }).privileges) },
  { id: "status", title: "Status", endpoint: () => "/api/status", render: (data) => renderStatus(data as StatusData) },
];
