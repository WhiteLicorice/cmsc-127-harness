from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, LiteralString, cast
from urllib.parse import parse_qs, unquote, urlsplit

import psycopg
from psycopg.rows import dict_row, tuple_row

from comparator import Difference, compare_erd


ROW_CAP = 100
POLL_INTERVAL_MS = 1000
FEED_LIMIT = 100

_SCHEMAS_SQL = r"""
SELECT schema_name
FROM information_schema.schemata
WHERE schema_name <> 'information_schema'
  AND schema_name NOT LIKE 'pg\_%' ESCAPE '\'
ORDER BY schema_name
"""

_TABLES_SQL = """
SELECT c.relname AS name,
       c.relkind AS kind,
       COALESCE(
           (
               SELECT array_agg(a.attname ORDER BY key_column.ordinality)
               FROM pg_catalog.pg_index AS index_row
               CROSS JOIN LATERAL unnest(index_row.indkey)
                   WITH ORDINALITY AS key_column(attnum, ordinality)
               JOIN pg_catalog.pg_attribute AS a
                 ON a.attrelid = c.oid
                AND a.attnum = key_column.attnum
               WHERE index_row.indrelid = c.oid
                 AND index_row.indisprimary
                 AND key_column.ordinality <= index_row.indnkeyatts
           ),
           ARRAY[]::name[]
       ) AS primary_key
FROM pg_catalog.pg_class AS c
JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
WHERE n.nspname = %s
  AND c.relkind IN ('r', 'p', 'v', 'm')
ORDER BY c.relname
"""

_COLUMNS_SQL = """
SELECT c.relname AS table_name,
       a.attname AS name,
       pg_catalog.format_type(a.atttypid, a.atttypmod) AS type,
       a.attnotnull AS not_null,
       pg_catalog.pg_get_expr(default_row.adbin, default_row.adrelid) AS default
FROM pg_catalog.pg_class AS c
JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
JOIN pg_catalog.pg_attribute AS a ON a.attrelid = c.oid
LEFT JOIN pg_catalog.pg_attrdef AS default_row
  ON default_row.adrelid = c.oid
 AND default_row.adnum = a.attnum
WHERE n.nspname = %s
  AND c.relkind IN ('r', 'p', 'v', 'm')
  AND a.attnum > 0
  AND NOT a.attisdropped
ORDER BY c.relname, a.attnum
"""

_CONSTRAINTS_SQL = """
SELECT child.relname AS table_name,
       constraint_row.conname AS name,
       CASE constraint_row.contype
           WHEN 'p' THEN 'PRIMARY KEY'
           WHEN 'f' THEN 'FOREIGN KEY'
           WHEN 'u' THEN 'UNIQUE'
           WHEN 'c' THEN 'CHECK'
       END AS type,
       pg_catalog.pg_get_constraintdef(constraint_row.oid, true) AS definition
FROM pg_catalog.pg_constraint AS constraint_row
JOIN pg_catalog.pg_class AS child ON child.oid = constraint_row.conrelid
JOIN pg_catalog.pg_namespace AS namespace_row
  ON namespace_row.oid = child.relnamespace
WHERE namespace_row.nspname = %s
  AND constraint_row.contype IN ('p', 'f', 'u', 'c')
ORDER BY child.relname, constraint_row.conname
"""

_RELATIONSHIPS_SQL = """
SELECT child.relname AS child,
       parent.relname AS parent,
       constraint_row.conname AS label,
       ARRAY(
           SELECT child_column.attname
           FROM unnest(constraint_row.conkey) WITH ORDINALITY AS key_column(attnum, ordinality)
           JOIN pg_catalog.pg_attribute AS child_column
             ON child_column.attrelid = child.oid
            AND child_column.attnum = key_column.attnum
           ORDER BY key_column.ordinality
       ) AS child_columns,
       ARRAY(
           SELECT parent_column.attname
           FROM unnest(constraint_row.confkey) WITH ORDINALITY AS key_column(attnum, ordinality)
           JOIN pg_catalog.pg_attribute AS parent_column
             ON parent_column.attrelid = parent.oid
            AND parent_column.attnum = key_column.attnum
           ORDER BY key_column.ordinality
       ) AS parent_columns,
       NOT EXISTS (
           SELECT 1
           FROM unnest(constraint_row.conkey) AS key_column(attnum)
           JOIN pg_catalog.pg_attribute AS child_column
             ON child_column.attrelid = child.oid
            AND child_column.attnum = key_column.attnum
           WHERE NOT child_column.attnotnull
       ) AS required,
       EXISTS (
           SELECT 1
           FROM pg_catalog.pg_constraint AS key_constraint
           WHERE key_constraint.conrelid = child.oid
             AND key_constraint.contype IN ('p', 'u')
             AND key_constraint.conkey = constraint_row.conkey
       ) AS unique
FROM pg_catalog.pg_constraint AS constraint_row
JOIN pg_catalog.pg_class AS child ON child.oid = constraint_row.conrelid
JOIN pg_catalog.pg_class AS parent ON parent.oid = constraint_row.confrelid
JOIN pg_catalog.pg_namespace AS namespace_row
  ON namespace_row.oid = child.relnamespace
WHERE namespace_row.nspname = %s
  AND constraint_row.contype = 'f'
ORDER BY child.relname, constraint_row.conname
"""

_SESSIONS_SQL = """
SELECT activity.pid,
       activity.usename AS role,
       activity.application_name AS application_name,
       activity.state,
       activity.xact_start IS NOT NULL AS open_transaction,
       pg_catalog.pg_blocking_pids(activity.pid) AS blocking_pids
FROM pg_catalog.pg_stat_activity AS activity
WHERE activity.datname = pg_catalog.current_database()
  AND activity.pid <> pg_catalog.pg_backend_pid()
ORDER BY activity.pid
"""

_ROLES_SQL = r"""
SELECT role_row.rolname AS role,
       table_row.relname AS table,
       array_remove(
           ARRAY[
               CASE WHEN pg_catalog.has_table_privilege(role_row.rolname, table_row.oid, 'SELECT') THEN 'SELECT' END,
               CASE WHEN pg_catalog.has_table_privilege(role_row.rolname, table_row.oid, 'INSERT') THEN 'INSERT' END,
               CASE WHEN pg_catalog.has_table_privilege(role_row.rolname, table_row.oid, 'UPDATE') THEN 'UPDATE' END,
               CASE WHEN pg_catalog.has_table_privilege(role_row.rolname, table_row.oid, 'DELETE') THEN 'DELETE' END,
               CASE WHEN pg_catalog.has_table_privilege(role_row.rolname, table_row.oid, 'TRUNCATE') THEN 'TRUNCATE' END,
               CASE WHEN pg_catalog.has_table_privilege(role_row.rolname, table_row.oid, 'REFERENCES') THEN 'REFERENCES' END,
               CASE WHEN pg_catalog.has_table_privilege(role_row.rolname, table_row.oid, 'TRIGGER') THEN 'TRIGGER' END
           ]::text[],
           NULL
       ) AS privileges
FROM pg_catalog.pg_roles AS role_row
CROSS JOIN pg_catalog.pg_class AS table_row
JOIN pg_catalog.pg_namespace AS namespace_row ON namespace_row.oid = table_row.relnamespace
WHERE namespace_row.nspname = %s
  AND table_row.relkind IN ('r', 'p', 'v', 'm')
  AND role_row.rolname NOT LIKE 'pg\_%%' ESCAPE '\'
ORDER BY role_row.rolname, table_row.relname
"""

_STATUS_SQL = """
SELECT pg_catalog.current_database() AS database,
       current_user AS role,
       pg_catalog.clock_timestamp() AS last_poll
"""


@dataclass(frozen=True)
class AppContext:
    project_root: Path
    asset_root: Path
    database_url: str | None


class HarnessHTTPServer(ThreadingHTTPServer):
    context: AppContext
    daemon_threads = True

    def __init__(self, context: AppContext, port: int) -> None:
        self.context = context
        super().__init__(("127.0.0.1", port), RequestHandler)


def _connect_dict(context: AppContext) -> psycopg.Connection[Any]:
    if context.database_url is None or context.database_url == "":
        raise RuntimeError("CMSC127_DATABASE_URL is not set.")
    return cast(
        psycopg.Connection[Any],
        psycopg.connect(
            context.database_url,
            connect_timeout=2,
            application_name="cmsc127-harness",
            options="-c default_transaction_read_only=on",
            row_factory=cast(Any, dict_row),
        ),
    )


def _connect_tuple(context: AppContext) -> psycopg.Connection[Any]:
    if context.database_url is None or context.database_url == "":
        raise RuntimeError("CMSC127_DATABASE_URL is not set.")
    return psycopg.connect(
        context.database_url,
        connect_timeout=2,
        application_name="cmsc127-harness",
        options="-c default_transaction_read_only=on",
        row_factory=tuple_row,
    )


def _fetch_rows(
    connection: psycopg.Connection[Any],
    statement: str,
    parameters: tuple[object, ...] = (),
) -> list[dict[str, object]]:
    with connection.cursor() as cursor:
        query = cast(LiteralString, statement)
        if parameters:
            cursor.execute(query, parameters)
        else:
            cursor.execute(query)
        return [cast(dict[str, object], row) for row in cursor.fetchall()]


def _json_value(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, list):
        return [_json_value(item) for item in cast(list[object], value)]
    if isinstance(value, dict):
        items = cast(dict[object, object], value)
        return {str(key): _json_value(item) for key, item in items.items()}
    return str(value)


def _json_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], _json_value(rows))


def _identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _schema_names(connection: psycopg.Connection[Any]) -> list[str]:
    rows = _fetch_rows(connection, _SCHEMAS_SQL)
    return [str(row["schema_name"]) for row in rows]


def _selected_schema(
    connection: psycopg.Connection[Any], query: dict[str, list[str]]
) -> tuple[str | None, list[str]]:
    schemas = _schema_names(connection)
    requested = query.get("schema", ["public"])[0]
    return (requested if requested in schemas else None), schemas


def _group_columns(rows: list[dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        table = str(row["table_name"])
        grouped.setdefault(table, []).append(
            {
                "name": row["name"],
                "type": row["type"],
                "notNull": row["not_null"],
                "default": row["default"],
            }
        )
    return grouped


def _table_rows(
    connection: psycopg.Connection[Any],
    schema: str,
    table: str,
    primary_key: list[str],
) -> list[dict[str, object]]:
    statement = f"SELECT * FROM {_identifier(schema)}.{_identifier(table)}"
    if primary_key:
        order = ", ".join(_identifier(column) for column in primary_key)
        statement += f" ORDER BY {order}"
    statement += f" LIMIT {ROW_CAP}"
    return _fetch_rows(connection, statement)


def _tables_endpoint(
    server: HarnessHTTPServer, query: dict[str, list[str]]
) -> tuple[int, object]:
    with _connect_dict(server.context) as connection:
        schema, schemas = _selected_schema(connection, query)
        if schema is None:
            return 400, {"message": "Choose a schema from the available list."}
        table_rows = _fetch_rows(connection, _TABLES_SQL, (schema,))
        column_groups = _group_columns(_fetch_rows(connection, _COLUMNS_SQL, (schema,)))
        tables: list[dict[str, object]] = []
        for table_row in table_rows:
            name = str(table_row["name"])
            primary_key = cast(list[str], table_row["primary_key"] or [])
            tables.append(
                {
                    "table": name,
                    "name": name,
                    "kind": table_row["kind"],
                    "primaryKey": primary_key,
                    "columns": [str(column["name"]) for column in column_groups.get(name, [])],
                    "rowCap": ROW_CAP,
                    "rows": _json_rows(_table_rows(connection, schema, name, primary_key)),
                }
            )
        return 200, {
            "schema": schema,
            "schemas": schemas,
            "rowCap": ROW_CAP,
            "tables": tables,
        }


def _extract_erd(project_root: Path) -> str:
    readme = project_root / "README.md"
    try:
        content = readme.read_text(encoding="utf-8")
    except OSError:
        return ""
    for match in re.finditer(r"```mermaid\s*\r?\n(.*?)```", content, re.DOTALL | re.IGNORECASE):
        block = match.group(1).strip()
        if block.startswith("erDiagram"):
            return block
    return ""


def _difference_data(difference: Difference) -> dict[str, str]:
    return {
        "property": difference.property,
        "subject": difference.subject,
        "erdValue": difference.erd_value,
        "schemaValue": difference.schema_value,
        "message": difference.message,
    }


def _schema_endpoint(
    server: HarnessHTTPServer, query: dict[str, list[str]]
) -> tuple[int, object]:
    erd_text = _extract_erd(server.context.project_root)
    with _connect_dict(server.context) as connection:
        schema, _ = _selected_schema(connection, query)
        if schema is None:
            return 400, {"message": "Choose a schema from the available list."}
        tables = _fetch_rows(connection, _TABLES_SQL, (schema,))
        columns = _group_columns(_fetch_rows(connection, _COLUMNS_SQL, (schema,)))
        constraints = _fetch_rows(connection, _CONSTRAINTS_SQL, (schema,))
        relationships = _fetch_rows(connection, _RELATIONSHIPS_SQL, (schema,))

    grouped_constraints: dict[str, list[dict[str, object]]] = {}
    for constraint in constraints:
        table_name = str(constraint["table_name"])
        grouped_constraints.setdefault(table_name, []).append(
            {
                "name": constraint["name"],
                "type": constraint["type"],
                "definition": constraint["definition"],
            }
        )

    table_data: list[dict[str, object]] = []
    for table in tables:
        name = str(table["name"])
        table_data.append(
            {
                "name": name,
                "kind": table["kind"],
                "primaryKey": cast(list[str], table["primary_key"] or []),
                "columns": columns.get(name, []),
                "constraints": grouped_constraints.get(name, []),
            }
        )

    relationship_data = [
        {
            "child": relationship["child"],
            "parent": relationship["parent"],
            "label": relationship["label"],
            "childColumns": relationship["child_columns"],
            "parentColumns": relationship["parent_columns"],
            "required": relationship["required"],
            "unique": relationship["unique"],
        }
        for relationship in relationships
    ]

    if erd_text:
        with _connect_tuple(server.context) as connection:
            differences = compare_erd(connection, erd_text, schema)
        difference_data = [_difference_data(difference) for difference in differences]
    else:
        difference_data = [
            {
                "property": "erd",
                "subject": "README.md",
                "erdValue": "erDiagram block",
                "schemaValue": "none",
                "message": "No Mermaid erDiagram block appears in README.md.",
            }
        ]

    return 200, {
        "schema": schema,
        "erdText": erd_text,
        "tables": table_data,
        "relationships": _json_value(relationship_data),
        "differences": difference_data,
    }


def _changes_endpoint(
    _server: HarnessHTTPServer, _query: dict[str, list[str]]
) -> tuple[int, object]:
    return 200, {"pollIntervalMs": POLL_INTERVAL_MS, "feedLimit": FEED_LIMIT}


def _sessions_endpoint(
    server: HarnessHTTPServer, _query: dict[str, list[str]]
) -> tuple[int, object]:
    with _connect_dict(server.context) as connection:
        sessions = _fetch_rows(connection, _SESSIONS_SQL)
    data = [
        {
            "pid": row["pid"],
            "role": row["role"],
            "applicationName": row["application_name"],
            "state": row["state"],
            "openTransaction": row["open_transaction"],
            "blockingPids": row["blocking_pids"],
        }
        for row in sessions
    ]
    return 200, {"sessions": _json_value(data)}


def _roles_endpoint(
    server: HarnessHTTPServer, query: dict[str, list[str]]
) -> tuple[int, object]:
    with _connect_dict(server.context) as connection:
        schema, _ = _selected_schema(connection, query)
        if schema is None:
            return 400, {"message": "Choose a schema from the available list."}
        privileges = _fetch_rows(connection, _ROLES_SQL, (schema,))
    data = [
        {
            "role": row["role"],
            "table": row["table"],
            "privileges": cast(list[object], row["privileges"] or []),
        }
        for row in privileges
    ]
    return 200, {"schema": schema, "privileges": _json_value(data)}


def _checks_endpoint(
    server: HarnessHTTPServer, _query: dict[str, list[str]]
) -> tuple[int, object]:
    result_path = server.context.project_root / ".cmsc127" / "last-run.json"
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 200, {
            "results": [],
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "message": "No check results found. Run check.sh from the project root.",
        }
    except (OSError, json.JSONDecodeError):
        return 200, {
            "results": [],
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "message": "The last check result file is not valid JSON.",
        }
    if not isinstance(result, dict):
        return 200, {
            "results": [],
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "message": "The last check result file has an unsupported format.",
        }
    result_data = cast(dict[str, object], result)
    if not isinstance(result_data.get("results"), list):
        return 200, {
            "results": [],
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "message": "The last check result file has an unsupported format.",
        }
    return 200, result_data


def _status_endpoint(
    server: HarnessHTTPServer, _query: dict[str, list[str]]
) -> tuple[int, object]:
    with _connect_dict(server.context) as connection:
        rows = _fetch_rows(connection, _STATUS_SQL)
    if not rows:
        return 503, {
            "server": "running",
            "databaseStatus": "down",
            "message": "PostgreSQL is unavailable. Check the database server and connection settings.",
        }
    row = rows[0]
    return 200, {
        "server": "running",
        "databaseStatus": "connected",
        "database": row["database"],
        "role": row["role"],
        "lastPoll": _json_value(row["last_poll"]),
    }


Endpoint = Callable[[HarnessHTTPServer, dict[str, list[str]]], tuple[int, object]]
API_ENDPOINTS: dict[str, Endpoint] = {
    "/api/tables": _tables_endpoint,
    "/api/changes": _changes_endpoint,
    "/api/schema": _schema_endpoint,
    "/api/sessions": _sessions_endpoint,
    "/api/checks": _checks_endpoint,
    "/api/roles": _roles_endpoint,
    "/api/status": _status_endpoint,
}


def _database_message(error: Exception) -> str:
    detail = str(error).lower()
    if "password" in detail or "authentication" in detail:
        return "Check the database password and restart the harness."
    if "cmsc127_database_url" in detail:
        return "Set CMSC127_DATABASE_URL to the local PostgreSQL connection URL."
    return "PostgreSQL is unavailable. Check the database server and connection settings."


class RequestHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        endpoint = API_ENDPOINTS.get(parts.path)
        if endpoint is not None:
            try:
                status, payload = endpoint(
                    cast(HarnessHTTPServer, self.server),
                    parse_qs(parts.query, keep_blank_values=True),
                )
            except (psycopg.Error, RuntimeError) as error:
                status = 503
                payload = {"message": _database_message(error)}
            except Exception:
                status = 500
                payload = {"message": "The harness could not complete this request."}
            self._send_json(status, payload)
            return
        if parts.path.startswith("/api/"):
            self._send_json(404, {"message": "This harness endpoint does not exist."})
            return
        self._send_asset(parts.path)

    def do_POST(self) -> None:
        self._send_json(405, {"message": "The harness accepts GET requests only."})

    def do_PUT(self) -> None:
        self._send_json(405, {"message": "The harness accepts GET requests only."})

    def do_DELETE(self) -> None:
        self._send_json(405, {"message": "The harness accepts GET requests only."})

    def _send_json(self, status: int, payload: object) -> None:
        body = json.dumps(_json_value(payload), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_asset(self, request_path: str) -> None:
        context = cast(HarnessHTTPServer, self.server).context
        relative = "index.html" if request_path == "/" else unquote(request_path).lstrip("/")
        asset_root = context.asset_root.resolve()
        asset_path = (asset_root / relative).resolve()
        if not asset_path.is_relative_to(asset_root) or not asset_path.is_file():
            self._send_json(404, {"message": "This page asset does not exist."})
            return
        try:
            body = asset_path.read_bytes()
        except OSError:
            self._send_json(404, {"message": "This page asset could not be read."})
            return
        content_type, _ = mimetypes.guess_type(asset_path.name)
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type or 'application/octet-stream'}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def create_server(
    project_root: Path,
    asset_root: Path,
    database_url: str | None,
    port: int,
) -> HarnessHTTPServer:
    context = AppContext(project_root.resolve(), asset_root.resolve(), database_url)
    return HarnessHTTPServer(context, port)


def _port_candidates(preferred: int) -> list[int]:
    candidates = [preferred, *range(55440, 55450)]
    return [port for index, port in enumerate(candidates) if port != 55445 and port not in candidates[:index]]


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve the read-only CMSC 127 harness page.")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the default browser.")
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    try:
        preferred = int(os.environ.get("CMSC127_PORT", "55440"))
    except ValueError:
        preferred = 55440
    if preferred < 55440 or preferred > 55449 or preferred == 55445:
        preferred = 55440

    bundle_root = Path(__file__).resolve().parent
    project_root = Path.cwd().resolve()
    server: HarnessHTTPServer | None = None
    for port in _port_candidates(preferred):
        try:
            server = create_server(
                project_root,
                bundle_root / "public",
                os.environ.get("CMSC127_DATABASE_URL"),
                port,
            )
        except OSError:
            print(f"Port {port} is busy. Trying another local port.", file=sys.stderr)
            continue
        break
    if server is None:
        print("Ports 55440 to 55449 are busy. Stop a local program and try again.", file=sys.stderr)
        return 1

    address = f"http://127.0.0.1:{server.server_port}/"
    print(f"CMSC 127 Harness is available at {address}", flush=True)
    if not arguments.no_browser and not webbrowser.open(address, new=2):
        print(f"Open this address in your browser: {address}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("The CMSC 127 Harness stopped.", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
