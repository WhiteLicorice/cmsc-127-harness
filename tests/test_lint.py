"""Red-first tests for the starter's ``./lint`` form check.

Each test names one fault class that ``./lint`` must report. The tests call
``runner.main(["--lint", ...])`` so they fail before the lint mode exists.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

import runner

DESIGN = (
    "# Design\n\n"
    "## Tables\n\n"
    "- item holds a name.\n\n"
    "## Keys and Links\n\n"
    "- item stands alone.\n\n"
    "## Checker Warnings\n\n"
    "No warning prints.\n"
)

DESIGN_CONFIG = {
    "file": "DESIGN.md",
    "headings": ["Tables", "Keys and Links", "Checker Warnings"],
    "placeholders": {
        "Tables": "Write one bullet per table: what it holds, and its key.",
        "Keys and Links": "Describe how the tables connect, and why each link table exists.",
        "Checker Warnings": (
            "Write one `### <table>.<column>` entry for each warning that the "
            "checks print, saying whether the column is a copy or a separate "
            "fact and why. If no warning prints, write one line that says so."
        ),
    },
}


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def make_root(root: Path, runner_dir: Path) -> None:
    """Build the smallest starter tree that a clean lint accepts."""
    write(root / "cases" / "manifest.json", json.dumps({"version": 1, "cases": []}))
    write(
        root / "cases" / "pair" / "manifest.json",
        json.dumps({"version": 1, "cases": []}),
    )
    write(
        root / "cases" / "published" / "lab1" / "design.json",
        json.dumps(DESIGN_CONFIG),
    )
    write(
        root / "cases" / "published" / "lab1" / "design.expected",
        json.dumps({"columns": ["row"], "rows": []}),
    )
    write(
        root / "cases" / "published" / "lab1" / "manifest.json",
        json.dumps(
            {
                "version": 1,
                "cases": [
                    {
                        "id": "lab1/design",
                        "stage": 1,
                        "group": "Data Design",
                        "kind": "document",
                        "file": "cases/published/lab1/design.json",
                    }
                ],
            }
        ),
    )
    write(root / ".stage", "1\n")
    write(root / "migrations" / "0001_create_item.sql", "SELECT 1;\n")
    write(root / "load.sql", "")
    write(root / "DESIGN.md", DESIGN)
    write(root / "README.md", "# Starter\n")
    write(root / "check.sh", "#!/usr/bin/env bash\nset -eu\nexit 0\n")
    write(root / "lint", "#!/usr/bin/env bash\nset -eu\nexit 0\n")
    write(root / "answers" / "nosql.md", "# No-SQL\n")
    write(root / "answers" / "operations.py", "")
    write(root / "race" / "race.py", "")
    write(root / ".gitignore", ".cmsc127/last-run.json\n")
    write(
        runner_dir / "protected.json",
        json.dumps(
            {
                "files": {
                    ".gitignore": digest(root / ".gitignore"),
                    "check.sh": digest(root / "check.sh"),
                    "lint": digest(root / "lint"),
                },
                "allowed_paths": [
                    "README.md",
                    "DESIGN.md",
                    "load.sql",
                    "migrations",
                    "cases",
                    "reports",
                    "answers",
                    "operations",
                    "race",
                    "variants",
                    ".stage",
                ],
                "optional_files": [],
            }
        ),
    )
    git(root, "init", "--quiet")
    git(root, "add", "-A")
    git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "fixture",
    )


def run_lint(root: Path, runner_dir: Path) -> tuple[int, str, str]:
    output = StringIO()
    errors = StringIO()
    with redirect_stdout(output), redirect_stderr(errors):
        try:
            code = runner.main(
                ["--lint", "--root", str(root), "--runner-dir", str(runner_dir)]
            )
        except SystemExit as exit_code:
            code = int(exit_code.code or 0)
    return code, output.getvalue(), errors.getvalue()


class LintTests(unittest.TestCase):
    def test_a_clean_form_exits_zero(self: LintTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with tempfile.TemporaryDirectory() as runner_directory:
                root = Path(directory)
                runner_dir = Path(runner_directory)
                make_root(root, runner_dir)

                code, output, errors = run_lint(root, runner_dir)

                self.assertEqual(0, code, output + errors)
                self.assertIn("The form is clean.", output)
                self.assertIn(
                    "This read the form only. It says nothing about whether an "
                    "answer is right.",
                    output,
                )

    def test_the_clean_line_reveals_no_expected_answer(self: LintTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with tempfile.TemporaryDirectory() as runner_directory:
                root = Path(directory)
                runner_dir = Path(runner_directory)
                make_root(root, runner_dir)
                write(
                    root / "cases" / "published" / "lab1" / "design.expected",
                    json.dumps({"columns": ["row"], "rows": [[424242]]}),
                )

                code, output, errors = run_lint(root, runner_dir)

                self.assertEqual(0, code, output + errors)
                self.assertNotIn("424242", output)

    def test_a_manifest_fault_names_the_file_line_and_repair(self: LintTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with tempfile.TemporaryDirectory() as runner_directory:
                root = Path(directory)
                runner_dir = Path(runner_directory)
                make_root(root, runner_dir)
                write(
                    root / "cases" / "pair" / "manifest.json",
                    '{\n  "version": 2,\n  "cases": []\n}\n',
                )

                code, output, errors = run_lint(root, runner_dir)

                self.assertEqual(1, code, output + errors)
                self.assertIn("cases/pair/manifest.json: line 2", output)
                self.assertIn("version must be 1", output)

    def test_a_stage_fault_names_the_file_and_repair(self: LintTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with tempfile.TemporaryDirectory() as runner_directory:
                root = Path(directory)
                runner_dir = Path(runner_directory)
                make_root(root, runner_dir)
                write(root / ".stage", "x\n")

                code, output, errors = run_lint(root, runner_dir)

                self.assertEqual(1, code, output + errors)
                self.assertIn(".stage must be a positive integer.", output)

    def test_a_protected_fault_names_the_file_and_repair(self: LintTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with tempfile.TemporaryDirectory() as runner_directory:
                root = Path(directory)
                runner_dir = Path(runner_directory)
                make_root(root, runner_dir)
                write(root / "check.sh", "#!/usr/bin/env bash\nset -eu\nexit 1\n")

                code, output, errors = run_lint(root, runner_dir)

                self.assertEqual(1, code, output + errors)
                self.assertIn("check.sh: the protected file changed.", output)
                self.assertIn("Restore it from the starter you received.", output)

    def test_a_design_fault_names_the_file_line_and_repair(self: LintTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with tempfile.TemporaryDirectory() as runner_directory:
                root = Path(directory)
                runner_dir = Path(runner_directory)
                make_root(root, runner_dir)
                write(
                    root / "DESIGN.md",
                    "# Design\n\n## Tables\n\n## Keys and Links\n\n- item.\n\n"
                    "## Checker Warnings\n\nNo warning prints.\n",
                )

                code, output, errors = run_lint(root, runner_dir)

                self.assertEqual(1, code, output + errors)
                self.assertIn("DESIGN.md line 3: has nothing under", output)
                self.assertIn('"## Tables"', output)

    def test_a_missing_checker_warnings_heading_has_no_line_number(
        self: LintTests,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with tempfile.TemporaryDirectory() as runner_directory:
                root = Path(directory)
                runner_dir = Path(runner_directory)
                make_root(root, runner_dir)
                write(
                    root / "DESIGN.md",
                    "# Design\n\n## Tables\n\n- item holds a name.\n\n"
                    "## Keys and Links\n\n- item stands alone.\n",
                )
                write(
                    root / ".cmsc127" / "last-run.json",
                    json.dumps(
                        {
                            "results": [
                                {
                                    "warnings": [
                                        "warning: item.price agrees with a copy"
                                    ]
                                }
                            ]
                        }
                    ),
                )

                code, output, errors = run_lint(root, runner_dir)

                self.assertEqual(1, code, output + errors)
                self.assertIn(
                    'DESIGN.md is missing the heading "## Checker Warnings".', output
                )
                self.assertIn(
                    'DESIGN.md: has no "### item.price" entry under '
                    '"## Checker Warnings"',
                    output,
                )
                self.assertNotIn("DESIGN.md line 1:", output)

    def test_an_uppercase_migration_extension_is_a_form_fault(
        self: LintTests,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with tempfile.TemporaryDirectory() as runner_directory:
                root = Path(directory)
                runner_dir = Path(runner_directory)
                make_root(root, runner_dir)
                (root / "migrations" / "0001_create_item.sql").unlink()
                write(root / "migrations" / "0001_create_item.SQL", "SELECT 1;\n")

                code, output, errors = run_lint(root, runner_dir)

                self.assertEqual(1, code, output + errors)
                self.assertIn(
                    "migrations/0001_create_item.SQL: the file name must read "
                    "NNNN_lowercase_description.sql.",
                    output,
                )

    def test_a_stray_file_fault_names_the_file_and_repair(
        self: LintTests,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with tempfile.TemporaryDirectory() as runner_directory:
                root = Path(directory)
                runner_dir = Path(runner_directory)
                make_root(root, runner_dir)
                write(root / "notes.txt", "notes\n")

                code, output, errors = run_lint(root, runner_dir)

                self.assertEqual(1, code, output + errors)
                self.assertIn(
                    "notes.txt: this file is outside the writable paths.", output
                )

    def test_a_migration_fault_names_the_file_and_repair(self: LintTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with tempfile.TemporaryDirectory() as runner_directory:
                root = Path(directory)
                runner_dir = Path(runner_directory)
                make_root(root, runner_dir)
                (root / "migrations" / "0001_create_item.sql").unlink()
                write(root / "migrations" / "0002_create_item.sql", "SELECT 1;\n")

                code, output, errors = run_lint(root, runner_dir)

                self.assertEqual(1, code, output + errors)
                self.assertIn(
                    "migrations/0002_create_item.sql: the numbers must start at 0001",
                    output,
                )
                self.assertIn("The next number is 0001", output)

    def test_a_required_file_fault_names_the_file(self: LintTests) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with tempfile.TemporaryDirectory() as runner_directory:
                root = Path(directory)
                runner_dir = Path(runner_directory)
                make_root(root, runner_dir)
                (root / "load.sql").unlink()

                code, output, errors = run_lint(root, runner_dir)

                self.assertEqual(1, code, output + errors)
                self.assertIn("load.sql: the file is missing.", output)

    def test_warning_entries_are_skipped_without_a_result_file(
        self: LintTests,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with tempfile.TemporaryDirectory() as runner_directory:
                root = Path(directory)
                runner_dir = Path(runner_directory)
                make_root(root, runner_dir)

                code, output, errors = run_lint(root, runner_dir)

                self.assertEqual(0, code, output + errors)
                self.assertIn(
                    "not checked yet because .cmsc127/last-run.json does not exist",
                    output,
                )

    def test_warning_entries_are_checked_from_the_result_file(
        self: LintTests,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with tempfile.TemporaryDirectory() as runner_directory:
                root = Path(directory)
                runner_dir = Path(runner_directory)
                make_root(root, runner_dir)
                write(
                    root / ".cmsc127" / "last-run.json",
                    json.dumps(
                        {
                            "results": [
                                {
                                    "warnings": [
                                        "warning: item.price agrees with a copy"
                                    ]
                                }
                            ]
                        }
                    ),
                )

                code, output, errors = run_lint(root, runner_dir)

                self.assertEqual(1, code, output + errors)
                self.assertIn('"### item.price"', output)
                self.assertIn("no", output)


if __name__ == "__main__":
    unittest.main()
