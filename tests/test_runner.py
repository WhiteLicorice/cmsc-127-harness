from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from contextlib import redirect_stdout
import hashlib
from io import StringIO
from types import TracebackType
from typing import Callable, Protocol, cast
from unittest.mock import MagicMock, Mock, patch
from uuid import uuid4

import psycopg
from psycopg import sql as psycopg_sql
import runner

from runner import compare_dataset, load_manifest, main, select_cases


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
        with (
            patch.object(runner, "uuid4", return_value=Mock(hex="0123456789abcdef")),
            patch.object(runner.psycopg, "connect", return_value=admin),
        ):
            name, connection = runner._create_database(  # pyright: ignore[reportPrivateUsage]
                "postgresql://postgres@127.0.0.1/postgres"
            )

        self.assertEqual("cmsc127_race_0123456789abcdef", name)
        self.assertIs(admin, connection)
        cursor.execute.assert_called_once_with(f'CREATE DATABASE "{name}"', None)


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


if __name__ == "__main__":
    unittest.main()
