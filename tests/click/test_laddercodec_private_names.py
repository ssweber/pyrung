"""Each name that pyrung imports from laddercodec must still be there after a pin move.

Three of them are not in laddercodec's public API: ``reads_back``, ``unchecked`` and
``_split_top_level_csv_like``. A laddercodec refactor can move them without notice.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src" / "pyrung"


def _laddercodec_imports() -> list[tuple[str, str, str]]:
    """Return each ``from laddercodec... import name`` under src/pyrung, with its file."""
    found = set()
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            module = node.module if isinstance(node, ast.ImportFrom) else None
            if module is not None and module.split(".")[0] == "laddercodec":
                where = path.relative_to(_SRC.parent).as_posix()
                found.update((module, alias.name, where) for alias in node.names)
    return sorted(found)


_IMPORTS = _laddercodec_imports()


def test_the_scan_finds_the_private_names():
    names = {(module, name) for module, name, _where in _IMPORTS}
    assert ("laddercodec.csv.converter", "reads_back") in names
    assert ("laddercodec.operands.unchecked", "unchecked") in names
    assert ("laddercodec.csv.token_parser", "_split_top_level_csv_like") in names


@pytest.mark.parametrize(("module", "name", "where"), _IMPORTS)
def test_laddercodec_still_has_each_imported_name(module, name, where):
    try:
        found = hasattr(importlib.import_module(module), name)
    except ImportError:
        found = False
    assert found, (
        f"laddercodec has no {module}.{name}, which {where} imports. The pin move removed or "
        "renamed it. Point the import at its new place, or ask laddercodec for a public name."
    )
