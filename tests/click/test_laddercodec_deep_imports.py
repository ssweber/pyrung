"""Fail when any module imports from a laddercodec module below the top level.

pyrung reads laddercodec through the names in ``laddercodec.__all__``. A submodule such as
``laddercodec.csv`` is not one of them, even though ``from laddercodec import csv`` works.
"""

from __future__ import annotations

import ast
from pathlib import Path

import laddercodec

import pyrung


def _imports_below_the_top_level(source: str, name: str) -> set[tuple[str, str, str]]:
    found: set[tuple[str, str, str]] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.startswith("laddercodec."):
                found |= {(name, module, alias.name) for alias in node.names}
            elif module == "laddercodec":
                found |= {
                    (name, module, alias.name)
                    for alias in node.names
                    if alias.name not in laddercodec.__all__
                }
        elif isinstance(node, ast.Import):
            found |= {
                (name, alias.name, "")
                for alias in node.names
                if alias.name.startswith("laddercodec.")
            }
    return found


def test_no_module_imports_laddercodec_below_the_top_level():
    root = Path(pyrung.__file__).resolve().parent
    found: set[tuple[str, str, str]] = set()
    for path in sorted(root.rglob("*.py")):
        name = f"src/pyrung/{path.relative_to(root).as_posix()}"
        found |= _imports_below_the_top_level(path.read_text(encoding="utf-8"), name)
    assert found == set()


def test_the_check_finds_each_form_of_a_deep_import():
    source = (
        "from laddercodec.csv.ast import AfCall\n"
        "from laddercodec import csv\n"
        "import laddercodec.csv\n"
        "from laddercodec import Send\n"
    )
    assert _imports_below_the_top_level(source, "m.py") == {
        ("m.py", "laddercodec.csv.ast", "AfCall"),
        ("m.py", "laddercodec", "csv"),
        ("m.py", "laddercodec.csv", ""),
    }
