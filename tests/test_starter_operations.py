"""The starter's enrollment command reports a section lock timeout.

The starter ships no test suite, so this test lives with the runner's tests. It
runs the starter's own ``answers/operations.py`` against a scratch database
that holds the published lock SQL and the lock function. The starter root comes
from ``CMSC127_STARTER_DIR``, or from ``../starter`` beside this repository.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

SCHEMA = """
CREATE TABLE student (
    student_id integer PRIMARY KEY
);

CREATE TABLE section (
    term text NOT NULL,
    instructor_id integer NOT NULL,
    section_id integer NOT NULL,
    section_capacity integer NOT NULL,
    PRIMARY KEY (term, instructor_id, section_id)
);

CREATE TABLE enrollment (
    student_id integer NOT NULL REFERENCES student (student_id),
    term text NOT NULL,
    instructor_id integer NOT NULL,
    enrolled_on date NOT NULL
);

CREATE TABLE course_choice (
    student_id integer NOT NULL REFERENCES student (student_id),
    term text NOT NULL,
    course_id integer NOT NULL,
    PRIMARY KEY (student_id, term, course_id)
);

CREATE FUNCTION record_course_choice() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    INSERT INTO course_choice (student_id, term, course_id)
    VALUES (NEW.student_id, NEW.term, 3);
    RETURN NEW;
END;
$$;

CREATE TRIGGER enrollment_course_choice
BEFORE INSERT ON enrollment
FOR EACH ROW EXECUTE FUNCTION record_course_choice();

INSERT INTO student (student_id) VALUES (834), (835);

INSERT INTO section (term, instructor_id, section_id, section_capacity)
VALUES ('2024-08-01', 1, 3, 5);
"""

# The published lock function, minus the grant that names the registrar role.
LOCK_FUNCTION = """
CREATE FUNCTION lock_section_for_enrollment(target_section_id integer) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO pg_catalog, public
AS $$
BEGIN
    PERFORM 1
      FROM public.section
     WHERE section_id = target_section_id
     FOR UPDATE;
END;
$$;
"""

SLOW_INSERT = """
CREATE FUNCTION slow_enrollment() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM pg_sleep(2);
    RETURN NEW;
END;
$$;

CREATE TRIGGER slow_enrollment
BEFORE INSERT ON enrollment
FOR EACH ROW EXECUTE FUNCTION slow_enrollment();
"""

LOCK_TIMEOUT = "SET LOCAL lock_timeout = '2s';\n"
STATEMENT_TIMEOUT = "SET LOCAL statement_timeout = '10s';\n"
LOCK = "SELECT lock_section_for_enrollment(%s);\n"
COUNT = """SELECT section.section_capacity,
       COUNT(enrollment.student_id)::integer AS enrollment_count
  FROM section
  LEFT JOIN enrollment
    ON enrollment.term = section.term
   AND enrollment.instructor_id = section.instructor_id
 WHERE section.section_id = %s
 GROUP BY section.section_id, section.section_capacity;
"""
INSERT = """INSERT INTO enrollment (student_id, term, instructor_id, enrolled_on)
VALUES (%s, %s, %s, %s);
"""

LOCK_MESSAGE = "The section lock timed out. Try again.\n"
FAILURE_MESSAGE = "Enrollment failed. No rows changed.\n"


def starter_root() -> Path | None:
    configured = os.environ.get("CMSC127_STARTER_DIR")
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured))
    candidates.append(Path(__file__).resolve().parents[2] / "starter")
    for candidate in candidates:
        if (candidate / "answers" / "operations.py").is_file():
            return candidate
    return None


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def operations_workdir(starter: Path, statement_timeout: str) -> Path:
    root = Path(tempfile.mkdtemp(prefix="cmsc127-operations-"))
    (root / "answers").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(starter / "answers" / "operations.py", root / "answers" / "operations.py")
    safe = root / "operations" / "enroll" / "safe"
    write(safe / "lock_timeout.sql", LOCK_TIMEOUT)
    write(safe / "statement_timeout.sql", statement_timeout)
    write(safe / "lock.sql", LOCK)
    write(safe / "count.sql", COUNT)
    write(safe / "insert.sql", INSERT)
    return root


@unittest.skipUnless(os.environ.get("CMSC127_DATABASE_URL"), "requires PostgreSQL")
class StarterLockTimeoutTests(unittest.TestCase):
    base_url: str
    name: str
    url: str
    admin: psycopg.Connection[tuple[object, ...]]

    def setUp(self: StarterLockTimeoutTests) -> None:
        self.base_url = os.environ["CMSC127_DATABASE_URL"]
        self.name = f"cmsc127_race_{uuid4().hex}"
        self.admin = psycopg.connect(self.base_url, autocommit=True)
        with self.admin.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(self.name)))
        self.url = make_conninfo(self.base_url, dbname=self.name)
        with psycopg.connect(self.url, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(SCHEMA)
                cursor.execute(LOCK_FUNCTION)

    def tearDown(self: StarterLockTimeoutTests) -> None:
        with self.admin.cursor() as cursor:
            cursor.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(self.name)
                )
            )
        self.admin.close()

    def enroll(self: StarterLockTimeoutTests, work: Path) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["CMSC127_REGISTRAR_DATABASE_URL"] = self.url
        return subprocess.run(
            [sys.executable, "answers/operations.py", "enroll", "834", "3", "2024-08-01"],
            cwd=work,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

    def test_a_held_section_lock_reports_the_lock_message(
        self: StarterLockTimeoutTests,
    ) -> None:
        starter = starter_root()
        if starter is None:
            self.skipTest("set CMSC127_STARTER_DIR to the starter repository")
        work = operations_workdir(starter, STATEMENT_TIMEOUT)
        self.addCleanup(shutil.rmtree, work, True)

        with psycopg.connect(self.url) as holder:
            with holder.cursor() as cursor:
                cursor.execute("SELECT section_id FROM section WHERE section_id = 3 FOR UPDATE")

            completed = self.enroll(work)

        self.assertEqual("", completed.stdout, completed.stderr)
        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertEqual(LOCK_MESSAGE, completed.stderr)

    def test_a_repeat_enrollment_reports_the_section(
        self: StarterLockTimeoutTests,
    ) -> None:
        starter = starter_root()
        if starter is None:
            self.skipTest("set CMSC127_STARTER_DIR to the starter repository")
        work = operations_workdir(starter, STATEMENT_TIMEOUT)
        self.addCleanup(shutil.rmtree, work, True)

        first = self.enroll(work)
        self.assertEqual(0, first.returncode, first.stderr)

        second = self.enroll(work)

        self.assertEqual(1, second.returncode, second.stderr)
        self.assertEqual(
            "The student is already enrolled in this section.\n", second.stderr
        )
        self.assertNotIn("already takes this course", second.stderr)

    def test_a_statement_timeout_keeps_its_own_message(
        self: StarterLockTimeoutTests,
    ) -> None:
        starter = starter_root()
        if starter is None:
            self.skipTest("set CMSC127_STARTER_DIR to the starter repository")
        work = operations_workdir(starter, "SET LOCAL statement_timeout = '300ms';\n")
        self.addCleanup(shutil.rmtree, work, True)
        with psycopg.connect(self.url, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(SLOW_INSERT)

        completed = self.enroll(work)

        self.assertEqual("", completed.stdout, completed.stderr)
        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertEqual(FAILURE_MESSAGE, completed.stderr)
        self.assertNotIn("lock timed out", completed.stderr)


if __name__ == "__main__":
    unittest.main()
