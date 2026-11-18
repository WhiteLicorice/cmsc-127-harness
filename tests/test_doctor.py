"""The PowerShell doctor probes the port that CMSC127_DATABASE_URL names.

The doctor runs one listener check with ``Get-NetTCPConnection``. A student who
installs PostgreSQL on another port needs that check to look at the port in the
database URL. The port is 5432 when the variable is absent or names no port.
These tests run the script itself, so they need Windows PowerShell and skip
elsewhere.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
DOCTOR = ROOT / "doctor.ps1"
FREE_PORT = 65431


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


if __name__ == "__main__":
    unittest.main()
