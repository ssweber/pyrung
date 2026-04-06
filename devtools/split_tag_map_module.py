"""Split ``src/pyrung/click/tag_map.py`` into a package.

The split is intentionally conservative:

- Copy the existing top-level definitions verbatim into new modules.
- Keep the package root thin, re-exporting only the public API.
- Use ``ruff`` afterwards for import cleanup and formatting.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/pyrung/click/tag_map.py"
PACKAGE = ROOT / "src/pyrung/click/tag_map"
FUTURE_IMPORT = "from __future__ import annotations\n"

TYPE_NAMES = [
    "MappedSlot",
    "OwnerInfo",
    "StructuredImport",
    "_TagEntry",
    "_BlockEntry",
    "_BlockImportSpec",
]

PARSER_NAMES = [
    "_DATA_TYPE_TO_TAG_TYPE",
    "_HARDWARE_BLOCK_CACHE",
    "_IDENTIFIER_TOKEN_RE",
    "_EXPLICIT_NAMED_ARRAY_RE",
    "_EXPLICIT_UDT_RE",
    "_EXPLICIT_BLOCK_RE",
    "_EXPLICIT_BLOCK_START_RE",
    "_tag_type_for_memory_type",
    "_compress_addresses_to_ranges",
    "_valid_ranges_for_bank",
    "_hardware_block_for",
    "_parse_default",
    "_format_default",
    "_parse_structured_block_name",
    "_build_block_spec",
    "_default_logical_block_start",
    "_extract_address_comment",
    "_compose_address_comment",
    "_is_marker_only_boundary_row",
]

MAP_NAMES = [
    "_RESERVED_SYSTEM_HARDWARE_KEYS",
    "_BLOCK_SLOT_OWNER_RE",
    "TagMap",
]


def _node_name(node: ast.stmt) -> str | None:
    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        return node.name
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
        if isinstance(target, ast.Name):
            return target.id
    return None


def _slice_node(source_lines: list[str], node: ast.stmt) -> str:
    decorator_lines = getattr(node, "decorator_list", [])
    start = node.lineno
    if decorator_lines:
        start = min([start, *[decorator.lineno for decorator in decorator_lines]])
    end = node.end_lineno
    if end is None:
        raise RuntimeError(f"Node {type(node).__name__} is missing end_lineno in {SOURCE}")
    return "\n".join(source_lines[start - 1 : end]).rstrip() + "\n"


def _join_blocks(*blocks: str) -> str:
    parts = [block.strip() for block in blocks if block.strip()]
    return "\n\n".join(parts) + "\n"


def _top_level_nodes(module: ast.Module) -> dict[str, ast.stmt]:
    nodes: dict[str, ast.stmt] = {}
    for node in module.body:
        name = _node_name(node)
        if name is not None:
            nodes[name] = node
    return nodes


def _require_blocks(
    source_lines: list[str],
    nodes: dict[str, ast.stmt],
    names: list[str],
) -> list[str]:
    missing = [name for name in names if name not in nodes]
    if missing:
        raise RuntimeError(
            f"Missing expected top-level symbol(s) in {SOURCE}: {', '.join(missing)}"
        )
    return [_slice_node(source_lines, nodes[name]) for name in names]


def _extract_docstring_block(source_lines: list[str], module: ast.Module) -> str:
    if not module.body:
        raise RuntimeError(f"No top-level statements found in {SOURCE}")
    doc_node = module.body[0]
    if not (
        isinstance(doc_node, ast.Expr)
        and isinstance(doc_node.value, ast.Constant)
        and isinstance(doc_node.value.value, str)
    ):
        raise RuntimeError(f"Expected a module docstring at the top of {SOURCE}")
    return _slice_node(source_lines, doc_node)


def _render_types_module(source_lines: list[str], nodes: dict[str, ast.stmt]) -> str:
    body = _join_blocks(*_require_blocks(source_lines, nodes, TYPE_NAMES))
    return _join_blocks(
        '"""Automatically generated module split."""',
        FUTURE_IMPORT.strip(),
        textwrap.dedent(
            """
            from dataclasses import dataclass
            from typing import Literal

            from pyrung.core import Block, BlockRange, Tag
            """
        ),
        body,
    )


def _render_parsers_module(source_lines: list[str], nodes: dict[str, ast.stmt]) -> str:
    body = _join_blocks(*_require_blocks(source_lines, nodes, PARSER_NAMES))
    return _join_blocks(
        '"""Automatically generated module split."""',
        FUTURE_IMPORT.strip(),
        textwrap.dedent(
            """
            import re
            from typing import Literal

            from pyclickplc.addresses import AddressRecord, format_address_display, parse_address
            from pyclickplc.banks import BANKS, DEFAULT_RETENTIVE, DataType
            from pyclickplc.blocks import BlockRange as ClickBlockRange, parse_block_tag

            from pyrung.core import Block, BlockRange, InputBlock, OutputBlock, TagType

            from ._types import _BlockImportSpec
            """
        ),
        body,
    )


def _render_map_module(source_lines: list[str], nodes: dict[str, ast.stmt]) -> str:
    body = _join_blocks(*_require_blocks(source_lines, nodes, MAP_NAMES))
    return _join_blocks(
        '"""Automatically generated module split."""',
        FUTURE_IMPORT.strip(),
        textwrap.dedent(
            """
            import re
            from collections import defaultdict
            from collections.abc import Iterable, Mapping
            from dataclasses import replace
            from pathlib import Path
            from typing import TYPE_CHECKING, Any, Literal, cast

            import pyclickplc
            from pyclickplc.addresses import (
                AddressRecord,
                format_address_display,
                get_addr_key,
                parse_address,
            )
            from pyclickplc.banks import BANKS, DEFAULT_RETENTIVE, MEMORY_TYPE_BASES
            from pyclickplc.blocks import compute_all_block_ranges, format_block_tag
            from pyclickplc.validation import validate_nickname

            from pyrung.click.system_mappings import SYSTEM_CLICK_SLOTS
            from pyrung.core import Block, BlockRange, Tag
            from pyrung.core.system_points import SYSTEM_TAGS_BY_NAME
            from pyrung.core.tag import MappingEntry

            from ._parsers import (
                _build_block_spec,
                _compose_address_comment,
                _default_logical_block_start,
                _extract_address_comment,
                _format_default,
                _hardware_block_for,
                _is_marker_only_boundary_row,
                _parse_default,
                _parse_structured_block_name,
                _tag_type_for_memory_type,
            )
            from ._types import (
                MappedSlot,
                OwnerInfo,
                StructuredImport,
                _BlockEntry,
                _BlockImportSpec,
                _TagEntry,
            )

            if TYPE_CHECKING:
                from pyrung.click.profile import HardwareProfile
                from pyrung.click.validation import ClickValidationReport, ValidationMode
                from pyrung.core.program import Program
            """
        ),
        body,
    )


def _render_package_init(docstring: str) -> str:
    return _join_blocks(
        docstring,
        FUTURE_IMPORT.strip(),
        textwrap.dedent(
            """
            from ._map import TagMap
            from ._types import MappedSlot, OwnerInfo, StructuredImport

            __all__ = ["TagMap", "MappedSlot", "OwnerInfo", "StructuredImport"]
            """
        ),
    )


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def main() -> None:
    if not SOURCE.is_file():
        raise SystemExit(f"Expected source module at {SOURCE}")

    if PACKAGE.exists() and any(PACKAGE.iterdir()):
        raise SystemExit(f"Refusing to overwrite existing package at {PACKAGE}")

    source_text = SOURCE.read_text(encoding="utf-8")
    source_lines = source_text.splitlines()
    module = ast.parse(source_text)
    nodes = _top_level_nodes(module)
    docstring = _extract_docstring_block(source_lines, module)

    PACKAGE.mkdir(parents=True, exist_ok=True)
    _write(PACKAGE / "_types.py", _render_types_module(source_lines, nodes))
    _write(PACKAGE / "_parsers.py", _render_parsers_module(source_lines, nodes))
    _write(PACKAGE / "_map.py", _render_map_module(source_lines, nodes))
    _write(PACKAGE / "__init__.py", _render_package_init(docstring))
    SOURCE.unlink()

    print(f"Split {SOURCE} -> {PACKAGE}")


if __name__ == "__main__":
    main()
