"""Compare an ERD block with tables and constraints in a PostgreSQL schema."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol, cast

from erd_parser import ERD, Relationship, parse_erd


class _CatalogCursor(Protocol):
    def execute(
        self: _CatalogCursor,
        query: str,
        parameters: tuple[str, ...] | None = None,
    ) -> object: ...

    def fetchall(self: _CatalogCursor) -> Sequence[tuple[object, ...]]: ...

    def close(self: _CatalogCursor) -> None: ...


class _Connection(Protocol):
    def cursor(self: _Connection) -> object: ...


@dataclass(frozen=True)
class Difference:
    property: str
    subject: str
    erd_value: str
    schema_value: str
    message: str


@dataclass
class _Column:
    not_null: bool


@dataclass
class _ForeignKey:
    child: str
    columns: tuple[str, ...]
    parent: str
    required: bool
    unique: bool

    @property
    def label(self: _ForeignKey) -> str:
        return f"fk_{self.child}_{'_'.join(self.columns)}"


@dataclass
class _Table:
    columns: dict[str, _Column]
    primary_key: frozenset[str]
    foreign_keys: list[_ForeignKey]


_TABLE_SQL = """
SELECT c.relname
FROM pg_catalog.pg_class AS c
JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
WHERE n.nspname = %s
  AND c.relkind IN ('r', 'p', 'f')
ORDER BY c.relname
"""

_COLUMN_SQL = """
SELECT c.relname, a.attname, a.attnum, a.attnotnull
FROM pg_catalog.pg_class AS c
JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
JOIN pg_catalog.pg_attribute AS a ON a.attrelid = c.oid
WHERE n.nspname = %s
  AND c.relkind IN ('r', 'p', 'f')
  AND a.attnum > 0
  AND NOT a.attisdropped
ORDER BY c.relname, a.attnum
"""

_CONSTRAINT_SQL = """
SELECT child.relname, constraint_row.contype, constraint_row.conkey,
       parent.relname
FROM pg_catalog.pg_constraint AS constraint_row
JOIN pg_catalog.pg_class AS child ON child.oid = constraint_row.conrelid
JOIN pg_catalog.pg_namespace AS child_namespace
  ON child_namespace.oid = child.relnamespace
LEFT JOIN pg_catalog.pg_class AS parent ON parent.oid = constraint_row.confrelid
WHERE child_namespace.nspname = %s
  AND constraint_row.contype IN ('p', 'u', 'f')
ORDER BY child.relname, constraint_row.conname
"""


def _fetchall(
    connection: _Connection, query: str, parameters: tuple[str, ...]
) -> Sequence[tuple[object, ...]]:
    cursor = cast(_CatalogCursor, connection.cursor())
    try:
        cursor.execute(query, parameters)
        return cursor.fetchall()
    finally:
        cursor.close()


def _read_schema(connection: _Connection, schema: str) -> dict[str, _Table]:
    table_rows = _fetchall(connection, _TABLE_SQL, (schema,))
    tables: dict[str, _Table] = {
        cast(str, row[0]): _Table({}, frozenset(), []) for row in table_rows
    }
    if not tables:
        return tables

    attribute_numbers: dict[str, dict[int, str]] = {name: {} for name in tables}
    for row in _fetchall(connection, _COLUMN_SQL, (schema,)):
        table_name = cast(str, row[0])
        column_name = cast(str, row[1])
        number = cast(int, row[2])
        not_null = cast(bool, row[3])
        table = tables[table_name]
        table.columns[column_name] = _Column(bool(not_null))
        attribute_numbers[table_name][int(number)] = column_name

    unique_numbers: dict[str, set[frozenset[int]]] = {name: set() for name in tables}
    constraints = _fetchall(connection, _CONSTRAINT_SQL, (schema,))
    for row in constraints:
        table_name = cast(str, row[0])
        kind = cast(str, row[1])
        child_numbers = cast(Sequence[int], row[2])
        if kind in ("p", "u"):
            unique_numbers[table_name].add(frozenset(int(value) for value in child_numbers))
            if kind == "p":
                tables[table_name].primary_key = frozenset(
                    attribute_numbers[table_name][int(value)] for value in child_numbers
                )

    for row in constraints:
        table_name = cast(str, row[0])
        kind = cast(str, row[1])
        child_numbers = cast(Sequence[int], row[2])
        parent_name = cast(str | None, row[3])
        if kind != "f" or parent_name is None:
            continue
        child_numbers = tuple(int(value) for value in child_numbers)
        child_columns = tuple(
            attribute_numbers[table_name][number] for number in child_numbers
        )
        table = tables[table_name]
        table.foreign_keys.append(
            _ForeignKey(
                child=table_name,
                columns=child_columns,
                parent=parent_name,
                required=all(table.columns[name].not_null for name in child_columns),
                unique=frozenset(child_numbers) in unique_numbers[table_name],
            )
        )

    return tables


def _difference(
    property_name: str,
    subject: str,
    erd_value: str,
    schema_value: str,
    message: str,
) -> Difference:
    return Difference(property_name, subject, erd_value, schema_value, message)


def _column_list(columns: Iterable[str]) -> str:
    return ", ".join(f"`{column}`" for column in sorted(columns))


def _relationship_text(parent: str, child: str, foreign_key: _ForeignKey) -> str:
    parent_mark = "||" if foreign_key.required else "|o"
    child_mark = "o|" if foreign_key.unique else "o{"
    return f"{parent} {parent_mark}--{child_mark} {child} : {foreign_key.label}"


def _relationship_line(relationship: Relationship) -> str:
    return (
        f"{relationship.left} {relationship.left_mark}--"
        f"{relationship.right_mark} {relationship.right} : {relationship.label}"
    )


def _mark_meaning(mark: str) -> tuple[str, int, int | None]:
    if mark == "||":
        return ("one", 1, 1)
    if mark in ("|o", "o|"):
        return ("one", 0, 1)
    if mark in ("}o", "o{"):
        return ("many", 0, None)
    return ("many", 1, None)


def _relationship_matches(
    relationship: Relationship, foreign_key: _ForeignKey
) -> tuple[bool, bool]:
    """Return whether endpoints and cardinality match the catalog foreign key."""
    expected_parent = ("one", 1, 1) if foreign_key.required else ("one", 0, 1)
    expected_child = ("one", 0, 1) if foreign_key.unique else ("many", 0, None)

    if (
        foreign_key.child == foreign_key.parent
        and relationship.left == foreign_key.child
        and relationship.right == foreign_key.parent
    ):
        direct = (_mark_meaning(relationship.left_mark), _mark_meaning(relationship.right_mark))
        reverse = (direct[1], direct[0])
        return True, (expected_parent, expected_child) in (direct, reverse)

    if relationship.left == foreign_key.parent and relationship.right == foreign_key.child:
        actual_parent = _mark_meaning(relationship.left_mark)
        actual_child = _mark_meaning(relationship.right_mark)
        return True, (actual_parent, actual_child) == (expected_parent, expected_child)

    if relationship.left == foreign_key.child and relationship.right == foreign_key.parent:
        actual_child = _mark_meaning(relationship.left_mark)
        actual_parent = _mark_meaning(relationship.right_mark)
        return True, (actual_parent, actual_child) == (expected_parent, expected_child)

    return False, False


def _compare_relationships(
    erd: ERD, tables: dict[str, _Table], schema: str
) -> list[Difference]:
    differences: list[Difference] = []
    remaining = list(erd.relationships)

    for table_name in sorted(tables):
        for foreign_key in tables[table_name].foreign_keys:
            expected_line = _relationship_text(
                foreign_key.parent, foreign_key.child, foreign_key
            )
            matching_index = None
            cardinality_matches = False

            for index, relationship in enumerate(remaining):
                endpoints_match, cardinality_matches = _relationship_matches(
                    relationship, foreign_key
                )
                if relationship.label == foreign_key.label and endpoints_match:
                    matching_index = index
                    break

            if matching_index is not None:
                actual = remaining.pop(matching_index)
                if not cardinality_matches:
                    differences.append(
                        _difference(
                            "relationship cardinality",
                            foreign_key.label,
                            _relationship_line(actual),
                            expected_line,
                            f"Change the relationship line to `{expected_line}`. "
                            f"Declared keys and nullability set the cardinality for "
                            f"`{foreign_key.child}.{', '.join(foreign_key.columns)}`.",
                        )
                    )
                continue

            same_endpoints = [
                index
                for index, relationship in enumerate(remaining)
                if _relationship_matches(relationship, foreign_key)[0]
            ]
            if len(same_endpoints) == 1:
                actual = remaining.pop(same_endpoints[0])
                differences.append(
                    _difference(
                        "relationship",
                        foreign_key.label,
                        _relationship_line(actual),
                        expected_line,
                        f"Change this relationship label to `{foreign_key.label}`. "
                        "The label uses the child table and foreign-key columns in their declared order.",
                    )
                )
            else:
                differences.append(
                    _difference(
                        "relationship",
                        foreign_key.label,
                        "missing",
                        expected_line,
                        f"Add this relationship line for the declared foreign key: `{expected_line}`.",
                    )
                )

    for relationship in remaining:
        differences.append(
            _difference(
                "relationship",
                relationship.label,
                _relationship_line(relationship),
                "no matching foreign key in the selected schema",
                f"Remove relationship line `{_relationship_line(relationship)}`. "
                f"Schema `{schema}` has no matching foreign key.",
            )
        )

    return differences


def compare_erd(
    connection: _Connection, erd_text: str, schema: str = "public"
) -> list[Difference]:
    """Return differences between an ERD block and one PostgreSQL schema."""
    erd = parse_erd(erd_text)
    tables = _read_schema(connection, schema)
    if not tables:
        return [
            _difference(
                "schema",
                schema,
                "ERD block supplied",
                "schema has no tables",
                f"no tables found in schema {schema}. Check that your migrations ran against this database.",
            )
        ]

    differences: list[Difference] = []
    erd_names = set(erd.entities)
    schema_names = set(tables)

    for name in sorted(erd_names - schema_names):
        differences.append(
            _difference(
                "entity",
                name,
                f"entity `{name}`",
                f"no table named `{name}` in schema `{schema}`",
                f"Remove entity `{name}` from the ERD. Schema `{schema}` has no table with that name.",
            )
        )
    for name in sorted(schema_names - erd_names):
        differences.append(
            _difference(
                "entity",
                name,
                "missing",
                f"table `{name}` in schema `{schema}`",
                f"Add entity `{name}` to the ERD because table `{name}` exists in schema `{schema}`.",
            )
        )

    common_names = erd_names & schema_names
    for table_name in sorted(common_names):
        erd_columns = erd.entities[table_name].attributes
        schema_columns = tables[table_name].columns
        erd_column_names = set(erd_columns)
        schema_column_names = set(schema_columns)

        for name in sorted(schema_column_names - erd_column_names):
            differences.append(
                _difference(
                    "attribute",
                    f"{table_name}.{name}",
                    "missing",
                    f"column `{name}`",
                    f"Add attribute `{name}` to entity `{table_name}` because the table has that column.",
                )
            )
        for name in sorted(erd_column_names - schema_column_names):
            differences.append(
                _difference(
                    "attribute",
                    f"{table_name}.{name}",
                    f"attribute `{name}`",
                    "no matching column",
                    f"Remove attribute `{name}` from entity `{table_name}` or add that column to the table.",
                )
            )

        erd_primary = {
            name for name, attribute in erd_columns.items() if "PK" in attribute.key_markers
        }
        schema_primary = set(tables[table_name].primary_key)
        missing_primary = schema_primary - erd_primary
        extra_primary = erd_primary - schema_primary
        if missing_primary or extra_primary:
            expected = f"primary-key columns: {_column_list(schema_primary)}"
            actual = f"ERD PK columns: {_column_list(erd_primary)}"
            actions: list[str] = []
            if missing_primary:
                actions.append(
                    f"Mark {_column_list(missing_primary)} with `PK` in `{table_name}`"
                )
            if extra_primary:
                actions.append(
                    f"Remove `PK` from {_column_list(extra_primary)} in `{table_name}`"
                )
            differences.append(
                _difference(
                    "primary key",
                    table_name,
                    actual,
                    expected,
                    f"{'. '.join(actions)}. The ERD PK markers must match the "
                    "table's primary-key columns.",
                )
            )

        erd_foreign = {
            name for name, attribute in erd_columns.items() if "FK" in attribute.key_markers
        }
        schema_foreign = {
            name
            for foreign_key in tables[table_name].foreign_keys
            for name in foreign_key.columns
        }
        missing_foreign = schema_foreign - erd_foreign
        extra_foreign = erd_foreign - schema_foreign
        if missing_foreign or extra_foreign:
            actions: list[str] = []
            if missing_foreign:
                actions.append(
                    f"Mark {_column_list(missing_foreign)} with `FK` in `{table_name}`"
                )
            if extra_foreign:
                actions.append(
                    f"Remove `FK` from {_column_list(extra_foreign)} in `{table_name}`"
                )
            differences.append(
                _difference(
                    "foreign key",
                    table_name,
                    f"ERD FK columns: {_column_list(erd_foreign)}",
                    f"foreign-key columns: {_column_list(schema_foreign)}",
                    f"{'. '.join(actions)}. Mark every column that belongs to a "
                    "declared foreign key.",
                )
            )

    differences.extend(_compare_relationships(erd, tables, schema))
    return differences
