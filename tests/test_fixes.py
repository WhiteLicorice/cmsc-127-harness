from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from types import TracebackType
from typing import Callable, Protocol, cast
from unittest import mock
from uuid import uuid4

import psycopg
import runner

from runner import load_manifest, main


class _TestCursor(Protocol):
    def execute(
        self: _TestCursor,
        query: str,
        parameters: Sequence[object] | None = None,
    ) -> object: ...

    def fetchall(self: _TestCursor) -> list[tuple[object, ...]]: ...

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

check_index = cast(
    Callable[[Path, object, Mapping[str, object], set[tuple[str, str]]], list[str]],
    getattr(runner, "_check_index"),
)
check_normal_form = cast(
    Callable[[object, Mapping[str, object]], list[str]],
    getattr(runner, "_check_normal_form"),
)


def check_pyright(
    root: Path, command: Sequence[str] | None
) -> tuple[list[str], dict[str, object]]:
    function = cast(
        Callable[[Path, Sequence[str] | None], tuple[list[str], dict[str, object]]],
        getattr(runner, "_check_pyright"),
    )
    return function(root, command)

contribution_table = cast(
    Callable[[Path], str], getattr(runner, "_contribution_table")
)
extract_erd = cast(Callable[[str], str], getattr(runner, "_extract_erd"))
print_results = cast(
    Callable[[Sequence[Mapping[str, object]], int], int],
    getattr(runner, "_print_results"),
)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def git(root: Path, *arguments: str, env: Mapping[str, str] | None = None) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=dict(os.environ) | dict(env or {}),
    )


def make_pair(root: Path, *, case_id: str = "lab1/one") -> None:
    write(
        root / "cases" / "manifest.json",
        json.dumps(
            {
                "version": 1,
                "cases": [
                    {
                        "id": case_id,
                        "stage": 1,
                        "kind": "query",
                        "file": "cases/lab1/one.sql",
                    }
                ],
            }
        ),
    )
    write(root / "cases" / "lab1" / "one.sql", "SELECT 1 AS value;\n")
    write(
        root / "cases" / "lab1" / "one.expected",
        '{"columns":["value"],"rows":[[1]]}\n',
    )
    write(root / "migrations" / "0001_create_item.sql", "SELECT 1;\n")
    write(root / "load.sql", "")
    write(root / "run", "#!/usr/bin/env bash\nset -eu\nexit 0\n")


class FormFaultTests(unittest.TestCase):
    def test_state_body_form_fault_reports_file_line_and_column(self: FormFaultTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(
                root / "cases" / "manifest.json",
                json.dumps(
                    {
                        "version": 1,
                        "cases": [
                            {
                                "id": "bad",
                                "stage": 1,
                                "kind": "state",
                                "file": "cases/bad.json",
                            }
                        ],
                    }
                ),
            )
            write(
                root / "cases" / "bad.json",
                '{\n  "operation": 127,\n  "check_sql": "SELECT 127"\n}\n',
            )
            write(root / "cases" / "bad.expected", '{"columns":["value"],"rows":[[127]]}\n')

            with self.assertRaises(ValueError) as raised:
                load_manifest(root)

            message = str(raised.exception)
            self.assertIn("bad.json: line 2, column 16", message)
            self.assertIn("operation must be a string", message)

    def test_race_body_form_fault_reports_its_location(self: FormFaultTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(
                root / "cases" / "manifest.json",
                json.dumps(
                    {
                        "version": 1,
                        "cases": [
                            {
                                "id": "bad",
                                "stage": 1,
                                "kind": "race",
                                "file": "cases/bad.json",
                            }
                        ],
                    }
                ),
            )
            write(
                root / "cases" / "bad.json",
                '{\n  "steps": [{"session": "c", "sql": "SELECT 1"}],\n  "check_sql": "SELECT 1"\n}\n',
            )
            write(root / "cases" / "bad.expected", '{"columns":["value"],"rows":[[1]]}\n')

            with self.assertRaises(ValueError) as raised:
                load_manifest(root)

            message = str(raised.exception)
            self.assertIn("bad.json: line 2, column 25", message)
            self.assertIn("session must be a or b", message)

    def test_expected_row_shape_reports_its_location(self: FormFaultTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_pair(root)
            write(
                root / "cases" / "lab1" / "one.expected",
                '{\n  "columns": ["a", "b"],\n  "rows": [[1]]\n}\n',
            )

            with self.assertRaises(ValueError) as raised:
                load_manifest(root)

            message = str(raised.exception)
            self.assertIn("one.expected: line 3, column 12", message)
            self.assertIn("row 1 must have 2 values", message)

    def test_invariant_required_field_reports_its_location(self: FormFaultTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_pair(root)
            write(
                root / "cases" / "manifest.json",
                json.dumps(
                    {
                        "version": 1,
                        "cases": [
                            {
                                "id": "lab1/one",
                                "stage": 1,
                                "kind": "query",
                                "file": "cases/lab1/one.sql",
                            }
                        ],
                        "invariants": [
                            {"id": "normal-form", "stage": 1, "kind": "normal_form"}
                        ],
                    }
                ),
            )

            with self.assertRaises(ValueError) as raised:
                load_manifest(root)

            message = str(raised.exception)
            self.assertIn("manifest.json: line 1, column 1", message)
            self.assertIn("normal_form needs a mapping object", message)

    def test_form_lint_needs_no_database_and_hides_answers(self: FormFaultTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_pair(root)
            write(
                root / "cases" / "lab1" / "one.expected",
                '{"columns":["value"],"rows":[[424242]]}\n',
            )
            output = StringIO()
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("CMSC127_DATABASE_URL", None)
                with redirect_stdout(output):
                    exit_code = main(["--form-lint", "--root", str(root)])

            self.assertEqual(0, exit_code, output.getvalue())
            self.assertIn("Form is valid.", output.getvalue())
            self.assertNotIn("424242", output.getvalue())

    def test_form_fault_stops_before_setup(self: FormFaultTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(
                root / "cases" / "manifest.json",
                json.dumps(
                    {
                        "version": 1,
                        "cases": [
                            {
                                "id": "bad",
                                "stage": 1,
                                "kind": "state",
                                "file": "cases/bad.json",
                            }
                        ],
                    }
                ),
            )
            write(root / "cases" / "bad.json", '{"operation": 127, "check_sql": "SELECT 1"}\n')
            write(root / "cases" / "bad.expected", '{"columns":["value"],"rows":[[1]]}\n')
            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(["1", "--root", str(root)])

            self.assertEqual(2, exit_code, output.getvalue())
            self.assertIn("== form fault ==", output.getvalue())
            self.assertFalse((root / ".cmsc127" / "last-run.json").exists())

    def assert_malformed_field_fault(
        self: FormFaultTests,
        root: Path,
        location: str,
        message: str,
    ) -> None:
        output = StringIO()
        errors = StringIO()
        create_database = mock.Mock(side_effect=AssertionError("database was created"))
        exit_code = -1
        with mock.patch.object(runner, "_create_database", create_database):
            with mock.patch.dict(
                os.environ,
                {"CMSC127_DATABASE_URL": "postgresql://postgres@127.0.0.1:1/postgres"},
            ):
                with redirect_stdout(output), redirect_stderr(errors):
                    try:
                        exit_code = main(["1", "--root", str(root)])
                    except BaseException as error:
                        self.fail(f"runner raised {error!r} instead of a form fault")

        self.assertEqual(2, exit_code, output.getvalue())
        self.assertIn("== form fault ==", output.getvalue())
        self.assertIn(location, output.getvalue())
        self.assertIn(message, output.getvalue())
        self.assertNotIn("Traceback", output.getvalue() + errors.getvalue())
        create_database.assert_not_called()
        self.assertFalse((root / ".cmsc127" / "last-run.json").exists())

    def test_race_session_array_is_a_form_fault(self: FormFaultTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(
                root / "cases" / "manifest.json",
                json.dumps(
                    {
                        "version": 1,
                        "cases": [
                            {
                                "id": "bad",
                                "stage": 1,
                                "kind": "race",
                                "file": "cases/bad.json",
                            }
                        ],
                    }
                ),
            )
            write(
                root / "cases" / "bad.json",
                '{\n  "steps": [{"session": [], "sql": "SELECT 1"}],\n'
                '  "check_sql": "SELECT 1"\n}\n',
            )
            write(root / "cases" / "bad.expected", '{"columns":["value"],"rows":[[1]]}\n')

            self.assert_malformed_field_fault(
                root,
                "bad.json: line 2, column 25",
                "session must be a or b",
            )

    def test_race_session_object_is_a_form_fault(self: FormFaultTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(
                root / "cases" / "manifest.json",
                json.dumps(
                    {
                        "version": 1,
                        "cases": [
                            {
                                "id": "bad",
                                "stage": 1,
                                "kind": "race",
                                "file": "cases/bad.json",
                            }
                        ],
                    }
                ),
            )
            write(
                root / "cases" / "bad.json",
                '{\n  "steps": [{"session": {}, "sql": "SELECT 1"}],\n'
                '  "check_sql": "SELECT 1"\n}\n',
            )
            write(root / "cases" / "bad.expected", '{"columns":["value"],"rows":[[1]]}\n')

            self.assert_malformed_field_fault(
                root,
                "bad.json: line 2, column 25",
                "session must be a or b",
            )

    def test_feature_array_value_is_a_form_fault(self: FormFaultTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_pair(root)
            write(
                root / "cases" / "manifest.json",
                '{\n'
                '  "version": 1,\n'
                '  "cases": [\n'
                "    {\n"
                '      "id": "lab1/one",\n'
                '      "stage": 1,\n'
                '      "kind": "query",\n'
                '      "file": "cases/lab1/one.sql"\n'
                "    }\n"
                "  ],\n"
                '  "invariants": [\n'
                "    {\n"
                '      "id": "feature",\n'
                '      "stage": 1,\n'
                '      "kind": "feature",\n'
                '      "schema": "public",\n'
                '      "view": "v",\n'
                '      "features": [[]]\n'
                "    }\n"
                "  ]\n"
                "}\n",
            )

            self.assert_malformed_field_fault(
                root,
                "manifest.json: line 18, column 20",
                "unsupported SQL feature",
            )

    def test_feature_object_value_is_a_form_fault(self: FormFaultTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_pair(root)
            write(
                root / "cases" / "manifest.json",
                '{\n'
                '  "version": 1,\n'
                '  "cases": [\n'
                "    {\n"
                '      "id": "lab1/one",\n'
                '      "stage": 1,\n'
                '      "kind": "query",\n'
                '      "file": "cases/lab1/one.sql"\n'
                "    }\n"
                "  ],\n"
                '  "invariants": [\n'
                "    {\n"
                '      "id": "feature",\n'
                '      "stage": 1,\n'
                '      "kind": "feature",\n'
                '      "schema": "public",\n'
                '      "view": "v",\n'
                '      "features": [{}]\n'
                "    }\n"
                "  ]\n"
                "}\n",
            )

            self.assert_malformed_field_fault(
                root,
                "manifest.json: line 18, column 20",
                "unsupported SQL feature",
            )

    def test_case_kind_array_is_a_form_fault(self: FormFaultTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(
                root / "cases" / "manifest.json",
                '{\n'
                '  "version": 1,\n'
                '  "cases": [\n'
                "    {\n"
                '      "id": "bad",\n'
                '      "stage": 1,\n'
                '      "kind": [],\n'
                '      "file": "cases/bad.sql"\n'
                "    }\n"
                "  ]\n"
                "}\n",
            )

            self.assert_malformed_field_fault(
                root,
                "manifest.json: line 7, column 15",
                "unsupported kind",
            )

    def test_invariant_kind_object_is_a_form_fault(self: FormFaultTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_pair(root)
            write(
                root / "cases" / "manifest.json",
                '{\n'
                '  "version": 1,\n'
                '  "cases": [\n'
                "    {\n"
                '      "id": "lab1/one",\n'
                '      "stage": 1,\n'
                '      "kind": "query",\n'
                '      "file": "cases/lab1/one.sql"\n'
                "    }\n"
                "  ],\n"
                '  "invariants": [\n'
                "    {\n"
                '      "id": "feature",\n'
                '      "stage": 1,\n'
                '      "kind": {},\n'
                '      "schema": "public",\n'
                '      "view": "v",\n'
                '      "features": ["left_join"]\n'
                "    }\n"
                "  ]\n"
                "}\n",
            )

            self.assert_malformed_field_fault(
                root,
                "manifest.json: line 15, column 15",
                "unsupported kind",
            )


class PairManifestTests(unittest.TestCase):
    def test_pair_manifest_adds_a_case_through_the_full_runner(self: PairManifestTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "pair"
            runner_dir = Path(directory) / "runner"
            root.mkdir()
            runner_dir.mkdir()
            make_pair(root)
            write(
                root / "cases" / "pair" / "own.sql",
                "SELECT 127 AS value;\n",
            )
            write(
                root / "cases" / "pair" / "own.expected",
                '{"columns":["value"],"rows":[[127]]}\n',
            )
            write(
                root / "cases" / "pair" / "manifest.json",
                json.dumps(
                    {
                        "version": 1,
                        "cases": [
                            {
                                "id": "pair/own",
                                "stage": 1,
                                "kind": "query",
                                "file": "cases/pair/own.sql",
                            }
                        ],
                    }
                ),
            )
            write(
                root / "cases" / "manifest.json",
                json.dumps(
                    {
                        "version": 1,
                        "cases": [
                            {
                                "id": "lab1/one",
                                "stage": 1,
                                "kind": "query",
                                "file": "cases/lab1/one.sql",
                            }
                        ],
                        "invariants": [
                            {"id": "protected-files", "stage": 1, "kind": "protected"}
                        ],
                    }
                ),
            )
            git(root, "init", "--quiet", "-b", "main")
            git(root, "add", "--all")
            git(
                root,
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "--quiet",
                "-m",
                "published",
            )
            protected_files = {
                relative: hashlib.sha256(
                    (root / relative).read_bytes().replace(b"\r\n", b"\n")
                ).hexdigest()
                for relative in (
                    "cases/manifest.json",
                    "cases/lab1/one.sql",
                    "cases/lab1/one.expected",
                    "run",
                )
            }
            write(
                runner_dir / "protected.json",
                json.dumps(
                    {
                        "files": protected_files,
                        "allowed_paths": ["cases/pair", "migrations", "load.sql", "reports"],
                    }
                ),
            )
            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(
                    ["1", "--root", str(root), "--runner-dir", str(runner_dir)]
                )

            self.assertEqual(0, exit_code, output.getvalue())
            self.assertIn("PASS pair/own", output.getvalue())
            self.assertIn("PASS protected-files", output.getvalue())
            self.assertIn("3/3 checks passed", output.getvalue())

    def test_pair_manifest_rejects_a_file_outside_cases_pair(self: PairManifestTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_pair(root)
            write(root / "cases" / "lab1" / "extra.sql", "SELECT 2;\n")
            write(root / "cases" / "lab1" / "extra.expected", '{"columns":["value"],"rows":[[2]]}\n')
            write(
                root / "cases" / "pair" / "manifest.json",
                json.dumps(
                    {
                        "version": 1,
                        "cases": [
                            {
                                "id": "pair/extra",
                                "stage": 1,
                                "kind": "query",
                                "file": "cases/lab1/extra.sql",
                            }
                        ],
                    }
                ),
            )

            with self.assertRaisesRegex(ValueError, "inside cases/pair/"):
                load_manifest(root)


class ErdExtractionTests(unittest.TestCase):
    def test_extract_erd_reads_the_starter_readme(self: ErdExtractionTests) -> None:
        readme = (Path(__file__).parent / "fixtures" / "starter-readme.md").read_text(
            encoding="utf-8"
        )

        erd = extract_erd(readme)

        self.assertIn("erDiagram", erd)
        self.assertNotIn("In Git Bash", erd)
        self.assertIn("observer ||--o{ session", erd)

    def test_extract_erd_does_not_treat_a_closing_fence_as_an_opening(self: ErdExtractionTests) -> None:
        text = (
            "# Example\n\n"
            "```bash\n"
            "source .venv/bin/activate\n"
            "```\n\n"
            "Prose between the blocks.\n\n"
            "```mermaid\n"
            "erDiagram\n"
            "    item {\n"
            "        int id PK\n"
            "    }\n"
            "```\n"
        )

        erd = extract_erd(text)

        self.assertIn("erDiagram", erd)
        self.assertNotIn("source .venv", erd)
        self.assertNotIn("Prose between", erd)


class PyrightTests(unittest.TestCase):
    def test_pyright_ignore_directive_is_rejected(self: PyrightTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(root / "reports" / "bad.py", 'value: int = "bad"  # pyright: ignore\n')

            problems, summary = check_pyright(root, None)

            self.assertTrue(any("reports/bad.py:1" in problem for problem in problems))
            self.assertTrue(any("pyright: ignore" in problem for problem in problems))
            self.assertEqual("fail", summary["status"])

    def test_pyright_missing_reports_the_install_repair(self: PyrightTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(
                root / "pyrightconfig.json",
                '{"typeCheckingMode": "strict"}\n',
            )
            empty = Path(directory) / "empty-bin"
            empty.mkdir()
            with mock.patch.dict(os.environ, {"PATH": str(empty)}, clear=False):
                problems, summary = check_pyright(root, None)

            self.assertTrue(
                any("python -m pip install -r requirements.txt" in problem for problem in problems)
            )
            self.assertEqual("missing", summary["status"])

    def test_pyright_result_reaches_the_count_line_and_json(self: PyrightTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_pair(root)
            write(
                root / "cases" / "manifest.json",
                json.dumps(
                    {
                        "version": 1,
                        "cases": [
                            {
                                "id": "lab1/one",
                                "stage": 1,
                                "kind": "query",
                                "file": "cases/lab1/one.sql",
                            }
                        ],
                        "invariants": [
                            {"id": "pyright", "stage": 1, "kind": "pyright"}
                        ],
                    }
                ),
            )
            output = StringIO()
            with mock.patch.object(
                runner,
                "_check_pyright",
                return_value=([], {"errors": 0, "status": "pass"}),
            ):
                with redirect_stdout(output):
                    exit_code = main(["1", "--root", str(root)])

            self.assertEqual(0, exit_code, output.getvalue())
            self.assertIn("Pyright: 0 errors", output.getvalue())
            document = json.loads(
                (root / ".cmsc127" / "last-run.json").read_text(encoding="utf-8")
            )
            self.assertEqual({"errors": 0, "status": "pass"}, document["pyright"])

    def test_ignore_directive_fails_the_full_run(self: PyrightTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_pair(root)
            write(
                root / "cases" / "manifest.json",
                json.dumps(
                    {
                        "version": 1,
                        "cases": [
                            {
                                "id": "lab1/one",
                                "stage": 1,
                                "kind": "query",
                                "file": "cases/lab1/one.sql",
                            }
                        ],
                        "invariants": [
                            {"id": "pyright", "stage": 1, "kind": "pyright"}
                        ],
                    }
                ),
            )
            write(root / "reports" / "bad.py", 'value: int = "bad"  # pyright: ignore\n')
            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(["1", "--root", str(root)])

            self.assertEqual(1, exit_code, output.getvalue())
            self.assertIn("FAIL pyright", output.getvalue())
            self.assertIn("reports/bad.py:1", output.getvalue())


class ContributionTests(unittest.TestCase):
    def test_contribution_table_counts_commits_and_lines(self: ContributionTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(root / "first.txt", "one\ntwo\n")
            git(root, "init", "--quiet", "-b", "main")
            git(root, "add", "--all")
            git(
                root,
                "-c",
                "user.name=Ada",
                "-c",
                "user.email=ada@example.invalid",
                "commit",
                "--quiet",
                "-m",
                "first",
                env={
                    "GIT_AUTHOR_DATE": "2026-11-02T10:00:00+00:00",
                    "GIT_COMMITTER_DATE": "2026-11-02T10:00:00+00:00",
                },
            )
            write(root / "second.txt", "three\n")
            git(root, "add", "--all")
            git(
                root,
                "-c",
                "user.name=Grace",
                "-c",
                "user.email=grace@example.invalid",
                "commit",
                "--quiet",
                "-m",
                "second",
                env={
                    "GIT_AUTHOR_DATE": "2026-11-03T10:00:00+00:00",
                    "GIT_COMMITTER_DATE": "2026-11-03T10:00:00+00:00",
                },
            )

            table = contribution_table(root)

            self.assertIn("Commits", table)
            self.assertIn("Changed lines", table)
            self.assertIn("Ada", table)
            self.assertIn("Grace", table)

    def test_contribution_adds_no_score(self: ContributionTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_pair(root)
            write(
                root / "cases" / "manifest.json",
                json.dumps(
                    {
                        "version": 1,
                        "cases": [
                            {
                                "id": "lab1/one",
                                "stage": 1,
                                "kind": "query",
                                "file": "cases/lab1/one.sql",
                            }
                        ],
                        "invariants": [
                            {"id": "contribution", "stage": 1, "kind": "contribution"}
                        ],
                    }
                ),
            )
            git(root, "init", "--quiet", "-b", "main")
            git(root, "add", "--all")
            git(
                root,
                "-c",
                "user.name=Ada",
                "-c",
                "user.email=ada@example.invalid",
                "commit",
                "--quiet",
                "-m",
                "work",
            )
            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(["1", "--root", str(root)])

            self.assertEqual(0, exit_code, output.getvalue())
            self.assertIn("1/1 checks passed", output.getvalue())
            self.assertIn("INFO contribution", output.getvalue())
            document = json.loads(
                (root / ".cmsc127" / "last-run.json").read_text(encoding="utf-8")
            )
            results = cast(list[dict[str, object]], document["results"])
            contribution = next(item for item in results if item["id"] == "contribution")
            self.assertEqual("info", contribution["status"])


@unittest.skipUnless(os.environ.get("CMSC127_DATABASE_URL"), "requires PostgreSQL")
class CatalogCheckTests(unittest.TestCase):
    connection: _TestConnection
    schema: str

    def setUp(self: CatalogCheckTests) -> None:
        self.connection = cast(
            _TestConnection,
            psycopg.connect(os.environ["CMSC127_DATABASE_URL"], autocommit=True),
        )
        self.schema = f"t13_fix_{uuid4().hex[:10]}"
        with self.connection.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{self.schema}"')

    def tearDown(self: CatalogCheckTests) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')
        self.connection.close()

    def test_normal_form_rejects_a_missing_column(self: CatalogCheckTests) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                f'CREATE TABLE "{self.schema}".city (id integer PRIMARY KEY, name text)'
            )
        config = {
            "mapping": {
                "id": {"table": f"{self.schema}.city", "column": "id"},
                "name": {"table": f"{self.schema}.city", "column": "does_not_exist"},
            },
            "rules": [
                {"id": "id-name", "determinant": ["id"], "dependent": ["name"]}
            ],
        }

        errors = check_normal_form(self.connection, config)

        self.assertTrue(any("does_not_exist" in error for error in errors))

    def test_normal_form_rejects_a_missing_table(self: CatalogCheckTests) -> None:
        config = {
            "mapping": {
                "id": {"table": f"{self.schema}.missing", "column": "id"},
            },
            "rules": [
                {"id": "id-name", "determinant": ["id"], "dependent": ["id"]}
            ],
        }

        errors = check_normal_form(self.connection, config)

        self.assertTrue(any("does not exist" in error for error in errors))

    def test_index_check_rejects_a_nonexistent_index_name(self: CatalogCheckTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(
                root / "report.sql",
                f'SELECT id FROM "{self.schema}".city WHERE name = \'a\';\n',
            )
            with self.connection.cursor() as cursor:
                cursor.execute(
                    f'CREATE TABLE "{self.schema}".city (id integer PRIMARY KEY, name text)'
                )
                cursor.execute(
                    f'INSERT INTO "{self.schema}".city VALUES (1, \'a\')'
                )
            errors = check_index(
                root,
                self.connection,
                {"file": "report.sql", "index": "pkey"},
                {(self.schema, "city_pkey")},
            )

            self.assertTrue(any("pkey" in error for error in errors))

    def test_index_check_requires_a_migration_index(self: CatalogCheckTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            index = f"t13_fix_index_{uuid4().hex[:8]}"
            write(
                root / "report.sql",
                f'SELECT id FROM "{self.schema}".city WHERE name = \'a\';\n',
            )
            with self.connection.cursor() as cursor:
                cursor.execute(
                    f'CREATE TABLE "{self.schema}".city (id integer PRIMARY KEY, name text)'
                )
                cursor.execute(
                    f'CREATE INDEX "{index}" ON "{self.schema}".city (name)'
                )
                cursor.execute(
                    f'INSERT INTO "{self.schema}".city VALUES (1, \'a\')'
                )
            item = {"file": "report.sql", "index": index}

            self.assertEqual(
                [],
                check_index(root, self.connection, item, {(self.schema, index)}),
            )
            errors = check_index(root, self.connection, item, set())
            self.assertTrue(any("migration" in error for error in errors))


@unittest.skipUnless(os.environ.get("CMSC127_DATABASE_URL"), "requires PostgreSQL")
class SingleCaseTests(unittest.TestCase):
    def test_single_case_entry_point_runs_only_that_case(self: SingleCaseTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_pair(root)
            write(root / "cases" / "lab1" / "two.sql", "SELECT 2 AS value;\n")
            write(
                root / "cases" / "lab1" / "two.expected",
                '{"columns":["value"],"rows":[[2]]}\n',
            )
            write(
                root / "cases" / "manifest.json",
                json.dumps(
                    {
                        "version": 1,
                        "cases": [
                            {
                                "id": "lab1/one",
                                "stage": 1,
                                "kind": "query",
                                "file": "cases/lab1/one.sql",
                            },
                            {
                                "id": "lab1/two",
                                "stage": 1,
                                "kind": "query",
                                "file": "cases/lab1/two.sql",
                            },
                        ],
                    }
                ),
            )
            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(["--case", "lab1/two", "--root", str(root)])

            self.assertEqual(0, exit_code, output.getvalue())
            self.assertIn("PASS lab1/two", output.getvalue())
            self.assertNotIn("PASS lab1/one", output.getvalue())
            self.assertIn("1/1 checks passed", output.getvalue())


class ResultLineTests(unittest.TestCase):
    def test_pyright_result_is_part_of_the_count_line(self: ResultLineTests) -> None:
        results: list[dict[str, object]] = [
            {
                "id": "pyright",
                "kind": "pyright",
                "status": "pass",
                "stage": 1,
                "group": "Types",
                "actual": {"errors": 0, "status": "pass"},
                "differences": [],
            }
        ]
        output = StringIO()

        with redirect_stdout(output):
            exit_code = print_results(results, 1)

        self.assertEqual(0, exit_code)
        self.assertIn("Pyright: 0 errors", output.getvalue())


if __name__ == "__main__":
    unittest.main()
