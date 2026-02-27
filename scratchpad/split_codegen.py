#!/usr/bin/env python3
"""Split codegen.py into a codegen/ package, re-exporting everything.

Usage:
    python split_codegen.py [--dry-run]

This script:
  1. Reads src/pyrung/circuitpy/codegen.py
  2. Splits it into sub-modules inside src/pyrung/circuitpy/codegen/
  3. Creates an __init__.py that re-exports the original __all__ names
  4. Backs up the original file to codegen.py.bak
  5. Removes the original codegen.py (since codegen/ replaces it)

Sub-module breakdown:
  - _constants.py : module-level constants only (no function/class deps)
  - _util.py      : small private helpers (no compile/render/generate deps)
  - context.py    : SlotBinding, BlockBinding, CodegenContext
  - compile.py    : compile_* public API + all _compile_* helpers + helpers
                    that depend on compile/context (formerly mis-bucketed)
  - render.py     : _render_* functions
  - generate.py   : generate_circuitpy entry point

Lessons encoded in this script:
  - Use node.decorator_list to capture @dataclass etc. (not just node.lineno)
  - Handle ast.AnnAssign (typed assignments like `x: dict[...] = {...}`)
  - Constants bucket is ONLY for simple assignments with no function/class deps
  - Cross-module imports use TYPE_CHECKING guards to avoid circular imports
  - __init__.py only re-exports the original __all__ names (no private bloat)
"""

from __future__ import annotations

import ast
import os
import shutil
import sys

SRC = os.path.join("src", "pyrung", "circuitpy", "codegen.py")
PKG = os.path.join("src", "pyrung", "circuitpy", "codegen")

# ---------------------------------------------------------------------------
# Categorisation rules
# ---------------------------------------------------------------------------

CONTEXT_NAMES = {"SlotBinding", "BlockBinding", "CodegenContext"}
GENERATE_NAMES = {"generate_circuitpy"}
RENDER_NAMES = {
    "_render_code",
    "_render_helper_section",
    "_render_embedded_functions",
    "_render_subroutine_functions",
    "_render_main_function",
    "_render_io_helpers",
    "_render_scan_loop",
}
COMPILE_PUBLIC = {
    "compile_condition",
    "compile_expression",
    "compile_instruction",
    "compile_rung",
}
UTIL_NAMES = {
    "_indent_body",
    "_bool_literal",
    "_mangle_symbol",
    "_subroutine_symbol",
    "_io_kind",
    "_source_location",
    "_first_defined_name",
    "_global_line",
    "_ret_defaults_literal",
    "_ret_types_literal",
    "_optional_value_type_name",
    "_optional_range_type_name",
    "_value_type_name",
    "_range_type_name",
    "_range_reverse",
    "_static_range_length",
    "_coil_target_default",
}


def categorize(name: str) -> str:
    """Return the target sub-module for a top-level name."""
    if name in CONTEXT_NAMES:
        return "context"
    if name in GENERATE_NAMES:
        return "generate"
    if name in RENDER_NAMES:
        return "render"
    if name in COMPILE_PUBLIC or name.startswith("_compile"):
        return "compile"
    if name in UTIL_NAMES:
        return "_util"
    return "_constants"


def _node_start_line(node: ast.AST) -> int:
    """Return the true start line, including decorators."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        if node.decorator_list:
            return node.decorator_list[0].lineno
    return node.lineno  # type: ignore[attr-defined]


def _node_name(node: ast.AST) -> str | None:
    """Extract the defined name from a top-level AST node."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name):
                return target.id
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id
    return None


def _is_pure_constant(node: ast.AST) -> bool:
    """True if the node defines a simple constant (no function/class references).

    We check whether the RHS of the assignment references any Name nodes that
    look like function calls to other codegen internals.  A "constant" should
    only contain literals, stdlib calls (re.compile, etc.), or simple containers.
    In practice: if the value subtree contains a Call whose func is a Name
    that starts with '_compile', '_render', or matches a known internal, it's
    NOT a constant — it belongs with its dependents.
    """
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return False

    # Check if the value side references internal names
    value_node = None
    if isinstance(node, ast.Assign):
        value_node = node.value
    elif isinstance(node, ast.AnnAssign):
        value_node = node.value

    if value_node is None:
        return True  # bare annotation, treat as constant

    for child in ast.walk(value_node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name):
                # If it calls an internal function, it's not a constant
                if func.id.startswith(("_compile", "_render", "compile_", "CodegenContext")):
                    return False
    return True


# ---------------------------------------------------------------------------
# Dependency-aware import builder
# ---------------------------------------------------------------------------

# Desired import order (no cycles): _constants -> _util -> context -> compile -> render -> generate
MODULE_ORDER = ["_constants", "_util", "context", "compile", "render", "generate"]


def _build_imports(
    mod_name: str,
    body_source: str,
    original_import_block: str,
    defined_names: dict[str, set[str]],
) -> str:
    """Build the import section for a sub-module.

    - Starts with the original external import block.
    - Adds intra-package imports for sibling names actually used.
    - Uses TYPE_CHECKING for imports from modules that come AFTER this one
      in the dependency order (to break potential cycles).
    """
    used = set()
    try:
        t = ast.parse(body_source)
    except SyntaxError:
        t = None
    if t:
        for node in ast.walk(t):
            if isinstance(node, ast.Name):
                used.add(node.id)

    my_order = MODULE_ORDER.index(mod_name) if mod_name in MODULE_ORDER else 999

    runtime_imports: list[str] = []
    typecheck_imports: list[str] = []

    for other_mod, other_names in sorted(defined_names.items()):
        if other_mod == mod_name:
            continue
        needed = sorted(used & other_names)
        if not needed:
            continue

        other_order = MODULE_ORDER.index(other_mod) if other_mod in MODULE_ORDER else 999
        names_str = ", ".join(needed)
        line = f"from pyrung.circuitpy.codegen.{other_mod} import {names_str}"

        if other_order > my_order:
            # This would be a backwards/circular import — guard it
            typecheck_imports.append(line)
        else:
            runtime_imports.append(line)

    parts = [original_import_block.rstrip("\n")]

    if runtime_imports:
        parts.append("")
        parts.extend(runtime_imports)

    if typecheck_imports:
        # Need to ensure TYPE_CHECKING is imported
        parts.append("")
        parts.append("if TYPE_CHECKING:")
        for line in typecheck_imports:
            parts.append(f"    {line}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Main split logic
# ---------------------------------------------------------------------------

def split_codegen(dry_run: bool = False) -> None:
    if not os.path.isfile(SRC):
        print(f"ERROR: {SRC} not found. Run from the repo root.", file=sys.stderr)
        sys.exit(1)

    with open(SRC, encoding="utf-8") as f:
        source = f.read()
    lines = source.splitlines(keepends=True)

    tree = ast.parse(source, filename=SRC)

    # --- 1. Collect import block (raw text, preserving formatting) ---
    import_end = 0
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            import_end = node.end_lineno  # type: ignore[attr-defined]
    import_block = "".join(lines[:import_end])

    # --- 2. Extract original __all__ ---
    original_all: list[str] | None = None
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    # Evaluate the list literal
                    try:
                        original_all = ast.literal_eval(node.value)
                    except (ValueError, TypeError):
                        pass

    # --- 3. Partition top-level definitions into buckets ---
    # Each bucket entry: (start_line_0indexed, end_line_0indexed_exclusive, name)
    buckets: dict[str, list[tuple[int, int, str]]] = {
        k: [] for k in MODULE_ORDER
    }

    for node in ast.iter_child_nodes(tree):
        # Skip imports
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        # Skip __all__
        name = _node_name(node)
        if name == "__all__":
            continue
        # Skip module docstring
        if isinstance(node, ast.Expr) and isinstance(node.value, (ast.Constant, ast.Str)):
            continue
        if name is None:
            continue

        cat = categorize(name)

        # If categorized as _constants but it's not a pure constant, promote to compile
        if cat == "_constants" and not _is_pure_constant(node):
            cat = "compile"

        start = _node_start_line(node) - 1  # 0-indexed, includes decorators
        end = node.end_lineno  # type: ignore[attr-defined]  # 1-indexed -> exclusive
        buckets[cat].append((start, end, name))

    # --- 4. Build source chunks for each sub-module ---
    module_sources: dict[str, str] = {}
    names_per_module: dict[str, list[str]] = {k: [] for k in buckets}

    for mod_name, entries in buckets.items():
        if not entries:
            continue
        chunks: list[str] = []
        for start, end, name in entries:
            chunk = "".join(lines[start:end])
            chunks.append(chunk)
            names_per_module[mod_name].append(name)
        module_sources[mod_name] = "\n\n".join(chunks)

    # --- 5. Build defined-names map for cross-module import resolution ---
    defined: dict[str, set[str]] = {
        mod: set(names) for mod, names in names_per_module.items()
    }

    # --- 6. Assemble each module file with proper imports ---
    final_sources: dict[str, str] = {}
    for mod_name, body in module_sources.items():
        header = _build_imports(mod_name, body, import_block, defined)

        # Check if we need to add TYPE_CHECKING import
        if "if TYPE_CHECKING:" in header and "TYPE_CHECKING" not in import_block:
            # Inject TYPE_CHECKING into the typing import
            if "from typing import " in header:
                header = header.replace(
                    "from typing import ",
                    "from typing import TYPE_CHECKING, ",
                    1,
                )
            elif "from typing import" not in header:
                # Add it after __future__
                header = header.replace(
                    "from __future__ import annotations\n",
                    "from __future__ import annotations\n\nfrom typing import TYPE_CHECKING\n",
                    1,
                )

        final_sources[mod_name] = f"{header}\n\n\n{body.rstrip()}\n"

    # --- 7. Build __init__.py ---
    if original_all:
        all_names = original_all
    else:
        # Fallback: public names only
        all_names = sorted(
            n for names in names_per_module.values() for n in names if not n.startswith("_")
        )

    # Map each __all__ name to its module
    name_to_mod: dict[str, str] = {}
    for mod_name, names in names_per_module.items():
        for n in names:
            name_to_mod[n] = mod_name

    init_lines = [
        '"""CircuitPython code generation (feature-complete v1)."""',
        "",
        "from __future__ import annotations",
        "",
    ]

    # Group __all__ names by source module for clean imports
    by_mod: dict[str, list[str]] = {}
    for name in all_names:
        mod = name_to_mod.get(name)
        if mod:
            by_mod.setdefault(mod, []).append(name)

    for mod_name in MODULE_ORDER:
        names = by_mod.get(mod_name)
        if not names:
            continue
        if len(names) == 1:
            init_lines.append(f"from pyrung.circuitpy.codegen.{mod_name} import {names[0]}")
        else:
            init_lines.append(f"from pyrung.circuitpy.codegen.{mod_name} import (")
            for n in names:
                init_lines.append(f"    {n},")
            init_lines.append(")")

    init_lines.append("")
    init_lines.append("__all__ = [")
    for name in all_names:
        init_lines.append(f'    "{name}",')
    init_lines.append("]")
    init_lines.append("")

    init_source = "\n".join(init_lines)

    # ---------------------------------------------------------------------------
    # Output
    # ---------------------------------------------------------------------------
    if dry_run:
        print("=== DRY RUN ===")
        print(f"\nWould create package: {PKG}/")
        print(f"Would back up: {SRC} -> {SRC}.bak")
        print(f"Would remove: {SRC}")
        print(f"\nOriginal __all__: {all_names}")
        print("\nSub-modules:")
        for mod_name in MODULE_ORDER:
            if mod_name not in final_sources:
                continue
            body = final_sources[mod_name]
            line_count = body.count("\n") + 1
            names = names_per_module[mod_name]
            public = [n for n in names if not n.startswith("_")]
            private = [n for n in names if n.startswith("_")]
            print(f"  {mod_name}.py  ({line_count} lines, {len(public)} public, {len(private)} private)")
            for n in names:
                print(f"    {'  ' if n.startswith('_') else '* '}{n}")

        # Verify no names were lost
        original_names = set()
        for node in ast.iter_child_nodes(tree):
            n = _node_name(node)
            if n and n != "__all__":
                original_names.add(n)
        split_names = set()
        for names in names_per_module.values():
            split_names.update(names)
        missing = original_names - split_names
        extra = split_names - original_names
        if missing:
            print(f"\n  WARNING: {len(missing)} names MISSING from split: {sorted(missing)}")
        if extra:
            print(f"\n  WARNING: {len(extra)} extra names in split: {sorted(extra)}")
        if not missing and not extra:
            print(f"\n  All {len(original_names)} names accounted for.")

        return

    # Create package directory
    os.makedirs(PKG, exist_ok=True)

    # Write sub-modules
    for mod_name in MODULE_ORDER:
        if mod_name not in final_sources:
            continue
        path = os.path.join(PKG, f"{mod_name}.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(final_sources[mod_name])
        print(f"  wrote {path}")

    # Write __init__.py
    init_path = os.path.join(PKG, "__init__.py")
    with open(init_path, "w", encoding="utf-8") as f:
        f.write(init_source)
    print(f"  wrote {init_path}")

    # Back up and remove original
    bak = SRC + ".bak"
    shutil.copy2(SRC, bak)
    print(f"  backed up {SRC} -> {bak}")
    os.remove(SRC)
    print(f"  removed {SRC}")

    print(f"\nDone! codegen.py split into {len(final_sources)} sub-modules + __init__.py")
    print("Run 'make lint' then 'make test' to verify.")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    split_codegen(dry_run=dry_run)
