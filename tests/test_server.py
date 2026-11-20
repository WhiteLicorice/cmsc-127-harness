from __future__ import annotations

import importlib
import io
import json
import os
import queue
import re
import subprocess
import sys
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request
from collections.abc import Generator, Sequence
from contextlib import contextmanager, nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from types import ModuleType
from tempfile import TemporaryDirectory
from typing import Any, cast
from unittest.mock import Mock, patch
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row


DATABASE_URL = os.environ.get("CMSC127_DATABASE_URL")
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ASSET_ROOT = REPOSITORY_ROOT / "public"


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
        project_root, ASSET_ROOT, database_url, 0
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def get_json(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=3) as response:
        return json.loads(response.read())


_ASCII_DIGITS = re.compile(r"[0-9]+")


def listed_ports(listed: str) -> list[int]:
    """The ports in a CMSC127_TEST_PORTS value.

    The value is a comma-separated list of ASCII decimal ports. An empty
    entry, a value that holds no port, a digit outside ASCII, and a port
    outside 1 to 65535 are faults. Each one stops the run here, before a
    socket opens, so the gate log carries the reason. The whole list is read
    before any port is chosen.
    """
    ports: list[int] = []
    for entry in listed.split(","):
        text = entry.strip()
        if not text:
            raise RuntimeError(
                "CMSC127_TEST_PORTS holds an empty entry. "
                "Give a port number for each entry."
            )
        if _ASCII_DIGITS.fullmatch(text) is None:
            raise RuntimeError(
                f"CMSC127_TEST_PORTS holds {text!r}, which is not a port number."
            )
        port = int(text)
        if not 1 <= port <= 65535:
            raise RuntimeError(
                f"CMSC127_TEST_PORTS holds {port}, which is outside 1 to 65535."
            )
        ports.append(port)
    return ports


def reserved_ports(count: int, fallback: Sequence[int] = ()) -> list[int]:
    """Loopback ports for a test that needs more than one server.

    A gate that runs beside another gate reserves its own ports. It sets
    CMSC127_TEST_PORTS to a comma-separated list. Every port this returns then
    comes from that list. No port repeats. A fixture never binds outside
    the range the gate reserved. A list that holds too few distinct ports
    stops the run. The message gives the count it needs, before any socket
    opens. The fallback range applies only when the variable is absent.
    Otherwise this asks the operating system for free ports.
    """
    listed = os.environ.get("CMSC127_TEST_PORTS")
    if listed is not None:
        chosen = list(dict.fromkeys(listed_ports(listed)))
        if len(chosen) < count:
            raise RuntimeError(
                f"CMSC127_TEST_PORTS lists {len(chosen)} distinct ports. "
                f"This fixture needs {count}. Add {count - len(chosen)} more."
            )
        return chosen[:count]
    if len(fallback) >= count:
        return list(fallback[:count])
    ports: list[int] = []
    while len(ports) < count:
        probe = socket.socket()
        try:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        finally:
            probe.close()
        if port not in ports:
            ports.append(port)
    return ports


def wait_for_server_port(
    process: subprocess.Popen[str], timeout: float
) -> tuple[int | None, list[str], queue.Queue[str], threading.Thread]:
    if process.stdout is None:
        raise RuntimeError("The server subprocess has no output stream.")
    output_queue: queue.Queue[str] = queue.Queue()
    output: list[str] = []

    def read_output() -> None:
        if process.stdout is None:
            return
        for line in process.stdout:
            output_queue.put(line)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            line = output_queue.get(timeout=min(0.1, deadline - time.monotonic()))
        except queue.Empty:
            if process.poll() is not None:
                break
            continue
        output.append(line)
        if "CMSC 127 Harness is available at http://127.0.0.1:" in line:
            port = int(line.rsplit(":", 1)[1].split("/", 1)[0])
            return port, output, output_queue, reader
    return None, output, output_queue, reader


def drain_server_output(
    output: list[str], output_queue: queue.Queue[str], reader: threading.Thread
) -> str:
    reader.join(timeout=1)
    while True:
        try:
            output.append(output_queue.get_nowait())
        except queue.Empty:
            break
    return "".join(output)


# The TypeScript selector in tests/ts/smokePort.test.ts reads the same
# cases, so one rule covers both languages.
# Each row is the value, the fault text it must raise, and the ports it must
# return. An empty fault text means the value is good.
PORT_LIST_CASES: tuple[tuple[str, str, tuple[int, ...]], ...] = (
    ("55740,55741", "", (55740, 55741)),
    (" 55740 , 55741 ", "", (55740, 55741)),
    ("1", "", (1,)),
    ("65535", "", (65535,)),
    ("55740,55740", "", (55740, 55740)),
    ("", "empty entry", ()),
    (" ", "empty entry", ()),
    ("55740,,55741", "empty entry", ()),
    ("55740,", "empty entry", ()),
    ("55740,99999", "outside 1 to 65535", ()),
    ("0", "outside 1 to 65535", ()),
    ("65536", "outside 1 to 65535", ()),
    ("\u00b2", "not a port number", ()),
    ("55740,\u00b2", "not a port number", ()),
    ("http", "not a port number", ()),
    ("+55740", "not a port number", ()),
    ("55740.0", "not a port number", ()),
)

# The same table for the count rule. Each row is the value, the count the
# fixture needs, the fallback range, and the ports it must return or the fault
# text it must raise. A value of None means the variable is absent.
PORT_RESERVE_CASES: tuple[
    tuple[str | None, int, tuple[int, ...], tuple[int, ...] | str], ...
] = (
    ("55740,55741,55742", 3, (), (55740, 55741, 55742)),
    ("55740,55741", 3, (55750, 55751, 55752), "distinct ports"),
    ("55740,55740,55741", 2, (), (55740, 55741)),
    ("55740,55740", 2, (), "distinct ports"),
    ("55740,55740,55740,55740,55740,55740", 6, (), "distinct ports"),
    (None, 3, (55750, 55751, 55752), (55750, 55751, 55752)),
    (" ", 1, (55750,), "empty entry"),
    ("55740,99999", 1, (), "outside 1 to 65535"),
)


class ReservedPortTests(unittest.TestCase):
    """A reserved port list is the only range a fixture may bind.

    A gate that runs beside another gate reserves its own ports. A fixture
    that then binds outside that list can take a port another gate holds. A
    list that cannot serve the fixture must stop the run before any socket
    opens. The message states the count the fixture needs.
    """

    def reserve(self, listed: str | None, count: int, fallback: Sequence[int]) -> list[int]:
        """Call reserved_ports with the variable set to one value."""
        environment = os.environ.copy()
        if listed is None:
            environment.pop("CMSC127_TEST_PORTS", None)
        else:
            environment["CMSC127_TEST_PORTS"] = listed
        with patch.dict(os.environ, environment, clear=True):
            return reserved_ports(count, fallback)

    def test_the_parse_table(self) -> None:
        """Every row of the shared table gives its ports or its fault."""
        for listed, fault, expected in PORT_LIST_CASES:
            with self.subTest(listed=listed):
                if fault:
                    with self.assertRaises(RuntimeError) as raised:
                        listed_ports(listed)
                    self.assertIn(fault, str(raised.exception))
                else:
                    self.assertEqual(list(expected), listed_ports(listed))

    def test_the_reserve_table(self) -> None:
        """Every row of the shared count table gives its ports or its fault."""
        for listed, count, fallback, expected in PORT_RESERVE_CASES:
            with self.subTest(listed=listed, count=count):
                if isinstance(expected, str):
                    with self.assertRaises(RuntimeError) as raised:
                        self.reserve(listed, count, fallback)
                    self.assertIn(expected, str(raised.exception))
                else:
                    self.assertEqual(
                        list(expected), self.reserve(listed, count, fallback)
                    )

    def test_an_unset_variable_without_a_fallback_asks_the_operating_system(self) -> None:
        chosen = self.reserve(None, 2, ())
        self.assertEqual(2, len(chosen))
        self.assertEqual(2, len(set(chosen)))
        for port in chosen:
            self.assertGreater(port, 0)
            self.assertLess(port, 65536)


class HarnessPageTests(unittest.TestCase):
    def load_server(self) -> ModuleType:
        return importlib.import_module("server")

    def test_server_binds_only_to_loopback(self) -> None:
        server_module = self.load_server()
        server = server_module.create_server(REPOSITORY_ROOT, ASSET_ROOT, None, 0)
        try:
            self.assertEqual("127.0.0.1", server.server_address[0])
        finally:
            server.server_close()

    def test_server_bind_does_not_resolve_the_loopback_hostname(self) -> None:
        server_module = self.load_server()
        with patch("socket.getfqdn", side_effect=AssertionError("reverse DNS lookup ran")):
            server = server_module.create_server(
                REPOSITORY_ROOT, ASSET_ROOT, None, 0
            )
        try:
            self.assertEqual("127.0.0.1", server.server_address[0])
            self.assertGreater(server.server_port, 0)
        finally:
            server.server_close()

    def test_main_uses_an_os_assigned_port_when_the_preferred_port_is_busy(self) -> None:
        server_module = self.load_server()
        server = Mock()
        server.server_port = 60123
        server.serve_forever.side_effect = KeyboardInterrupt
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(server_module, "create_server", side_effect=[OSError("busy"), server]) as create_server,
            patch.dict(os.environ, {"CMSC127_PORT": "55440"}),
            patch.object(sys, "argv", ["server.py", "--no-browser"]),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            exit_code = server_module.main()

        self.assertEqual(0, exit_code)
        self.assertEqual(0, create_server.call_args_list[1].args[3])
        self.assertIn("http://127.0.0.1:60123/", stdout.getvalue())
        self.assertIn("Port 55440 is busy", stderr.getvalue())

    def test_startup_uses_and_prints_a_free_port(self) -> None:
        self.load_server()
        # Hold a port that this gate reserved, so a busy default port on the
        # runner cannot break the test and the test stays inside the gate's
        # own range.
        held_socket = socket.socket()
        held_port = 0
        for candidate in reserved_ports(1):
            try:
                held_socket.bind(("127.0.0.1", candidate))
            except OSError:
                continue
            held_port = candidate
            break
        if held_port == 0:
            held_socket.close()
            self.skipTest("The reserved port is busy.")
        held_socket.listen()
        environment = os.environ.copy()
        environment["CMSC127_PORT"] = str(held_port)
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
        output_lines: list[str] = []
        output_queue: queue.Queue[str] = queue.Queue()
        reader: threading.Thread | None = None
        output = ""
        try:
            selected_port, output_lines, output_queue, reader = wait_for_server_port(process, 5)
            if selected_port is not None:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{selected_port}/", timeout=3
                ) as response:
                    self.assertEqual(200, response.status)
        finally:
            process.terminate()
            process.wait(timeout=5)
            held_socket.close()
            if reader is not None:
                output = drain_server_output(output_lines, output_queue, reader)
            if process.stdout is not None:
                process.stdout.close()
        self.assertIn(f"Port {held_port} is busy", output)
        if selected_port is not None:
            self.assertNotEqual(held_port, selected_port)
            self.assertIn(f":{selected_port}/", output)
        self.assertIsNotNone(selected_port, output)

    def test_main_keeps_an_explicit_port_outside_the_default_range(self) -> None:
        """A gate that reserves a port gets that port."""
        server_module = self.load_server()
        server = Mock()
        server.server_port = 55691
        server.serve_forever.side_effect = KeyboardInterrupt
        with (
            patch.object(server_module, "create_server", return_value=server) as create_server,
            patch.dict(os.environ, {"CMSC127_PORT": "55691"}),
            patch.object(sys, "argv", ["server.py", "--no-browser"]),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            exit_code = server_module.main()

        self.assertEqual(0, exit_code)
        self.assertEqual(55691, create_server.call_args.args[3])

    def test_main_uses_the_default_port_when_none_is_given(self) -> None:
        server_module = self.load_server()
        server = Mock()
        server.server_port = 55440
        server.serve_forever.side_effect = KeyboardInterrupt
        environment = os.environ.copy()
        environment.pop("CMSC127_PORT", None)
        with (
            patch.object(server_module, "create_server", return_value=server) as create_server,
            patch.dict(os.environ, environment, clear=True),
            patch.object(sys, "argv", ["server.py", "--no-browser"]),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            exit_code = server_module.main()

        self.assertEqual(0, exit_code)
        self.assertEqual(55440, create_server.call_args.args[3])

    def test_main_ignores_an_explicit_port_that_is_not_usable(self) -> None:
        """A value outside the port range falls back to the default."""
        server_module = self.load_server()
        for requested in ("not-a-port", "0", "70000", "-1"):
            server = Mock()
            server.server_port = 55440
            server.serve_forever.side_effect = KeyboardInterrupt
            with (
                patch.object(server_module, "create_server", return_value=server) as create_server,
                patch.dict(os.environ, {"CMSC127_PORT": requested}),
                patch.object(sys, "argv", ["server.py", "--no-browser"]),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                exit_code = server_module.main()

            self.assertEqual(0, exit_code, requested)
            self.assertEqual(55440, create_server.call_args.args[3], requested)

    def test_two_launchers_keep_two_explicit_ports(self) -> None:
        """Two gates on one host each bind the port they reserved."""
        self.load_server()
        ports = reserved_ports(2)
        processes: list[subprocess.Popen[str]] = []
        outputs: list[str] = []
        try:
            for port in ports:
                environment = os.environ.copy()
                environment["CMSC127_PORT"] = str(port)
                environment.pop("CMSC127_DATABASE_URL", None)
                processes.append(
                    subprocess.Popen(
                        [sys.executable, str(REPOSITORY_ROOT / "server.py"), "--no-browser"],
                        cwd=REPOSITORY_ROOT,
                        env=environment,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                    )
                )
            for process, port in zip(processes, ports):
                selected, lines, output_queue, reader = wait_for_server_port(process, 10)
                self.assertEqual(port, selected, "".join(lines))
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/", timeout=3
                ) as response:
                    self.assertEqual(200, response.status)
                outputs.append(drain_server_output(lines, output_queue, reader))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()
        for port, output in zip(ports, outputs):
            self.assertIn(f"http://127.0.0.1:{port}/", output)

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
            self.assertIn("vendor/mermaid.min.js", page)
            # The page loads every asset from its own folder. The byline link is
            # the one off-origin reference, and a reader clicks it on purpose.
            references = re.findall(r'(?:src|href)="([^"]+)"', page)
            external = [
                reference
                for reference in references
                if reference.startswith(("http://", "https://", "//"))
            ]
            self.assertEqual(external, ["https://github.com/WhiteLicorice/cmsc-127-harness"])

    def test_page_assets_contain_compiled_modules(self) -> None:
        self.assertTrue((ASSET_ROOT / "app.js").is_file())
        self.assertTrue((ASSET_ROOT / "panelRequest.js").is_file())

    def test_allowed_origin_receives_the_cors_header_and_vary(self) -> None:
        self.load_server()
        with running_server(REPOSITORY_ROOT, None) as base_url:
            request = urllib.request.Request(
                f"{base_url}/api/changes",
                headers={"Origin": "https://whitelicorice.github.io"},
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                self.assertEqual(
                    "https://whitelicorice.github.io",
                    response.headers.get("Access-Control-Allow-Origin"),
                )
                self.assertEqual("Origin", response.headers.get("Vary"))

    def test_another_origin_receives_no_cors_header(self) -> None:
        self.load_server()
        with running_server(REPOSITORY_ROOT, None) as base_url:
            for origin in ("https://example.com", "http://127.0.0.1:55440", "null"):
                request = urllib.request.Request(
                    f"{base_url}/api/changes", headers={"Origin": origin}
                )
                with urllib.request.urlopen(request, timeout=3) as response:
                    self.assertIsNone(
                        response.headers.get("Access-Control-Allow-Origin"), origin
                    )
                    self.assertIsNone(response.headers.get("Vary"), origin)

    def test_a_request_without_an_origin_receives_no_cors_header(self) -> None:
        self.load_server()
        with running_server(REPOSITORY_ROOT, None) as base_url:
            with urllib.request.urlopen(f"{base_url}/api/changes", timeout=3) as response:
                self.assertIsNone(response.headers.get("Access-Control-Allow-Origin"))
                self.assertIsNone(response.headers.get("Vary"))

    def test_cors_header_never_allows_every_origin(self) -> None:
        self.load_server()
        with running_server(REPOSITORY_ROOT, None) as base_url:
            request = urllib.request.Request(
                f"{base_url}/api/changes", headers={"Origin": "https://example.com"}
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                self.assertNotEqual("*", response.headers.get("Access-Control-Allow-Origin"))

    def test_status_reports_the_api_version(self) -> None:
        self.load_server()
        with running_server(REPOSITORY_ROOT, None) as base_url:
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(f"{base_url}/api/status", timeout=3)
            payload = json.loads(error.exception.read())
            error.exception.close()
        server_module = self.load_server()
        self.assertEqual(1, server_module.API_VERSION)
        self.assertEqual(server_module.API_VERSION, payload["api_version"])

    def test_connected_status_reports_the_api_version(self) -> None:
        # The hosted page reads this body, so the field must be here too.
        server_module = self.load_server()
        if DATABASE_URL is None:
            raise unittest.SkipTest("requires the isolated PostgreSQL service on port 55445")
        database_url = DATABASE_URL
        with running_server(REPOSITORY_ROOT, database_url) as base_url:
            payload = get_json(f"{base_url}/api/status")
        self.assertEqual("connected", payload["databaseStatus"])
        self.assertEqual(server_module.API_VERSION, payload["api_version"])

    def test_status_without_a_database_row_reports_the_api_version(self) -> None:
        # The page still reads the version when the database reports no row.
        server_module = self.load_server()
        if DATABASE_URL is None:
            raise unittest.SkipTest("requires the isolated PostgreSQL service on port 55445")
        database_url = DATABASE_URL
        with patch.object(server_module, "_fetch_rows", return_value=[]):
            with running_server(REPOSITORY_ROOT, database_url) as base_url:
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(f"{base_url}/api/status", timeout=3)
                self.assertEqual(503, error.exception.code)
                payload = json.loads(error.exception.read())
                error.exception.close()
        self.assertEqual("down", payload["databaseStatus"])
        self.assertEqual(server_module.API_VERSION, payload["api_version"])

    def test_api_version_does_not_depend_on_the_database(self) -> None:
        server_module = self.load_server()
        with patch.object(
            server_module, "_connect_dict", side_effect=psycopg.OperationalError("down")
        ):
            with running_server(
                REPOSITORY_ROOT, "postgresql://postgres@127.0.0.1:55449/postgres"
            ) as base_url:
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(f"{base_url}/api/status", timeout=3)
                self.assertEqual(503, error.exception.code)
                payload = json.loads(error.exception.read())
                error.exception.close()
        self.assertEqual(server_module.API_VERSION, payload["api_version"])

    def test_status_returns_a_plain_message_when_database_is_stopped(self) -> None:
        self.load_server()
        with running_server(
            REPOSITORY_ROOT,
            "postgresql://postgres@127.0.0.1:55449/postgres",
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

    def test_status_returns_a_plain_message_for_a_missing_privilege(self) -> None:
        server_module = self.load_server()
        with patch.object(
            server_module.psycopg,
            "connect",
            side_effect=psycopg.errors.InsufficientPrivilege(
                "permission denied for table student"
            ),
        ):
            with running_server(
                REPOSITORY_ROOT,
                "postgresql://postgres@127.0.0.1:55446/postgres",
            ) as base_url:
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(f"{base_url}/api/status", timeout=3)
                payload = json.loads(error.exception.read())
                error.exception.close()
            self.assertIn(
                "This database role cannot read a table. Check the role in "
                "CMSC127_DATABASE_URL and what it can read.",
                payload["message"],
            )
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

    def test_schema_selection_falls_back_when_public_is_absent(self) -> None:
        server_module = self.load_server()
        with patch.object(server_module, "_schema_names", return_value=["pair"]):
            schema, schemas = server_module._selected_schema(cast(Any, None), {})
        self.assertEqual("pair", schema)
        self.assertEqual(["pair"], schemas)

    def test_schema_selection_prefers_public_when_the_request_is_unknown(self) -> None:
        server_module = self.load_server()
        with patch.object(server_module, "_schema_names", return_value=["pair", "public"]):
            schema, schemas = server_module._selected_schema(
                cast(Any, None), {"schema": ["missing"]}
            )
        self.assertEqual("public", schema)
        self.assertEqual(["pair", "public"], schemas)

    def test_tables_endpoint_reports_an_empty_schema_list(self) -> None:
        server_module = self.load_server()
        with (
            patch.object(server_module, "_schema_names", return_value=[]),
            patch.object(server_module, "_connect_dict", return_value=nullcontext(None)),
        ):
            status, payload = server_module._tables_endpoint(Mock(), {})
        self.assertEqual(200, status)
        self.assertEqual([], payload["schemas"])
        self.assertIsNone(payload["schema"])
        self.assertEqual([], payload["tables"])
        self.assertIn("No schema", payload["message"])

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

    def test_relationship_query_counts_a_unique_subset_in_any_order(self) -> None:
        server_module = self.load_server()
        if DATABASE_URL is None:
            raise unittest.SkipTest("requires an isolated PostgreSQL service")
        schema = f"t12_{uuid4().hex[:10]}"
        try:
            with psycopg.connect(DATABASE_URL) as connection:
                connection.execute(
                    sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
                )
                connection.execute(
                    sql.SQL(
                        "CREATE TABLE {}.parent ("
                        "a integer, b integer, PRIMARY KEY (a, b))"
                    ).format(sql.Identifier(schema))
                )
                connection.execute(
                    sql.SQL(
                        "CREATE TABLE {}.child_a ("
                        "id integer PRIMARY KEY, a integer, b integer, "
                        "UNIQUE (a), "
                        "FOREIGN KEY (a, b) REFERENCES {}.parent (a, b))"
                    ).format(sql.Identifier(schema), sql.Identifier(schema))
                )
                connection.execute(
                    sql.SQL(
                        "CREATE TABLE {}.child_ba ("
                        "id integer PRIMARY KEY, a integer, b integer, "
                        "UNIQUE (b, a), "
                        "FOREIGN KEY (a, b) REFERENCES {}.parent (a, b))"
                    ).format(sql.Identifier(schema), sql.Identifier(schema))
                )
            with psycopg.connect(
                DATABASE_URL, row_factory=cast(Any, dict_row)
            ) as connection:
                rows = server_module._fetch_rows(
                    connection, server_module._RELATIONSHIPS_SQL, (schema,)
                )
        finally:
            with psycopg.connect(DATABASE_URL) as connection:
                connection.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
                )
        unique_by_child = {
            str(row["child"]): bool(row["unique"]) for row in rows
        }
        self.assertEqual({"child_a": True, "child_ba": True}, unique_by_child)

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

    def test_release_runs_without_node_on_path(self) -> None:
        self.load_server()
        if DATABASE_URL is None:
            raise unittest.SkipTest("requires the isolated PostgreSQL service on port 55445")
        database_url = DATABASE_URL
        environment = os.environ.copy()
        environment["CMSC127_DATABASE_URL"] = database_url
        environment["CMSC127_PORT"] = str(reserved_ports(1)[0])
        environment["CMSC127_NO_BROWSER"] = "1"
        environment["PATH"] = os.pathsep.join(
            entry for entry in environment.get("PATH", "").split(os.pathsep) if "node" not in entry.lower()
        )
        process = subprocess.Popen(
            [sys.executable, str(REPOSITORY_ROOT / "server.py"), "--no-browser"],
            cwd=REPOSITORY_ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        selected_port: int | None = None
        output_lines: list[str] = []
        output_queue: queue.Queue[str] = queue.Queue()
        reader: threading.Thread | None = None
        output = ""
        try:
            selected_port, output_lines, output_queue, reader = wait_for_server_port(process, 10)
            deadline = time.monotonic() + 10
            while selected_port is not None and time.monotonic() < deadline:
                try:
                    payload = get_json(f"http://127.0.0.1:{selected_port}/api/status")
                    if payload.get("server") == "running" and payload.get("databaseStatus") == "connected":
                        break
                except (OSError, urllib.error.URLError):
                    time.sleep(0.1)
            else:
                selected_port = None
        finally:
            process.terminate()
            process.wait(timeout=5)
            if reader is not None:
                output = drain_server_output(output_lines, output_queue, reader)
            if process.stdout is not None:
                process.stdout.close()
        self.assertIsNotNone(selected_port, output)


if __name__ == "__main__":
    unittest.main()
