from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
import urllib.error
import urllib.request

import psycopg
from psycopg import sql

from tests.test_server import get_json, running_server, test_schema


class PonytailBoundaryTests(unittest.TestCase):
    def test_checks_reject_entries_that_cannot_be_rendered(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            result_file = root / ".cmsc127" / "last-run.json"
            result_file.parent.mkdir()
            with running_server(root, None) as address:
                for entry in (None, {}, {"id": 1, "status": "pass"},
                              {"id": "case", "status": "pass", "stage": "<img src=x>"}):
                    with self.subTest(entry=entry):
                        result_file.write_text(json.dumps({"results": [entry]}), encoding="utf-8")
                        data = get_json(address + "/api/checks")
                        self.assertEqual([], data["results"])
                        self.assertIn("unsupported format", data["message"])

    def test_untrusted_host_cannot_read_local_results(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            result_file = root / ".cmsc127" / "last-run.json"
            result_file.parent.mkdir()
            result_file.write_text('{"results":[]}', encoding="utf-8")
            with running_server(root, None) as address:
                request = urllib.request.Request(address + "/api/checks", headers={"Host": "attacker.invalid"})
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    urllib.request.urlopen(request, timeout=3)
                self.assertEqual(403, failure.exception.code)
                self.assertEqual([], get_json(address + "/api/checks")["results"])

    @unittest.skipUnless(os.environ.get("CMSC127_DATABASE_URL"), "requires PostgreSQL")
    def test_table_poll_times_out_while_another_transaction_holds_a_lock(self) -> None:
        url = os.environ["CMSC127_DATABASE_URL"]
        with test_schema() as schema, TemporaryDirectory() as directory:
            with psycopg.connect(url) as locker:
                locker.execute(sql.SQL("LOCK TABLE {}.parent IN ACCESS EXCLUSIVE MODE").format(sql.Identifier(schema)))
                with running_server(Path(directory), url) as address:
                    started = time.monotonic()
                    try:
                        with self.assertRaises(urllib.error.HTTPError) as failure:
                            urllib.request.urlopen(address + "/api/tables?schema=" + schema, timeout=5)
                        self.assertEqual(503, failure.exception.code)
                        self.assertLess(time.monotonic() - started, 4)
                    finally:
                        locker.rollback()
                    self.assertEqual(schema, get_json(address + "/api/tables?schema=" + schema)["schema"])


if __name__ == "__main__":
    unittest.main()
