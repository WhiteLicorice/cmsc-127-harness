from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import unittest
from collections.abc import Mapping, Sequence
from contextlib import redirect_stderr, redirect_stdout
import hashlib
from io import StringIO
from types import SimpleNamespace, TracebackType
from typing import Any, Callable, Protocol, cast
from unittest.mock import MagicMock, Mock, patch
from uuid import uuid4

import psycopg
from psycopg import sql as psycopg_sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import runner

from runner import compare_dataset, load_manifest, main, select_cases
from tests.test_lint import make_root as make_starter_root
from tests.test_server import reserved_ports


class _TestCursor(Protocol):
    def execute(
        self: _TestCursor,
        query: str,
        parameters: Sequence[object] | None = None,
    ) -> object: ...

    def fetchall(self: _TestCursor) -> list[tuple[object, ...]]: ...

    def fetchone(self: _TestCursor) -> tuple[object, ...] | None: ...

    def __enter__(self: _TestCursor) -> _TestCursor: ...

    def __exit__(
        self: _TestCursor,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...


class _TestConnection(Protocol):
    def cursor(self: _TestConnection) -> _TestCursor: ...

    def close(self: _TestConnection) -> None: ...


check_feature = cast(
    Callable[[object, Mapping[str, object], Path | None], list[str]],
    getattr(runner, "_check_feature"),
)
run_case = cast(
    Callable[[Path, Mapping[str, object], object, str], dict[str, object]],
    getattr(runner, "_run_case"),
)
sql_error = cast(Callable[[object, str], object], getattr(runner, "_sql_error"))
check_index = cast(
    Callable[[Path, object, Mapping[str, object], set[tuple[str, str]]], list[str]],
    getattr(runner, "_check_index"),
)
check_normal_form = cast(
    Callable[[object, Mapping[str, object]], list[str]],
    getattr(runner, "_check_normal_form"),
)
plan_indexes = cast(
    Callable[[object], set[tuple[str | None, str]]],
    getattr(runner, "_plan_indexes"),
)
check_protected = cast(
    Callable[[Path, Path], list[str]], getattr(runner, "_check_protected")
)
print_results = cast(
    Callable[[Sequence[Mapping[str, object]], int], int],
    getattr(runner, "_print_results"),
)
check_written = cast(
    Callable[[Path, Mapping[str, object]], list[str]],
    getattr(runner, "_check_written"),
)
design_rows = cast(
    Callable[[Path, Mapping[str, object], Sequence[str]], list[str]],
    getattr(runner, "_design_rows"),
)
split_warnings = cast(
    Callable[[Mapping[str, object]], tuple[dict[str, object], list[str]]],
    getattr(runner, "_split_warnings"),
)
run_invariants = cast(
    Callable[
        [
            Path,
            Mapping[str, object],
            object,
            str,
            Path,
            int,
            set[tuple[str, str]],
        ],
        list[dict[str, object]],
    ],
    getattr(runner, "_run_invariants"),
)



class DatasetComparisonTests(unittest.TestCase):
    def test_numeric_formats_and_null_values_compare_by_value(self: DatasetComparisonTests) -> None:
        expected = {"columns": ["amount", "note"], "rows": [[1, None], [2.50, "ok"]]}
        actual = {"columns": ["amount", "note"], "rows": [[1.0, None], [2.5, "ok"]]}

        self.assertEqual([], compare_dataset(expected, actual, ordered=True))

    def test_column_order_is_significant(self: DatasetComparisonTests) -> None:
        expected = {"columns": ["id", "label"], "rows": [[1, "a"]]}
        actual = {"columns": ["label", "id"], "rows": [["a", 1]]}

        differences = compare_dataset(expected, actual, ordered=True)

        self.assertTrue(any("column order" in item for item in differences))

    def test_row_order_can_be_ignored_only_when_declared(self: DatasetComparisonTests) -> None:
        expected = {"columns": ["id"], "rows": [[1], [2]]}
        actual = {"columns": ["id"], "rows": [[2], [1]]}

        self.assertEqual([], compare_dataset(expected, actual, ordered=False))
        self.assertTrue(compare_dataset(expected, actual, ordered=True))


class ResultOutputTests(unittest.TestCase):
    def test_skip_and_failure_keep_each_group_count_separate(
        self: ResultOutputTests,
    ) -> None:
        results: list[dict[str, object]] = [
            {
                "id": "lab1/ok",
                "status": "pass",
                "stage": 1,
                "group": "Lab 1",
                "differences": [],
            },
            {
                "id": "lab2/bad",
                "status": "fail",
                "stage": 2,
                "group": "Lab 2",
                "expected": {"rows": [[1]]},
                "actual": {"rows": [[2]]},
                "differences": ["row 1 differs"],
            },
            {
                "id": "lab2/unavailable",
                "status": "skip",
                "stage": 2,
                "group": "Lab 2",
                "reason": "platform limit",
                "differences": [],
            },
        ]
        output = StringIO()

        with redirect_stdout(output):
            exit_code = print_results(results, 2)

        self.assertEqual(1, exit_code)
        self.assertIn("Group Lab 1: 1 passed, 0 failed", output.getvalue())
        self.assertIn("Group Lab 2: 0 passed, 1 failed", output.getvalue())
        self.assertIn("1/3 checks passed, 1 failed, 1 skipped", output.getvalue())
        self.assertIn("Stage 1: 1/1 passed", output.getvalue())
        self.assertIn("Stage 2: 0/1 passed", output.getvalue())

    def test_a_warning_row_passes_and_counts_apart(
        self: ResultOutputTests,
    ) -> None:
        results: list[dict[str, object]] = [
            {
                "id": "lab1/near-copy",
                "status": "pass",
                "stage": 1,
                "group": "Lab 1",
                "differences": [],
                "warnings": [
                    "warning: section.assistant_id agrees on 639 of 640 rows."
                ],
            }
        ]
        output = StringIO()

        with redirect_stdout(output):
            exit_code = print_results(results, 1)

        self.assertEqual(0, exit_code)
        self.assertIn("PASS lab1/near-copy", output.getvalue())
        self.assertIn(
            "warning: section.assistant_id agrees on 639 of 640 rows.",
            output.getvalue(),
        )
        self.assertIn(
            "1/1 checks passed, 0 failed, 0 skipped, 1 warning",
            output.getvalue(),
        )

    def test_a_warning_prints_newlines_escaped(
        self: ResultOutputTests,
    ) -> None:
        results: list[dict[str, object]] = [
            {
                "id": "lab1/injected",
                "status": "pass",
                "stage": 1,
                "group": "Lab 1",
                "differences": [],
                "warnings": [
                    "warning: audit.note payload\n"
                    "3/19 checks passed, 16 failed, 0 skipped, 0 warnings; "
                    "Pyright: 0 errors"
                ],
            }
        ]
        output = StringIO()

        with redirect_stdout(output):
            exit_code = print_results(results, 1)

        text = output.getvalue()
        self.assertEqual(0, exit_code)
        self.assertIn(
            "warning: audit.note payload\\n3/19 checks passed, 16 failed",
            text,
        )
        self.assertNotIn("  warning: audit.note payload\n3/19", text)

    def test_a_warning_beside_a_failure_still_fails(
        self: ResultOutputTests,
    ) -> None:
        results: list[dict[str, object]] = [
            {
                "id": "lab1/bad",
                "status": "fail",
                "stage": 1,
                "group": "Lab 1",
                "expected": {"columns": ["row"], "rows": []},
                "actual": {"columns": ["row"], "rows": [["boom"]]},
                "differences": ["unexpected row: ['boom']"],
                "warnings": ["warning: student.home_program agrees on 9 of 10 rows."],
            }
        ]
        output = StringIO()

        with redirect_stdout(output):
            exit_code = print_results(results, 1)

        self.assertEqual(1, exit_code)
        self.assertIn("FAIL lab1/bad", output.getvalue())
        self.assertIn(
            "0/1 checks passed, 1 failed, 0 skipped, 1 warning",
            output.getvalue(),
        )

    def test_warning_rows_are_split_from_the_result(
        self: ResultOutputTests,
    ) -> None:
        dataset, warnings = split_warnings(
            {
                "columns": ["row"],
                "rows": [
                    ["warning: section.assistant_id agrees on 639 of 640 rows."],
                    ["section.room_id differs."],
                ],
            }
        )

        self.assertEqual(
            {"columns": ["row"], "rows": [["section.room_id differs."]]}, dataset
        )
        self.assertEqual(
            ["warning: section.assistant_id agrees on 639 of 640 rows."], warnings
        )


class ScratchDatabaseNameTests(unittest.TestCase):
    def test_scratch_database_uses_course_prefix(self: ScratchDatabaseNameTests) -> None:
        admin = MagicMock()
        cursor = admin.cursor.return_value.__enter__.return_value
        guard = runner._StopGuard()  # pyright: ignore[reportPrivateUsage]
        with (
            patch.object(runner, "uuid4", return_value=Mock(hex="0123456789abcdef")),
            patch.object(runner.psycopg, "connect", return_value=admin),
        ):
            name, connection = runner._create_database(  # pyright: ignore[reportPrivateUsage]
                "postgresql://postgres@127.0.0.1/postgres", guard
            )

        self.assertEqual("cmsc127_race_0123456789abcdef", name)
        self.assertIs(admin, connection)
        cursor.execute.assert_called_once_with(f'CREATE DATABASE "{name}"', None)

    def test_scratch_database_holds_a_stop_until_the_caller_owns_the_name(
        self: ScratchDatabaseNameTests,
    ) -> None:
        """The create section runs in the cleanup phase, so a stop is held."""
        admin = MagicMock()
        guard = runner._StopGuard()  # pyright: ignore[reportPrivateUsage]
        with (
            patch.object(runner, "uuid4", return_value=Mock(hex="0123456789abcdef")),
            patch.object(runner.psycopg, "connect", return_value=admin),
        ):
            name, _ = runner._create_database(  # pyright: ignore[reportPrivateUsage]
                "postgresql://postgres@127.0.0.1/postgres", guard
            )

        self.assertEqual("cleanup", guard.phase)
        self.assertEqual("cmsc127_race_0123456789abcdef", name)


class ManifestTests(unittest.TestCase):
    def make_pair(self: ManifestTests, root: Path) -> None:
        (root / "cases" / "lab1").mkdir(parents=True)
        (root / "cases" / "manifest.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "cases": [
                        {
                            "id": "lab1/list",
                            "stage": 1,
                            "kind": "output",
                            "file": "cases/lab1/list.sql",
                            "ordered": True,
                        },
                        {
                            "id": "lab2/summary",
                            "stage": 2,
                            "kind": "query",
                            "file": "cases/lab1/list.sql",
                            "ordered": True,
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        (root / "cases" / "lab1" / "list.sql").write_text("SELECT 1;\n", encoding="utf-8")
        (root / "cases" / "lab1" / "list.expected").write_text(
            '{"columns":["?column?"],"rows":[[1]]}\n', encoding="utf-8"
        )

    def add_published_stage(self: ManifestTests, root: Path, stage: int) -> None:
        published = root / "cases" / "published" / f"lab{stage}"
        published.mkdir(parents=True)
        (published / "manifest.json").write_text(
            json.dumps({"version": 1, "cases": [{"stage": stage}]}),
            encoding="utf-8",
        )

    def test_reject_case_requires_one_actual_insert(self: ManifestTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_pair(root)
            manifest_path = root / "cases" / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["cases"].append(
                {
                    "id": "lab1/reject",
                    "stage": 1,
                    "kind": "reject",
                    "file": "cases/lab1/reject.sql",
                }
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            reject_path = root / "cases" / "lab1" / "reject.sql"
            reject_path.with_suffix(".expected").write_text(
                '{"sqlstate":"23505"}\n', encoding="utf-8"
            )

            for sql in (
                "DO $$ BEGIN RAISE EXCEPTION 'fake' USING ERRCODE = '23505'; END $$;\n",
                "INSERT INTO item VALUES (1, 'x'); DO $$ BEGIN RAISE EXCEPTION 'fake' USING ERRCODE = '23505'; END $$;\n",
            ):
                reject_path.write_text(sql, encoding="utf-8")
                with self.assertRaisesRegex(
                    ValueError, r"cases/lab1/reject\.sql.*one plain"
                ):
                    load_manifest(root)

            reject_path.write_text(
                "-- A real constraint must reject this row.\n"
                "INSERT INTO item (id, label) VALUES (1, 'x');\n",
                encoding="utf-8",
            )
            self.assertEqual(
                "lab1/reject", select_cases(load_manifest(root), 1)[-1].get("id")
            )
            reject_path.write_text(
                "INSERT INTO item (id, label) VALUES (1, 'x;y');\n", encoding="utf-8"
            )
            self.assertEqual(
                "lab1/reject", select_cases(load_manifest(root), 1)[-1].get("id")
            )

    def test_reject_role_case_requires_privilege_sqlstate(self: ManifestTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_pair(root)
            manifest_path = root / "cases" / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["cases"].append(
                {
                    "id": "lab1/role-reject",
                    "stage": 1,
                    "kind": "reject",
                    "file": "cases/lab1/reject.sql",
                    "role": "cashier",
                    "role_env": "CMSC127_ROLE_CASHIER",
                }
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            reject_path = root / "cases" / "lab1" / "reject.sql"
            reject_path.write_text(
                "INSERT INTO item (id) VALUES (1);\n", encoding="utf-8"
            )
            reject_path.with_suffix(".expected").write_text(
                '{"sqlstate":"23505"}\n', encoding="utf-8"
            )

            with self.assertRaisesRegex(ValueError, "role.*42501"):
                load_manifest(root)

            expected_path = reject_path.with_suffix(".expected")
            expected_path.write_text('{"sqlstate":"42501"}\n', encoding="utf-8")
            case = select_cases(load_manifest(root), 1)[-1]
            with patch.dict(os.environ, {"CMSC127_ROLE_CASHIER": "c127_verify_cashier"}):
                self.assertEqual("c127_verify_cashier", runner._case_role(case))  # pyright: ignore[reportPrivateUsage]

    def test_allow_case_needs_a_role_and_a_success_sqlstate(
        self: ManifestTests,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_pair(root)
            manifest_path = root / "cases" / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["cases"].append(
                {
                    "id": "lab1/role-allow",
                    "stage": 1,
                    "kind": "allow",
                    "file": "cases/lab1/allow.sql",
                }
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            allow_path = root / "cases" / "lab1" / "allow.sql"
            allow_path.write_text(
                "INSERT INTO item (id) VALUES (1);\n", encoding="utf-8"
            )
            allow_path.with_suffix(".expected").write_text(
                '{"sqlstate":"00000"}\n', encoding="utf-8"
            )

            with self.assertRaisesRegex(ValueError, "allow case needs a role"):
                load_manifest(root)

            manifest["cases"][-1]["role"] = "cashier"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            allow_path.with_suffix(".expected").write_text(
                '{"sqlstate":"42501"}\n', encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "00000"):
                load_manifest(root)

            allow_path.with_suffix(".expected").write_text(
                '{"sqlstate":"00000"}\n', encoding="utf-8"
            )
            case = select_cases(load_manifest(root), 1)[-1]
            self.assertEqual("lab1/role-allow", case.get("id"))

    def test_reject_case_requires_one_plain_row(self: ManifestTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_pair(root)
            manifest_path = root / "cases" / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["cases"].append(
                {
                    "id": "lab1/reject",
                    "stage": 1,
                    "kind": "reject",
                    "file": "cases/lab1/reject.sql",
                }
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            reject_path = root / "cases" / "lab1" / "reject.sql"
            reject_path.with_suffix(".expected").write_text(
                '{"sqlstate":"23505"}\n', encoding="utf-8"
            )

            for sql in (
                "WITH x AS (INSERT INTO item VALUES (1, 'x') RETURNING 1)\n"
                "SELECT * FROM x;\n",
                "INSERT INTO item (id) SELECT 1;\n",
                "INSERT INTO item (id) VALUES (1), (2);\n",
                "INSERT INTO item (id) VALUES (nextval('item_seq'));\n",
                "INSERT INTO item (id) VALUES ((SELECT 1));\n",
                "INSERT INTO item (id) VALUES (1);\n"
                "INSERT INTO item (id) VALUES (2);\n",
            ):
                reject_path.write_text(sql, encoding="utf-8")
                with self.assertRaisesRegex(
                    ValueError, r"cases/lab1/reject\.sql.*one plain"
                ):
                    load_manifest(root)

            for sql in (
                "-- A real constraint must reject this row.\n"
                "INSERT INTO item (id, label) VALUES (1, 'x');\n",
                "INSERT INTO item (id, label) VALUES (1, 'x;y');\n",
                "INSERT INTO item (id, label, at) VALUES (1, 'x', DATE '2024-08-01');\n",
                "INSERT INTO item (id, label) VALUES (-5, NULL);\n",
                "INSERT INTO item VALUES (1, 'x');\n",
            ):
                reject_path.write_text(sql, encoding="utf-8")
                self.assertEqual(
                    "lab1/reject", select_cases(load_manifest(root), 1)[-1].get("id")
                )

    def test_manifest_rejects_unlisted_case_files(self: ManifestTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_pair(root)
            (root / "cases" / "lab1" / "extra.sql").write_text("SELECT 2;\n", encoding="utf-8")
            (root / "cases" / "lab1" / "extra.expected").write_text("{}\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "unlisted case file.*extra.sql"):
                load_manifest(root)

    def test_published_and_pair_manifests_can_coexist(self: ManifestTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_pair(root)
            cases = root / "cases"
            pair = cases / "pair"
            pair.mkdir()
            lab1 = cases / "published" / "lab1"
            lab2 = cases / "published" / "lab2"
            lab1.mkdir(parents=True)
            lab2.mkdir(parents=True)
            (pair / "manifest.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "cases": [
                            {
                                "id": "pair/later",
                                "stage": 3,
                                "kind": "query",
                                "file": "cases/pair/later.sql",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (lab1 / "manifest.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "cases": [
                            {
                                "id": "lab1/erd",
                                "stage": 1,
                                "kind": "schema",
                                "file": "cases/pair/erd.md",
                                "expected_file": "cases/published/lab1/erd.expected",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (lab2 / "manifest.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "cases": [
                            {
                                "id": "lab2/published",
                                "stage": 2,
                                "kind": "query",
                                "file": "cases/published/lab2/report.sql",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (pair / "erd.md").write_text("```mermaid\nerDiagram\n```\n", encoding="utf-8")
            (lab1 / "erd.expected").write_text(
                '{"columns":["difference"],"rows":[]}\n', encoding="utf-8"
            )
            (lab2 / "report.sql").write_text("SELECT 2;\n", encoding="utf-8")
            (lab2 / "report.expected").write_text(
                '{"columns":["?column?"],"rows":[[2]]}\n', encoding="utf-8"
            )
            (pair / "later.sql").write_text("SELECT 3;\n", encoding="utf-8")
            (pair / "later.expected").write_text(
                '{"columns":["?column?"],"rows":[[3]]}\n', encoding="utf-8"
            )

            manifest = load_manifest(root)
            erd_case = next(
                case
                for case in cast(list[dict[str, object]], manifest["cases"])
                if case["id"] == "lab1/erd"
            )
            self.assertEqual(
                root / "cases/pair/erd.md",
                runner._case_file(root, erd_case),  # pyright: ignore[reportPrivateUsage]
            )
            self.assertEqual(
                lab1 / "erd.expected",
                runner._case_expected_file(root, erd_case),  # pyright: ignore[reportPrivateUsage]
            )
            self.assertTrue(
                {"lab1/erd", "lab2/published", "pair/later"}
                <= {str(case["id"]) for case in cast(list[dict[str, object]], manifest["cases"])}
            )
            selected_ids = {
                str(case["id"])
                for case in select_cases(manifest, 3)
            }
            self.assertTrue(
                {"lab1/erd", "lab2/published", "pair/later"} <= selected_ids
            )

    def test_manifest_rejects_orphan_expected_sidecars(self: ManifestTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_pair(root)
            (root / "cases" / "lab1" / "orphan.expected").write_text(
                '{"columns":[],"rows":[]}\n', encoding="utf-8"
            )

            with self.assertRaisesRegex(ValueError, "unlisted expected or .exit file.*orphan.expected"):
                load_manifest(root)

    def test_duplicate_id_names_existing_manifest(self: ManifestTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = root / "cases"
            published = cases / "published" / "lab2"
            pair = cases / "pair"
            published.mkdir(parents=True)
            pair.mkdir()
            (cases / "manifest.json").write_text(
                '{"version":1,"cases":[]}\n', encoding="utf-8"
            )
            case = {
                "id": "lab2/health",
                "stage": 2,
                "kind": "query",
                "file": "cases/published/lab2/health.sql",
            }
            (published / "manifest.json").write_text(
                json.dumps({"version": 1, "cases": [case]}), encoding="utf-8"
            )
            (published / "health.sql").write_text("SELECT 1;\n", encoding="utf-8")
            (published / "health.expected").write_text(
                '{"columns":["?column?"],"rows":[[1]]}\n', encoding="utf-8"
            )
            pair_case = {
                **case,
                "stage": 3,
                "file": "cases/pair/health.sql",
            }
            (pair / "manifest.json").write_text(
                json.dumps({"version": 1, "cases": [pair_case]}), encoding="utf-8"
            )
            (pair / "health.sql").write_text("SELECT 1;\n", encoding="utf-8")
            (pair / "health.expected").write_text(
                '{"columns":["?column?"],"rows":[[1]]}\n', encoding="utf-8"
            )

            with self.assertRaisesRegex(
                ValueError,
                r"cases/pair/manifest\.json: line 1.*already defined in "
                r"cases/published/lab2/manifest\.json",
            ):
                load_manifest(root)

    def test_stage_selection_keeps_earlier_stages(self: ManifestTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_pair(root)

            manifest = load_manifest(root)

            self.assertEqual(
                ["lab1/list", "lab2/summary"],
                [case["id"] for case in select_cases(manifest, 2)],
            )
            self.assertEqual(["lab1/list"], [case["id"] for case in select_cases(manifest, 1)])

    def test_stage_selector_reads_stage_file(self: ManifestTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_pair(root)
            self.add_published_stage(root, 2)
            (root / ".stage").write_text(" \t2 \r\n", encoding="utf-8")

            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(["--select-stage", "--root", str(root)])

            self.assertEqual(0, exit_code)
            self.assertEqual("2\n", output.getvalue())

    def test_stage_selector_reports_form_faults(self: ManifestTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_pair(root)
            self.add_published_stage(root, 2)
            for stage_text, expected in (
                (None, "Form fault: .stage is missing."),
                ("x\n", "Form fault: .stage must be a positive integer."),
                ("0\n", "Form fault: .stage must be a positive integer."),
                (
                    "3\n",
                    "Form fault: .stage must not exceed the highest published stage (2).",
                ),
            ):
                with self.subTest(stage_text=stage_text):
                    stage_path = root / ".stage"
                    if stage_text is None:
                        stage_path.unlink(missing_ok=True)
                    else:
                        stage_path.write_text(stage_text, encoding="utf-8")
                    output = StringIO()
                    with redirect_stdout(output):
                        exit_code = main(["--select-stage", "--root", str(root)])

                    self.assertEqual(2, exit_code)
                    self.assertEqual(f"{expected}\n", output.getvalue())

    def test_stage_file_is_an_allowed_student_path(self: ManifestTests) -> None:
        protected_path = Path(__file__).resolve().parents[1] / "protected.json"
        protected = json.loads(protected_path.read_text(encoding="utf-8"))

        self.assertIn(".stage", protected["allowed_paths"])

    def test_protected_paths_check_committed_and_untracked_files(self: ManifestTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "fork"
            runner_dir = Path(directory) / "runner"
            pair = root / "cases" / "pair"
            pair.mkdir(parents=True)
            runner_dir.mkdir()
            (root / "check.sh").write_bytes(b"published\n")
            (pair / "answer.sql").write_text("SELECT 1;\n", encoding="utf-8")
            (runner_dir / "protected.json").write_text(
                json.dumps(
                    {
                        "files": {
                            "check.sh": hashlib.sha256(b"published\n").hexdigest()
                        },
                        "allowed_paths": ["cases/pair"],
                    }
                ),
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)
            subprocess.run(
                ["git", "-C", str(root), "config", "core.autocrlf", "false"],
                check=True,
            )
            subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "--quiet",
                    "-m",
                    "clean fork",
                ],
                check=True,
            )

            self.assertEqual([], check_protected(root, runner_dir))

            committed_path = root / "committed file.sql"
            committed_path.write_text("SELECT 2;\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "--quiet",
                    "-m",
                    "outside file",
                ],
                check=True,
            )

            errors = check_protected(root, runner_dir)
            self.assertIn(
                "file is outside the published writable paths: committed file.sql",
                errors,
            )

            untracked_path = root / "untracked file.sql"
            untracked_path.write_text("SELECT 3;\n", encoding="utf-8")
            errors = check_protected(root, runner_dir)
            self.assertIn(
                "file is outside the published writable paths: untracked file.sql",
                errors,
            )

    def test_stage_selector_uses_stage_file_within_published_range(self: ManifestTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_pair(root)
            lab3 = root / "cases" / "published" / "lab3"
            lab3.mkdir(parents=True)
            (lab3 / "manifest.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "cases": [
                            {
                                "id": "lab3/design",
                                "stage": 3,
                                "kind": "query",
                                "file": "cases/published/lab3/design.sql",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (lab3 / "design.sql").write_text("SELECT 1;\n", encoding="utf-8")
            (lab3 / "design.expected").write_text(
                '{"columns":["?column?"],"rows":[[1]]}\n', encoding="utf-8"
            )
            (root / ".stage").write_text("3\n", encoding="utf-8")

            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(["--select-stage", "--root", str(root)])
            self.assertEqual(0, exit_code)
            self.assertEqual("3\n", output.getvalue())

            empty_root = root / "empty"
            empty_root.mkdir()
            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(["--select-stage", "--root", str(empty_root)])
            self.assertEqual(2, exit_code)
            self.assertEqual("Form fault: .stage is missing.\n", output.getvalue())

    def test_later_published_case_replaces_earlier_case(self: ManifestTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_pair(root)
            cases = root / "cases"
            lab1 = cases / "published" / "lab1"
            lab3 = cases / "published" / "lab3"
            pair = cases / "pair"
            lab1.mkdir(parents=True)
            lab3.mkdir(parents=True)
            pair.mkdir()
            for folder, case_id, stage in (
                (lab1, "lab1/design", 1),
                (lab3, "lab3/design", 3),
            ):
                case_file = f"cases/published/{folder.name}/{case_id.split('/')[-1]}.sql"
                case: dict[str, object] = {
                    "id": case_id,
                    "stage": stage,
                    "kind": "query",
                    "file": case_file,
                }
                if stage == 3:
                    case["replaces"] = "lab1/design"
                (folder / "manifest.json").write_text(
                    json.dumps({"version": 1, "cases": [case]}), encoding="utf-8"
                )
                (folder / f"{case_id.split('/')[-1]}.sql").write_text(
                    "SELECT 1;\n", encoding="utf-8"
                )
                (folder / f"{case_id.split('/')[-1]}.expected").write_text(
                    '{"columns":["?column?"],"rows":[[1]]}\n', encoding="utf-8"
                )
            (pair / "manifest.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "cases": [
                            {
                                "id": "pair/future",
                                "stage": 4,
                                "kind": "query",
                                "file": "cases/pair/future.sql",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (pair / "future.sql").write_text("SELECT 4;\n", encoding="utf-8")
            (pair / "future.expected").write_text(
                '{"columns":["?column?"],"rows":[[4]]}\n', encoding="utf-8"
            )

            manifest = load_manifest(root)
            stage_two_ids = {str(case["id"]) for case in select_cases(manifest, 2)}
            stage_three_ids = {str(case["id"]) for case in select_cases(manifest, 3)}
            self.assertIn("lab1/design", stage_two_ids)
            self.assertNotIn("lab3/design", stage_two_ids)
            self.assertNotIn("lab1/design", stage_three_ids)
            self.assertIn("lab3/design", stage_three_ids)
            self.assertNotIn("pair/future", stage_three_ids)

    def test_bad_json_reports_line_and_column(self: ManifestTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cases").mkdir()
            (root / "cases" / "manifest.json").write_text('{\n  "version": 1,\n  bad\n}', encoding="utf-8")

            with self.assertRaisesRegex(ValueError, r"manifest.json: line 3, column 3"):
                load_manifest(root)

    def test_invalid_manifest_value_reports_its_line_and_column(
        self: ManifestTests,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cases").mkdir()
            (root / "cases" / "manifest.json").write_bytes(
                b'{\n  "version": 2,\n  "cases": []\n}\n'
            )

            with self.assertRaisesRegex(ValueError, r"manifest.json: line 2, column 14"):
                load_manifest(root)

    def test_invalid_expected_value_reports_its_line_and_column(
        self: ManifestTests,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_pair(root)
            (root / "cases" / "lab1" / "list.expected").write_bytes(
                b'{\n  "columns": [],\n  "rows": "bad"\n}\n'
            )

            with self.assertRaisesRegex(
                ValueError, r"list.expected: line 3, column 11"
            ):
                load_manifest(root)

    def test_report_case_needs_a_report_string(self: ManifestTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cases").mkdir()
            (root / "cases" / "manifest.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "cases": [
                            {
                                "id": "lab2/report",
                                "stage": 2,
                                "kind": "report",
                                "file": "cases/report.json",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (root / "cases" / "report.json").write_text(
                json.dumps({"report": 1}), encoding="utf-8"
            )
            (root / "cases" / "report.expected").write_text(
                '{"columns":[],"rows":[]}\n', encoding="utf-8"
            )

            with self.assertRaisesRegex(ValueError, r"report must be a string"):
                load_manifest(root)


class DesignRecordTests(unittest.TestCase):
    CONFIG: dict[str, object] = {
        "file": "DESIGN.md",
        "headings": ["Tables", "Keys and Links", "Checker Warnings"],
        "placeholders": {
            "Tables": "Write one bullet per table.",
            "Keys and Links": "Describe how the tables connect.",
            "Checker Warnings": "Write one entry for each warning.",
        },
    }

    def write(self: DesignRecordTests, root: Path, text: str) -> None:
        (root / "DESIGN.md").write_text(text, encoding="utf-8")

    def test_a_complete_record_passes(self: DesignRecordTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(
                root,
                "# Design\n\n## Tables\n\n- student holds a name.\n\n"
                "## Keys and Links\n\n- charge links to student.\n\n"
                "## Checker Warnings\n\nNo warning prints.\n",
            )

            self.assertEqual([], design_rows(root, self.CONFIG, []))

    def test_a_missing_file_is_named(self: DesignRecordTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rows = design_rows(Path(directory), self.CONFIG, [])

            self.assertEqual(1, len(rows))
            self.assertIn("DESIGN.md is missing", rows[0])

    def test_a_missing_heading_is_named(self: DesignRecordTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(
                root,
                "# Design\n\n## Tables\n\n- student.\n\n"
                "## Checker Warnings\n\nNo warning prints.\n",
            )

            rows = design_rows(root, self.CONFIG, [])

            self.assertEqual(1, len(rows))
            self.assertIn('missing the heading "## Keys and Links"', rows[0])

    def test_out_of_order_headings_are_named(self: DesignRecordTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(
                root,
                "# Design\n\n## Keys and Links\n\n- charge.\n\n"
                "## Tables\n\n- student.\n\n"
                "## Checker Warnings\n\nNo warning prints.\n",
            )

            rows = design_rows(root, self.CONFIG, [])

            self.assertEqual(1, len(rows))
            self.assertIn("out of order", rows[0])

    def test_an_empty_section_is_named(self: DesignRecordTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(
                root,
                "# Design\n\n## Tables\n\n## Keys and Links\n\n- charge.\n\n"
                "## Checker Warnings\n\nNo warning prints.\n",
            )

            rows = design_rows(root, self.CONFIG, [])

            self.assertEqual(1, len(rows))
            self.assertIn('nothing under "## Tables"', rows[0])

    def test_a_placeholder_line_is_named(self: DesignRecordTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(
                root,
                "# Design\n\n## Tables\n\n- Write one bullet per table.\n\n"
                "## Keys and Links\n\n- charge.\n\n"
                "## Checker Warnings\n\nNo warning prints.\n",
            )

            rows = design_rows(root, self.CONFIG, [])

            self.assertEqual(1, len(rows))
            self.assertIn('placeholder line under "## Tables"', rows[0])

    def test_an_unexplained_warning_is_named(self: DesignRecordTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(
                root,
                "# Design\n\n## Tables\n\n- student.\n\n"
                "## Keys and Links\n\n- charge.\n\n"
                "## Checker Warnings\n\nNo warning prints.\n",
            )

            rows = design_rows(root, self.CONFIG, ["section.assistant_id"])

            self.assertEqual(1, len(rows))
            self.assertIn('"### section.assistant_id"', rows[0])

    def test_an_explained_warning_passes(self: DesignRecordTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(
                root,
                "# Design\n\n## Tables\n\n- student.\n\n"
                "## Keys and Links\n\n- charge.\n\n"
                "## Checker Warnings\n\n"
                "### section.assistant_id\n\n"
                "It is a separate fact: the assistant is a second instructor.\n",
            )

            self.assertEqual(
                [], design_rows(root, self.CONFIG, ["section.assistant_id"])
            )

    def test_a_report_path_warning_heading_passes(self: DesignRecordTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(
                root,
                "# Design\n\n## Tables\n\n- student.\n\n"
                "## Keys and Links\n\n- charge.\n\n"
                "## Checker Warnings\n\n"
                "### `reports/room_assignments.sql`\n\n"
                "The query uses CASE for NULL handling.\n",
            )

            self.assertEqual(
                [], design_rows(root, self.CONFIG, ["reports/room_assignments.sql"])
            )

    def test_a_trailing_annotation_on_the_entry_heading_passes(
        self: DesignRecordTests,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(
                root,
                "# Design\n\n## Tables\n\n- student.\n\n"
                "## Keys and Links\n\n- charge.\n\n"
                "## Checker Warnings\n\n"
                "### section.assistant_id (copy)\n\n"
                "It is a copy.\n",
            )

            self.assertEqual(
                [], design_rows(root, self.CONFIG, ["section.assistant_id"])
            )

    def test_bold_marks_around_the_entry_heading_pass(
        self: DesignRecordTests,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(
                root,
                "# Design\n\n## Tables\n\n- student.\n\n"
                "## Keys and Links\n\n- charge.\n\n"
                "## Checker Warnings\n\n"
                "### **section.assistant_id**\n\n"
                "It is a copy.\n",
            )

            self.assertEqual(
                [], design_rows(root, self.CONFIG, ["section.assistant_id"])
            )

    def test_backticks_around_the_entry_heading_pass(
        self: DesignRecordTests,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(
                root,
                "# Design\n\n## Tables\n\n- student.\n\n"
                "## Keys and Links\n\n- charge.\n\n"
                "## Checker Warnings\n\n"
                "### `section.assistant_id`\n\n"
                "It is a copy.\n",
            )

            self.assertEqual(
                [], design_rows(root, self.CONFIG, ["section.assistant_id"])
            )

    def test_a_level_four_entry_heading_names_the_level(
        self: DesignRecordTests,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(
                root,
                "# Design\n\n## Tables\n\n- student.\n\n"
                "## Keys and Links\n\n- charge.\n\n"
                "## Checker Warnings\n\n"
                "#### section.assistant_id\n\n"
                "It is a copy.\n",
            )

            rows = design_rows(root, self.CONFIG, ["section.assistant_id"])

            self.assertEqual(1, len(rows))
            self.assertIn("heading level 4", rows[0])


@unittest.skipUnless(os.environ.get("CMSC127_DATABASE_URL"), "requires PostgreSQL")
class RunnerIntegrationTests(unittest.TestCase):
    def test_role_reject_uses_a_temporary_probe_role(
        self: RunnerIntegrationTests,
    ) -> None:
        group_role = f"c127_race_group_{uuid4().hex}"
        database_url = os.environ["CMSC127_DATABASE_URL"]
        with psycopg.connect(database_url, autocommit=True) as admin:
            with admin.cursor() as cursor:
                cursor.execute(
                    psycopg_sql.SQL("CREATE ROLE {} NOLOGIN").format(
                        psycopg_sql.Identifier(group_role)
                    )
                )
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "cases" / "lab1").mkdir(parents=True)
                (root / "migrations").mkdir()
                (root / "cases" / "manifest.json").write_text(
                    json.dumps(
                        {
                            "version": 1,
                            "cases": [
                                {
                                    "id": "lab1/role-reject",
                                    "stage": 1,
                                    "kind": "reject",
                                    "file": "cases/lab1/role-reject.sql",
                                    "role": group_role,
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                reject_path = root / "cases" / "lab1" / "role-reject.sql"
                reject_path.write_text(
                    "INSERT INTO item (id, label) VALUES (1, 'allowed');\n",
                    encoding="utf-8",
                )
                reject_path.with_suffix(".expected").write_text(
                    '{"sqlstate":"42501"}\n', encoding="utf-8"
                )
                (root / "migrations" / "0001_create_item.sql").write_text(
                    "CREATE TABLE item (id integer PRIMARY KEY, label text);\n",
                    encoding="utf-8",
                )
                (root / "load.sql").write_text("", encoding="utf-8")
                (root / "run").write_text(
                    "#!/usr/bin/env bash\n"
                    "set -eu\n"
                    'if [ "$1" = prepare ]; then\n'
                    '  psql "$CMSC127_DATABASE_URL" --set ON_ERROR_STOP=1 --file load.sql\n'
                    "fi\n",
                    encoding="utf-8",
                    newline="\n",
                )

                output = StringIO()
                with redirect_stdout(output):
                    exit_code = main(["1", "--root", str(root)])

                self.assertEqual(0, exit_code, output.getvalue())
                (root / "migrations" / "0001_create_item.sql").write_text(
                    "CREATE TABLE item (id integer PRIMARY KEY, label text);\n"
                    f'GRANT INSERT ON item TO "{group_role}";\n',
                    encoding="utf-8",
                )
                output = StringIO()
                with redirect_stdout(output):
                    exit_code = main(["1", "--root", str(root)])

                self.assertEqual(1, exit_code, output.getvalue())
                result = json.loads(
                    (root / ".cmsc127" / "last-run.json").read_text(
                        encoding="utf-8"
                    )
                )
                rows = cast(list[dict[str, object]], result["results"])
                self.assertEqual("fail", rows[0]["status"])
        finally:
            with psycopg.connect(database_url, autocommit=True) as admin:
                with admin.cursor() as cursor:
                    cursor.execute(
                        psycopg_sql.SQL("DROP ROLE IF EXISTS {}").format(
                            psycopg_sql.Identifier(group_role)
                        )
                    )

    def test_allow_case_checks_the_granted_insert_and_rolls_back(
        self: RunnerIntegrationTests,
    ) -> None:
        group_role = f"c127_allow_group_{uuid4().hex[:12]}"
        table = f"t13_allow_{uuid4().hex[:8]}"
        database_url = os.environ["CMSC127_DATABASE_URL"]
        with psycopg.connect(database_url, autocommit=True) as admin:
            with admin.cursor() as cursor:
                cursor.execute(
                    psycopg_sql.SQL("CREATE ROLE {} NOLOGIN").format(
                        psycopg_sql.Identifier(group_role)
                    )
                )
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "cases" / "lab1").mkdir(parents=True)
                case_path = root / "cases" / "lab1" / "role-allow.sql"
                case_path.write_text(
                    f'INSERT INTO "{table}" (id) VALUES (1);\n',
                    encoding="utf-8",
                )
                case_path.with_suffix(".expected").write_text(
                    '{"sqlstate":"00000"}\n', encoding="utf-8"
                )
                case = {
                    "id": "lab1/role-allow",
                    "stage": 1,
                    "kind": "allow",
                    "file": "cases/lab1/role-allow.sql",
                    "role": group_role,
                    "role_env": "CMSC127_ROLE_ALLOW_TEST",
                }
                connection = cast(
                    runner._DbConnection,  # pyright: ignore[reportPrivateUsage]
                    psycopg.connect(database_url, autocommit=True),
                )
                try:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            f'CREATE TABLE "{table}" (id integer PRIMARY KEY)'
                        )
                        cursor.execute(
                            f'GRANT INSERT ON "{table}" TO "{group_role}"'
                        )
                    with patch.dict(
                        os.environ, {"CMSC127_ROLE_ALLOW_TEST": group_role}
                    ):
                        result = runner._run_case(  # pyright: ignore[reportPrivateUsage]
                            root, case, connection, database_url
                        )
                    self.assertEqual("pass", result["status"], result)
                    with connection.cursor() as cursor:
                        cursor.execute(f'SELECT count(*) FROM "{table}"')
                        row = cursor.fetchone()
                    self.assertEqual((0,), row)

                    with connection.cursor() as cursor:
                        cursor.execute(
                            f'REVOKE INSERT ON "{table}" FROM "{group_role}"'
                        )
                    with patch.dict(
                        os.environ, {"CMSC127_ROLE_ALLOW_TEST": group_role}
                    ):
                        result = runner._run_case(  # pyright: ignore[reportPrivateUsage]
                            root, case, connection, database_url
                        )
                    self.assertEqual("fail", result["status"], result)
                    self.assertTrue(
                        any(
                            "needs the grant" in str(difference)
                            for difference in cast(
                                list[object], result["differences"]
                            )
                        ),
                        result,
                    )
                finally:
                    connection.close()
        finally:
            # The fixture table goes before the role. A later run against the
            # same database would otherwise find the table in its exact public
            # schema comparison. The comparator would report it.
            with psycopg.connect(database_url, autocommit=True) as admin:
                with admin.cursor() as cursor:
                    cursor.execute(
                        psycopg_sql.SQL("DROP TABLE IF EXISTS public.{}").format(
                            psycopg_sql.Identifier(table)
                        )
                    )
                    cursor.execute(
                        psycopg_sql.SQL("DROP ROLE IF EXISTS {}").format(
                            psycopg_sql.Identifier(group_role)
                        )
                    )

    def test_state_case_restores_before_checking_rows(
        self: RunnerIntegrationTests,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cases" / "lab1").mkdir(parents=True)
            (root / "migrations").mkdir()
            (root / "cases" / "manifest.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "cases": [
                            {
                                "id": "lab1/restore-state",
                                "stage": 1,
                                "kind": "state",
                                "file": "cases/lab1/restore-state.json",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (root / "cases" / "lab1" / "restore-state.json").write_text(
                json.dumps(
                    {
                        "operation": "restore",
                        "check_sql": "SELECT count(*) AS total FROM item",
                    }
                ),
                encoding="utf-8",
            )
            (root / "cases" / "lab1" / "restore-state.expected").write_text(
                '{"columns":["total"],"rows":[[2]]}\n', encoding="utf-8"
            )
            (root / "migrations" / "0001_create_item.sql").write_text(
                "CREATE TABLE item (id integer PRIMARY KEY, label text);\n",
                encoding="utf-8",
            )
            (root / "load.sql").write_text(
                "INSERT INTO item VALUES (1, 'one'), (2, 'two');\n",
                encoding="utf-8",
            )
            (root / "run").write_text(
                "#!/usr/bin/env bash\n"
                "set -eu\n"
                'if [ "$1" = prepare ]; then\n'
                '  psql "$CMSC127_DATABASE_URL" --set ON_ERROR_STOP=1 --file load.sql\n'
                "else\n"
                "  exit 2\n"
                "fi\n",
                encoding="utf-8",
                newline="\n",
            )

            output = StringIO()
            with (
                redirect_stdout(output),
                patch.object(runner, "_backup_restore", return_value=[]) as restore,
            ):
                exit_code = main(["1", "--root", str(root)])

            self.assertEqual(0, exit_code, output.getvalue())
            restore.assert_called_once()

    def test_mapped_reject_reads_the_table_and_columns_from_the_mapping(
        self: RunnerIntegrationTests,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cases" / "lab1").mkdir(parents=True)
            (root / "migrations").mkdir()
            (root / "cases" / "manifest.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "cases": [
                            {
                                "id": "lab1/receipt",
                                "stage": 1,
                                "kind": "reject",
                                "file": "cases/lab1/receipt.sql",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (root / "cases" / "lab1" / "receipt.sql").write_text(
                "-- The checks read the names from answers/column_mapping.sql.\n"
                "INSERT INTO mapped_table (shelf_id, shelf_code) VALUES (1, NULL);\n",
                encoding="utf-8",
            )
            (root / "cases" / "lab1" / "receipt.expected").write_text(
                '{"sqlstate":"23502"}\n', encoding="utf-8"
            )
            migration = root / "migrations" / "0001_create_storage.sql"
            migration.write_text(
                "CREATE TABLE storage (shelf integer PRIMARY KEY, code text NOT NULL);\n"
                "CREATE VIEW column_mapping AS\n"
                "SELECT * FROM (VALUES\n"
                "    ('shelf_id', 'storage', 'shelf'),\n"
                "    ('shelf_code', 'storage', 'code')\n"
                ") AS mapping (fact, table_name, column_name);\n",
                encoding="utf-8",
            )
            (root / "load.sql").write_text("", encoding="utf-8")
            (root / "run").write_text(
                "#!/usr/bin/env bash\n"
                "set -eu\n"
                'if [ "$1" = prepare ]; then\n'
                '  psql "$CMSC127_DATABASE_URL" --set ON_ERROR_STOP=1 --file load.sql\n'
                "fi\n",
                encoding="utf-8",
                newline="\n",
            )

            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(["1", "--root", str(root)])
            self.assertEqual(0, exit_code, output.getvalue())

            migration.write_text(
                "CREATE TABLE storage (shelf integer PRIMARY KEY, code text NOT NULL);\n"
                "CREATE VIEW column_mapping AS\n"
                "SELECT * FROM (VALUES\n"
                "    ('shelf_id', 'storage', 'shelf')\n"
                ") AS mapping (fact, table_name, column_name);\n",
                encoding="utf-8",
            )
            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(["1", "--root", str(root)])

            self.assertEqual(1, exit_code, output.getvalue())
            result = json.loads(
                (root / ".cmsc127" / "last-run.json").read_text(encoding="utf-8")
            )
            rows = cast(list[dict[str, object]], result["results"])
            differences = cast(list[str], rows[0]["differences"])
            self.assertIn("shelf_code", " ".join(differences))

    def test_each_run_uses_a_scratch_database_and_repeats_the_result(
        self: RunnerIntegrationTests,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cases" / "lab1").mkdir(parents=True)
            (root / "migrations").mkdir()
            (root / "cases" / "manifest.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "invariants": [
                            {"id": "backup-restore", "stage": 1, "kind": "backup_restore"}
                        ],
                        "cases": [
                            {
                                "id": "lab1/items",
                                "stage": 1,
                                "kind": "query",
                                "file": "cases/lab1/items.sql",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (root / "cases" / "lab1" / "items.sql").write_text(
                "SELECT id, label FROM item ORDER BY id;\n", encoding="utf-8"
            )
            (root / "cases" / "lab1" / "items.expected").write_text(
                '{"columns":["id","label"],"rows":[[1,"alpha"],[2,null]]}\n',
                encoding="utf-8",
            )
            (root / "migrations" / "0001_create_item.sql").write_text(
                "CREATE TABLE item (id integer PRIMARY KEY, label text);\n",
                encoding="utf-8",
            )
            (root / "load.sql").write_text(
                "INSERT INTO item VALUES (1, 'alpha'), (2, NULL);\n", encoding="utf-8"
            )
            (root / "run").write_text(
                "#!/usr/bin/env bash\n"
                "set -eu\n"
                "if [ \"$1\" = prepare ]; then\n"
                "  psql \"$CMSC127_DATABASE_URL\" --set ON_ERROR_STOP=1 --file load.sql\n"
                "elif [ \"$1\" = backup ]; then\n"
                "  pg_dump --format=custom --file \"$CMSC127_BACKUP_FILE\" \"$CMSC127_DATABASE_URL\"\n"
                "elif [ \"$1\" = restore ]; then\n"
                "  pg_restore --clean --if-exists --no-owner --dbname \"$CMSC127_DATABASE_URL\" \"$CMSC127_BACKUP_FILE\"\n"
                "else\n"
                "  exit 2\n"
                "fi\n",
                encoding="utf-8",
                newline="\n",
            )

            database_url = os.environ["CMSC127_DATABASE_URL"]
            with psycopg.connect(database_url) as base_connection:
                with base_connection.cursor() as cursor:
                    cursor.execute("CREATE TEMP TABLE t13_sentinel (value integer) ON COMMIT DROP")
                    cursor.execute("INSERT INTO t13_sentinel VALUES (127)")
                first_output = StringIO()
                with redirect_stdout(first_output):
                    first_exit = main(["1", "--root", str(root)])
                self.assertEqual(0, first_exit, first_output.getvalue())
                first_result = (root / ".cmsc127" / "last-run.json").read_text(encoding="utf-8")
                second_output = StringIO()
                with redirect_stdout(second_output):
                    second_exit = main(["1", "--root", str(root)])
                self.assertEqual(0, second_exit, second_output.getvalue())
                second_result = (root / ".cmsc127" / "last-run.json").read_text(encoding="utf-8")
                with base_connection.cursor() as cursor:
                    cursor.execute("SELECT value FROM t13_sentinel")
                    self.assertEqual([(127,)], cursor.fetchall())

            self.assertEqual(first_result, second_result)

            broken_run = (root / "run").read_text(encoding="utf-8").replace(
                "pg_restore --clean --if-exists --no-owner --dbname \"$CMSC127_DATABASE_URL\" \"$CMSC127_BACKUP_FILE\"",
                ":",
            )
            (root / "run").write_text(broken_run, encoding="utf-8", newline="\n")
            defect_output = StringIO()
            with redirect_stdout(defect_output):
                defect_exit = main(["1", "--root", str(root)])
            self.assertEqual(1, defect_exit, defect_output.getvalue())
            defect_result = json.loads(
                (root / ".cmsc127" / "last-run.json").read_text(encoding="utf-8")
            )
            defect_rows = cast(list[dict[str, object]], defect_result["results"])
            self.assertEqual("fail", defect_rows[1]["status"])

    def test_a_query_case_warning_passes_and_is_recorded(
        self: RunnerIntegrationTests,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cases" / "lab1").mkdir(parents=True)
            (root / "migrations").mkdir()
            (root / "cases" / "manifest.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "cases": [
                            {
                                "id": "lab1/warn",
                                "stage": 1,
                                "kind": "query",
                                "file": "cases/lab1/warn.sql",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (root / "cases" / "lab1" / "warn.sql").write_text(
                "SELECT 'warning: item.label agrees on 2 of 2 rows.' AS row;\n",
                encoding="utf-8",
            )
            (root / "cases" / "lab1" / "warn.expected").write_text(
                '{"columns":["row"],"rows":[]}\n', encoding="utf-8"
            )
            (root / "migrations" / "0001_create_item.sql").write_text(
                "CREATE TABLE item (id integer PRIMARY KEY, label text);\n",
                encoding="utf-8",
            )
            (root / "load.sql").write_text(
                "INSERT INTO item VALUES (1, 'alpha'), (2, 'alpha');\n",
                encoding="utf-8",
            )
            (root / "run").write_text(
                "#!/usr/bin/env bash\n"
                "set -eu\n"
                "if [ \"$1\" = prepare ]; then\n"
                "  psql \"$CMSC127_DATABASE_URL\" --set ON_ERROR_STOP=1 --file load.sql\n"
                "else\n"
                "  exit 2\n"
                "fi\n",
                encoding="utf-8",
                newline="\n",
            )
            output = StringIO()

            with redirect_stdout(output):
                exit_code = main(["1", "--root", str(root)])

            self.assertEqual(0, exit_code, output.getvalue())
            self.assertIn("PASS lab1/warn", output.getvalue())
            self.assertIn("warning: item.label agrees on 2 of 2 rows.", output.getvalue())
            self.assertIn("1 warning", output.getvalue())
            result = json.loads(
                (root / ".cmsc127" / "last-run.json").read_text(encoding="utf-8")
            )
            self.assertEqual(1, result["warnings"])
            self.assertEqual("pass", result["results"][0]["status"])

    def test_a_query_case_warning_needs_a_design_entry(
        self: RunnerIntegrationTests,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = root / "cases"
            cases.mkdir()
            (root / "migrations").mkdir()
            (cases / "manifest.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "cases": [
                            {
                                "id": "lab1/warn",
                                "stage": 1,
                                "kind": "query",
                                "file": "cases/warn.sql",
                            },
                            {
                                "id": "lab1/design",
                                "stage": 1,
                                "kind": "document",
                                "file": "cases/design.json",
                            },
                            {
                                "id": "pair/readme",
                                "stage": 1,
                                "kind": "document",
                                "file": "cases/readme.json",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (cases / "warn.sql").write_text(
                "SELECT 'warning: item.label is not validated.' AS row;\n",
                encoding="utf-8",
            )
            (cases / "warn.expected").write_text(
                '{"columns":["row"],"rows":[]}\n', encoding="utf-8"
            )
            (cases / "design.json").write_text(
                json.dumps(
                    {
                        "file": "DESIGN.md",
                        "headings": ["Tables", "Keys and Links", "Checker Warnings"],
                        "placeholders": {},
                    }
                ),
                encoding="utf-8",
            )
            (cases / "design.expected").write_text(
                '{"columns":["row"],"rows":[]}\n', encoding="utf-8"
            )
            (cases / "readme.json").write_text(
                json.dumps(
                    {
                        "file": "README.md",
                        "headings": ["Setup"],
                        "placeholders": {},
                    }
                ),
                encoding="utf-8",
            )
            (cases / "readme.expected").write_text(
                '{"columns":["row"],"rows":[]}\n', encoding="utf-8"
            )
            (root / "README.md").write_text(
                "# Readme\n\n## Setup\n\nRun the checks.\n", encoding="utf-8"
            )
            (root / "migrations" / "0001_create_item.sql").write_text(
                "CREATE TABLE item (id integer PRIMARY KEY, label text);\n",
                encoding="utf-8",
            )
            (root / "load.sql").write_text(
                "INSERT INTO item VALUES (1, 'alpha'), (2, 'alpha');\n",
                encoding="utf-8",
            )
            (root / "run").write_text(
                "#!/usr/bin/env bash\n"
                "set -eu\n"
                "if [ \"$1\" = prepare ]; then\n"
                "  psql \"$CMSC127_DATABASE_URL\" --set ON_ERROR_STOP=1 --file load.sql\n"
                "else\n"
                "  exit 2\n"
                "fi\n",
                encoding="utf-8",
                newline="\n",
            )
            design_path = root / "DESIGN.md"
            design_path.write_text(
                "# Design\n\n## Tables\n\n- item.\n\n"
                "## Keys and Links\n\n- item has a key.\n\n"
                "## Checker Warnings\n\nNo warning prints.\n",
                encoding="utf-8",
            )

            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(["1", "--root", str(root)])
            self.assertEqual(1, exit_code, output.getvalue())
            self.assertIn("DESIGN.md line", output.getvalue())
            self.assertIn("item.label", output.getvalue())
            self.assertIn("the checker warning is not explained", output.getvalue())

            design_path.write_text(
                "# Design\n\n## Tables\n\n- item.\n\n"
                "## Keys and Links\n\n- item has a key.\n\n"
                "## Checker Warnings\n\n"
                "### item.label\n\n"
                "The column holds a separate fact.\n",
                encoding="utf-8",
            )
            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(["1", "--root", str(root)])
            self.assertEqual(0, exit_code, output.getvalue())
            self.assertIn("PASS pair/readme", output.getvalue())
            self.assertIn("warning: item.label is not validated.", output.getvalue())
            self.assertIn("3/3 checks passed, 0 failed, 0 skipped, 1 warning", output.getvalue())

            design_path.write_text(
                "# Design\n\n## Tables\n\n- item.\n\n"
                "## Keys and Links\n\n- item has a key.\n",
                encoding="utf-8",
            )
            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(["1", "--root", str(root)])
            self.assertEqual(1, exit_code, output.getvalue())
            self.assertIn("DESIGN.md: has no", output.getvalue())
            self.assertIn("item.label", output.getvalue())
            self.assertIn("the checker warning is not explained", output.getvalue())
            self.assertNotIn("DESIGN.md line", output.getvalue())

    def test_all_case_kinds_pass_and_each_defect_fails(
        self: RunnerIntegrationTests,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cases" / "lab1").mkdir(parents=True)
            (root / "cases" / "lab2").mkdir()
            (root / "cases" / "lab3").mkdir()
            (root / "migrations").mkdir()
            case_entries = [
                {
                    "id": "lab1/output",
                    "stage": 1,
                    "kind": "output",
                    "file": "cases/lab1/output.sql",
                },
                {
                    "id": "lab1/query",
                    "stage": 1,
                    "kind": "query",
                    "file": "cases/lab1/query.sql",
                },
                {
                    "id": "lab2/schema",
                    "stage": 2,
                    "kind": "schema",
                    "file": "cases/lab2/schema.erd",
                },
                {
                    "id": "lab2/reject",
                    "stage": 2,
                    "kind": "reject",
                    "file": "cases/lab2/reject.sql",
                },
                {
                    "id": "lab2/state",
                    "stage": 2,
                    "kind": "state",
                    "file": "cases/lab2/state.json",
                },
                {
                    "id": "lab3/race",
                    "stage": 3,
                    "kind": "race",
                    "file": "cases/lab3/race.json",
                },
                {
                    "id": "lab3/query",
                    "stage": 3,
                    "kind": "query",
                    "file": "cases/lab3/count.sql",
                },
            ]
            (root / "cases" / "manifest.json").write_text(
                json.dumps({"version": 1, "cases": case_entries}), encoding="utf-8"
            )
            (root / "cases" / "lab1" / "output.sql").write_text(
                "SELECT true AS ready;\n", encoding="utf-8"
            )
            (root / "cases" / "lab1" / "output.expected").write_text(
                '{"columns":["ready"],"rows":[[true]]}\n', encoding="utf-8"
            )
            (root / "cases" / "lab1" / "query.sql").write_text(
                "SELECT id, label FROM item ORDER BY id;\n", encoding="utf-8"
            )
            (root / "cases" / "lab1" / "query.expected").write_text(
                '{"columns":["id","label"],"rows":[[1,"alpha"],[2,null]]}\n',
                encoding="utf-8",
            )
            (root / "cases" / "lab2" / "schema.erd").write_text(
                "erDiagram\n    item {\n        int id PK\n        string label\n    }\n",
                encoding="utf-8",
            )
            (root / "cases" / "lab2" / "schema.expected").write_text(
                '{"columns":["difference"],"rows":[]}\n', encoding="utf-8"
            )
            (root / "cases" / "lab2" / "reject.sql").write_text(
                "INSERT INTO item (id, label) VALUES (1, 'duplicate');\n",
                encoding="utf-8",
            )
            (root / "cases" / "lab2" / "reject.expected").write_text(
                '{"sqlstate":"23505"}\n', encoding="utf-8"
            )
            (root / "cases" / "lab2" / "state.json").write_text(
                json.dumps(
                    {
                        "operation": "INSERT INTO item VALUES (3, 'gamma')",
                        "check_sql": "SELECT label FROM item WHERE id = 3",
                    }
                ),
                encoding="utf-8",
            )
            (root / "cases" / "lab2" / "state.expected").write_text(
                '{"columns":["label"],"rows":[["gamma"]]}\n', encoding="utf-8"
            )
            (root / "cases" / "lab3" / "race.json").write_text(
                json.dumps(
                    {
                        "steps": [
                            {
                                "session": "a",
                                "sql": "SELECT pg_try_advisory_lock(127) AS acquired",
                                "expected": {
                                    "columns": ["acquired"],
                                    "rows": [[True]],
                                },
                            },
                            {
                                "session": "b",
                                "sql": "SELECT pg_try_advisory_lock(127) AS acquired",
                                "expected": {"columns": ["acquired"], "rows": [[False]]},
                            },
                            {
                                "session": "a",
                                "sql": "SELECT pg_advisory_unlock(127) AS released",
                                "expected": {"columns": ["released"], "rows": [[True]]},
                            },
                        ],
                        "check_sql": "SELECT true AS ready",
                    }
                ),
                encoding="utf-8",
            )
            (root / "cases" / "lab3" / "race.expected").write_text(
                '{"columns":["ready"],"rows":[[true]]}\n', encoding="utf-8"
            )
            (root / "cases" / "lab3" / "count.sql").write_text(
                "SELECT count(*) AS total FROM item;\n", encoding="utf-8"
            )
            (root / "cases" / "lab3" / "count.expected").write_text(
                '{"columns":["total"],"rows":[[3]]}\n', encoding="utf-8"
            )
            (root / "migrations" / "0001_create_item.sql").write_text(
                "CREATE TABLE item (id integer PRIMARY KEY, label text);\n",
                encoding="utf-8",
            )
            (root / "load.sql").write_text(
                "INSERT INTO item VALUES (1, 'alpha'), (2, NULL);\n", encoding="utf-8"
            )
            (root / "run").write_text(
                "#!/usr/bin/env bash\n"
                "set -eu\n"
                "if [ \"$1\" = prepare ]; then\n"
                "  psql \"$CMSC127_DATABASE_URL\" --set ON_ERROR_STOP=1 --file load.sql\n"
                "else\n"
                "  printf '%s\\n' '{\"columns\":[\"ready\"],\"rows\":[[true]]}'\n"
                "fi\n",
                encoding="utf-8",
                newline="\n",
            )

            passing_output = StringIO()
            with redirect_stdout(passing_output):
                pass_exit = main(["3", "--root", str(root)])
            self.assertEqual(0, pass_exit, passing_output.getvalue())
            pass_result = json.loads(
                (root / ".cmsc127" / "last-run.json").read_text(encoding="utf-8")
            )
            pass_rows = cast(list[dict[str, object]], pass_result["results"])
            self.assertTrue(all(row["status"] == "pass" for row in pass_rows))

            (root / "cases" / "lab1" / "output.expected").write_text(
                '{"columns":["ready"],"rows":[[false]]}\n', encoding="utf-8"
            )
            (root / "cases" / "lab2" / "schema.expected").write_text(
                '{"columns":["difference"],"rows":[["unexpected"]]}\n',
                encoding="utf-8",
            )
            (root / "cases" / "lab2" / "reject.expected").write_text(
                '{"sqlstate":"P0001"}\n', encoding="utf-8"
            )
            (root / "cases" / "lab2" / "state.expected").write_text(
                '{"columns":["label"],"rows":[["wrong"]]}\n', encoding="utf-8"
            )
            race_case = json.loads(
                (root / "cases" / "lab3" / "race.json").read_text(encoding="utf-8")
            )
            race_case["steps"][1]["expected"]["rows"] = [[True]]
            (root / "cases" / "lab3" / "race.json").write_text(
                json.dumps(race_case), encoding="utf-8"
            )
            (root / "cases" / "lab3" / "count.expected").write_text(
                '{"columns":["total"],"rows":[[4]]}\n', encoding="utf-8"
            )
            defect_output = StringIO()
            with redirect_stdout(defect_output):
                defect_exit = main(["3", "--root", str(root)])
            self.assertEqual(1, defect_exit, defect_output.getvalue())
            defect_result = json.loads(
                (root / ".cmsc127" / "last-run.json").read_text(encoding="utf-8")
            )
            defect_rows = cast(list[dict[str, object]], defect_result["results"])
            failed_ids = {
                str(row["id"]) for row in defect_rows if row["status"] == "fail"
            }
            self.assertEqual(
                {str(row["id"]) for row in case_entries if row["id"] != "lab1/query"},
                failed_ids,
            )

    def test_report_and_view_cases_run_the_named_files(
        self: RunnerIntegrationTests,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cases" / "pair").mkdir(parents=True)
            (root / "reports").mkdir()
            (root / "migrations").mkdir()
            (root / "migrations" / "0001_create_item.sql").write_text(
                "CREATE TABLE item (id integer PRIMARY KEY, label text);\n"
                "CREATE VIEW item_roster AS SELECT id, label FROM item;\n",
                encoding="utf-8",
            )
            (root / "load.sql").write_text(
                "INSERT INTO item VALUES (1, 'alpha'), (2, 'beta');\n",
                encoding="utf-8",
            )
            (root / "reports" / "list.sql").write_text(
                "SELECT id, label FROM item ORDER BY id;\n", encoding="utf-8"
            )
            (root / "reports" / "roster.sql").write_text(
                "SELECT id, label FROM item_roster ORDER BY id;\n", encoding="utf-8"
            )
            (root / "reports" / "base.sql").write_text(
                "SELECT id, label FROM item ORDER BY id;\n", encoding="utf-8"
            )
            (root / "cases" / "manifest.json").write_text(
                json.dumps({"version": 1, "cases": []}), encoding="utf-8"
            )
            (root / "cases" / "pair" / "manifest.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "cases": [
                            {
                                "id": "lab2/list",
                                "stage": 2,
                                "group": "Query Correctness",
                                "kind": "report",
                                "file": "cases/pair/list.json",
                            },
                            {
                                "id": "lab2/view-bypass",
                                "stage": 2,
                                "group": "SQL Craft",
                                "kind": "view",
                                "file": "cases/pair/view.json",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (root / "cases" / "pair" / "list.json").write_text(
                json.dumps({"report": "reports/list.sql"}), encoding="utf-8"
            )
            (root / "cases" / "pair" / "list.expected").write_text(
                '{"columns":["id","label"],"rows":[[1,"alpha"],[2,"beta"]]}\n',
                encoding="utf-8",
            )
            (root / "cases" / "pair" / "view.json").write_text(
                json.dumps(
                    {"report": "reports/roster.sql", "view": "item_roster"}
                ),
                encoding="utf-8",
            )
            (root / "cases" / "pair" / "view.expected").write_text(
                '{"columns":["relation","problem"],"rows":[]}\n', encoding="utf-8"
            )
            (root / "run").write_text(
                "#!/usr/bin/env bash\n"
                "set -eu\n"
                "if [ \"$1\" = prepare ]; then\n"
                "  psql \"$CMSC127_DATABASE_URL\" --set ON_ERROR_STOP=1 --file load.sql\n"
                "fi\n",
                encoding="utf-8",
                newline="\n",
            )

            passing_output = StringIO()
            with redirect_stdout(passing_output):
                pass_exit = main(["2", "--root", str(root)])
            self.assertEqual(0, pass_exit, passing_output.getvalue())

            (root / "cases" / "pair" / "view.json").write_text(
                json.dumps({"report": "reports/base.sql", "view": "item_roster"}),
                encoding="utf-8",
            )
            defect_output = StringIO()
            with redirect_stdout(defect_output):
                defect_exit = main(["2", "--root", str(root)])
            self.assertEqual(1, defect_exit, defect_output.getvalue())
            defect_result = json.loads(
                (root / ".cmsc127" / "last-run.json").read_text(encoding="utf-8")
            )
            defect_rows = cast(list[dict[str, object]], defect_result["results"])
            failed_ids = {
                str(row["id"]) for row in defect_rows if row["status"] == "fail"
            }
            self.assertEqual({"lab2/view-bypass"}, failed_ids)
            bypass_row = next(
                row for row in defect_rows if row["id"] == "lab2/view-bypass"
            )
            actual = cast(dict[str, object], bypass_row["actual"])
            actual_rows = cast(list[list[object]], actual["rows"])
            self.assertTrue(
                any(
                    "instead of" in str(cell)
                    for row in actual_rows
                    for cell in row
                ),
                actual_rows,
            )

    def test_view_must_come_from_a_migration(self: RunnerIntegrationTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = root / "cases"
            pair = cases / "pair"
            migrations = root / "migrations"
            pair.mkdir(parents=True)
            migrations.mkdir()
            (cases / "manifest.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "cases": [
                            {
                                "id": "lab2/ready",
                                "stage": 2,
                                "kind": "query",
                                "file": "cases/ready.sql",
                            }
                        ],
                        "invariants": [
                            {
                                "id": "lab2/view",
                                "stage": 2,
                                "kind": "feature",
                                "schema": "public",
                                "view": "item_roster",
                                "features": [],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (pair / "manifest.json").write_text(
                json.dumps({"version": 1, "cases": []}), encoding="utf-8"
            )
            (cases / "ready.sql").write_text("SELECT true AS ready;\n", encoding="utf-8")
            (cases / "ready.expected").write_text(
                '{"columns":["ready"],"rows":[[true]]}\n', encoding="utf-8"
            )
            (root / "load.sql").write_text(
                "CREATE TABLE item (id integer PRIMARY KEY);\n"
                "CREATE VIEW item_roster AS SELECT id FROM item;\n",
                encoding="utf-8",
            )
            (root / "run").write_text(
                "#!/usr/bin/env bash\n"
                "set -eu\n"
                'if [ "$1" = prepare ]; then\n'
                '  psql "$CMSC127_DATABASE_URL" --set ON_ERROR_STOP=1 --file load.sql\n'
                "fi\n",
                encoding="utf-8",
                newline="\n",
            )

            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(["2", "--root", str(root)])
            self.assertEqual(1, exit_code, output.getvalue())
            self.assertIn(
                "item_roster was not created by a migration", output.getvalue()
            )

            (migrations / "0001_create_item_roster.sql").write_text(
                "CREATE TABLE item (id integer PRIMARY KEY);\n"
                "CREATE VIEW item_roster AS SELECT id FROM item;\n",
                encoding="utf-8",
            )
            (root / "load.sql").write_text(
                "INSERT INTO item VALUES (1);\n", encoding="utf-8"
            )
            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(["2", "--root", str(root)])
            self.assertEqual(0, exit_code, output.getvalue())

    def test_feature_warning_costs_nothing_but_needs_a_design_heading(
        self: RunnerIntegrationTests,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = root / "cases"
            pair = cases / "pair"
            reports = root / "reports"
            pair.mkdir(parents=True)
            reports.mkdir()
            (cases / "manifest.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "cases": [
                            {
                                "id": "lab2/outer-join-output",
                                "stage": 2,
                                "kind": "report",
                                "file": "cases/outer.json",
                            },
                            {
                                "id": "lab1/design",
                                "stage": 1,
                                "kind": "document",
                                "file": "cases/design.json",
                            },
                            {
                                "id": "pair/readme",
                                "stage": 1,
                                "kind": "document",
                                "file": "cases/readme.json",
                            },
                        ],
                        "invariants": [
                            {
                                "id": "lab2/outer-join",
                                "stage": 2,
                                "kind": "feature",
                                "group": "SQL Craft",
                                "file": "reports/outer.sql",
                                "features": ["left_join"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (pair / "manifest.json").write_text(
                json.dumps({"version": 1, "cases": []}), encoding="utf-8"
            )
            (cases / "outer.json").write_text(
                json.dumps({"report": "reports/outer.sql"}), encoding="utf-8"
            )
            (cases / "outer.expected").write_text(
                '{"columns":["one"],"rows":[[1]]}\n', encoding="utf-8"
            )
            (cases / "design.json").write_text(
                json.dumps(
                    {
                        "file": "DESIGN.md",
                        "headings": ["Tables", "Keys and Links", "Checker Warnings"],
                        "placeholders": {},
                    }
                ),
                encoding="utf-8",
            )
            (cases / "design.expected").write_text(
                '{"columns":["row"],"rows":[]}\n', encoding="utf-8"
            )
            (cases / "readme.json").write_text(
                json.dumps(
                    {
                        "file": "README.md",
                        "headings": ["Setup"],
                        "placeholders": {},
                    }
                ),
                encoding="utf-8",
            )
            (cases / "readme.expected").write_text(
                '{"columns":["row"],"rows":[]}\n', encoding="utf-8"
            )
            (root / "README.md").write_text(
                "# Readme\n\n## Setup\n\nRun the checks.\n", encoding="utf-8"
            )
            (reports / "outer.sql").write_text("SELECT 1 AS one;\n", encoding="utf-8")
            (root / "migrations").mkdir()
            (root / "load.sql").write_text("", encoding="utf-8")
            (root / "run").write_text(
                "#!/usr/bin/env bash\n"
                "set -eu\n"
                'if [ "$1" = prepare ]; then\n'
                '  psql "$CMSC127_DATABASE_URL" --set ON_ERROR_STOP=1 --file load.sql\n'
                "fi\n",
                encoding="utf-8",
                newline="\n",
            )
            design_path = root / "DESIGN.md"
            design_path.write_text(
                "# Design\n\n## Tables\n\n- item.\n\n"
                "## Keys and Links\n\n- item has a key.\n\n"
                "## Checker Warnings\n\n"
                "### reports/outer.sql\n\n"
                "This report uses a CASE expression for missing room values.\n",
                encoding="utf-8",
            )

            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(["2", "--root", str(root)])
            self.assertEqual(0, exit_code, output.getvalue())
            self.assertIn("PASS pair/readme", output.getvalue())
            self.assertIn("warning: reports/outer.sql shows no outer join", output.getvalue())
            self.assertIn("4/4 checks passed, 0 failed, 0 skipped, 1 warning", output.getvalue())

            design_path.write_text(
                "# Design\n\n## Tables\n\n- item.\n\n"
                "## Keys and Links\n\n- item has a key.\n\n"
                "## Checker Warnings\n\nNo warning prints.\n",
                encoding="utf-8",
            )
            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(["2", "--root", str(root)])
            self.assertEqual(1, exit_code, output.getvalue())
            self.assertIn("DESIGN.md line", output.getvalue())
            self.assertIn("reports/outer.sql", output.getvalue())
            self.assertIn("the checker warning is not explained", output.getvalue())

    def test_reject_case_requires_a_real_constraint(
        self: RunnerIntegrationTests,
    ) -> None:
        def make_root(root: Path, migration: str, load: str) -> None:
            (root / "cases" / "lab1").mkdir(parents=True)
            (root / "migrations").mkdir()
            (root / "cases" / "manifest.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "cases": [
                            {
                                "id": "lab1/reject",
                                "stage": 1,
                                "kind": "reject",
                                "file": "cases/lab1/reject.sql",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (root / "cases" / "lab1" / "reject.sql").write_text(
                "INSERT INTO item (id, label) VALUES (1, 'duplicate');\n",
                encoding="utf-8",
            )
            (root / "cases" / "lab1" / "reject.expected").write_text(
                '{"sqlstate":"23505"}\n', encoding="utf-8"
            )
            (root / "migrations" / "0001_create_item.sql").write_text(
                migration, encoding="utf-8"
            )
            (root / "load.sql").write_text(load, encoding="utf-8")
            (root / "run").write_text(
                "#!/usr/bin/env bash\n"
                "set -eu\n"
                "if [ \"$1\" = prepare ]; then\n"
                "  psql \"$CMSC127_DATABASE_URL\" --set ON_ERROR_STOP=1 --file load.sql\n"
                "else\n"
                "  exit 2\n"
                "fi\n",
                encoding="utf-8",
                newline="\n",
            )

        with tempfile.TemporaryDirectory() as directory:
            real_root = Path(directory) / "real"
            make_root(
                real_root,
                "CREATE TABLE item (id integer PRIMARY KEY, label text);\n",
                "INSERT INTO item VALUES (1, 'alpha');\n",
            )
            real_output = StringIO()
            with redirect_stdout(real_output):
                real_exit = main(["1", "--root", str(real_root)])
            self.assertEqual(0, real_exit, real_output.getvalue())

            fake_root = Path(directory) / "fake"
            make_root(
                fake_root,
                "CREATE TABLE item (id integer PRIMARY KEY, label text);\n"
                "CREATE FUNCTION refuse_item() RETURNS trigger LANGUAGE plpgsql AS $$\n"
                "BEGIN\n"
                "    RAISE EXCEPTION 'duplicate' USING ERRCODE = '23505';\n"
                "END;\n"
                "$$;\n"
                "CREATE TRIGGER refuse_item BEFORE INSERT ON item\n"
                "FOR EACH ROW EXECUTE FUNCTION refuse_item();\n",
                "SELECT 1;\n",
            )
            fake_output = StringIO()
            with redirect_stdout(fake_output):
                fake_exit = main(["1", "--root", str(fake_root)])
            self.assertEqual(1, fake_exit, fake_output.getvalue())
            fake_result = json.loads(
                (fake_root / ".cmsc127" / "last-run.json").read_text(encoding="utf-8")
            )
            fake_rows = cast(list[dict[str, object]], fake_result["results"])
            self.assertEqual("fail", fake_rows[0]["status"])
            fake_differences = cast(list[str], fake_rows[0]["differences"])
            self.assertTrue(
                any(
                    "the error does not come from a named constraint. A RAISE or "
                    "ASSERT, or an error with no constraint name, does not test the "
                    "schema. Change the insert so a table constraint refuses the row."
                    in difference
                    for difference in fake_differences
                ),
                fake_differences,
            )

    def test_reject_case_reports_a_statement_that_succeeds(
        self: RunnerIntegrationTests,
    ) -> None:
        migration = "CREATE TABLE item (id integer PRIMARY KEY, label text);\n"
        with tempfile.TemporaryDirectory() as directory:
            exit_code, rows = self._run_reject_root(
                Path(directory),
                migration,
                "SELECT 1;\n",
                "INSERT INTO item (id, label) VALUES (1, 'valid');\n",
            )
        self.assertEqual(1, exit_code)
        self.assertEqual("fail", rows[0]["status"])
        differences = cast(list[str], rows[0]["differences"])
        self.assertTrue(
            any(
                "statement succeeded, but the case requires rejection. Change "
                "the statement so a table constraint refuses the row."
                in difference
                for difference in differences
            ),
            differences,
        )

    def test_reject_case_reports_a_different_sqlstate(
        self: RunnerIntegrationTests,
    ) -> None:
        migration = (
            "CREATE TABLE item (id integer PRIMARY KEY, n integer CHECK (n > 0));\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            exit_code, rows = self._run_reject_root(
                Path(directory),
                migration,
                "SELECT 1;\n",
                "INSERT INTO item (id, n) VALUES (1, -1);\n",
            )
        self.assertEqual(1, exit_code)
        self.assertEqual("fail", rows[0]["status"])
        differences = cast(list[str], rows[0]["differences"])
        self.assertTrue(
            any(
                "SQLSTATE differs: expected '23505', actual '23514'. Change the "
                "statement so it fails with the expected SQLSTATE."
                in difference
                for difference in differences
            ),
            differences,
        )

    def _run_reject_root(
        self: RunnerIntegrationTests,
        root: Path,
        migration: str,
        load: str,
        insert: str,
        sqlstate: str = "23505",
    ) -> tuple[int, list[dict[str, object]]]:
        (root / "cases" / "lab1").mkdir(parents=True)
        (root / "migrations").mkdir()
        (root / "cases" / "manifest.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "cases": [
                        {
                            "id": "lab1/reject",
                            "stage": 1,
                            "kind": "reject",
                            "file": "cases/lab1/reject.sql",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (root / "cases" / "lab1" / "reject.sql").write_text(insert, encoding="utf-8")
        (root / "cases" / "lab1" / "reject.expected").write_text(
            json.dumps({"sqlstate": sqlstate}) + "\n", encoding="utf-8"
        )
        (root / "migrations" / "0001_create_item.sql").write_text(
            migration, encoding="utf-8"
        )
        (root / "load.sql").write_text(load, encoding="utf-8")
        (root / "run").write_text(
            "#!/usr/bin/env bash\n"
            "set -eu\n"
            "if [ \"$1\" = prepare ]; then\n"
            "  psql \"$CMSC127_DATABASE_URL\" --set ON_ERROR_STOP=1 --file load.sql\n"
            "else\n"
            "  exit 2\n"
            "fi\n",
            encoding="utf-8",
            newline="\n",
        )
        output = StringIO()
        with redirect_stdout(output):
            exit_code = main(["1", "--root", str(root)])
        result_path = root / ".cmsc127" / "last-run.json"
        if not result_path.is_file():
            run_output = output.getvalue().strip() or "(no output)"
            self.fail(
                f"Runner exited {exit_code} without writing {result_path}.\n"
                f"Run output:\n{run_output}"
            )
        result = json.loads(result_path.read_text(encoding="utf-8"))
        rows = cast(list[dict[str, object]], result["results"])
        return exit_code, rows

    def test_reject_root_reports_output_when_last_run_is_missing(
        self: RunnerIntegrationTests,
    ) -> None:
        def fail_before_result(_arguments: list[str]) -> int:
            print("run output: simulated failure")
            return 1

        with tempfile.TemporaryDirectory() as directory:
            with patch("tests.test_runner.main", side_effect=fail_before_result):
                with self.assertRaisesRegex(
                    AssertionError, "run output: simulated failure"
                ):
                    self._run_reject_root(
                        Path(directory),
                        "CREATE TABLE item (id integer PRIMARY KEY);\n",
                        "SELECT 1;\n",
                        "INSERT INTO item VALUES (1);\n",
                    )

    def test_reject_case_rejects_a_trigger_that_names_a_real_constraint(
        self: RunnerIntegrationTests,
    ) -> None:
        migration = (
            "CREATE TABLE item (id integer PRIMARY KEY, label text);\n"
            "CREATE FUNCTION fake_unique() RETURNS trigger LANGUAGE plpgsql AS $$\n"
            "BEGIN\n"
            "    RAISE EXCEPTION USING\n"
            "        ERRCODE = '23505',\n"
            "        MESSAGE = 'duplicate key value violates unique constraint \"item_pkey\"',\n"
            "        CONSTRAINT = 'item_pkey',\n"
            "        TABLE = 'item',\n"
            "        SCHEMA = 'public';\n"
            "END;\n"
            "$$;\n"
            "CREATE TRIGGER item_fake BEFORE INSERT ON item\n"
            "FOR EACH ROW EXECUTE FUNCTION fake_unique();\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            exit_code, rows = self._run_reject_root(
                Path(directory),
                migration,
                "SELECT 1;\n",
                "INSERT INTO item (id, label) VALUES (2, 'valid');\n",
            )
        self.assertEqual(1, exit_code)
        self.assertEqual("fail", rows[0]["status"])

    def test_reject_case_accepts_a_unique_index_and_rolls_back_success(
        self: RunnerIntegrationTests,
    ) -> None:
        migration = (
            "CREATE TABLE item (id integer NOT NULL, label text);\n"
            "CREATE UNIQUE INDEX item_id_key ON item (id);\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            exit_code, rows = self._run_reject_root(
                Path(directory),
                migration,
                "INSERT INTO item (id, label) VALUES (1, 'alpha');\n",
                "INSERT INTO item (id, label) VALUES (1, 'duplicate');\n",
            )
        self.assertEqual(0, exit_code)
        self.assertEqual("pass", rows[0]["status"])
        with psycopg.connect(os.environ["CMSC127_DATABASE_URL"], autocommit=True) as connection:
            connection.execute("CREATE TEMP TABLE reject_probe (id integer)")
            self.assertIsNone(
                sql_error(
                    connection,
                    "INSERT INTO reject_probe VALUES (1)",
                )
            )
            self.assertEqual(
                (0,),
                connection.execute("SELECT count(*) FROM reject_probe").fetchone(),
            )

    def test_reject_case_accepts_a_domain_constraint(
        self: RunnerIntegrationTests,
    ) -> None:
        migration = (
            "CREATE DOMAIN positive_int AS integer CHECK (VALUE > 0);\n"
            "CREATE TABLE item (id integer PRIMARY KEY, n positive_int);\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            exit_code, rows = self._run_reject_root(
                Path(directory),
                migration,
                "SELECT 1;\n",
                "INSERT INTO item (id, n) VALUES (1, -5);\n",
                "23514",
            )
        self.assertEqual(0, exit_code)
        self.assertEqual("pass", rows[0]["status"])

    def test_reject_case_accepts_a_constraint_inside_a_trigger(
        self: RunnerIntegrationTests,
    ) -> None:
        migration = (
            "CREATE TABLE helper (a integer, b integer, PRIMARY KEY (a, b));\n"
            "CREATE TABLE item (id integer PRIMARY KEY, a integer, b integer);\n"
            "CREATE FUNCTION sync_helper() RETURNS trigger LANGUAGE plpgsql AS $$\n"
            "BEGIN\n"
            "    INSERT INTO helper (a, b) VALUES (NEW.a, NEW.b);\n"
            "    RETURN NEW;\n"
            "END;\n"
            "$$;\n"
            "CREATE TRIGGER item_sync BEFORE INSERT ON item\n"
            "FOR EACH ROW EXECUTE FUNCTION sync_helper();\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            exit_code, rows = self._run_reject_root(
                Path(directory),
                migration,
                "INSERT INTO item (id, a, b) VALUES (1, 1, 1);\n",
                "INSERT INTO item (id, a, b) VALUES (2, 1, 1);\n",
            )
        self.assertEqual(0, exit_code)
        self.assertEqual("pass", rows[0]["status"])

    def test_reject_case_rejects_a_trigger_that_changes_the_row(
        self: RunnerIntegrationTests,
    ) -> None:
        scenarios = (
            (
                "check",
                "CREATE TABLE item (id integer PRIMARY KEY, n integer CHECK (n > 0));\n"
                "CREATE FUNCTION corrupt() RETURNS trigger LANGUAGE plpgsql AS $$\n"
                "BEGIN\n"
                "    IF NEW.id = 2 THEN NEW.n := -1; END IF;\n"
                "    RETURN NEW;\n"
                "END;\n"
                "$$;\n"
                "CREATE TRIGGER item_corrupt BEFORE INSERT ON item\n"
                "FOR EACH ROW EXECUTE FUNCTION corrupt();\n",
                "INSERT INTO item (id, n) VALUES (2, 5);\n",
                "23514",
            ),
            (
                "primary_key",
                "CREATE TABLE item (id integer PRIMARY KEY, label text);\n"
                "INSERT INTO item (id, label) VALUES (1, 'a');\n"
                "CREATE FUNCTION corrupt() RETURNS trigger LANGUAGE plpgsql AS $$\n"
                "BEGIN\n"
                "    IF NEW.id = 2 THEN NEW.id := 1; END IF;\n"
                "    RETURN NEW;\n"
                "END;\n"
                "$$;\n"
                "CREATE TRIGGER item_corrupt BEFORE INSERT ON item\n"
                "FOR EACH ROW EXECUTE FUNCTION corrupt();\n",
                "INSERT INTO item (id, label) VALUES (2, 'b');\n",
                "23505",
            ),
            (
                "unique",
                "CREATE TABLE item (id integer PRIMARY KEY, code text UNIQUE);\n"
                "INSERT INTO item (id, code) VALUES (1, 'a');\n"
                "CREATE FUNCTION corrupt() RETURNS trigger LANGUAGE plpgsql AS $$\n"
                "BEGIN\n"
                "    IF NEW.id = 2 THEN NEW.code := 'a'; END IF;\n"
                "    RETURN NEW;\n"
                "END;\n"
                "$$;\n"
                "CREATE TRIGGER item_corrupt BEFORE INSERT ON item\n"
                "FOR EACH ROW EXECUTE FUNCTION corrupt();\n",
                "INSERT INTO item (id, code) VALUES (2, 'b');\n",
                "23505",
            ),
            (
                "foreign_key",
                "CREATE TABLE parent (id integer PRIMARY KEY);\n"
                "INSERT INTO parent (id) VALUES (1);\n"
                "CREATE TABLE child (id integer PRIMARY KEY, "
                "parent_id integer REFERENCES parent (id));\n"
                "CREATE FUNCTION corrupt() RETURNS trigger LANGUAGE plpgsql AS $$\n"
                "BEGIN\n"
                "    IF NEW.id = 2 THEN NEW.parent_id := 99; END IF;\n"
                "    RETURN NEW;\n"
                "END;\n"
                "$$;\n"
                "CREATE TRIGGER child_corrupt BEFORE INSERT ON child\n"
                "FOR EACH ROW EXECUTE FUNCTION corrupt();\n",
                "INSERT INTO child (id, parent_id) VALUES (2, 1);\n",
                "23503",
            ),
            (
                "not_null",
                "CREATE TABLE item (id integer PRIMARY KEY, label text NOT NULL);\n"
                "CREATE FUNCTION corrupt() RETURNS trigger LANGUAGE plpgsql AS $$\n"
                "BEGIN\n"
                "    IF NEW.id = 2 THEN NEW.label := NULL; END IF;\n"
                "    RETURN NEW;\n"
                "END;\n"
                "$$;\n"
                "CREATE TRIGGER item_corrupt BEFORE INSERT ON item\n"
                "FOR EACH ROW EXECUTE FUNCTION corrupt();\n",
                "INSERT INTO item (id, label) VALUES (2, 'valid');\n",
                "23502",
            ),
            (
                "trigger_inserts_target",
                "CREATE TABLE item (id integer PRIMARY KEY, label text);\n"
                "INSERT INTO item (id, label) VALUES (1, 'a');\n"
                "CREATE FUNCTION trip() RETURNS trigger LANGUAGE plpgsql AS $$\n"
                "BEGIN\n"
                "    IF NEW.id = 2 THEN\n"
                "        INSERT INTO item (id, label) VALUES (1, 'dup');\n"
                "    END IF;\n"
                "    RETURN NEW;\n"
                "END;\n"
                "$$;\n"
                "CREATE TRIGGER item_trip BEFORE INSERT ON item\n"
                "FOR EACH ROW EXECUTE FUNCTION trip();\n",
                "INSERT INTO item (id, label) VALUES (2, 'valid');\n",
                "23505",
            ),
        )
        for name, migration, insert, sqlstate in scenarios:
            with self.subTest(name), tempfile.TemporaryDirectory() as directory:
                exit_code, rows = self._run_reject_root(
                    Path(directory), migration, "SELECT 1;\n", insert, sqlstate
                )
                self.assertEqual(1, exit_code)
                self.assertEqual("fail", rows[0]["status"])

    def test_reject_case_rejects_an_assert(
        self: RunnerIntegrationTests,
    ) -> None:
        migration = (
            "CREATE TABLE item (id integer PRIMARY KEY, label text);\n"
            "CREATE FUNCTION assert_unique() RETURNS trigger LANGUAGE plpgsql AS $$\n"
            "BEGIN\n"
            "    ASSERT FALSE, 'duplicate';\n"
            "    RETURN NEW;\n"
            "END;\n"
            "$$;\n"
            "CREATE TRIGGER item_assert BEFORE INSERT ON item\n"
            "FOR EACH ROW EXECUTE FUNCTION assert_unique();\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            exit_code, rows = self._run_reject_root(
                Path(directory),
                migration,
                "SELECT 1;\n",
                "INSERT INTO item (id, label) VALUES (2, 'valid');\n",
            )
        self.assertEqual(1, exit_code)
        self.assertEqual("fail", rows[0]["status"])

    def test_reject_case_accepts_a_not_null_column(
        self: RunnerIntegrationTests,
    ) -> None:
        migration = (
            "CREATE TABLE item (id integer PRIMARY KEY, label text NOT NULL);\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            exit_code, rows = self._run_reject_root(
                Path(directory),
                migration,
                "SELECT 1;\n",
                "INSERT INTO item (id, label) VALUES (1, NULL);\n",
                "23502",
            )
        self.assertEqual(0, exit_code)
        self.assertEqual("pass", rows[0]["status"])

    def test_reject_case_rejects_a_rewrite_rule_on_the_target(
        self: RunnerIntegrationTests,
    ) -> None:
        migration = (
            "CREATE TABLE item (id integer PRIMARY KEY, n integer CHECK (n > 0));\n"
            "INSERT INTO item VALUES (1, 5);\n"
            "CREATE RULE item_force AS ON INSERT TO item DO ALSO "
            "UPDATE item SET n = -1 WHERE id = NEW.id;\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            exit_code, rows = self._run_reject_root(
                Path(directory),
                migration,
                "SELECT 1;\n",
                "INSERT INTO item (id, n) VALUES (2, 5);\n",
                "23514",
            )
        self.assertEqual(1, exit_code)
        self.assertEqual("fail", rows[0]["status"])
        differences = cast(list[str], rows[0]["differences"])
        self.assertTrue(
            any(
                "is a view" in difference
                and "Insert into the table itself" in difference
                for difference in differences
            ),
            differences,
        )

    def test_reject_case_rejects_a_trigger_on_a_partition(
        self: RunnerIntegrationTests,
    ) -> None:
        migration = (
            "CREATE TABLE item (id integer PRIMARY KEY, n integer CHECK (n > 0)) "
            "PARTITION BY RANGE (id);\n"
            "CREATE TABLE item_p1 PARTITION OF item FOR VALUES FROM (0) TO (100);\n"
            "INSERT INTO item VALUES (1, 5);\n"
            "CREATE FUNCTION corrupt() RETURNS trigger LANGUAGE plpgsql AS $$\n"
            "BEGIN\n"
            "    IF NEW.id = 2 THEN NEW.n := -1; END IF;\n"
            "    RETURN NEW;\n"
            "END;\n"
            "$$;\n"
            "CREATE TRIGGER item_corrupt BEFORE INSERT ON item_p1 "
            "FOR EACH ROW EXECUTE FUNCTION corrupt();\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            exit_code, rows = self._run_reject_root(
                Path(directory),
                migration,
                "SELECT 1;\n",
                "INSERT INTO item (id, n) VALUES (2, 5);\n",
                "23514",
            )
        self.assertEqual(1, exit_code)
        self.assertEqual("fail", rows[0]["status"])

    def test_reject_case_accepts_a_deferred_unique_constraint(
        self: RunnerIntegrationTests,
    ) -> None:
        migration = (
            "CREATE TABLE item (id integer PRIMARY KEY, code text, "
            "CONSTRAINT item_code_key UNIQUE (code) "
            "DEFERRABLE INITIALLY DEFERRED);\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            exit_code, rows = self._run_reject_root(
                Path(directory),
                migration,
                "INSERT INTO item VALUES (1, 'a');\n",
                "INSERT INTO item (id, code) VALUES (2, 'a');\n",
            )
        self.assertEqual(0, exit_code)
        self.assertEqual("pass", rows[0]["status"])


@unittest.skipUnless(os.environ.get("CMSC127_DATABASE_URL"), "requires PostgreSQL")
class InvariantTests(unittest.TestCase):
    connection: _TestConnection
    schema: str

    def setUp(self: InvariantTests) -> None:
        self.connection = cast(
            _TestConnection,
            psycopg.connect(os.environ["CMSC127_DATABASE_URL"], autocommit=True),
        )
        self.schema = f"t13_invariant_{uuid4().hex[:10]}"
        with self.connection.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{self.schema}"')

    def tearDown(self: InvariantTests) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')
        self.connection.close()

    def test_normal_form_requires_a_key_in_the_relation(
        self: InvariantTests,
    ) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                f'CREATE TABLE "{self.schema}".item '
                "(id integer PRIMARY KEY, label text)"
            )
            cursor.execute(
                f'CREATE TABLE "{self.schema}".other (label text)'
            )
        mapping = {
            "id": {"table": f"{self.schema}.item", "column": "id"},
            "label": {"table": f"{self.schema}.item", "column": "label"},
        }
        rule = {"id": "id determines label", "determinant": ["id"], "dependent": ["label"]}

        self.assertEqual([], check_normal_form(self.connection, {"mapping": mapping, "rules": [rule]}))

        with self.connection.cursor() as cursor:
            cursor.execute(f'ALTER TABLE "{self.schema}".item DROP CONSTRAINT item_pkey')
        errors = check_normal_form(self.connection, {"mapping": mapping, "rules": [rule]})
        self.assertTrue(any("no primary or unique key" in error for error in errors))

        split_mapping = {
            **mapping,
            "label": {"table": f"{self.schema}.other", "column": "label"},
        }
        split_errors = check_normal_form(
            self.connection, {"mapping": split_mapping, "rules": [rule]}
        )
        self.assertTrue(any("must share one table" in error for error in split_errors))

    def test_feature_check_does_not_require_a_join_in_the_view(
        self: InvariantTests,
    ) -> None:
        view = f"t13_feature_{uuid4().hex[:8]}"
        with self.connection.cursor() as cursor:
            cursor.execute(f'CREATE TABLE "{self.schema}".room (id integer)')
            cursor.execute(
                f'CREATE VIEW "{self.schema}"."{view}" AS '
                f'SELECT id FROM "{self.schema}".room'
            )
        item = {"schema": self.schema, "view": view, "features": ["left_join"]}

        self.assertEqual([], check_feature(self.connection, item, None))

    def test_index_check_requires_the_named_plan_index(self: InvariantTests) -> None:
        index = f"t13_index_{uuid4().hex[:8]}"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "report.sql").write_text(
                f"SELECT id FROM \"{self.schema}\".item WHERE label = 'alpha';\n",
                encoding="utf-8",
            )
            with self.connection.cursor() as cursor:
                cursor.execute(
                    f'CREATE TABLE "{self.schema}".item '
                    "(id integer, label text)"
                )
                cursor.execute(
                    f'CREATE INDEX "{index}" ON "{self.schema}".item (label)'
                )
                cursor.execute(
                    f'INSERT INTO "{self.schema}".item VALUES (1, \'alpha\')'
                )
            self.assertEqual(
                [],
                check_index(
                    root,
                    self.connection,
                    {"file": "report.sql", "index": index},
                    {(self.schema, index)},
                ),
            )
            errors = check_index(
                root,
                self.connection,
                {"file": "report.sql", "index": "missing_index"},
                {(self.schema, index)},
            )
            self.assertTrue(any("does not exist" in error for error in errors))

    def test_index_check_requires_the_leading_key_column(
        self: InvariantTests,
    ) -> None:
        index = f"t13_index_{uuid4().hex[:8]}"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "report.sql").write_text(
                f"SELECT id FROM \"{self.schema}\".item WHERE label = 'alpha';\n",
                encoding="utf-8",
            )
            with self.connection.cursor() as cursor:
                cursor.execute(
                    f'CREATE TABLE "{self.schema}".item '
                    "(id integer, label text)"
                )
                cursor.execute(
                    f'CREATE INDEX "{index}" ON "{self.schema}".item (label)'
                )
                cursor.execute(
                    f'INSERT INTO "{self.schema}".item VALUES (1, \'alpha\')'
                )
            errors = check_index(
                root,
                self.connection,
                {"file": "report.sql", "index": index, "key_column": "id"},
                {(self.schema, index)},
            )
            self.assertTrue(
                any(
                    "leads with label. The case expects id as the first column. "
                    "Change the index to lead with id." in error
                    for error in errors
                ),
                errors,
            )
            with self.connection.cursor() as cursor:
                cursor.execute(f'DROP INDEX "{self.schema}"."{index}"')
                cursor.execute(
                    f'CREATE INDEX "{index}" ON "{self.schema}".item (id, label)'
                )
            errors = check_index(
                root,
                self.connection,
                {"file": "report.sql", "index": index, "key_column": "id"},
                {(self.schema, index)},
            )
            self.assertTrue(
                any(
                    f"the query plan does not filter on id through index {index}. "
                    "Add a filter on id." in error
                    for error in errors
                ),
                errors,
            )

    def test_index_check_requires_the_query_to_use_the_index(
        self: InvariantTests,
    ) -> None:
        index = f"t13_index_{uuid4().hex[:8]}"
        other = f"t13_other_{uuid4().hex[:8]}"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "report.sql").write_text(
                f"SELECT id FROM \"{self.schema}\".item WHERE label = 'alpha';\n",
                encoding="utf-8",
            )
            with self.connection.cursor() as cursor:
                cursor.execute(
                    f'CREATE TABLE "{self.schema}".item '
                    "(id integer, label text)"
                )
                cursor.execute(
                    f'CREATE INDEX "{index}" ON "{self.schema}".item (id)'
                )
                cursor.execute(
                    f'CREATE INDEX "{other}" ON "{self.schema}".item (label)'
                )
                cursor.execute(
                    f'INSERT INTO "{self.schema}".item VALUES (1, \'alpha\')'
                )
            errors = check_index(
                root,
                self.connection,
                {"file": "report.sql", "index": index},
                {(self.schema, index), (self.schema, other)},
            )
            self.assertTrue(
                any(
                    f"the query plan does not use index {index}. It uses: {other}. "
                    f"Rewrite the query so it uses index {index}." in error
                    for error in errors
                ),
                errors,
            )

    def test_index_check_accepts_a_composite_index_that_applies_the_key(
        self: InvariantTests,
    ) -> None:
        required = f"t13_index_{uuid4().hex[:8]}"
        composite = f"t13_composite_{uuid4().hex[:8]}"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "report.sql").write_text(
                f'SELECT paid_on, COUNT(*) FROM "{self.schema}".payment\n'
                "WHERE term = '2024-1S'\n"
                "  AND paid_on >= DATE '2024-09-01'\n"
                "  AND paid_on <= DATE '2024-12-08'\n"
                "GROUP BY paid_on;\n",
                encoding="utf-8",
            )
            with self.connection.cursor() as cursor:
                cursor.execute(
                    f'CREATE TABLE "{self.schema}".payment '
                    "(student_id integer, term text, seq smallint, "
                    "amount numeric(10, 2), paid_on date)"
                )
                cursor.execute(
                    f'INSERT INTO "{self.schema}".payment '
                    "SELECT g, (ARRAY['2024-1S', '2024-2S', '2025-1S', "
                    "'2025-2S', '2026-1S', '2026-2S', '2027-1S', "
                    "'2027-2S'])[1 + (g % 8)], 1, 10, "
                    "DATE '2024-09-01' + ((g % 460)::integer) "
                    "FROM generate_series(1, 40000) AS g"
                )
                cursor.execute(
                    f'CREATE INDEX "{required}" ON "{self.schema}".payment '
                    "(paid_on)"
                )
                cursor.execute(
                    f'CREATE INDEX "{composite}" ON "{self.schema}".payment '
                    "(term, paid_on)"
                )
                cursor.execute(f'ANALYZE "{self.schema}".payment')
                cursor.execute("SET enable_seqscan = off")
                cursor.execute(
                    "EXPLAIN (FORMAT JSON, COSTS OFF) "
                    + (root / "report.sql").read_text(encoding="utf-8")
                )
                row = cursor.fetchone()
                cursor.execute("SET enable_seqscan = on")
            self.assertIsNotNone(row)
            plan = row[0] if row is not None else None
            used = plan_indexes(plan)
            self.assertIn(composite, {name for _, name in used})
            self.assertEqual(
                [],
                check_index(
                    root,
                    self.connection,
                    {
                        "file": "report.sql",
                        "index": required,
                        "key_column": "paid_on",
                    },
                    {(self.schema, required), (self.schema, composite)},
                ),
            )

    def test_feature_check_reads_a_report_file(self: InvariantTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reports = root / "reports"
            reports.mkdir()
            (reports / "outer.sql").write_text(
                "SELECT a.id FROM item AS a LEFT JOIN other AS b ON b.id = a.id;\n",
                encoding="utf-8",
            )
            item = {
                "file": "reports/outer.sql",
                "features": ["left_join"],
            }

            self.assertEqual([], check_feature(self.connection, item, root))
            errors = check_feature(
                self.connection,
                {"file": "reports/outer.sql", "features": ["intersect"]},
                root,
            )
            self.assertEqual(1, len(errors), errors)
            self.assertTrue(errors[0].startswith("warning:"), errors)

    def test_outer_join_check_accepts_each_join_direction(self: InvariantTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reports = root / "reports"
            reports.mkdir()
            report = reports / "outer.sql"
            item = {"file": "reports/outer.sql", "features": ["left_join"]}
            for query in (
                "SELECT 1 FROM room r RIGHT JOIN section s ON s.id = r.id;",
                "SELECT 1 FROM room r FULL OUTER\nJOIN section s ON s.id = r.id;",
                "SELECT 1 FROM room r LEFT OUTER JOIN section s ON s.id = r.id;",
            ):
                report.write_text(query, encoding="utf-8")
                self.assertEqual([], check_feature(self.connection, item, root), query)

    def test_null_handling_check_accepts_the_taught_forms(self: InvariantTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reports = root / "reports"
            reports.mkdir()
            report = reports / "nulls.sql"
            item = {"file": "reports/nulls.sql", "features": ["coalesce"]}
            for query in (
                "SELECT CASE WHEN label IS NULL THEN 'missing' ELSE label END FROM item;",
                "SELECT label FROM item WHERE label IS NULL;",
                "SELECT label FROM item WHERE label IS NOT NULL;",
                "SELECT NULLIF(label, '') FROM item;",
                "SELECT COALESCE(label, '') FROM item;",
            ):
                report.write_text(query, encoding="utf-8")
                self.assertEqual([], check_feature(self.connection, item, root), query)

    def test_null_handling_check_reads_a_view_used_by_the_report(
        self: InvariantTests,
    ) -> None:
        view = f"t13_null_view_{uuid4().hex[:8]}"
        with self.connection.cursor() as cursor:
            cursor.execute(f'CREATE TABLE "{self.schema}".room_name (label text)')
            cursor.execute(
                f'CREATE VIEW "{self.schema}"."{view}" AS '
                f'SELECT COALESCE(label, \'Unassigned\') AS room_name '
                f'FROM "{self.schema}".room_name'
            )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reports = root / "reports"
            reports.mkdir()
            (reports / "nulls.sql").write_text(
                f'SELECT room_name FROM "{self.schema}"."{view}";\n',
                encoding="utf-8",
            )

            self.assertEqual(
                [],
                check_feature(
                    self.connection,
                    {"file": "reports/nulls.sql", "features": ["coalesce"]},
                    root,
                ),
            )

    def test_feature_checks_ignore_comments_and_string_literals(
        self: InvariantTests,
    ) -> None:
        phrases = {
            "left_join": "LEFT JOIN",
            "union": "UNION",
            "intersect": "INTERSECT",
            "except": "EXCEPT",
            "self_join": "FROM item a JOIN item b ON true",
            "having": "HAVING count(*) > 1",
            "where_subquery": "WHERE id IN (SELECT id FROM other)",
            "correlated_subquery": "(SELECT id WHERE a.id = b.id)",
            "coalesce": "COALESCE(label, '')",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reports = root / "reports"
            reports.mkdir()
            report = reports / "feature.sql"
            for feature, phrase in phrases.items():
                item = {"file": "reports/feature.sql", "features": [feature]}
                for text in (
                    f"SELECT '{phrase}';\n",
                    f"-- {phrase}\nSELECT 1;\n",
                    f"/* {phrase} */ SELECT 1;\n",
                    f"/* outer /* {phrase} */ */ SELECT 1;\n",
                ):
                    with self.subTest(feature=feature, text=text):
                        report.write_text(text, encoding="utf-8")
                        warnings = check_feature(self.connection, item, root)
                        self.assertEqual(1, len(warnings), warnings)
                        self.assertTrue(warnings[0].startswith("warning:"), warnings)

    def test_feature_miss_warns_and_passes_the_invariant(self: InvariantTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reports = root / "reports"
            reports.mkdir()
            (reports / "outer.sql").write_text("SELECT 1;\n", encoding="utf-8")
            results = run_invariants(
                root,
                {
                    "invariants": [
                        {
                            "id": "lab2/outer-join",
                            "kind": "feature",
                            "stage": 2,
                            "group": "SQL Craft",
                            "file": "reports/outer.sql",
                            "features": ["left_join"],
                        }
                    ]
                },
                self.connection,
                "",
                root,
                2,
                set(),
            )

            self.assertEqual("pass", results[0]["status"])
            self.assertEqual([], results[0]["differences"])
            self.assertEqual(
                [
                    "warning: reports/outer.sql shows no outer join. "
                    "Explain your query under Checker Warnings in DESIGN.md."
                ],
                results[0]["warnings"],
            )

    def test_self_join_check_accepts_comma_and_qualified_tables(
        self: InvariantTests,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reports = root / "reports"
            reports.mkdir()
            report = reports / "self.sql"
            item = {"file": "reports/self.sql", "features": ["self_join"]}
            for query in (
                "SELECT a.id FROM item a, item b WHERE a.id <> b.id;",
                "SELECT a.id FROM public.item AS a JOIN public.item AS b ON a.id = b.id;",
            ):
                report.write_text(query, encoding="utf-8")
                self.assertEqual([], check_feature(self.connection, item, root), query)

    def test_report_case_requires_one_statement_and_accepts_trailing_comments(
        self: InvariantTests,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reports = root / "reports"
            cases = root / "cases"
            reports.mkdir()
            cases.mkdir()
            case_path = cases / "report.json"
            case_path.write_text(
                json.dumps({"report": "reports/report.sql"}), encoding="utf-8"
            )
            (cases / "report.expected").write_text(
                '{"columns":["one"],"rows":[["a;b"]]}\n', encoding="utf-8"
            )
            report_path = reports / "report.sql"
            report_path.write_text(
                "SELECT 'a;b' AS one; -- tail ;\n/* comment ; */\n",
                encoding="utf-8",
            )
            case = {
                "id": "lab2/report",
                "kind": "report",
                "file": "cases/report.json",
                "group": "Query Correctness",
            }

            passing = run_case(root, case, self.connection, "")
            self.assertEqual("pass", passing["status"])

            report_path.write_text(
                "SELECT 'a;b' AS one; SELECT 2 AS two;\n", encoding="utf-8"
            )
            failing = run_case(root, case, self.connection, "")
            self.assertEqual("fail", failing["status"])
            actual = cast(dict[str, object], failing["actual"])
            rows = cast(list[list[object]], actual["rows"])
            self.assertEqual(
                [["report file reports/report.sql holds 2 statements. "
                  "The report must be one query."]],
                rows,
            )

    def test_protected_files_allow_pair_cases_and_reject_changes(
        self: InvariantTests,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "pair"
            runner_dir = Path(directory) / "runner"
            root.mkdir()
            runner_dir.mkdir()
            (root / "check.sh").write_bytes(b"published\n")
            expected_hash = hashlib.sha256(b"published\n").hexdigest()
            published_path = "cases/published/lab1/list.sql"
            published_hash = hashlib.sha256(b"SELECT 1;\n").hexdigest()
            (runner_dir / "protected.json").write_text(
                json.dumps(
                    {
                        "files": {
                            "check.sh": expected_hash,
                            published_path: published_hash,
                        },
                        "optional_files": [published_path],
                        "allowed_paths": ["cases/pair", "migrations", "load.sql", ".stage"],
                    }
                ),
                encoding="utf-8",
            )
            (root / ".git").mkdir()
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)
            (root / ".stage").write_text("1\n", encoding="utf-8")
            pair_cases = root / "cases" / "pair"
            pair_cases.mkdir(parents=True)
            (pair_cases / "new.sql").write_text("SELECT 1;\n", encoding="utf-8")

            self.assertEqual([], check_protected(root, runner_dir))

            published_case = root / published_path
            published_case.parent.mkdir(parents=True)
            published_case.write_text("SELECT 1;\n", encoding="utf-8")
            self.assertEqual([], check_protected(root, runner_dir))

            published_case.write_text("SELECT 2;\n", encoding="utf-8")
            errors = check_protected(root, runner_dir)
            self.assertTrue(
                any(f"protected file changed: {published_path}" in error for error in errors)
            )
            published_case.write_text("SELECT 1;\n", encoding="utf-8")

            (root / "check.sh").write_bytes(b"published\r\n")
            self.assertEqual([], check_protected(root, runner_dir))

            (root / "check.sh").write_text("changed\n", encoding="utf-8")
            errors = check_protected(root, runner_dir)
            self.assertTrue(any("protected file changed: check.sh" in error for error in errors))

    def test_written_check_rejects_an_empty_template(self: InvariantTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            answer = root / "answers.md"
            answer.write_text("# Summary\n\nTODO\n", encoding="utf-8")
            item = {"file": "answers.md", "sections": ["Summary"]}

            errors = check_written(root, item)

            self.assertTrue(any("empty or still a template" in error for error in errors))
            answer.write_text("# Summary\n\nThe result is 3.\n", encoding="utf-8")
            self.assertEqual([], check_written(root, item))

    def test_work_sql_invariant_rejects_python_report_files(
        self: InvariantTests,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reports = root / "reports"
            reports.mkdir()
            (reports / "count.py").write_text("print(1)\n", encoding="utf-8")
            manifest: dict[str, object] = {
                "invariants": [
                    {
                        "id": "work-is-sql",
                        "stage": 1,
                        "kind": "work_sql",
                        "files": ["reports/count.py"],
                    }
                ]
            }

            results = run_invariants(
                root,
                manifest,
                self.connection,
                os.environ["CMSC127_DATABASE_URL"],
                root,
                1,
                set(),
            )

            self.assertEqual("fail", results[0]["status"])
            self.assertIn("must be a SQL file", str(results[0]["differences"]))


SIGNAL_DRIVER = '''\
"""Run the runner and send a stop signal at one named point in its cleanup."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import signal
import subprocess
import sys
import sysconfig
import threading
import time

HARNESS = Path(sys.argv[1])
MARKER = Path(sys.argv[2])
POINT = sys.argv[3]
TOKEN = sys.argv[4]
sys.path.insert(0, str(HARNESS))

import psycopg
import runner

SENT = {"child-run": 0, "nested": 0}
# The stop signals this platform has, and the handlers that were in place
# before the run. The tail reports which of them came back.
STOP_NUMBERS = [
    getattr(signal, name)
    for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK")
    if hasattr(signal, name)
]
ORIGINAL = {number: signal.getsignal(number) for number in STOP_NUMBERS}
REPORT_POINTS = ("report", "selftest-report")
# The points that send the first stop signal at the boundary where the guard
# puts the old handlers back. The last check for a held signal runs before
# that boundary, so a signal that arrives here is the one that can be lost.
RESTORE_POINTS = (
    "restore",
    "selftest-restore",
    "console-restore",
    "selftest-console-restore",
)
# The points whose drop fails, so the run reports the database it could not
# remove. The restore points join them when the test also wants that report.
FAILED_DROP_POINTS = REPORT_POINTS + ("restore-fail", "selftest-restore-fail")
RESTORE_FAIL_POINTS = ("restore-fail", "selftest-restore-fail")
# The points that send a second stop after the SIGINT handler changes. That is
# the interval where the default Ctrl+C handler can replace the first stop's
# code with the Windows control status.
MIXED_RESTORE_POINTS = (
    "console-mixed-restore",
    "selftest-console-mixed-restore",
)
# The point that sends the stop at the entry of _finish_run, before its first
# statement. The phase is still body there, so a stop at that boundary used to
# skip the drop.
FINISH_POINTS = ("finish", "selftest-finish")
# The point that latches the first stop, then delivers Ctrl+C between the
# handler replacements. SIGINT must go back last, so the Ctrl+C still reaches
# the guard's handler.
BETWEEN_POINTS = ("restore-between", "selftest-restore-between")
# The point that nests a second stop at the entry of the first stop's handler.
NESTED_POINTS = ("nested", "selftest-nested")
# The point that delivers a native Ctrl+C just before the guard registers
# SIGINT. Python's default handler is still in place there, so the interrupt
# arrives as KeyboardInterrupt.
INSTALL_BEFORE_POINTS = ("install-before", "selftest-install-before")
# The point where the caller's own handler raises between the restore calls.
# Every slot must still go back.
CALLER_RAISES_POINTS = ("caller-raises", "selftest-caller-raises")
# The console stop that this run delivers, as the name of a Python signal.
CONSOLE_SIGNAL = os.environ.get("CMSC127_CONSOLE_SIGNAL", "")
# The pair points deliver two stops at the boundary where the handlers used to
# change. The first names the exit code, and the second must not replace it.
PAIR_POINTS = ("pair", "selftest-pair")
FIRST_SIGNAL = os.environ.get("CMSC127_FIRST_SIGNAL", "")
SECOND_SIGNAL = os.environ.get("CMSC127_SECOND_SIGNAL", "")
# The CLI path keeps the guard installed until the process exits. A private
# console lets this process send a real Ctrl+C to itself, because Windows
# cannot aim that event at a process group.
KEEP_GUARD = os.environ.get("CMSC127_KEEP_GUARD") == "1"
PRIVATE_CONSOLE = os.environ.get("CMSC127_PRIVATE_CONSOLE") == "1"


class _Token:
    """Stand in for the UUID, so the test knows the name the run created."""

    hex = TOKEN


def note(text: str) -> None:
    with MARKER.open("a", encoding="utf-8") as handle:
        handle.write(text + "\\n")


def stop(signum: int = signal.SIGTERM) -> None:
    os.kill(os.getpid(), signum)


# Set when the guard's own handler records a signal, so a point that waits for
# a console event wakes as soon as the event arrives.
ARRIVED = threading.Event()
plain_handle = runner._StopGuard.handle


def handle(guard, signum, frame):
    ARRIVED.set()
    return plain_handle(guard, signum, frame)


runner._StopGuard.handle = handle


def console_stop() -> None:
    """Deliver the console stop for this run at the current point.

    Ctrl+Break is a real console event, so the parent sends it to this
    process's own group and this call waits for the guard to record it.
    Ctrl+C cannot be sent to a process group on Windows, and a console-wide
    event would reach the test runner, so this process raises SIGINT itself.
    CPython's console handler for CTRL_C_EVENT calls the same
    signal_handler(SIGINT) that raise_signal calls, so the run meets the same
    code either way.
    """
    if CONSOLE_SIGNAL == "SIGINT":
        note("raising SIGINT")
        signal.raise_signal(signal.SIGINT)
    else:
        note("waiting for SIGBREAK")
        ARRIVED.wait(8)


def deliver() -> None:
    """Deliver the stop that this point asks for."""
    if POINT.startswith("console") or POINT.startswith("selftest-console"):
        console_stop()
    else:
        stop()


def console_event_number(signal_name: str) -> int:
    """Return the console event number for a stop. Windows only.

    The event numbers exist only on Windows, so they are looked up at run
    time and the type checker on a POSIX host still reads this file.
    """
    name = "CTRL_C_EVENT" if signal_name == "SIGINT" else "CTRL_BREAK_EVENT"
    return getattr(signal, name)


def wait_for_arrival(seconds: float) -> bool:
    """Wait for the guard to record a stop, and let its handler run.

    A sleep returns to the interpreter loop, so a pending signal reaches the
    Python handler. A blocking wait does not.
    """
    deadline = time.monotonic() + seconds
    while not ARRIVED.is_set() and time.monotonic() < deadline:
        time.sleep(0.01)
    return ARRIVED.is_set()


def send_console(signal_name: str) -> None:
    """Send a console event to this process's own console. Windows only.

    The console holds this process alone, so the event cannot reach another
    process. The check below proves that before the event goes out.
    """
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    processes = (ctypes.c_uint32 * 32)()
    count = api.GetConsoleProcessList(processes, len(processes))
    if count != 1 or processes[0] != os.getpid():
        raise RuntimeError(f"Expected a private console: {count} {list(processes)}")
    ARRIVED.clear()
    # A parent that ignores Ctrl+C passes that flag to this process, and the
    # console then drops every CTRL_C_EVENT. Clearing the flag makes the
    # console deliver the event to this process's own handler.
    api.SetConsoleCtrlHandler(None, False)
    note("private console event " + signal_name)
    if not api.GenerateConsoleCtrlEvent(console_event_number(signal_name), 0):
        raise ctypes.WinError(ctypes.get_last_error())
    if not wait_for_arrival(8):
        raise RuntimeError("The guard did not receive the console event")


def deliver_signal(signal_name: str) -> None:
    """Deliver one stop to this process and wait for the guard to record it."""
    if PRIVATE_CONSOLE:
        send_console(signal_name)
        return
    note("signal " + signal_name)
    os.kill(os.getpid(), getattr(signal, signal_name))
    wait_for_arrival(8)


def deliver_held(signal_name: str) -> None:
    """Deliver a stop that a critical section holds.

    The guard's handler does not run inside the section, so this call does not
    wait for it. The section delivers the stop when it ends.
    """
    if PRIVATE_CONSOLE:
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        processes = (ctypes.c_uint32 * 32)()
        count = api.GetConsoleProcessList(processes, len(processes))
        if count != 1 or processes[0] != os.getpid():
            raise RuntimeError(f"Expected a private console: {count} {list(processes)}")
        ARRIVED.clear()
        api.SetConsoleCtrlHandler(None, False)
        note("private console event " + signal_name)
        if not api.GenerateConsoleCtrlEvent(console_event_number(signal_name), 0):
            raise ctypes.WinError(ctypes.get_last_error())
        time.sleep(0.2)
        return
    note("signal " + signal_name)
    os.kill(os.getpid(), getattr(signal, signal_name))


def raise_held(signal_name: str) -> None:
    """Deliver a stop through CPython's own path, without waiting.

    The section holds the stop, so the guard's handler does not run here.
    """
    note("raising " + signal_name + " inside the section")
    signal.raise_signal(getattr(signal, signal_name))


def raise_stop(signal_name: str) -> None:
    """Deliver a stop through CPython's own signal path.

    CPython's console handler for CTRL_C_EVENT and CTRL_BREAK_EVENT calls the
    same signal_handler that raise_signal calls, so the run meets the same
    code either way. This needs no console, so it works on both platforms.
    """
    note("raising " + signal_name)
    ARRIVED.clear()
    signal.raise_signal(getattr(signal, signal_name))
    if not wait_for_arrival(8):
        raise RuntimeError("The guard did not receive " + signal_name)


def finish_stop() -> None:
    """Deliver the stop at the entry of _finish_run.

    Windows cannot aim Ctrl+C at a process group, so this process raises
    SIGINT itself, which is the code path the console handler takes. A
    Ctrl+Break case waits for the parent's console event. POSIX sends the
    named signal to this process.
    """
    if CONSOLE_SIGNAL == "SIGINT":
        note("raising SIGINT at the finish entry")
        signal.raise_signal(signal.SIGINT)
    elif sys.platform != "win32":
        note("signal " + CONSOLE_SIGNAL + " at the finish entry")
        os.kill(os.getpid(), getattr(signal, CONSOLE_SIGNAL))
    else:
        note("waiting for SIGBREAK at the finish entry")
        ARRIVED.wait(8)


runner.uuid4 = _Token
plain_execute = runner._execute
plain_run_pair = runner._run_pair
plain_drop_database = runner._drop_database
plain_report_cleanup_failure = runner._report_cleanup_failure
plain_restore = runner._StopGuard.restore
plain_install = runner._StopGuard.install
plain_finish_run = runner._finish_run
plain_database_url = runner._database_url


def finish_run(guard, name, admin):
    """Send the stop at the call boundary into _finish_run.

    The wrapper's own entry is the check point that _finish_run's entry would
    be, so a stop here is the stop that used to skip the drop.
    """
    if POINT in FINISH_POINTS:
        finish_stop()
    return plain_finish_run(guard, name, admin)


runner._finish_run = finish_run


def install_before(guard):
    """Deliver a native Ctrl+C just before the guard registers SIGINT.

    Python's default handler is still in place at that moment, so the
    interrupt arrives as KeyboardInterrupt. The guard must record it as a
    SIGINT stop, finish its cleanup, and replay code 130.
    """
    real_signal = signal.signal
    sent = {"done": False}

    def change(number, handler):
        if not sent["done"] and number == signal.SIGINT:
            sent["done"] = True
            note("before installing SIGINT")
            deliver_signal("SIGINT")
        return real_signal(number, handler)

    signal.signal = change
    try:
        return plain_install(guard)
    finally:
        signal.signal = real_signal


def nested_install(guard):
    """Install the guard, with this point's delivery around the call.

    The install-before point sends a native Ctrl+C before the guard registers
    SIGINT. The nested point wraps the guard's handler to nest a second stop.
    """
    if POINT in INSTALL_BEFORE_POINTS:
        return install_before(guard)
    plain_install(guard)
    if POINT not in NESTED_POINTS:
        return
    sent = {"done": False}

    def wrap():
        def handler(signum, frame):
            if not sent["done"]:
                sent["done"] = True
                note("nested " + SECOND_SIGNAL + " at the entry of " + FIRST_SIGNAL)
                signal.raise_signal(getattr(signal, SECOND_SIGNAL))
            return guard.handle(signum, frame)

        return handler

    for number in list(guard.previous):
        signal.signal(number, wrap())


runner._StopGuard.install = nested_install


def execute(cursor, statement, parameters=None):
    if POINT == "mixed-before-drop" and statement.startswith("DROP DATABASE"):
        note("second SIGHUP")
        stop(signal.SIGHUP)
    if POINT.endswith("drop") and statement.startswith("DROP DATABASE"):
        note("before-drop")
        deliver()
    result = plain_execute(cursor, statement, parameters)
    if statement.startswith("CREATE DATABASE"):
        note("created " + statement.split('"')[1])
        if POINT.endswith("create"):
            stop()
    return result


def run_pair(*args, **kwargs):
    if POINT in ("drop", "mixed-before-drop", "report") and SENT["child-run"] == 0:
        SENT["child-run"] = 1
        note("child-run")
        stop()
    return plain_run_pair(*args, **kwargs)


def database_url(base_url, name):
    """Send the first stop once the run owns its database and is in body.

    Both entry paths call this after they move the phase to body, so the stop
    arrives during the run and not during the create.
    """
    if POINT in NESTED_POINTS and SENT["nested"] == 0:
        SENT["nested"] = 1
        note("raising " + FIRST_SIGNAL + " during the run")
        signal.raise_signal(getattr(signal, FIRST_SIGNAL))
    return plain_database_url(base_url, name)


def drop_database(name, admin):
    """Fail the drop, so the run reports the database it could not remove."""
    if POINT in FAILED_DROP_POINTS:
        note("drop failed")
        raise psycopg.OperationalError("the server refused the drop")
    return plain_drop_database(name, admin)


def report_cleanup_failure(name):
    """Send the second signal at the report boundary, then report."""
    if POINT in REPORT_POINTS:
        note("second SIGHUP")
        stop(signal.SIGHUP)
    return plain_report_cleanup_failure(name)


def restore_between(guard):
    """Latch the first stop, then deliver Ctrl+C between the replacements.

    The Ctrl+C arrives once the last non-SIGINT handler is back and before the
    SIGINT handler goes back. The guard must still own SIGINT there, so the
    Ctrl+C is recorded and the loop finishes. The caller then sees the latched
    stop.

    The line trace fires at the first line of restore that runs after that
    handler is back. That line sits outside the retry that catches a Ctrl+C
    raised inside signal.signal, so a default Ctrl+C handler there ends the
    run instead of being recorded.
    """
    note("before restore")
    raise_stop(FIRST_SIGNAL)
    remaining = [number for number in guard.previous if number != signal.SIGINT]
    last = remaining[-1] if remaining else None
    expected = guard.previous[last] if last is not None else None
    changed = {"done": False}
    restore_code = plain_restore.__code__

    def on_line(frame, event, argument):
        if event == "line" and not changed["done"] and last is not None:
            if signal.getsignal(last) is expected:
                changed["done"] = True
                sys.settrace(None)
                note("restored " + signal.Signals(last).name + " before the Ctrl+C")
                raise_held("SIGINT")
        return on_line

    def on_call(frame, event, argument):
        if event == "call" and frame.f_code is restore_code:
            return on_line
        return None

    sys.settrace(on_call)
    try:
        return plain_restore(guard)
    finally:
        sys.settrace(None)


def caller_raises(guard):
    """Latch the first stop, then let the caller's handler raise between calls.

    The caller owns every stop handler and each one raises when it runs. The
    first stop is latched while the guard still owns the handlers. Then a
    native stop arrives once the last non-SIGINT handler is back. The caller's
    handler raises there. Every slot must still go back.
    """
    note("before restore")
    deliver_signal(FIRST_SIGNAL)
    remaining = [number for number in guard.previous if number != signal.SIGINT]
    last = remaining[-1] if remaining else None
    expected = guard.previous[last] if last is not None else None
    changed = {"done": False}
    restore_code = plain_restore.__code__

    def on_line(frame, event, argument):
        if event == "line" and not changed["done"] and last is not None:
            if signal.getsignal(last) is expected:
                changed["done"] = True
                note("restored " + signal.Signals(last).name + " before the caller stop")
                deliver_held(signal.Signals(last).name)
        return on_line

    def on_call(frame, event, argument):
        if event == "call" and frame.f_code is restore_code:
            return on_line
        return None

    sys.settrace(on_call)
    try:
        return plain_restore(guard)
    finally:
        sys.settrace(None)


def restore(guard):
    """Send the first stop signal at the boundary where the handlers change.

    The guard has already made its last check for a held signal when this
    runs, so a signal here is held and never replayed unless the guard checks
    once more after it puts the old handlers back.

    The mixed points send a second stop after the SIGINT handler changes.
    That is the interval where the default Ctrl+C handler can replace the
    first stop's code, so the run must still end with the first code.
    """
    if POINT in CALLER_RAISES_POINTS:
        return caller_raises(guard)
    if POINT in BETWEEN_POINTS:
        return restore_between(guard)
    if POINT in RESTORE_POINTS or POINT in RESTORE_FAIL_POINTS:
        note("before restore")
        deliver()
    if POINT not in MIXED_RESTORE_POINTS:
        return plain_restore(guard)
    note("before restore")
    console_stop()
    old_signal = signal.signal

    def restore_signal(number, handler):
        result = old_signal(number, handler)
        if number == signal.SIGINT:
            note("SIGINT handler restored, first pending " + str(guard.pending))
            signal.raise_signal(signal.SIGINT)
            time.sleep(0.5)
        return result

    signal.signal = restore_signal
    try:
        return plain_restore(guard)
    finally:
        signal.signal = old_signal


runner._execute = execute
runner._run_pair = run_pair
runner._database_url = database_url
runner._drop_database = drop_database
runner._report_cleanup_failure = report_cleanup_failure
runner._StopGuard.restore = restore
plain_end_run = runner._end_run


def end_run(guard, code, keep_guard):
    """Deliver the pair at the boundary where the handlers used to change.

    The CLI path keeps the guard installed there, so the second stop is held
    like the first. The first stop's code must still name the exit.
    """
    if POINT in PAIR_POINTS:
        note("before end-run")
        deliver_signal(FIRST_SIGNAL)
        if SECOND_SIGNAL:
            deliver_signal(SECOND_SIGNAL)
    return plain_end_run(guard, code, keep_guard)


runner._end_run = end_run


class _SkipChild:
    """Stand in for subprocess, so the self-test skips its child suite."""

    CompletedProcess = subprocess.CompletedProcess

    @staticmethod
    def run(*args, **kwargs):
        if POINT == "selftest-report":
            note("child-run")
            stop()
        return subprocess.CompletedProcess(args[0] if args else [], 0)


if POINT in CALLER_RAISES_POINTS:
    # The caller's own handlers go in before the run, so install replaces
    # them. Each one raises when it runs. The tail then reports whether every
    # one of them came back.
    def caller_handler(number, frame):
        note("caller signal " + signal.Signals(number).name)
        ARRIVED.set()
        raise RuntimeError("caller custom stop")

    for number in STOP_NUMBERS:
        signal.signal(number, caller_handler)
    ORIGINAL = {number: signal.getsignal(number) for number in STOP_NUMBERS}


if KEEP_GUARD:
    # The CLI path. The process ends when the run ends, so the guard stays
    # installed and the stop becomes the process exit status.
    if POINT.startswith("selftest"):
        runner.subprocess = _SkipChild
        raise SystemExit(runner._self_test(keep_guard=True))
    raise SystemExit(runner.main(sys.argv[5:], keep_guard=True))

code = 0
try:
    if POINT.startswith("selftest"):
        runner.subprocess = _SkipChild
        code = runner._self_test()
    else:
        code = runner.main(sys.argv[5:])
except SystemExit as requested:
    code = requested.code if isinstance(requested.code, int) else 1
noted = signal.getsignal(signal.SIGTERM) is signal.SIG_DFL
note("handler " + ("default" if noted else "custom"))
note("non-SIGINT handlers restored " + str(all(
    signal.getsignal(number) is handler
    for number, handler in ORIGINAL.items()
    if number != signal.SIGINT)))
note("all handlers restored " + str(all(
    signal.getsignal(number) is handler
    for number, handler in ORIGINAL.items())))
raise SystemExit(code)
'''

PHASE_DRIVER = '''\
"""Run the runner and call the guard's handler at one line event."""

from __future__ import annotations

from pathlib import Path
import signal
import subprocess
import sys

HARNESS = Path(sys.argv[1])
MARKER = Path(sys.argv[2])
POINT = sys.argv[3]
TOKEN = sys.argv[4]
ENTRY = sys.argv[5]
FIRST = sys.argv[6]
SECOND = sys.argv[7]
KEEP = sys.argv[8] == "1"
ROOT = Path(sys.argv[9])
RESOURCES = Path(sys.argv[10])
sys.path.insert(0, str(HARNESS))

import runner

# The functions whose line events are injection points. install, restore,
# _restore_handler, _end_run and stop_if_pending hold every handler change and
# every exit decision, so a stop at any of their lines must keep the first
# code.
TARGETS = ("install", "restore", "_restore_handler", "_end_run", "stop_if_pending")
# The call boundary into _finish_run is the point where a stop used to skip
# the drop. The tracer injects there before the first line of the function.
FINISH = "_finish_run"
STOPS = [
    getattr(signal, name)
    for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK")
    if hasattr(signal, name)
]
ORIGINAL = {number: signal.getsignal(number) for number in STOPS}
GUARD = {"value": None}
SEEN: list[tuple[str, int]] = []
INJECTED = {"done": False}


def note(text: str) -> None:
    with MARKER.open("a", encoding="utf-8") as handle:
        handle.write(text + "\\n")


class _Token:
    """Stand in for the UUID, so the test knows the name the run created."""

    hex = TOKEN


class _SkipChild:
    """Stand in for subprocess, so the self-test skips its child suite."""

    CompletedProcess = subprocess.CompletedProcess

    @staticmethod
    def run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0] if args else [], 0)


plain_init = runner._StopGuard.__init__


def init(self):
    plain_init(self)
    GUARD["value"] = self


def fake_exit(code: int) -> None:
    """Stand in for os._exit in the final phase. The process ends here."""
    note("os._exit " + str(code))
    raise SystemExit(code)


runner._StopGuard.__init__ = init
runner.uuid4 = _Token
runner.subprocess = _SkipChild
runner.os._exit = fake_exit


def inject() -> None:
    """Send the first stop, then a different one, at this point."""
    guard = GUARD["value"]
    if guard is None or INJECTED["done"]:
        return
    INJECTED["done"] = True
    note("inject " + FIRST + " then " + SECOND)
    try:
        guard.handle(getattr(signal, FIRST), None)
    finally:
        guard.handle(getattr(signal, SECOND), None)


def local(frame, event, arg):
    if event == "line":
        SEEN.append((frame.f_code.co_name, frame.f_lineno))
        if POINT == "line:" + str(len(SEEN) - 1):
            inject()
    return local


def tracer(frame, event, arg):
    if event == "call" and frame.f_code.co_name in TARGETS:
        return local
    if event == "call" and frame.f_code.co_name == FINISH:
        SEEN.append((FINISH, 0))
        if POINT == "line:" + str(len(SEEN) - 1):
            inject()
        return None
    return None


def run() -> int:
    if ENTRY == "selftest":
        return runner._self_test(keep_guard=KEEP)
    return runner.main(
        ["1", "--root", str(ROOT), "--runner-dir", str(RESOURCES)], keep_guard=KEEP
    )


code = 0
try:
    sys.settrace(tracer)
    try:
        code = run()
        if POINT == "after-return":
            inject()
    finally:
        sys.settrace(None)
except SystemExit as requested:
    code = requested.code if isinstance(requested.code, int) else 1
note("exit " + str(code))
note("restored " + str(all(signal.getsignal(n) is h for n, h in ORIGINAL.items())))
note("points " + str(len(SEEN)))
for name, line in SEEN:
    note(f"line {name}:{line}")
raise SystemExit(code)
'''

SECTION_DRIVER = '''\
"""Deliver a native stop at one line event inside a critical section."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

HARNESS = Path(sys.argv[1])
MARKER = Path(sys.argv[2])
POINT = sys.argv[3]
TOKEN = sys.argv[4]
ENTRY = sys.argv[5]
FIRST = sys.argv[6]
ROOT = Path(sys.argv[7])
RESOURCES = Path(sys.argv[8])
sys.path.insert(0, str(HARNESS))

import runner

# The two functions that change a handler. Every line event inside them is an
# injection point.
TARGETS = ("install", "restore")
STOPS = [
    getattr(signal, name)
    for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK")
    if hasattr(signal, name)
]
ORIGINAL = {number: signal.getsignal(number) for number in STOPS}
GUARD = {"value": None}
SEEN: list[tuple[str, int]] = []
# Every handler change and every guard handler call, in order. A handler call
# that sits before the last change ran inside the section.
EVENTS: list[str] = []
DELIVERED = {"done": False}
PRIVATE_CONSOLE = os.environ.get("CMSC127_PRIVATE_CONSOLE") == "1"


def note(text: str) -> None:
    with MARKER.open("a", encoding="utf-8") as handle:
        handle.write(text + "\\n")


class _Token:
    """Stand in for the UUID, so the test knows the name the run created."""

    hex = TOKEN


class _SkipChild:
    """Stand in for subprocess, so the self-test skips its child suite."""

    CompletedProcess = subprocess.CompletedProcess

    @staticmethod
    def run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0] if args else [], 0)


plain_init = runner._StopGuard.__init__
plain_handle = runner._StopGuard.handle


def init(self):
    plain_init(self)
    GUARD["value"] = self


def handle(self, signum, frame):
    """Record a guard handler call, so the test can place it."""
    EVENTS.append("handle " + str(signum))
    return plain_handle(self, signum, frame)


real_signal = signal.signal


def change(number, handler):
    """Record a handler change, so the test can place it."""
    EVENTS.append("change " + str(number))
    return real_signal(number, handler)


runner._StopGuard.__init__ = init
runner._StopGuard.handle = handle
signal.signal = change

# The section boundaries, where the runner has them. A runner without them
# has no section, so the test falls back to the first and last change.
plain_begin = getattr(runner._StopGuard, "_begin_section", None)
plain_end = getattr(runner._StopGuard, "_end_section", None)

if plain_begin is not None:
    def begin(self):
        EVENTS.append("begin")
        return plain_begin(self)

    runner._StopGuard._begin_section = begin

if plain_end is not None:
    def end(self):
        EVENTS.append("end")
        return plain_end(self)

    runner._StopGuard._end_section = end
runner.uuid4 = _Token
runner.subprocess = _SkipChild


def deliver() -> None:
    """Send the stop to this process, inside the section.

    A console event runs on its own thread, so this call waits until the
    section records it. A POSIX signal stays pending in the kernel, so the
    call returns at once.
    """
    guard = GUARD["value"]
    inside = guard is not None and getattr(guard, "section", False)
    if PRIVATE_CONSOLE:
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        processes = (ctypes.c_uint32 * 32)()
        count = api.GetConsoleProcessList(processes, len(processes))
        if count != 1 or processes[0] != os.getpid():
            raise RuntimeError(f"Expected a private console: {count} {list(processes)}")
        api.SetConsoleCtrlHandler(None, False)
        name = "CTRL_C_EVENT" if FIRST == "SIGINT" else "CTRL_BREAK_EVENT"
        note("private console event " + FIRST)
        if not api.GenerateConsoleCtrlEvent(getattr(signal, name), 0):
            raise ctypes.WinError(ctypes.get_last_error())
        if not inside:
            # The section is not open yet, so the event reaches the guard's
            # handler at once. There is nothing to wait for.
            time.sleep(0.2)
            return
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if guard is None or getattr(guard, "console_stop", None) is not None:
                return
            time.sleep(0.01)
        raise RuntimeError("The section did not record the console event")
    note("signal " + FIRST)
    os.kill(os.getpid(), getattr(signal, FIRST))


def local(frame, event, arg):
    if event == "line":
        SEEN.append((frame.f_code.co_name, frame.f_lineno))
        if POINT == "line:" + str(len(SEEN) - 1) and not DELIVERED["done"]:
            DELIVERED["done"] = True
            note("deliver at " + frame.f_code.co_name + ":" + str(frame.f_lineno))
            deliver()
    return local


def tracer(frame, event, arg):
    if event == "call" and frame.f_code.co_name in TARGETS:
        return local
    return None


def run() -> int:
    if ENTRY == "selftest":
        return runner._self_test()
    return runner.main(["1", "--root", str(ROOT), "--runner-dir", str(RESOURCES)])


code = 0
failure = ""
try:
    sys.settrace(tracer)
    try:
        code = run()
    finally:
        sys.settrace(None)
except SystemExit as requested:
    code = requested.code if isinstance(requested.code, int) else 1
except BaseException as error:
    failure = type(error).__name__
    code = 1
guard = GUARD["value"]
note("exit " + str(code))
note("failure " + failure)
note("pending " + str(None if guard is None else guard.pending))
note("events " + "|".join(EVENTS))
note("restored " + str(all(signal.getsignal(n) is h for n, h in ORIGINAL.items())))
note("points " + str(len(SEEN)))
for name, line in SEEN:
    note(f"line {name}:{line}")
raise SystemExit(code)
'''

SECTION_STOP_DRIVER = '''\
"""Deliver a native stop inside a critical section and report the result."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import signal
import sys
import time

HARNESS = Path(sys.argv[1])
MARKER = Path(sys.argv[2])
MODE = sys.argv[3]
sys.path.insert(0, str(HARNESS))

import runner

STOPS = [
    getattr(signal, name)
    for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK")
    if hasattr(signal, name)
]
ORIGINAL = {number: signal.getsignal(number) for number in STOPS}
PRIVATE_CONSOLE = os.environ.get("CMSC127_PRIVATE_CONSOLE") == "1"


def note(text: str) -> None:
    with MARKER.open("a", encoding="utf-8") as handle:
        handle.write(text + "\\n")


def send(event_name: str) -> None:
    """Send a console event to this process's own console. Windows only."""
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    processes = (ctypes.c_uint32 * 32)()
    count = api.GetConsoleProcessList(processes, len(processes))
    if count != 1 or processes[0] != os.getpid():
        raise RuntimeError(f"Expected a private console: {count} {list(processes)}")
    api.SetConsoleCtrlHandler(None, False)
    note("private console event " + event_name)
    if not api.GenerateConsoleCtrlEvent(getattr(signal, event_name), 0):
        raise ctypes.WinError(ctypes.get_last_error())


def wait_for_record(guard, seconds: float = 8) -> bool:
    """Wait until the section records the console event."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if getattr(guard, "console_stop", None) is not None:
            return True
        time.sleep(0.01)
    return False


def caller_handler(number, frame):
    note("caller signal " + signal.Signals(number).name)
    raise ValueError("caller custom stop")


def caller_keyboard(number, frame):
    note("caller signal " + signal.Signals(number).name)
    raise KeyboardInterrupt("caller custom stop")


def caller_raises(handler=caller_handler) -> None:
    """Raise from a caller handler that the section holds.

    The caller owns every stop handler and each one raises. The guard installs
    its own handlers, then restore puts the caller's handlers back. A native
    stop arrives inside the restore section. The section must hold it, and the
    caller's handler must run when the section ends.
    """
    for number in STOPS:
        signal.signal(number, handler)
    ORIGINAL.clear()
    ORIGINAL.update({number: signal.getsignal(number) for number in STOPS})
    guard = runner._StopGuard()
    guard.install()
    real_signal = signal.signal
    sent = {"done": False}

    def change(number, handler):
        if not sent["done"] and number == signal.SIGINT:
            sent["done"] = True
            note("before restoring SIGINT")
            if PRIVATE_CONSOLE:
                send("CTRL_C_EVENT")
                if not wait_for_record(guard):
                    raise RuntimeError("The section did not record the event")
            else:
                note("signal SIGINT")
                os.kill(os.getpid(), signal.SIGINT)
        return real_signal(number, handler)

    signal.signal = change
    try:
        guard.restore()
    except BaseException as error:
        note("raised " + type(error).__name__ + ": " + str(error))
    finally:
        signal.signal = real_signal
    note("restored " + str(all(signal.getsignal(n) is h for n, h in ORIGINAL.items())))


def install_mixed() -> None:
    """Send Ctrl+C first, then a second stop at the record.

    POSIX: the Ctrl+C arrives before the mask, so Python's default handler
    raises KeyboardInterrupt. The second stop arrives at the entry of
    record_interrupt, inside the section. The section must hold it, so the
    first stop keeps code 130.

    Windows: the native handler holds the Ctrl+C, so no KeyboardInterrupt
    arrives. The second stop arrives after the SIGINT replacement, inside the
    section. The first stop still keeps code 130.
    """
    guard = runner._StopGuard()
    real_signal = signal.signal
    real_record = runner._StopGuard.record_interrupt
    sent = {"first": False, "second": False}

    def change(number, handler):
        if not sent["first"] and number == signal.SIGINT:
            sent["first"] = True
            note("before installing SIGINT")
            if PRIVATE_CONSOLE:
                send("CTRL_C_EVENT")
                wait_for_record(guard)
            else:
                note("signal SIGINT")
                os.kill(os.getpid(), signal.SIGINT)
        previous = real_signal(number, handler)
        if not sent["second"] and number == signal.SIGINT:
            sent["second"] = True
            note("after installing SIGINT")
            if PRIVATE_CONSOLE:
                send("CTRL_BREAK_EVENT")
                time.sleep(0.2)
            else:
                note("signal SIGTERM")
                os.kill(os.getpid(), signal.SIGTERM)
        return previous

    def record(self):
        note("at the record, pending " + str(self.pending))
        if PRIVATE_CONSOLE:
            send("CTRL_BREAK_EVENT")
            time.sleep(0.2)
        else:
            note("signal SIGTERM")
            os.kill(os.getpid(), signal.SIGTERM)
        return real_record(self)

    signal.signal = change
    runner._StopGuard.record_interrupt = record
    try:
        guard.install()
    finally:
        signal.signal = real_signal
        runner._StopGuard.record_interrupt = real_record
    note("pending " + str(guard.pending))
    note("phase " + guard.phase)


def install_break() -> None:
    """Send Ctrl+Break just before the guard replaces its handler.

    The native console handler is already registered, so the section holds the
    event. The run must end at 149, not at the console exit status.
    """
    guard = runner._StopGuard()
    real_signal = signal.signal
    sent = {"done": False}

    def change(number, handler):
        if not sent["done"] and number == getattr(signal, "SIGBREAK", None):
            sent["done"] = True
            note("before installing SIGBREAK")
            send("CTRL_BREAK_EVENT")
            if not wait_for_record(guard):
                raise RuntimeError("The section did not record the event")
        return real_signal(number, handler)

    signal.signal = change
    try:
        guard.install()
    finally:
        signal.signal = real_signal
    note("pending " + str(guard.pending))
    note("phase " + guard.phase)


def install_pair(first: str, second: str) -> None:
    """Send two stops inside the install section, in the given order.

    Both stops arrive while the section holds them. On POSIX the mask keeps
    them as one pending set, which carries no order. On Windows the native
    handler records the first event at arrival. Either way the run must end
    with one stop's code, print no traceback, and put every handler back.
    """
    guard = runner._StopGuard()
    real_signal = signal.signal
    sent = {"done": False}

    def change(number, handler):
        if not sent["done"] and number == signal.SIGINT:
            sent["done"] = True
            note("before installing SIGINT")
            for index, name in enumerate((first, second)):
                note("signal " + name)
                if PRIVATE_CONSOLE:
                    send("CTRL_C_EVENT" if name == "SIGINT" else "CTRL_BREAK_EVENT")
                    if index == 0:
                        if not wait_for_record(guard):
                            raise RuntimeError("The section did not record the event")
                    else:
                        time.sleep(0.2)
                else:
                    os.kill(os.getpid(), getattr(signal, name))
        return real_signal(number, handler)

    signal.signal = change
    try:
        guard.install()
        note("pending " + str(guard.pending))
        note("phase " + guard.phase)
        guard.phase = "cleanup"
        guard.restore()
    finally:
        signal.signal = real_signal
        note(
            "restored "
            + str(all(signal.getsignal(n) is h for n, h in ORIGINAL.items()))
        )


def restore_default() -> None:
    """Leave SIGTERM at its default action and send it inside the restore.

    The caller owns no handler for SIGTERM, so restore puts the default action
    back. A stop that is pending at that moment must not reach the default
    action, or the process ends instead of returning to the caller.
    """
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    ORIGINAL.clear()
    ORIGINAL.update({number: signal.getsignal(number) for number in STOPS})
    guard = runner._StopGuard()
    guard.install()
    guard.phase = "cleanup"
    real_signal = signal.signal
    sent = {"done": False}

    def change(number, handler):
        if not sent["done"] and number == signal.SIGTERM:
            sent["done"] = True
            note("before restoring SIGTERM")
            note("signal SIGTERM")
            os.kill(os.getpid(), signal.SIGTERM)
        return real_signal(number, handler)

    signal.signal = change
    try:
        guard.restore()
    except BaseException as error:
        note("raised " + type(error).__name__ + ": " + str(error))
    finally:
        signal.signal = real_signal
    note("pending " + str(guard.pending))
    note("restored " + str(all(signal.getsignal(n) is h for n, h in ORIGINAL.items())))


def restore_default_console() -> None:
    """Leave SIGBREAK at its default action and send it inside the restore.

    Windows has no signal mask, so the native console handler holds the event
    for the length of the section. The guard must record it rather than let it
    reach the default action once the handler is gone.
    """
    break_number = getattr(signal, "SIGBREAK", None)
    if break_number is None:
        raise RuntimeError("This platform has no SIGBREAK")
    signal.signal(break_number, signal.SIG_DFL)
    ORIGINAL.clear()
    ORIGINAL.update({number: signal.getsignal(number) for number in STOPS})
    guard = runner._StopGuard()
    guard.install()
    guard.phase = "cleanup"
    real_signal = signal.signal
    sent = {"done": False}

    def change(number, handler):
        if not sent["done"] and number == break_number:
            sent["done"] = True
            note("before restoring SIGBREAK")
            send("CTRL_BREAK_EVENT")
            if not wait_for_record(guard):
                raise RuntimeError("The section did not record the event")
        return real_signal(number, handler)

    signal.signal = change
    try:
        guard.restore()
    except BaseException as error:
        note("raised " + type(error).__name__ + ": " + str(error))
    finally:
        signal.signal = real_signal
    note("pending " + str(guard.pending))
    note("restored " + str(all(signal.getsignal(n) is h for n, h in ORIGINAL.items())))


MODES = {
    "caller-value": caller_raises,
    "caller-keyboard": lambda: caller_raises(caller_keyboard),
    "install-mixed": install_mixed,
    "install-break": install_break,
    "install-pair-term-hup": lambda: install_pair("SIGTERM", "SIGHUP"),
    "install-pair-hup-term": lambda: install_pair("SIGHUP", "SIGTERM"),
    "install-pair-int-break": lambda: install_pair("SIGINT", "SIGBREAK"),
    "install-pair-break-int": lambda: install_pair("SIGBREAK", "SIGINT"),
    "restore-default": restore_default,
    "restore-default-console": restore_default_console,
}

code = 0
try:
    MODES[MODE]()
except SystemExit as requested:
    code = requested.code if isinstance(requested.code, int) else 1
except BaseException as error:
    note("escaped " + type(error).__name__ + ": " + str(error))
    code = 1
note("exit " + str(code))
raise SystemExit(code)
'''


PREBLOCKED_STOP_DRIVER = '''\
"""Block a stop in the caller, run the runner, and report what is left."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import sys

HARNESS = Path(sys.argv[1])
MARKER = Path(sys.argv[2])
ROOT = Path(sys.argv[3])
RESOURCES = Path(sys.argv[4])
sys.path.insert(0, str(HARNESS))

import runner

STOPS = [
    getattr(signal, name)
    for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK")
    if hasattr(signal, name)
]


def note(text: str) -> None:
    with MARKER.open("a", encoding="utf-8") as handle:
        handle.write(text + "\\n")


signal.signal(signal.SIGTERM, signal.SIG_DFL)
ORIGINAL = {number: signal.getsignal(number) for number in STOPS}
initial = signal.pthread_sigmask(signal.SIG_BLOCK, set())
# The caller blocks the stop and asks for it before the run starts. The
# kernel then holds it for the caller, outside the guard's control.
signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
os.kill(os.getpid(), signal.SIGTERM)
note("caller pending " + str(signal.SIGTERM in signal.sigpending()))

code = 0
try:
    code = runner.main(["1", "--root", str(ROOT), "--runner-dir", str(RESOURCES)])
except SystemExit as requested:
    code = requested.code if isinstance(requested.code, int) else 1
note("exit " + str(code))
note("still pending " + str(signal.SIGTERM in signal.sigpending()))
note(
    "handlers restored "
    + str(
        all(
            signal.getsignal(number) is handler
            for number, handler in ORIGINAL.items()
        )
    )
)
note(
    "mask restored "
    + str(
        signal.pthread_sigmask(signal.SIG_BLOCK, set())
        == initial | {signal.SIGTERM}
    )
)
if signal.SIGTERM in signal.sigpending():
    note("taken " + signal.Signals(signal.sigwait({signal.SIGTERM})).name)
signal.pthread_sigmask(signal.SIG_SETMASK, initial)
raise SystemExit(0)
'''


def console_break_event() -> int:
    """Return the Ctrl+Break console event number. Windows only.

    The name exists only on Windows, so it is looked up at run time and the
    type checker on a POSIX host still reads this file.
    """
    name = "CTRL_BREAK_EVENT"
    return cast(int, getattr(signal, name))


def race_database_exists(url: str, name: str) -> bool:
    """Report whether the named race database is still present."""
    with psycopg.connect(url, autocommit=True) as admin:
        with admin.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
            return cursor.fetchone() is not None


def drop_race_database(url: str, name: str) -> None:
    """Drop the named race database, whatever state the run left it in."""
    with psycopg.connect(url, autocommit=True) as admin:
        with admin.cursor() as cursor:
            cursor.execute(
                psycopg_sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    psycopg_sql.Identifier(name)
                )
            )


@unittest.skipUnless(os.environ.get("CMSC127_DATABASE_URL"), "requires PostgreSQL")
@unittest.skipIf(
    sys.platform == "win32",
    "Windows ends a process on SIGTERM without running a Python handler",
)
class StopSignalCleanupTests(unittest.TestCase):
    """A stop signal during a run leaves no race database behind."""

    def exists(self: StopSignalCleanupTests, url: str, name: str) -> bool:
        return race_database_exists(url, name)

    def drop(self: StopSignalCleanupTests, url: str, name: str) -> None:
        drop_race_database(url, name)

    def assert_stop_leaves_no_database(
        self: StopSignalCleanupTests,
        point: str,
        self_test: bool = False,
        signal_name: str = "SIGTERM",
        failed_drop: bool = False,
    ) -> None:
        """Run the runner, send a signal at one point, and check the drop.

        A failed drop leaves the database behind on purpose, so the run reports
        the identifier it could not remove. The test then checks that report.
        """
        url = os.environ["CMSC127_DATABASE_URL"]
        token = uuid4().hex
        name = f"cmsc127_race_{token}"
        notes: list[str] = []
        output = ""
        returncode = -1
        leftover = False
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "starter"
            runner_dir = base / "runner"
            make_starter_root(root, runner_dir)
            (root / "run").write_text(
                "#!/usr/bin/env bash\nset -eu\nsleep 20\n",
                encoding="utf-8",
                newline="\n",
            )
            driver = base / "driver.py"
            driver.write_text(SIGNAL_DRIVER, encoding="utf-8")
            marker = base / "marker.txt"
            arguments = (
                ["--self-test"]
                if self_test
                else ["1", "--root", str(root), "--runner-dir", str(runner_dir)]
            )
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(driver),
                    str(Path(runner.__file__).resolve().parent),
                    str(marker),
                    point,
                    token,
                    *arguments,
                ],
                cwd=root,
                env=os.environ.copy(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            try:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    if marker.exists():
                        notes = marker.read_text(encoding="utf-8").splitlines()
                    if f"created {name}" in notes:
                        break
                    if process.poll() is not None:
                        break
                    time.sleep(0.1)
                self.assertIn(f"created {name}", notes, "the run made no race database")
                if point == "run":
                    process.send_signal(getattr(signal, signal_name))
                returncode = process.wait(timeout=90)
                if marker.exists():
                    notes = marker.read_text(encoding="utf-8").splitlines()
                leftover = self.exists(url, name)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                if process.stdout is not None:
                    output = process.stdout.read()
                    process.stdout.close()
                self.drop(url, name)

        detail = f"{point} exited {returncode} notes {notes}\n{output}"
        if failed_drop:
            self.assertIn("drop failed", notes, detail)
            self.assertIn(name, output, detail)
        else:
            self.assertFalse(leftover, detail)
        self.assertIn("handler default", notes, detail)
        if point == "run":
            self.assertEqual(128 + getattr(signal, signal_name), returncode, detail)
        else:
            self.assertEqual(128 + signal.SIGTERM, returncode, detail)

    def test_sigterm_during_a_run_drops_the_race_database(
        self: StopSignalCleanupTests,
    ) -> None:
        self.assert_stop_leaves_no_database("run", signal_name="SIGTERM")

    def test_sighup_during_a_run_drops_the_race_database(
        self: StopSignalCleanupTests,
    ) -> None:
        self.assert_stop_leaves_no_database("run", signal_name="SIGHUP")

    def test_sigterm_just_after_create_drops_the_race_database(
        self: StopSignalCleanupTests,
    ) -> None:
        self.assert_stop_leaves_no_database("create")

    def test_sigterm_just_before_drop_drops_the_race_database(
        self: StopSignalCleanupTests,
    ) -> None:
        self.assert_stop_leaves_no_database("drop")

    def test_sigterm_then_sighup_before_drop_keeps_the_first_code(
        self: StopSignalCleanupTests,
    ) -> None:
        """A later signal during cleanup keeps the first signal's exit code."""
        self.assert_stop_leaves_no_database("mixed-before-drop")

    def test_self_test_sigterm_just_after_create_drops_the_race_database(
        self: StopSignalCleanupTests,
    ) -> None:
        self.assert_stop_leaves_no_database("selftest-create", self_test=True)

    def test_self_test_sigterm_just_before_drop_drops_the_race_database(
        self: StopSignalCleanupTests,
    ) -> None:
        self.assert_stop_leaves_no_database("selftest-drop", self_test=True)

    def test_sigterm_at_restore_drops_the_race_database(
        self: StopSignalCleanupTests,
    ) -> None:
        """The first signal at the restore boundary still ends the run.

        The guard makes its last check for a held signal before it puts the old
        handlers back. A signal in that interval is held, so the guard must
        check once more after the restore and replay it.
        """
        self.assert_stop_leaves_no_database("restore")

    def test_self_test_sigterm_at_restore_drops_the_race_database(
        self: StopSignalCleanupTests,
    ) -> None:
        self.assert_stop_leaves_no_database("selftest-restore", self_test=True)

    def test_sigterm_at_restore_after_a_failed_drop_names_the_database(
        self: StopSignalCleanupTests,
    ) -> None:
        """The report keeps the identifier. The held signal still replays."""
        self.assert_stop_leaves_no_database("restore-fail", failed_drop=True)

    def test_self_test_sigterm_at_restore_after_a_failed_drop_names_the_database(
        self: StopSignalCleanupTests,
    ) -> None:
        self.assert_stop_leaves_no_database(
            "selftest-restore-fail", self_test=True, failed_drop=True
        )


@unittest.skipUnless(os.environ.get("CMSC127_DATABASE_URL"), "requires PostgreSQL")
@unittest.skipIf(
    sys.platform == "win32",
    "Windows ends a process on SIGTERM without running a Python handler",
)
class StopReportSignalTests(unittest.TestCase):
    """A second signal during the failure report keeps the first code.

    The drop fails, so the run reports the database it could not remove. A
    SIGHUP arrives at that report. The handlers must still be latched, or the
    default action ends the process before the report gives the database
    identifier.
    """

    def exists(self: StopReportSignalTests, url: str, name: str) -> bool:
        return race_database_exists(url, name)

    def drop(self: StopReportSignalTests, url: str, name: str) -> None:
        drop_race_database(url, name)

    def assert_report_survives_the_second_signal(
        self: StopReportSignalTests,
        point: str,
        self_test: bool = False,
    ) -> None:
        """Run the runner, fail its drop, and signal at the report."""
        url = os.environ["CMSC127_DATABASE_URL"]
        token = uuid4().hex
        name = f"cmsc127_race_{token}"
        notes: list[str] = []
        output = ""
        returncode = -1
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "starter"
            runner_dir = base / "runner"
            make_starter_root(root, runner_dir)
            (root / "run").write_text(
                "#!/usr/bin/env bash\nset -eu\nsleep 20\n",
                encoding="utf-8",
                newline="\n",
            )
            driver = base / "driver.py"
            driver.write_text(SIGNAL_DRIVER, encoding="utf-8")
            marker = base / "marker.txt"
            arguments = (
                ["--self-test"]
                if self_test
                else ["1", "--root", str(root), "--runner-dir", str(runner_dir)]
            )
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(driver),
                    str(Path(runner.__file__).resolve().parent),
                    str(marker),
                    point,
                    token,
                    *arguments,
                ],
                cwd=root,
                env=os.environ.copy(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            try:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    if marker.exists():
                        notes = marker.read_text(encoding="utf-8").splitlines()
                    if f"created {name}" in notes:
                        break
                    if process.poll() is not None:
                        break
                    time.sleep(0.1)
                self.assertIn(f"created {name}", notes, "the run made no race database")
                returncode = process.wait(timeout=90)
                if marker.exists():
                    notes = marker.read_text(encoding="utf-8").splitlines()
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                if process.stdout is not None:
                    output = process.stdout.read()
                    process.stdout.close()
                self.drop(url, name)

        detail = f"{point} exited {returncode} notes {notes}\n{output}"
        self.assertIn("drop failed", notes, detail)
        self.assertIn("second SIGHUP", notes, detail)
        self.assertIn(name, output, detail)
        self.assertEqual(128 + signal.SIGTERM, returncode, detail)
        self.assertIn("handler default", notes, detail)

    def test_a_second_signal_during_the_failure_report_keeps_the_first_code(
        self: StopReportSignalTests,
    ) -> None:
        self.assert_report_survives_the_second_signal("report")

    def test_the_self_test_keeps_the_first_code_at_the_failure_report(
        self: StopReportSignalTests,
    ) -> None:
        self.assert_report_survives_the_second_signal("selftest-report", self_test=True)


@unittest.skipUnless(os.environ.get("CMSC127_DATABASE_URL"), "requires PostgreSQL")
class ConsoleStopSignalTests(unittest.TestCase):
    """A Ctrl+C or Ctrl+Break during cleanup keeps the stop and the database.

    Windows ends a process on SIGTERM without running a Python handler, so the
    POSIX cases skip there. A student on Windows stops a run with Ctrl+C or
    Ctrl+Break, and those reach the guard as SIGINT and SIGBREAK. This class
    runs on every platform, because SIGINT is the same signal everywhere.
    """

    def exists(self: ConsoleStopSignalTests, url: str, name: str) -> bool:
        return race_database_exists(url, name)

    def drop(self: ConsoleStopSignalTests, url: str, name: str) -> None:
        drop_race_database(url, name)

    def assert_console_stop_leaves_no_database(
        self: ConsoleStopSignalTests,
        point: str,
        signal_name: str,
        self_test: bool = False,
        mixed: bool = False,
    ) -> None:
        """Run the runner, deliver a console stop at one point, and check it."""
        url = os.environ["CMSC127_DATABASE_URL"]
        token = uuid4().hex
        name = f"cmsc127_race_{token}"
        notes: list[str] = []
        output = ""
        returncode = -1
        leftover = False
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "starter"
            runner_dir = base / "runner"
            make_starter_root(root, runner_dir)
            (root / "run").write_text(
                "#!/usr/bin/env bash\nset -eu\nsleep 20\n",
                encoding="utf-8",
                newline="\n",
            )
            driver = base / "driver.py"
            driver.write_text(SIGNAL_DRIVER, encoding="utf-8")
            marker = base / "marker.txt"
            arguments = (
                ["--self-test"]
                if self_test
                else ["1", "--root", str(root), "--runner-dir", str(runner_dir)]
            )
            environment = os.environ.copy()
            environment["CMSC127_CONSOLE_SIGNAL"] = signal_name
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(driver),
                    str(Path(runner.__file__).resolve().parent),
                    str(marker),
                    point,
                    token,
                    *arguments,
                ],
                cwd=root,
                env=environment,
                # A console event reaches only this process's own group, so the
                # test runner never meets the stop it sends.
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    if sys.platform == "win32"
                    else 0
                ),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            try:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    if marker.exists():
                        notes = marker.read_text(encoding="utf-8").splitlines()
                    if f"created {name}" in notes:
                        break
                    if process.poll() is not None:
                        break
                    time.sleep(0.1)
                self.assertIn(f"created {name}", notes, "the run made no race database")
                if signal_name == "SIGBREAK":
                    deadline = time.monotonic() + 30
                    while time.monotonic() < deadline:
                        if marker.exists():
                            notes = marker.read_text(encoding="utf-8").splitlines()
                        if "waiting for SIGBREAK" in notes:
                            break
                        if process.poll() is not None:
                            break
                        time.sleep(0.05)
                    if process.poll() is None:
                        os.kill(process.pid, console_break_event())
                returncode = process.wait(timeout=90)
                if marker.exists():
                    notes = marker.read_text(encoding="utf-8").splitlines()
                leftover = self.exists(url, name)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                if process.stdout is not None:
                    output = process.stdout.read()
                    process.stdout.close()
                self.drop(url, name)

        detail = f"{point} {signal_name} exited {returncode} notes {notes}\n{output}"
        self.assertFalse(leftover, detail)
        self.assertIn("handler default", notes, detail)
        self.assertEqual(128 + getattr(signal, signal_name), returncode, detail)
        if mixed:
            # The second stop must not reach the default handler, which would
            # print a traceback and replace the first stop's code.
            self.assertIn("SIGINT handler restored", "\n".join(notes), detail)
            self.assertNotIn("Traceback", output, detail)

    def test_sigint_at_the_drop_drops_the_race_database(
        self: ConsoleStopSignalTests,
    ) -> None:
        self.assert_console_stop_leaves_no_database("console-drop", "SIGINT")

    def test_sigint_at_restore_drops_the_race_database(
        self: ConsoleStopSignalTests,
    ) -> None:
        self.assert_console_stop_leaves_no_database("console-restore", "SIGINT")

    def test_self_test_sigint_at_the_drop_drops_the_race_database(
        self: ConsoleStopSignalTests,
    ) -> None:
        self.assert_console_stop_leaves_no_database(
            "selftest-console-drop", "SIGINT", self_test=True
        )

    def test_self_test_sigint_at_restore_drops_the_race_database(
        self: ConsoleStopSignalTests,
    ) -> None:
        self.assert_console_stop_leaves_no_database(
            "selftest-console-restore", "SIGINT", self_test=True
        )

    @unittest.skipUnless(sys.platform == "win32", "SIGBREAK is a Windows signal")
    def test_sigbreak_at_the_drop_drops_the_race_database(
        self: ConsoleStopSignalTests,
    ) -> None:
        self.assert_console_stop_leaves_no_database("console-drop", "SIGBREAK")

    @unittest.skipUnless(sys.platform == "win32", "SIGBREAK is a Windows signal")
    def test_sigbreak_at_restore_drops_the_race_database(
        self: ConsoleStopSignalTests,
    ) -> None:
        self.assert_console_stop_leaves_no_database("console-restore", "SIGBREAK")

    @unittest.skipUnless(sys.platform == "win32", "SIGBREAK is a Windows signal")
    def test_self_test_sigbreak_at_the_drop_drops_the_race_database(
        self: ConsoleStopSignalTests,
    ) -> None:
        self.assert_console_stop_leaves_no_database(
            "selftest-console-drop", "SIGBREAK", self_test=True
        )

    @unittest.skipUnless(sys.platform == "win32", "SIGBREAK is a Windows signal")
    def test_self_test_sigbreak_at_restore_drops_the_race_database(
        self: ConsoleStopSignalTests,
    ) -> None:
        self.assert_console_stop_leaves_no_database(
            "selftest-console-restore", "SIGBREAK", self_test=True
        )

    @unittest.skipUnless(sys.platform == "win32", "SIGBREAK is a Windows signal")
    def test_sigbreak_at_restore_then_sigint_keeps_the_first_code(
        self: ConsoleStopSignalTests,
    ) -> None:
        """A Ctrl+C after the SIGINT handler changes keeps the Ctrl+Break code.

        Restoration puts the default Ctrl+C handler back. A Ctrl+C in that
        interval raises KeyboardInterrupt through it, so the process must
        still end with the first stop's code and print no traceback.
        """
        self.assert_console_stop_leaves_no_database(
            "console-mixed-restore", "SIGBREAK", mixed=True
        )

    @unittest.skipUnless(sys.platform == "win32", "SIGBREAK is a Windows signal")
    def test_self_test_sigbreak_at_restore_then_sigint_keeps_the_first_code(
        self: ConsoleStopSignalTests,
    ) -> None:
        self.assert_console_stop_leaves_no_database(
            "selftest-console-mixed-restore", "SIGBREAK", self_test=True, mixed=True
        )


@unittest.skipUnless(os.environ.get("CMSC127_DATABASE_URL"), "requires PostgreSQL")
class FinishEntryStopTests(unittest.TestCase):
    """A stop at the entry of _finish_run still drops the scratch database.

    The phase moves to cleanup in the caller's finally, before the call. A
    stop at that call boundary then finds the cleanup phase, so the handler
    records it and returns, and the drop runs. The exit code is the first
    stop's code.
    """

    def exists(self: FinishEntryStopTests, url: str, name: str) -> bool:
        return race_database_exists(url, name)

    def drop(self: FinishEntryStopTests, url: str, name: str) -> None:
        drop_race_database(url, name)

    def assert_finish_entry_drops_the_database(
        self: FinishEntryStopTests,
        signal_name: str,
        self_test: bool = False,
    ) -> None:
        """Run the runner, stop it at the finish entry, and check the drop."""
        url = os.environ["CMSC127_DATABASE_URL"]
        token = uuid4().hex
        name = f"cmsc127_race_{token}"
        notes: list[str] = []
        output = ""
        returncode = -1
        leftover = False
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "starter"
            runner_dir = base / "runner"
            make_starter_root(root, runner_dir)
            (root / "run").write_text(
                "#!/usr/bin/env bash\nset -eu\nexit 0\n",
                encoding="utf-8",
                newline="\n",
            )
            driver = base / "driver.py"
            driver.write_text(SIGNAL_DRIVER, encoding="utf-8")
            marker = base / "marker.txt"
            arguments = (
                ["--self-test"]
                if self_test
                else ["1", "--root", str(root), "--runner-dir", str(runner_dir)]
            )
            environment = os.environ.copy()
            environment["CMSC127_CONSOLE_SIGNAL"] = signal_name
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(driver),
                    str(Path(runner.__file__).resolve().parent),
                    str(marker),
                    "selftest-finish" if self_test else "finish",
                    token,
                    *arguments,
                ],
                cwd=root,
                env=environment,
                # A console event reaches only this process's own group, so the
                # test runner never meets the stop it sends.
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    if sys.platform == "win32"
                    else 0
                ),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            try:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    if marker.exists():
                        notes = marker.read_text(encoding="utf-8").splitlines()
                    if f"created {name}" in notes:
                        break
                    if process.poll() is not None:
                        break
                    time.sleep(0.1)
                self.assertIn(f"created {name}", notes, "the run made no race database")
                if signal_name == "SIGBREAK":
                    deadline = time.monotonic() + 30
                    while time.monotonic() < deadline:
                        if marker.exists():
                            notes = marker.read_text(encoding="utf-8").splitlines()
                        if "waiting for SIGBREAK at the finish entry" in notes:
                            break
                        if process.poll() is not None:
                            break
                        time.sleep(0.05)
                    if process.poll() is None:
                        os.kill(process.pid, console_break_event())
                returncode = process.wait(timeout=90)
                if marker.exists():
                    notes = marker.read_text(encoding="utf-8").splitlines()
                leftover = self.exists(url, name)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                if process.stdout is not None:
                    output = process.stdout.read()
                    process.stdout.close()
                self.drop(url, name)

        detail = f"finish {signal_name} exited {returncode} notes {notes}\n{output}"
        self.assertFalse(leftover, detail)
        self.assertNotIn("Traceback", output, detail)
        self.assertEqual(128 + getattr(signal, signal_name), returncode, detail)
        self.assertIn("handler default", notes, detail)

    def test_sigint_at_the_finish_entry_drops_the_race_database(
        self: FinishEntryStopTests,
    ) -> None:
        self.assert_finish_entry_drops_the_database("SIGINT")

    def test_self_test_sigint_at_the_finish_entry_drops_the_race_database(
        self: FinishEntryStopTests,
    ) -> None:
        self.assert_finish_entry_drops_the_database("SIGINT", self_test=True)

    @unittest.skipIf(sys.platform == "win32", "Windows has no SIGHUP")
    def test_sigterm_at_the_finish_entry_drops_the_race_database(
        self: FinishEntryStopTests,
    ) -> None:
        self.assert_finish_entry_drops_the_database("SIGTERM")

    @unittest.skipIf(sys.platform == "win32", "Windows has no SIGHUP")
    def test_self_test_sigterm_at_the_finish_entry_drops_the_race_database(
        self: FinishEntryStopTests,
    ) -> None:
        self.assert_finish_entry_drops_the_database("SIGTERM", self_test=True)

    @unittest.skipUnless(sys.platform == "win32", "SIGBREAK is a Windows signal")
    def test_sigbreak_at_the_finish_entry_drops_the_race_database(
        self: FinishEntryStopTests,
    ) -> None:
        self.assert_finish_entry_drops_the_database("SIGBREAK")

    @unittest.skipUnless(sys.platform == "win32", "SIGBREAK is a Windows signal")
    def test_self_test_sigbreak_at_the_finish_entry_drops_the_race_database(
        self: FinishEntryStopTests,
    ) -> None:
        self.assert_finish_entry_drops_the_database("SIGBREAK", self_test=True)


@unittest.skipUnless(os.environ.get("CMSC127_DATABASE_URL"), "requires PostgreSQL")
class RestoreBetweenStopTests(unittest.TestCase):
    """A Ctrl+C between the handler replacements keeps the latched stop.

    SIGINT goes back last, so a Ctrl+C that arrives after another handler is
    back still reaches the guard's handler. The guard records it and returns,
    the loop finishes, and the caller sees the latched stop.
    """

    def exists(self: RestoreBetweenStopTests, url: str, name: str) -> bool:
        return race_database_exists(url, name)

    def drop(self: RestoreBetweenStopTests, url: str, name: str) -> None:
        drop_race_database(url, name)

    def assert_between_keeps_the_latched_stop(
        self: RestoreBetweenStopTests,
        first: str,
        self_test: bool = False,
    ) -> None:
        """Latch the first stop, deliver Ctrl+C between, and check the code."""
        url = os.environ["CMSC127_DATABASE_URL"]
        token = uuid4().hex
        name = f"cmsc127_race_{token}"
        notes: list[str] = []
        output = ""
        returncode = -1
        leftover = False
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "starter"
            runner_dir = base / "runner"
            make_starter_root(root, runner_dir)
            (root / "run").write_text(
                "#!/usr/bin/env bash\nset -eu\nexit 0\n",
                encoding="utf-8",
                newline="\n",
            )
            driver = base / "driver.py"
            driver.write_text(SIGNAL_DRIVER, encoding="utf-8")
            marker = base / "marker.txt"
            arguments = (
                ["--self-test"]
                if self_test
                else ["1", "--root", str(root), "--runner-dir", str(runner_dir)]
            )
            environment = os.environ.copy()
            environment["CMSC127_FIRST_SIGNAL"] = first
            environment["CMSC127_SECOND_SIGNAL"] = "SIGINT"
            process = subprocess.run(
                [
                    sys.executable,
                    str(driver),
                    str(Path(runner.__file__).resolve().parent),
                    str(marker),
                    "selftest-restore-between" if self_test else "restore-between",
                    token,
                    *arguments,
                ],
                cwd=root,
                env=environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
            returncode = process.returncode
            output = process.stdout + process.stderr
            if marker.exists():
                notes = marker.read_text(encoding="utf-8").splitlines()
            leftover = self.exists(url, name)
            self.drop(url, name)

        detail = f"{first} then SIGINT exited {returncode} notes {notes}\n{output}"
        self.assertIn(f"created {name}", notes, detail)
        self.assertIn("before restore", notes, detail)
        self.assertIn("before the Ctrl+C", " ".join(notes), detail)
        self.assertIn("raising SIGINT", " ".join(notes), detail)
        self.assertIn("non-SIGINT handlers restored True", notes, detail)
        self.assertNotIn("Traceback", output, detail)
        self.assertFalse(leftover, detail)
        self.assertEqual(128 + getattr(signal, first), returncode, detail)

    def test_sigterm_then_sigint_between_the_replacements_keeps_the_latched_stop(
        self: RestoreBetweenStopTests,
    ) -> None:
        self.assert_between_keeps_the_latched_stop("SIGTERM")

    def test_self_test_sigterm_then_sigint_between_the_replacements(
        self: RestoreBetweenStopTests,
    ) -> None:
        self.assert_between_keeps_the_latched_stop("SIGTERM", self_test=True)

    @unittest.skipUnless(sys.platform == "win32", "SIGBREAK is a Windows signal")
    def test_sigbreak_then_sigint_between_the_replacements_keeps_the_latched_stop(
        self: RestoreBetweenStopTests,
    ) -> None:
        self.assert_between_keeps_the_latched_stop("SIGBREAK")

    @unittest.skipUnless(sys.platform == "win32", "SIGBREAK is a Windows signal")
    def test_self_test_sigbreak_then_sigint_between_the_replacements(
        self: RestoreBetweenStopTests,
    ) -> None:
        self.assert_between_keeps_the_latched_stop("SIGBREAK", self_test=True)


@unittest.skipUnless(os.environ.get("CMSC127_DATABASE_URL"), "requires PostgreSQL")
class NestedStopTests(unittest.TestCase):
    """A nested stop at the handler entry keeps the run safe.

    CPython runs a pending handler at the entry check point of the handler
    that is already running, before any statement of that handler. A second
    stop that arrives in that window therefore records itself first. No
    Python-level handler can record arrival order across the window. The run
    must still end with one of the two stop codes. It prints no traceback. It
    drops its scratch database.
    """

    def exists(self: NestedStopTests, url: str, name: str) -> bool:
        return race_database_exists(url, name)

    def drop(self: NestedStopTests, url: str, name: str) -> None:
        drop_race_database(url, name)

    def assert_nested_stop_stays_safe(
        self: NestedStopTests,
        first: str,
        second: str,
        keep: bool,
        self_test: bool = False,
    ) -> None:
        """Nest the second stop at the first handler's entry, and check it."""
        url = os.environ["CMSC127_DATABASE_URL"]
        token = uuid4().hex
        name = f"cmsc127_race_{token}"
        notes: list[str] = []
        output = ""
        returncode = -1
        leftover = False
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "starter"
            runner_dir = base / "runner"
            make_starter_root(root, runner_dir)
            (root / "run").write_text(
                "#!/usr/bin/env bash\nset -eu\nexit 0\n",
                encoding="utf-8",
                newline="\n",
            )
            driver = base / "driver.py"
            driver.write_text(SIGNAL_DRIVER, encoding="utf-8")
            marker = base / "marker.txt"
            arguments = (
                ["--self-test"]
                if self_test
                else ["1", "--root", str(root), "--runner-dir", str(runner_dir)]
            )
            environment = os.environ.copy()
            environment["CMSC127_FIRST_SIGNAL"] = first
            environment["CMSC127_SECOND_SIGNAL"] = second
            environment["CMSC127_KEEP_GUARD"] = "1" if keep else "0"
            process = subprocess.run(
                [
                    sys.executable,
                    str(driver),
                    str(Path(runner.__file__).resolve().parent),
                    str(marker),
                    "selftest-nested" if self_test else "nested",
                    token,
                    *arguments,
                ],
                cwd=root,
                env=environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
            returncode = process.returncode
            output = process.stdout + process.stderr
            if marker.exists():
                notes = marker.read_text(encoding="utf-8").splitlines()
            leftover = self.exists(url, name)
            self.drop(url, name)

        detail = (
            f"{first} then {second} keep={keep} exited {returncode} "
            f"notes {notes}\n{output}"
        )
        self.assertIn(f"created {name}", notes, detail)
        self.assertIn(f"nested {second} at the entry of {first}", notes, detail)
        self.assertNotIn("Traceback", output, detail)
        self.assertFalse(leftover, detail)
        self.assertIn(
            returncode,
            (128 + getattr(signal, first), 128 + getattr(signal, second)),
            detail,
        )

    def test_a_nested_stop_on_the_library_path_stays_safe(
        self: NestedStopTests,
    ) -> None:
        second = "SIGTERM" if sys.platform != "win32" else "SIGBREAK"
        self.assert_nested_stop_stays_safe("SIGINT", second, keep=False)

    def test_a_nested_stop_on_the_cli_path_stays_safe(
        self: NestedStopTests,
    ) -> None:
        second = "SIGTERM" if sys.platform != "win32" else "SIGBREAK"
        self.assert_nested_stop_stays_safe("SIGINT", second, keep=True)

    def test_the_self_test_nested_stop_on_the_library_path_stays_safe(
        self: NestedStopTests,
    ) -> None:
        second = "SIGTERM" if sys.platform != "win32" else "SIGBREAK"
        self.assert_nested_stop_stays_safe("SIGINT", second, keep=False, self_test=True)

    def test_the_self_test_nested_stop_on_the_cli_path_stays_safe(
        self: NestedStopTests,
    ) -> None:
        second = "SIGTERM" if sys.platform != "win32" else "SIGBREAK"
        self.assert_nested_stop_stays_safe("SIGINT", second, keep=True, self_test=True)


# The two stops a student reaches on Windows, as Python signal names.
CONSOLE_STOPS = ("SIGINT", "SIGBREAK")
# The three stops a POSIX host has.
POSIX_STOPS = ("SIGINT", "SIGTERM", "SIGHUP")


@unittest.skipUnless(os.environ.get("CMSC127_DATABASE_URL"), "requires PostgreSQL")
class RestoreRaisesStopTests(unittest.TestCase):
    """A caller handler that raises between the restore calls still gets every slot.

    The caller owns every stop handler and each one raises when it runs. The
    first stop is latched while the guard still owns the handlers. Then a
    native stop arrives once the last non-SIGINT handler is back. The caller's
    handler raises there. Every slot must still go back, so the caller owns
    every handler afterwards. The latched stop names the exit.
    """

    def exists(self: RestoreRaisesStopTests, url: str, name: str) -> bool:
        return race_database_exists(url, name)

    def drop(self: RestoreRaisesStopTests, url: str, name: str) -> None:
        drop_race_database(url, name)

    def process_options(self: RestoreRaisesStopTests) -> dict[str, Any]:
        """The process options that this platform's delivery needs."""
        return {}

    def interpreter(self: RestoreRaisesStopTests) -> str:
        """The interpreter that runs the driver."""
        return sys.executable

    def extra_environment(self: RestoreRaisesStopTests) -> dict[str, str]:
        """The variables that this platform's delivery needs."""
        return {}

    def assert_every_slot_comes_back(
        self: RestoreRaisesStopTests,
        first: str,
        self_test: bool = False,
    ) -> None:
        """Run the runner, raise from a restored handler, and check the slots."""
        url = os.environ["CMSC127_DATABASE_URL"]
        token = uuid4().hex
        name = f"cmsc127_race_{token}"
        notes: list[str] = []
        output = ""
        returncode = -1
        leftover = False
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "starter"
            runner_dir = base / "runner"
            make_starter_root(root, runner_dir)
            (root / "run").write_text(
                "#!/usr/bin/env bash\nset -eu\nexit 0\n",
                encoding="utf-8",
                newline="\n",
            )
            driver = base / "driver.py"
            driver.write_text(SIGNAL_DRIVER, encoding="utf-8")
            marker = base / "marker.txt"
            arguments = (
                ["--self-test"]
                if self_test
                else ["1", "--root", str(root), "--runner-dir", str(runner_dir)]
            )
            environment = os.environ.copy()
            environment["CMSC127_FIRST_SIGNAL"] = first
            environment.update(self.extra_environment())
            process = subprocess.Popen(
                [
                    self.interpreter(),
                    str(driver),
                    str(Path(runner.__file__).resolve().parent),
                    str(marker),
                    "selftest-caller-raises" if self_test else "caller-raises",
                    token,
                    *arguments,
                ],
                cwd=root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                **self.process_options(),
            )
            try:
                returncode = process.wait(timeout=120)
                if marker.exists():
                    notes = marker.read_text(encoding="utf-8").splitlines()
                leftover = self.exists(url, name)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                if process.stdout is not None:
                    output = process.stdout.read()
                    process.stdout.close()
                self.drop(url, name)

        detail = f"{first} caller-raises exited {returncode} notes {notes}\n{output}"
        self.assertIn(f"created {name}", notes, detail)
        self.assertIn("before restore", notes, detail)
        self.assertIn("before the caller stop", " ".join(notes), detail)
        self.assertIn("caller signal", " ".join(notes), detail)
        self.assertIn("all handlers restored True", notes, detail)
        self.assertNotIn("Traceback", output, detail)
        self.assertFalse(leftover, detail)
        self.assertEqual(128 + getattr(signal, first), returncode, detail)


@unittest.skipUnless(sys.platform == "win32", "a console event is a Windows event")
class ConsoleRestoreRaisesStopTests(RestoreRaisesStopTests):
    """A native Ctrl+Break between the restore calls still restores every slot.

    Each case runs in its own console, so the event cannot reach the test
    runner. The driver proves the console holds it alone before it sends.
    """

    def process_options(self: ConsoleRestoreRaisesStopTests) -> dict[str, Any]:
        return private_console_options()

    def interpreter(self: ConsoleRestoreRaisesStopTests) -> str:
        """The base interpreter, so the console holds one process."""
        return str(getattr(sys, "_base_executable", sys.executable))

    def extra_environment(self: ConsoleRestoreRaisesStopTests) -> dict[str, str]:
        paths = [sysconfig.get_paths()["purelib"]]
        existing = os.environ.get("PYTHONPATH")
        if existing:
            paths.append(existing)
        return {
            "CMSC127_PRIVATE_CONSOLE": "1",
            "PYTHONPATH": os.pathsep.join(paths),
        }

    def test_sigbreak_between_the_restore_calls_restores_every_slot(
        self: ConsoleRestoreRaisesStopTests,
    ) -> None:
        self.assert_every_slot_comes_back("SIGINT")

    def test_self_test_sigbreak_between_the_restore_calls_restores_every_slot(
        self: ConsoleRestoreRaisesStopTests,
    ) -> None:
        self.assert_every_slot_comes_back("SIGINT", self_test=True)


@unittest.skipIf(
    sys.platform == "win32",
    "Windows ends a process on SIGTERM without running a Python handler",
)
class PosixRestoreRaisesStopTests(RestoreRaisesStopTests):
    """A native SIGHUP between the restore calls still restores every slot."""

    def test_sighup_between_the_restore_calls_restores_every_slot(
        self: PosixRestoreRaisesStopTests,
    ) -> None:
        self.assert_every_slot_comes_back("SIGINT")

    def test_self_test_sighup_between_the_restore_calls_restores_every_slot(
        self: PosixRestoreRaisesStopTests,
    ) -> None:
        self.assert_every_slot_comes_back("SIGINT", self_test=True)


@unittest.skipUnless(os.environ.get("CMSC127_DATABASE_URL"), "requires PostgreSQL")
class InstallInterruptTests(unittest.TestCase):
    """A Ctrl+C before the SIGINT registration still ends at 130.

    Python's default handler is still in place at that moment, so the
    interrupt arrives as KeyboardInterrupt. The guard records it as a SIGINT
    stop, finishes its cleanup, and replays code 130. No traceback appears and
    no database is left.
    """

    def exists(self: InstallInterruptTests, url: str, name: str) -> bool:
        return race_database_exists(url, name)

    def drop(self: InstallInterruptTests, url: str, name: str) -> None:
        drop_race_database(url, name)

    def process_options(self: InstallInterruptTests) -> dict[str, Any]:
        """The process options that this platform's delivery needs."""
        return {}

    def interpreter(self: InstallInterruptTests) -> str:
        """The interpreter that runs the driver."""
        return sys.executable

    def extra_environment(self: InstallInterruptTests) -> dict[str, str]:
        """The variables that this platform's delivery needs."""
        return {}

    def assert_interrupt_before_registration_ends_at_130(
        self: InstallInterruptTests,
        keep: bool,
        self_test: bool = False,
    ) -> None:
        """Deliver a native Ctrl+C before the registration, and check the end."""
        url = os.environ["CMSC127_DATABASE_URL"]
        token = uuid4().hex
        name = f"cmsc127_race_{token}"
        notes: list[str] = []
        output = ""
        returncode = -1
        leftover = False
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "starter"
            runner_dir = base / "runner"
            make_starter_root(root, runner_dir)
            (root / "run").write_text(
                "#!/usr/bin/env bash\nset -eu\nexit 0\n",
                encoding="utf-8",
                newline="\n",
            )
            driver = base / "driver.py"
            driver.write_text(SIGNAL_DRIVER, encoding="utf-8")
            marker = base / "marker.txt"
            arguments = (
                ["--self-test"]
                if self_test
                else ["1", "--root", str(root), "--runner-dir", str(runner_dir)]
            )
            environment = os.environ.copy()
            environment["CMSC127_FIRST_SIGNAL"] = "SIGINT"
            environment["CMSC127_KEEP_GUARD"] = "1" if keep else "0"
            environment.update(self.extra_environment())
            process = subprocess.Popen(
                [
                    self.interpreter(),
                    str(driver),
                    str(Path(runner.__file__).resolve().parent),
                    str(marker),
                    "selftest-install-before" if self_test else "install-before",
                    token,
                    *arguments,
                ],
                cwd=root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                **self.process_options(),
            )
            try:
                returncode = process.wait(timeout=120)
                if marker.exists():
                    notes = marker.read_text(encoding="utf-8").splitlines()
                leftover = self.exists(url, name)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                if process.stdout is not None:
                    output = process.stdout.read()
                    process.stdout.close()
                self.drop(url, name)

        detail = f"install-before keep={keep} exited {returncode} notes {notes}\n{output}"
        self.assertIn("before installing SIGINT", notes, detail)
        self.assertNotIn(f"created {name}", notes, detail)
        self.assertNotIn("Traceback", output, detail)
        self.assertFalse(leftover, detail)
        self.assertEqual(128 + signal.SIGINT, returncode, detail)


@unittest.skipUnless(sys.platform == "win32", "a console event is a Windows event")
class ConsoleInstallInterruptTests(InstallInterruptTests):
    """A native Ctrl+C before the registration ends at 130 on Windows."""

    def process_options(self: ConsoleInstallInterruptTests) -> dict[str, Any]:
        return private_console_options()

    def interpreter(self: ConsoleInstallInterruptTests) -> str:
        """The base interpreter, so the console holds one process."""
        return str(getattr(sys, "_base_executable", sys.executable))

    def extra_environment(self: ConsoleInstallInterruptTests) -> dict[str, str]:
        paths = [sysconfig.get_paths()["purelib"]]
        existing = os.environ.get("PYTHONPATH")
        if existing:
            paths.append(existing)
        return {
            "CMSC127_PRIVATE_CONSOLE": "1",
            "PYTHONPATH": os.pathsep.join(paths),
        }

    def test_ctrl_c_before_the_registration_ends_at_130(
        self: ConsoleInstallInterruptTests,
    ) -> None:
        self.assert_interrupt_before_registration_ends_at_130(keep=False)

    def test_ctrl_c_before_the_registration_ends_at_130_on_the_cli_path(
        self: ConsoleInstallInterruptTests,
    ) -> None:
        self.assert_interrupt_before_registration_ends_at_130(keep=True)

    def test_self_test_ctrl_c_before_the_registration_ends_at_130(
        self: ConsoleInstallInterruptTests,
    ) -> None:
        self.assert_interrupt_before_registration_ends_at_130(keep=False, self_test=True)

    def test_self_test_ctrl_c_before_the_registration_ends_at_130_on_the_cli_path(
        self: ConsoleInstallInterruptTests,
    ) -> None:
        self.assert_interrupt_before_registration_ends_at_130(keep=True, self_test=True)


@unittest.skipIf(
    sys.platform == "win32",
    "Windows ends a process on SIGTERM without running a Python handler",
)
class PosixInstallInterruptTests(InstallInterruptTests):
    """A native Ctrl+C before the registration ends at 130 on POSIX."""

    def test_ctrl_c_before_the_registration_ends_at_130(
        self: PosixInstallInterruptTests,
    ) -> None:
        self.assert_interrupt_before_registration_ends_at_130(keep=False)

    def test_ctrl_c_before_the_registration_ends_at_130_on_the_cli_path(
        self: PosixInstallInterruptTests,
    ) -> None:
        self.assert_interrupt_before_registration_ends_at_130(keep=True)

    def test_self_test_ctrl_c_before_the_registration_ends_at_130(
        self: PosixInstallInterruptTests,
    ) -> None:
        self.assert_interrupt_before_registration_ends_at_130(keep=False, self_test=True)

    def test_self_test_ctrl_c_before_the_registration_ends_at_130_on_the_cli_path(
        self: PosixInstallInterruptTests,
    ) -> None:
        self.assert_interrupt_before_registration_ends_at_130(keep=True, self_test=True)


def private_console_options() -> dict[str, Any]:
    """The options that give a child its own hidden console. Windows only.

    The names exist only on Windows, so they are looked up at run time and the
    type checker on a POSIX host still reads this file.
    """
    startup = getattr(subprocess, "STARTUPINFO")()
    startup.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW")
    startup.wShowWindow = getattr(subprocess, "SW_HIDE")
    return {
        "creationflags": getattr(subprocess, "CREATE_NEW_CONSOLE"),
        "startupinfo": startup,
    }


class StopPairTests(unittest.TestCase):
    """Two stops at the handler change keep the first stop's code.

    The CLI path keeps the guard installed until the process exits, so the
    second stop is held like the first. Each case runs main or _self_test in
    its own process. The database must be gone.
    """

    def exists(self: StopPairTests, url: str, name: str) -> bool:
        return race_database_exists(url, name)

    def drop(self: StopPairTests, url: str, name: str) -> None:
        drop_race_database(url, name)

    def process_options(self: StopPairTests) -> dict[str, Any]:
        """The process options that this platform's delivery needs."""
        return {}

    def interpreter(self: StopPairTests) -> str:
        """The interpreter that runs the driver."""
        return sys.executable

    def extra_environment(self: StopPairTests) -> dict[str, str]:
        """The variables that this platform's delivery needs."""
        return {}

    def assert_pair_keeps_the_first_code(
        self: StopPairTests,
        first: str,
        second: str,
        self_test: bool = False,
    ) -> None:
        """Run the runner, deliver two stops at the boundary, and check it."""
        url = os.environ["CMSC127_DATABASE_URL"]
        token = uuid4().hex
        name = f"cmsc127_race_{token}"
        notes: list[str] = []
        output = ""
        returncode = -1
        leftover = False
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "starter"
            runner_dir = base / "runner"
            make_starter_root(root, runner_dir)
            driver = base / "driver.py"
            driver.write_text(SIGNAL_DRIVER, encoding="utf-8")
            marker = base / "marker.txt"
            arguments = (
                ["--self-test"]
                if self_test
                else ["1", "--root", str(root), "--runner-dir", str(runner_dir)]
            )
            environment = os.environ.copy()
            environment["CMSC127_KEEP_GUARD"] = "1"
            environment["CMSC127_FIRST_SIGNAL"] = first
            environment["CMSC127_SECOND_SIGNAL"] = second
            environment.update(self.extra_environment())
            process = subprocess.Popen(
                [
                    self.interpreter(),
                    str(driver),
                    str(Path(runner.__file__).resolve().parent),
                    str(marker),
                    "selftest-pair" if self_test else "pair",
                    token,
                    *arguments,
                ],
                cwd=root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                **self.process_options(),
            )
            try:
                returncode = process.wait(timeout=120)
                if marker.exists():
                    notes = marker.read_text(encoding="utf-8").splitlines()
                leftover = self.exists(url, name)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                if process.stdout is not None:
                    output = process.stdout.read()
                    process.stdout.close()
                self.drop(url, name)

        detail = f"{first} then {second} exited {returncode} notes {notes}\n{output}"
        self.assertIn(f"created {name}", notes, detail)
        self.assertIn("before end-run", notes, detail)
        self.assertFalse(leftover, detail)
        self.assertNotIn("Traceback", output, detail)
        self.assertEqual(128 + getattr(signal, first), returncode, detail)


@unittest.skipUnless(sys.platform == "win32", "a console event is a Windows event")
class ConsoleStopPairTests(StopPairTests):
    """Two console stops at the handler change keep the first stop's code.

    Each case runs in its own console, so the event cannot reach the test
    runner. The driver proves the console holds it alone before it sends.
    """

    def process_options(self: ConsoleStopPairTests) -> dict[str, Any]:
        return private_console_options()

    def interpreter(self: ConsoleStopPairTests) -> str:
        """The base interpreter, so the console holds one process.

        A virtual environment's python.exe starts the base interpreter as a
        child, and both then share the console. The base interpreter alone
        keeps the console private.
        """
        return str(getattr(sys, "_base_executable", sys.executable))

    def extra_environment(self: ConsoleStopPairTests) -> dict[str, str]:
        paths = [sysconfig.get_paths()["purelib"]]
        existing = os.environ.get("PYTHONPATH")
        if existing:
            paths.append(existing)
        return {
            "CMSC127_PRIVATE_CONSOLE": "1",
            "PYTHONPATH": os.pathsep.join(paths),
        }

    def test_sigint_then_sigint_keeps_the_first_code(
        self: ConsoleStopPairTests,
    ) -> None:
        self.assert_pair_keeps_the_first_code("SIGINT", "SIGINT")

    def test_sigint_then_sigbreak_keeps_the_first_code(
        self: ConsoleStopPairTests,
    ) -> None:
        self.assert_pair_keeps_the_first_code("SIGINT", "SIGBREAK")

    def test_sigbreak_then_sigint_keeps_the_first_code(
        self: ConsoleStopPairTests,
    ) -> None:
        self.assert_pair_keeps_the_first_code("SIGBREAK", "SIGINT")

    def test_sigbreak_then_sigbreak_keeps_the_first_code(
        self: ConsoleStopPairTests,
    ) -> None:
        self.assert_pair_keeps_the_first_code("SIGBREAK", "SIGBREAK")

    def test_self_test_sigint_then_sigint_keeps_the_first_code(
        self: ConsoleStopPairTests,
    ) -> None:
        self.assert_pair_keeps_the_first_code("SIGINT", "SIGINT", self_test=True)

    def test_self_test_sigint_then_sigbreak_keeps_the_first_code(
        self: ConsoleStopPairTests,
    ) -> None:
        self.assert_pair_keeps_the_first_code("SIGINT", "SIGBREAK", self_test=True)

    def test_self_test_sigbreak_then_sigint_keeps_the_first_code(
        self: ConsoleStopPairTests,
    ) -> None:
        self.assert_pair_keeps_the_first_code("SIGBREAK", "SIGINT", self_test=True)

    def test_self_test_sigbreak_then_sigbreak_keeps_the_first_code(
        self: ConsoleStopPairTests,
    ) -> None:
        self.assert_pair_keeps_the_first_code("SIGBREAK", "SIGBREAK", self_test=True)


@unittest.skipIf(
    sys.platform == "win32",
    "Windows ends a process on SIGTERM without running a Python handler",
)
class StopSignalPairTests(StopPairTests):
    """Two POSIX stops at the handler change keep the first stop's code.

    The CLI path keeps the guard installed until the process exits, so the
    second stop is held like the first. Every ordered pair runs on both entry
    paths. The first signal sets the exit code.
    """

    def test_every_ordered_pair_keeps_the_first_code(
        self: StopSignalPairTests,
    ) -> None:
        for self_test in (False, True):
            for first in POSIX_STOPS:
                for second in POSIX_STOPS:
                    with self.subTest(first=first, second=second, self_test=self_test):
                        self.assert_pair_keeps_the_first_code(
                            first, second, self_test=self_test
                        )


class StopSignalHandlerTests(unittest.TestCase):
    """The stop handlers go in before the create and come off after the drop."""

    def stop_numbers(self: StopSignalHandlerTests) -> list[int]:
        """Every stop signal that this platform has."""
        return [
            getattr(signal, name)
            for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK")
            if hasattr(signal, name)
        ]

    def test_the_cli_path_keeps_the_guard_installed(
        self: StopSignalHandlerTests,
    ) -> None:
        """The CLI path never puts a default handler back while it lives."""
        original = signal.getsignal(signal.SIGTERM)
        guard = runner._StopGuard()  # pyright: ignore[reportPrivateUsage]
        guard.install()
        try:
            code = runner._end_run(guard, 7, True)  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(7, code)
            self.assertEqual(guard.handle, signal.getsignal(signal.SIGTERM))
        finally:
            guard.restore()

        self.assertIs(original, signal.getsignal(signal.SIGTERM))

    def test_the_library_path_puts_every_handler_back(
        self: StopSignalHandlerTests,
    ) -> None:
        original = signal.getsignal(signal.SIGTERM)
        guard = runner._StopGuard()  # pyright: ignore[reportPrivateUsage]
        guard.install()
        code = runner._end_run(guard, 7, False)  # pyright: ignore[reportPrivateUsage]

        self.assertEqual(7, code)
        self.assertIs(original, signal.getsignal(signal.SIGTERM))

    def test_a_caught_stop_leaves_every_previous_handler_in_place(
        self: StopSignalHandlerTests,
    ) -> None:
        """A caller that catches the stop gets every handler back."""
        numbers = self.stop_numbers()
        before = {number: signal.getsignal(number) for number in numbers}
        guard = runner._StopGuard()  # pyright: ignore[reportPrivateUsage]
        guard.install()
        guard.phase = "cleanup"
        guard.handle(signal.SIGINT, None)

        with self.assertRaises(SystemExit) as raised:
            runner._end_run(guard, 0, False)  # pyright: ignore[reportPrivateUsage]

        self.assertEqual(128 + signal.SIGINT, raised.exception.code)
        for number in numbers:
            self.assertIs(before[number], signal.getsignal(number))

    def test_a_registration_failure_reaches_the_caller(
        self: StopSignalHandlerTests,
    ) -> None:
        """A real registration failure reaches the caller, and the slot stays.

        The section keeps every caller handler out of the restore loop, so the
        only exception from signal.signal is a real registration failure. The
        slot stays in the list, so a later pass can retry it.
        """
        guard = runner._StopGuard()  # pyright: ignore[reportPrivateUsage]
        guard.install()
        guard.phase = "cleanup"
        guard.handle(signal.SIGINT, None)
        real_signal = signal.signal
        calls: list[int] = []

        def interrupted(number: int, handler: Any) -> Any:
            calls.append(number)
            if len(calls) == 1:
                raise KeyboardInterrupt
            return real_signal(number, handler)

        with patch.object(signal, "signal", interrupted):
            with self.assertRaises(SystemExit) as raised:
                runner._end_run(guard, 0, False)  # pyright: ignore[reportPrivateUsage]

        self.assertEqual(128 + signal.SIGINT, raised.exception.code)
        self.assertIn(calls[0], guard.previous)

    def test_install_sets_the_handler_and_restore_puts_the_old_one_back(
        self: StopSignalHandlerTests,
    ) -> None:
        original = signal.getsignal(signal.SIGTERM)
        guard = runner._StopGuard()  # pyright: ignore[reportPrivateUsage]
        guard.install()
        try:
            self.assertEqual(guard.handle, signal.getsignal(signal.SIGTERM))
        finally:
            guard.restore()

        self.assertIs(original, signal.getsignal(signal.SIGTERM))

    @unittest.skipIf(sys.platform == "win32", "Windows has no SIGHUP")
    def test_install_covers_sighup(self: StopSignalHandlerTests) -> None:
        sighup = getattr(signal, "SIGHUP")
        guard = runner._StopGuard()  # pyright: ignore[reportPrivateUsage]
        guard.install()
        try:
            self.assertEqual(guard.handle, signal.getsignal(sighup))
        finally:
            guard.restore()

    @unittest.skipUnless(sys.platform == "win32", "a console handler is a Windows event")
    def test_install_registers_the_console_handler_and_restore_removes_it(
        self: StopSignalHandlerTests,
    ) -> None:
        guard = runner._StopGuard()  # pyright: ignore[reportPrivateUsage]
        guard.install()
        try:
            self.assertIsNotNone(guard.console_handler)
        finally:
            guard.restore()

        self.assertIsNone(guard.console_handler)

    def test_a_console_close_drops_the_protected_database(
        self: StopSignalHandlerTests,
    ) -> None:
        """The close handler names the database that the run still holds."""
        guard = runner._StopGuard()  # pyright: ignore[reportPrivateUsage]
        guard.protect("cmsc127_race_0123456789abcdef", "postgresql://localhost/postgres")
        with patch.object(
            guard, "_drop_console_database", wraps=None
        ) as drop:
            code = guard.console_event(2)

        self.assertEqual(0, code)
        drop.assert_called_once_with("cmsc127_race_0123456789abcdef")

    def test_a_console_close_without_a_database_drops_nothing(
        self: StopSignalHandlerTests,
    ) -> None:
        guard = runner._StopGuard()  # pyright: ignore[reportPrivateUsage]
        with patch.object(guard, "_drop_console_database") as drop:
            code = guard.console_event(2)

        self.assertEqual(0, code)
        drop.assert_not_called()

    def test_a_body_stop_ends_the_run_through_system_exit(
        self: StopSignalHandlerTests,
    ) -> None:
        guard = runner._StopGuard()  # pyright: ignore[reportPrivateUsage]
        guard.phase = "body"

        with self.assertRaises(SystemExit) as raised:
            guard.handle(signal.SIGTERM, None)

        self.assertEqual(128 + signal.SIGTERM, raised.exception.code)

    def test_a_cleanup_stop_waits_for_the_section_to_finish(
        self: StopSignalHandlerTests,
    ) -> None:
        guard = runner._StopGuard()  # pyright: ignore[reportPrivateUsage]
        guard.phase = "cleanup"

        guard.handle(signal.SIGTERM, None)
        guard.handle(signal.SIGTERM, None)

        self.assertEqual(signal.SIGTERM, guard.pending)

    def test_an_install_stop_waits_for_the_handlers_to_go_in(
        self: StopSignalHandlerTests,
    ) -> None:
        """A stop during install is recorded, so the loop can finish."""
        guard = runner._StopGuard()  # pyright: ignore[reportPrivateUsage]

        guard.handle(signal.SIGTERM, None)

        self.assertEqual("install", guard.phase)
        self.assertEqual(signal.SIGTERM, guard.pending)

    def test_a_final_stop_ends_the_process_at_once(
        self: StopSignalHandlerTests,
    ) -> None:
        """The final phase ends the process with the first code."""
        guard = runner._StopGuard()  # pyright: ignore[reportPrivateUsage]
        guard.phase = "cleanup"
        guard.handle(signal.SIGTERM, None)
        guard.phase = "final"
        exits: list[int] = []

        with patch.object(runner.os, "_exit", exits.append):
            guard.handle(signal.SIGINT, None)

        self.assertEqual([128 + signal.SIGTERM], exits)

    def test_pending_stop_signal_ends_the_run_after_cleanup(
        self: StopSignalHandlerTests,
    ) -> None:
        """The signal stays recorded, so a later one cannot replace it."""
        guard = runner._StopGuard()  # pyright: ignore[reportPrivateUsage]
        guard.phase = "cleanup"
        guard.handle(signal.SIGTERM, None)
        guard.phase = "body"

        with self.assertRaises(SystemExit) as raised:
            guard.stop_if_pending()

        self.assertEqual(128 + signal.SIGTERM, raised.exception.code)
        self.assertEqual(signal.SIGTERM, guard.pending)

    @unittest.skipIf(sys.platform == "win32", "Windows has no SIGHUP")
    def test_a_later_cleanup_signal_keeps_the_first_exit_code(
        self: StopSignalHandlerTests,
    ) -> None:
        """The first signal sets the exit, through cleanup and after it."""
        guard = runner._StopGuard()  # pyright: ignore[reportPrivateUsage]
        first = runner._StopGuard()  # pyright: ignore[reportPrivateUsage]
        first.phase = "body"

        with self.assertRaises(SystemExit) as raised:
            first.handle(signal.SIGTERM, None)
        self.assertEqual(128 + signal.SIGTERM, raised.exception.code)

        guard.phase = "cleanup"
        guard.handle(signal.SIGTERM, None)
        guard.handle(cast(int, getattr(signal, "SIGHUP")), None)

        self.assertEqual(signal.SIGTERM, guard.pending)
        with self.assertRaises(SystemExit) as stopped:
            guard.stop_if_pending()
        self.assertEqual(128 + signal.SIGTERM, stopped.exception.code)


GUARD_ERROR_DRIVER = '''\
"""Reach one guard error branch and report the lines it ran."""

from __future__ import annotations

from pathlib import Path
import signal
import sys
import threading

HARNESS = Path(sys.argv[1])
MARKER = Path(sys.argv[2])
MODE = sys.argv[3]
sys.path.insert(0, str(HARNESS))

import runner

TARGETS = (
    "_guarded_signals",
    "_begin_section",
    "_end_section",
    "_deliver_console_stop",
    "_hold_console_stop",
    "install",
    "record_interrupt",
    "restore",
    "_restore_handler",
    "stop_if_pending",
    "_end_run",
)
SEEN: list[tuple[str, int]] = []


def note(text: str) -> None:
    with MARKER.open("a", encoding="utf-8") as handle:
        handle.write(text + "\\n")


def local(frame, event, arg):
    if event == "line":
        SEEN.append((frame.f_code.co_name, frame.f_lineno))
    return local


def tracer(frame, event, arg):
    if event == "call" and frame.f_code.co_name in TARGETS:
        return local
    return None


def off_thread() -> None:
    """Install while the guard sees a worker thread, so it returns early."""
    guard = runner._StopGuard()
    real = threading.current_thread
    threading.current_thread = lambda: object()
    try:
        guard.install()
    finally:
        threading.current_thread = real
    note("phase " + guard.phase)


def stop_if_pending() -> None:
    """Raise the recorded stop through the cleanup check."""
    guard = runner._StopGuard()
    guard.pending = signal.SIGTERM
    try:
        guard.stop_if_pending()
    except SystemExit as requested:
        note("exit " + str(requested.code))


def install_getsignal_error() -> None:
    """Make the platform refuse the handler query for one signal."""
    guard = runner._StopGuard()
    real = signal.getsignal

    def getsignal(number):
        if number == signal.SIGTERM:
            raise OSError("the platform refused the query")
        return real(number)

    signal.getsignal = getsignal
    try:
        guard.install()
    finally:
        signal.getsignal = real
    note("previous " + str(sorted(guard.previous)))


def install_signal_error() -> None:
    """Make the platform refuse the handler change for one signal."""
    guard = runner._StopGuard()
    real = signal.signal

    def change(number, handler):
        if number == signal.SIGTERM:
            raise ValueError("the platform refused the handler")
        return real(number, handler)

    signal.signal = change
    try:
        guard.install()
    finally:
        signal.signal = real
    note("previous " + str(sorted(guard.previous)))


def install_interrupt() -> None:
    """Raise KeyboardInterrupt before the guard registers SIGINT."""
    guard = runner._StopGuard()
    real = signal.signal
    state = {"done": False}

    def change(number, handler):
        if not state["done"] and number == signal.SIGINT:
            state["done"] = True
            raise KeyboardInterrupt
        return real(number, handler)

    signal.signal = change
    try:
        guard.install()
    finally:
        signal.signal = real
    note("pending " + str(guard.pending))
    note("phase " + guard.phase)


def install_interrupt_custom() -> None:
    """Raise KeyboardInterrupt while the caller owns SIGINT."""
    guard = runner._StopGuard()

    def custom(number, frame):
        pass

    signal.signal(signal.SIGINT, custom)
    real = signal.signal
    state = {"done": False}

    def change(number, handler):
        if not state["done"] and number == signal.SIGTERM:
            state["done"] = True
            raise KeyboardInterrupt
        return real(number, handler)

    signal.signal = change
    try:
        guard.install()
    except KeyboardInterrupt:
        note("interrupt escaped install")
    finally:
        signal.signal = real
        signal.signal(signal.SIGINT, signal.default_int_handler)
    note("pending " + str(guard.pending))


def record_interrupt_error() -> None:
    """Make the platform refuse the SIGINT query."""
    guard = runner._StopGuard()
    real = signal.getsignal

    def getsignal(number):
        if number == signal.SIGINT:
            raise ValueError("the platform refused the query")
        return real(number)

    signal.getsignal = getsignal
    try:
        note("recorded " + str(guard.record_interrupt()))
    finally:
        signal.getsignal = real


def restore_interrupt() -> None:
    """Raise KeyboardInterrupt inside the first handler replacement.

    The section keeps every caller handler out of the loop, so this exception
    comes from signal.signal itself and reaches the caller.
    """
    guard = runner._StopGuard()
    guard.install()
    real = signal.signal
    state = {"done": False}

    def change(number, handler):
        if not state["done"] and number != signal.SIGINT:
            state["done"] = True
            raise KeyboardInterrupt
        return real(number, handler)

    signal.signal = change
    try:
        guard.restore()
    except BaseException as error:
        note("raised " + type(error).__name__)
    finally:
        signal.signal = real
    note("pending " + str(guard.pending))
    note("previous " + str(sorted(guard.previous)))


def restore_signal_error() -> None:
    """Make the platform refuse the first handler replacement.

    A real registration failure reaches the caller, and the slot stays in the
    list.
    """
    guard = runner._StopGuard()
    guard.install()
    real = signal.signal
    state = {"done": False}

    def change(number, handler):
        if not state["done"] and number != signal.SIGINT:
            state["done"] = True
            raise OSError("the platform refused the handler")
        return real(number, handler)

    signal.signal = change
    try:
        guard.restore()
    except BaseException as error:
        note("raised " + type(error).__name__)
    finally:
        signal.signal = real
    note("previous " + str(sorted(guard.previous)))


def restore_failure() -> None:
    """Raise from a restored caller handler, with no latched stop."""
    guard = runner._StopGuard()
    guard.install()
    real = signal.signal
    state = {"done": False}

    def change(number, handler):
        result = real(number, handler)
        if not state["done"] and number != signal.SIGINT:
            state["done"] = True
            raise RuntimeError("caller custom stop")
        return result

    signal.signal = change
    try:
        guard.restore()
    except RuntimeError as error:
        note("raised " + str(error))
    finally:
        signal.signal = real
    note("previous " + str(sorted(guard.previous)))


def restore_skip() -> None:
    """Put one handler back twice, so the second call skips the slot."""
    guard = runner._StopGuard()
    guard.install()
    guard._restore_handler(signal.SIGTERM)
    guard._restore_handler(signal.SIGTERM)
    note("previous " + str(sorted(guard.previous)))


def flush_error() -> None:
    """Make the output stream refuse the flush on the CLI path."""
    guard = runner._StopGuard()

    class Refusing:
        def flush(self):
            raise OSError("the stream refused the flush")

        def write(self, text):
            return len(text)

    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout = Refusing()
    sys.stderr = Refusing()
    try:
        code = runner._end_run(guard, 0, True)
    finally:
        sys.stdout, sys.stderr = real_out, real_err
    note("code " + str(code))


def guarded_signals() -> None:
    """Read the signal numbers that install replaces."""
    guard = runner._StopGuard()
    note("guarded " + str(sorted(guard._guarded_signals())))


def mask_path() -> None:
    """Run the mask path where the platform has no signal mask."""
    guard = runner._StopGuard()
    calls = []

    def block(how, mask):
        calls.append((how, sorted(mask)))
        return set()

    real = runner._signal_mask
    runner._signal_mask = lambda: (block, 0, 1)
    try:
        guard._begin_section()
        guard._end_section()
    finally:
        runner._signal_mask = real
    note("mask calls " + str(calls))


def hold_console_stop() -> None:
    """Hold a console stop inside a section and outside one."""
    guard = runner._StopGuard()
    signal.signal(signal.SIGINT, guard.handle)
    note("outside " + str(guard._hold_console_stop(signal.SIGINT)))
    guard._begin_section()
    note("inside " + str(guard._hold_console_stop(signal.SIGINT)))
    note("again " + str(guard._hold_console_stop(signal.SIGTERM)))
    guard._end_section()
    note("pending " + str(guard.pending))
    signal.signal(signal.SIGINT, signal.default_int_handler)


def deliver_console_stop() -> None:
    """Give a held console stop to the guard's handler and to a default one."""
    guard = runner._StopGuard()
    guard.install()
    guard.phase = "cleanup"
    guard._deliver_console_stop(signal.SIGINT)
    note("pending " + str(guard.pending))
    fresh = runner._StopGuard()
    real = signal.getsignal

    def getsignal(number):
        if number == signal.SIGTERM:
            raise OSError("the platform refused the query")
        return real(number)

    signal.getsignal = getsignal
    try:
        fresh._deliver_console_stop(signal.SIGTERM)
    finally:
        signal.getsignal = real
    note("fresh pending " + str(fresh.pending))
    try:
        guard.restore()
    except SystemExit as requested:
        note("exit " + str(requested.code))


MODES = {
    "off-thread": off_thread,
    "install-getsignal-error": install_getsignal_error,
    "install-signal-error": install_signal_error,
    "install-interrupt": install_interrupt,
    "install-interrupt-custom": install_interrupt_custom,
    "record-interrupt-error": record_interrupt_error,
    "restore-interrupt": restore_interrupt,
    "restore-signal-error": restore_signal_error,
    "restore-failure": restore_failure,
    "restore-skip": restore_skip,
    "flush-error": flush_error,
    "stop-if-pending": stop_if_pending,
    "guarded-signals": guarded_signals,
    "mask-path": mask_path,
    "hold-console-stop": hold_console_stop,
    "deliver-console-stop": deliver_console_stop,
}

sys.settrace(tracer)
try:
    MODES[MODE]()
finally:
    sys.settrace(None)
for name, line in SEEN:
    note(f"line {name}:{line}")
note("done")
'''

# The guard lines that StopPhaseInjectionTests must reach. Each entry names
# the function, a source snippet that identifies the line, and the occurrence
# of that snippet inside the function. The test reads the line number from
# runner.py, so a shift in the file cannot hide a gap.
REQUIRED_GUARD_LINES = (
    ("_guarded_signals", "return {", 0),
    ("_guarded_signals", "            number", 0),
    ("_guarded_signals", "for number in (getattr(signal, name, None) for name in GUARD_SIGNALS)", 0),
    ("_guarded_signals", "if number is not None", 0),
    ("_begin_section", "with self.section_lock:", 0),
    ("_begin_section", "self.section = True", 0),
    ("_begin_section", "self.console_stop = None", 0),
    ("_begin_section", "operations = _signal_mask()", 0),
    ("_begin_section", "if operations is None:", 0),
    ("_begin_section", "block, how, _ = operations", 0),
    ("_begin_section", "self.mask = block(how, self._guarded_signals())", 0),
    ("_end_section", "mask = self.mask", 0),
    ("_end_section", "self.mask = None", 0),
    ("_end_section", "with self.section_lock:", 0),
    ("_end_section", "self.section = False", 0),
    ("_end_section", "number = self.console_stop", 0),
    ("_end_section", "self.console_stop = None", 0),
    ("_end_section", "operations = _signal_mask()", 0),
    ("_end_section", "if mask is not None and operations is not None:", 0),
    ("_end_section", "block, _, how = operations", 0),
    ("_end_section", "block(how, mask)", 0),
    ("_end_section", "if number is not None:", 0),
    ("_end_section", "self._deliver_console_stop(number)", 0),
    ("_deliver_console_stop", "try:", 0),
    ("_deliver_console_stop", "handler = signal.getsignal(number)", 0),
    ("_deliver_console_stop", "except (OSError, ValueError):", 0),
    ("_deliver_console_stop", "handler = None", 0),
    ("_deliver_console_stop", "if callable(handler):", 0),
    ("_deliver_console_stop", "handler(number, None)", 0),
    ("_deliver_console_stop", "return", 0),
    ("_deliver_console_stop", "if self.pending is None:", 0),
    ("_deliver_console_stop", "self.pending = number", 0),
    ("_deliver_console_stop", "self.stopped_at = time.monotonic()", 0),
    ("_hold_console_stop", "with self.section_lock:", 0),
    ("_hold_console_stop", "if not self.section:", 0),
    ("_hold_console_stop", "return 0", 0),
    ("_hold_console_stop", "if self.console_stop is None:", 0),
    ("_hold_console_stop", "self.console_stop = number", 0),
    ("_hold_console_stop", "return 1", 0),
    ("install", "if threading.current_thread() is not threading.main_thread():", 0),
    ("install", 'self.phase = "body"', 0),
    ("install", "return", 0),
    ("install", "self._install_console_handler()", 0),
    ("install", "try:", 0),
    ("install", "try:", 1),
    ("install", "self._begin_section()", 0),
    ("install", "for name in GUARD_SIGNALS:", 0),
    ("install", "number = getattr(signal, name, None)", 0),
    ("install", "if number is None:", 0),
    ("install", "continue", 0),
    ("install", "try:", 2),
    ("install", "previous = signal.getsignal(number)", 0),
    ("install", "except (OSError, ValueError):", 0),
    ("install", "continue", 1),
    ("install", "self.previous[number] = previous", 0),
    ("install", "try:", 3),
    ("install", "signal.signal(number, self.handle)", 0),
    ("install", "except (OSError, ValueError):", 1),
    ("install", "self.previous.pop(number, None)", 0),
    ("install", "continue", 2),
    ("install", "except KeyboardInterrupt:", 0),
    ("install", "if not self.record_interrupt():", 0),
    ("install", "                raise", 0),
    ("install", "self._end_section()", 0),
    ("install", 'self.phase = "body"', 1),
    ("record_interrupt", "try:", 0),
    ("record_interrupt", "current = signal.getsignal(signal.SIGINT)", 0),
    ("record_interrupt", "except (OSError, ValueError):", 0),
    ("record_interrupt", "return False", 0),
    ("record_interrupt", "if current is not signal.default_int_handler:", 0),
    ("record_interrupt", "return False", 1),
    ("record_interrupt", "if self.pending is None:", 0),
    ("record_interrupt", "self.pending = signal.SIGINT", 0),
    ("record_interrupt", "self.stopped_at = time.monotonic()", 0),
    ("record_interrupt", "return True", 0),
    ("restore", "try:", 0),
    ("restore", "self._begin_section()", 0),
    ("restore", "for number in [n for n in self.previous if n != signal.SIGINT]:", 0),
    ("restore", "self._restore_handler(number)", 0),
    ("restore", "if signal.SIGINT in self.previous:", 0),
    ("restore", "self._restore_handler(signal.SIGINT)", 0),
    ("restore", "try:", 1),
    ("restore", "self._end_section()", 0),
    ("restore", "self._remove_console_handler()", 0),
    ("restore", "if self.pending is not None:", 0),
    ("restore", "raise SystemExit(128 + self.pending)", 0),
    ("_restore_handler", "handler = self.previous.get(number)", 0),
    ("_restore_handler", "if handler is None:", 0),
    ("_restore_handler", "return", 0),
    ("_restore_handler", "signal.signal(number, handler)", 0),
    ("_restore_handler", "self.previous.pop(number, None)", 0),
    ("stop_if_pending", "while self.pending is not None:", 0),
    ("stop_if_pending", "raise SystemExit(128 + self.pending)", 0),
    ("_end_run", "if keep_guard:", 0),
    ("_end_run", "for stream in (sys.stdout, sys.stderr):", 0),
    ("_end_run", "try:", 0),
    ("_end_run", "stream.flush()", 0),
    ("_end_run", "except (OSError, ValueError):", 0),
    ("_end_run", "pass", 0),
    ("_end_run", 'guard.phase = "final"', 0),
    ("_end_run", "guard.stop_if_pending()", 0),
    ("_end_run", "return code", 0),
    ("_end_run", 'guard.phase = "cleanup"', 0),
    ("_end_run", "try:", 1),
    ("_end_run", "guard.restore()", 0),
    ("_end_run", "return code", 1),
    ("_end_run", "guard.stop_if_pending()", 1),
)

# A platform with no signal mask returns from _begin_section before the mask
# call, so that one line runs only on Windows. Every other required line runs
# on both platforms.
NO_MASK_GUARD_LINES = (
    ("_begin_section", "return", 0),
)


def guard_source() -> list[str]:
    """The lines of the runner module under test."""
    return Path(runner.__file__).read_text(encoding="utf-8").splitlines()


def guard_line(function: str, anchor: str, occurrence: int = 0) -> int:
    """The line number of an anchor inside one guard function.

    The search starts at the function's def line and stops at the next def at
    the same indentation, so an anchor that appears in two functions resolves
    to the right one.
    """
    source = guard_source()
    start = next(
        index
        for index, line in enumerate(source, start=1)
        if line.strip().startswith(f"def {function}(")
    )
    indent = len(source[start - 1]) - len(source[start - 1].lstrip())
    end = len(source)
    for index in range(start + 1, len(source) + 1):
        line = source[index - 1]
        stripped = line.lstrip()
        if stripped.startswith("def ") and len(line) - len(stripped) <= indent:
            end = index - 1
            break
    seen = 0
    for index in range(start, end + 1):
        if anchor in source[index - 1]:
            if seen == occurrence:
                return index
            seen += 1
    raise AssertionError(f"{function} has no line {occurrence} that reads {anchor}")


@unittest.skipUnless(os.environ.get("CMSC127_DATABASE_URL"), "requires PostgreSQL")
class StopPhaseInjectionTests(unittest.TestCase):
    """A stop at any line of the guard's own code keeps the first code.

    A handler can run between any two bytecodes, so no placement of checks
    closes the class. This test calls the guard's handler at every line event
    inside install, restore, _end_run, and stop_if_pending. It runs both entry
    functions on both entry paths, and sends a second, different stop right
    after the first at each point. The first code must still set the exit. No
    traceback may appear. The library path must put every handler back. The
    scratch database must be gone or never created.
    """

    def stop_names(self: StopPhaseInjectionTests) -> list[str]:
        """Every stop signal that this platform has."""
        return [
            name
            for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK")
            if hasattr(signal, name)
        ]

    def run_driver(
        self: StopPhaseInjectionTests,
        entry: str,
        keep: bool,
        point: str,
        first: str,
        second: str,
    ) -> tuple[int, list[str], str, bool]:
        """Run the driver at one point and report what the run left."""
        url = os.environ["CMSC127_DATABASE_URL"]
        token = uuid4().hex
        name = f"cmsc127_race_{token}"
        notes: list[str] = []
        output = ""
        returncode = -1
        leftover = False
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "starter"
            runner_dir = base / "runner"
            make_starter_root(root, runner_dir)
            (root / "run").write_text(
                "#!/usr/bin/env bash\nset -eu\nexit 0\n",
                encoding="utf-8",
                newline="\n",
            )
            driver = base / "driver.py"
            driver.write_text(PHASE_DRIVER, encoding="utf-8")
            marker = base / "marker.txt"
            process = subprocess.run(
                [
                    sys.executable,
                    str(driver),
                    str(Path(runner.__file__).resolve().parent),
                    str(marker),
                    point,
                    token,
                    entry,
                    first,
                    second,
                    "1" if keep else "0",
                    str(root),
                    str(runner_dir),
                ],
                cwd=root,
                env=os.environ.copy(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
            returncode = process.returncode
            output = process.stdout + process.stderr
            if marker.exists():
                notes = marker.read_text(encoding="utf-8").splitlines()
            leftover = race_database_exists(url, name)
            drop_race_database(url, name)
        return returncode, notes, output, leftover

    def assert_point_keeps_the_first_code(
        self: StopPhaseInjectionTests,
        entry: str,
        keep: bool,
        point: str,
        first: str,
        second: str,
    ) -> None:
        """Check one injection point for the first code and a clean exit."""
        returncode, notes, output, leftover = self.run_driver(
            entry, keep, point, first, second
        )
        detail = (
            f"{entry} keep={keep} {point} exited {returncode} notes {notes}\n{output}"
        )
        self.assertIn(f"inject {first} then {second}", notes, detail)
        self.assertNotIn("Traceback", output, detail)
        self.assertEqual(128 + getattr(signal, first), returncode, detail)
        self.assertFalse(leftover, detail)
        if not keep:
            self.assertIn("restored True", notes, detail)

    def test_every_line_event_keeps_the_first_code(
        self: StopPhaseInjectionTests,
    ) -> None:
        """Every line event of the guard's own code is an injection point."""
        names = self.stop_names()
        first = "SIGINT"
        second = "SIGTERM" if "SIGTERM" in names else "SIGBREAK"
        covered = 0
        for entry in ("normal", "selftest"):
            for keep in (False, True):
                _, notes, output, _ = self.run_driver(
                    entry, keep, "none", first, second
                )
                counted = [line for line in notes if line.startswith("points ")]
                self.assertTrue(counted, output)
                points = int(counted[0].split()[1])
                self.assertGreater(points, 0, output)
                for index in range(points):
                    with self.subTest(entry=entry, keep=keep, point=index):
                        self.assert_point_keeps_the_first_code(
                            entry, keep, f"line:{index}", first, second
                        )
                    covered += 1
                if keep:
                    with self.subTest(entry=entry, keep=keep, point="after-return"):
                        self.assert_point_keeps_the_first_code(
                            entry, keep, "after-return", first, second
                        )
                    covered += 1
        self.assertGreaterEqual(covered, 150)

    def lines_from(self: StopPhaseInjectionTests, notes: list[str]) -> set[tuple[str, int]]:
        """The guard lines that a driver run reported."""
        reached: set[tuple[str, int]] = set()
        for line in notes:
            if not line.startswith("line "):
                continue
            name, _, number = line[5:].partition(":")
            reached.add((name, int(number)))
        return reached

    def run_error_driver(self: StopPhaseInjectionTests, mode: str) -> list[str]:
        """Run one guard error branch and report the lines it reached."""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            driver = base / "driver.py"
            driver.write_text(GUARD_ERROR_DRIVER, encoding="utf-8")
            marker = base / "marker.txt"
            process = subprocess.run(
                [
                    sys.executable,
                    str(driver),
                    str(Path(runner.__file__).resolve().parent),
                    str(marker),
                    mode,
                ],
                cwd=base,
                env=os.environ.copy(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
            notes = (
                marker.read_text(encoding="utf-8").splitlines()
                if marker.exists()
                else []
            )
        detail = f"{mode} notes {notes}\n{process.stdout}{process.stderr}"
        self.assertNotIn("Traceback", process.stdout + process.stderr, detail)
        self.assertIn("done", notes, detail)
        return notes

    def test_every_required_guard_line_runs(
        self: StopPhaseInjectionTests,
    ) -> None:
        """Every guard line this class must reach is reached by some case.

        The clean run covers the lines a normal end takes. The error-path
        cases cover the branches a clean run never takes. The cleanup entry
        and the final exit are named markers, because neither is a line event
        inside a traced function.
        """
        reached: set[tuple[str, int]] = set()
        markers: set[str] = set()
        for entry in ("normal", "selftest"):
            for keep in (False, True):
                _, notes, output, _ = self.run_driver(
                    entry, keep, "none", "SIGINT", "SIGTERM"
                )
                self.assertTrue(notes, output)
                reached.update(self.lines_from(notes))
                markers.update(notes)
        for mode in (
            "off-thread",
            "install-getsignal-error",
            "install-signal-error",
            "install-interrupt",
            "install-interrupt-custom",
            "record-interrupt-error",
            "restore-interrupt",
            "restore-signal-error",
            "restore-failure",
            "restore-skip",
            "flush-error",
            "stop-if-pending",
            "guarded-signals",
            "mask-path",
            "hold-console-stop",
            "deliver-console-stop",
        ):
            reached.update(self.lines_from(self.run_error_driver(mode)))
        required = list(REQUIRED_GUARD_LINES)
        if sys.platform == "win32":
            required.extend(NO_MASK_GUARD_LINES)
        missing = [
            f"{function}:{guard_line(function, anchor, occurrence)} {anchor}"
            for function, anchor, occurrence in required
            if (function, guard_line(function, anchor, occurrence)) not in reached
        ]
        self.assertEqual(
            missing, [], "the class test never reached these guard lines"
        )
        self.assertIn(
            ("_finish_run", 0), reached, "the class test never reached the cleanup entry"
        )
        # The final exit is a named marker. It runs only when a stop arrives
        # after the CLI path sets the final phase, so this case injects at the
        # line that follows that assignment.
        _, notes, output, _ = self.run_driver(
            "normal", True, "none", "SIGINT", "SIGTERM"
        )
        seen = [
            (line[5:].partition(":")[0], int(line[5:].partition(":")[2]))
            for line in notes
            if line.startswith("line ")
        ]
        final = ("_end_run", guard_line("_end_run", "guard.stop_if_pending()", 0))
        self.assertIn(final, seen, output)
        _, notes, output, _ = self.run_driver(
            "normal", True, f"line:{seen.index(final)}", "SIGINT", "SIGTERM"
        )
        markers.update(notes)
        self.assertIn(
            "os._exit 130",
            markers,
            "the class test never reached the final exit",
        )


@unittest.skipIf(
    sys.platform == "win32",
    "Windows delivers a stop as a console event, which needs its own console",
)
@unittest.skipUnless(os.environ.get("CMSC127_DATABASE_URL"), "requires PostgreSQL")
class SectionMaskTests(unittest.TestCase):
    """No Python handler runs inside install or restore.

    The driver delivers a native stop at every line event inside install and
    restore. The guard's handler must not run while the section is open. The
    section must take the stop when it ends, so the run ends at the stop's
    code, or the caller's own handler runs and its exception reaches the
    caller.
    """

    def process_options(self: SectionMaskTests) -> dict[str, Any]:
        """The process options that this platform's delivery needs."""
        return {}

    def interpreter(self: SectionMaskTests) -> str:
        """The interpreter that runs the driver."""
        return sys.executable

    def extra_environment(self: SectionMaskTests) -> dict[str, str]:
        """The variables that this platform's delivery needs."""
        return {}

    def run_driver(
        self: SectionMaskTests,
        entry: str,
        point: str,
        first: str,
    ) -> tuple[int, list[str], str, bool]:
        """Run the driver at one point and report what the run left."""
        url = os.environ["CMSC127_DATABASE_URL"]
        token = uuid4().hex
        name = f"cmsc127_race_{token}"
        notes: list[str] = []
        output = ""
        returncode = -1
        leftover = False
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "starter"
            runner_dir = base / "runner"
            make_starter_root(root, runner_dir)
            (root / "run").write_text(
                "#!/usr/bin/env bash\nset -eu\nexit 0\n",
                encoding="utf-8",
                newline="\n",
            )
            driver = base / "driver.py"
            driver.write_text(SECTION_DRIVER, encoding="utf-8")
            marker = base / "marker.txt"
            environment = os.environ.copy()
            environment.update(self.extra_environment())
            process = subprocess.run(
                [
                    self.interpreter(),
                    str(driver),
                    str(Path(runner.__file__).resolve().parent),
                    str(marker),
                    point,
                    token,
                    entry,
                    first,
                    str(root),
                    str(runner_dir),
                ],
                cwd=root,
                env=environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                **self.process_options(),
            )
            returncode = process.returncode
            output = process.stdout + process.stderr
            if marker.exists():
                notes = marker.read_text(encoding="utf-8").splitlines()
            leftover = race_database_exists(url, name)
            drop_race_database(url, name)
        return returncode, notes, output, leftover

    def inside_count(self: SectionMaskTests, notes: list[str]) -> int:
        """The guard handler calls that ran inside a critical section.

        The driver records every handler change, every guard handler call, and
        each section boundary. A runner without a section has no boundary, so
        the first and last change stand in for one.
        """
        events: list[str] = []
        for line in notes:
            if line.startswith("events "):
                events = line[7:].split("|")
        if not events:
            return -1
        if "begin" in events and "end" in events:
            inside = 0
            depth = 0
            for event in events:
                if event == "begin":
                    depth += 1
                elif event == "end":
                    depth -= 1
                elif event.startswith("handle ") and depth > 0:
                    inside += 1
            return inside
        changes = [
            index for index, event in enumerate(events) if event.startswith("change ")
        ]
        if not changes:
            return -1
        first, last = changes[0], changes[-1]
        return sum(
            1 for event in events[first : last + 1] if event.startswith("handle ")
        )

    def assert_no_handler_runs_inside(
        self: SectionMaskTests,
        entry: str,
        point: str,
        first: str,
    ) -> None:
        """Check one injection point for a held stop and a clean exit."""
        returncode, notes, output, leftover = self.run_driver(entry, point, first)
        detail = f"{entry} {point} exited {returncode} notes {notes}\n{output}"
        self.assertIn("deliver at ", " ".join(notes), detail)
        self.assertEqual(0, self.inside_count(notes), detail)
        self.assertNotIn("Traceback", output, detail)
        self.assertFalse(leftover, detail)
        self.assertIn("restored True", notes, detail)
        pending = [line for line in notes if line.startswith("pending ")]
        self.assertTrue(pending, detail)
        failure = [line for line in notes if line.startswith("failure ")]
        self.assertTrue(failure, detail)
        if pending[0] != "pending None":
            self.assertEqual(128 + getattr(signal, first), returncode, detail)
        else:
            self.assertNotEqual(failure[0], "failure ", detail)

    def test_no_handler_runs_inside_install_or_restore(
        self: SectionMaskTests,
    ) -> None:
        """Every line event inside install and restore holds the stop."""
        first = "SIGINT"
        covered = 0
        for entry in ("normal", "selftest"):
            _, notes, output, _ = self.run_driver(entry, "none", first)
            counted = [line for line in notes if line.startswith("points ")]
            self.assertTrue(counted, output)
            points = int(counted[0].split()[1])
            self.assertGreater(points, 0, output)
            for index in range(points):
                with self.subTest(entry=entry, point=index):
                    self.assert_no_handler_runs_inside(
                        entry, f"line:{index}", first
                    )
                covered += 1
        self.assertGreaterEqual(covered, 40)


@unittest.skipIf(
    sys.platform == "win32",
    "Windows delivers a stop as a console event, which needs its own console",
)
@unittest.skipUnless(os.environ.get("CMSC127_DATABASE_URL"), "requires PostgreSQL")
class SectionStopTests(unittest.TestCase):
    """A native stop inside a critical section is held, then taken.

    The three cases are the review's three findings. Each one delivers a
    native stop inside install or restore and checks the first stop's code,
    the absence of a traceback, and the caller's handlers.
    """

    def process_options(self: SectionStopTests) -> dict[str, Any]:
        """The process options that this platform's delivery needs."""
        return {}

    def interpreter(self: SectionStopTests) -> str:
        """The interpreter that runs the driver."""
        return sys.executable

    def extra_environment(self: SectionStopTests) -> dict[str, str]:
        """The variables that this platform's delivery needs."""
        return {}

    def run_driver(self: SectionStopTests, mode: str) -> tuple[int, list[str], str]:
        """Run one delivery mode and report what it left."""
        notes: list[str] = []
        output = ""
        returncode = -1
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            driver = base / "driver.py"
            driver.write_text(SECTION_STOP_DRIVER, encoding="utf-8")
            marker = base / "marker.txt"
            environment = os.environ.copy()
            environment.update(self.extra_environment())
            process = subprocess.run(
                [
                    self.interpreter(),
                    str(driver),
                    str(Path(runner.__file__).resolve().parent),
                    str(marker),
                    mode,
                ],
                cwd=base,
                env=environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                **self.process_options(),
            )
            returncode = process.returncode
            output = process.stdout + process.stderr
            if marker.exists():
                notes = marker.read_text(encoding="utf-8").splitlines()
        return returncode, notes, output

    def test_a_caller_value_error_reaches_the_caller(
        self: SectionStopTests,
    ) -> None:
        """A caller ValueError inside the section reaches the caller."""
        returncode, notes, output = self.run_driver("caller-value")
        detail = f"caller-value exited {returncode} notes {notes}\n{output}"
        self.assertIn("before restoring SIGINT", notes, detail)
        self.assertIn("caller signal SIGINT", notes, detail)
        self.assertIn("raised ValueError: caller custom stop", notes, detail)
        self.assertIn("restored True", notes, detail)
        self.assertNotIn("Traceback", output, detail)

    def test_a_caller_keyboard_interrupt_reaches_the_caller(
        self: SectionStopTests,
    ) -> None:
        """A caller KeyboardInterrupt inside the section reaches the caller."""
        returncode, notes, output = self.run_driver("caller-keyboard")
        detail = f"caller-keyboard exited {returncode} notes {notes}\n{output}"
        self.assertIn("before restoring SIGINT", notes, detail)
        self.assertIn("caller signal SIGINT", notes, detail)
        self.assertIn(
            "raised KeyboardInterrupt: caller custom stop", notes, detail
        )
        self.assertIn("restored True", notes, detail)
        self.assertNotIn("Traceback", output, detail)

    def test_a_later_stop_at_the_record_keeps_the_first_code(
        self: SectionStopTests,
    ) -> None:
        """A stop at the record does not replace the first stop's code."""
        returncode, notes, output = self.run_driver("install-mixed")
        detail = f"install-mixed exited {returncode} notes {notes}\n{output}"
        self.assertIn("before installing SIGINT", notes, detail)
        self.assertIn("pending 2", notes, detail)
        self.assertIn("phase body", notes, detail)
        self.assertNotIn("Traceback", output, detail)

    def test_a_pair_of_stops_in_install_keeps_one_code(
        self: SectionStopTests,
    ) -> None:
        """Two stops inside the install section end the run with one code.

        The mask holds both stops as one pending set, and the set carries no
        order. The run must still end with one of the two codes, print no
        traceback, and put every handler back. Both arrival orders run, so a
        change to the order rule shows up here.
        """
        codes: list[int] = []
        for first, second in (("SIGTERM", "SIGHUP"), ("SIGHUP", "SIGTERM")):
            with self.subTest(first=first, second=second):
                mode = f"install-pair-{first[3:].lower()}-{second[3:].lower()}"
                returncode, notes, output = self.run_driver(mode)
                detail = f"{mode} exited {returncode} notes {notes}\n{output}"
                self.assertIn("before installing SIGINT", notes, detail)
                self.assertIn(f"signal {first}", notes, detail)
                self.assertIn(f"signal {second}", notes, detail)
                self.assertIn("phase body", notes, detail)
                self.assertNotIn("Traceback", output, detail)
                self.assertIn("restored True", notes, detail)
                pending = [line for line in notes if line.startswith("pending ")]
                self.assertTrue(pending, detail)
                self.assertIn(pending[0], ("pending 1", "pending 15"), detail)
                self.assertEqual(
                    128 + int(pending[0].split()[1]), returncode, detail
                )
                codes.append(returncode)
        # The pending set carries no order, so both arrival orders report the
        # same code. The class comment records why no primitive can do better.
        self.assertEqual(codes[0], codes[1], codes)

    def test_a_stop_pending_at_a_default_restore_reaches_the_caller(
        self: SectionStopTests,
    ) -> None:
        """A stop pending at a default restore does not end the process.

        The caller owns no handler for SIGTERM, so restore puts the default
        action back. A stop that is pending at that moment must be taken by
        the guard, or the process ends instead of returning to the caller.
        """
        returncode, notes, output = self.run_driver("restore-default")
        detail = f"restore-default exited {returncode} notes {notes}\n{output}"
        self.assertIn("before restoring SIGTERM", notes, detail)
        self.assertIn("signal SIGTERM", notes, detail)
        self.assertIn("raised SystemExit: 143", notes, detail)
        self.assertIn("pending 15", notes, detail)
        self.assertIn("restored True", notes, detail)
        self.assertNotIn("Traceback", output, detail)
        self.assertEqual(0, returncode, detail)


@unittest.skipUnless(sys.platform == "win32", "a console event is a Windows event")
class ConsoleSectionStopTests(SectionStopTests):
    """The same three cases, with a native console event on Windows."""

    # The base class skips on Windows. This class is the Windows case.
    __unittest_skip__ = False

    def process_options(self: ConsoleSectionStopTests) -> dict[str, Any]:
        return private_console_options()

    def interpreter(self: ConsoleSectionStopTests) -> str:
        """The base interpreter, so the console holds one process."""
        return str(getattr(sys, "_base_executable", sys.executable))

    def extra_environment(self: ConsoleSectionStopTests) -> dict[str, str]:
        paths = [sysconfig.get_paths()["purelib"]]
        existing = os.environ.get("PYTHONPATH")
        if existing:
            paths.append(existing)
        return {
            "CMSC127_PRIVATE_CONSOLE": "1",
            "PYTHONPATH": os.pathsep.join(paths),
        }

    def test_a_ctrl_break_before_its_replacement_ends_at_149(
        self: ConsoleSectionStopTests,
    ) -> None:
        """A Ctrl+Break before its replacement is held, not the console exit."""
        returncode, notes, output = self.run_driver("install-break")
        detail = f"install-break exited {returncode} notes {notes}\n{output}"
        self.assertIn("before installing SIGBREAK", notes, detail)
        self.assertIn("pending 21", notes, detail)
        self.assertIn("phase body", notes, detail)
        self.assertNotIn("Traceback", output, detail)

    def test_a_pair_of_stops_in_install_keeps_one_code(
        self: ConsoleSectionStopTests,
    ) -> None:
        """Two console stops inside the install section keep the first one.

        The native handler runs at arrival, so the first event is the one the
        guard records. Each order must report its own first event.
        """
        for first, second, code in (
            ("SIGINT", "SIGBREAK", 130),
            ("SIGBREAK", "SIGINT", 149),
        ):
            with self.subTest(first=first, second=second):
                mode = f"install-pair-{first[3:].lower()}-{second[3:].lower()}"
                returncode, notes, output = self.run_driver(mode)
                detail = f"{mode} exited {returncode} notes {notes}\n{output}"
                self.assertIn("before installing SIGINT", notes, detail)
                self.assertIn(f"signal {first}", notes, detail)
                self.assertIn(f"signal {second}", notes, detail)
                self.assertIn("phase body", notes, detail)
                self.assertNotIn("Traceback", output, detail)
                self.assertIn("restored True", notes, detail)
                self.assertIn(f"pending {code - 128}", notes, detail)
                self.assertEqual(code, returncode, detail)

    def test_a_stop_pending_at_a_default_restore_reaches_the_caller(
        self: ConsoleSectionStopTests,
    ) -> None:
        """A console stop pending at a default restore ends at 149.

        The caller owns no handler for SIGBREAK, so the guard must record the
        held event rather than let it reach the default action.
        """
        returncode, notes, output = self.run_driver("restore-default-console")
        detail = (
            f"restore-default-console exited {returncode} notes {notes}\n{output}"
        )
        self.assertIn("before restoring SIGBREAK", notes, detail)
        self.assertIn("raised SystemExit: 149", notes, detail)
        self.assertIn("pending 21", notes, detail)
        self.assertIn("restored True", notes, detail)
        self.assertNotIn("Traceback", output, detail)
        self.assertEqual(0, returncode, detail)


@unittest.skipIf(sys.platform == "win32", "a caller signal mask is a POSIX feature")
@unittest.skipUnless(os.environ.get("CMSC127_DATABASE_URL"), "requires PostgreSQL")
class CallerMaskRestoreTests(unittest.TestCase):
    """A restore leaves a stop pending that the caller's mask already holds.

    An embedding caller can block a stop before it calls the runner and read
    the pending set afterwards. The signal belongs to the caller, so the guard
    must not take it. The run reports its own result, every handler comes back,
    and the caller still holds the stop.
    """

    def scratch_databases(self: CallerMaskRestoreTests) -> list[str]:
        """The scratch databases that a run of this suite would leave."""
        url = os.environ["CMSC127_DATABASE_URL"]
        with psycopg.connect(url) as connection:
            rows = connection.execute(
                "SELECT datname FROM pg_database"
                " WHERE starts_with(datname, 'cmsc127_race_')"
            )
            return sorted(row[0] for row in rows)

    def run_driver(self: CallerMaskRestoreTests) -> tuple[int, list[str], str]:
        """Run one preblocked case and report what it left."""
        notes: list[str] = []
        output = ""
        returncode = -1
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "starter"
            runner_dir = base / "runner"
            make_starter_root(root, runner_dir)
            (root / "run").write_text(
                "#!/usr/bin/env bash\nset -eu\nexit 0\n",
                encoding="utf-8",
                newline="\n",
            )
            driver = base / "driver.py"
            driver.write_text(PREBLOCKED_STOP_DRIVER, encoding="utf-8")
            marker = base / "marker.txt"
            process = subprocess.run(
                [
                    sys.executable,
                    str(driver),
                    str(Path(runner.__file__).resolve().parent),
                    str(marker),
                    str(root),
                    str(runner_dir),
                ],
                cwd=root,
                env=os.environ.copy(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
            returncode = process.returncode
            output = process.stdout + process.stderr
            if marker.exists():
                notes = marker.read_text(encoding="utf-8").splitlines()
        return returncode, notes, output

    def test_a_restore_leaves_a_caller_blocked_stop_pending(
        self: CallerMaskRestoreTests,
    ) -> None:
        """The restore does not take a stop that the caller's mask holds.

        The caller blocks SIGTERM and asks for it before the run starts, so the
        kernel holds one pending stop. The restore puts the default action back
        for SIGTERM. A drain that ignores the caller's mask takes that stop and
        reports 143, so the run ends the caller's process.
        """
        before = self.scratch_databases()
        returncode, notes, output = self.run_driver()
        after = self.scratch_databases()
        detail = f"preblocked exited {returncode} notes {notes}\\n{output}"
        self.assertIn("caller pending True", notes, detail)
        self.assertIn("1/1 checks passed", output, detail)
        self.assertIn("exit 0", notes, detail)
        self.assertIn("still pending True", notes, detail)
        self.assertIn("handlers restored True", notes, detail)
        self.assertIn("mask restored True", notes, detail)
        self.assertIn("taken SIGTERM", notes, detail)
        self.assertEqual(before, after, detail)
        self.assertNotIn("Traceback", output, detail)
        self.assertEqual(0, returncode, detail)


@unittest.skipUnless(sys.platform == "win32", "a console event is a Windows event")
class ConsoleSectionMaskTests(SectionMaskTests):
    """The class test, with a native console event on Windows."""

    # The base class skips on Windows. This class is the Windows case.
    __unittest_skip__ = False

    def process_options(self: ConsoleSectionMaskTests) -> dict[str, Any]:
        return private_console_options()

    def interpreter(self: ConsoleSectionMaskTests) -> str:
        """The base interpreter, so the console holds one process."""
        return str(getattr(sys, "_base_executable", sys.executable))

    def extra_environment(self: ConsoleSectionMaskTests) -> dict[str, str]:
        paths = [sysconfig.get_paths()["purelib"]]
        existing = os.environ.get("PYTHONPATH")
        if existing:
            paths.append(existing)
        return {
            "CMSC127_PRIVATE_CONSOLE": "1",
            "PYTHONPATH": os.pathsep.join(paths),
        }


CLEANUP_WATCH_SECONDS = 6.0
NORMAL_WATCH_SECONDS = 15.0
# A stall fixture takes one loopback port that the operating system chooses
# when it starts. It skips a busy port, so the database cluster and the browser
# tests stay free. A gate that runs beside another gate reserves its own ports
# through CMSC127_TEST_PORTS, the same variable the launcher tests read.
STALL_LANE = tuple(reserved_ports(6))


def stall_drop(*arguments: object) -> None:
    """Stand in for a drop that the server never answers."""
    time.sleep(60)


def run_with_watch(action: Callable[[], object], seconds: float) -> tuple[bool, float]:
    """Run one action on a thread and report whether it returned in time."""
    finished = threading.Event()

    def target() -> None:
        try:
            action()
        except BaseException:
            pass
        finally:
            finished.set()

    worker = threading.Thread(target=target, daemon=True)
    start = time.monotonic()
    worker.start()
    returned = finished.wait(seconds)
    return returned, time.monotonic() - start


def _stall_message(kind: bytes, payload: bytes) -> bytes:
    return kind + struct.pack("!i", len(payload) + 4) + payload


def _stall_receive(client: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = client.recv(size - len(data))
        if not chunk:
            raise ConnectionError("the client closed the connection")
        data += chunk
    return data


class StallServer:
    """A PostgreSQL stand-in that accepts a connection and stops answering.

    With handshake set, the server answers the startup and then withholds
    every query reply. Without it, the server accepts the TCP connection and
    sends nothing at all. Both cases are what a database does when it stalls.
    """

    def __init__(self: StallServer, handshake: bool) -> None:
        self.handshake = handshake
        self.connections = 0
        self.queries = 0
        self._clients: list[socket.socket] = []
        self._lock = threading.Lock()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        port = 0
        for candidate in STALL_LANE:
            try:
                self._listener.bind(("127.0.0.1", candidate))
            except OSError:
                continue
            port = candidate
            break
        if port == 0:
            self._listener.close()
            raise unittest.SkipTest("Every port in the stall lane is busy.")
        self._listener.listen(8)
        self.port = port
        threading.Thread(target=self._accept, daemon=True).start()

    def url(self: StallServer, database: str = "postgres") -> str:
        return f"postgresql://postgres:password@127.0.0.1:{self.port}/{database}"

    def _accept(self: StallServer) -> None:
        while True:
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            with self._lock:
                self.connections += 1
                self._clients.append(client)
            threading.Thread(
                target=self._serve, args=(client,), daemon=True
            ).start()

    def _serve(self: StallServer, client: socket.socket) -> None:
        try:
            if not self.handshake:
                while client.recv(65536):
                    pass
                return
            length = struct.unpack("!i", _stall_receive(client, 4))[0]
            body = _stall_receive(client, length - 4)
            if length == 8 and struct.unpack("!i", body)[0] == 80877103:
                # SSLRequest: answer "no TLS", then read the startup message.
                client.sendall(b"N")
                length = struct.unpack("!i", _stall_receive(client, 4))[0]
                _stall_receive(client, length - 4)
            client.sendall(
                _stall_message(b"R", struct.pack("!i", 0))
                + _stall_message(b"S", b"server_version\x0018.6\x00")
                + _stall_message(b"S", b"client_encoding\x00UTF8\x00")
                + _stall_message(b"S", b"integer_datetimes\x00on\x00")
                + _stall_message(b"S", b"standard_conforming_strings\x00on\x00")
                + _stall_message(b"K", struct.pack("!ii", 1234, 5678))
                + _stall_message(b"Z", b"I")
            )
            while True:
                kind = _stall_receive(client, 1)
                size = struct.unpack("!i", _stall_receive(client, 4))[0]
                _stall_receive(client, size - 4)
                with self._lock:
                    self.queries += 1
                if kind == b"X":
                    return
        except (ConnectionError, OSError):
            return

    def close(self: StallServer) -> None:
        self._listener.close()
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                client.close()
            except OSError:
                pass

    def __enter__(self: StallServer) -> StallServer:
        return self

    def __exit__(
        self: StallServer,
        kind: type[BaseException] | None,
        value: BaseException | None,
        trace: type[TracebackType] | None,
    ) -> None:
        self.close()


class CleanupDeadlineTests(unittest.TestCase):
    """Every cleanup path ends inside one budget when the database stalls."""

    def test_a_stalled_handshake_ends_inside_the_cleanup_budget(
        self: CleanupDeadlineTests,
    ) -> None:
        """A server that never sends its startup reply must not hold the close."""
        with StallServer(handshake=False) as server:
            guard = runner._StopGuard()  # pyright: ignore[reportPrivateUsage]
            guard.protect(f"cmsc127_race_{uuid4().hex}", server.url())
            returned, elapsed = run_with_watch(
                lambda: guard.console_event(2), CLEANUP_WATCH_SECONDS
            )
            connections = server.connections

        self.assertEqual(1, connections, "the callback opened no connection")
        self.assertTrue(
            returned, f"the callback was still blocked at {elapsed:.2f} s"
        )
        self.assertLess(
            elapsed,
            runner.CLEANUP_SECONDS + 1.0,
            f"the callback took {elapsed:.2f} s on a stalled handshake",
        )

    def test_a_stalled_query_ends_inside_the_cleanup_budget(
        self: CleanupDeadlineTests,
    ) -> None:
        """A server that answers the startup and then stops must not hold it."""
        with StallServer(handshake=True) as server:
            guard = runner._StopGuard()  # pyright: ignore[reportPrivateUsage]
            guard.protect(f"cmsc127_race_{uuid4().hex}", server.url())
            returned, elapsed = run_with_watch(
                lambda: guard.console_event(2), CLEANUP_WATCH_SECONDS
            )
            queries = server.queries

        self.assertGreater(queries, 0, "the callback sent no query")
        self.assertTrue(
            returned, f"the callback was still blocked at {elapsed:.2f} s"
        )
        self.assertLess(
            elapsed,
            runner.CLEANUP_SECONDS + 1.0,
            f"the callback took {elapsed:.2f} s on a stalled query",
        )

    def test_the_cleanup_connection_carries_its_limits(
        self: CleanupDeadlineTests,
    ) -> None:
        """The cleanup connect and its statements carry their own limits."""
        url = "postgresql://postgres:password@127.0.0.1:55460/postgres"
        built = runner._cleanup_conninfo(url)  # pyright: ignore[reportPrivateUsage]

        self.assertIn("connect_timeout=2", built)
        self.assertIn("statement_timeout=2000", built)
        self.assertIn("lock_timeout=2000", built)
        self.assertIn("dbname=postgres", built)

    def test_the_cleanup_connection_keeps_the_url_options(
        self: CleanupDeadlineTests,
    ) -> None:
        """A supplied option survives, and the cleanup limits follow it."""
        url = (
            "postgresql://postgres:password@127.0.0.1:55460/postgres"
            "?options=-c%20default_transaction_read_only%3Doff"
        )
        built = runner._cleanup_conninfo(url)  # pyright: ignore[reportPrivateUsage]
        options = conninfo_to_dict(built)["options"]

        self.assertEqual(
            "-c default_transaction_read_only=off "
            "-c statement_timeout=2000 -c lock_timeout=2000",
            options,
        )

    def test_a_url_without_options_gets_only_the_cleanup_limits(
        self: CleanupDeadlineTests,
    ) -> None:
        """An absent option list must not leave a stray space or a blank value."""
        url = "postgresql://postgres:password@127.0.0.1:55460/postgres"
        built = runner._cleanup_conninfo(url)  # pyright: ignore[reportPrivateUsage]

        self.assertEqual(
            "-c statement_timeout=2000 -c lock_timeout=2000",
            conninfo_to_dict(built)["options"],
        )

    def test_the_cleanup_connection_keeps_the_environment_options(
        self: CleanupDeadlineTests,
    ) -> None:
        """PGOPTIONS reaches the run, so the cleanup keeps it as well."""
        url = "postgresql://postgres:password@127.0.0.1:55460/postgres"
        with patch.dict(
            os.environ, {"PGOPTIONS": "-c default_transaction_read_only=off"}
        ):
            built = runner._cleanup_conninfo(url)  # pyright: ignore[reportPrivateUsage]

        self.assertEqual(
            "-c default_transaction_read_only=off "
            "-c statement_timeout=2000 -c lock_timeout=2000",
            conninfo_to_dict(built)["options"],
        )

    def test_a_url_option_wins_over_the_environment(
        self: CleanupDeadlineTests,
    ) -> None:
        """libpq reads the connection string first, so the cleanup does too."""
        url = (
            "postgresql://postgres:password@127.0.0.1:55460/postgres"
            "?options=-c%20default_transaction_read_only%3Doff"
        )
        with patch.dict(
            os.environ, {"PGOPTIONS": "-c default_transaction_read_only=on"}
        ):
            built = runner._cleanup_conninfo(url)  # pyright: ignore[reportPrivateUsage]

        self.assertEqual(
            "-c default_transaction_read_only=off "
            "-c statement_timeout=2000 -c lock_timeout=2000",
            conninfo_to_dict(built)["options"],
        )

    def test_an_empty_url_option_wins_over_the_environment(
        self: CleanupDeadlineTests,
    ) -> None:
        """An empty option list is a value, so PGOPTIONS does not apply.

        libpq uses PGOPTIONS only when the connection string leaves options
        unset. An explicit empty value is set, so the cleanup connection must
        keep it and add only its own limits.
        """
        url = "postgresql://postgres:password@127.0.0.1:55460/postgres?options="
        with patch.dict(
            os.environ, {"PGOPTIONS": "-c default_transaction_read_only=on"}
        ):
            built = runner._cleanup_conninfo(url)  # pyright: ignore[reportPrivateUsage]

        self.assertEqual(
            "-c statement_timeout=2000 -c lock_timeout=2000",
            conninfo_to_dict(built)["options"],
        )

    def test_an_empty_url_option_without_the_environment_gets_only_the_limits(
        self: CleanupDeadlineTests,
    ) -> None:
        """The empty value must not leave a stray space before the limits."""
        url = "postgresql://postgres:password@127.0.0.1:55460/postgres?options="
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PGOPTIONS", None)
            built = runner._cleanup_conninfo(url)  # pyright: ignore[reportPrivateUsage]

        self.assertEqual(
            "-c statement_timeout=2000 -c lock_timeout=2000",
            conninfo_to_dict(built)["options"],
        )

    def test_a_failed_thread_start_returns_inside_the_budget(
        self: CleanupDeadlineTests,
    ) -> None:
        """A cleanup with no worker thread must skip the drop, not run it late."""
        with StallServer(handshake=True) as server:
            guard = runner._StopGuard()  # pyright: ignore[reportPrivateUsage]
            guard.protect(f"cmsc127_race_{uuid4().hex}", server.url())

            class _FailedThread:
                """Stand in for a thread the interpreter will not start."""

                def __init__(self, **options: object) -> None:
                    pass

                def start(self) -> None:
                    raise RuntimeError("cannot start new thread")

            fallback = SimpleNamespace(Event=threading.Event, Thread=_FailedThread)
            notes = StringIO()
            with patch.object(runner, "threading", fallback), redirect_stderr(notes):
                returned, elapsed = run_with_watch(
                    lambda: guard.console_event(2), CLEANUP_WATCH_SECONDS
                )
            connections = server.connections

        self.assertTrue(
            returned, f"the callback was still blocked at {elapsed:.2f} s"
        )
        self.assertLess(elapsed, 1.0, f"the fallback took {elapsed:.2f} s")
        self.assertEqual(0, connections, "the fallback opened a connection")
        self.assertIn("Cleanup skipped.", notes.getvalue())

    def test_a_failed_thread_start_keeps_the_database_protected(
        self: CleanupDeadlineTests,
    ) -> None:
        """A skipped drop leaves the name, so a later close still tries."""
        name = f"cmsc127_race_{uuid4().hex}"
        guard = runner._StopGuard()  # pyright: ignore[reportPrivateUsage]
        guard.protect(name, "postgresql://127.0.0.1/postgres")

        class _FailedThread:
            """Stand in for a thread the interpreter will not start."""

            def __init__(self, **options: object) -> None:
                pass

            def start(self) -> None:
                raise RuntimeError("cannot start new thread")

        fallback = SimpleNamespace(Event=threading.Event, Thread=_FailedThread)
        with patch.object(runner, "threading", fallback), redirect_stderr(StringIO()):
            dropped = guard.drop(name, cast(Any, Mock()), 0.5)

        self.assertFalse(dropped)
        self.assertEqual(name, guard.console_name)

    def test_the_signal_cleanup_gives_up_at_its_budget(
        self: CleanupDeadlineTests,
    ) -> None:
        """A stalled drop must not hold the SIGTERM cleanup path open."""
        name = f"cmsc127_race_{uuid4().hex}"
        guard = runner._StopGuard()  # pyright: ignore[reportPrivateUsage]
        guard.protect(name, "postgresql://127.0.0.1/postgres")
        with patch.object(
            runner, "_drop_database", side_effect=stall_drop
        ):
            returned, elapsed = run_with_watch(
                lambda: guard.drop(name, cast(Any, Mock())), CLEANUP_WATCH_SECONDS
            )

        self.assertTrue(
            returned, f"the signal cleanup was still blocked at {elapsed:.2f} s"
        )
        self.assertLess(
            elapsed,
            runner.CLEANUP_SECONDS + 1.0,
            f"the signal cleanup took {elapsed:.2f} s on a stalled drop",
        )

    def test_a_stop_during_a_normal_end_keeps_the_cleanup_budget(
        self: CleanupDeadlineTests,
    ) -> None:
        """A stop that arrives during the wait still gets the cleanup budget."""
        name = f"cmsc127_race_{uuid4().hex}"
        guard = runner._StopGuard()  # pyright: ignore[reportPrivateUsage]
        guard.protect(name, "postgresql://127.0.0.1/postgres")

        def stop_soon() -> None:
            time.sleep(0.5)
            guard.pending = signal.SIGTERM

        with patch.object(runner, "_drop_database", side_effect=stall_drop):
            threading.Thread(target=stop_soon, daemon=True).start()
            returned, elapsed = run_with_watch(
                lambda: guard.drop(
                    name, cast(Any, Mock()), runner.NORMAL_DROP_SECONDS
                ),
                NORMAL_WATCH_SECONDS,
            )

        self.assertTrue(
            returned, f"the normal end was still blocked at {elapsed:.2f} s"
        )
        self.assertLess(
            elapsed,
            0.5 + runner.CLEANUP_SECONDS + 1.0,
            f"the stop waited {elapsed:.2f} s on a stalled drop",
        )
        self.assertGreaterEqual(
            elapsed,
            0.5 + runner.CLEANUP_SECONDS - 0.5,
            f"the stop cut the drop short at {elapsed:.2f} s",
        )

    def test_a_stop_before_the_wait_gets_the_budget_from_its_own_time(
        self: CleanupDeadlineTests,
    ) -> None:
        """A stop recorded before the wait still counts from the signal.

        The guard holds a signal that arrives during CREATE. The drop then
        runs after that section, so the wait begins with the signal already
        recorded. The budget starts at the signal. The wait keeps that start.
        """
        name = f"cmsc127_race_{uuid4().hex}"
        guard = runner._StopGuard()  # pyright: ignore[reportPrivateUsage]
        guard.protect(name, "postgresql://127.0.0.1/postgres")
        guard.phase = "cleanup"
        guard.handle(signal.SIGTERM, None)

        with patch.object(runner, "_drop_database", side_effect=stall_drop):
            returned, elapsed = run_with_watch(
                lambda: guard.drop(
                    name, cast(Any, Mock()), runner.NORMAL_DROP_SECONDS
                ),
                NORMAL_WATCH_SECONDS,
            )

        self.assertTrue(
            returned, f"the stop was still blocked at {elapsed:.2f} s"
        )
        self.assertLess(
            elapsed,
            runner.CLEANUP_SECONDS + 1.0,
            f"the stop waited {elapsed:.2f} s on a stalled drop",
        )

    def test_the_self_test_cleanup_gives_up_at_its_budget(
        self: CleanupDeadlineTests,
    ) -> None:
        """The self-test shares the drop of the signal cleanup path."""
        notes = StringIO()
        # The self-test reads a base URL before it creates a database. Every
        # database call here is mocked, so a parseable placeholder is enough.
        # The case must run without a database, not skip like the classes that
        # connect to one.
        with patch.object(
            runner,
            "_create_database",
            return_value=(f"cmsc127_race_{uuid4().hex}", cast(Any, Mock())),
        ), patch.object(runner, "subprocess") as child, patch.object(
            runner, "_drop_database", side_effect=stall_drop
        ), patch.dict(
            os.environ,
            {"CMSC127_DATABASE_URL": "postgresql://postgres@127.0.0.1:55890/postgres"},
        ):
            child.run.return_value.returncode = 0
            with redirect_stderr(notes):
                returned, elapsed = run_with_watch(
                    runner._self_test,  # pyright: ignore[reportPrivateUsage]
                    NORMAL_WATCH_SECONDS,
                )

        self.assertTrue(
            returned, f"the self-test was still blocked at {elapsed:.2f} s"
        )
        self.assertLess(
            elapsed,
            runner.NORMAL_DROP_SECONDS + 1.0,
            f"the self-test took {elapsed:.2f} s on a stalled drop",
        )
        self.assertIn("Cleanup failed.", notes.getvalue())


NORMAL_DROP_DRIVER = '''\
"""Run the runner and hold its real DROP until the parent allows it."""

from __future__ import annotations

from pathlib import Path
import sys
import sysconfig
import time

HARNESS = Path(sys.argv[1])
MARKER = Path(sys.argv[2])
TOKEN = sys.argv[3]
BUDGET = sys.argv[4]
sys.path.insert(0, str(HARNESS))

import runner


class _Token:
    """Stand in for the UUID, so the parent knows the database name."""

    hex = TOKEN


runner.uuid4 = _Token
if BUDGET:
    runner.NORMAL_DROP_SECONDS = float(BUDGET)
plain_execute = runner._execute


def execute(cursor, statement, parameters=None):
    if statement.startswith("DROP DATABASE"):
        MARKER.write_text("ready", encoding="utf-8")
        deadline = time.monotonic() + 30
        while MARKER.read_text(encoding="utf-8") != "drop":
            if time.monotonic() >= deadline:
                raise SystemExit("the parent never released the drop")
            time.sleep(0.01)
    return plain_execute(cursor, statement, parameters)


runner._execute = execute
raise SystemExit(runner.main(sys.argv[5:]))
'''


def application_url(url: str, application: str) -> str:
    """The run's URL with an application name, in either connection shape.

    The suite gets a URI from the test runner and a keyword list from a
    self-test, which hands its child the string that make_conninfo returns. A
    query appended to a keyword list lands in the last setting's value, so the
    name goes in through the connection string parser.
    """
    return make_conninfo(url, application_name=application)


def nested_output(path: Path) -> str:
    """The nested runner's own output, for a failure message.

    A driver writes to a file, not to a pipe. A pipe that fills stops the
    child, and a child that ends early leaves its reason unread. Read the file
    after the child has ended or reached its marker.
    """
    if not path.exists():
        return "the nested runner wrote no output"
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    return text or "the nested runner wrote no output"


class ApplicationUrlTests(unittest.TestCase):
    """The driver's URL keeps every setting in either connection shape."""

    def assert_keeps_settings(self: ApplicationUrlTests, built: str) -> None:
        """The name is added and the rest of the string is unchanged."""
        settings = conninfo_to_dict(built)

        self.assertEqual("probe-name", settings["application_name"])
        self.assertEqual("127.0.0.1", settings["host"])
        self.assertEqual("55890", settings["port"])
        self.assertEqual("c127_t20ga_probe", settings["dbname"])
        self.assertEqual("postgres", settings["user"])

    def test_a_uri_gains_the_application_name(self: ApplicationUrlTests) -> None:
        """A self-test hands its child a keyword list, a test runner a URI."""
        built = application_url(
            "postgresql://postgres@127.0.0.1:55890/c127_t20ga_probe", "probe-name"
        )

        self.assert_keeps_settings(built)

    def test_a_keyword_list_gains_the_application_name(
        self: ApplicationUrlTests,
    ) -> None:
        """A query appended to a keyword list would land in its last value."""
        url = make_conninfo(
            "postgresql://postgres@127.0.0.1:55890/c127_t20ga_probe",
            dbname="c127_t20ga_probe",
        )
        # The case exists for the keyword shape. A URI would pass on either
        # body, because make_conninfo returns it unchanged.
        self.assertNotIn("://", url)
        built = application_url(url, "probe-name")

        self.assert_keeps_settings(built)

    def test_a_supplied_name_is_replaced(self: ApplicationUrlTests) -> None:
        """Two runs that share a base URL still report distinct names."""
        built = application_url(
            "postgresql://postgres@127.0.0.1:55890/c127_t20ga_probe"
            "?application_name=earlier",
            "probe-name",
        )

        self.assert_keeps_settings(built)


@unittest.skipUnless(os.environ.get("CMSC127_DATABASE_URL"), "requires PostgreSQL")
class NormalDropCompletionTests(unittest.TestCase):
    """A normal end drops its database before it reports success."""

    def exists(self: NormalDropCompletionTests, url: str, name: str) -> bool:
        with psycopg.connect(url, autocommit=True) as admin:
            with admin.cursor() as cursor:
                cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
                return cursor.fetchone() is not None

    def drop(self: NormalDropCompletionTests, url: str, name: str) -> None:
        with psycopg.connect(url, autocommit=True) as admin:
            with admin.cursor() as cursor:
                cursor.execute(
                    psycopg_sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        psycopg_sql.Identifier(name)
                    )
                )

    def run_with_lock(
        self: NormalDropCompletionTests, budget: str
    ) -> tuple[int, str, bool, bool]:
        """Run the runner, hold its DROP, then report what happened.

        Return the exit code, the error output, whether the runner had already
        ended while the lock was held, and whether the database was then
        still present.
        """
        url = os.environ["CMSC127_DATABASE_URL"]
        token = uuid4().hex
        name = f"cmsc127_race_{token}"
        failure = ""
        returncode = -1
        present = False
        early = False
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "starter"
            runner_dir = base / "runner"
            make_starter_root(root, runner_dir)
            (root / "run").write_text(
                "#!/usr/bin/env bash\nexit 0\n", encoding="utf-8", newline="\n"
            )
            driver = base / "driver.py"
            driver.write_text(NORMAL_DROP_DRIVER, encoding="utf-8")
            marker = base / "marker.txt"
            log = base / "driver.log"
            sink = log.open("wb")
            try:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        str(driver),
                        str(Path(runner.__file__).resolve().parent),
                        str(marker),
                        token,
                        budget,
                        "1",
                        "--root",
                        str(root),
                        "--runner-dir",
                        str(runner_dir),
                    ],
                    cwd=root,
                    env=os.environ.copy(),
                    stdout=sink,
                    stderr=subprocess.STDOUT,
                )
            finally:
                sink.close()
            try:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    if marker.exists() and marker.read_text(encoding="utf-8") == "ready":
                        break
                    if process.poll() is not None:
                        break
                    time.sleep(0.05)
                self.assertTrue(
                    marker.exists() and marker.read_text(encoding="utf-8") == "ready",
                    "the runner did not reach its real DROP\n" + nested_output(log),
                )
                with psycopg.connect(url) as blocker:
                    with blocker.cursor() as cursor:
                        cursor.execute(
                            "SELECT oid FROM pg_database WHERE datname = %s FOR UPDATE",
                            (name,),
                        )
                    marker.write_text("drop", encoding="utf-8")
                    started = time.monotonic()
                    # Four seconds is past the 3 s cleanup budget that a stop
                    # path uses, so an early end here is the defect.
                    while time.monotonic() - started < 4.0:
                        if process.poll() is not None:
                            break
                        time.sleep(0.05)
                    early = process.poll() is not None
                    present = self.exists(url, name)
                if process.poll() is None:
                    returncode = process.wait(timeout=90)
                else:
                    returncode = process.returncode
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                failure = nested_output(log)
                self.drop(url, name)
        return returncode if returncode is not None else -1, failure, early, present

    def test_a_normal_end_waits_for_a_lock_past_the_cleanup_budget(
        self: NormalDropCompletionTests,
    ) -> None:
        """The drop waits on a four-second lock instead of giving up at 3 s."""
        returncode, failure, early, present = self.run_with_lock("")

        self.assertFalse(early, "the runner gave up before the lock was released")
        self.assertTrue(present, "the lock was not held while the runner waited")
        self.assertEqual(0, returncode, failure)
        self.assertNotIn("Cleanup failed", failure)

    def test_a_normal_end_reports_a_drop_that_never_finishes(
        self: NormalDropCompletionTests,
    ) -> None:
        """A drop that cannot finish in its budget fails the run and says so."""
        returncode, failure, early, present = self.run_with_lock("1.5")

        self.assertTrue(early, "the runner waited past its own budget")
        self.assertTrue(present, "the lock was not held while the runner waited")
        self.assertEqual(1, returncode, failure)
        self.assertIn("Cleanup failed.", failure)


@unittest.skipUnless(os.environ.get("CMSC127_DATABASE_URL"), "requires PostgreSQL")
@unittest.skipIf(
    sys.platform == "win32",
    "Windows ends a process on SIGTERM without running a Python handler",
)
class StoppedDropReportTests(unittest.TestCase):
    """A stop during the normal drop wait names the database and keeps its budget."""

    def exists(self: StoppedDropReportTests, url: str, name: str) -> bool:
        with psycopg.connect(url, autocommit=True) as admin:
            with admin.cursor() as cursor:
                cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
                return cursor.fetchone() is not None

    def drop(self: StoppedDropReportTests, url: str, name: str) -> None:
        with psycopg.connect(url, autocommit=True) as admin:
            with admin.cursor() as cursor:
                cursor.execute(
                    psycopg_sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        psycopg_sql.Identifier(name)
                    )
                )

    def stop_during_lock(
        self: StoppedDropReportTests, budget: str, delay: float
    ) -> tuple[int, str, float, bool]:
        """Hold the real DROP, send SIGTERM after the delay, and report.

        Return the exit code, the output, the seconds from the signal to the
        exit, and whether the database was still present at that moment.
        """
        url = os.environ["CMSC127_DATABASE_URL"]
        token = uuid4().hex
        name = f"cmsc127_race_{token}"
        application = f"c127_t20fl_stop_{token[:8]}"
        base = application_url(url, application)
        output = ""
        returncode = -1
        waited = 0.0
        present = False
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "starter"
            runner_dir = Path(directory) / "runner"
            make_starter_root(root, runner_dir)
            (root / "run").write_text(
                "#!/usr/bin/env bash\nexit 0\n", encoding="utf-8", newline="\n"
            )
            driver = Path(directory) / "driver.py"
            driver.write_text(NORMAL_DROP_DRIVER, encoding="utf-8")
            marker = Path(directory) / "marker.txt"
            log = Path(directory) / "driver.log"
            environment = dict(os.environ, CMSC127_DATABASE_URL=base)
            sink = log.open("wb")
            try:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        str(driver),
                        str(Path(runner.__file__).resolve().parent),
                        str(marker),
                        token,
                        budget,
                        "1",
                        "--root",
                        str(root),
                        "--runner-dir",
                        str(runner_dir),
                    ],
                    cwd=root,
                    env=environment,
                    stdout=sink,
                    stderr=subprocess.STDOUT,
                )
            finally:
                sink.close()
            try:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    if marker.exists() and marker.read_text(encoding="utf-8") == "ready":
                        break
                    if process.poll() is not None:
                        break
                    time.sleep(0.05)
                self.assertTrue(
                    marker.exists() and marker.read_text(encoding="utf-8") == "ready",
                    "the runner did not reach its real DROP\n" + nested_output(log),
                )
                with psycopg.connect(url) as blocker:
                    with blocker.cursor() as cursor:
                        cursor.execute(
                            "SELECT oid FROM pg_database WHERE datname = %s FOR UPDATE",
                            (name,),
                        )
                    marker.write_text("drop", encoding="utf-8")
                    # Wait for the real DROP to block, so the delay counts from
                    # the moment the normal wait began.
                    with psycopg.connect(url, autocommit=True) as watcher:
                        deadline = time.monotonic() + 5
                        row: tuple[object, ...] | None = None
                        while time.monotonic() < deadline:
                            row = watcher.execute(
                                "SELECT wait_event_type FROM pg_stat_activity "
                                "WHERE application_name = %s AND query LIKE %s",
                                (application, "DROP DATABASE%"),
                            ).fetchone()
                            if row and row[0] == "Lock":
                                break
                            time.sleep(0.02)
                        self.assertTrue(
                            row and row[0] == "Lock",
                            f"the DROP never blocked on the lock: {row}",
                        )
                    time.sleep(delay)
                    signal_at = time.monotonic()
                    process.send_signal(signal.SIGTERM)
                    returncode = process.wait(timeout=60)
                    waited = time.monotonic() - signal_at
                    present = self.exists(url, name)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                output = nested_output(log)
                self.drop(url, name)
        return returncode, output, waited, present

    def test_a_stop_during_the_normal_wait_names_the_database(
        self: StoppedDropReportTests,
    ) -> None:
        """The run must say which database it left behind before it exits."""
        returncode, output, _, present = self.stop_during_lock("", 0.5)

        self.assertEqual(128 + signal.SIGTERM, returncode, output)
        self.assertTrue(present, "the lock was not held while the runner waited")
        self.assertIn("Cleanup failed.", output)
        self.assertIn("cmsc127_race_", output)
        self.assertIn("is still present", output)

    def test_a_stop_during_the_normal_wait_keeps_the_cleanup_budget(
        self: StoppedDropReportTests,
    ) -> None:
        """The normal deadline must not cut the stop budget short.

        The normal wait is two seconds and the stop arrives one second in, so
        the normal deadline would end the drop one second after the signal.
        The stop gets its own three seconds from the moment it arrives.
        """
        returncode, output, waited, present = self.stop_during_lock("2.0", 1.0)

        self.assertEqual(128 + signal.SIGTERM, returncode, output)
        self.assertTrue(present, "the lock was not held while the runner waited")
        self.assertGreaterEqual(
            waited,
            runner.CLEANUP_SECONDS - 0.5,
            f"the stop cut the drop short at {waited:.2f} s",
        )
        self.assertLess(
            waited,
            runner.CLEANUP_SECONDS + 1.5,
            f"the stop waited {waited:.2f} s on a stalled drop",
        )


STOPPED_CREATE_DRIVER = '''\
"""Run the runner, hold its CREATE, then hold its real DROP."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import sysconfig
import time

HARNESS = Path(sys.argv[1])
MARKER = Path(sys.argv[2])
TOKEN = sys.argv[3]
SELF_TEST = sys.argv[4] == "self-test"
sys.path.insert(0, str(HARNESS))

import runner


class _Token:
    """Stand in for the UUID, so the parent knows the database name."""

    hex = TOKEN


class _SkipChild:
    """Stand in for subprocess, so the self-test skips its child suite."""

    CompletedProcess = subprocess.CompletedProcess

    @staticmethod
    def run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0] if args else [], 0)


runner.uuid4 = _Token
plain_execute = runner._execute


def execute(cursor, statement, parameters=None):
    if statement.startswith("CREATE DATABASE"):
        # Hold the latched CREATE section open, so the parent can send its
        # first signal while the guard holds it.
        MARKER.write_text("create", encoding="utf-8")
        plain_execute(cursor, "SELECT pg_sleep(1.5)")
    if statement.startswith("DROP DATABASE"):
        MARKER.write_text("ready", encoding="utf-8")
        deadline = time.monotonic() + 30
        while MARKER.read_text(encoding="utf-8") != "drop":
            if time.monotonic() >= deadline:
                raise SystemExit("the parent never released the drop")
            time.sleep(0.01)
    return plain_execute(cursor, statement, parameters)


runner._execute = execute
if SELF_TEST:
    runner.subprocess = _SkipChild
    raise SystemExit(runner._self_test())
raise SystemExit(runner.main(sys.argv[5:]))
'''


@unittest.skipUnless(os.environ.get("CMSC127_DATABASE_URL"), "requires PostgreSQL")
@unittest.skipIf(
    sys.platform == "win32",
    "Windows ends a process on SIGTERM without running a Python handler",
)
class StoppedCreateReportTests(unittest.TestCase):
    """A stop during CREATE keeps its code, its time, and its report."""

    def exists(self: StoppedCreateReportTests, url: str, name: str) -> bool:
        with psycopg.connect(url, autocommit=True) as admin:
            with admin.cursor() as cursor:
                cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
                return cursor.fetchone() is not None

    def drop(self: StoppedCreateReportTests, url: str, name: str) -> None:
        with psycopg.connect(url, autocommit=True) as admin:
            with admin.cursor() as cursor:
                cursor.execute(
                    psycopg_sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        psycopg_sql.Identifier(name)
                    )
                )

    def stop_during_create(
        self: StoppedCreateReportTests, self_test: bool, second: str | None
    ) -> tuple[int, str, float, bool]:
        """Hold CREATE, send SIGTERM, hold the DROP, then report.

        Return the exit code, the output, the seconds from the first signal to
        the exit, and whether the database was still present at that moment.
        """
        url = os.environ["CMSC127_DATABASE_URL"]
        token = uuid4().hex
        name = f"cmsc127_race_{token}"
        application = f"c127_t20fo_create_{token[:8]}"
        base = application_url(url, application)
        output = ""
        returncode = -1
        waited = 0.0
        present = False
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "starter"
            runner_dir = Path(directory) / "runner"
            make_starter_root(root, runner_dir)
            (root / "run").write_text(
                "#!/usr/bin/env bash\nexit 0\n", encoding="utf-8", newline="\n"
            )
            driver = Path(directory) / "driver.py"
            driver.write_text(STOPPED_CREATE_DRIVER, encoding="utf-8")
            marker = Path(directory) / "marker.txt"
            environment = dict(os.environ, CMSC127_DATABASE_URL=base)
            arguments = (
                ["self-test"]
                if self_test
                else ["run", "1", "--root", str(root), "--runner-dir", str(runner_dir)]
            )
            log = Path(directory) / "driver.log"
            sink = log.open("wb")
            try:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        str(driver),
                        str(Path(runner.__file__).resolve().parent),
                        str(marker),
                        token,
                        *arguments,
                    ],
                    cwd=root,
                    env=environment,
                    stdout=sink,
                    stderr=subprocess.STDOUT,
                )
            finally:
                sink.close()
            try:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    if marker.exists() and marker.read_text(encoding="utf-8") == "create":
                        break
                    if process.poll() is not None:
                        break
                    time.sleep(0.02)
                self.assertTrue(
                    marker.exists() and marker.read_text(encoding="utf-8") == "create",
                    "the runner did not reach its latched CREATE\n" + nested_output(log),
                )
                signal_at = time.monotonic()
                process.send_signal(signal.SIGTERM)
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    if marker.exists() and marker.read_text(encoding="utf-8") == "ready":
                        break
                    if process.poll() is not None:
                        break
                    time.sleep(0.02)
                self.assertTrue(
                    marker.exists() and marker.read_text(encoding="utf-8") == "ready",
                    "the runner did not reach its real DROP\n" + nested_output(log),
                )
                with psycopg.connect(url) as blocker:
                    with blocker.cursor() as cursor:
                        cursor.execute(
                            "SELECT oid FROM pg_database WHERE datname = %s FOR UPDATE",
                            (name,),
                        )
                    marker.write_text("drop", encoding="utf-8")
                    with psycopg.connect(url, autocommit=True) as watcher:
                        deadline = time.monotonic() + 5
                        row: tuple[object, ...] | None = None
                        while time.monotonic() < deadline:
                            row = watcher.execute(
                                "SELECT wait_event_type FROM pg_stat_activity "
                                "WHERE application_name = %s AND query LIKE %s",
                                (application, "DROP DATABASE%"),
                            ).fetchone()
                            if row and row[0] == "Lock":
                                break
                            time.sleep(0.02)
                        self.assertTrue(
                            row and row[0] == "Lock",
                            f"the DROP never blocked on the lock: {row}",
                        )
                    if second is not None:
                        time.sleep(0.3)
                        process.send_signal(getattr(signal, second))
                    returncode = process.wait(timeout=60)
                    waited = time.monotonic() - signal_at
                    present = self.exists(url, name)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                output = nested_output(log)
                self.drop(url, name)
        return returncode, output, waited, present

    def assert_stop_keeps_its_budget(
        self: StoppedCreateReportTests,
        returncode: int,
        output: str,
        waited: float,
        present: bool,
    ) -> None:
        """The first signal sets the exit code and starts the stop budget."""
        self.assertEqual(128 + signal.SIGTERM, returncode, output)
        self.assertTrue(present, "the lock was not held while the runner waited")
        self.assertIn("Cleanup failed.", output)
        self.assertIn("cmsc127_race_", output)
        self.assertIn("is still present", output)
        self.assertGreaterEqual(
            waited,
            runner.CLEANUP_SECONDS - 0.5,
            f"the stop cut the drop short at {waited:.2f} s",
        )
        self.assertLess(
            waited,
            runner.CLEANUP_SECONDS + 1.5,
            f"the stop waited {waited:.2f} s on a stalled drop",
        )

    def test_a_stop_during_create_keeps_the_cleanup_budget(
        self: StoppedCreateReportTests,
    ) -> None:
        """A signal held during CREATE must not hand the drop the normal wait."""
        self.assert_stop_keeps_its_budget(*self.stop_during_create(False, None))

    def test_a_stop_during_create_keeps_the_first_code(
        self: StoppedCreateReportTests,
    ) -> None:
        """A second signal during the drop must not replace the first code."""
        self.assert_stop_keeps_its_budget(*self.stop_during_create(False, "SIGHUP"))

    def test_the_self_test_keeps_the_cleanup_budget_after_a_stop_in_create(
        self: StoppedCreateReportTests,
    ) -> None:
        """The self-test shares the guard, so it shares the stop budget."""
        self.assert_stop_keeps_its_budget(*self.stop_during_create(True, None))

    def test_the_self_test_keeps_the_first_code_after_a_stop_in_create(
        self: StoppedCreateReportTests,
    ) -> None:
        """The self-test keeps the first signal through its own cleanup."""
        self.assert_stop_keeps_its_budget(*self.stop_during_create(True, "SIGHUP"))


CONSOLE_DRIVER = '''\
"""Start the runner in a new console and report that console's window."""

from __future__ import annotations

import ctypes
from pathlib import Path
import sys
import sysconfig

HARNESS = Path(sys.argv[1])
MARKER = Path(sys.argv[2])
sys.path.insert(0, str(HARNESS))

KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
KERNEL32.GetConsoleWindow.restype = ctypes.c_void_p
MARKER.write_text(str(int(KERNEL32.GetConsoleWindow() or 0)), encoding="utf-8")

import runner

raise SystemExit(runner.main(sys.argv[3:]))
'''


@unittest.skipUnless(
    sys.platform == "win32", "a console window close is a Windows event"
)
@unittest.skipUnless(os.environ.get("CMSC127_DATABASE_URL"), "requires PostgreSQL")
class ConsoleCloseCleanupTests(unittest.TestCase):
    """A closed console window leaves no race database behind."""

    def race_databases(self: ConsoleCloseCleanupTests, url: str) -> set[str]:
        with psycopg.connect(url, autocommit=True) as admin:
            with admin.cursor() as cursor:
                cursor.execute(
                    "SELECT datname FROM pg_database WHERE datname LIKE 'cmsc127_race_%'"
                )
                return {str(row[0]) for row in cursor.fetchall()}

    def drop(
        self: ConsoleCloseCleanupTests, url: str, name: str
    ) -> None:
        with psycopg.connect(url, autocommit=True) as admin:
            with admin.cursor() as cursor:
                cursor.execute(
                    psycopg_sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        psycopg_sql.Identifier(name)
                    )
                )

    def test_a_closed_console_window_drops_the_race_database(
        self: ConsoleCloseCleanupTests,
    ) -> None:
        url = os.environ["CMSC127_DATABASE_URL"]
        before = self.race_databases(url)
        output = ""
        window = 0
        created: set[str] = set()
        leftover: set[str] = set()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            base = Path(directory)
            root = base / "starter"
            runner_dir = base / "runner"
            make_starter_root(root, runner_dir)
            (root / "run").write_text(
                "#!/usr/bin/env bash\nset -eu\nsleep 120\n",
                encoding="utf-8",
                newline="\n",
            )
            driver = base / "console-driver.py"
            driver.write_text(CONSOLE_DRIVER, encoding="utf-8")
            marker = base / "window.txt"
            # The Windows-only names stay out of the type checker's way, so the
            # suite type-checks on Linux and macOS too. The class skips them.
            windows = cast(Any, subprocess)
            startup = windows.STARTUPINFO()
            startup.dwFlags |= windows.STARTF_USESHOWWINDOW
            startup.wShowWindow = windows.SW_HIDE
            environment = os.environ.copy()
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(driver),
                    str(Path(runner.__file__).resolve().parent),
                    str(marker),
                    "1",
                    "--root",
                    str(root),
                    "--runner-dir",
                    str(runner_dir),
                ],
                cwd=root,
                env=environment,
                startupinfo=startup,
                creationflags=0x00000010,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            try:
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    if marker.exists():
                        window = int(marker.read_text(encoding="utf-8").strip() or "0")
                    created = self.race_databases(url) - before
                    if created and window:
                        break
                    if process.poll() is not None:
                        break
                    time.sleep(0.1)
                self.assertTrue(created, "the run made no race database")
                self.assertTrue(window, "the run reported no console window")
                post = cast(Any, ctypes).WinDLL("user32", use_last_error=True).PostMessageW
                post.argtypes = [
                    ctypes.c_void_p,
                    ctypes.c_uint,
                    ctypes.c_size_t,
                    ctypes.c_ssize_t,
                ]
                post.restype = ctypes.c_int
                posted = post(window, 0x0010, 0, 0)
                self.assertTrue(posted, "WM_CLOSE did not reach the console window")
                process.wait(timeout=90)
                deadline = time.monotonic() + 15
                leftover = self.race_databases(url) & created
                while leftover and time.monotonic() < deadline:
                    time.sleep(0.2)
                    leftover = self.race_databases(url) & created
            finally:
                if process.poll() is None:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                        capture_output=True,
                        check=False,
                    )
                    process.wait()
                if process.stdout is not None:
                    output = process.stdout.read()
                    process.stdout.close()
                for leftover_name in created:
                    self.drop(url, leftover_name)

        self.assertFalse(
            leftover, f"the race database outlived the closed console\n{output}"
        )


if __name__ == "__main__":
    unittest.main()
