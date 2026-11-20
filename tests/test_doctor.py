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

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

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

    def run_doctor(self) -> tuple[str, int]:
        completed = subprocess.run(
            [self.bash, str(SHELL_DOCTOR)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self.environment(),
            timeout=300,
        )
        return completed.stdout + completed.stderr, completed.returncode

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


if __name__ == "__main__":
    unittest.main()
