"""List every import of a laddercodec module below the top level.

pyrung reads laddercodec through its top-level names. The send and receive family also
imports three names from laddercodec's inner modules, because laddercodec has no top-level
function that parses a Send or Receive call. laddercodec will add `Send.from_csv_token()` and
`Receive.from_csv_token()` to replace them. This test fails when the list changes, so that
the change gets looked at, and a new deep import has to be added here by hand.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pyrung

DEEP_IMPORTS = {
    ("src/pyrung/click/validation/families/send_receive.py", "laddercodec.csv.ast", "AfCall"),
    (
        "src/pyrung/click/validation/families/send_receive.py",
        "laddercodec.csv.token_parser",
        "parse_af_token",
    ),
    (
        "src/pyrung/click/validation/families/send_receive.py",
        "laddercodec.instructions.send_receive",
        "parse_af_call",
    ),
}


def _deep_imports() -> set[tuple[str, str, str]]:
    root = Path(pyrung.__file__).resolve().parent
    found: set[tuple[str, str, str]] = set()
    for path in sorted(root.rglob("*.py")):
        name = f"src/pyrung/{path.relative_to(root).as_posix()}"
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("laddercodec."):
                module = node.module or ""
                found |= {(name, module, alias.name) for alias in node.names}
            elif isinstance(node, ast.Import):
                found |= {
                    (name, alias.name, "")
                    for alias in node.names
                    if alias.name.startswith("laddercodec.")
                }
    return found


def test_the_deep_laddercodec_imports_are_the_three_that_the_send_receive_family_uses():
    assert _deep_imports() == DEEP_IMPORTS
