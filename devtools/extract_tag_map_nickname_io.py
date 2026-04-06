"""Extract TagMap nickname CSV round-trip methods into a helper module.

This refactor is intentionally conservative:

- Copy the existing ``TagMap.from_nickname_file`` and ``TagMap.to_nickname_file``
  bodies verbatim into a new ``_nickname_io.py`` module.
- Keep ``TagMap`` as the public facade, delegating via thin wrappers.
- Use ``ruff`` afterwards for import cleanup and formatting.
"""

from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/pyrung/click/tag_map/_map.py"
TARGET = ROOT / "src/pyrung/click/tag_map/_nickname_io.py"

FROM_METHOD = "from_nickname_file"
TO_METHOD = "to_nickname_file"
HELPER_IMPORT = (
    "from ._nickname_io import tag_map_from_nickname_file, write_tag_map_to_nickname_file\n"
)


def _slice_node(source_lines: list[str], node: ast.stmt) -> str:
    decorator_lines = getattr(node, "decorator_list", [])
    start = node.lineno
    if decorator_lines:
        start = min([start, *[decorator.lineno for decorator in decorator_lines]])
    end = node.end_lineno
    if end is None:
        raise RuntimeError(f"Node {type(node).__name__} is missing end_lineno in {SOURCE}")
    return "\n".join(source_lines[start - 1 : end]).rstrip() + "\n"


def _find_tag_map_class(module: ast.Module) -> ast.ClassDef:
    for node in module.body:
        if isinstance(node, ast.ClassDef) and node.name == "TagMap":
            return node
    raise RuntimeError(f"Could not find TagMap class in {SOURCE}")


def _find_method(class_node: ast.ClassDef, name: str) -> ast.FunctionDef:
    for node in class_node.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise RuntimeError(f"Could not find TagMap.{name} in {SOURCE}")


def _extract_helper_source(
    source_lines: list[str],
    method_node: ast.FunctionDef,
    *,
    new_name: str,
    drop_classmethod: bool = False,
) -> str:
    text = textwrap.dedent(_slice_node(source_lines, method_node))
    lines = text.splitlines()
    if drop_classmethod:
        if not lines or lines[0].strip() != "@classmethod":
            raise RuntimeError(f"Expected @classmethod decorator on {method_node.name}")
        lines = lines[1:]
    text = "\n".join(lines).rstrip() + "\n"
    pattern = rf"^def\s+{re.escape(method_node.name)}\b"
    replaced = re.sub(pattern, f"def {new_name}", text, count=1, flags=re.MULTILINE)
    if replaced == text:
        raise RuntimeError(f"Failed to rename helper extracted from {method_node.name}")
    if method_node.name == FROM_METHOD:
        replaced = re.sub(
            r'(\s*mode: Literal\["warn", "strict"\] = "warn",\n)(\))',
            r"\1    reserved_system_hardware_keys: frozenset[int],\n\2",
            replaced,
            count=1,
        )
        replaced = replaced.replace(
            "_RESERVED_SYSTEM_HARDWARE_KEYS",
            "reserved_system_hardware_keys",
        )
    return replaced


def _render_nickname_io_module(from_helper: str, to_helper: str) -> str:
    parts = [
        '"""Automatically generated nickname CSV helper extraction."""',
        "from __future__ import annotations",
        textwrap.dedent(
            """
            import re
            from collections import defaultdict
            from dataclasses import replace
            from pathlib import Path
            from typing import TYPE_CHECKING, Any, Literal, cast

            import pyclickplc
            from pyclickplc.addresses import AddressRecord, format_address_display, get_addr_key
            from pyclickplc.banks import BANKS, DEFAULT_RETENTIVE, MEMORY_TYPE_BASES
            from pyclickplc.blocks import compute_all_block_ranges, format_block_tag
            from pyclickplc.validation import validate_nickname

            from pyrung.core import Block, Tag
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
            from ._types import StructuredImport, _BlockEntry, _BlockImportSpec

            if TYPE_CHECKING:
                from ._map import TagMap
            """
        ).strip(),
        from_helper.strip(),
        to_helper.strip(),
        '__all__ = ["tag_map_from_nickname_file", "write_tag_map_to_nickname_file"]',
    ]
    return "\n\n".join(parts) + "\n"


def _wrapper_source(indent: str = "    ") -> tuple[str, str]:
    from_wrapper = textwrap.dedent(
        '''
        @classmethod
        def from_nickname_file(
            cls,
            path: str | Path,
            *,
            mode: Literal["warn", "strict"] = "warn",
        ) -> TagMap:
            """Build a `TagMap` from a Click nickname CSV file."""
            return tag_map_from_nickname_file(
                cls,
                path,
                mode=mode,
                reserved_system_hardware_keys=_RESERVED_SYSTEM_HARDWARE_KEYS,
            )
        '''
    ).strip("\n")
    to_wrapper = textwrap.dedent(
        '''
        def to_nickname_file(self, path: str | Path) -> int:
            """Write this mapping to a Click nickname CSV file."""
            return write_tag_map_to_nickname_file(self, path)
        '''
    ).strip("\n")
    return textwrap.indent(from_wrapper, indent), textwrap.indent(to_wrapper, indent)


def _replace_method_blocks(
    source_lines: list[str],
    from_method: ast.FunctionDef,
    to_method: ast.FunctionDef,
) -> list[str]:
    from_wrapper, to_wrapper = _wrapper_source()
    replacements = [
        (to_method.lineno, to_method.end_lineno, to_wrapper),
        (from_method.decorator_list[0].lineno, from_method.end_lineno, from_wrapper),
    ]
    updated = list(source_lines)
    for start, end, replacement in replacements:
        if end is None:
            raise RuntimeError("Expected method end line information")
        updated[start - 1 : end] = replacement.splitlines()
    return updated


def _insert_helper_import(source_text: str) -> str:
    if HELPER_IMPORT in source_text:
        return source_text
    marker = "\n\nif TYPE_CHECKING:\n"
    if marker not in source_text:
        raise RuntimeError(f"Could not find TYPE_CHECKING marker in {SOURCE}")
    return source_text.replace(marker, f"\n\n{HELPER_IMPORT}{marker.lstrip()}", 1)


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def main() -> None:
    if not SOURCE.is_file():
        raise SystemExit(f"Expected source module at {SOURCE}")
    if TARGET.exists():
        raise SystemExit(f"Refusing to overwrite existing helper module at {TARGET}")

    source_text = SOURCE.read_text(encoding="utf-8")
    source_lines = source_text.splitlines()
    module = ast.parse(source_text)
    class_node = _find_tag_map_class(module)
    from_method = _find_method(class_node, FROM_METHOD)
    to_method = _find_method(class_node, TO_METHOD)

    from_helper = _extract_helper_source(
        source_lines,
        from_method,
        new_name="tag_map_from_nickname_file",
        drop_classmethod=True,
    )
    to_helper = _extract_helper_source(
        source_lines,
        to_method,
        new_name="write_tag_map_to_nickname_file",
    )

    updated_lines = _replace_method_blocks(source_lines, from_method, to_method)
    updated_source = _insert_helper_import("\n".join(updated_lines) + "\n")

    _write(TARGET, _render_nickname_io_module(from_helper, to_helper))
    _write(SOURCE, updated_source)

    print(f"Extracted nickname I/O helpers from {SOURCE} -> {TARGET}")


if __name__ == "__main__":
    main()
