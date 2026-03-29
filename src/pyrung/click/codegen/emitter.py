from __future__ import annotations

import re
from typing import TYPE_CHECKING

from pyrung.click.codegen.constants import (
    _COMPARE_RE,
    _CONDITION_WRAPPERS,
    _DROP_KWARGS,
    _FUNC_RE,
    _OPERAND_PREFIXES,
)
from pyrung.click.codegen.models import (
    Leaf,
    Parallel,
    Series,
    SPNode,
    _AnalyzedRung,
    _InstructionInfo,
    _OperandCollection,
    _PinInfo,
    _StructureDecl,
    _SubroutineInfo,
)
from pyrung.click.codegen.utils import (
    _CLICK_FUNC_RE,
    _CLICK_FUNC_TO_PYTHON,
    _CLICK_PI_RE,
    _EXPR_FUNC_IMPORT_NAMES,
    _parse_af_args,
    _sub_operand,
    _sub_operand_kwarg,
)

if TYPE_CHECKING:
    from pyrung.click.tag_map import TagMap


# ---------------------------------------------------------------------------
# Phase 4: Generate Code
# ---------------------------------------------------------------------------


def _is_trailing_return(rung: _AnalyzedRung) -> bool:
    """True if rung is an unconditional return_early() — implicit in pyrung subroutines."""
    return (
        rung.condition_tree is None
        and len(rung.instructions) == 1
        and rung.instructions[0].af_token.startswith("return(")
        and rung.instructions[0].branch_tree is None
        and not rung.instructions[0].pins
    )


def _prescan_expr_funcs(
    rungs: list[_AnalyzedRung],
    collection: _OperandCollection,
    subroutines: list[_SubroutineInfo] | None,
) -> None:
    """Scan AF tokens for Click expression function names before emitting."""

    def _scan_token(token: str) -> None:
        # Check for Click-uppercase function names (SQRT, LSH, etc.)
        for m in _CLICK_FUNC_RE.finditer(token):
            py_name = _CLICK_FUNC_TO_PYTHON[m.group(1)]
            if py_name in _EXPR_FUNC_IMPORT_NAMES:
                collection.used_expr_funcs.add(py_name)
        if _CLICK_PI_RE.search(token):
            collection.used_expr_funcs.add("PI")

    for rung in rungs:
        for instr in rung.instructions:
            if instr.af_token:
                _scan_token(instr.af_token)
    if subroutines:
        for sub in subroutines:
            for rung in sub.analyzed:
                for instr in rung.instructions:
                    if instr.af_token:
                        _scan_token(instr.af_token)


def _generate_code(
    rungs: list[_AnalyzedRung],
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
    *,
    subroutines: list[_SubroutineInfo] | None = None,
    structured_map: TagMap | None = None,
) -> str:
    """Generate the complete Python source file."""
    # Pre-scan AF tokens for expression function names so imports are complete.
    _prescan_expr_funcs(rungs, collection, subroutines)

    lines: list[str] = []

    # Module docstring
    lines.append('"""Auto-generated pyrung program from laddercodec CSV."""')
    lines.append("")

    # Imports
    _emit_imports(lines, collection)
    lines.append("")

    # Tag declarations (skip structure-owned)
    has_flat_tags = any(op not in collection.structure_owned_operands for op in collection.tags)
    if has_flat_tags:
        lines.append("# --- Tags ---")
        _emit_tag_declarations(lines, collection)
        lines.append("")

    # Range declarations
    if collection.ranges:
        lines.append("# --- Ranges ---")
        _emit_range_declarations(lines, collection)
        lines.append("")

    # Structure declarations
    if collection.structures:
        lines.append("# --- Structures ---")
        _emit_structure_declarations(lines, collection)
        lines.append("")

    # Program body
    lines.append("# --- Program ---")
    _emit_program(
        lines,
        rungs,
        collection,
        nicknames,
        subroutines=subroutines,
        structured_map=structured_map,
    )
    lines.append("")

    # Tag map
    lines.append("# --- Tag Map ---")
    _emit_tag_map(lines, collection)
    lines.append("")

    return "\n".join(lines) + "\n"


def _emit_imports(lines: list[str], collection: _OperandCollection) -> None:
    """Emit import statements."""
    # Core imports
    core_imports: list[str] = ["Program", "Rung"]

    # Block/TagType if ranges are used
    if collection.ranges:
        core_imports.append("Block")
        core_imports.append("TagType")

    # Structure imports
    has_named_array = any(s.structure_type == "named_array" for s in collection.structures)
    has_udt = any(s.structure_type == "udt" for s in collection.structures)
    has_retentive_field = any(
        any(v for v in s.field_retentive.values()) for s in collection.structures
    )
    if has_named_array:
        core_imports.append("named_array")
    if has_udt:
        core_imports.append("udt")
    if has_retentive_field:
        core_imports.append("Field")

    # Tag types
    for tt in sorted(collection.used_types):
        if tt not in core_imports:
            core_imports.append(tt)

    # Condition helpers
    if collection.has_any_of:
        core_imports.append("any_of")
    if collection.has_all_of:
        core_imports.append("all_of")
    for cw in sorted(collection.used_conditions):
        core_imports.append(cw)

    # Instructions
    instruction_map = {
        "out": "out",
        "latch": "latch",
        "reset": "reset",
        "copy": "copy",
        "blockcopy": "blockcopy",
        "fill": "fill",
        "math": "calc",
        "on_delay": "on_delay",
        "off_delay": "off_delay",
        "count_up": "count_up",
        "count_down": "count_down",
        "shift": "shift",
        "search": "search",
        "pack_bits": "pack_bits",
        "pack_words": "pack_words",
        "pack_text": "pack_text",
        "unpack_to_bits": "unpack_to_bits",
        "unpack_to_words": "unpack_to_words",
        "event_drum": "event_drum",
        "time_drum": "time_drum",
        "call": "call",
        "return": "return_early",
    }
    for instr_name in sorted(collection.used_instructions):
        import_name = instruction_map.get(instr_name)
        if import_name and import_name not in core_imports:
            core_imports.append(import_name)

    if collection.has_branch:
        core_imports.append("branch")
    if collection.has_forloop:
        core_imports.append("forloop")
    if collection.has_subroutine:
        core_imports.append("subroutine")

    # Time units
    for tu in sorted(collection.used_time_units):
        core_imports.append(tu)

    # Copy converters
    for cc in sorted(collection.used_copy_converters):
        core_imports.append(cc)

    lines.append(f"from pyrung import {', '.join(core_imports)}")

    # Click imports
    click_imports: list[str] = ["TagMap"]
    for bv in sorted(collection.used_blocks):
        click_imports.append(bv)
    if collection.has_modbus_target:
        click_imports.append("ModbusTcpTarget")
    if collection.has_modbus_rtu_target:
        click_imports.append("ModbusRtuTarget")
    if collection.has_modbus_address:
        click_imports.append("ModbusAddress")
    if collection.has_subroutine:
        pass
    if "send" in collection.used_instructions:
        click_imports.append("send")
    if "receive" in collection.used_instructions:
        click_imports.append("receive")

    lines.append(f"from pyrung.click import {', '.join(click_imports)}")

    # Expression function imports (sqrt, lsh, etc.)
    if collection.used_expr_funcs:
        expr_imports = sorted(collection.used_expr_funcs)
        lines.append(f"from pyrung.core.expression import {', '.join(expr_imports)}")

    if collection.has_system_operands:
        lines.append("from pyrung.core.system_points import system")


def _emit_tag_declarations(
    lines: list[str],
    collection: _OperandCollection,
    *,
    suppress_comments: bool = False,
) -> None:
    """Emit tag variable declarations."""
    # Sort by block order, then by index
    block_order = {bv: i for i, (_, _, bv) in enumerate(_OPERAND_PREFIXES)}
    sorted_tags = sorted(
        collection.tags.values(),
        key=lambda t: (block_order.get(t.block_var, 99), t.block_index),
    )
    for decl in sorted_tags:
        if decl.operand in collection.structure_owned_operands:
            continue
        line = f'{decl.var_name} = {decl.tag_type}("{decl.tag_name}")'
        if decl.comment and not suppress_comments:
            line += decl.comment
        lines.append(line)


def _emit_range_declarations(lines: list[str], collection: _OperandCollection) -> None:
    """Emit logical Block declarations for ranges."""
    for decl in sorted(collection.ranges.values(), key=lambda r: r.operand_str):
        lines.append(
            f'{decl.var_name} = Block("{decl.var_name}", TagType.{decl.tag_type}, '
            f"{decl.start}, {decl.end})"
        )


def _emit_structure_declarations(lines: list[str], collection: _OperandCollection) -> None:
    """Emit @named_array / @udt class declarations."""
    for decl in collection.structures:
        if decl.structure_type == "named_array":
            _emit_named_array_decl(lines, decl)
        elif decl.structure_type == "udt":
            _emit_udt_decl(lines, decl)


def _emit_named_array_decl(lines: list[str], decl: _StructureDecl) -> None:
    """Emit a @named_array decorator + class."""
    stride_part = ""
    if decl.stride is not None and decl.stride != len(decl.fields):
        stride_part = f", stride={decl.stride}"
    count_part = f"count={decl.count}" if decl.count > 1 else ""
    deco_args = decl.base_type or "Int"
    if count_part:
        deco_args += f", {count_part}"
    deco_args += stride_part
    lines.append(f"@named_array({deco_args})")
    lines.append(f"class {decl.name}:")
    for field_name, _type_name, default in decl.fields:
        retentive = decl.field_retentive.get(field_name, False)
        if retentive:
            lines.append(f"    {field_name} = Field(retentive=True)")
        else:
            default_repr = _format_field_default(default)
            lines.append(f"    {field_name} = {default_repr}")


def _emit_udt_decl(lines: list[str], decl: _StructureDecl) -> None:
    """Emit a @udt decorator + class."""
    count_part = f"count={decl.count}" if decl.count > 1 else ""
    lines.append(f"@udt({count_part})")
    lines.append(f"class {decl.name}:")
    for field_name, type_name, default in decl.fields:
        retentive = decl.field_retentive.get(field_name, False)
        if retentive:
            lines.append(f"    {field_name}: {type_name} = Field(retentive=True)")
        else:
            default_repr = _format_field_default(default)
            lines.append(f"    {field_name}: {type_name} = {default_repr}")


def _format_field_default(default: object) -> str:
    """Format a field default value for code emission."""
    if isinstance(default, bool):
        return "True" if default else "False"
    if isinstance(default, float):
        return repr(default)
    if isinstance(default, int):
        return str(default)
    if isinstance(default, str):
        return repr(default)
    return repr(default)


def _emit_program(
    lines: list[str],
    rungs: list[_AnalyzedRung],
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
    *,
    subroutines: list[_SubroutineInfo] | None = None,
    structured_map: TagMap | None = None,
    call_func_map: dict[str, str] | None = None,
) -> None:
    """Emit the program body."""
    lines.append("with Program() as logic:")

    if not rungs and not subroutines:
        lines.append("    pass")
        return

    _emit_rung_sequence(
        lines,
        rungs,
        collection,
        nicknames,
        indent=1,
        structured_map=structured_map,
        call_func_map=call_func_map,
    )

    # Emit subroutine blocks
    if subroutines:
        for sub in subroutines:
            sub_rungs = sub.analyzed
            if sub_rungs and _is_trailing_return(sub_rungs[-1]):
                sub_rungs = sub_rungs[:-1]
            lines.append("")
            lines.append(f'    with subroutine("{sub.name}"):')
            if sub_rungs:
                _emit_rung_sequence(
                    lines,
                    sub_rungs,
                    collection,
                    nicknames,
                    indent=2,
                    structured_map=structured_map,
                    call_func_map=call_func_map,
                )
            else:
                lines.append("        pass")


def _emit_rung_sequence(
    lines: list[str],
    rungs: list[_AnalyzedRung],
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
    indent: int,
    structured_map: TagMap | None = None,
    call_func_map: dict[str, str] | None = None,
) -> None:
    """Emit a sequence of rungs (main program or subroutine body)."""
    if not rungs:
        pad = "    " * indent
        lines.append(f"{pad}pass")
        return

    i = 0
    while i < len(rungs):
        rung = rungs[i]

        if rung.is_forloop_start:
            _emit_forloop(
                lines,
                rungs,
                i,
                collection,
                nicknames,
                indent=indent,
                structured_map=structured_map,
                call_func_map=call_func_map,
            )
            # Skip to after next()
            i += 1
            while i < len(rungs) and not rungs[i].is_forloop_next:
                i += 1
            i += 1  # skip the next() rung
            continue

        if rung.is_forloop_next:
            # Should have been consumed by forloop handler
            i += 1
            continue

        _emit_rung(
            lines,
            rung,
            collection,
            nicknames,
            indent=indent,
            structured_map=structured_map,
            call_func_map=call_func_map,
        )
        i += 1


def _emit_forloop(
    lines: list[str],
    rungs: list[_AnalyzedRung],
    start_idx: int,
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
    indent: int,
    structured_map: TagMap | None = None,
    call_func_map: dict[str, str] | None = None,
) -> None:
    """Emit a for/next block."""
    pad = "    " * indent
    for_rung = rungs[start_idx]

    # Build the rung conditions
    conditions_str = _build_conditions_str(for_rung, collection, nicknames, structured_map)

    # Parse for() token to get count and kwargs
    af = for_rung.instructions[0].af_token if for_rung.instructions else ""
    match = _FUNC_RE.match(af)
    if match:
        args_str = match.group(3) or ""
        args, kwargs = _parse_af_args(args_str)
        count_arg = _sub_operand(args[0], collection, nicknames, structured_map) if args else "1"
        kw_parts = []
        for k, v in kwargs:
            rendered_v = _sub_operand_kwarg(k, v, collection, nicknames, structured_map)
            kw_parts.append(f"{k}={rendered_v}")
    else:
        count_arg = "1"
        kw_parts = []

    # Emit rung with forloop
    _emit_rung_header(lines, for_rung, conditions_str, indent)

    forloop_args = count_arg
    if kw_parts:
        forloop_args += ", " + ", ".join(kw_parts)
    lines.append(f"{pad}    with forloop({forloop_args}):")

    # Body rungs — forloop body instructions are bare (not wrapped in Rung)
    body_pad = "    " * (indent + 2)
    body_count = 0
    for j in range(start_idx + 1, len(rungs)):
        if rungs[j].is_forloop_next:
            break
        body_rung = rungs[j]
        for instr in body_rung.instructions:
            _emit_instruction(
                lines, instr, collection, nicknames, indent + 2, structured_map, call_func_map
            )
        body_count += 1

    if body_count == 0:
        lines.append(f"{body_pad}pass")


def _emit_rung_header(
    lines: list[str],
    rung: _AnalyzedRung,
    conditions_str: str,
    indent: int,
) -> None:
    """Emit 'with Rung(...):' or 'with Rung(...) as r:' + comment lines."""
    pad = "    " * indent
    as_clause = " as r" if rung.comment else ""
    continued = ".continued()" if rung.is_continued else ""
    if conditions_str:
        lines.append(f"{pad}with Rung({conditions_str}){continued}{as_clause}:")
    else:
        lines.append(f"{pad}with Rung(){continued}{as_clause}:")
    if rung.comment:
        _emit_comment(lines, rung.comment, indent + 1)


def _emit_rung(
    lines: list[str],
    rung: _AnalyzedRung,
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
    indent: int,
    structured_map: TagMap | None = None,
    call_func_map: dict[str, str] | None = None,
) -> None:
    """Emit a single rung."""
    pad = "    " * indent

    # Filter out bare NOP tokens — they become empty rungs (pass).
    real_instructions = [i for i in rung.instructions if i.af_token != "NOP"]

    if not real_instructions:
        # Empty or NOP-only rung — preserve with pass (comment-only rungs survive).
        if not rung.instructions and not rung.comment:
            return  # truly empty, no comment — skip
        conditions_str = _build_conditions_str(rung, collection, nicknames, structured_map)
        _emit_rung_header(lines, rung, conditions_str, indent)
        lines.append(f"{pad}    pass")
        return

    conditions_str = _build_conditions_str(rung, collection, nicknames, structured_map)

    # Check if we need branch() blocks
    has_branches = any(instr.branch_tree is not None for instr in real_instructions)
    multi_output = len(real_instructions) > 1

    if has_branches and multi_output:
        _emit_rung_header(lines, rung, conditions_str, indent)

        for instr in real_instructions:
            if instr.branch_tree is not None:
                branch_cond = _render_sp_node(
                    instr.branch_tree, collection, nicknames, structured_map
                )
                lines.append(f"{pad}    with branch({branch_cond}):")
                _emit_instruction(
                    lines, instr, collection, nicknames, indent + 2, structured_map, call_func_map
                )
            else:
                _emit_instruction(
                    lines, instr, collection, nicknames, indent + 1, structured_map, call_func_map
                )
    elif multi_output and not has_branches:
        _emit_rung_header(lines, rung, conditions_str, indent)

        for instr in real_instructions:
            _emit_instruction(
                lines, instr, collection, nicknames, indent + 1, structured_map, call_func_map
            )
    else:
        _emit_rung_header(lines, rung, conditions_str, indent)

        for instr in real_instructions:
            _emit_instruction(
                lines, instr, collection, nicknames, indent + 1, structured_map, call_func_map
            )


def _render_sp_node(
    node: SPNode,
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
    structured_map: TagMap | None = None,
) -> str:
    """Render an SP tree node to Python condition syntax."""
    if isinstance(node, Leaf):
        return _render_condition(node.label, collection, nicknames, structured_map)

    if isinstance(node, Series):
        return ", ".join(
            _render_sp_node(child, collection, nicknames, structured_map) for child in node.children
        )

    if isinstance(node, Parallel):
        parts: list[str] = []
        for child in node.children:
            rendered = _render_sp_node(child, collection, nicknames, structured_map)
            if isinstance(child, Series) and len(child.children) > 1:
                parts.append(f"all_of({rendered})")
            else:
                parts.append(rendered)
        if len(parts) == 1:
            return parts[0]
        return f"any_of({', '.join(parts)})"

    return ""


def _build_conditions_str(
    rung: _AnalyzedRung,
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
    structured_map: TagMap | None = None,
) -> str:
    """Build the condition string for a Rung() constructor."""
    if rung.condition_tree is None:
        return ""
    return _render_sp_node(rung.condition_tree, collection, nicknames, structured_map)


def _render_condition(
    token: str,
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
    structured_map: TagMap | None = None,
) -> str:
    """Render a condition token to Python expression."""
    # Negation
    if token.startswith("~"):
        inner = _sub_operand(token[1:], collection, nicknames, structured_map)
        return f"~{inner}"

    # Function wrapper: rise(X001), fall(X001), immediate(X001)
    match = _FUNC_RE.match(token)
    if match:
        func_name = match.group(2)
        args_str = match.group(3) or ""
        if func_name in _CONDITION_WRAPPERS:
            inner = _sub_operand(args_str, collection, nicknames, structured_map)
            return f"{func_name}({inner})"

    # Comparison: DS1==5, DS1!=DS2
    cmp_match = _COMPARE_RE.match(token)
    if cmp_match:
        left = _sub_operand(cmp_match.group(1), collection, nicknames, structured_map)
        op = cmp_match.group(2)
        right = _sub_operand(cmp_match.group(3), collection, nicknames, structured_map)
        return f"{left} {op} {right}"

    # Plain operand
    return _sub_operand(token, collection, nicknames, structured_map)


def _emit_instruction(
    lines: list[str],
    instr: _InstructionInfo,
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
    indent: int,
    structured_map: TagMap | None = None,
    call_func_map: dict[str, str] | None = None,
) -> None:
    """Emit a single instruction call."""
    pad = "    " * indent
    af = instr.af_token

    if not af:
        return

    rendered = _render_af_token(af, collection, nicknames, structured_map, call_func_map)

    # Handle pin chaining
    pin_strs: list[str] = []
    for pin in instr.pins:
        pin_rendered = _render_pin(pin, collection, nicknames, structured_map)
        pin_strs.append(pin_rendered)

    if pin_strs:
        lines.append(f"{pad}{rendered}{''.join(pin_strs)}")
    else:
        lines.append(f"{pad}{rendered}")


_SEARCH_OP_RE = re.compile(r"^(.+?)\s+(==|!=|<=|>=|<|>)\s+(.+)$")


def _render_search_token(
    args: list[str],
    kwargs: list[tuple[str, str]],
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
    structured_map: TagMap | None = None,
) -> str:
    """Render search CSV token as ``search(range <op> value, ...)``."""
    # First positional arg is the comparison expression: "RANGE OP VALUE"
    raw = args[0] if args else ""
    m = _SEARCH_OP_RE.match(raw)
    if m:
        range_expr = _sub_operand(m.group(1), collection, nicknames, structured_map)
        op = m.group(2)
        value_expr = _sub_operand(m.group(3), collection, nicknames, structured_map)
        comparison = f"{range_expr} {op} {value_expr}"
    else:
        comparison = _sub_operand(raw, collection, nicknames, structured_map)
    rest: list[str] = []
    for key, value in kwargs:
        if key in _DROP_KWARGS:
            continue
        rendered_v = _sub_operand_kwarg(key, value, collection, nicknames, structured_map)
        rest.append(f"{key}={rendered_v}")
    return f"search({', '.join([comparison, *rest])})"


def _render_af_token(
    token: str,
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
    structured_map: TagMap | None = None,
    call_func_map: dict[str, str] | None = None,
) -> str:
    """Render an AF token to a pyrung DSL call."""
    match = _FUNC_RE.match(token)
    if not match:
        # Safeguard: reject unknown bare text that isn't a recognised operand.
        # Known operands are substituted normally; anything else is an error
        # that would produce invalid Python (bare undefined names).
        sub = _sub_operand(token, collection, nicknames, structured_map)
        if sub == token and token not in collection.tags:
            raise ValueError(
                f"Unrecognised AF token {token!r} — not a known instruction or operand. "
                f"If this is a new Click instruction, add it to the codegen."
            )
        return sub

    func_name = match.group(2)
    args_str = match.group(3) or ""

    # Map CSV token names → Python DSL names
    _CSV_TO_DSL = {"return": "return_early", "math": "calc"}
    py_func = _CSV_TO_DSL.get(func_name, func_name)

    # In project mode, call("name") → call(func_name) using bare identifier
    if func_name == "call" and call_func_map is not None and args_str:
        # Strip surrounding quotes from the subroutine name
        sub_name = args_str.strip().strip('"')
        func_id = call_func_map.get(sub_name)
        if func_id is not None:
            return f"call({func_id})"

    # raw(ClassName,fields...) → raw("ClassName", 'fields...')
    # Single-quote fields because values may contain double quotes (e.g. "TEST").
    if func_name == "raw":
        parts = args_str.split(",", 1)
        class_name = parts[0].strip()
        fields = parts[1].strip() if len(parts) > 1 else ""
        return f"""raw("{class_name}", '{fields}')"""

    if not args_str:
        return f"{py_func}()"

    args, kwargs = _parse_af_args(args_str)

    # search("cond",value=V,search_range=R,...) → search(R <op> V, ...)
    if func_name == "search":
        return _render_search_token(args, kwargs, collection, nicknames, structured_map)

    rendered_parts: list[str] = []
    for arg in args:
        rendered_parts.append(_sub_operand(arg, collection, nicknames, structured_map))
    for key, value in kwargs:
        if key in _DROP_KWARGS:
            continue
        rendered_v = _sub_operand_kwarg(key, value, collection, nicknames, structured_map)
        rendered_parts.append(f"{key}={rendered_v}")

    return f"{py_func}({', '.join(rendered_parts)})"


def _render_pin(
    pin: _PinInfo,
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
    structured_map: TagMap | None = None,
) -> str:
    """Render a pin as a chained method call."""
    if pin.conditions:
        cond = _render_condition(pin.conditions[0], collection, nicknames, structured_map)
        if pin.arg:
            arg = _sub_operand(pin.arg, collection, nicknames, structured_map)
            return f".{pin.name}({cond}, {arg})"
        return f".{pin.name}({cond})"
    if pin.arg:
        arg = _sub_operand(pin.arg, collection, nicknames, structured_map)
        return f".{pin.name}({arg})"
    return f".{pin.name}()"


def _emit_comment(lines: list[str], comment: str, indent: int) -> None:
    """Emit a rung comment assignment."""
    pad = "    " * indent
    if "\n" in comment:
        # Multi-line → triple-quoted string
        escaped = comment.replace("\\", "\\\\")
        parts = escaped.split("\n")
        lines.append(f'{pad}r.comment = """\\')
        for part in parts[:-1]:
            lines.append(part)
        lines.append(f'{parts[-1]}"""')
    else:
        escaped = comment.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'{pad}r.comment = "{escaped}"')


def _emit_tag_map(lines: list[str], collection: _OperandCollection) -> None:
    """Emit the TagMap constructor."""
    has_structures = bool(collection.structures)

    if has_structures:
        lines.append("mapping = TagMap([")
    else:
        lines.append("mapping = TagMap({")

    block_order = {bv: i for i, (_, _, bv) in enumerate(_OPERAND_PREFIXES)}
    sorted_tags = sorted(
        collection.tags.values(),
        key=lambda t: (block_order.get(t.block_var, 99), t.block_index),
    )

    if has_structures:
        # Structure-level map_to entries
        for sdecl in collection.structures:
            if sdecl.structure_type == "named_array":
                # Named arrays use a single contiguous range
                lines.append(
                    f"    *{sdecl.name}.map_to("
                    f"{sdecl.hw_block_var}.select({sdecl.hw_start}, {sdecl.hw_end})),"
                )
            else:
                # UDTs may span multiple memory types → per-field map_to
                for fn, _, _ in sdecl.fields:
                    fhw = sdecl.field_hw.get(fn)
                    if fhw is None:
                        continue
                    if fhw.start == fhw.end:
                        lines.append(f"    {sdecl.name}.{fn}.map_to({fhw.block_var}[{fhw.start}]),")
                    else:
                        lines.append(
                            f"    {sdecl.name}.{fn}.map_to("
                            f"{fhw.block_var}.select({fhw.start}, {fhw.end})),"
                        )
        # Flat tags (non-structure-owned)
        for decl in sorted_tags:
            if decl.operand in collection.structure_owned_operands:
                continue
            lines.append(f"    {decl.var_name}.map_to({decl.block_var}[{decl.block_index}]),")
        # Flat ranges
        for decl in sorted(collection.ranges.values(), key=lambda r: r.operand_str):
            lines.append(
                f"    {decl.var_name}.map_to({decl.block_var}.select({decl.start}, {decl.end})),"
            )
        lines.append("])")
    else:
        for decl in sorted_tags:
            lines.append(f"    {decl.var_name}: {decl.block_var}[{decl.block_index}],")

        # Add ranges
        for decl in sorted(collection.ranges.values(), key=lambda r: r.operand_str):
            lines.append(f"    {decl.var_name}: {decl.block_var}.select({decl.start}, {decl.end}),")

        lines.append("})")
