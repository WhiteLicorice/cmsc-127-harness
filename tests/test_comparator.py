from __future__ import annotations

from collections.abc import Sequence
from contextlib import redirect_stdout
from importlib import import_module
from io import StringIO
import os
from types import TracebackType
import unittest
from unittest.mock import Mock, patch
from typing import Protocol, cast

from comparator import Difference, compare_erd
import doctor_db
from erd_parser import ERDParseError, parse_erd


class _TestCursor(Protocol):
    def execute(
        self: _TestCursor,
        query: str,
        parameters: tuple[str, ...] | None = None,
    ) -> object: ...

    def fetchall(self: _TestCursor) -> Sequence[tuple[object, ...]]: ...

    def close(self: _TestCursor) -> None: ...

    def __enter__(self: _TestCursor) -> _TestCursor: ...

    def __exit__(
        self: _TestCursor,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...


class _TestConnection(Protocol):
    autocommit: bool

    def cursor(self: _TestConnection) -> _TestCursor: ...

    def close(self: _TestConnection) -> None: ...


class _PsycopgAPI(Protocol):
    def connect(self: _PsycopgAPI, url: str) -> object: ...


class _Pg8000API(Protocol):
    def connect(
        self: _Pg8000API,
        *,
        host: str,
        port: int,
        user: str,
        password: str | None,
        database: str,
    ) -> object: ...


class ComparatorTests(unittest.TestCase):
    connection: _TestConnection

    @classmethod
    def setUpClass(cls: type[ComparatorTests]) -> None:
        psycopg = cast(_PsycopgAPI, import_module("psycopg"))
        cls.connection = cast(
            _TestConnection, psycopg.connect(os.environ["CMSC127_DATABASE_URL"])
        )
        cls.connection.autocommit = True

    @classmethod
    def tearDownClass(cls: type[ComparatorTests]) -> None:
        with cls.connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS t04_compare CASCADE")
            cursor.execute("DROP SCHEMA IF EXISTS t04_empty CASCADE")
            cursor.execute("DROP SCHEMA IF EXISTS t04_search_first CASCADE")
            cursor.execute("DROP TABLE IF EXISTS public.t04_default")
        cls.connection.close()

    def setUp(self: ComparatorTests) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS t04_compare CASCADE")
            cursor.execute("DROP SCHEMA IF EXISTS t04_empty CASCADE")
            cursor.execute("DROP SCHEMA IF EXISTS t04_search_first CASCADE")
            cursor.execute("CREATE SCHEMA t04_compare")
            cursor.execute("CREATE SCHEMA t04_empty")
            cursor.execute("CREATE SCHEMA t04_search_first")

    def compare(
        self: ComparatorTests, erd_text: str, schema: str = "t04_compare"
    ) -> list[Difference]:
        return compare_erd(self.connection, erd_text, schema=schema)

    def properties(self: ComparatorTests, differences: Sequence[Difference]) -> set[str]:
        return {difference.property for difference in differences}

    def test_missing_entity_is_reported(self: ComparatorTests) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute("CREATE TABLE t04_compare.parent (id integer PRIMARY KEY)")
            cursor.execute("CREATE TABLE t04_compare.child (id integer PRIMARY KEY)")

        differences = self.compare("""erDiagram
            parent {
                int id PK
            }
        """)

        self.assertIn("entity", self.properties(differences))
        self.assertTrue(any("Add entity `child`" in item.message for item in differences))

    def test_wrong_primary_key_is_reported(self: ComparatorTests) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute("""CREATE TABLE t04_compare.parent (
                id integer PRIMARY KEY,
                code text UNIQUE
            )""")

        differences = self.compare("""erDiagram
            parent {
                int id
                string code PK
            }
        """)

        self.assertIn("primary key", self.properties(differences))
        self.assertTrue(any("Mark `id` with `PK`" in item.message for item in differences))

    def test_missing_attribute_is_reported(self: ComparatorTests) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute("""CREATE TABLE t04_compare.parent (
                id integer PRIMARY KEY,
                label text
            )""")

        differences = self.compare("""erDiagram
            parent {
                int id PK
            }
        """)

        self.assertIn("attribute", self.properties(differences))
        self.assertTrue(any("Add attribute `label`" in item.message for item in differences))

    def test_missing_foreign_key_marker_and_line_are_reported(self: ComparatorTests) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute("CREATE TABLE t04_compare.parent (id integer PRIMARY KEY)")
            cursor.execute("""CREATE TABLE t04_compare.child (
                id integer PRIMARY KEY,
                parent_id integer NOT NULL REFERENCES t04_compare.parent (id)
            )""")

        differences = self.compare("""erDiagram
            parent {
                int id PK
            }
            child {
                int id PK
                int parent_id
            }
        """)

        self.assertIn("foreign key", self.properties(differences))
        self.assertIn("relationship", self.properties(differences))
        self.assertTrue(any("Mark `parent_id` with `FK`" in item.message for item in differences))
        self.assertTrue(any("Add this relationship line" in item.message for item in differences))

    def test_wrong_cardinality_is_reported(self: ComparatorTests) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute("CREATE TABLE t04_compare.parent (id integer PRIMARY KEY)")
            cursor.execute("""CREATE TABLE t04_compare.child (
                id integer PRIMARY KEY,
                parent_id integer NOT NULL REFERENCES t04_compare.parent (id)
            )""")

        differences = self.compare("""erDiagram
            parent ||--|{ child : fk_child_parent_id

            parent {
                int id PK
            }
            child {
                int id PK
                int parent_id FK
            }
        """)

        self.assertIn("relationship cardinality", self.properties(differences))
        self.assertTrue(any("parent ||--o{ child" in item.message for item in differences))

    def test_reversed_relationship_endpoints_are_valid(self: ComparatorTests) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute("CREATE TABLE t04_compare.parent (id integer PRIMARY KEY)")
            cursor.execute("""CREATE TABLE t04_compare.child (
                id integer PRIMARY KEY,
                parent_id integer NOT NULL REFERENCES t04_compare.parent (id)
            )""")

        differences = self.compare("""erDiagram
            child }o--|| parent : fk_child_parent_id

            parent {
                int id PK
            }
            child {
                int id PK
                int parent_id FK
            }
        """)

        self.assertEqual([], differences)

    def test_nullable_foreign_key_allows_zero_or_one_parent(self: ComparatorTests) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute("CREATE TABLE t04_compare.parent (id integer PRIMARY KEY)")
            cursor.execute("""CREATE TABLE t04_compare.child (
                id integer PRIMARY KEY,
                parent_id integer REFERENCES t04_compare.parent (id)
            )""")

        differences = self.compare("""erDiagram
            parent |o--o{ child : fk_child_parent_id

            parent {
                int id PK
            }
            child {
                int id PK
                int parent_id FK
            }
        """)

        self.assertEqual([], differences)

    def test_unique_foreign_key_allows_at_most_one_child(self: ComparatorTests) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute("CREATE TABLE t04_compare.parent (id integer PRIMARY KEY)")
            cursor.execute("""CREATE TABLE t04_compare.child (
                id integer PRIMARY KEY,
                parent_id integer NOT NULL UNIQUE REFERENCES t04_compare.parent (id)
            )""")

        differences = self.compare("""erDiagram
            parent ||--o| child : fk_child_parent_id

            parent {
                int id PK
            }
            child {
                int id PK
                int parent_id FK
            }
        """)

        self.assertEqual([], differences)

    def test_many_to_many_uses_two_lines_to_the_associative_table(self: ComparatorTests) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute("CREATE TABLE t04_compare.student (student_id integer PRIMARY KEY)")
            cursor.execute("CREATE TABLE t04_compare.course (course_id integer PRIMARY KEY)")
            cursor.execute("""CREATE TABLE t04_compare.enrollment (
                student_id integer NOT NULL REFERENCES t04_compare.student (student_id),
                course_id integer NOT NULL REFERENCES t04_compare.course (course_id),
                PRIMARY KEY (student_id, course_id)
            )""")

        differences = self.compare("""erDiagram
            student ||--o{ enrollment : fk_enrollment_student_id
            course ||--o{ enrollment : fk_enrollment_course_id

            student {
                int student_id PK
            }
            course {
                int course_id PK
            }
            enrollment {
                int student_id PK, FK
                int course_id PK, FK
            }
        """)

        self.assertEqual([], differences)

    def test_composite_foreign_key_label_keeps_declared_column_order(self: ComparatorTests) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute("""CREATE TABLE t04_compare.parent (
                tenant_id integer NOT NULL,
                code text NOT NULL,
                PRIMARY KEY (tenant_id, code)
            )""")
            cursor.execute("""CREATE TABLE t04_compare.child (
                tenant_id integer NOT NULL,
                parent_code text NOT NULL,
                FOREIGN KEY (tenant_id, parent_code)
                    REFERENCES t04_compare.parent (tenant_id, code)
            )""")

        differences = self.compare("""erDiagram
            parent ||--o{ child : fk_child_tenant_id_parent_code

            parent {
                int tenant_id PK
                string code PK
            }
            child {
                int tenant_id FK
                string parent_code FK
            }
        """)

        self.assertEqual([], differences)

    def test_uppercase_identifier_is_rejected(self: ComparatorTests) -> None:
        with self.assertRaisesRegex(ERDParseError, "lowercase snake_case"):
            parse_erd("""erDiagram
                Student {
                    int student_id PK
                }
            """)

    def test_empty_schema_returns_one_actionable_message(self: ComparatorTests) -> None:
        differences = self.compare("""erDiagram
            missing_entity {
                int id PK
            }
        """, schema="t04_empty")

        self.assertEqual(1, len(differences))
        self.assertIn("no tables found in schema t04_empty", differences[0].message)
        self.assertIn(
            "check that your migrations ran against this database",
            differences[0].message.lower(),
        )

    def test_schema_argument_ignores_search_path(self: ComparatorTests) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute("CREATE TABLE t04_compare.parent (id integer PRIMARY KEY)")
            cursor.execute("SET search_path TO t04_search_first")

        differences = self.compare("""erDiagram
            parent {
                int id PK
            }
        """, schema="t04_compare")

        self.assertEqual([], differences)

    def test_schema_defaults_to_public(self: ComparatorTests) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute("CREATE TABLE public.t04_default (id integer PRIMARY KEY)")

        try:
            differences = compare_erd(self.connection, """erDiagram
                t04_default {
                    int id PK
                }
            """)
        finally:
            with self.connection.cursor() as cursor:
                cursor.execute("DROP TABLE public.t04_default")

        self.assertEqual([], differences)

    def test_pg8000_connection_reads_the_same_catalog(self: ComparatorTests) -> None:
        dbapi = cast(_Pg8000API, import_module("pg8000.dbapi"))

        with self.connection.cursor() as cursor:
            cursor.execute("CREATE TABLE t04_compare.parent (id integer PRIMARY KEY)")

        connection = cast(
            _TestConnection,
            dbapi.connect(
                host=os.environ.get("PGHOST", "127.0.0.1"),
                port=int(os.environ.get("PGPORT", "5432")),
                user=os.environ.get("PGUSER", "postgres"),
                password=os.environ.get("PGPASSWORD") or None,
                database=os.environ.get("PGDATABASE", "postgres"),
            ),
        )
        connection.autocommit = True
        try:
            differences = compare_erd(connection, """erDiagram
                parent {
                    int id PK
                }
            """, schema="t04_compare")
        finally:
            connection.close()

        self.assertEqual([], differences)

    def test_uncompared_type_default_check_and_index_differences_return_empty(
        self: ComparatorTests,
    ) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute("""CREATE TABLE t04_compare.parent (
                id integer PRIMARY KEY,
                score integer DEFAULT 7 CHECK (score > 0),
                code text
            )""")
            cursor.execute(
                "CREATE INDEX parent_code_idx ON t04_compare.parent (code)"
            )

        differences = self.compare("""erDiagram
            parent {
                string id PK
                text score
                int code
            }
        """)

        self.assertEqual([], differences)

    def test_unique_superset_of_foreign_key_columns_keeps_many_children(
        self: ComparatorTests,
    ) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute("CREATE TABLE t04_compare.parent (id integer PRIMARY KEY)")
            cursor.execute("""CREATE TABLE t04_compare.child (
                child_id integer PRIMARY KEY,
                parent_id integer NOT NULL REFERENCES t04_compare.parent (id),
                other_id integer NOT NULL,
                UNIQUE (parent_id, other_id)
            )""")

        differences = self.compare("""erDiagram
            parent ||--o{ child : fk_child_parent_id

            parent {
                int id PK
            }
            child {
                int child_id PK
                int parent_id FK
                int other_id
            }
        """)

        self.assertEqual([], differences)


class ParserEdgeCaseTests(unittest.TestCase):
    def test_reversed_key_marker_order_is_rejected(self: ParserEdgeCaseTests) -> None:
        with self.assertRaisesRegex(ERDParseError, "PK.*FK"):
            parse_erd("""erDiagram
                parent {
                    int id PK
                    int parent_id FK, PK
                }
            """)

    def test_lowercase_key_marker_is_rejected(self: ParserEdgeCaseTests) -> None:
        with self.assertRaisesRegex(ERDParseError, "PK.*FK"):
            parse_erd("""erDiagram
                parent {
                    int id pk
                }
            """)

    def test_identifier_allows_repeated_underscores(self: ParserEdgeCaseTests) -> None:
        erd = parse_erd("""erDiagram
            parent {
                int a__b
            }
        """)

        self.assertIn("a__b", erd.entities["parent"].attributes)

    def test_identifier_allows_trailing_underscore(self: ParserEdgeCaseTests) -> None:
        erd = parse_erd("""erDiagram
            parent {
                int a_
            }
        """)

        self.assertIn("a_", erd.entities["parent"].attributes)

    def test_one_line_entity_is_accepted(self: ParserEdgeCaseTests) -> None:
        erd = parse_erd("erDiagram\n    parent { int id PK }\n")

        self.assertEqual(
            frozenset({"PK"}), erd.entities["parent"].attributes["id"].key_markers
        )

    def test_one_line_entity_accepts_both_key_markers(
        self: ParserEdgeCaseTests,
    ) -> None:
        erd = parse_erd("erDiagram\n    child { int parent_id PK, FK }\n")

        self.assertEqual(
            frozenset({"PK", "FK"}),
            erd.entities["child"].attributes["parent_id"].key_markers,
        )

    def test_one_line_entity_rejects_multiple_attributes(
        self: ParserEdgeCaseTests,
    ) -> None:
        declarations = (
            "parent { int id PK, int x }",
            "parent { int id PK int y int z }",
        )
        for declaration in declarations:
            with self.subTest(declaration=declaration):
                with self.assertRaisesRegex(
                    ERDParseError, "write each attribute on its own line"
                ):
                    parse_erd(f"erDiagram\n    {declaration}\n")


class DoctorDatabaseTests(unittest.TestCase):
    def test_stale_fallback_version_is_hidden_during_connection_check(
        self: DoctorDatabaseTests,
    ) -> None:
        output = StringIO()
        connection = Mock()
        with (
            patch.object(
                doctor_db.metadata,
                "version",
                side_effect=["3.3.9", "1.31.5"],
            ),
            patch.object(doctor_db, "_connect", return_value=connection),
            patch.dict(
                os.environ,
                {"CMSC127_DATABASE_URL": "postgresql://postgres@127.0.0.1/postgres"},
            ),
            redirect_stdout(output),
        ):
            exit_code = doctor_db.main()

        self.assertEqual(0, exit_code)
        self.assertNotIn(
            "FAIL  pg8000 is 1.31.5, not 1.31.6.",
            output.getvalue().splitlines(),
        )
        self.assertNotIn(
            "      Install pg8000 1.31.6.",
            output.getvalue().splitlines(),
        )


if __name__ == "__main__":
    unittest.main()
