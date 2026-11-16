"""Run staged CMSC 127 cases in a disposable PostgreSQL database."""

from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
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


CASE_KINDS = {
    "output",
    "schema",
    "reject",
    "state",
    "race",
    "query",
    "document",
    "report",
    "view",
}
WARNING_LABEL = "warning:"
INVARIANT_KINDS = {
    "work_sql",
    "normal_form",
    "feature",
    "index",
    "backup_restore",
    "protected",
    "written",
    "contribution",
    "pyright",
}
CASE_SUFFIXES = {".json", ".md", ".erd", ".sql", ".sh"}
MIGRATION_NAME = re.compile(r"(?P<number>[0-9]{4})_[a-z0-9_]+\.sql\Z")
ROLE_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}\Z")
FEATURE_PATTERNS = {
    "left_join": re.compile(
        r"\b(?:LEFT|RIGHT|FULL)(?:\s+OUTER)?\s+JOIN\b", re.IGNORECASE
    ),
    "right_join": re.compile(r"\bRIGHT(?:\s+OUTER)?\s+JOIN\b", re.IGNORECASE),
    "full_join": re.compile(r"\bFULL(?:\s+OUTER)?\s+JOIN\b", re.IGNORECASE),
    "union": re.compile(r"\bUNION(?:\s+ALL)?\b", re.IGNORECASE),
    "intersect": re.compile(r"\bINTERSECT\b", re.IGNORECASE),
    "except": re.compile(r"\bEXCEPT\b", re.IGNORECASE),
    "self_join": re.compile(
        r"\bFROM\s+(?P<table>(?:[A-Za-z_][A-Za-z0-9_]*\.)?"
        r"[A-Za-z_][A-Za-z0-9_]*)"
        r"\s+(?:AS\s+)?(?P<first>[A-Za-z_][A-Za-z0-9_]*)\b"
        r"[\s\S]*?(?:\bJOIN\s+(?P=table)|,\s*(?P=table))"
        r"\s+(?:AS\s+)?(?P<second>[A-Za-z_][A-Za-z0-9_]*)\b",
        re.IGNORECASE,
    ),
    "having": re.compile(r"\bHAVING\b", re.IGNORECASE),
    "where_subquery": re.compile(
        r"\bWHERE\b[\s\S]*?\(\s*SELECT\b", re.IGNORECASE
    ),
    "correlated_subquery": re.compile(
        r"\(\s*SELECT\b[\s\S]*?\bWHERE\b[\s\S]*?"
        r"\b[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*\s*=\s*"
        r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*",
        re.IGNORECASE,
    ),
    "coalesce": re.compile(
        r"\b(?:COALESCE|NULLIF)\s*\(|\bIS\s+(?:NOT\s+)?NULL\b",
        re.IGNORECASE,
    ),
}
FEATURE_LABELS = {
    "left_join": "outer join",
    "right_join": "outer join",
    "full_join": "outer join",
    "coalesce": "NULL handling",
    "self_join": "self join",
    "union": "set operation",
    "intersect": "set operation",
    "except": "set operation",
    "having": "HAVING clause",
    "where_subquery": "subquery in WHERE",
    "correlated_subquery": "correlated subquery",
}
_DOLLAR_QUOTE = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")


def _strip_sql_comments_and_strings(text: str, strip_identifiers: bool = False) -> str:
    """Replace SQL comments and literals with spaces before text checks."""
    output = list(text)

    def blank(start: int, end: int) -> None:
        for position in range(start, end):
            if output[position] not in "\r\n":
                output[position] = " "

    index = 0
    while index < len(text):
        if text.startswith("--", index):
            end = text.find("\n", index)
            if end < 0:
                end = len(text)
            blank(index, end)
            index = end
            continue
        if text.startswith("/*", index):
            start = index
            depth = 1
            index += 2
            while index < len(text) and depth:
                if text.startswith("/*", index):
                    depth += 1
                    index += 2
                elif text.startswith("*/", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            blank(start, index)
            continue
        if text[index] == "'":
            escaped = (
                index > 0
                and text[index - 1] in "eE"
                and (index == 1 or not (text[index - 2].isalnum() or text[index - 2] == "_"))
            )
            start = index - 1 if escaped else index
            index += 1
            while index < len(text):
                if escaped and text[index] == "\\":
                    index += 2
                elif text[index] == "'":
                    if index + 1 < len(text) and text[index + 1] == "'":
                        index += 2
                    else:
                        index += 1
                        break
                else:
                    index += 1
            blank(start, index)
            continue
        if strip_identifiers and text[index] == '"':
            start = index
            index += 1
            while index < len(text):
                if text[index] == '"':
                    if index + 1 < len(text) and text[index + 1] == '"':
                        index += 2
                    else:
                        index += 1
                        break
                else:
                    index += 1
            blank(start, index)
            continue
        if text[index] == "$":
            delimiter = _DOLLAR_QUOTE.match(text, index)
            if delimiter is not None:
                closing = text.find(delimiter.group(), delimiter.end())
                end = len(text) if closing < 0 else closing + len(delimiter.group())
                blank(index, end)
                index = end
                continue
        index += 1
    return "".join(output)


def _sql_statement_count(text: str) -> int:
    code = _strip_sql_comments_and_strings(text, strip_identifiers=True)
    return sum(bool(statement.strip()) for statement in code.split(";"))


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

    def transaction(
        self: _DbConnection, *, force_rollback: bool = False
    ) -> Any: ...

    def close(self: _DbConnection) -> None: ...


class FormFault(ValueError):
    """A form fault with a source location."""

    def __init__(
        self,
        path: Path,
        line: int,
        column: int,
        message: str,
        *,
        root: Path | None = None,
    ) -> None:
        self.path = path
        self.line = line
        self.column = column
        self.message = message
        display = path.name
        if root is not None:
            try:
                display = path.relative_to(root).as_posix()
            except ValueError:
                display = path.name
        super().__init__(f"{display}: line {line}, column {column}: {message}")


def _as_object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    candidate = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in candidate):
        raise ValueError(f"{label} must use string keys")
    return {str(key): item for key, item in candidate.items()}


def _read_json(path: Path) -> object:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise FormFault(path, 1, 1, "file is not UTF-8 text") from error
    try:
        return json.loads(text, parse_float=Decimal)
    except json.JSONDecodeError as error:
        raise FormFault(path, error.lineno, error.colno, error.msg) from error


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


def _json_fault(
    path: Path,
    text: str,
    location: tuple[str | int, ...],
    message: str,
    *,
    root: Path | None = None,
) -> FormFault:
    try:
        locations = _json_locations(text)
    except (ValueError, IndexError):
        locations = {}
    line, column = locations.get(location, (1, 1))
    return FormFault(path, line, column, message, root=root)


def _validate_dataset(
    value: Mapping[str, object],
    fault: Callable[[str, tuple[str | int, ...]], FormFault],
    base: tuple[str | int, ...],
) -> None:
    columns = value.get("columns")
    if not isinstance(columns, list):
        raise fault("columns must be a list", (*base, "columns"))
    column_list = cast(list[object], columns)
    for column_index, name in enumerate(column_list):
        if not isinstance(name, str):
            raise fault(
                f"column {column_index + 1} must be a string",
                (*base, "columns", column_index),
            )
    rows = value.get("rows")
    if not isinstance(rows, list):
        raise fault("rows must be a list", (*base, "rows"))
    for row_index, row in enumerate(cast(list[object], rows)):
        if not isinstance(row, list):
            raise fault(
                f"row {row_index + 1} must be a list",
                (*base, "rows", row_index),
            )
        row_values = cast(list[object], row)
        if len(row_values) != len(column_list):
            raise fault(
                f"row {row_index + 1} must have {len(column_list)} values",
                (*base, "rows", row_index),
            )
        for cell in row_values:
            if isinstance(cell, (dict, list)):
                raise fault(
                    f"row {row_index + 1} must hold JSON scalars",
                    (*base, "rows", row_index),
                )


def _validate_expected(path: Path, expected: Mapping[str, object], kind: str) -> None:
    text = path.read_text(encoding="utf-8")

    def fault(message: str, location: tuple[str | int, ...]) -> FormFault:
        return _json_fault(path, text, location, message)

    if kind == "reject":
        sqlstate = expected.get("sqlstate")
        if sqlstate is not None and not isinstance(sqlstate, str):
            raise fault("sqlstate must be a string", ("sqlstate",))
        return
    _validate_dataset(expected, fault, ())


_REJECT_LITERAL_WORDS = frozenset({"NULL", "TRUE", "FALSE"})

# Longest type name a typed literal may carry: TIMESTAMP WITH TIME ZONE.
_REJECT_TYPE_WORDS = 4


def _reject_tokens(sql: str) -> list[tuple[str, str]] | None:
    """Split a reject file into tokens, or None when it holds a body.

    Comments are dropped. A quoted string becomes a ``string`` token and a
    quoted identifier becomes an ``ident`` token. A dollar-quoted body, a
    non-ASCII character, or an unterminated quote returns None.
    """
    tokens: list[tuple[str, str]] = []
    index = 0
    length = len(sql)
    while index < length:
        character = sql[index]
        if character.isspace():
            index += 1
            continue
        if sql.startswith("--", index):
            end = sql.find("\n", index)
            index = length if end < 0 else end + 1
            continue
        if sql.startswith("/*", index):
            depth = 1
            index += 2
            while index < length and depth:
                if sql.startswith("/*", index):
                    depth += 1
                    index += 2
                elif sql.startswith("*/", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            if depth:
                return None
            continue
        if character in "'\"":
            quote = character
            start = index
            escaped = quote == "'" and index > 0 and sql[index - 1] in "Ee"
            index += 1
            while index < length:
                if escaped and sql[index] == "\\":
                    index += 2
                elif sql[index] == quote:
                    if index + 1 < length and sql[index + 1] == quote:
                        index += 2
                    else:
                        index += 1
                        break
                else:
                    index += 1
            else:
                return None
            kind = "string" if quote == "'" else "ident"
            tokens.append((kind, sql[start:index]))
            continue
        if character == "$":
            return None
        if character.isdigit() or (
            character == "." and index + 1 < length and sql[index + 1].isdigit()
        ):
            number = re.match(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", sql[index:])
            if number is None:
                return None
            tokens.append(("number", number.group()))
            index += len(number.group())
            continue
        word = re.match(r"[A-Za-z_][A-Za-z_0-9$]*", sql[index:])
        if word is not None:
            tokens.append(("word", word.group()))
            index += len(word.group())
            continue
        if character in "(),;.-+":
            tokens.append(("punct", character))
            index += 1
            continue
        return None
    return tokens


def _reject_insert_target(sql: str) -> str | None:
    """The target table when the file is one plain INSERT ... VALUES row.

    The accepted shape is ``INSERT INTO <table> [(<columns>)] VALUES (<row>)``
    with one row of literal values: numbers, quoted strings, ``NULL``,
    ``TRUE``, ``FALSE``, and a typed literal such as ``DATE '2024-08-01'``.
    A ``WITH``, a ``SELECT``, a function call, a subquery, or a second row
    returns None, so the case fails its form check.
    """
    tokens = _reject_tokens(sql)
    if tokens is None:
        return None
    position = 0

    def take(kind: str, value: str | None = None) -> tuple[str, str] | None:
        nonlocal position
        if position >= len(tokens):
            return None
        token = tokens[position]
        if token[0] != kind:
            return None
        if value is not None and token[1].upper() != value:
            return None
        position += 1
        return token

    def identifier() -> str | None:
        token = take("word")
        if token is None:
            token = take("ident")
        return None if token is None else token[1]

    def literal() -> bool:
        nonlocal position
        start = position
        if position < len(tokens) and tokens[position][0] == "punct":
            if tokens[position][1] in ("+", "-"):
                if position + 1 < len(tokens) and tokens[position + 1][0] == "number":
                    position += 2
                    return True
                return False
        if position < len(tokens) and tokens[position][0] in ("number", "string"):
            position += 1
            return True
        if position < len(tokens) and tokens[position][0] == "word":
            if tokens[position][1].upper() in _REJECT_LITERAL_WORDS:
                position += 1
                return True
            words = 0
            while (
                position < len(tokens)
                and tokens[position][0] == "word"
                and words < _REJECT_TYPE_WORDS
            ):
                position += 1
                words += 1
            if position < len(tokens) and tokens[position][0] == "string":
                position += 1
                return True
        position = start
        return False

    if take("word", "INSERT") is None or take("word", "INTO") is None:
        return None
    table = identifier()
    if table is None:
        return None
    if take("punct", ".") is not None:
        qualifier = identifier()
        if qualifier is None:
            return None
        table = f"{table}.{qualifier}"
    if take("punct", "(") is not None:
        if identifier() is None:
            return None
        while take("punct", ",") is not None:
            if identifier() is None:
                return None
        if take("punct", ")") is None:
            return None
    if take("word", "VALUES") is None:
        return None
    if take("punct", "(") is None or not literal():
        return None
    while take("punct", ",") is not None:
        if not literal():
            return None
    if take("punct", ")") is None:
        return None
    if take("punct", ";") is not None and position != len(tokens):
        return None
    if position != len(tokens):
        return None
    return table


def _is_single_insert_statement(sql: str) -> bool:
    return _reject_insert_target(sql) is not None


def _validate_case_body(path: Path, kind: str, case_id: str) -> None:
    text = path.read_text(encoding="utf-8")
    try:
        body = _as_object(_read_json(path), f"case {case_id}")
    except ValueError as error:
        raise _json_fault(path, text, (), str(error)) from error

    def fault(message: str, location: tuple[str | int, ...]) -> FormFault:
        return _json_fault(path, text, location, message)

    if kind == "state":
        if not isinstance(body.get("operation"), str):
            raise fault("operation must be a string", ("operation",))
        if not isinstance(body.get("check_sql"), str):
            raise fault("check_sql must be a string", ("check_sql",))
        return
    if kind == "document":
        if not isinstance(body.get("file"), str):
            raise fault("file must be a string", ("file",))
        headings = body.get("headings")
        if not isinstance(headings, list) or not all(
            isinstance(heading, str) for heading in cast(list[object], headings)
        ):
            raise fault("headings must be a list of strings", ("headings",))
        placeholders = body.get("placeholders")
        if not isinstance(placeholders, dict) or not all(
            isinstance(value, str)
            for value in cast(dict[object, object], placeholders).values()
        ):
            raise fault("placeholders must be an object of strings", ("placeholders",))
        warning_sql = body.get("warning_sql")
        if warning_sql is not None and not isinstance(warning_sql, str):
            raise fault("warning_sql must be a string", ("warning_sql",))
        return
    if kind in {"report", "view"}:
        if not isinstance(body.get("report"), str):
            raise fault("report must be a string", ("report",))
        if kind == "view" and not isinstance(body.get("view"), str):
            raise fault("view must be a string", ("view",))
        return
    steps = body.get("steps")
    if not isinstance(steps, list):
        raise fault("steps must be a list", ("steps",))
    for index, raw_step in enumerate(cast(list[object], steps)):
        try:
            step = _as_object(raw_step, "step")
        except ValueError as error:
            raise fault(str(error), ("steps", index)) from error
        session = step.get("session")
        if not isinstance(session, str) or session not in {"a", "b"}:
            raise fault("session must be a or b", ("steps", index, "session"))
        if not isinstance(step.get("sql"), str):
            raise fault("sql must be a string", ("steps", index, "sql"))
        expected = step.get("expected")
        if expected is not None:
            try:
                expected_object = _as_object(expected, "expected")
            except ValueError as error:
                raise fault(str(error), ("steps", index, "expected")) from error
            _validate_dataset(expected_object, fault, ("steps", index, "expected"))
    if not isinstance(body.get("check_sql"), str):
        raise fault("check_sql must be a string", ("check_sql",))


def _validate_invariant_fields(
    invariant: Mapping[str, object],
    kind: str,
    index: int,
    fault: Callable[[str, tuple[str | int, ...]], FormFault],
    location: tuple[str | int, ...],
) -> None:
    def require(name: str, expected_type: type[object], description: str) -> object:
        value = invariant.get(name)
        if not isinstance(value, expected_type):
            raise fault(
                f"invariant {index}: {kind} needs a {description}",
                (*location, name),
            )
        return value

    if kind == "work_sql":
        files = require("files", list, "files list")
        file_list = cast(list[object], files)
        if not file_list:
            raise fault(
                f"invariant {index}: work_sql needs a non-empty files list",
                (*location, "files"),
            )
        for file_index, file_name in enumerate(file_list):
            if not isinstance(file_name, str) or not file_name.endswith(".sql"):
                raise fault(
                    f"invariant {index}: work_sql needs SQL files",
                    (*location, "files", file_index),
                )
    elif kind == "normal_form":
        require("mapping", dict, "mapping object")
        rules = require("rules", list, "rules list")
        for rule_index, raw_rule in enumerate(cast(list[object], rules)):
            try:
                rule = _as_object(raw_rule, "rule")
            except ValueError as error:
                raise fault(str(error), (*location, "rules", rule_index)) from error
            for field in ("determinant", "dependent"):
                if not isinstance(rule.get(field), list):
                    raise fault(
                        f"invariant {index}: rule {rule_index + 1} needs a {field} list",
                        (*location, "rules", rule_index, field),
                    )
    elif kind == "feature":
        features = require("features", list, "features list")
        for feature_index, feature in enumerate(cast(list[object], features)):
            if not isinstance(feature, str) or feature not in FEATURE_PATTERNS:
                raise fault(
                    f"invariant {index}: unsupported SQL feature {feature!r}",
                    (*location, "features", feature_index),
                )
        file_name = invariant.get("file")
        if file_name is not None:
            if not isinstance(file_name, str):
                raise fault(
                    f"invariant {index}: feature file must be a string",
                    (*location, "file"),
                )
        else:
            require("schema", str, "schema string")
            require("view", str, "view string")
    elif kind == "index":
        require("file", str, "file string")
        require("index", str, "index string")
        key_column = invariant.get("key_column")
        if key_column is not None and not isinstance(key_column, str):
            raise fault(
                f"invariant {index}: key_column must be a string",
                (*location, "key_column"),
            )
    elif kind == "written":
        require("file", str, "file string")
        sections = require("sections", list, "sections list")
        for section_index, section in enumerate(cast(list[object], sections)):
            if not isinstance(section, str):
                raise fault(
                    f"invariant {index}: section names must be strings",
                    (*location, "sections", section_index),
                )


def _validate_case(
    root: Path,
    case_root: Path,
    raw_case: object,
    index: int,
    fault: Callable[[str, tuple[str | int, ...]], FormFault],
    identifiers: dict[str, str],
    listed: set[Path],
    source_manifest: str,
    *,
    pair: bool = False,
) -> dict[str, object]:
    location = ("cases", index - 1)
    try:
        case = _as_object(raw_case, f"case {index}")
    except ValueError as error:
        raise fault(str(error), location) from error
    case_id = case.get("id")
    if not isinstance(case_id, str) or not case_id:
        raise fault(f"case {index}: id must be a non-empty string", (*location, "id"))
    replacement = case.get("replaces")
    if replacement is not None:
        if pair or not isinstance(replacement, str) or not replacement:
            raise fault(
                f"case {case_id}: replaces must name one published case",
                (*location, "replaces"),
            )
    existing_manifest = identifiers.get(case_id)
    if existing_manifest is not None:
        raise fault(
            f"case {case_id}: duplicate id, already defined in {existing_manifest}",
            (*location, "id"),
        )
    identifiers[case_id] = source_manifest
    stage = case.get("stage")
    if not isinstance(stage, int) or isinstance(stage, bool) or stage < 1:
        raise fault(
            f"case {case_id}: stage must be a positive integer",
            (*location, "stage"),
        )
    kind = case.get("kind")
    if not isinstance(kind, str) or kind not in CASE_KINDS:
        raise fault(f"case {case_id}: unsupported kind {kind!r}", (*location, "kind"))
    relative_file = case.get("file")
    if not isinstance(relative_file, str) or not relative_file:
        raise fault(
            f"case {case_id}: file must be a relative path",
            (*location, "file"),
        )
    case_path = (root / relative_file).resolve()
    try:
        case_path.relative_to(case_root.resolve())
    except ValueError as error:
        raise fault(
            f"case {case_id}: file must be inside cases/",
            (*location, "file"),
        ) from error
    if pair:
        try:
            case_path.relative_to((case_root / "pair").resolve())
        except ValueError as error:
            raise fault(
                f"case {case_id}: file must be inside cases/pair/",
                (*location, "file"),
            ) from error
    if not case_path.is_file():
        raise fault(
            f"case {case_id}: case file is missing: {relative_file}",
            (*location, "file"),
        )
    allowed_suffixes = {
        "output": {".json", ".sql", ".sh"},
        "schema": {".erd", ".md"},
        "reject": {".sql"},
        "state": {".json"},
        "race": {".json"},
        "query": {".sql"},
        "document": {".json"},
        "report": {".json"},
        "view": {".json"},
    }
    if case_path.suffix not in allowed_suffixes[str(kind)]:
        raise fault(
            f"case {case_id}: {kind} needs a supported case-file type",
            (*location, "file"),
        )
    alternate_file = case.get("alternate_file")
    alternate_case_path: Path | None = None
    if alternate_file is not None:
        if not isinstance(alternate_file, str) or not alternate_file:
            raise fault(
                f"case {case_id}: alternate_file must be a relative path",
                (*location, "alternate_file"),
            )
        alternate_case_path = (root / alternate_file).resolve()
        try:
            alternate_case_path.relative_to(case_root.resolve())
        except ValueError as error:
            raise fault(
                f"case {case_id}: alternate_file must be inside cases/",
                (*location, "alternate_file"),
            ) from error
        if alternate_case_path.suffix not in allowed_suffixes[str(kind)]:
            raise fault(
                f"case {case_id}: alternate_file has an unsupported type for {kind}",
                (*location, "alternate_file"),
            )
        if not alternate_case_path.is_file():
            raise fault(
                f"case {case_id}: alternate case file is missing: {alternate_file}",
                (*location, "alternate_file"),
            )
        listed.add(alternate_case_path)
    if kind == "reject" and not _is_single_insert_statement(
        case_path.read_text(encoding="utf-8")
    ):
        raise FormFault(
            case_path,
            1,
            1,
            "a reject case must be one plain `INSERT ... VALUES` row of "
            "literals: no `WITH`, `SELECT`, function call, cast, subquery, or "
            "second row",
            root=root,
        )
    if kind == "reject" and alternate_case_path is not None and not _is_single_insert_statement(
        alternate_case_path.read_text(encoding="utf-8")
    ):
        raise FormFault(
            alternate_case_path,
            1,
            1,
            "a reject case must be one plain `INSERT ... VALUES` row of "
            "literals: no `WITH`, `SELECT`, function call, cast, subquery, or "
            "second row",
            root=root,
        )
    listed.add(case_path)
    expected_file = case.get("expected_file")
    if expected_file is None:
        expected_path = case_path.with_suffix(".expected")
    else:
        if not isinstance(expected_file, str) or not expected_file:
            raise fault(
                f"case {case_id}: expected_file must be a relative path",
                (*location, "expected_file"),
            )
        expected_path = (root / expected_file).resolve()
        try:
            expected_path.relative_to(case_root.resolve())
        except ValueError as error:
            raise fault(
                f"case {case_id}: expected_file must be inside cases/",
                (*location, "expected_file"),
            ) from error
        if expected_path.suffix != ".expected":
            raise fault(
                f"case {case_id}: expected_file must end in .expected",
                (*location, "expected_file"),
            )
    listed.add(expected_path)
    if not expected_path.is_file():
        raise fault(
            f"case {case_id}: .expected sidecar is missing",
            (*location, "file"),
        )
    expected = _as_object(_read_json(expected_path), f"case {case_id} .expected")
    _validate_expected(expected_path, expected, str(kind))
    alternate_expected_file = case.get("alternate_expected_file")
    alternate_expected_path: Path | None = None
    if alternate_expected_file is not None:
        if not isinstance(alternate_expected_file, str) or not alternate_expected_file:
            raise fault(
                f"case {case_id}: alternate_expected_file must be a relative path",
                (*location, "alternate_expected_file"),
            )
        alternate_expected_path = (root / alternate_expected_file).resolve()
        try:
            alternate_expected_path.relative_to(case_root.resolve())
        except ValueError as error:
            raise fault(
                f"case {case_id}: alternate_expected_file must be inside cases/",
                (*location, "alternate_expected_file"),
            ) from error
        if alternate_expected_path.suffix != ".expected":
            raise fault(
                f"case {case_id}: alternate_expected_file must end in .expected",
                (*location, "alternate_expected_file"),
            )
        listed.add(alternate_expected_path)
        if not alternate_expected_path.is_file():
            raise fault(
                f"case {case_id}: alternate .expected sidecar is missing",
                (*location, "alternate_expected_file"),
            )
        alternate_expected = _as_object(
            _read_json(alternate_expected_path),
            f"case {case_id} alternate .expected",
        )
        _validate_expected(alternate_expected_path, alternate_expected, str(kind))
    if "role" in case or "role_env" in case:
        role = case.get("role")
        if kind != "reject":
            raise fault(f"case {case_id}: role is only valid for a reject case", (*location, "role"))
        if not isinstance(role, str) or ROLE_NAME.fullmatch(role) is None:
            raise fault(f"case {case_id}: role must be a lowercase PostgreSQL role name", (*location, "role"))
        role_env = case.get("role_env")
        if role_env is not None and (
            not isinstance(role_env, str)
            or re.fullmatch(r"[A-Z_][A-Z0-9_]*", role_env) is None
        ):
            raise fault(
                f"case {case_id}: role_env must be an environment variable name",
                (*location, "role_env"),
            )
        if expected.get("sqlstate") != "42501":
            raise fault(f"case {case_id}: role reject cases must expect SQLSTATE 42501", (*location, "role"))
    exit_path = case_path.with_suffix(".exit")
    if exit_path.exists():
        try:
            expected_exit = int(exit_path.read_text(encoding="utf-8").strip())
        except (ValueError, UnicodeDecodeError) as error:
            raise FormFault(exit_path, 1, 1, "must contain one integer", root=root) from error
        if expected_exit < 0:
            raise FormFault(exit_path, 1, 1, "must be zero or greater", root=root)
    if kind in {"state", "race", "document", "report", "view"}:
        _validate_case_body(case_path, str(kind), case_id)
        if alternate_case_path is not None:
            _validate_case_body(alternate_case_path, str(kind), case_id)
    return case


def _case_file(root: Path, case: Mapping[str, object]) -> Path:
    alternate_file = case.get("alternate_file")
    if os.environ.get("CMSC127_SCHEMA") == "alt" and isinstance(alternate_file, str):
        return root / alternate_file
    return root / str(case["file"])


def _case_expected_file(root: Path, case: Mapping[str, object]) -> Path:
    alternate_expected_file = case.get("alternate_expected_file")
    if (
        os.environ.get("CMSC127_SCHEMA") == "alt"
        and isinstance(alternate_expected_file, str)
    ):
        return root / alternate_expected_file
    expected_file = case.get("expected_file")
    if isinstance(expected_file, str):
        return root / expected_file
    return (root / str(case["file"])).with_suffix(".expected")


def _case_role(case: Mapping[str, object]) -> object:
    role = case.get("role")
    role_env = case.get("role_env")
    if isinstance(role_env, str):
        return os.environ.get(role_env, role)
    return role


def _load_pair_manifest(
    root: Path,
    case_root: Path,
    identifiers: dict[str, str],
    listed: set[Path],
) -> list[dict[str, object]]:
    pair_path = case_root / "pair" / "manifest.json"
    if not pair_path.is_file():
        return []
    text = pair_path.read_text(encoding="utf-8")
    pair_manifest = _as_object(_read_json(pair_path), "cases/pair/manifest.json")
    locations = _json_locations(text)

    def fault(message: str, location: tuple[str | int, ...] = ()) -> FormFault:
        line, column = locations.get(location, (1, 1))
        return FormFault(pair_path, line, column, message, root=root)

    if pair_manifest.get("version") != 1:
        raise fault("version must be 1", ("version",))
    if "invariants" in pair_manifest or "skips" in pair_manifest:
        raise fault("the pair manifest may list only cases", ())
    raw_cases = pair_manifest.get("cases")
    if not isinstance(raw_cases, list):
        raise fault("cases must be a list", ("cases",))
    cases: list[dict[str, object]] = []
    for index, raw_case in enumerate(cast(list[object], raw_cases), start=1):
        cases.append(
            _validate_case(
                root,
                case_root,
                raw_case,
                index,
                fault,
                identifiers,
                listed,
                source_manifest="cases/pair/manifest.json",
                pair=True,
            )
        )
    return cases


def _load_manifest(root: Path) -> dict[str, object]:
    """Load the core, published, and pair manifests."""
    case_root = root / "cases"
    manifest_path = case_root / "manifest.json"
    if not manifest_path.is_file():
        raise FormFault(manifest_path, 1, 1, "file is missing", root=root)
    manifest_text = manifest_path.read_text(encoding="utf-8")
    manifest = _as_object(_read_json(manifest_path), "cases/manifest.json")
    locations = _json_locations(manifest_text)

    def fault(message: str, location: tuple[str | int, ...] = ()) -> FormFault:
        line, column = locations.get(location, (1, 1))
        return FormFault(manifest_path, line, column, message, root=root)

    if manifest.get("version") != 1:
        raise fault("version must be 1", ("version",))
    raw_cases = manifest.get("cases")
    if not isinstance(raw_cases, list):
        raise fault("cases must be a list", ("cases",))

    identifiers: dict[str, str] = {}
    listed: set[Path] = set()
    core_cases: list[dict[str, object]] = []
    for index, raw_case in enumerate(cast(list[object], raw_cases), start=1):
        core_cases.append(
            _validate_case(
                root,
                case_root,
                raw_case,
                index,
                fault,
                identifiers,
                listed,
                source_manifest="cases/manifest.json",
            )
        )
    published_paths = sorted((case_root / "published").glob("*/manifest.json"))
    published_cases: list[dict[str, object]] = []
    for published_path in published_paths:
        published_text = published_path.read_text(encoding="utf-8")
        published_manifest = _as_object(
            _read_json(published_path), published_path.relative_to(root).as_posix()
        )
        published_locations = _json_locations(published_text)

        def published_fault(
            message: str,
            location: tuple[str | int, ...] = (),
            *,
            path: Path = published_path,
            source_locations: dict[tuple[str | int, ...], tuple[int, int]] = published_locations,
        ) -> FormFault:
            line, column = source_locations.get(location, (1, 1))
            return FormFault(path, line, column, message, root=root)

        if published_manifest.get("version") != 1:
            raise published_fault("version must be 1", ("version",))
        if "invariants" in published_manifest or "skips" in published_manifest:
            raise published_fault("a published lab manifest may list only cases", ())
        published_raw_cases = published_manifest.get("cases")
        if not isinstance(published_raw_cases, list):
            raise published_fault("cases must be a list", ("cases",))
        for index, raw_case in enumerate(
            cast(list[object], published_raw_cases), start=1
        ):
            published_cases.append(
                _validate_case(
                    root,
                    case_root,
                    raw_case,
                    index,
                    published_fault,
                    identifiers,
                    listed,
                    source_manifest=published_path.relative_to(root).as_posix(),
                )
            )
    pair_cases = _load_pair_manifest(root, case_root, identifiers, listed)
    all_cases = [*core_cases, *published_cases, *pair_cases]
    cases_by_id = {str(case["id"]): case for case in all_cases}
    for case in all_cases:
        replacement = case.get("replaces")
        if not isinstance(replacement, str):
            continue
        replaced = cases_by_id.get(replacement)
        if replaced is None:
            raise ValueError(
                f"case {case['id']}: replaces unknown case {replacement}"
            )
        replaced_stage = replaced.get("stage")
        replacement_stage = case.get("stage")
        if (
            not isinstance(replaced_stage, int)
            or isinstance(replaced_stage, bool)
            or not isinstance(replacement_stage, int)
            or isinstance(replacement_stage, bool)
        ):
            raise ValueError(f"case {case['id']}: replacement stage is invalid")
        if replaced_stage >= replacement_stage:
            raise ValueError(
                f"case {case['id']}: replacement stage must be later than {replacement}"
            )
    manifest["cases"] = all_cases

    manifest_paths = {
        manifest_path.resolve(),
        (case_root / "pair" / "manifest.json").resolve(),
        *(path.resolve() for path in published_paths),
    }
    unlisted_cases = sorted(
        path.resolve()
        for path in case_root.rglob("*")
        if path.is_file()
        and path.suffix in CASE_SUFFIXES
        and path.resolve() not in manifest_paths
        and path.resolve() not in listed
    )
    if unlisted_cases:
        relative = unlisted_cases[0].relative_to(root).as_posix()
        raise fault(f"unlisted case file: {relative}", ("cases",))
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
        raise fault(f"unlisted case sidecar: {relative}", ("cases",))

    raw_invariants = manifest.get("invariants", [])
    if not isinstance(raw_invariants, list):
        raise fault("invariants must be a list", ("invariants",))
    for index, raw_invariant in enumerate(cast(list[object], raw_invariants), start=1):
        location = ("invariants", index - 1)
        try:
            invariant = _as_object(raw_invariant, f"invariant {index}")
        except ValueError as error:
            raise fault(str(error), location) from error
        invariant_stage = invariant.get("stage")
        if (
            not isinstance(invariant_stage, int)
            or isinstance(invariant_stage, bool)
            or invariant_stage < 1
        ):
            raise fault(
                f"invariant {index}: stage must be a positive integer",
                (*location, "stage"),
            )
        kind = invariant.get("kind")
        if not isinstance(kind, str) or kind not in INVARIANT_KINDS:
            raise fault(
                f"invariant {index}: unsupported kind {kind!r}",
                (*location, "kind"),
            )
        _validate_invariant_fields(invariant, str(kind), index, fault, location)
    skips = manifest.get("skips", [])
    if not isinstance(skips, list):
        raise fault("skips must be a list", ("skips",))
    for index, raw_skip in enumerate(cast(list[object], skips), start=1):
        location = ("skips", index - 1)
        try:
            skip = _as_object(raw_skip, f"skip {index}")
        except ValueError as error:
            raise fault(str(error), location) from error
        skip_stage = skip.get("stage", 1)
        if not isinstance(skip_stage, int) or isinstance(skip_stage, bool) or skip_stage < 1:
            raise fault(
                f"skip {index}: stage must be a positive integer",
                (*location, "stage"),
            )
    return manifest


def load_manifest(root: Path) -> dict[str, object]:
    """Load the manifest and add a source location to each form fault."""
    return _load_manifest(root.resolve())


def published_stage(root: Path) -> int:
    """Return the highest stage in the published lab manifests, or 1."""
    published_root = root / "cases" / "published"
    stages: list[int] = []
    for path in sorted(published_root.glob("*/manifest.json")):
        manifest = _as_object(_read_json(path), path.relative_to(root).as_posix())
        raw_cases = manifest.get("cases")
        if not isinstance(raw_cases, list):
            raise ValueError(f"{path.relative_to(root).as_posix()}: cases must be a list")
        for index, raw_case in enumerate(cast(list[object], raw_cases), start=1):
            case = _as_object(raw_case, f"case {index}")
            stage = case.get("stage")
            if not isinstance(stage, int) or isinstance(stage, bool) or stage < 1:
                raise ValueError(
                    f"{path.relative_to(root).as_posix()}: case {index}: "
                    "stage must be a positive integer"
                )
            stages.append(stage)
    return max(stages, default=1)


def select_cases(manifest: Mapping[str, object], stage: int) -> list[dict[str, object]]:
    """Return all cases through the requested stage."""
    raw_cases_value = manifest.get("cases")
    if not isinstance(raw_cases_value, list):
        return []
    replaced_ids: set[str] = set()
    for raw_case in cast(list[object], raw_cases_value):
        if not isinstance(raw_case, dict):
            continue
        case = cast(dict[str, object], raw_case)
        case_stage = case.get("stage")
        replacement = case.get("replaces")
        if (
            isinstance(case_stage, int)
            and not isinstance(case_stage, bool)
            and case_stage <= stage
            and isinstance(replacement, str)
        ):
            replaced_ids.add(replacement)
    selected: list[dict[str, object]] = []
    for raw_case in cast(list[object], raw_cases_value):
        if isinstance(raw_case, dict):
            case = cast(dict[str, object], raw_case)
            case_stage = case.get("stage")
            if (
                isinstance(case_stage, int)
                and case_stage <= stage
                and str(case.get("id")) not in replaced_ids
            ):
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


def _read_expected(
    case_path: Path, expected_path: Path | None = None
) -> dict[str, object]:
    path = expected_path if expected_path is not None else case_path.with_suffix(".expected")
    value = _as_object(_read_json(path), ".expected")
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
    blocks: list[tuple[str, str]] = []
    lines = text.splitlines(keepends=True)
    index = 0
    while index < len(lines):
        opening = re.match(
            r"^[ \t]*(`{3,}|~{3,})(.*)$", lines[index].rstrip("\r\n")
        )
        if opening is None:
            index += 1
            continue
        fence = opening.group(1)
        info = opening.group(2).strip()
        block_lines: list[str] = []
        index += 1
        while index < len(lines):
            closing = re.match(
                r"^[ \t]*(`{3,}|~{3,})\s*$", lines[index].rstrip("\r\n")
            )
            if (
                closing is not None
                and closing.group(1)[0] == fence[0]
                and len(closing.group(1)) >= len(fence)
            ):
                index += 1
                break
            block_lines.append(lines[index])
            index += 1
        blocks.append((info, "".join(block_lines)))
    for block_info, block_body in blocks:
        if "erDiagram" in block_body and "mermaid" in block_info.lower():
            return block_body
    for _, block_body in blocks:
        if "erDiagram" in block_body:
            return block_body
    return text


def _bash_executable() -> str:
    if os.name == "nt":
        candidates: list[Path] = []
        git_executable = shutil.which("git")
        if git_executable is not None:
            candidates.append(
                Path(git_executable).resolve().parent.parent / "bin" / "bash.exe"
            )
        for root in (
            Path(r"C:\Program Files\Git"),
            Path(r"C:\Program Files (x86)\Git"),
        ):
            candidates.append(root / "bin" / "bash.exe")
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
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
    name = f"c127_race_{uuid4().hex}"
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


def _report_path(root: Path, report_name: str, case_id: str) -> Path | None:
    report_path = (root / report_name).resolve()
    try:
        report_path.relative_to(root.resolve())
    except ValueError as error:
        raise ValueError(
            f"case {case_id}: the report must stay inside the repository"
        ) from error
    if report_path.suffix != ".sql" or not report_path.is_file():
        return None
    return report_path


def _view_bypass_rows(
    connection: _DbConnection, report_path: Path, view_name: str
) -> list[list[object]]:
    query = report_path.read_text(encoding="utf-8")
    probe = f"cmsc127_probe_{uuid4().hex[:12]}"
    relations: list[str] = []
    with connection.cursor() as cursor:
        try:
            _execute(cursor, f'CREATE TEMP VIEW "{probe}" AS ' + query)
            _execute(
                cursor,
                """SELECT DISTINCT referenced.relname::text AS relation
                   FROM pg_rewrite AS rewrite
                   JOIN pg_class AS view_class ON view_class.oid = rewrite.ev_class
                   JOIN pg_depend AS dependency
                     ON dependency.classid = 'pg_rewrite'::regclass
                    AND dependency.objid = rewrite.oid
                    AND dependency.refclassid = 'pg_class'::regclass
                   JOIN pg_class AS referenced ON referenced.oid = dependency.refobjid
                   WHERE view_class.oid = %s::regclass
                     AND referenced.oid <> view_class.oid
                   ORDER BY relation""",
                (f"pg_temp.{probe}",),
            )
            relations = [str(row[0]) for row in cursor.fetchall()]
        finally:
            _execute(cursor, f'DROP VIEW IF EXISTS pg_temp."{probe}"')
    if view_name in relations:
        return []
    if not relations:
        return [[view_name, f"the report does not read the {view_name} view"]]
    return [
        [relation, f"the report reads {relation} instead of the {view_name} view"]
        for relation in relations
    ]


def _referenced_view_definitions(
    connection: _DbConnection, report_path: Path
) -> list[str]:
    query = report_path.read_text(encoding="utf-8")
    probe = f"cmsc127_feature_{uuid4().hex[:12]}"
    created = False
    try:
        with connection.cursor() as cursor:
            try:
                _execute(cursor, f'CREATE TEMP VIEW "{probe}" AS ' + query)
                created = True
                _execute(
                    cursor,
                    """SELECT DISTINCT pg_get_viewdef(referenced.oid, true)
                       FROM pg_rewrite AS report_rewrite
                       JOIN pg_depend AS dependency
                         ON dependency.classid = 'pg_rewrite'::regclass
                        AND dependency.objid = report_rewrite.oid
                        AND dependency.refclassid = 'pg_class'::regclass
                       JOIN pg_class AS referenced
                         ON referenced.oid = dependency.refobjid
                       WHERE report_rewrite.ev_class = %s::regclass
                         AND referenced.relkind IN ('v', 'm')
                         AND referenced.relname <> %s""",
                    (f"pg_temp.{probe}", probe),
                )
                return [str(row[0]) for row in cursor.fetchall()]
            except psycopg.Error:
                return []
    finally:
        if created:
            with connection.cursor() as cursor:
                _execute(cursor, f'DROP VIEW IF EXISTS pg_temp."{probe}"')


class _RollbackRoleProbe(Exception):
    pass


def _sql_error(
    connection: _DbConnection,
    statement: str,
    role: str | None = None,
) -> tuple[str, str | None, str | None, str | None, str | None] | None:
    if role is None:
        try:
            with connection.transaction(force_rollback=True):
                with connection.cursor() as cursor:
                    _execute(cursor, statement)
                    cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        except psycopg.Error as error:
            return (
                error.sqlstate or error.__class__.__name__,
                error.diag.constraint_name,
                error.diag.table_name,
                error.diag.column_name,
                error.diag.source_function,
            )
        return None

    probe_role = f"c127_race_{uuid4().hex}_probe"
    probe_error: tuple[str, str | None, str | None, str | None, str | None] | None = None
    try:
        try:
            with connection.transaction():
                with connection.cursor() as cursor:
                    _execute(cursor, f'CREATE ROLE "{probe_role}" NOLOGIN')
                    _execute(cursor, f'GRANT "{role}" TO "{probe_role}"')
                try:
                    with connection.transaction():
                        with connection.cursor() as cursor:
                            _execute(cursor, f'SET ROLE "{probe_role}"')
                            _execute(cursor, statement)
                except psycopg.Error as error:
                    probe_error = (
                        error.sqlstate or error.__class__.__name__,
                        error.diag.constraint_name,
                        error.diag.table_name,
                        error.diag.column_name,
                        error.diag.source_function,
                    )
                raise _RollbackRoleProbe()
        except _RollbackRoleProbe:
            pass
    finally:
        with connection.cursor() as cursor:
            _execute(cursor, f'DROP ROLE IF EXISTS "{probe_role}"')
    return probe_error


def _relation_tree(
    connection: _DbConnection, target: str
) -> list[tuple[str, str]]:
    """The target's partition and inheritance tree, in both directions.

    ``pg_inherits`` links a child to its parent. The walk climbs to every
    ancestor and descends to every descendant, so a trigger or a constraint on
    a partition or on an inherited child belongs to the insert's tree. The list
    is empty when the target does not resolve.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            WITH RECURSIVE roots AS (
                SELECT to_regclass(%s) AS relation_oid
            ), ancestors AS (
                SELECT relation_oid
                FROM roots
                WHERE relation_oid IS NOT NULL
                UNION
                SELECT link.inhparent
                FROM pg_inherits AS link
                JOIN ancestors ON link.inhrelid = ancestors.relation_oid
            ), tree AS (
                SELECT relation_oid FROM ancestors
                UNION
                SELECT link.inhrelid
                FROM pg_inherits AS link
                JOIN tree ON link.inhparent = tree.relation_oid
            )
            SELECT namespace_entry.nspname, class_entry.relname
            FROM tree
            JOIN pg_class AS class_entry ON class_entry.oid = tree.relation_oid
            JOIN pg_namespace AS namespace_entry
              ON namespace_entry.oid = class_entry.relnamespace
            ORDER BY 1, 2
            """,
            (target,),
        )
        return [(str(row[0]), str(row[1])) for row in cursor.fetchall()]


def _relation_in_tree(tree: Sequence[tuple[str, str]], table_name: str) -> bool:
    """True when the error's table is any table in the insert's tree."""
    wanted = table_name.strip('"').lower()
    return any(relname.strip('"').lower() == wanted for _, relname in tree)


def _tree_rule(
    connection: _DbConnection, tree: Sequence[tuple[str, str]]
) -> str | None:
    """The first rewrite rule on the tree that is not a view's ``_RETURN``."""
    if not tree:
        return None
    conditions = " OR ".join(
        "(namespace_entry.nspname = %s AND class_entry.relname = %s)" for _ in tree
    )
    parameters = [value for pair in tree for value in pair]
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT class_entry.relname, rule_entry.rulename
            FROM pg_rewrite AS rule_entry
            JOIN pg_class AS class_entry ON class_entry.oid = rule_entry.ev_class
            JOIN pg_namespace AS namespace_entry
              ON namespace_entry.oid = class_entry.relnamespace
            WHERE ({conditions})
              AND rule_entry.rulename <> '_RETURN'
            ORDER BY 1, 2
            LIMIT 1
            """,
            parameters,
        )
        row = cursor.fetchone()
    if row is None:
        return None
    return f"{row[0]}.{row[1]}"


def _refused_without_user_triggers(
    connection: _DbConnection,
    statement: str,
    target: str,
    sqlstate: str,
    constraint_name: str | None,
    column_name: str | None,
) -> bool:
    """True when the same constraint refuses the insert with user triggers off.

    The insert runs again in a transaction that is always rolled back, after
    ``ALTER TABLE ... DISABLE TRIGGER USER`` on every table in the target's
    partition and inheritance tree. ``SET CONSTRAINTS ALL IMMEDIATE`` makes a
    deferrable constraint fire at the insert. Internal foreign-key triggers
    stay on, so a foreign-key case still fires. A replay that succeeds, or that
    fails on a different constraint, returns False.
    """
    tree = _relation_tree(connection, target)
    try:
        with connection.transaction(force_rollback=True):
            with connection.cursor() as cursor:
                for schema, relname in tree:
                    cursor.execute(
                        f"ALTER TABLE {_quote_identifier(schema)}."
                        f"{_quote_identifier(relname)} DISABLE TRIGGER USER"
                    )
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
                _execute(cursor, statement)
    except psycopg.Error as error:
        return (
            (error.sqlstate or error.__class__.__name__) == sqlstate
            and error.diag.constraint_name == constraint_name
            and error.diag.column_name == column_name
        )
    return False


_RAISE_SOURCE_FUNCTIONS = frozenset({"exec_stmt_raise", "exec_stmt_assert"})


def _constraint_refused(
    connection: _DbConnection,
    sqlstate: str,
    constraint_name: str | None,
    table_name: str | None,
    column_name: str | None,
    source_function: str | None,
) -> bool:
    """True when PostgreSQL raised the error from a constraint, not a RAISE.

    PostgreSQL fills ``diag.source_function`` from its own C source, so the
    check does not depend on the translated error text. The PL/pgSQL ``RAISE``
    handler is ``exec_stmt_raise`` and the ``ASSERT`` handler is
    ``exec_stmt_assert``; an error from either fails, whatever constraint it
    names and whatever ``lc_messages`` says. A constraint that fires inside a
    trigger's own SQL statement reports the constraint's own function, so Rule
    11's helper-table insert passes. The named constraint may be a table
    constraint, a unique index, or a domain constraint. A ``NOT NULL``
    violation (SQLSTATE 23502) carries no constraint name, so it passes when
    the named column is a ``NOT NULL`` column of the named table.
    """
    if source_function in _RAISE_SOURCE_FUNCTIONS:
        return False
    if constraint_name is None:
        if sqlstate != "23502" or column_name is None or table_name is None:
            return False
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1
                FROM pg_attribute AS attribute
                JOIN pg_class AS class_entry
                  ON class_entry.oid = attribute.attrelid
                JOIN pg_namespace AS namespace_entry
                  ON namespace_entry.oid = class_entry.relnamespace
                WHERE class_entry.relname = %s
                  AND namespace_entry.nspname = current_schema()
                  AND attribute.attname = %s
                  AND attribute.attnum > 0
                  AND NOT attribute.attisdropped
                  AND attribute.attnotnull
                """,
                (table_name, column_name),
            )
            return cursor.fetchone() is not None
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT 1
            FROM pg_constraint AS constraint_entry
            JOIN pg_namespace AS namespace_entry
              ON namespace_entry.oid = constraint_entry.connamespace
            WHERE constraint_entry.conname = %s
              AND namespace_entry.nspname = current_schema()
              AND (
                    (constraint_entry.conrelid = 0
                     AND constraint_entry.contypid <> 0)
                 OR constraint_entry.conrelid = (
                        SELECT class_entry.oid
                        FROM pg_class AS class_entry
                        JOIN pg_namespace AS class_namespace
                          ON class_namespace.oid = class_entry.relnamespace
                        WHERE class_entry.relname = %s
                          AND class_namespace.nspname = current_schema()
                    )
              )
            UNION ALL
            SELECT 1
            FROM pg_index AS index_entry
            JOIN pg_class AS index_class ON index_class.oid = index_entry.indexrelid
            JOIN pg_class AS table_class ON table_class.oid = index_entry.indrelid
            JOIN pg_namespace AS namespace_entry
              ON namespace_entry.oid = table_class.relnamespace
            WHERE index_class.relname = %s
              AND table_class.relname = %s
              AND namespace_entry.nspname = current_schema()
              AND index_entry.indisunique
              AND index_entry.indisvalid
              AND index_entry.indpred IS NULL
              AND index_entry.indexprs IS NULL
            """,
            (constraint_name, table_name, constraint_name, table_name),
        )
        return cursor.fetchone() is not None


def _split_warnings(
    dataset: Mapping[str, object],
) -> tuple[dict[str, object], list[str]]:
    """Split warning-labelled rows out of a case result.

    A row is a warning when its first cell is a string that starts with
    ``warning:``. The runner prints those rows and counts them apart from
    failures, so a case that prints only warnings passes.
    """
    rows_value = dataset.get("rows")
    if not isinstance(rows_value, list):
        return dict(dataset), []
    rows = cast(list[object], rows_value)
    kept: list[object] = []
    warnings: list[str] = []
    for row in rows:
        cells = cast(list[object], row) if isinstance(row, list) else None
        first = cells[0] if cells else None
        if isinstance(first, str) and first.lower().startswith(WARNING_LABEL):
            warnings.append(first)
        else:
            kept.append(cast(object, row))
    return {**dataset, "rows": kept}, warnings


def _warning_columns(rows: Sequence[str]) -> list[str]:
    """The table column or report path named at the start of each warning row."""
    columns: list[str] = []
    for row in rows:
        if not row.lower().startswith(WARNING_LABEL):
            continue
        rest = row[len(WARNING_LABEL) :].strip()
        subject = rest.split()[0].rstrip(".,;:") if rest else ""
        if _WARNING_SUBJECT.fullmatch(subject) and subject not in columns:
            columns.append(subject)
    return columns


_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_HEADING_MARKS = "*_`"
_WARNING_SUBJECT = re.compile(
    r"(?:[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*|"
    r"reports/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.sql)\Z"
)


def _heading_first_word(title: str) -> str:
    """The first word of a heading, without Markdown emphasis marks."""
    words = title.split()
    if not words:
        return ""
    return words[0].strip(_HEADING_MARKS)


def _document_sections(text: str) -> list[tuple[int, str, str]]:
    """The level, title, and body of every Markdown heading."""
    lines = text.splitlines()
    headings: list[tuple[int, int, str]] = []
    for number, line in enumerate(lines):
        match = _HEADING.match(line)
        if match is not None:
            headings.append((number, len(match.group(1)), match.group(2)))
    sections: list[tuple[int, str, str]] = []
    for index, (number, level, title) in enumerate(headings):
        end = len(lines)
        for later_number, later_level, _ in headings[index + 1 :]:
            if later_level <= level:
                end = later_number
                break
        sections.append((level, title, "\n".join(lines[number + 1 : end])))
    return sections


def _normalized_line(line: str) -> str:
    stripped = line.strip().lstrip("-*+ ").strip()
    return re.sub(r"\s+", " ", stripped).lower()


def _design_rows(
    root: Path,
    config: Mapping[str, object],
    warning_columns: Sequence[str],
) -> list[str]:
    """The structural faults in a student's design record."""
    file_name = config.get("file")
    headings = config.get("headings")
    placeholders = config.get("placeholders")
    if (
        not isinstance(file_name, str)
        or not isinstance(headings, list)
        or not isinstance(placeholders, dict)
    ):
        return ["the design case needs file, headings, and placeholders"]
    path = root / file_name
    if not path.is_file():
        return [f"{file_name} is missing, so the design record cannot be checked."]
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return [f"{file_name} is not UTF-8 text, so the design record cannot be read."]
    placeholder_text = cast(dict[str, object], placeholders)
    level_two = [
        (title, body) for level, title, body in _document_sections(text) if level == 2
    ]
    titles = [title for title, _ in level_two]
    bodies = dict(level_two)
    expected_titles = [str(heading) for heading in cast(list[object], headings)]
    rows: list[str] = []
    for title in expected_titles:
        if title not in titles:
            rows.append(f'{file_name} is missing the heading "## {title}".')
    present = [title for title in titles if title in expected_titles]
    if present != [title for title in expected_titles if title in present]:
        rows.append(
            f"{file_name} puts the headings out of order: it reads "
            + ", ".join(f'"{title}"' for title in present)
            + "."
        )
    for title in expected_titles:
        body = bodies.get(title)
        if body is None:
            continue
        if not body.strip():
            rows.append(f'{file_name} has nothing under "## {title}".')
            continue
        placeholder = placeholder_text.get(title)
        if isinstance(placeholder, str):
            wanted = _normalized_line(placeholder)
            if wanted and any(
                _normalized_line(line) == wanted for line in body.splitlines()
            ):
                rows.append(
                    f'{file_name} still holds the placeholder line under "## {title}".'
                )
    warning_body = bodies.get("Checker Warnings", "")
    entries: list[tuple[int, str]] = []
    for line in warning_body.splitlines():
        heading = _HEADING.match(line)
        if heading is not None:
            subject = _heading_first_word(heading.group(2))
            if _WARNING_SUBJECT.fullmatch(subject):
                entries.append((len(heading.group(1)), subject))
    for column in warning_columns:
        levels = [level for level, word in entries if word == column]
        if not levels:
            rows.append(
                f'{file_name} has no "### {column}" entry under "## Checker Warnings", '
                "so the checker warning is not explained."
            )
        elif 3 not in levels:
            rows.append(
                f'{file_name} puts the "{column}" entry at heading level {levels[0]}, '
                f'but a Checker Warnings entry is a level-3 heading ("### {column}").'
            )
    return rows


def _run_case(
    root: Path,
    case: Mapping[str, object],
    connection: _DbConnection,
    database_url: str,
    feature_warning_subjects: Sequence[str] = (),
) -> dict[str, object]:
    case_id = str(case["id"])
    case_path = _case_file(root, case)
    expected = _read_expected(case_path, _case_expected_file(root, case))
    kind = str(case["kind"])
    ordered = case.get("ordered", True) is not False
    warnings: list[str] = []
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
        actual, warnings = _split_warnings(actual)
        problems = compare_dataset(expected, actual, ordered=ordered)
    elif kind == "document":
        body = _as_object(_read_json(case_path), f"case {case_id}")
        warning_columns = list(feature_warning_subjects)
        warning_sql = body.get("warning_sql")
        if isinstance(warning_sql, str):
            warning_rows = _run_sql_file(connection, root / warning_sql).get("rows")
            if isinstance(warning_rows, list):
                texts: list[str] = []
                for row in cast(list[object], warning_rows):
                    cells = cast(list[object], row) if isinstance(row, list) else []
                    if cells:
                        texts.append(str(cells[0]))
                warning_columns.extend(_warning_columns(texts))
        warning_columns = list(dict.fromkeys(warning_columns))
        rows = _design_rows(root, body, warning_columns)
        actual = {"columns": ["row"], "rows": [[row] for row in rows]}
        problems = compare_dataset(expected, actual, ordered=ordered)
    elif kind == "reject":
        statement = case_path.read_text(encoding="utf-8")
        target = _reject_insert_target(statement)
        role = _case_role(case)
        error = _sql_error(
            connection,
            statement,
            role if isinstance(role, str) else None,
        )
        expected_code = expected.get("sqlstate")
        if error is None:
            actual = {"sqlstate": None}
            problems = ["statement succeeded, but the case requires rejection"]
        else:
            error_code, constraint_name, table_name, column_name, source_function = error
            actual = {"sqlstate": error_code}
            problems = []
            if expected_code is not None and expected_code != error_code:
                problems.append(
                    f"SQLSTATE differs: expected {expected_code!r}, actual {error_code!r}"
                )
            if role is not None:
                if error_code != "42501" or source_function in _RAISE_SOURCE_FUNCTIONS:
                    problems.append("the error does not come from the restricted role's privileges")
            elif not _constraint_refused(
                connection,
                error_code,
                constraint_name,
                table_name,
                column_name,
                source_function,
            ):
                problems.append(
                    "the error does not come from a named constraint; a RAISE, "
                    "or an error with no constraint, does not test the schema"
                )
            elif target is not None:
                tree = _relation_tree(connection, target)
                rule = _tree_rule(connection, tree)
                if rule is not None:
                    problems.append(
                        f"the insert target {target} carries the rewrite rule "
                        f"{rule}; rewrite rules are outside this course, so the "
                        "case cannot show that a constraint refuses the row"
                    )
                elif (
                    table_name is not None
                    and _relation_in_tree(tree, table_name)
                    and not _refused_without_user_triggers(
                        connection,
                        statement,
                        target,
                        error_code,
                        constraint_name,
                        column_name,
                    )
                ):
                    problems.append(
                        "the constraint refuses the row only after a user trigger "
                        "runs; it must refuse the same insert with user triggers off"
                    )
    elif kind == "state":
        body = _as_object(_read_json(case_path), f"case {case_id}")
        operation = body.get("operation")
        check_sql = body.get("check_sql")
        if not isinstance(operation, str) or not isinstance(check_sql, str):
            raise ValueError(f"case {case_id}: state file needs operation and check_sql strings")
        restore_problems = _backup_restore(root, connection, database_url) if operation == "restore" else []
        if operation != "restore":
            with connection.cursor() as cursor:
                _execute(cursor, operation)
        if restore_problems:
            actual = {"columns": [], "rows": []}
            problems = restore_problems
        else:
            with connection.cursor() as cursor:
                _execute(cursor, check_sql)
                actual = _dataset_from_cursor(cursor)
            actual, warnings = _split_warnings(actual)
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
                if (
                    not isinstance(session, str)
                    or session not in {"a", "b"}
                    or not isinstance(statement, str)
                ):
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
    elif kind == "report":
        body = _as_object(_read_json(case_path), f"case {case_id}")
        report_name = body.get("report")
        if not isinstance(report_name, str):
            raise ValueError(f"case {case_id}: report file needs a report string")
        report_path = _report_path(root, report_name, case_id)
        if report_path is None:
            actual = {"columns": [], "rows": []}
            problems = [f"report SQL file is missing: {report_name}"]
        else:
            statement_count = _sql_statement_count(
                report_path.read_text(encoding="utf-8")
            )
            if statement_count != 1:
                row = (
                    f"report file {report_name} holds {statement_count} statements; "
                    "the report must be one query."
                )
                actual = {"columns": ["row"], "rows": [[row]]}
                problems = [row]
            else:
                actual = _run_sql_file(connection, report_path)
                actual, warnings = _split_warnings(actual)
                problems = compare_dataset(expected, actual, ordered=ordered)
    elif kind == "view":
        body = _as_object(_read_json(case_path), f"case {case_id}")
        report_name = body.get("report")
        view_name = body.get("view")
        if not isinstance(report_name, str) or not isinstance(view_name, str):
            raise ValueError(f"case {case_id}: view file needs report and view strings")
        report_path = _report_path(root, report_name, case_id)
        view_rows: list[list[object]]
        if report_path is None:
            view_rows = cast(
                list[list[object]],
                [[view_name, f"the report SQL file is missing: {report_name}"]],
            )
        else:
            view_rows = _view_bypass_rows(connection, report_path, view_name)
        actual = {"columns": ["relation", "problem"], "rows": view_rows}
        problems = compare_dataset(expected, actual, ordered=ordered)
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
        "warnings": warnings,
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


def _table_columns(connection: _DbConnection, table: str) -> set[str] | None:
    with connection.cursor() as cursor:
        _execute(cursor, "SELECT to_regclass(%s)", (table,))
        row = cursor.fetchone()
        if row is None or row[0] is None:
            return None
        _execute(
            cursor,
            """SELECT attribute.attname
               FROM pg_attribute AS attribute
               WHERE attribute.attrelid = to_regclass(%s)
                 AND attribute.attnum > 0
                 AND NOT attribute.attisdropped""",
            (table,),
        )
        return {str(record[0]) for record in cursor.fetchall()}


def _check_normal_form(
    connection: _DbConnection, config: Mapping[str, object]
) -> list[str]:
    mapping = config.get("mapping")
    rules = config.get("rules")
    if not isinstance(mapping, dict) or not isinstance(rules, list):
        return ["normal_form needs mapping and rules"]
    columns = _as_object(cast(object, mapping), "normal_form mapping")
    errors: list[str] = []
    targets: dict[str, tuple[str, str]] = {}
    catalog: dict[str, set[str] | None] = {}
    if not columns:
        errors.append("normal_form mapping is empty")
    for source_name, target_value in columns.items():
        if not isinstance(target_value, dict):
            errors.append(f"mapping for {source_name} must be an object")
            continue
        target_object = _as_object(
            cast(object, target_value), f"mapping for {source_name}"
        )
        table = target_object.get("table")
        column = target_object.get("column")
        if not isinstance(table, str) or not isinstance(column, str):
            errors.append(f"mapping for {source_name} needs table and column")
            continue
        if table not in catalog:
            catalog[table] = _table_columns(connection, table)
        table_columns = catalog[table]
        if table_columns is None:
            errors.append(
                f"mapping for {source_name} names a table that does not exist: {table}"
            )
            continue
        if column not in table_columns:
            errors.append(
                f"mapping for {source_name} names a column that does not exist: "
                f"{table}.{column}"
            )
            continue
        targets[source_name] = (table, column)
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
            target = targets.get(source_name)
            if target is None:
                valid = False
                continue
            determinant.append(target)
        for source_name in cast(list[object], dependent_names):
            if not isinstance(source_name, str) or source_name not in columns:
                errors.append(f"{rule_id}: source column {source_name!r} has no mapping")
                valid = False
                continue
            target = targets.get(source_name)
            if target is None:
                valid = False
                continue
            dependents.append(target)
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
    connection: _DbConnection,
    item: Mapping[str, object],
    root: Path | None = None,
    migration_views: set[tuple[str, str]] | None = None,
) -> list[str]:
    features = item.get("features")
    if not isinstance(features, list):
        return ["feature invariant needs a features list"]
    invalid = [
        f"unsupported SQL feature {feature!r}"
        for feature in cast(list[object], features)
        if not isinstance(feature, str) or feature not in FEATURE_PATTERNS
    ]
    if invalid:
        return invalid
    selected_features = cast(list[str], features)
    file_name = item.get("file")
    if isinstance(file_name, str):
        if root is None:
            return ["feature invariant needs the repository root for a file check"]
        path = (root / file_name).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError:
            return [f"feature file must stay inside the repository: {file_name}"]
        if not path.is_file():
            return [f"feature file is missing: {file_name}"]
        feature_text = _strip_sql_comments_and_strings(
            path.read_text(encoding="utf-8")
        )
        warnings: list[str] = []
        for feature in selected_features:
            if FEATURE_PATTERNS[feature].search(feature_text) is None:
                found_in_view = False
                if feature == "coalesce":
                    definitions = _referenced_view_definitions(connection, path)
                    view_text = _strip_sql_comments_and_strings("\n".join(definitions))
                    found_in_view = FEATURE_PATTERNS[feature].search(view_text) is not None
                if not found_in_view:
                    label = FEATURE_LABELS.get(feature, feature.replace("_", " "))
                    warnings.append(
                        f"warning: {file_name} shows no {label}. "
                        "Explain your query under Checker Warnings in DESIGN.md."
                    )
        return warnings
    view = item.get("view")
    schema = item.get("schema", "public")
    if not isinstance(view, str) or not isinstance(schema, str):
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
    if migration_views is not None and (schema, view) not in migration_views:
        return [f"view {view} was not created by a migration"]
    return []


def _catalog_indexes(connection: _DbConnection) -> set[tuple[str, str]]:
    with connection.cursor() as cursor:
        _execute(
            cursor,
            """SELECT namespace_entry.nspname, class_entry.relname
               FROM pg_class AS class_entry
               JOIN pg_namespace AS namespace_entry
                 ON namespace_entry.oid = class_entry.relnamespace
               WHERE class_entry.relkind = 'i'""",
        )
        return {(str(row[0]), str(row[1])) for row in cursor.fetchall()}


def _catalog_views(connection: _DbConnection) -> set[tuple[str, str]]:
    with connection.cursor() as cursor:
        _execute(
            cursor,
            """SELECT namespace_entry.nspname, class_entry.relname
               FROM pg_class AS class_entry
               JOIN pg_namespace AS namespace_entry
                 ON namespace_entry.oid = class_entry.relnamespace
               WHERE class_entry.relkind IN ('v', 'm')""",
        )
        return {(str(row[0]), str(row[1])) for row in cursor.fetchall()}


def _resolve_index(connection: _DbConnection, name: str) -> tuple[str, str] | None:
    if "." in name:
        schema, _, index_name = name.partition(".")
        with connection.cursor() as cursor:
            _execute(cursor, "SELECT to_regclass(%s)", (name,))
            row = cursor.fetchone()
        if row is None or row[0] is None:
            return None
        return schema, index_name
    with connection.cursor() as cursor:
        _execute(
            cursor,
            """SELECT namespace_entry.nspname, class_entry.relname
               FROM pg_class AS class_entry
               JOIN pg_namespace AS namespace_entry
                 ON namespace_entry.oid = class_entry.relnamespace
               WHERE class_entry.relkind = 'i'
                 AND class_entry.relname = %s""",
            (name,),
        )
        identities = {(str(row[0]), str(row[1])) for row in cursor.fetchall()}
    if len(identities) != 1:
        return None
    return next(iter(identities))


def _plan_indexes(plan: object) -> set[tuple[str | None, str]]:
    found: set[tuple[str | None, str]] = set()

    def walk(node: object) -> None:
        if isinstance(node, list):
            for child in cast(list[object], node):
                walk(child)
            return
        if not isinstance(node, dict):
            return
        entry = cast(dict[str, object], node)
        index_name = entry.get("Index Name")
        schema = entry.get("Schema")
        if isinstance(index_name, str):
            found.add((schema if isinstance(schema, str) else None, index_name))
        children = entry.get("Plans")
        if isinstance(children, list):
            for child in cast(list[object], children):
                walk(child)
        plan_child = entry.get("Plan")
        if isinstance(plan_child, dict):
            walk(cast(dict[str, object], plan_child))
        elif isinstance(plan_child, list):
            walk(cast(list[object], plan_child))

    walk(plan)
    return found


def _plan_index_conditions(plan: object) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []

    def walk(node: object) -> None:
        if isinstance(node, list):
            for child in cast(list[object], node):
                walk(child)
            return
        if not isinstance(node, dict):
            return
        entry = cast(dict[str, object], node)
        index_name = entry.get("Index Name")
        condition = entry.get("Index Cond")
        if isinstance(index_name, str) and isinstance(condition, str):
            found.append((index_name, condition))
        children = entry.get("Plans")
        if isinstance(children, list):
            for child in cast(list[object], children):
                walk(child)
        plan_child = entry.get("Plan")
        if isinstance(plan_child, dict):
            walk(cast(dict[str, object], plan_child))
        elif isinstance(plan_child, list):
            walk(cast(list[object], plan_child))

    walk(plan)
    return found


def _index_key_columns(
    connection: _DbConnection, identity: tuple[str, str]
) -> list[str]:
    schema, name = identity
    with connection.cursor() as cursor:
        _execute(
            cursor,
            """SELECT attribute.attname::text
               FROM pg_index AS index_entry
               JOIN pg_class AS index_class
                 ON index_class.oid = index_entry.indexrelid
               JOIN pg_namespace AS index_namespace
                 ON index_namespace.oid = index_class.relnamespace
               CROSS JOIN LATERAL unnest(index_entry.indkey) WITH ORDINALITY
                    AS key_column(attnum, ordinality)
               JOIN pg_attribute AS attribute
                 ON attribute.attrelid = index_entry.indrelid
                AND attribute.attnum = key_column.attnum
               WHERE index_namespace.nspname = %s
                 AND index_class.relname = %s
                 AND key_column.ordinality <= index_entry.indnkeyatts
               ORDER BY key_column.ordinality""",
            (schema, name),
        )
        return [str(row[0]) for row in cursor.fetchall()]


def _check_index(
    root: Path,
    connection: _DbConnection,
    item: Mapping[str, object],
    migration_indexes: set[tuple[str, str]],
) -> list[str]:
    file_name = item.get("file")
    index_name = item.get("index")
    key_column = item.get("key_column")
    if not isinstance(file_name, str) or not isinstance(index_name, str):
        return ["index invariant needs file and index"]
    if key_column is not None and not isinstance(key_column, str):
        return ["index key_column must be a string"]
    query_path = root / file_name
    if not query_path.is_file() or query_path.suffix != ".sql":
        return [f"index query must be a SQL file: {file_name}"]
    identity = _resolve_index(connection, index_name)
    if identity is None:
        return [f"index {index_name} does not exist in the catalog"]
    if identity not in migration_indexes:
        return [f"index {index_name} was not created by a migration"]
    if isinstance(key_column, str):
        key_columns = _index_key_columns(connection, identity)
        if not key_columns:
            return [f"index {index_name} has no key column in the catalog"]
        if key_columns[0] != key_column:
            return [
                f"index {index_name} leads with {key_columns[0]}, not {key_column}: "
                f"{key_columns!r}"
            ]
    query = query_path.read_text(encoding="utf-8")
    with connection.cursor() as cursor:
        try:
            _execute(cursor, "SET enable_seqscan = off")
            _execute(cursor, "EXPLAIN (FORMAT JSON, COSTS OFF) " + query)
            row = cursor.fetchone()
        finally:
            _execute(cursor, "SET enable_seqscan = on")
    if row is None:
        return [f"query plan is missing for index {index_name}"]
    plan_value = row[0]
    if isinstance(plan_value, (str, bytes)):
        try:
            plan: object = json.loads(plan_value)
        except json.JSONDecodeError:
            return [f"query plan is not JSON for index {index_name}"]
    else:
        plan = plan_value
    used_names = _plan_indexes(plan)
    used: set[tuple[str, str]] = set()
    for plan_schema, plan_name in used_names:
        if plan_schema is not None:
            used.add((plan_schema, plan_name))
            continue
        resolved = _resolve_index(connection, plan_name)
        if resolved is not None:
            used.add(resolved)
    if identity not in used:
        return [
            f"query plan does not use index {index_name}: {sorted(used_names)!r}"
        ]
    if isinstance(key_column, str):
        conditions = [
            condition
            for condition_name, condition in _plan_index_conditions(plan)
            if condition_name == index_name
        ]
        pattern = re.compile(rf"\b{re.escape(key_column)}\b")
        if not any(pattern.search(condition) for condition in conditions):
            return [
                f"query plan does not filter index {index_name} on {key_column}: "
                f"{conditions!r}"
            ]
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
    optional_value = manifest.get("optional_files", [])
    if not isinstance(optional_value, list):
        return ["pinned protected-file optional_files list is invalid"]
    allowed_paths = [
        value for value in cast(list[object], allowed_value) if isinstance(value, str)
    ]
    optional_files = {
        value for value in cast(list[object], optional_value) if isinstance(value, str)
    }
    if any(value not in protected_files for value in optional_files):
        return ["pinned protected-file optional_files must name protected files"]
    errors: list[str] = []
    for relative, expected_hash in protected_files.items():
        if not isinstance(expected_hash, str):
            errors.append(f"protected hash for {relative} is invalid")
            continue
        path = root / relative
        if not path.is_file():
            if relative not in optional_files:
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
        [
            "git",
            "log",
            "--since=12 weeks ago",
            "--date=short",
            "--pretty=format:%ad%x09%an",
            "--numstat",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        return (
            "Contribution by member and week (information only): "
            "SKIP (Git history is not available)."
        )
    commits: Counter[tuple[str, str]] = Counter()
    lines: Counter[tuple[str, str]] = Counter()
    current_week = ""
    current_author = ""
    for line in completed.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) == 2 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", fields[0]):
            parsed = date.fromisoformat(fields[0])
            current_week = parsed.strftime("%G-W%V")
            current_author = fields[1]
            commits[(current_week, current_author)] += 1
        elif (
            len(fields) == 3
            and current_week
            and fields[0].isdigit()
            and fields[1].isdigit()
        ):
            lines[(current_week, current_author)] += int(fields[0]) + int(fields[1])
    rows = [
        "Contribution by member and week (information only, not a score):",
        "Member\tWeek\tCommits\tChanged lines",
    ]
    for week, author in sorted(commits):
        rows.append(
            f"{author}\t{week}\t{commits[(week, author)]}\t{lines[(week, author)]}"
        )
    return "\n".join(rows)


PYRIGHT_IGNORE = re.compile(r"#\s*pyright:\s*ignore\b")
PYRIGHT_REPAIR = (
    "Pyright is not installed. Run python -m pip install -r requirements.txt."
)
PYRIGHT_SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules"}
PYRIGHT_STUB_RULES = {"reportMissingTypeStubs", "reportMissingModuleSource"}


def _python_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*.py"):
        if any(part in PYRIGHT_SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        files.append(path)
    return sorted(files)


def _pyright_ignores(root: Path) -> list[str]:
    found: list[str] = []
    for path in _python_files(root):
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            if PYRIGHT_IGNORE.search(line):
                found.append(f"{path.relative_to(root).as_posix()}:{number}")
    return found


def _check_pyright(
    root: Path, command: Sequence[str] | None = None
) -> tuple[list[str], dict[str, object]]:
    ignores = _pyright_ignores(root)
    if ignores:
        return (
            [
                f'student code must not use "# pyright: ignore": {item}'
                for item in ignores
            ],
            {"errors": len(ignores), "status": "fail"},
        )
    config_path = root / "pyrightconfig.json"
    if not config_path.is_file():
        return (
            ["pyrightconfig.json is missing; strict type checking needs it"],
            {"errors": None, "status": "fail"},
        )
    try:
        loaded_config = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return (
            ["pyrightconfig.json is not valid JSON"],
            {"errors": None, "status": "fail"},
        )
    if not isinstance(loaded_config, dict):
        return (
            ['pyrightconfig.json must set "typeCheckingMode" to "strict"'],
            {"errors": None, "status": "fail"},
        )
    config = cast(dict[str, object], loaded_config)
    if config.get("typeCheckingMode") != "strict":
        return (
            ['pyrightconfig.json must set "typeCheckingMode" to "strict"'],
            {"errors": None, "status": "fail"},
        )
    if command is None:
        executable = shutil.which("pyright")
        if executable is None:
            return ([PYRIGHT_REPAIR], {"errors": None, "status": "missing"})
        command = [executable]
    completed = subprocess.run(
        [*command, "--outputjson"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300.0,
        check=False,
    )
    try:
        loaded = json.loads(completed.stdout)
    except json.JSONDecodeError:
        detail = (completed.stderr or completed.stdout).strip()
        return (
            [f"Pyright did not return JSON: {detail}"],
            {"errors": None, "status": "fail"},
        )
    if not isinstance(loaded, dict):
        return (["Pyright returned an unexpected report"], {"errors": None, "status": "fail"})
    report = cast(dict[str, object], loaded)
    diagnostics = report.get("generalDiagnostics")
    if not isinstance(diagnostics, list):
        return (["Pyright returned an unexpected report"], {"errors": None, "status": "fail"})
    problems: list[str] = []
    for raw in cast(list[object], diagnostics):
        if not isinstance(raw, dict):
            continue
        diagnostic = cast(dict[str, object], raw)
        if diagnostic.get("rule") in PYRIGHT_STUB_RULES:
            continue
        if diagnostic.get("severity") != "error":
            continue
        file_name = diagnostic.get("file")
        message = diagnostic.get("message")
        line = 1
        column = 1
        range_value = diagnostic.get("range")
        if isinstance(range_value, dict):
            start = cast(dict[str, object], range_value).get("start")
            if isinstance(start, dict):
                start_object = cast(dict[str, object], start)
                if isinstance(start_object.get("line"), int):
                    line = cast(int, start_object["line"]) + 1
                if isinstance(start_object.get("character"), int):
                    column = cast(int, start_object["character"]) + 1
        display = "unknown file"
        if isinstance(file_name, str):
            try:
                display = Path(file_name).relative_to(root).as_posix()
            except ValueError:
                display = Path(file_name).name
        problems.append(f"{display}:{line}:{column}: {message}")
    summary: dict[str, object] = {
        "errors": len(problems),
        "status": "pass" if not problems else "fail",
    }
    return problems, summary


def _run_invariants(
    root: Path,
    manifest: Mapping[str, object],
    connection: _DbConnection,
    database_url: str,
    runner_dir: Path,
    stage: int,
    migration_indexes: set[tuple[str, str]],
    migration_views: set[tuple[str, str]] | None = None,
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
        summary: dict[str, object] = {}
        warnings: list[str] = []
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
                feature_rows = _check_feature(connection, item, root, migration_views)
                if isinstance(item.get("file"), str):
                    plain, warnings = _split_warnings(
                        {"rows": [[row] for row in feature_rows]}
                    )
                    raw_rows = plain.get("rows")
                    problems = [
                        str(cast(list[object], row)[0])
                        for row in cast(list[object], raw_rows)
                        if isinstance(row, list) and row
                    ]
                else:
                    problems = feature_rows
            elif kind == "index":
                problems = _check_index(root, connection, item, migration_indexes)
            elif kind == "backup_restore":
                problems = _backup_restore(root, connection, database_url)
            elif kind == "protected":
                problems = _check_protected(root, runner_dir)
            elif kind == "written":
                problems = _check_written(root, item)
            elif kind == "contribution":
                print(_contribution_table(root))
                problems = []
            elif kind == "pyright":
                problems, summary = _check_pyright(root)
            else:
                problems = [f"unsupported invariant kind {kind!r}"]
        except (OSError, ValueError, psycopg.Error, subprocess.SubprocessError) as error:
            problems = [str(error)]
        status = (
            "info"
            if kind == "contribution"
            else ("pass" if not problems else "fail")
        )
        actual: dict[str, object] = {"differences": problems}
        actual.update(summary)
        result: dict[str, object] = {
            "id": invariant_id,
            "kind": str(kind),
            "stage": invariant_stage,
            "group": str(item.get("group", "invariants")),
            "status": status,
            "expected": item,
            "actual": actual,
            "differences": problems,
        }
        if warnings:
            result["warnings"] = warnings
        results.append(result)
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
    skipped = 0
    warnings = 0
    for result in results:
        status = str(result.get("status"))
        if status == "skip":
            print(f"SKIP {result.get('id')}: {result.get('reason', 'platform limit')}")
            skipped += 1
            continue
        if status == "info":
            print(f"INFO {result.get('id')}")
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
        for warning in cast(list[object], result.get("warnings", [])):
            print(f"  {warning}")
            warnings += 1
    for group, tally in sorted(groups.items()):
        print(f"Group {group}: {tally['pass']} passed, {tally['fail']} failed")
    checked_total = counts["pass"] + counts["fail"]
    total = checked_total + skipped
    pyright_line = ""
    for result in results:
        if result.get("kind") != "pyright":
            continue
        actual = result.get("actual")
        errors: object = None
        if isinstance(actual, dict):
            errors = cast(dict[str, object], actual).get("errors")
        pyright_line = (
            f"; Pyright: {errors} errors"
            if isinstance(errors, int)
            else "; Pyright: unavailable"
        )
        break
    warning_word = "warning" if warnings == 1 else "warnings"
    print("== result ==")
    print(
        f"{counts['pass']}/{total} checks passed, "
        f"{counts['fail']} failed, {skipped} skipped, "
        f"{warnings} {warning_word}{pyright_line}"
    )
    for stage_number in sorted(stage_counts):
        tally = stage_counts[stage_number]
        denominator = tally["pass"] + tally["fail"]
        print(f"Stage {stage_number}: {tally['pass']}/{denominator} passed")
    if not stage_counts:
        print(f"Stage {stage}: 0/0 passed")
    return 0 if counts["fail"] == 0 and checked_total > 0 else 1


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
    parser.add_argument("--runner-dir", type=Path, default=None)
    parser.add_argument("--case", dest="case_id", default=None)
    parser.add_argument("--form-lint", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--select-stage", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.self_test:
        return _self_test()
    root = arguments.root.resolve()
    if arguments.select_stage:
        try:
            print(published_stage(root))
        except ValueError as error:
            print(str(error))
            return 2
        return 0
    if arguments.stage < 1:
        parser.error("stage must be a positive integer")
    runner_dir = (
        arguments.runner_dir.resolve()
        if arguments.runner_dir is not None
        else Path(__file__).resolve().parent
    )
    try:
        manifest = load_manifest(root)
    except ValueError as error:
        print("== form fault ==")
        print(str(error))
        return 2
    if arguments.form_lint:
        print("Form is valid.")
        return 0
    database_url = os.environ.get("CMSC127_DATABASE_URL")
    if not database_url:
        print("CMSC127_DATABASE_URL is not set. Set it to the local PostgreSQL connection URL.")
        return 2

    selected = select_cases(manifest, arguments.stage)
    if arguments.case_id is not None:
        selected = [
            case for case in selected if str(case.get("id")) == arguments.case_id
        ]
        if not selected:
            print(
                f"No case is listed with id {arguments.case_id} for stage "
                f"{arguments.stage} or an earlier stage."
            )
            return 2
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
            migration_indexes = _catalog_indexes(connection)
            migration_views = _catalog_views(connection)
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
                if arguments.case_id is None:
                    invariant_results = _run_invariants(
                        root,
                        manifest,
                        connection,
                        scratch_url,
                        runner_dir,
                        arguments.stage,
                        migration_indexes,
                        migration_views,
                    )
                    feature_warning_rows: list[str] = []
                    for invariant_result in invariant_results:
                        if invariant_result.get("kind") != "feature":
                            continue
                        raw_warnings = invariant_result.get("warnings")
                        if isinstance(raw_warnings, list):
                            feature_warning_rows.extend(
                                str(warning) for warning in cast(list[object], raw_warnings)
                            )
                    feature_warning_subjects = _warning_columns(feature_warning_rows)
                    if feature_warning_subjects:
                        for case_index, case in enumerate(selected):
                            if case.get("kind") != "document":
                                continue
                            try:
                                results[case_index] = _run_case(
                                    root,
                                    case,
                                    connection,
                                    scratch_url,
                                    feature_warning_subjects,
                                )
                            except (
                                OSError,
                                ValueError,
                                psycopg.Error,
                                subprocess.SubprocessError,
                            ) as error:
                                results[case_index] = {
                                    "id": case["id"],
                                    "kind": case.get("kind"),
                                    "stage": case.get("stage"),
                                    "group": str(
                                        case.get("group", f"stage-{case.get('stage')}")
                                    ),
                                    "status": "fail",
                                    "expected": "case completes without a runner error",
                                    "actual": str(error),
                                    "differences": [str(error)],
                                }
                    results.extend(invariant_results)
        finally:
            connection.close()
    except (OSError, ValueError, psycopg.Error) as error:
        print(f"Runner setup failed: {error}")
        return 1
    finally:
        if admin is not None and database_name:
            _drop_database(database_name, admin)
    skipped = manifest.get("skips", [])
    if arguments.case_id is None and isinstance(skipped, list):
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
        "warnings": sum(
            len(cast(list[object], item.get("warnings", []))) for item in results
        ),
    }
    for result in results:
        if result.get("kind") != "pyright":
            continue
        actual = result.get("actual")
        if isinstance(actual, dict):
            actual_object = cast(dict[str, object], actual)
            result_document["pyright"] = {
                "errors": actual_object.get("errors"),
                "status": actual_object.get("status"),
            }
        break
    result_path = _save_result(root, result_document)
    print(f"Result file: {result_path.relative_to(root).as_posix()}")
    return _print_results(results, arguments.stage)


if __name__ == "__main__":
    raise SystemExit(main())
