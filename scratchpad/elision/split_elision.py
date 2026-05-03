"""AST-based splitter: elision.py → elision/ package.

Walks the AST to find top-level node line ranges, slices the original source
text (preserving comments, decorators, formatting), categorises each node,
resolves per-file imports, and writes the three output files.

Usage:
    uv run python scratchpad/elision/split_elision.py
"""

from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path

SRC = Path("src/pyrung/core/analysis/prove/elision.py")
OUT_DIR = SRC.parent / "elision"

# ── Node categorisation ──────────────────────────────────────────────

ABSTRACT_NAMES = {
    # types
    "_AbsValue",
    "_AbstractState",
    "_ExecutionResult",
    "_ElidedSummary",
    "_CandidateRun",
    "_ExactContext",
    # classes
    "_TagElisionCheck",
    "_ScanLocalStateElider",
    # functions
    "_dep_union",
    "_merge_values",
    "_merge_states",
    "_merge_return_paths",
    # constants
    "_NO_CONST",
    "_CONST_FALSE",
    "_CONST_TRUE",
    "_RETAINED_VALUE",
    "_INPUT_VALUE",
    "_ENTRY_VALUE",
    "_UNKNOWN_VALUE",
    "_ZERO_VALUE",
    "_EXPR_ENUM_LIMIT",
}

CONCRETE_NAMES = {
    # types
    "_ForcedTrueCoverage",
    # classes
    "_ConcreteStateElider",
    # functions
    "_domain_from_tag_metadata",
    "_product_size",
    "_is_fault_tag",
    "_alternate_seed_value",
    "_seed_profile",
    "_coverage_domain_items",
    "_collect_forced_true_coverage",
    # constants
    "_ELISION_ENUM_LIMIT",
    "_ELISION_PROOF_BUDGET",
    "_ELISION_BATCH_REMOVE",
    "_FORCED_TRUE_COMBO_LIMIT",
    "_DEFAULT_DT",
    "_MEMORY_EXCLUDED_PREFIXES",
}

INIT_NAMES = {
    "_elide_scan_local_stateful_dims",
}

# ── Helpers ───────────────────────────────────────────────────────────


def _node_name(node: ast.AST) -> str | None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name):
                return target.id
    return None


def _node_start_line(node: ast.AST) -> int:
    """Earliest source line including decorators."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        if node.decorator_list:
            return node.decorator_list[0].lineno
    return node.lineno


def _categorise(name: str) -> str:
    if name in ABSTRACT_NAMES:
        return "abstract"
    if name in CONCRETE_NAMES:
        return "concrete"
    if name in INIT_NAMES:
        return "init"
    raise ValueError(f"uncategorised top-level name: {name!r}")


def _collect_names_in_source(source: str) -> set[str]:
    """All identifiers referenced in *source* (AST Name nodes + attributes)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


# ── Import resolution ─────────────────────────────────────────────────


def _parse_imports(
    source_lines: list[str],
) -> tuple[list[str], list[str], list[str], int]:
    """Parse the import block and module docstring from the top of the file.

    Returns (docstring_lines, regular_imports, type_checking_imports, body_start_line).
    """
    tree = ast.parse("\n".join(source_lines))
    docstring_lines: list[str] = []
    regular_imports: list[str] = []
    type_checking_imports: list[str] = []
    body_start = 0

    in_type_checking = False
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            # module docstring
            start = node.lineno - 1
            end = node.end_lineno
            docstring_lines = source_lines[start:end]
            body_start = max(body_start, end)
            continue

        if isinstance(node, (ast.Import, ast.ImportFrom)):
            line = _get_source_block(source_lines, node)
            if in_type_checking:
                type_checking_imports.append(line)
            else:
                regular_imports.append(line)
            body_start = max(body_start, node.end_lineno)
            continue

        if isinstance(node, ast.If):
            # TYPE_CHECKING block
            test = node.test
            if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
                in_type_checking = True
                for sub in node.body:
                    if isinstance(sub, (ast.Import, ast.ImportFrom)):
                        line = _get_source_block(source_lines, sub)
                        type_checking_imports.append(line)
                in_type_checking = False
                body_start = max(body_start, node.end_lineno)
                continue

        # first non-import node
        break

    return docstring_lines, regular_imports, type_checking_imports, body_start


def _get_source_block(lines: list[str], node: ast.AST) -> str:
    """Extract source text for an AST node, including continuation lines."""
    start = node.lineno - 1
    end = node.end_lineno
    return "\n".join(lines[start:end])


def _filter_imports(
    imports: list[str], needed_names: set[str]
) -> list[str]:
    """Keep only import lines that provide at least one needed name."""
    kept: list[str] = []
    for imp_text in imports:
        try:
            tree = ast.parse(imp_text)
        except SyntaxError:
            kept.append(imp_text)
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                # Keep if any imported name is needed
                if any(
                    (alias.asname or alias.name) in needed_names
                    for alias in node.names
                ):
                    kept.append(imp_text)
                    break
                # Also keep `from __future__` always
                if node.module == "__future__":
                    kept.append(imp_text)
                    break
            elif isinstance(node, ast.Import):
                if any(
                    (alias.asname or alias.name.split(".")[0]) in needed_names
                    for alias in node.names
                ):
                    kept.append(imp_text)
                    break
    return kept


# ── Main ──────────────────────────────────────────────────────────────


def main() -> None:
    source = SRC.read_text(encoding="utf-8")
    lines = source.splitlines()
    tree = ast.parse(source)

    # 1. Parse imports and docstring
    docstring_lines, regular_imports, type_checking_imports, import_end = _parse_imports(lines)

    # 2. Collect top-level body nodes (after imports) and categorise
    chunks: dict[str, list[str]] = {"abstract": [], "concrete": [], "init": []}
    body_nodes: list[tuple[str, ast.AST]] = []

    for node in ast.iter_child_nodes(tree):
        name = _node_name(node)
        if name is None:
            continue
        start = _node_start_line(node)
        if start <= import_end:
            continue
        body_nodes.append((name, node))

    # Sort by start line
    body_nodes.sort(key=lambda pair: _node_start_line(pair[1]))

    # 3. Extract source blocks with intervening comments
    for i, (name, node) in enumerate(body_nodes):
        cat = _categorise(name)

        # Start: include any comment/blank lines between previous node and this one
        if i == 0:
            block_start = import_end
        else:
            _, prev_node = body_nodes[i - 1]
            block_start = prev_node.end_lineno

        node_start = _node_start_line(node)
        node_end = node.end_lineno

        # Grab interstitial lines (comments/blanks between nodes)
        interstitial = lines[block_start:node_start - 1]
        body_text = lines[node_start - 1:node_end]

        chunk = "\n".join(interstitial + body_text)
        chunks[cat].append(chunk)

    # 4. Build each file with filtered imports
    abstract_body = "\n\n".join(chunks["abstract"])
    concrete_body = "\n\n".join(chunks["concrete"])
    init_body = "\n\n".join(chunks["init"])

    abstract_names_used = _collect_names_in_source(abstract_body)
    concrete_names_used = _collect_names_in_source(concrete_body)
    init_names_used = _collect_names_in_source(init_body)

    # Cross-module imports
    abstract_cross: list[str] = []
    concrete_cross: list[str] = []
    init_cross: list[str] = []

    # init needs from .abstract and .concrete
    init_from_abstract = sorted(ABSTRACT_NAMES & init_names_used)
    init_from_concrete = sorted(CONCRETE_NAMES & init_names_used)
    if init_from_abstract:
        init_cross.append(f"from .abstract import {', '.join(init_from_abstract)}")
    if init_from_concrete:
        init_cross.append(f"from .concrete import {', '.join(init_from_concrete)}")

    # concrete might need abstract types
    concrete_from_abstract = sorted(ABSTRACT_NAMES & concrete_names_used)
    if concrete_from_abstract:
        concrete_cross.append(f"from .abstract import {', '.join(concrete_from_abstract)}")

    # abstract might need concrete constants (unlikely but check)
    abstract_from_concrete = sorted(CONCRETE_NAMES & abstract_names_used)
    if abstract_from_concrete:
        abstract_cross.append(f"from .concrete import {', '.join(abstract_from_concrete)}")

    # Filter stdlib/third-party imports per file
    abstract_regular = _filter_imports(regular_imports, abstract_names_used)
    concrete_regular = _filter_imports(regular_imports, concrete_names_used)
    init_regular = _filter_imports(regular_imports, init_names_used)

    abstract_tc = _filter_imports(type_checking_imports, abstract_names_used)
    concrete_tc = _filter_imports(type_checking_imports, concrete_names_used)
    init_tc = _filter_imports(type_checking_imports, init_names_used)

    # 5. Assemble files
    def _assemble(
        docstring: str | None,
        regular: list[str],
        cross: list[str],
        tc: list[str],
        body: str,
    ) -> str:
        parts: list[str] = []
        if docstring:
            parts.append(docstring)
            parts.append("")
        parts.append("from __future__ import annotations")
        parts.append("")
        if regular:
            parts.append("\n".join(regular))
            parts.append("")
        if cross:
            parts.append("\n".join(cross))
            parts.append("")
        if tc:
            parts.append("if TYPE_CHECKING:")
            for line in tc:
                parts.append(textwrap.indent(line, "    "))
            parts.append("")
        parts.append(body)
        parts.append("")  # trailing newline
        return "\n".join(parts)

    # Remove `from __future__ import annotations` from filtered lists (we add it ourselves)
    abstract_regular = [i for i in abstract_regular if "__future__" not in i]
    concrete_regular = [i for i in concrete_regular if "__future__" not in i]
    init_regular = [i for i in init_regular if "__future__" not in i]

    abstract_src = _assemble(
        '"""Abstract provenance analysis for state-key elision."""',
        abstract_regular,
        abstract_cross,
        abstract_tc,
        abstract_body,
    )
    concrete_src = _assemble(
        '"""Concrete kernel proofs for state-key elision."""',
        concrete_regular,
        concrete_cross,
        concrete_tc,
        concrete_body,
    )

    # init also gets re-exports for backwards compat
    init_reexports = [
        "from .concrete import _collect_forced_true_coverage, _ConcreteStateElider  # noqa: F401",
    ]

    init_src = _assemble(
        '"""Two-phase state-key elision: abstract pre-filter then concrete kernel proofs."""',
        init_regular,
        init_cross + init_reexports,
        init_tc,
        init_body,
    )

    # 6. Write output
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "__init__.py").write_text(init_src, encoding="utf-8")
    (OUT_DIR / "abstract.py").write_text(abstract_src, encoding="utf-8")
    (OUT_DIR / "concrete.py").write_text(concrete_src, encoding="utf-8")

    # Report
    for name in ("__init__.py", "abstract.py", "concrete.py"):
        path = OUT_DIR / name
        n = len(path.read_text(encoding="utf-8").splitlines())
        print(f"  {path.relative_to(SRC.parent.parent.parent.parent.parent)}: {n} lines")

    print(f"\nDone. Now:")
    print(f"  1. Delete {SRC.relative_to(SRC.parent.parent.parent.parent.parent)}")
    print(f"  2. Fix monkeypatch target in test_prove_passes.py")
    print(f"  3. Run: make lint && make test")


if __name__ == "__main__":
    main()
