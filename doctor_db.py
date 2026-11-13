"""Check an explicit PostgreSQL URL with each installed course driver."""

from __future__ import annotations

from importlib import import_module, metadata
import os
import sys
from typing import Protocol, cast
from urllib.parse import unquote, urlsplit


DRIVERS = (("psycopg", "3.3.9"), ("pg8000", "1.31.6"))


class _Connection(Protocol):
    def close(self: _Connection) -> None: ...


class _Pg8000API(Protocol):
    def connect(
        self: _Pg8000API,
        *,
        user: str,
        host: str,
        database: str,
        port: int,
        password: str | None,
        timeout: int,
    ) -> _Connection: ...


class _PsycopgAPI(Protocol):
    def connect(
        self: _PsycopgAPI, url: str, *, connect_timeout: int
    ) -> _Connection: ...


def _pg8000_connection(url: str) -> _Connection:
    dbapi = cast(_Pg8000API, import_module("pg8000.dbapi"))

    parsed = urlsplit(url)
    return dbapi.connect(
        user=unquote(parsed.username or ""),
        host=parsed.hostname or "127.0.0.1",
        database=unquote(parsed.path.lstrip("/")),
        port=parsed.port or 5432,
        password=unquote(parsed.password) if parsed.password else None,
        timeout=3,
    )


def _connect(driver: str, url: str) -> _Connection:
    if driver == "psycopg":
        psycopg = cast(_PsycopgAPI, import_module("psycopg"))
        return psycopg.connect(url, connect_timeout=3)
    return _pg8000_connection(url)


def _installed_drivers() -> tuple[list[tuple[str, str]], list[str]]:
    installed: list[tuple[str, str]] = []
    failures: list[str] = []
    for driver, expected_version in DRIVERS:
        try:
            version = metadata.version(driver)
        except metadata.PackageNotFoundError:
            continue

        if version != expected_version:
            failures.append(f"FAIL  {driver} is {version}, not {expected_version}.")
            failures.append(f"      Install {driver} {expected_version}.")
        else:
            installed.append((driver, version))

    if not installed:
        failures.append(
            "FAIL  No course driver is installed. "
            "Install psycopg[binary] 3.3.9 or pg8000 1.31.6."
        )
    return installed, failures


def _failure_message(error: Exception) -> str:
    detail = str(error).lower()
    if "password authentication failed" in detail or "28p01" in detail:
        return "The password is wrong. Check the password in CMSC127_DATABASE_URL."
    if any(
        token in detail
        for token in (
            "connection refused",
            "timeout",
            "could not connect",
            "can't create a connection",
        )
    ):
        return (
            "PostgreSQL did not answer at that address. Check that the server is "
            "running. Check the host and port in CMSC127_DATABASE_URL."
        )
    return (
        "The database connection failed. Check the server and CMSC127_DATABASE_URL. "
        "Use a free port if another process holds this one."
    )


def check_drivers() -> int:
    _installed, failures = _installed_drivers()
    for failure in failures:
        print(failure)
    return 1 if failures else 0


def main() -> int:
    url = os.environ.get("CMSC127_DATABASE_URL")
    if not url:
        print(
            "FAIL  CMSC127_DATABASE_URL is not set. "
            "Set it to the PostgreSQL database for this course."
        )
        return 1

    installed, failures = _installed_drivers()
    connected = False
    for driver, _version in installed:
        connection = None
        try:
            connection = _connect(driver, url)
            connected = True
        except Exception as error:
            failures.append(f"FAIL  {driver}: {_failure_message(error)}")
        finally:
            if connection is not None:
                connection.close()

    for failure in failures:
        if failure.startswith("FAIL  No course driver is installed."):
            print(failure)
    return 0 if connected else 1


if __name__ == "__main__":
    raise SystemExit(check_drivers() if sys.argv[1:] == ["--drivers"] else main())
