from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from tempfile import TemporaryDirectory
from typing import Any, cast
from unittest.mock import patch
from uuid import uuid4

import psycopg
from psycopg import sql


DATABASE_URL = os.environ.get("CMSC127_DATABASE_URL")
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BUNDLE_ROOT = REPOSITORY_ROOT / "dist" / "bundle"
TEST_PORT = 55448


@contextmanager
def test_schema() -> Generator[str, None, None]:
    if DATABASE_URL is None:
        raise RuntimeError("Set CMSC127_DATABASE_URL to the isolated PostgreSQL service.")
    schema = f"t12_{uuid4().hex[:10]}"
    with psycopg.connect(DATABASE_URL) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        connection.execute(
            sql.SQL(
                "CREATE TABLE {}.parent ("
                "id integer PRIMARY KEY, "
                "name text NOT NULL UNIQUE, "
                "score integer NOT NULL CHECK (score > 0))"
            ).format(sql.Identifier(schema))
        )
        connection.execute(
            sql.SQL(
                "CREATE TABLE {}.child ("
                "id integer PRIMARY KEY, "
                "parent_id integer NOT NULL REFERENCES {}.parent(id), "
                "code text UNIQUE, "
                "amount numeric NOT NULL CHECK (amount > 0))"
            ).format(sql.Identifier(schema), sql.Identifier(schema))
        )
        connection.execute(
            sql.SQL("CREATE TABLE {}.no_key (label text, value integer)").format(
                sql.Identifier(schema)
            )
        )
        connection.execute(
            sql.SQL("INSERT INTO {}.parent VALUES (1, 'parent', 5)").format(
                sql.Identifier(schema)
            )
        )
        connection.execute(
            sql.SQL("INSERT INTO {}.child VALUES (1, 1, 'one', 2)").format(
                sql.Identifier(schema)
            )
        )
    try:
        yield schema
    finally:
        with psycopg.connect(DATABASE_URL) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


@contextmanager
def running_server(project_root: Path, database_url: str | None) -> Generator[str, None, None]:
    server_module = importlib.import_module("server")
    server = server_module.create_server(
        project_root, BUNDLE_ROOT / "public", database_url, TEST_PORT
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{TEST_PORT}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def get_json(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=3) as response:
        return json.loads(response.read())


class HarnessPageTests(unittest.TestCase):
    def load_server(self) -> ModuleType:
        return importlib.import_module("server")

    def test_server_binds_only_to_loopback(self) -> None:
        server_module = self.load_server()
        server = server_module.create_server(REPOSITORY_ROOT, BUNDLE_ROOT / "public", None, TEST_PORT)
        try:
            self.assertEqual("127.0.0.1", server.server_address[0])
        finally:
            server.server_close()

    def test_startup_uses_and_prints_a_free_port(self) -> None:
        self.load_server()
        held_socket = socket.socket()
        held_socket.bind(("127.0.0.1", 55440))
        held_socket.listen()
        environment = os.environ.copy()
        environment["CMSC127_PORT"] = "55440"
        environment.pop("CMSC127_DATABASE_URL", None)
        process = subprocess.Popen(
            [sys.executable, str(REPOSITORY_ROOT / "server.py"), "--no-browser"],
            cwd=REPOSITORY_ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        selected_port: int | None = None
        output = ""
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and process.poll() is None:
                for port in [*range(55441, 55450)]:
                    if port == 55445:
                        continue
                    try:
                        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=0.2) as response:
                            if response.status == 200:
                                selected_port = port
                                break
                    except (OSError, urllib.error.URLError):
                        continue
                if selected_port is not None:
                    break
                time.sleep(0.05)
        finally:
            process.terminate()
            process.wait(timeout=5)
            held_socket.close()
            if process.stdout is not None:
                output = process.stdout.read()
                process.stdout.close()
                self.assertIn("Port 55440 is busy", output)
                if selected_port is not None:
                    self.assertIn(f":{selected_port}/", output)
        self.assertIsNotNone(selected_port, output)

    def test_non_get_requests_are_rejected(self) -> None:
        self.load_server()
        with running_server(REPOSITORY_ROOT, None) as base_url:
            request = urllib.request.Request(f"{base_url}/api/status", method="POST")
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request, timeout=3)
            self.assertEqual(405, error.exception.code)
            error.exception.close()

    def test_page_assets_are_local(self) -> None:
        self.load_server()
        with running_server(REPOSITORY_ROOT, None) as base_url:
            with urllib.request.urlopen(f"{base_url}/", timeout=3) as response:
                page = response.read().decode("utf-8")
            self.assertIn("/vendor/mermaid.min.js", page)
            self.assertNotIn("https://", page)

    def test_release_bundle_contains_compiled_page_modules(self) -> None:
        self.assertTrue((BUNDLE_ROOT / "public" / "app.js").is_file())
        self.assertTrue((BUNDLE_ROOT / "public" / "panelRequest.js").is_file())

    def test_status_returns_a_plain_message_when_database_is_stopped(self) -> None:
        self.load_server()
        with running_server(
            REPOSITORY_ROOT,
            "postgresql://postgres@127.0.0.1:55446/postgres",
        ) as base_url:
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(f"{base_url}/api/status", timeout=3)
            self.assertEqual(503, error.exception.code)
            payload = json.loads(error.exception.read())
            error.exception.close()
            self.assertIn("PostgreSQL is unavailable", payload["message"])
            self.assertNotIn("Traceback", json.dumps(payload))

    def test_status_returns_a_plain_message_for_a_bad_password(self) -> None:
        server_module = self.load_server()
        with patch.object(
            server_module.psycopg,
            "connect",
            side_effect=psycopg.OperationalError("password authentication failed"),
        ):
            with running_server(
                REPOSITORY_ROOT,
                "postgresql://postgres@127.0.0.1:55446/postgres",
            ) as base_url:
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(f"{base_url}/api/status", timeout=3)
                payload = json.loads(error.exception.read())
                error.exception.close()
            self.assertIn("Check the database password", payload["message"])
            self.assertNotIn("Traceback", json.dumps(payload))

    def test_server_catalog_statements_are_select_only(self) -> None:
        server_module = self.load_server()
        namespace = cast(dict[str, object], vars(server_module))
        statements = [
            value
            for name, value in namespace.items()
            if name.endswith("_SQL") and isinstance(value, str)
        ]
        self.assertTrue(statements)
        self.assertTrue(all(statement.lstrip().upper().startswith("SELECT") for statement in statements))
        source_file = server_module.__file__
        self.assertIsNotNone(source_file)
        source_path = Path(cast(str, source_file))
        source = source_path.read_text(encoding="utf-8")
        self.assertNotRegex(source, r"(?i)\b(?:INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|GRANT|REVOKE)\s")

    def test_parameterless_query_preserves_literal_percent(self) -> None:
        server_module = self.load_server()
        if DATABASE_URL is None:
            raise unittest.SkipTest("requires the isolated PostgreSQL service on port 55445")
        with psycopg.connect(DATABASE_URL) as connection:
            rows = server_module._fetch_rows(connection, r"SELECT 'pg\_%' AS pattern")
        self.assertEqual(r"pg\_%", rows[0][0])

    def test_parameterized_roles_query_preserves_literal_percent(self) -> None:
        server_module = self.load_server()
        if DATABASE_URL is None:
            raise unittest.SkipTest("requires the isolated PostgreSQL service on port 55445")
        with psycopg.connect(DATABASE_URL) as connection:
            rows = server_module._fetch_rows(
                connection, server_module._ROLES_SQL, ("public",)
            )
        self.assertIsInstance(rows, list)

    def test_checks_endpoint_reads_the_t13_result_file(self) -> None:
        self.load_server()
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            result_directory = root / ".cmsc127"
            result_directory.mkdir()
            (result_directory / "last-run.json").write_text(
                json.dumps(
                    {
                        "results": [
                            {
                                "id": "case-1",
                                "status": "fail",
                                "expected": {"rows": [[1]]},
                                "actual": {"rows": [[2]]},
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with running_server(root, None) as base_url:
                payload = get_json(f"{base_url}/api/checks")
            self.assertEqual("fail", payload["results"][0]["status"])
            self.assertEqual({"rows": [[1]]}, payload["results"][0]["expected"])
            self.assertEqual({"rows": [[2]]}, payload["results"][0]["actual"])

    def test_table_catalog_query_reads_the_seeded_schema(self) -> None:
        self.load_server()
        if DATABASE_URL is None:
            raise unittest.SkipTest("requires the isolated PostgreSQL service on port 55445")
        database_url = DATABASE_URL
        with test_schema() as schema, running_server(REPOSITORY_ROOT, database_url) as base_url:
            payload = get_json(f"{base_url}/api/tables?schema={schema}")
        tables = {table["name"]: table for table in payload["tables"]}
        self.assertEqual(["id"], tables["parent"]["primaryKey"])
        self.assertEqual(1, tables["parent"]["rows"][0]["id"])
        self.assertIn(schema, payload["schemas"])

    def test_schema_catalog_query_reports_types_constraints_and_differences(self) -> None:
        self.load_server()
        if DATABASE_URL is None:
            raise unittest.SkipTest("requires the isolated PostgreSQL service on port 55445")
        database_url = DATABASE_URL
        with test_schema() as schema, TemporaryDirectory() as temporary_directory:
            project_root = Path(temporary_directory)
            (project_root / "README.md").write_text(
                "```mermaid\nerDiagram\n  parent {\n    int id PK\n    string missing\n  }\n```\n",
                encoding="utf-8",
            )
            with running_server(project_root, database_url) as base_url:
                payload = get_json(f"{base_url}/api/schema?schema={schema}")
        parent = next(table for table in payload["tables"] if table["name"] == "parent")
        child_relationship = next(
            relationship
            for relationship in payload["relationships"]
            if relationship["child"] == "child"
        )
        self.assertEqual(["parent_id"], child_relationship["childColumns"])
        self.assertEqual(["id"], child_relationship["parentColumns"])
        self.assertEqual("integer", next(column for column in parent["columns"] if column["name"] == "id")["type"])
        constraint_types = {constraint["type"] for table in payload["tables"] for constraint in table["constraints"]}
        self.assertTrue({"PRIMARY KEY", "FOREIGN KEY", "UNIQUE", "CHECK"}.issubset(constraint_types))
        self.assertTrue(any("missing" in item["message"] for item in payload["differences"]))
        self.assertTrue(any(column["notNull"] for table in payload["tables"] for column in table["columns"]))

    def test_sessions_catalog_query_reports_an_open_transaction(self) -> None:
        self.load_server()
        if DATABASE_URL is None:
            raise unittest.SkipTest("requires the isolated PostgreSQL service on port 55445")
        database_url = DATABASE_URL
        with psycopg.connect(database_url, application_name="t12-open-transaction") as connection:
            connection.execute("BEGIN")
            connection.execute("SELECT 1")
            with running_server(REPOSITORY_ROOT, DATABASE_URL) as base_url:
                payload = get_json(f"{base_url}/api/sessions")
            session = next(item for item in payload["sessions"] if item["applicationName"] == "t12-open-transaction")
            self.assertTrue(session["openTransaction"])
            connection.rollback()

    def test_sessions_catalog_query_reports_the_blocking_pid(self) -> None:
        self.load_server()
        if DATABASE_URL is None:
            raise unittest.SkipTest("requires the isolated PostgreSQL service on port 55445")
        database_url = DATABASE_URL
        failures: list[BaseException] = []
        with test_schema() as schema:
            holder = psycopg.connect(database_url, application_name="t12-lock-holder")
            waiter = psycopg.connect(database_url, application_name="t12-lock-waiter")
            holder_pid = int(cast(tuple[int, ...], holder.execute("SELECT pg_backend_pid()").fetchone())[0])
            waiter_pid = int(cast(tuple[int, ...], waiter.execute("SELECT pg_backend_pid()").fetchone())[0])
            holder.execute(
                sql.SQL("SELECT id FROM {}.parent WHERE id = 1 FOR UPDATE").format(
                    sql.Identifier(schema)
                )
            )

            def wait_for_row() -> None:
                try:
                    waiter.execute(
                        sql.SQL("SELECT id FROM {}.parent WHERE id = 1 FOR UPDATE").format(
                            sql.Identifier(schema)
                        )
                    )
                except BaseException as error:
                    failures.append(error)

            thread = threading.Thread(target=wait_for_row, daemon=True)
            thread.start()
            try:
                with running_server(REPOSITORY_ROOT, database_url) as base_url:
                    deadline = time.monotonic() + 5
                    blocked: dict[str, Any] | None = None
                    while time.monotonic() < deadline:
                        payload = get_json(f"{base_url}/api/sessions")
                        blocked = next(
                            (
                                item
                                for item in payload["sessions"]
                                if item["pid"] == waiter_pid and holder_pid in item["blockingPids"]
                            ),
                            None,
                        )
                        if blocked is not None:
                            break
                        time.sleep(0.05)
                    self.assertIsNotNone(blocked)
            finally:
                holder.rollback()
                thread.join(timeout=3)
                waiter.rollback()
                holder.close()
                waiter.close()
            self.assertEqual([], failures)

    def test_roles_catalog_query_tracks_grant_and_revoke(self) -> None:
        self.load_server()
        if DATABASE_URL is None:
            raise unittest.SkipTest("requires the isolated PostgreSQL service on port 55445")
        database_url = DATABASE_URL
        role = f"t12_role_{uuid4().hex[:10]}"
        try:
            with test_schema() as schema, psycopg.connect(database_url) as connection:
                connection.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(role)))
                connection.execute(
                    sql.SQL("GRANT SELECT ON {}.parent TO {}").format(
                        sql.Identifier(schema), sql.Identifier(role)
                    )
                )
                connection.commit()
                with running_server(REPOSITORY_ROOT, database_url) as base_url:
                    granted = get_json(f"{base_url}/api/roles?schema={schema}")
                privileges = next(
                    item["privileges"]
                    for item in granted["privileges"]
                    if item["role"] == role and item["table"] == "parent"
                )
                self.assertIn("SELECT", privileges)
                connection.execute(
                    sql.SQL("REVOKE SELECT ON {}.parent FROM {}").format(
                        sql.Identifier(schema), sql.Identifier(role)
                    )
                )
                connection.commit()
                with running_server(REPOSITORY_ROOT, database_url) as base_url:
                    revoked = get_json(f"{base_url}/api/roles?schema={schema}")
                privileges = next(
                    item["privileges"]
                    for item in revoked["privileges"]
                    if item["role"] == role and item["table"] == "parent"
                )
                self.assertNotIn("SELECT", privileges)
                connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
                connection.commit()
        finally:
            with psycopg.connect(database_url) as cleanup:
                cleanup.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))

    def test_release_bundle_runs_without_node_on_path(self) -> None:
        self.load_server()
        if DATABASE_URL is None:
            raise unittest.SkipTest("requires the isolated PostgreSQL service on port 55445")
        database_url = DATABASE_URL
        environment = os.environ.copy()
        environment["CMSC127_DATABASE_URL"] = database_url
        environment["CMSC127_PORT"] = "55442"
        environment["CMSC127_NO_BROWSER"] = "1"
        environment["PATH"] = os.pathsep.join(
            entry for entry in environment.get("PATH", "").split(os.pathsep) if "node" not in entry.lower()
        )
        process = subprocess.Popen(
            [sys.executable, str(BUNDLE_ROOT / "server.py"), "--no-browser"],
            cwd=REPOSITORY_ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        selected_port: int | None = None
        output = ""
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                for port in range(55440, 55450):
                    if port == 55445:
                        continue
                    try:
                        payload = get_json(f"http://127.0.0.1:{port}/api/status")
                        if (
                            payload.get("server") == "running"
                            and payload.get("databaseStatus") == "connected"
                        ):
                            selected_port = port
                            break
                    except (OSError, urllib.error.URLError):
                        continue
                if selected_port is not None:
                    break
                time.sleep(0.1)
        finally:
            process.terminate()
            process.wait(timeout=5)
            if process.stdout is not None:
                output = process.stdout.read()
                process.stdout.close()
        self.assertIsNotNone(selected_port, output)


if __name__ == "__main__":
    unittest.main()
