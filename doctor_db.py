"""Check an explicit PostgreSQL URL with the installed PostgreSQL driver."""

from __future__ import annotations

from importlib import import_module, metadata
import os
import platform
import sys
from typing import Protocol, cast


DRIVERS = (("psycopg", "3.3.9"), ("pg8000", "1.31.6"))
REQUIRED_DRIVER = ("psycopg", "3.3.9")
PSYCOPG_REQUIREMENT = (
    "FAIL  psycopg 3.3.9 is not installed. "
    "Run python -m pip install -r requirements.txt from the harness folder."
)


class _Connection(Protocol):
    def close(self: _Connection) -> None: ...


class _PsycopgAPI(Protocol):
    def connect(
        self: _PsycopgAPI, url: str, *, connect_timeout: int
    ) -> _Connection: ...


def _connect(url: str) -> _Connection:
    psycopg = cast(_PsycopgAPI, import_module("psycopg"))
    return psycopg.connect(url, connect_timeout=3)


def _interpreter_line() -> str:
    return f"Python {platform.python_version()} at {sys.executable}"


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

    if not any(driver == REQUIRED_DRIVER[0] for driver, _ in installed):
        failures.append(PSYCOPG_REQUIREMENT)
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
    print(_interpreter_line())
    _installed, failures = _installed_drivers()
    for failure in failures:
        print(failure)
    return 1 if failures else 0


def main() -> int:
    print(_interpreter_line())
    url = os.environ.get("CMSC127_DATABASE_URL")
    if not url:
        print(
            "FAIL  CMSC127_DATABASE_URL is not set. "
            "Set it to a PostgreSQL database that the installed driver can read."
        )
        return 1

    installed, _failures = _installed_drivers()
    if not any(driver == REQUIRED_DRIVER[0] for driver, _ in installed):
        print(PSYCOPG_REQUIREMENT)
        return 1

    connection = None
    try:
        connection = _connect(url)
    except Exception as error:
        print(f"FAIL  psycopg: {_failure_message(error)}")
        return 1
    finally:
        if connection is not None:
            connection.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(check_drivers() if sys.argv[1:] == ["--drivers"] else main())
