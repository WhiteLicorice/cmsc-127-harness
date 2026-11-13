"""Parse the course ERD text format."""

from dataclasses import dataclass
import re


_IDENTIFIER = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*\Z")
_ENTITY_LINE = re.compile(r"(?P<name>[A-Za-z][A-Za-z0-9_]*)\s*\{\Z")
_ATTRIBUTE_LINE = re.compile(
    r"(?P<type>.+?)\s+(?P<name>\S+)"
    r"(?:\s+(?P<markers>PK(?:\s*,\s*FK)?|FK))?\Z"
)
_RELATIONSHIP_LINE = re.compile(
    r"(?P<left>[A-Za-z][A-Za-z0-9_]*)\s+"
    r"(?P<left_mark>\|\||\|o|\}o|\}\|)\s*--\s*"
    r"(?P<right_mark>\|\||o\||o\{|\|\{)\s+"
    r"(?P<right>[A-Za-z][A-Za-z0-9_]*)\s*:\s*"
    r"(?P<label>[A-Za-z][A-Za-z0-9_]*)\Z"
)


class ERDParseError(ValueError):
    """An ERD line does not follow the course format."""


@dataclass(frozen=True)
class Attribute:
    name: str
    type_name: str
    key_markers: frozenset[str]
    line: int


@dataclass(frozen=True)
class Entity:
    name: str
    attributes: dict[str, Attribute]
    line: int


@dataclass(frozen=True)
class Relationship:
    left: str
    left_mark: str
    right_mark: str
    right: str
    label: str
    line: int


@dataclass(frozen=True)
class ERD:
    entities: dict[str, Entity]
    relationships: tuple[Relationship, ...]


def _check_identifier(identifier: str, line: int) -> None:
    if not _IDENTIFIER.fullmatch(identifier):
        raise ERDParseError(
            f"line {line}: use a lowercase snake_case identifier that starts with a letter: {identifier}"
        )


def parse_erd(text: str) -> ERD:
    """Parse one Mermaid erDiagram block, without its Markdown fence."""
    if not isinstance(text, str):
        raise TypeError("ERD text must be a string")

    entities: dict[str, Entity] = {}
    relationships: list[Relationship] = []
    current_name: str | None = None
    current_line = 0
    attributes: dict[str, Attribute] = {}
    saw_header = False

    for line_number, source_line in enumerate(text.splitlines(), start=1):
        line = source_line.strip()
        if not line or line.startswith("%%"):
            continue

        if not saw_header:
            if line != "erDiagram":
                raise ERDParseError(f"line {line_number}: start the block with erDiagram")
            saw_header = True
            continue

        if current_name is not None:
            if line == "}":
                entities[current_name] = Entity(current_name, attributes, current_line)
                current_name = None
                current_line = 0
                attributes = {}
                continue

            match = _ATTRIBUTE_LINE.fullmatch(line)
            if match is None:
                raise ERDParseError(
                    f"line {line_number}: write an attribute as a type, an identifier, and optional PK or FK markers"
                )

            name = match.group("name")
            _check_identifier(name, line_number)
            if name in attributes:
                raise ERDParseError(f"line {line_number}: attribute {name} appears more than once")

            markers = match.group("markers") or ""
            key_markers = frozenset(part.strip() for part in markers.split(",") if part.strip())
            attributes[name] = Attribute(
                name=name,
                type_name=match.group("type").strip(),
                key_markers=key_markers,
                line=line_number,
            )
            continue

        entity_match = _ENTITY_LINE.fullmatch(line)
        if entity_match is not None:
            name = entity_match.group("name")
            _check_identifier(name, line_number)
            if name in entities:
                raise ERDParseError(f"line {line_number}: entity {name} appears more than once")
            current_name = name
            current_line = line_number
            attributes = {}
            continue

        relationship_match = _RELATIONSHIP_LINE.fullmatch(line)
        if relationship_match is not None:
            left = relationship_match.group("left")
            right = relationship_match.group("right")
            label = relationship_match.group("label")
            for identifier in (left, right, label):
                _check_identifier(identifier, line_number)
            relationships.append(
                Relationship(
                    left=left,
                    left_mark=relationship_match.group("left_mark"),
                    right_mark=relationship_match.group("right_mark"),
                    right=right,
                    label=label,
                    line=line_number,
                )
            )
            continue

        if line == "}":
            raise ERDParseError(f"line {line_number}: no entity is open")
        raise ERDParseError(f"line {line_number}: this line does not follow the ERD text format")

    if not saw_header:
        raise ERDParseError("line 1: start the block with erDiagram")
    if current_name is not None:
        raise ERDParseError(f"line {current_line}: close entity {current_name} with }}")

    return ERD(entities=entities, relationships=tuple(relationships))
