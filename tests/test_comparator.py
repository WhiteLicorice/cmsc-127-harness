import os
import unittest

from comparator import compare_erd
from erd_parser import ERDParseError, parse_erd


class ComparatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg

        cls.connection = psycopg.connect(os.environ["DATABASE_URL"])
        cls.connection.autocommit = True

    @classmethod
    def tearDownClass(cls):
        with cls.connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS t04_compare CASCADE")
            cursor.execute("DROP SCHEMA IF EXISTS t04_empty CASCADE")
            cursor.execute("DROP SCHEMA IF EXISTS t04_search_first CASCADE")
            cursor.execute("DROP TABLE IF EXISTS public.t04_default")
        cls.connection.close()

    def setUp(self):
        with self.connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS t04_compare CASCADE")
            cursor.execute("DROP SCHEMA IF EXISTS t04_empty CASCADE")
            cursor.execute("DROP SCHEMA IF EXISTS t04_search_first CASCADE")
            cursor.execute("CREATE SCHEMA t04_compare")
            cursor.execute("CREATE SCHEMA t04_empty")
            cursor.execute("CREATE SCHEMA t04_search_first")

    def compare(self, erd_text, schema="t04_compare"):
        return compare_erd(self.connection, erd_text, schema=schema)

    def properties(self, differences):
        return {difference.property for difference in differences}

    def test_missing_entity_is_reported(self):
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

    def test_wrong_primary_key_is_reported(self):
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

    def test_missing_attribute_is_reported(self):
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

    def test_missing_foreign_key_marker_and_line_are_reported(self):
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

    def test_wrong_cardinality_is_reported(self):
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

    def test_reversed_relationship_endpoints_are_valid(self):
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

    def test_nullable_foreign_key_allows_zero_or_one_parent(self):
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

    def test_unique_foreign_key_allows_at_most_one_child(self):
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

    def test_many_to_many_uses_two_lines_to_the_associative_table(self):
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

    def test_composite_foreign_key_label_keeps_declared_column_order(self):
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

    def test_uppercase_identifier_is_rejected(self):
        with self.assertRaisesRegex(ERDParseError, "lowercase snake_case"):
            parse_erd("""erDiagram
                Student {
                    int student_id PK
                }
            """)

    def test_empty_schema_returns_one_actionable_message(self):
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

    def test_schema_argument_ignores_search_path(self):
        with self.connection.cursor() as cursor:
            cursor.execute("CREATE TABLE t04_compare.parent (id integer PRIMARY KEY)")
            cursor.execute("SET search_path TO t04_search_first")

        differences = self.compare("""erDiagram
            parent {
                int id PK
            }
        """, schema="t04_compare")

        self.assertEqual([], differences)

    def test_schema_defaults_to_public(self):
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

    def test_pg8000_connection_reads_the_same_catalog(self):
        import pg8000.dbapi

        with self.connection.cursor() as cursor:
            cursor.execute("CREATE TABLE t04_compare.parent (id integer PRIMARY KEY)")

        connection = pg8000.dbapi.connect(
            host=os.environ.get("PGHOST", "127.0.0.1"),
            port=int(os.environ.get("PGPORT", "5432")),
            user=os.environ.get("PGUSER", "postgres"),
            password=os.environ.get("PGPASSWORD") or None,
            database=os.environ.get("PGDATABASE", "postgres"),
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


if __name__ == "__main__":
    unittest.main()
