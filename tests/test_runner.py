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
from psycopg.conninfo import conninfo_to_dict
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

    def test_scratch_database_stays_latched_until_the_caller_unlatches(
        self: ScratchDatabaseNameTests,
    ) -> None:
        admin = MagicMock()
        guard = runner._StopGuard()  # pyright: ignore[reportPrivateUsage]
        with (
            patch.object(runner, "uuid4", return_value=Mock(hex="0123456789abcdef")),
            patch.object(runner.psycopg, "connect", return_value=admin),
        ):
            name, _ = runner._create_database(  # pyright: ignore[reportPrivateUsage]
                "postgresql://postgres@127.0.0.1/postgres", guard
            )

        self.assertTrue(guard.latched)
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
            with psycopg.connect(database_url, autocommit=True) as admin:
                with admin.cursor() as cursor:
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

import os
from pathlib import Path
import signal
import subprocess
import sys
import threading

HARNESS = Path(sys.argv[1])
MARKER = Path(sys.argv[2])
POINT = sys.argv[3]
TOKEN = sys.argv[4]
sys.path.insert(0, str(HARNESS))

import psycopg
import runner

SENT = {"child-run": 0}
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
# The console stop that this run delivers, as the name of a Python signal.
CONSOLE_SIGNAL = os.environ.get("CMSC127_CONSOLE_SIGNAL", "")


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


runner.uuid4 = _Token
plain_execute = runner._execute
plain_run_pair = runner._run_pair
plain_drop_database = runner._drop_database
plain_report_cleanup_failure = runner._report_cleanup_failure
plain_restore = runner._StopGuard.restore


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


def restore(guard):
    """Send the first stop signal at the boundary where the handlers change.

    The guard has already made its last check for a held signal when this
    runs, so a signal here is held and never replayed unless the guard checks
    once more after it puts the old handlers back.
    """
    if POINT in RESTORE_POINTS or POINT in RESTORE_FAIL_POINTS:
        note("before restore")
        deliver()
    return plain_restore(guard)


runner._execute = execute
runner._run_pair = run_pair
runner._drop_database = drop_database
runner._report_cleanup_failure = report_cleanup_failure
runner._StopGuard.restore = restore


class _SkipChild:
    """Stand in for subprocess, so the self-test skips its child suite."""

    CompletedProcess = subprocess.CompletedProcess

    @staticmethod
    def run(*args, **kwargs):
        if POINT == "selftest-report":
            note("child-run")
            stop()
        return subprocess.CompletedProcess(args[0] if args else [], 0)


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
raise SystemExit(code)
'''


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
                        os.kill(process.pid, signal.CTRL_BREAK_EVENT)
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


class StopSignalHandlerTests(unittest.TestCase):
    """The stop handlers go in before the create and come off after the drop."""

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

    def test_unlatched_stop_signal_ends_the_run_through_system_exit(
        self: StopSignalHandlerTests,
    ) -> None:
        guard = runner._StopGuard()  # pyright: ignore[reportPrivateUsage]

        with self.assertRaises(SystemExit) as raised:
            guard.handle(signal.SIGTERM, None)

        self.assertEqual(128 + signal.SIGTERM, raised.exception.code)

    def test_latched_stop_signal_waits_for_cleanup(
        self: StopSignalHandlerTests,
    ) -> None:
        guard = runner._StopGuard()  # pyright: ignore[reportPrivateUsage]
        guard.latch()

        guard.handle(signal.SIGTERM, None)
        guard.handle(signal.SIGTERM, None)

        self.assertEqual(signal.SIGTERM, guard.pending)

    def test_pending_stop_signal_ends_the_run_after_cleanup(
        self: StopSignalHandlerTests,
    ) -> None:
        """The signal stays recorded, so a later one cannot replace it."""
        guard = runner._StopGuard()  # pyright: ignore[reportPrivateUsage]
        guard.latch()
        guard.handle(signal.SIGTERM, None)
        guard.unlatch()

        with self.assertRaises(SystemExit) as raised:
            guard.stop_if_pending()

        self.assertEqual(128 + signal.SIGTERM, raised.exception.code)
        self.assertEqual(signal.SIGTERM, guard.pending)

    @unittest.skipIf(sys.platform == "win32", "Windows has no SIGHUP")
    def test_a_later_latched_signal_keeps_the_first_exit_code(
        self: StopSignalHandlerTests,
    ) -> None:
        """The first signal names the exit, through cleanup and after it."""
        guard = runner._StopGuard()  # pyright: ignore[reportPrivateUsage]
        first = runner._StopGuard()  # pyright: ignore[reportPrivateUsage]

        with self.assertRaises(SystemExit) as raised:
            first.handle(signal.SIGTERM, None)
        self.assertEqual(128 + signal.SIGTERM, raised.exception.code)

        guard.latch()
        guard.handle(signal.SIGTERM, None)
        guard.handle(cast(int, getattr(signal, "SIGHUP")), None)

        self.assertEqual(signal.SIGTERM, guard.pending)
        with self.assertRaises(SystemExit) as stopped:
            guard.stop_if_pending()
        self.assertEqual(128 + signal.SIGTERM, stopped.exception.code)


CLEANUP_WATCH_SECONDS = 6.0
NORMAL_WATCH_SECONDS = 15.0
# A stall fixture takes one loopback port from this lane. It skips a busy port,
# so the database cluster and the browser tests stay free. A gate that runs
# beside another gate reserves its own ports through CMSC127_TEST_PORTS, the
# same variable the launcher tests read, so no gate's ports are fixed here.
# Without that variable the lane is the one CI uses.
STALL_DEFAULT_LANE = range(55721, 55727)
STALL_LANE = tuple(reserved_ports(len(STALL_DEFAULT_LANE), STALL_DEFAULT_LANE))


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
        guard.latch()
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
        with patch.object(
            runner,
            "_create_database",
            return_value=(f"cmsc127_race_{uuid4().hex}", cast(Any, Mock())),
        ), patch.object(runner, "subprocess") as child, patch.object(
            runner, "_drop_database", side_effect=stall_drop
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
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
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
                    "the runner did not reach its real DROP",
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
                if process.stderr is not None:
                    failure = process.stderr.read()
                    process.stderr.close()
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
        base = url + ("&" if "?" in url else "?") + "application_name=" + application
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
            environment = dict(os.environ, CMSC127_DATABASE_URL=base)
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
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
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
                    "the runner did not reach its real DROP",
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
                if process.stdout is not None:
                    output = process.stdout.read()
                    process.stdout.close()
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
        base = url + ("&" if "?" in url else "?") + "application_name=" + application
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
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
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
                    "the runner did not reach its latched CREATE",
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
                    "the runner did not reach its real DROP",
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
                if process.stdout is not None:
                    output = process.stdout.read()
                    process.stdout.close()
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
