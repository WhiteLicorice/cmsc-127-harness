from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The positional index that carries the message for each call. A location
# tuple, a path, or a root keyword must not be read as a message.
MESSAGE_ARGUMENTS: dict[str, tuple[int, ...]] = {
    "FormFault": (3,),
    "fault": (0,),
    "published_fault": (0,),
    "error": (0,),
    "ValueError": (0,),
    "RuntimeError": (0,),
    "append": (0,),
    "extend": (0,),
}
MESSAGE_KEYWORDS = {"message", "reason", "text"}

# Fragments that the runner joins into a longer line, labels, paths, field
# names, SQL, machine-readable status lines, and internal runner-folder faults.
# A period on one of these would sit in the middle of the joined line or would
# turn a status line into prose. Each pattern names the exact shape it skips;
# do not add a bare prefix that could hide a complete sentence.
FRAGMENT_PATTERNS = [
    r"^unlisted case file: ",
    r"^unlisted expected or \.exit file: ",
    r"^column order or names differ: ",
    r"^row count differs: ",
    r"^row \d+ differs: ",
    r"^missing row: ",
    r"^unexpected row: ",
    r"^file is outside the published writable paths: ",
    r"^protected file changed: ",
    r"^protected file is missing: ",
    r"^report SQL file is missing: ",
    r"^feature file must stay inside the repository: ",
    r"^feature file is missing: ",
    r"^written answer file is missing: ",
    r"^index query must be a SQL file: ",
    r"^\{display\}: line \{line\}, column \{column\}: \{message\}$",
    r"^\{display\}:\{line\}:\{column\}: \{message\}$",
    r"^\{path\.relative_to\(root\)\.as_posix\(\)\}: case \{index\}: $",
    r"^\{path\.relative_to\(root\)\.as_posix\(\)\}:\{number\}$",
    r"^\{row\[0\]\}\.\{row\[1\]\}$",
    r"^\{error\[len\(prefix\) :\]\}: \{repair\}$",
    r"^row \{index\} differs: ",
    r"^\{author\}\t",
    r"^\./run prepare exits 0$",
    r"^\./run prepare failed$",
    r"^not available on this platform$",
    r"^bash\.exe$",
    r"^expected$",
    r"^differences$",
    r"^columns$",
    r"^rows$",
    r"^steps$",
    r"^check_sql$",
    r"^operation$",
    r"^file$",
    r"^headings$",
    r"^placeholders$",
    r"^warning_sql$",
    r"^report$",
    r"^view$",
    r"^sqlstate$",
    r"^session$",
    r"^sql$",
    r"^features$",
    r"^key_column$",
    r"^sections$",
    r"^replaces$",
    r"^alternate_file$",
    r"^expected_file$",
    r"^alternate_expected_file$",
    r"^role_env$",
    r"^(SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|WITH)\b",
    # Machine-readable lines printed by the runner and the server.
    r"^== .* ==$",
    r"^\{counts\['pass'\]\}/\{total\} checks passed, ",
    r"^Result file: ",
    r"^\{state\} ",
    r"^Group \{group\}: ",
    r"^Stage \{stage_number\}: ",
    r"^Stage \{stage\}: ",
    r"^SKIP ",
    r"^INFO ",
    r"^  \{fault\}$",
    r"^  \{difference\}$",
    r"^  expected: ",
    r"^  actual:   ",
    r"^  \{text\}$",
    r"^CMSC 127 Harness is available at ",
    r"^Open this address in your browser: ",
]


def literal_text(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                parts.append(str(value.value))
            elif isinstance(value, ast.FormattedValue):
                parts.append("{" + ast.unparse(value.value) + "}")
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = literal_text(node.left)
        right = literal_text(node.right)
        if left is None or right is None:
            return None
        return left + right
    return None


def resolved_text(node: ast.AST, constants: dict[str, tuple[int, str]]) -> str | None:
    if isinstance(node, ast.Name) and node.id in constants:
        return constants[node.id][1]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = resolved_text(node.left, constants)
        right = resolved_text(node.right, constants)
        if left is None or right is None:
            return None
        return left + right
    return literal_text(node)


def display_text(node: ast.AST, constants: dict[str, tuple[int, str]]) -> str:
    text = resolved_text(node, constants)
    if text is not None:
        return text
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return display_text(node.left, constants) + display_text(node.right, constants)
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                parts.append(str(value.value))
            elif isinstance(value, ast.FormattedValue):
                parts.append("{" + ast.unparse(value.value) + "}")
        return "".join(parts)
    if isinstance(node, (ast.List, ast.Tuple)):
        return ", ".join(display_text(element, constants) for element in node.elts)
    return ast.unparse(node)


def ending_text(node: ast.AST, constants: dict[str, tuple[int, str]]) -> str | None:
    """The text that fixes the final character, or None if it is dynamic."""
    if isinstance(node, ast.Name) and node.id in constants:
        return constants[node.id][1]
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        for value in reversed(node.values):
            if isinstance(value, ast.Constant):
                return str(value.value)
            if isinstance(value, ast.FormattedValue):
                return None
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return ending_text(node.right, constants)
    return None


def module_constants(tree: ast.Module) -> dict[str, tuple[int, str]]:
    constants: dict[str, tuple[int, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                text = literal_text(node.value)
                if text is not None:
                    constants[target.id] = (node.lineno, text)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            text = literal_text(node.value)
            if text is not None:
                constants[node.target.id] = (node.lineno, text)
    return constants


def value_texts(
    node: ast.AST, constants: dict[str, tuple[int, str]]
) -> list[tuple[int, str, str | None]]:
    if isinstance(node, ast.Name):
        if node.id in constants:
            line, text = constants[node.id]
            return [(line, text, text)]
        return []
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            return [(node.lineno, node.value, node.value)]
        return []
    if isinstance(node, ast.JoinedStr):
        text = literal_text(node)
        if text is None:
            return []
        return [(node.lineno, text, ending_text(node, constants))]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        text = resolved_text(node, constants)
        if text is not None:
            return [(node.lineno, text, text)]
        return [
            (
                node.lineno,
                display_text(node, constants),
                ending_text(node, constants),
            )
        ]
    if isinstance(node, ast.List):
        texts: list[tuple[int, str, str | None]] = []
        for element in node.elts:
            texts.extend(value_texts(element, constants))
        return texts
    return []


def return_texts(
    node: ast.AST, constants: dict[str, tuple[int, str]]
) -> list[tuple[int, str, str | None]]:
    """Messages a function returns, without reading data helpers as messages."""
    if isinstance(node, ast.Name) and node.id in constants:
        line, text = constants[node.id]
        return [(line, text, text)]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        text = resolved_text(node, constants)
        if text is not None:
            return [(node.lineno, text, text)]
        return return_texts(node.left, constants) + return_texts(node.right, constants)
    if isinstance(node, (ast.List, ast.Tuple)):
        texts: list[tuple[int, str, str | None]] = []
        for element in node.elts:
            texts.extend(return_texts(element, constants))
        return texts
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [(node.lineno, node.value, node.value)]
    return []


def is_fragment(text: str) -> bool:
    return any(re.match(pattern, text) for pattern in FRAGMENT_PATTERNS)


def exception_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.name is not None:
            names.add(node.name)
    return names


def exception_suffix(text: str, names: set[str]) -> str | None:
    match = re.search(r"\{([A-Za-z_][A-Za-z0-9_]*)\}$", text)
    if match is not None and match.group(1) in names:
        return match.group(1)
    return None


def messages_in(path: Path) -> list[tuple[int, str, str | None]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    constants = module_constants(tree)
    messages: list[tuple[int, str, str | None]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = None
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            if name == "print":
                for arg in node.args:
                    messages.extend(value_texts(arg, constants))
            elif name in MESSAGE_ARGUMENTS:
                for index in MESSAGE_ARGUMENTS[name]:
                    if index < len(node.args):
                        messages.extend(value_texts(node.args[index], constants))
                for keyword in node.keywords:
                    if keyword.arg in MESSAGE_KEYWORDS:
                        messages.extend(value_texts(keyword.value, constants))
        elif isinstance(node, ast.Return) and node.value is not None:
            messages.extend(return_texts(node.value, constants))
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "message":
                    messages.extend(value_texts(value, constants))
    unique: list[tuple[int, str, str | None]] = []
    seen: set[tuple[int, str, str | None]] = set()
    for message in messages:
        if message not in seen:
            seen.add(message)
            unique.append(message)
    return unique


class MessagePunctuationTests(unittest.TestCase):
    def test_every_complete_sentence_ends_with_punctuation(self) -> None:
        failures: list[str] = []
        for name in ("runner.py", "server.py"):
            path = ROOT / name
            tree = ast.parse(path.read_text(encoding="utf-8"))
            exceptions = exception_names(tree)
            for line, text, ending in messages_in(path):
                suffix = exception_suffix(text, exceptions)
                if suffix is not None:
                    failures.append(
                        f"{name}:{line}: raw exception suffix {suffix!r}: {text!r}"
                    )
                    continue
                if len(text) < 8:
                    continue
                if is_fragment(text):
                    continue
                if ending is None:
                    failures.append(f"{name}:{line}: ends with a dynamic value: {text!r}")
                    continue
                if ending.endswith((".", "?", ":")):
                    continue
                failures.append(f"{name}:{line}: {text!r}")
        self.assertEqual([], failures, "\n".join(failures))

    def test_form_fault_keeps_the_message_period(self) -> None:
        from runner import FormFault

        fault = FormFault(Path("cases") / "manifest.json", 1, 1, "file is missing.")
        self.assertEqual(
            "manifest.json: line 1, column 1: file is missing.",
            str(fault),
        )

        root = Path("repo")
        fault = FormFault(
            root / "cases" / "manifest.json",
            2,
            3,
            "file is missing.",
            root=root,
        )
        self.assertEqual(
            "cases/manifest.json: line 2, column 3: file is missing.",
            str(fault),
        )
