"""The doctors report the tools that CMSC 127 uses.

``doctor.ps1`` probes the port in ``CMSC127_DATABASE_URL``. The doctor runs one
listener check with ``Get-NetTCPConnection``. A student may install PostgreSQL
on another port. That check must look at the port in the database URL. The port
is 5432 when the variable is absent or holds no port. These tests run the script
itself, so they need Windows PowerShell and skip elsewhere.

``doctor.sh`` reads a version from each tool. A Windows tool writes a carriage
return before the newline, so the doctor must strip it. The doctor must also
read the exit code of the tool. A pipeline reports the exit code of its last
command. These tests put a fake tool first on ``PATH`` and run the script.
"""

from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import venv

import doctor_db

ROOT = Path(__file__).resolve().parent.parent
DOCTOR = ROOT / "doctor.ps1"
SHELL_DOCTOR = ROOT / "doctor.sh"
FREE_PORT = 65431

# Each fake writes the version that the doctor expects, with a carriage return
# before the newline, the way a Windows tool writes it.
FAKE_PYTHON = r"""#!/usr/bin/env bash
printf 'Python 3.15.2\r\n'
exit 0
"""
FAKE_PYTHON_FAILS = r"""#!/usr/bin/env bash
printf 'Python 3.15.2\r\n'
exit 1
"""
FAKE_POSTGRES = r"""#!/usr/bin/env bash
printf 'psql (PostgreSQL) 18.6\r\n'
exit 0
"""
FAKE_POSTGRES_FAILS = r"""#!/usr/bin/env bash
printf 'psql (PostgreSQL) 18.6\r\n'
exit 1
"""
FAKE_GIT = r"""#!/usr/bin/env bash
printf 'git version 2.51.0\r\n'
exit 0
"""
FAKE_GIT_FAILS = r"""#!/usr/bin/env bash
printf 'git version 2.51.0\r\n'
exit 1
"""
FAKE_PG_ISREADY = r"""#!/usr/bin/env bash
exit 0
"""


def fake_python(version: str) -> str:
    return f"""#!/usr/bin/env bash
printf 'Python {version}\\r\\n'
exit 0
"""


def bash_path() -> str | None:
    """Return the bash that runs the shell doctor, or None."""
    if sys.platform == "win32":
        git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
        if git_bash.is_file():
            return str(git_bash)
    return shutil.which("bash")


@unittest.skipUnless(sys.platform == "win32", "The PowerShell doctor checks Windows tools.")
class DoctorPortTest(unittest.TestCase):
    def setUp(self) -> None:
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if shell is None:
            self.skipTest("PowerShell is not on PATH.")
        self.shell = shell

    def run_doctor(self, url: str | None) -> str:
        environment = dict(os.environ)
        environment.pop("VIRTUAL_ENV", None)
        environment.pop("CMSC127_DATABASE_URL", None)
        if url is not None:
            environment["CMSC127_DATABASE_URL"] = url
        completed = subprocess.run(
            [
                self.shell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(DOCTOR),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=300,
        )
        return completed.stdout + completed.stderr

    def port_line(self, output: str) -> str:
        lines = [line for line in output.splitlines() if "server listens on port" in line]
        self.assertTrue(lines, output)
        return lines[0]

    def test_url_port(self) -> None:
        line = self.port_line(
            self.run_doctor(
                f"postgresql://postgres:password@127.0.0.1:{FREE_PORT}/postgres"
            )
        )
        self.assertIn(f"port {FREE_PORT}", line)

    def test_default_port(self) -> None:
        line = self.port_line(self.run_doctor(None))
        self.assertIn("port 5432", line)

    def test_no_port_in_url(self) -> None:
        line = self.port_line(
            self.run_doctor("postgresql://postgres:password@127.0.0.1/postgres")
        )
        self.assertIn("port 5432", line)


class DoctorShellVersionTest(unittest.TestCase):
    """The shell doctor reads a version and the exit code of each tool."""

    def setUp(self) -> None:
        bash = bash_path()
        if bash is None:
            self.skipTest("bash is not on PATH.")
        self.bash = bash
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.bin = Path(scratch.name)
        self.write_tool("python", FAKE_PYTHON)
        for tool in ("psql", "pg_dump", "pg_ctl"):
            self.write_tool(tool, FAKE_POSTGRES)
        self.write_tool("pg_isready", FAKE_PG_ISREADY)
        self.write_tool("git", FAKE_GIT)

    def environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        environment.pop("VIRTUAL_ENV", None)
        environment.pop("CMSC127_DATABASE_URL", None)
        environment["PATH"] = str(self.bin) + os.pathsep + environment.get("PATH", "")
        return environment

    def write_tool(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(body, encoding="utf-8", newline="\n")
        path.chmod(0o755)

    def run_doctor(
        self, environment: dict[str, str] | None = None
    ) -> tuple[str, int]:
        completed = subprocess.run(
            [self.bash, str(SHELL_DOCTOR)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self.environment() if environment is None else environment,
            timeout=300,
        )
        return completed.stdout + completed.stderr, completed.returncode

    def write_version(self, version: str) -> None:
        """Write a fake 3.15 launcher under every name the doctor tries.

        Windows checks ``py -3.15``. macOS and Linux check ``python3.15``.
        A correct ``python`` wins on every platform. The doctor must accept
        any 3.15 patch from any of the three.
        """
        for name in ("python", "py", "python3.15"):
            self.write_tool(name, fake_python(version))

    def fake_answers(self, name: str) -> bool:
        """Return True when the fake tool is the one that runs.

        Git Bash puts its own folders before the caller's PATH, so a fake
        ``git`` loses to the installed ``git`` there. The test skips that case.
        """
        completed = subprocess.run(
            [self.bash, "-c", f"command -v {name}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self.environment(),
            timeout=300,
        )
        return self.bin.name in completed.stdout

    def test_crlf_version_is_accepted(self) -> None:
        output, code = self.run_doctor()
        self.assertIn("ok    Python 3.15.2 runs on", output)
        self.assertNotIn("FAIL  Python reports", output)
        self.assertEqual(code, 0)

    def test_any_315_patch_is_accepted(self) -> None:
        for version in ("3.15.0", "3.15.3"):
            with self.subTest(version=version):
                self.write_version(version)
                output, code = self.run_doctor()
                self.assertIn(f"ok    Python {version} runs on", output)
                self.assertNotIn("FAIL  Python reports", output)
                self.assertEqual(code, 0, output)

    def test_316_is_rejected(self) -> None:
        self.write_version("3.16.0")
        output, code = self.run_doctor()
        self.assertIn("FAIL  Python reports Python 3.16.0 on", output)
        self.assertNotIn("ok    Python 3.16.0 runs on", output)
        self.assertEqual(code, 1)

    def test_virtual_env_python_is_checked(self) -> None:
        if sys.platform == "win32":
            name = Path("Scripts") / "python.exe"
        else:
            name = Path("bin") / "python"
        environment_python = self.bin / "venv" / name
        environment_python.parent.mkdir(parents=True)
        environment_python.write_text(fake_python("3.15.2"), encoding="utf-8", newline="\n")
        environment_python.chmod(0o755)
        self.write_version("3.12.0")
        environment = self.environment()
        environment["VIRTUAL_ENV"] = str(self.bin / "venv")

        output, code = self.run_doctor(environment)

        self.assertIn("ok    Python 3.15.2 runs on", output)
        self.assertNotIn("FAIL  Python reports", output)
        self.assertEqual(code, 0, output)

    def test_nonzero_exit_is_rejected(self) -> None:
        self.write_tool("python", FAKE_PYTHON_FAILS)
        output, code = self.run_doctor()
        self.assertIn("FAIL  Python reports Python 3.15.2 on", output)
        self.assertNotIn("ok    Python 3.15.2 runs on", output)
        self.assertEqual(code, 1)

    def test_crlf_tool_version_is_accepted(self) -> None:
        output, _ = self.run_doctor()
        for tool in ("psql", "pg_dump", "pg_ctl"):
            self.assertIn(f"ok    {tool} reports PostgreSQL 18.x.", output)

    def test_tool_nonzero_exit_is_rejected(self) -> None:
        self.write_tool("psql", FAKE_POSTGRES_FAILS)
        output, _ = self.run_doctor()
        self.assertIn("FAIL  psql did not report a version.", output)
        self.assertNotIn("ok    psql reports PostgreSQL 18.x.", output)

    def test_crlf_git_version_is_accepted(self) -> None:
        output, _ = self.run_doctor()
        self.assertIn("ok    Git runs on", output)

    def test_git_nonzero_exit_is_rejected(self) -> None:
        if not self.fake_answers("git"):
            self.skipTest("The real git answers before the fake on this platform.")
        self.write_tool("git", FAKE_GIT_FAILS)
        output, _ = self.run_doctor()
        self.assertIn("FAIL  Git did not report a version.", output)
        self.assertNotIn("ok    Git runs on", output)


def powershell() -> str | None:
    if sys.platform != "win32":
        return None
    return shutil.which("pwsh") or shutil.which("powershell")


@unittest.skipUnless(sys.platform == "win32", "The PowerShell doctor checks Windows tools.")
class DoctorPythonVersionTest(unittest.TestCase):
    """The PowerShell doctor accepts any 3.15 patch and prints it."""

    def setUp(self) -> None:
        shell = powershell()
        if shell is None:
            self.skipTest("PowerShell is not on PATH.")
        self.shell = shell
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.bin = Path(scratch.name)

    def write_py(self, version: str) -> None:
        (self.bin / "py.bat").write_text(
            f"@echo Python {version}\r\n", encoding="utf-8", newline=""
        )

    def run_doctor(self) -> str:
        environment = dict(os.environ)
        environment.pop("VIRTUAL_ENV", None)
        environment.pop("CMSC127_DATABASE_URL", None)
        environment["PATH"] = str(self.bin) + os.pathsep + environment.get("PATH", "")
        completed = subprocess.run(
            [
                self.shell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(DOCTOR),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=300,
        )
        return completed.stdout + completed.stderr

    def test_any_315_patch_is_accepted(self) -> None:
        for version in ("3.15.0", "3.15.3"):
            with self.subTest(version=version):
                self.write_py(version)
                output = self.run_doctor()
                self.assertIn(f"ok    Python {version} runs.", output)
                self.assertNotIn("FAIL  Python reports", output)

    def test_316_is_rejected(self) -> None:
        self.write_py("3.16.0")
        output = self.run_doctor()
        self.assertIn("FAIL  Python reports Python 3.16.0.", output)
        self.assertNotIn("ok    Python 3.16.0 runs.", output)


@unittest.skipUnless(sys.platform == "win32", "The PowerShell doctor checks Windows tools.")
class DoctorVirtualEnvironmentTest(unittest.TestCase):
    """An active environment supplies the interpreter, whatever PATH holds."""

    def setUp(self) -> None:
        shell = powershell()
        if shell is None:
            self.skipTest("PowerShell is not on PATH.")
        self.shell = shell
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.bin = Path(scratch.name)
        self.environment_dir = self.bin / "venv"
        venv.create(self.environment_dir, with_pip=False)
        (self.bin / "python.bat").write_text(
            "@echo Python 3.12.0\r\n", encoding="utf-8", newline=""
        )

    def run_doctor(self) -> str:
        environment = dict(os.environ)
        environment["VIRTUAL_ENV"] = str(self.environment_dir)
        environment.pop("CMSC127_DATABASE_URL", None)
        environment["PATH"] = str(self.bin) + os.pathsep + environment.get("PATH", "")
        completed = subprocess.run(
            [
                self.shell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(DOCTOR),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=300,
        )
        return completed.stdout + completed.stderr

    def test_environment_python_wins_over_path(self) -> None:
        output = self.run_doctor()

        self.assertIn("ok    Python 3.15.", output)
        self.assertNotIn("FAIL  Python reports Python 3.12.0.", output)


@unittest.skipUnless(sys.platform == "win32", "Both doctors run on Windows.")
class DoctorStepCountTest(unittest.TestCase):
    """Both doctors report the same step count."""

    def setUp(self) -> None:
        bash = bash_path()
        if bash is None:
            self.skipTest("bash is not on PATH.")
        shell = powershell()
        if shell is None:
            self.skipTest("PowerShell is not on PATH.")
        self.bash = bash
        self.shell = shell
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.bin = Path(scratch.name)
        for name, body in (
            ("python", FAKE_PYTHON),
            ("py", FAKE_PYTHON),
            ("python3.15", FAKE_PYTHON),
            ("psql", FAKE_POSTGRES),
            ("pg_dump", FAKE_POSTGRES),
            ("pg_ctl", FAKE_POSTGRES),
            ("pg_isready", FAKE_PG_ISREADY),
            ("git", FAKE_GIT),
        ):
            path = self.bin / name
            path.write_text(body, encoding="utf-8", newline="\n")
            path.chmod(0o755)
        self.windows_bin = self.bin / "windows"
        self.windows_bin.mkdir()
        for name, line in (
            ("python", "Python 3.15.2"),
            ("py", "Python 3.15.2"),
            ("psql", "psql (PostgreSQL) 18.6"),
            ("pg_dump", "pg_dump (PostgreSQL) 18.6"),
            ("pg_ctl", "pg_ctl (PostgreSQL) 18.6"),
            ("git", "git version 2.51.0"),
        ):
            (self.windows_bin / f"{name}.bat").write_text(
                f"@echo {line}\r\n", encoding="utf-8", newline=""
            )

    def environment(self, windows: bool = False) -> dict[str, str]:
        environment = dict(os.environ)
        environment.pop("VIRTUAL_ENV", None)
        environment.pop("CMSC127_DATABASE_URL", None)
        first = self.windows_bin if windows else self.bin
        environment["PATH"] = str(first) + os.pathsep + environment.get("PATH", "")
        return environment

    def step_total(self, output: str) -> int:
        matches = re.findall(r"\d+ of (\d+) steps complete", output)
        self.assertTrue(matches, output)
        return int(matches[-1])

    def run_shell_doctor(self) -> str:
        completed = subprocess.run(
            [self.bash, str(SHELL_DOCTOR)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self.environment(),
            timeout=300,
        )
        return completed.stdout + completed.stderr

    def run_powershell_doctor(self) -> str:
        completed = subprocess.run(
            [
                self.shell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(DOCTOR),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self.environment(windows=True),
            timeout=300,
        )
        return completed.stdout + completed.stderr

    def test_both_doctors_report_the_same_step_count(self) -> None:
        shell_output = self.run_shell_doctor()
        powershell_output = self.run_powershell_doctor()

        self.assertEqual(
            self.step_total(shell_output),
            self.step_total(powershell_output),
            shell_output + powershell_output,
        )


class DoctorDatabaseTest(unittest.TestCase):
    """The database check names psycopg and the interpreter that ran."""

    def test_psycopg_is_required_when_pg8000_is_installed(self) -> None:
        output = StringIO()
        with (
            mock.patch.object(
                doctor_db.metadata,
                "version",
                side_effect=[
                    doctor_db.metadata.PackageNotFoundError,
                    "1.31.6",
                ],
            ),
            redirect_stdout(output),
        ):
            exit_code = doctor_db.check_drivers()

        self.assertEqual(1, exit_code)
        self.assertIn("psycopg 3.3.9 is not installed", output.getvalue())

    def test_driver_check_prints_the_interpreter(self) -> None:
        output = StringIO()
        with (
            mock.patch.object(
                doctor_db.metadata,
                "version",
                side_effect=["3.3.9", "1.31.6"],
            ),
            redirect_stdout(output),
        ):
            exit_code = doctor_db.check_drivers()

        self.assertEqual(0, exit_code)
        lines = output.getvalue().splitlines()
        self.assertTrue(
            any(
                line.startswith("Python 3.") and sys.executable in line
                for line in lines
            ),
            lines,
        )

    def test_connection_check_requires_psycopg(self) -> None:
        output = StringIO()
        connect = mock.Mock()
        with (
            mock.patch.object(
                doctor_db.metadata,
                "version",
                side_effect=[
                    doctor_db.metadata.PackageNotFoundError,
                    "1.31.6",
                ],
            ),
            mock.patch.object(doctor_db, "_connect", connect),
            mock.patch.dict(
                os.environ,
                {"CMSC127_DATABASE_URL": "postgresql://postgres@127.0.0.1/postgres"},
            ),
            redirect_stdout(output),
        ):
            exit_code = doctor_db.main()

        self.assertEqual(1, exit_code)
        connect.assert_not_called()
        self.assertIn("psycopg 3.3.9 is not installed", output.getvalue())


if __name__ == "__main__":
    unittest.main()
