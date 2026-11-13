"""Run staged CMSC 127 cases in a disposable PostgreSQL database."""

from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from types import TracebackType
from typing import Any, Protocol, cast
from uuid import uuid4

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from comparator import compare_erd


CASE_KINDS = {"output", "schema", "reject", "state", "race", "query"}
CASE_SUFFIXES = {".json", ".md", ".erd", ".sql", ".sh"}
MIGRATION_NAME = re.compile(r"(?P<number>[0-9]{4})_[a-z0-9_]+\.sql\Z")
FEATURE_PATTERNS = {
    "left_join": re.compile(r"\bLEFT(?:\s+OUTER)?\s+JOIN\b", re.IGNORECASE),
    "right_join": re.compile(r"\bRIGHT(?:\s+OUTER)?\s+JOIN\b", re.IGNORECASE),
    "full_join": re.compile(r"\bFULL(?:\s+OUTER)?\s+JOIN\b", re.IGNORECASE),
    "union": re.compile(r"\bUNION(?:\s+ALL)?\b", re.IGNORECASE),
    "intersect": re.compile(r"\bINTERSECT\b", re.IGNORECASE),
    "except": re.compile(r"\bEXCEPT\b", re.IGNORECASE),
}


class _Column(Protocol):
    name: str


class _DbCursor(Protocol):
    description: Sequence[_Column] | None

    def execute(
        self: _DbCursor,
        query: str,
        parameters: Sequence[object] | None = None,
    ) -> object: ...

    def fetchall(self: _DbCursor) -> list[tuple[object, ...]]: ...

    def fetchone(self: _DbCursor) -> tuple[object, ...] | None: ...

    def close(self: _DbCursor) -> None: ...

    def __enter__(self: _DbCursor) -> _DbCursor: ...

    def __exit__(
        self: _DbCursor,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...


class _DbConnection(Protocol):
    def cursor(self: _DbConnection) -> _DbCursor: ...

    def transaction(self: _DbConnection) -> Any: ...

    def close(self: _DbConnection) -> None: ...


def _as_object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    candidate = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in candidate):
        raise ValueError(f"{label} must use string keys")
    return {str(key): item for key, item in candidate.items()}


def _as_list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return cast(list[object], value)


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"{path.name}: line {error.lineno}, column {error.colno}: {error.msg}"
        ) from error
    except UnicodeDecodeError as error:
        raise ValueError(f"{path.name}: file is not UTF-8 text") from error


def _json_locations(text: str) -> dict[tuple[str | int, ...], tuple[int, int]]:
    """Return the start line and column for each JSON value path."""
    line_starts = [0, *(index + 1 for index, char in enumerate(text) if char == "\n")]
    decoder = json.JSONDecoder(parse_float=Decimal)
    locations: dict[tuple[str | int, ...], tuple[int, int]] = {}

    def skip_space(position: int) -> int:
        while position < len(text) and text[position].isspace():
            position += 1
        return position

    def line_column(position: int) -> tuple[int, int]:
        line_index = bisect_right(line_starts, position) - 1
        return line_index + 1, position - line_starts[line_index] + 1

    def visit(
        position: int, path: tuple[str | int, ...]
    ) -> tuple[object, int]:
        position = skip_space(position)
        locations[path] = line_column(position)
        if text[position] == "{":
            result: dict[str, object] = {}
            position = skip_space(position + 1)
            if text[position] == "}":
                return result, position + 1
            while True:
                key_value, position = decoder.raw_decode(text, position)
                key = cast(str, key_value)
                position = skip_space(position) + 1
                value, position = visit(position, (*path, key))
                result[key] = value
                position = skip_space(position)
                if text[position] == "}":
                    return result, position + 1
                position = skip_space(position + 1)
        if text[position] == "[":
            result_list: list[object] = []
            position = skip_space(position + 1)
            if text[position] == "]":
                return result_list, position + 1
            index = 0
            while True:
                value, position = visit(position, (*path, index))
                result_list.append(value)
                index += 1
                position = skip_space(position)
                if text[position] == "]":
                    return result_list, position + 1
                position = skip_space(position + 1)
        return decoder.raw_decode(text, position)

    visit(0, ())
    return locations


def _form_fault_location(root: Path, message: str) -> str:
    manifest_path = root / "cases" / "manifest.json"
    if not manifest_path.is_file():
        return f"cases/manifest.json: line 1, column 1: {message}"
    try:
        text = manifest_path.read_text(encoding="utf-8")
        parsed = json.loads(text, parse_float=Decimal)
        locations = _json_locations(text)
    except (OSError, ValueError, UnicodeDecodeError):
        return f"cases/manifest.json: line 1, column 1: {message}"
    if isinstance(parsed, dict):
        parsed_object = _as_object(cast(object, parsed), "cases/manifest.json")
    else:
        parsed_object = {}

    manifest_location: tuple[str | int, ...] = ()
    sidecar_path: Path | None = None
    sidecar_location: tuple[str | int, ...] = ()
    case_items = parsed_object.get("cases")
    cases = cast(list[object], case_items) if isinstance(case_items, list) else []
    matching_cases: list[int] = []
    for index, raw_case in enumerate(cases):
        if isinstance(raw_case, dict):
            raw_case_object = _as_object(cast(object, raw_case), f"case {index + 1}")
            case_id = raw_case_object.get("id")
            if isinstance(case_id, str) and message.startswith(
                (f"case {case_id}:", f"case {case_id} .expected:")
            ):
                matching_cases.append(index)
    case_index: int | None = None
    if matching_cases:
        case_index = matching_cases[-1] if "duplicate id" in message else matching_cases[0]
    numbered_case = re.match(r"case (\d+):", message)
    if case_index is None and numbered_case is not None:
        case_index = int(numbered_case.group(1)) - 1
    if case_index is not None and 0 <= case_index < len(cases):
        case_value = cases[case_index]
        case_path: tuple[str | int, ...] = ("cases", case_index)
        manifest_location = case_path
        if isinstance(case_value, dict):
            case_object = _as_object(cast(object, case_value), f"case {case_index + 1}")
            if ".expected" in message:
                relative_file = case_object.get("file")
                if isinstance(relative_file, str):
                    case_file = root / relative_file
                    sidecar_path = case_file.with_suffix(".expected")
                    if "columns" in message:
                        sidecar_location = ("columns",)
                    elif "rows" in message:
                        sidecar_location = ("rows",)
                manifest_location = (*case_path, "file")
            elif ".exit" in message:
                relative_file = case_object.get("file")
                if isinstance(relative_file, str):
                    sidecar_path = (root / relative_file).with_suffix(".exit")
                manifest_location = (*case_path, "file")
            elif "duplicate id" in message or " id " in message:
                manifest_location = (*case_path, "id")
            elif "stage" in message:
                manifest_location = (*case_path, "stage")
            elif "kind" in message:
                manifest_location = (*case_path, "kind")
            elif "file" in message:
                manifest_location = (*case_path, "file")
    elif message.startswith("cases/manifest.json: version"):
        manifest_location = ("version",)
    elif "invariant " in message:
        match = re.search(r"invariant (\d+)", message)
        if match is not None:
            manifest_location = ("invariants", int(match.group(1)) - 1)
            if "stage" in message:
                manifest_location = (*manifest_location, "stage")
    elif "skip " in message:
        match = re.search(r"skip (\d+)", message)
        if match is not None:
            manifest_location = ("skips", int(match.group(1)) - 1)
            if "stage" in message:
                manifest_location = (*manifest_location, "stage")
    elif "cases" in message or "unlisted case" in message:
        manifest_location = ("cases",)
    elif "invariants" in message:
        manifest_location = ("invariants",)
    elif "skips" in message:
        manifest_location = ("skips",)

    if sidecar_path is not None and sidecar_path.is_file():
        try:
            sidecar_text = sidecar_path.read_text(encoding="utf-8")
            sidecar_value = json.loads(sidecar_text, parse_float=Decimal)
            if isinstance(sidecar_value, dict):
                sidecar_locations = _json_locations(sidecar_text)
                line, column = sidecar_locations.get(sidecar_location, (1, 1))
                relative = sidecar_path.relative_to(root).as_posix()
                return f"{relative}: line {line}, column {column}: {message}"
        except (OSError, ValueError, UnicodeDecodeError):
            pass
    line, column = locations.get(manifest_location, locations.get((), (1, 1)))
    return f"cases/manifest.json: line {line}, column {column}: {message}"


def _load_manifest(root: Path) -> dict[str, object]:
    """Load and validate the pair's manifest and its case-file inventory."""
    case_root = root / "cases"
    manifest_path = case_root / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("cases/manifest.json: file is missing")
    manifest = _as_object(_read_json(manifest_path), "cases/manifest.json")
    if manifest.get("version") != 1:
        raise ValueError("cases/manifest.json: version must be 1")
    raw_cases = _as_list(manifest.get("cases"), "cases/manifest.json: cases")

    identifiers: set[str] = set()
    listed: set[Path] = set()
    for index, raw_case in enumerate(raw_cases, start=1):
        case = _as_object(raw_case, f"case {index}")
        case_id = case.get("id")
        stage = case.get("stage")
        kind = case.get("kind")
        relative_file = case.get("file")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError(f"case {index}: id must be a non-empty string")
        if case_id in identifiers:
            raise ValueError(f"case {case_id}: duplicate id")
        identifiers.add(case_id)
        if not isinstance(stage, int) or isinstance(stage, bool) or stage < 1:
            raise ValueError(f"case {case_id}: stage must be a positive integer")
        if kind not in CASE_KINDS:
            raise ValueError(f"case {case_id}: unsupported kind {kind!r}")
        if not isinstance(relative_file, str) or not relative_file:
            raise ValueError(f"case {case_id}: file must be a relative path")

        case_path = (root / relative_file).resolve()
        try:
            case_path.relative_to(case_root.resolve())
        except ValueError as error:
            raise ValueError(f"case {case_id}: file must be inside cases/") from error
        if not case_path.is_file():
            raise ValueError(f"case {case_id}: case file is missing: {relative_file}")
        allowed_suffixes = {
            "output": {".json", ".sql", ".sh"},
            "schema": {".erd", ".md"},
            "reject": {".sql"},
            "state": {".json"},
            "race": {".json"},
            "query": {".sql"},
        }
        if case_path.suffix not in allowed_suffixes[str(kind)]:
            raise ValueError(f"case {case_id}: {kind} needs a supported case-file type")
        listed.add(case_path)
        expected_path = case_path.with_suffix(".expected")
        if not expected_path.is_file():
            raise ValueError(f"case {case_id}: .expected sidecar is missing")
        expected = _as_object(_read_json(expected_path), f"case {case_id} .expected")
        if kind != "reject":
            columns = expected.get("columns")
            rows = expected.get("rows")
            if not isinstance(columns, list):
                raise ValueError(f"case {case_id} .expected: columns must be a list")
            if not isinstance(rows, list):
                raise ValueError(f"case {case_id} .expected: rows must be a list")
        elif expected.get("sqlstate") is not None and not isinstance(
            expected.get("sqlstate"), str
        ):
            raise ValueError(f"case {case_id} .expected: sqlstate must be a string")
        exit_path = case_path.with_suffix(".exit")
        if exit_path.exists():
            try:
                expected_exit = int(exit_path.read_text(encoding="utf-8").strip())
            except (ValueError, UnicodeDecodeError) as error:
                raise ValueError(f"case {case_id}: .exit must contain one integer") from error
            if expected_exit < 0:
                raise ValueError(f"case {case_id}: .exit must be zero or greater")

    unlisted_cases = sorted(
        path.resolve()
        for path in case_root.rglob("*")
        if path.is_file()
        and path.suffix in CASE_SUFFIXES
        and path.name != "manifest.json"
        and path.resolve() not in listed
    )
    if unlisted_cases:
        relative = unlisted_cases[0].relative_to(root).as_posix()
        raise ValueError(f"unlisted case file: {relative}")
    unlisted_sidecars = sorted(
        path.resolve()
        for path in case_root.rglob("*")
        if path.is_file()
        and path.suffix in {".expected", ".exit"}
        and path.with_suffix("").resolve()
        not in {case_path.with_suffix("") for case_path in listed}
    )
    if unlisted_sidecars:
        relative = unlisted_sidecars[0].relative_to(root).as_posix()
        raise ValueError(f"unlisted case sidecar: {relative}")
    raw_invariants = manifest.get("invariants", [])
    if not isinstance(raw_invariants, list):
        raise ValueError("cases/manifest.json: invariants must be a list")
    for index, raw_invariant in enumerate(cast(list[object], raw_invariants), start=1):
        invariant = _as_object(raw_invariant, f"invariant {index}")
        invariant_stage = invariant.get("stage")
        if not isinstance(invariant_stage, int) or isinstance(invariant_stage, bool) or invariant_stage < 1:
            raise ValueError(f"invariant {index}: stage must be a positive integer")
    skips = manifest.get("skips", [])
    if not isinstance(skips, list):
        raise ValueError("cases/manifest.json: skips must be a list")
    for index, raw_skip in enumerate(cast(list[object], skips), start=1):
        skip = _as_object(raw_skip, f"skip {index}")
        skip_stage = skip.get("stage", 1)
        if not isinstance(skip_stage, int) or isinstance(skip_stage, bool) or skip_stage < 1:
            raise ValueError(f"skip {index}: stage must be a positive integer")
    return manifest


def load_manifest(root: Path) -> dict[str, object]:
    """Load the manifest and add a source location to each form fault."""
    try:
        return _load_manifest(root)
    except ValueError as error:
        message = str(error)
        if re.search(r"line \d+, column \d+", message):
            raise
        raise ValueError(_form_fault_location(root, message)) from error


def select_cases(manifest: Mapping[str, object], stage: int) -> list[dict[str, object]]:
    """Return all cases through the requested stage."""
    raw_cases_value = manifest.get("cases")
    if not isinstance(raw_cases_value, list):
        return []
    selected: list[dict[str, object]] = []
    for raw_case in cast(list[object], raw_cases_value):
        if isinstance(raw_case, dict):
            case = cast(dict[str, object], raw_case)
            case_stage = case.get("stage")
            if isinstance(case_stage, int) and case_stage <= stage:
                selected.append(case)
    return selected


def _same_value(expected: object, actual: object) -> bool:
    if expected is None or actual is None:
        return expected is None and actual is None
    if isinstance(expected, bool) or isinstance(actual, bool):
        return type(expected) is type(actual) and expected == actual
    numeric = (int, float, Decimal)
    if isinstance(expected, numeric) and isinstance(actual, numeric):
        try:
            return Decimal(str(expected)) == Decimal(str(actual))
        except InvalidOperation:
            return False
    return expected == actual


def _json_text(value: object) -> str:
    def encode(item: object) -> str:
        if item is None:
            return "null"
        if isinstance(item, bool):
            return "true" if item else "false"
        if isinstance(item, Decimal):
            return format(item, "f") if item.is_finite() else json.dumps(str(item))
        if isinstance(item, int):
            return str(item)
        if isinstance(item, float):
            return json.dumps(item) if math.isfinite(item) else json.dumps(str(item))
        if isinstance(item, str):
            return json.dumps(item, ensure_ascii=False)
        if isinstance(item, (date,)):
            return json.dumps(item.isoformat())
        if isinstance(item, Mapping):
            mapping = cast(Mapping[str, object], item)
            return "{" + ", ".join(
                f"{json.dumps(str(key), ensure_ascii=False)}: {encode(child)}"
                for key, child in mapping.items()
            ) + "}"
        if isinstance(item, (list, tuple)):
            sequence = cast(Sequence[object], item)
            return "[" + ", ".join(encode(child) for child in sequence) + "]"
        return json.dumps(str(item), ensure_ascii=False)

    return encode(value)


def compare_dataset(
    expected: Mapping[str, object], actual: Mapping[str, object], *, ordered: bool
) -> list[str]:
    """Compare result column names and rows, with typed NULL and numeric values."""
    differences: list[str] = []
    expected_columns = expected.get("columns")
    actual_columns = actual.get("columns")
    expected_rows_value = expected.get("rows")
    actual_rows_value = actual.get("rows")
    if not isinstance(expected_columns, list) or not isinstance(actual_columns, list):
        return ["expected and actual results need a columns list"]
    if expected_columns != actual_columns:
        differences.append(
            "column order or names differ: "
            f"expected {expected_columns!r}, actual {actual_columns!r}"
        )
    if not isinstance(expected_rows_value, list) or not isinstance(actual_rows_value, list):
        return differences + ["expected and actual results need a rows list"]
    expected_rows = cast(list[object], expected_rows_value)
    actual_rows = cast(list[object], actual_rows_value)
    expected_column_list = cast(list[object], expected_columns)
    actual_column_list = cast(list[object], actual_columns)
    for label, rows in (("expected", expected_rows), ("actual", actual_rows)):
        for index, row in enumerate(rows, start=1):
            if not isinstance(row, list):
                differences.append(f"{label} row {index} is not a list")
            elif len(cast(list[object], row)) != len(
                expected_column_list if label == "expected" else actual_column_list
            ):
                differences.append(f"{label} row {index} has the wrong column count")
    if differences:
        return differences
    expected_rows = [cast(list[object], row) for row in expected_rows]
    actual_rows = [cast(list[object], row) for row in actual_rows]

    if ordered:
        row_pairs = zip(expected_rows, actual_rows, strict=False)
        if len(expected_rows) != len(actual_rows):
            differences.append(
                f"row count differs: expected {len(expected_rows)}, actual {len(actual_rows)}"
            )
        for index, (expected_row, actual_row) in enumerate(row_pairs, start=1):
            if not all(
                _same_value(expected_value, actual_value)
                for expected_value, actual_value in zip(expected_row, actual_row, strict=True)
            ):
                differences.append(
                    f"row {index} differs: expected {expected_row!r}, actual {actual_row!r}"
                )
    else:
        remaining = list(actual_rows)
        for expected_row in expected_rows:
            match = next(
                (
                    index
                    for index, actual_row in enumerate(remaining)
                    if len(expected_row) == len(actual_row)
                    and all(
                        _same_value(expected_value, actual_value)
                        for expected_value, actual_value in zip(
                            expected_row, actual_row, strict=True
                        )
                    )
                ),
                None,
            )
            if match is None:
                differences.append(f"missing row: {expected_row!r}")
            else:
                remaining.pop(match)
        differences.extend(f"unexpected row: {row!r}" for row in remaining)
    return differences


def _read_expected(case_path: Path) -> dict[str, object]:
    value = _as_object(_read_json(case_path.with_suffix(".expected")), ".expected")
    return value


def _expected_exit(case_path: Path) -> int:
    path = case_path.with_suffix(".exit")
    if not path.exists():
        return 0
    return int(path.read_text(encoding="utf-8").strip())


def _dataset_from_cursor(cursor: _DbCursor) -> dict[str, object]:
    if cursor.description is None:
        return {"columns": [], "rows": []}
    columns = [column.name for column in cursor.description]
    rows = [list(row) for row in cursor.fetchall()]
    return {"columns": columns, "rows": rows}


def _execute(
    cursor: _DbCursor,
    statement: str,
    parameters: tuple[object, ...] | None = None,
) -> None:
    cursor.execute(statement, parameters)


def _extract_erd(text: str) -> str:
    match = re.search(r"```(?:mermaid)?\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    return match.group(1) if match is not None else text


def _bash_executable() -> str:
    if os.name == "nt":
        git_executable = shutil.which("git")
        if git_executable is not None:
            git_root = Path(git_executable).resolve().parent.parent
            git_bash = git_root / "bin" / "bash.exe"
            if git_bash.is_file():
                return str(git_bash)
    return "bash"


def _run_pair(
    root: Path, arguments: Sequence[str], database_url: str, timeout: float = 60.0
) -> subprocess.CompletedProcess[str]:
    command = [_bash_executable(), "./run", *arguments]
    environment = os.environ.copy()
    environment["CMSC127_DATABASE_URL"] = database_url
    environment["CMSC127_PYTHON"] = sys.executable
    database_name = conninfo_to_dict(database_url).get("dbname", "postgres")
    environment["PGDATABASE"] = str(database_name)
    return subprocess.run(
        command,
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _database_url(base_url: str, database: str) -> str:
    return make_conninfo(base_url, dbname=database)


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _create_database(base_url: str) -> tuple[str, _DbConnection]:
    name = f"cmsc127_{uuid4().hex}"
    admin = cast(_DbConnection, psycopg.connect(base_url, autocommit=True))
    try:
        with admin.cursor() as cursor:
            _execute(cursor, f"CREATE DATABASE {_quote_identifier(name)}")
    except BaseException:
        admin.close()
        raise
    return name, admin


def _drop_database(name: str, admin: _DbConnection) -> None:
    try:
        with admin.cursor() as cursor:
            _execute(cursor, f"DROP DATABASE IF EXISTS {_quote_identifier(name)} WITH (FORCE)")
    finally:
        admin.close()


def _apply_migrations(root: Path, connection: _DbConnection) -> None:
    migration_dir = root / "migrations"
    files = sorted(migration_dir.glob("*.sql")) if migration_dir.exists() else []
    numbers: list[int] = []
    for path in files:
        match = MIGRATION_NAME.fullmatch(path.name)
        if match is None:
            raise ValueError(
                f"invalid migration name {path.name!r}; use NNNN_lowercase_description.sql"
            )
        numbers.append(int(match.group("number")))
    if numbers != list(range(1, len(numbers) + 1)):
        raise ValueError("migration numbers must start at 0001 and have no gaps")
    for path in files:
        with connection.cursor() as cursor:
            _execute(cursor, path.read_text(encoding="utf-8"))


def _run_sql_file(
    connection: _DbConnection, path: Path
) -> dict[str, object]:
    with connection.cursor() as cursor:
        _execute(cursor, path.read_text(encoding="utf-8"))
        return _dataset_from_cursor(cursor)


def _sql_error(connection: _DbConnection, statement: str) -> str | None:
    try:
        with connection.transaction():
            with connection.cursor() as cursor:
                _execute(cursor, statement)
    except psycopg.Error as error:
        return error.sqlstate or error.__class__.__name__
    return None


def _run_case(
    root: Path,
    case: Mapping[str, object],
    connection: _DbConnection,
    database_url: str,
) -> dict[str, object]:
    case_id = str(case["id"])
    case_path = root / str(case["file"])
    expected = _read_expected(case_path)
    kind = str(case["kind"])
    ordered = case.get("ordered", True) is not False
    if kind == "schema":
        erd_text = _extract_erd(case_path.read_text(encoding="utf-8"))
        differences = compare_erd(connection, erd_text)
        actual: dict[str, object] = {
            "columns": ["difference"],
            "rows": [[item.message] for item in differences],
        }
        problems = compare_dataset(expected, actual, ordered=ordered)
    elif kind == "query":
        actual = _run_sql_file(connection, case_path)
        problems = compare_dataset(expected, actual, ordered=ordered)
    elif kind == "reject":
        error_code = _sql_error(connection, case_path.read_text(encoding="utf-8"))
        expected_code = expected.get("sqlstate")
        if error_code is None:
            actual = {"sqlstate": None}
            problems = ["statement succeeded, but the case requires rejection"]
        else:
            actual = {"sqlstate": error_code}
            problems = (
                []
                if expected_code is None or expected_code == error_code
                else [f"SQLSTATE differs: expected {expected_code!r}, actual {error_code!r}"]
            )
    elif kind == "state":
        body = _as_object(_read_json(case_path), f"case {case_id}")
        operation = body.get("operation")
        check_sql = body.get("check_sql")
        if not isinstance(operation, str) or not isinstance(check_sql, str):
            raise ValueError(f"case {case_id}: state file needs operation and check_sql strings")
        with connection.cursor() as cursor:
            _execute(cursor, operation)
        with connection.cursor() as cursor:
            _execute(cursor, check_sql)
            actual = _dataset_from_cursor(cursor)
        problems = compare_dataset(expected, actual, ordered=ordered)
    elif kind == "race":
        body = _as_object(_read_json(case_path), f"case {case_id}")
        steps = body.get("steps")
        check_sql = body.get("check_sql")
        if not isinstance(steps, list) or not isinstance(check_sql, str):
            raise ValueError(f"case {case_id}: race file needs steps and check_sql")
        second = cast(_DbConnection, psycopg.connect(database_url, autocommit=True))
        step_results: list[dict[str, object]] = []
        problems = []
        try:
            for index, raw_step in enumerate(cast(list[object], steps), start=1):
                step = _as_object(raw_step, f"case {case_id} step {index}")
                session = step.get("session")
                statement = step.get("sql")
                if session not in {"a", "b"} or not isinstance(statement, str):
                    raise ValueError(f"case {case_id}: each race step needs session a or b and SQL")
                active = connection if session == "a" else second
                with active.cursor() as cursor:
                    _execute(cursor, statement)
                    step_actual = _dataset_from_cursor(cursor)
                step_expected_value = step.get("expected")
                step_result: dict[str, object] = {
                    "step": index,
                    "session": session,
                    "expected": step_expected_value,
                    "actual": step_actual,
                }
                if step_expected_value is not None:
                    step_expected = _as_object(
                        step_expected_value, f"case {case_id} step {index} expected"
                    )
                    step_differences = compare_dataset(
                        step_expected, step_actual, ordered=True
                    )
                    if step_differences:
                        problems.extend(
                            f"step {index}: {difference}" for difference in step_differences
                        )
                    step_result["differences"] = step_differences
                step_results.append(step_result)
            with connection.cursor() as cursor:
                _execute(cursor, check_sql)
                actual = _dataset_from_cursor(cursor)
        finally:
            second.close()
        actual["steps"] = step_results
        problems.extend(compare_dataset(expected, actual, ordered=ordered))
    elif kind == "output":
        raw_args = case.get("args")
        arguments = (
            [str(argument) for argument in cast(list[object], raw_args)]
            if isinstance(raw_args, list)
            else [str(case["file"])]
        )
        completed = _run_pair(root, arguments, database_url)
        expected_exit = _expected_exit(case_path)
        actual = {"exit": completed.returncode, "stdout": completed.stdout.strip()}
        problems: list[str] = []
        if completed.returncode != expected_exit:
            problems.append(
                f"exit differs: expected {expected_exit}, actual {completed.returncode}"
            )
        if expected_exit == 0:
            try:
                output_value = json.loads(completed.stdout, parse_float=Decimal)
                output = _as_object(output_value, "./run output")
                problems.extend(compare_dataset(expected, output, ordered=ordered))
                actual["result"] = output
            except (json.JSONDecodeError, ValueError) as error:
                problems.append(f"./run must print a JSON result object: {error}")
                actual["stderr"] = completed.stderr.strip()
    else:
        problems = [f"unsupported case kind {kind!r}"]
        actual = {}

    return {
        "id": case_id,
        "kind": kind,
        "stage": case.get("stage"),
        "group": str(case.get("group", f"stage-{case.get('stage')}")),
        "status": "pass" if not problems else "fail",
        "expected": expected,
        "actual": actual,
        "differences": problems,
    }


def _unique_keys(
    connection: _DbConnection, table: str
) -> set[frozenset[str]]:
    with connection.cursor() as cursor:
        _execute(
            cursor,
            """SELECT array_agg(attribute.attname ORDER BY key_column.ordinality)
               FROM pg_index AS index_entry
               CROSS JOIN LATERAL unnest(index_entry.indkey) WITH ORDINALITY
                    AS key_column(attnum, ordinality)
               JOIN pg_attribute AS attribute
                 ON attribute.attrelid = index_entry.indrelid
                AND attribute.attnum = key_column.attnum
               WHERE index_entry.indrelid = to_regclass(%s)
                 AND index_entry.indisunique
                 AND index_entry.indpred IS NULL
                 AND index_entry.indexprs IS NULL
                 AND key_column.ordinality <= index_entry.indnkeyatts
               GROUP BY index_entry.indexrelid""",
            (table,),
        )
        rows = cursor.fetchall()
    return {frozenset(cast(list[str], row[0])) for row in rows}


def _check_normal_form(
    connection: _DbConnection, config: Mapping[str, object]
) -> list[str]:
    mapping = config.get("mapping")
    rules = config.get("rules")
    if not isinstance(mapping, dict) or not isinstance(rules, list):
        return ["normal_form needs mapping and rules"]
    columns = _as_object(cast(object, mapping), "normal_form mapping")
    errors: list[str] = []
    for raw_rule in cast(list[object], rules):
        rule = _as_object(raw_rule, "normal_form rule")
        rule_id = str(rule.get("id", "rule"))
        determinant_names = rule.get("determinant")
        dependent_names = rule.get("dependent")
        if not isinstance(determinant_names, list) or not isinstance(dependent_names, list):
            errors.append(f"{rule_id}: determinant and dependent must be lists")
            continue
        determinant: list[tuple[str, str]] = []
        dependents: list[tuple[str, str]] = []
        valid = True
        for source_name in cast(list[object], determinant_names):
            if not isinstance(source_name, str) or source_name not in columns:
                errors.append(f"{rule_id}: source column {source_name!r} has no mapping")
                valid = False
                continue
            target_value = columns[source_name]
            if not isinstance(target_value, dict):
                errors.append(f"{rule_id}: mapping for {source_name} must be an object")
                valid = False
                continue
            target_object = _as_object(cast(object, target_value), f"mapping for {source_name}")
            table = target_object.get("table")
            column = target_object.get("column")
            if not isinstance(table, str) or not isinstance(column, str):
                errors.append(f"{rule_id}: mapping for {source_name} needs table and column")
                valid = False
                continue
            determinant.append((table, column))
        for source_name in cast(list[object], dependent_names):
            if not isinstance(source_name, str) or source_name not in columns:
                errors.append(f"{rule_id}: source column {source_name!r} has no mapping")
                valid = False
                continue
            target_value = columns[source_name]
            if not isinstance(target_value, dict):
                errors.append(f"{rule_id}: mapping for {source_name} must be an object")
                valid = False
                continue
            target_object = _as_object(cast(object, target_value), f"mapping for {source_name}")
            table = target_object.get("table")
            column = target_object.get("column")
            if not isinstance(table, str) or not isinstance(column, str):
                errors.append(f"{rule_id}: mapping for {source_name} needs table and column")
                valid = False
                continue
            dependents.append((table, column))
        if not valid or not determinant:
            continue
        determinant_tables = {table for table, _ in determinant}
        all_tables = determinant_tables | {table for table, _ in dependents}
        if len(all_tables) != 1:
            errors.append(f"{rule_id}: determinant and dependent columns must share one table")
            continue
        determinant_columns = frozenset(column for _, column in determinant)
        if len(determinant_tables) != 1:
            errors.append(f"{rule_id}: determinant columns must be in one table")
            continue
        determinant_table = next(iter(determinant_tables))
        if determinant_columns not in _unique_keys(connection, determinant_table):
            errors.append(
                f"{rule_id}: no primary or unique key enforces the determinant in {determinant_table}"
            )
            continue
    return errors


def _check_feature(
    connection: _DbConnection, item: Mapping[str, object]
) -> list[str]:
    view = item.get("view")
    schema = item.get("schema", "public")
    features = item.get("features")
    if not isinstance(view, str) or not isinstance(schema, str) or not isinstance(features, list):
        return ["feature invariant needs a schema, view, and features list"]
    with connection.cursor() as cursor:
        _execute(
            cursor,
            """SELECT pg_get_viewdef(class_entry.oid, true)
               FROM pg_class AS class_entry
               JOIN pg_namespace AS namespace_entry ON namespace_entry.oid = class_entry.relnamespace
               WHERE namespace_entry.nspname = %s
                 AND class_entry.relname = %s
                 AND class_entry.relkind IN ('v', 'm')""",
            (schema, view),
        )
        row = cursor.fetchone()
    if row is None or not isinstance(row[0], str):
        return [f"required view {view} does not exist"]
    definition = row[0]
    errors: list[str] = []
    for feature in cast(list[object], features):
        if not isinstance(feature, str) or feature not in FEATURE_PATTERNS:
            errors.append(f"unsupported SQL feature {feature!r}")
        elif FEATURE_PATTERNS[feature].search(definition) is None:
            errors.append(f"view {view} does not show required feature {feature}")
    return errors


def _check_index(
    root: Path,
    connection: _DbConnection,
    item: Mapping[str, object],
) -> list[str]:
    file_name = item.get("file")
    index_name = item.get("index")
    if not isinstance(file_name, str) or not isinstance(index_name, str):
        return ["index invariant needs file and index"]
    query_path = root / file_name
    if not query_path.is_file() or query_path.suffix != ".sql":
        return [f"index query must be a SQL file: {file_name}"]
    query = query_path.read_text(encoding="utf-8")
    with connection.cursor() as cursor:
        try:
            _execute(cursor, "SET enable_seqscan = off")
            _execute(cursor, "EXPLAIN (COSTS OFF) " + query)
            plan = "\n".join(str(row[0]) for row in cursor.fetchall())
        finally:
            _execute(cursor, "SET enable_seqscan = on")
    if index_name not in plan:
        return [f"query plan does not use index {index_name}: {plan}"]
    return []


def _check_written(root: Path, item: Mapping[str, object]) -> list[str]:
    file_name = item.get("file")
    sections = item.get("sections")
    if not isinstance(file_name, str) or not isinstance(sections, list):
        return ["written invariant needs file and sections"]
    path = root / file_name
    if not path.is_file():
        return [f"written answer file is missing: {file_name}"]
    content = path.read_text(encoding="utf-8")
    for heading in cast(list[object], sections):
        if not isinstance(heading, str):
            return ["written section names must be strings"]
        pattern = re.compile(
            rf"^#+\s+{re.escape(heading)}\s*$([\s\S]*?)(?=^#+\s+|\Z)",
            re.MULTILINE,
        )
        match = pattern.search(content)
        if match is None:
            return [f"written answer is missing section {heading!r}"]
        body = match.group(1).strip()
        if not body or re.fullmatch(r"(?is)(?:TODO|write here|your answer here)[.\s]*", body):
            return [f"written section {heading!r} is empty or still a template"]
    return []


def _check_protected(root: Path, runner_dir: Path) -> list[str]:
    manifest_path = runner_dir / "protected.json"
    if not manifest_path.is_file():
        return ["pinned runner has no published protected-file list"]
    manifest = _as_object(_read_json(manifest_path), "protected.json")
    files = manifest.get("files")
    if not isinstance(files, dict):
        return ["pinned protected-file list is invalid"]
    protected_files = _as_object(cast(object, files), "pinned protected-file list files")
    allowed_value = manifest.get("allowed_paths", [])
    if not isinstance(allowed_value, list):
        return ["pinned protected-file allowed_paths list is invalid"]
    allowed_paths = [
        value for value in cast(list[object], allowed_value) if isinstance(value, str)
    ]
    errors: list[str] = []
    for relative, expected_hash in protected_files.items():
        if not isinstance(expected_hash, str):
            errors.append(f"protected hash for {relative} is invalid")
            continue
        path = root / relative
        if not path.is_file():
            errors.append(f"protected file is missing: {relative}")
        else:
            content = path.read_bytes().replace(b"\r\n", b"\n")
            actual_hash = hashlib.sha256(content).hexdigest()
            if actual_hash != expected_hash:
                errors.append(f"protected file changed: {relative}")
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if status.returncode != 0:
        errors.append("Git status could not check new or changed files")
    else:
        for line in status.stdout.splitlines():
            if len(line) < 4:
                continue
            relative = line[3:].replace("\\", "/").strip('"')
            if relative in protected_files:
                continue
            if any(
                relative == allowed.rstrip("/")
                or relative.startswith(allowed.rstrip("/") + "/")
                for allowed in allowed_paths
            ):
                continue
            errors.append(f"file is outside the published writable paths: {relative}")
    return errors


def _schema_snapshot(connection: _DbConnection) -> dict[str, list[tuple[object, ...]]]:
    with connection.cursor() as cursor:
        _execute(
            cursor,
            """SELECT table_name FROM information_schema.tables
               WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
               ORDER BY table_name"""
        )
        tables = [str(row[0]) for row in cursor.fetchall()]
    snapshot: dict[str, list[tuple[object, ...]]] = {}
    for table in tables:
        with connection.cursor() as cursor:
            _execute(cursor, f"SELECT * FROM {_quote_identifier(table)}")
            snapshot[table] = sorted(cursor.fetchall(), key=repr)
    return snapshot


def _backup_restore(
    root: Path,
    connection: _DbConnection,
    database_url: str,
) -> list[str]:
    before = _schema_snapshot(connection)
    with tempfile.TemporaryDirectory(prefix="cmsc127-backup-") as temp_dir:
        backup_path = Path(temp_dir) / "backup.dump"
        environment = os.environ.copy()
        environment["CMSC127_DATABASE_URL"] = database_url
        environment["CMSC127_BACKUP_FILE"] = str(backup_path)
        for command in (["backup"], ["restore"]):
            completed = subprocess.run(
                [_bash_executable(), "./run", *command],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=120.0,
            )
            if completed.returncode != 0:
                return [
                    f"./run {command[0]} failed with {completed.returncode}: "
                    f"{completed.stderr.strip()}"
                ]
            if command[0] == "backup":
                if not backup_path.is_file():
                    return ["./run backup did not create CMSC127_BACKUP_FILE"]
                with connection.cursor() as cursor:
                    _execute(
                        cursor,
                        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'",
                    )
                    tables = [str(row[0]) for row in cursor.fetchall()]
                if tables:
                    quoted_tables = ", ".join(
                        _quote_identifier("public") + "." + _quote_identifier(name)
                        for name in tables
                    )
                    with connection.cursor() as cursor:
                        _execute(cursor, "TRUNCATE " + quoted_tables + " CASCADE")
        after = _schema_snapshot(connection)
    return [] if before == after else ["restored rows differ from the pre-backup rows"]


def _contribution_table(root: Path) -> str:
    completed = subprocess.run(
        ["git", "log", "--since=12 weeks ago", "--date=short", "--pretty=format:%ad%x09%an%x09%ae", "--numstat"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        return "Contribution table: SKIP (Git history is not available)."
    totals: Counter[tuple[str, str]] = Counter()
    current_week = ""
    current_author = ""
    for line in completed.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) == 3 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", fields[0]):
            parsed = date.fromisoformat(fields[0])
            current_week = parsed.strftime("%G-W%V")
            current_author = fields[1]
        elif len(fields) == 3 and current_week and fields[0].isdigit() and fields[1].isdigit():
            totals[(current_week, current_author)] += int(fields[0]) + int(fields[1])
    rows = ["Contribution by week and Git author (changed lines, not a score):"]
    rows.extend(
        f"{week}\t{author}\t{lines}"
        for (week, author), lines in sorted(totals.items())
    )
    return "\n".join(rows)


def _run_invariants(
    root: Path,
    manifest: Mapping[str, object],
    connection: _DbConnection,
    database_url: str,
    runner_dir: Path,
    stage: int,
) -> list[dict[str, object]]:
    raw_invariants = manifest.get("invariants", [])
    if not isinstance(raw_invariants, list):
        raise ValueError("cases/manifest.json: invariants must be a list")
    results: list[dict[str, object]] = []
    for index, raw_item in enumerate(cast(list[object], raw_invariants), start=1):
        item = _as_object(raw_item, f"invariant {index}")
        invariant_stage = item.get("stage", 1)
        if not isinstance(invariant_stage, int) or invariant_stage > stage:
            continue
        invariant_id = str(item.get("id", f"invariant-{index}"))
        kind = item.get("kind")
        try:
            if kind == "work_sql":
                files = item.get("files")
                problems: list[str] = []
                if not isinstance(files, list) or not files:
                    problems.append("work_sql needs a non-empty files list")
                else:
                    for file_name in cast(list[object], files):
                        if not isinstance(file_name, str) or not file_name.endswith(".sql"):
                            problems.append(f"report must be a SQL file: {file_name!r}")
                        elif not (root / file_name).is_file():
                            problems.append(f"report SQL file is missing: {file_name}")
                        else:
                            report_path = root / file_name
                            expected = _read_expected(report_path)
                            actual = _run_sql_file(connection, report_path)
                            problems.extend(
                                compare_dataset(
                                    expected,
                                    actual,
                                    ordered=item.get("ordered", True) is not False,
                                )
                            )
            elif kind == "normal_form":
                problems = _check_normal_form(connection, item)
            elif kind == "feature":
                problems = _check_feature(connection, item)
            elif kind == "index":
                problems = _check_index(root, connection, item)
            elif kind == "backup_restore":
                problems = _backup_restore(root, connection, database_url)
            elif kind == "protected":
                problems = _check_protected(root, runner_dir)
            elif kind == "written":
                problems = _check_written(root, item)
            elif kind == "contribution":
                print(_contribution_table(root))
                problems = []
            else:
                problems = [f"unsupported invariant kind {kind!r}"]
        except (OSError, ValueError, psycopg.Error, subprocess.SubprocessError) as error:
            problems = [str(error)]
        results.append(
            {
                "id": invariant_id,
                "kind": str(kind),
                "stage": invariant_stage,
                "group": str(item.get("group", "invariants")),
                "status": "pass" if not problems else "fail",
                "expected": item,
                "actual": {"differences": problems},
                "differences": problems,
            }
        )
    return results


def _save_result(root: Path, result: Mapping[str, object]) -> Path:
    result_path = root / ".cmsc127" / "last-run.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(_json_text(result) + "\n", encoding="utf-8")
    return result_path


def _print_results(results: Sequence[Mapping[str, object]], stage: int) -> int:
    counts: Counter[str] = Counter()
    stage_counts: dict[int, Counter[str]] = {}
    groups: dict[str, Counter[str]] = {}
    for result in results:
        status = str(result.get("status"))
        if status == "skip":
            print(f"SKIP {result.get('id')}: {result.get('reason', 'platform limit')}")
            continue
        counts[status] += 1
        group = str(result.get("group", "checks"))
        groups.setdefault(group, Counter())[status] += 1
        case_stage = result.get("stage")
        if isinstance(case_stage, int):
            stage_counts.setdefault(case_stage, Counter())[status] += 1
        state = "PASS" if status == "pass" else "FAIL"
        print(f"{state} {result.get('id')}")
        if status == "fail":
            for difference in cast(list[object], result.get("differences", [])):
                print(f"  {difference}")
            print(f"  expected: {_json_text(result.get('expected'))}")
            print(f"  actual:   {_json_text(result.get('actual'))}")
    for group, tally in sorted(groups.items()):
        print(f"Group {group}: {tally['pass']} passed, {tally['fail']} failed")
    total = counts["pass"] + counts["fail"]
    print("== result ==")
    print(
        f"{counts['pass']}/{total} checks passed, "
        f"{counts['fail']} failed, {sum(1 for item in results if item.get('status') == 'skip')} skipped"
    )
    for stage_number in sorted(stage_counts):
        tally = stage_counts[stage_number]
        denominator = tally["pass"] + tally["fail"]
        print(f"Stage {stage_number}: {tally['pass']}/{denominator} passed")
    if not stage_counts:
        print(f"Stage {stage}: 0/0 passed")
    return 0 if counts["fail"] == 0 and total > 0 else 1


def _self_test() -> int:
    base_url = os.environ.get("CMSC127_DATABASE_URL")
    if not base_url:
        print("CMSC127_DATABASE_URL is not set. Set it to the local PostgreSQL connection URL.")
        return 2
    name = ""
    admin: _DbConnection | None = None
    try:
        name, admin = _create_database(base_url)
        test_url = _database_url(base_url, name)
        settings = conninfo_to_dict(test_url)
        environment = os.environ.copy()
        environment["CMSC127_DATABASE_URL"] = test_url
        environment["PGHOST"] = str(settings.get("host", "127.0.0.1"))
        environment["PGPORT"] = str(settings.get("port", "5432"))
        environment["PGUSER"] = str(settings.get("user", "postgres"))
        environment["PGPASSWORD"] = str(settings.get("password", ""))
        environment["PGDATABASE"] = name
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "--start-directory",
                "tests",
                "--top-level-directory",
                ".",
                "--verbose",
            ],
            cwd=Path(__file__).resolve().parent,
            env=environment,
            check=False,
        )
        return completed.returncode
    except (OSError, psycopg.Error) as error:
        print(f"Self-test setup failed: {error}")
        return 1
    finally:
        if admin is not None and name:
            _drop_database(name, admin)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", nargs="?", type=int, default=1)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--self-test", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.self_test:
        return _self_test()
    if arguments.stage < 1:
        parser.error("stage must be a positive integer")
    root = arguments.root.resolve()
    try:
        manifest = load_manifest(root)
    except ValueError as error:
        print("== form fault ==")
        print(str(error))
        return 2
    database_url = os.environ.get("CMSC127_DATABASE_URL")
    if not database_url:
        print("CMSC127_DATABASE_URL is not set. Set it to the local PostgreSQL connection URL.")
        return 2

    selected = select_cases(manifest, arguments.stage)
    if not selected:
        print(f"No cases are listed for stage {arguments.stage} or an earlier stage.")
        return 1
    database_name = ""
    admin: _DbConnection | None = None
    results: list[dict[str, object]] = []
    try:
        database_name, admin = _create_database(database_url)
        scratch_url = _database_url(database_url, database_name)
        connection = cast(_DbConnection, psycopg.connect(scratch_url, autocommit=True))
        try:
            _apply_migrations(root, connection)
            setup = _run_pair(root, ["prepare"], scratch_url)
            if setup.returncode != 0:
                results.append(
                    {
                        "id": "prepare",
                        "kind": "setup",
                        "stage": None,
                        "group": "setup",
                        "status": "fail",
                        "expected": "./run prepare exits 0",
                        "actual": {"exit": setup.returncode, "stderr": setup.stderr.strip()},
                        "differences": ["./run prepare failed"],
                    }
                )
            else:
                for case in selected:
                    case_id = str(case["id"])
                    try:
                        case_result = _run_case(root, case, connection, scratch_url)
                    except (OSError, ValueError, psycopg.Error, subprocess.SubprocessError) as error:
                        case_result = {
                            "id": case_id,
                            "kind": case.get("kind"),
                            "stage": case.get("stage"),
                            "group": str(case.get("group", f"stage-{case.get('stage')}")),
                            "status": "fail",
                            "expected": "case completes without a runner error",
                            "actual": str(error),
                            "differences": [str(error)],
                        }
                    results.append(case_result)
                results.extend(
                    _run_invariants(
                        root,
                        manifest,
                        connection,
                        scratch_url,
                        Path(__file__).resolve().parent,
                        arguments.stage,
                    )
                )
        finally:
            connection.close()
    except (OSError, ValueError, psycopg.Error) as error:
        print(f"Runner setup failed: {error}")
        return 1
    finally:
        if admin is not None and database_name:
            _drop_database(database_name, admin)
    skipped = manifest.get("skips", [])
    if isinstance(skipped, list):
        for index, raw_skip in enumerate(cast(list[object], skipped), start=1):
            if isinstance(raw_skip, dict):
                skip = cast(dict[str, object], raw_skip)
                if skip.get("stage", 1) <= arguments.stage:
                    results.append(
                        {
                            "id": str(skip.get("id", f"skip-{index}")),
                            "kind": "skip",
                            "stage": skip.get("stage", 1),
                            "group": str(skip.get("group", "skipped")),
                            "status": "skip",
                            "reason": str(skip.get("reason", "not available on this platform")),
                            "expected": None,
                            "actual": None,
                            "differences": [],
                        }
                    )
    result_document: dict[str, object] = {
        "version": 1,
        "stage": arguments.stage,
        "results": results,
        "passed": sum(item.get("status") == "pass" for item in results),
        "failed": sum(item.get("status") == "fail" for item in results),
        "skipped": sum(item.get("status") == "skip" for item in results),
    }
    result_path = _save_result(root, result_document)
    print(f"Result file: {result_path.relative_to(root).as_posix()}")
    return _print_results(results, arguments.stage)


if __name__ == "__main__":
    raise SystemExit(main())
