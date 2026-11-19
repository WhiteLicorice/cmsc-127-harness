import { renderChangeFeed } from "./panels/changes.js";
import { renderChecks } from "./panels/checks.js";
import { renderRoles } from "./panels/roles.js";
import { renderSchema } from "./panels/schema.js";
import { renderSessions } from "./panels/sessions.js";
import { renderStatus } from "./panels/status.js";
import { renderTables } from "./panels/tables.js";
export const panelRegistry = [
    { id: "tables", title: "Tables", endpoint: (schema) => `/api/tables?schema=${encodeURIComponent(schema)}`, render: (data, previous) => renderTables(data, previous) },
    { id: "changes", title: "Changes", endpoint: () => "/api/changes", render: (data) => renderChangeFeed(data) },
    { id: "schema", title: "Schema", endpoint: (schema) => `/api/schema?schema=${encodeURIComponent(schema)}`, render: (data) => renderSchema(data) },
    { id: "sessions", title: "Sessions", endpoint: () => "/api/sessions", render: (data) => renderSessions(data.sessions) },
    { id: "checks", title: "Checks", endpoint: () => "/api/checks", render: (data) => renderChecks(data) },
    { id: "roles", title: "Roles", endpoint: (schema) => `/api/roles?schema=${encodeURIComponent(schema)}`, render: (data) => renderRoles(data.privileges) },
    { id: "status", title: "Status", endpoint: () => "/api/status", render: (data) => renderStatus(data) },
];
