from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, cast

from pyrung.click.codegen.constants import (
    _COMPARE_RE,
    _CONDITION_WRAPPERS,
    _COPY_CONVERTERS,
    _FUNC_RE,
    _INSTRUCTION_NAMES,
    _OPERAND_RE,
    _RANGE_RE,
    _TIME_UNITS,
)
from pyrung.click.codegen.models import (
    Leaf,
    Parallel,
    Series,
    SPNode,
    _AnalyzedRung,
    _FieldHw,
    _FileRefs,
    _OperandCollection,
    _RangeDecl,
    _StructureDecl,
    _TagDecl,
)
from pyrung.click.codegen.utils import _parse_operand_prefix, _strip_quoted_strings
from pyrung.click.system_mappings import SYSTEM_OPERAND_PATHS

if TYPE_CHECKING:
    from pyrung.click.tag_map import TagMap


# ---------------------------------------------------------------------------
# SP tree helpers
# ---------------------------------------------------------------------------


def _walk_tree_labels(node: SPNode | None) -> Iterator[str]:
    """Yield all Leaf labels from an SP tree."""
    if node is None:
        return
    if isinstance(node, Leaf):
        yield node.label
    elif isinstance(node, (Series, Parallel)):
        for child in node.children:
            yield from _walk_tree_labels(child)


def _tree_has_parallel(node: SPNode | None) -> bool:
    """Check if tree contains any Parallel node."""
    if node is None:
        return False
    if isinstance(node, Parallel):
        return True
    if isinstance(node, Series):
        return any(_tree_has_parallel(c) for c in node.children)
    return False


def _tree_has_all_of(node: SPNode | None) -> bool:
    """Check if tree has a multi-child Series inside a Parallel."""
    if node is None:
        return False
    if isinstance(node, Parallel):
        for child in node.children:
            if isinstance(child, Series) and len(child.children) > 1:
                return True
            if _tree_has_all_of(child):
                return True
    if isinstance(node, Series):
        return any(_tree_has_all_of(c) for c in node.children)
    return False


# ---------------------------------------------------------------------------
# Phase 3: Collect Operands → Tag Declarations
# ---------------------------------------------------------------------------


def _collect_operands(
    rungs: list[_AnalyzedRung],
    nicknames: dict[str, str] | None,
    *,
    structured_map: TagMap | None = None,
) -> _OperandCollection:
    """Scan all rungs and collect operand declarations."""
    collection = _OperandCollection()

    for rung in rungs:
        if _tree_has_parallel(rung.condition_tree):
            collection.has_any_of = True
        if _tree_has_all_of(rung.condition_tree):
            collection.has_all_of = True

        if rung.comment:
            collection.has_comment = True
        if rung.is_forloop_start:
            collection.has_forloop = True

        # Scan conditions from tree
        for cond in _walk_tree_labels(rung.condition_tree):
            _scan_token_for_operands(cond, collection, nicknames)

        # Scan instructions
        for instr in rung.instructions:
            _scan_af_token(instr.af_token, collection, nicknames)
            for cond in _walk_tree_labels(instr.branch_tree):
                _scan_token_for_operands(cond, collection, nicknames)
            if _tree_has_parallel(instr.branch_tree):
                collection.has_any_of = True
            if _tree_has_all_of(instr.branch_tree):
                collection.has_all_of = True
            if instr.branch_tree is not None:
                collection.has_branch = True
            for pin in instr.pins:
                for cond in pin.conditions:
                    _scan_token_for_operands(cond, collection, nicknames)
                if pin.arg:
                    _scan_token_for_operands(pin.arg, collection, nicknames)

    # Enrich with structured metadata if available
    if structured_map is not None:
        _enrich_with_structures(collection, structured_map)

    return collection


def _enrich_with_structures(
    collection: _OperandCollection,
    structured_map: TagMap,
) -> None:
    """Mark structure-owned operands and build _StructureDecl entries."""

    seen_structures: dict[str, _StructureDecl] = {}

    for operand in list(collection.tags):
        owner = structured_map.owner_of(operand)
        if owner is None:
            continue
        if owner.structure_type not in ("named_array", "udt"):
            continue

        collection.structure_owned_operands.add(operand)

        if owner.structure_name in seen_structures:
            continue

        # Build _StructureDecl from StructuredImport metadata
        si = structured_map.structure_by_name(owner.structure_name)
        if si is None:
            continue

        runtime = cast(Any, si.runtime)
        field_names = runtime.field_names

        _TAG_TYPE_MAP = {
            "BOOL": "Bool",
            "INT": "Int",
            "DINT": "Dint",
            "REAL": "Real",
            "WORD": "Word",
            "CHAR": "Char",
        }

        fields: list[tuple[str, str, object]] = []
        field_retentive: dict[str, bool] = {}
        for fn in field_names:
            block = runtime._blocks[fn]
            type_name = _TAG_TYPE_MAP.get(block.type.name, block.type.name)
            default = block.slot_config(1).default
            fields.append((fn, type_name, default))
            field_retentive[fn] = block.slot_config(1).retentive

        # Determine base_type for named_array (all fields share same type)
        base_type: str | None = None
        if si.kind == "named_array":
            base_type = _TAG_TYPE_MAP.get(runtime.type.name, runtime.type.name)

        # Determine hw_block_var and hw address range
        hw_block_var = ""
        hw_start = 0
        hw_end = 0

        def _resolve_hw_tag(slot_tag: Any) -> Any:
            """Resolve a logical slot to its hardware tag."""
            hw = structured_map._block_slot_forward_by_name.get(slot_tag.name)
            if hw is None:
                hw = structured_map._block_slot_forward_by_id.get(id(slot_tag))
            if hw is None:
                tag_entry = structured_map._tag_forward.get(slot_tag.name)
                if tag_entry is not None:
                    hw = tag_entry.hardware
            return hw

        _MEM_TO_BLOCK = {
            "X": "x",
            "Y": "y",
            "C": "c",
            "DS": "ds",
            "DD": "dd",
            "DH": "dh",
            "DF": "df",
            "T": "t",
            "TD": "td",
            "CT": "ct",
            "CTD": "ctd",
            "SC": "sc",
            "SD": "sd",
            "TXT": "txt",
            "XD": "xd",
            "YD": "yd",
        }

        from pyclickplc.addresses import parse_address

        # Build per-field hardware info
        per_field_hw: dict[str, _FieldHw] = {}
        for fn in field_names:
            fblock = runtime._blocks[fn]
            first_hw = _resolve_hw_tag(fblock[1])
            last_hw = _resolve_hw_tag(fblock[si.count])
            if first_hw is not None and last_hw is not None:
                mem_type, fstart = parse_address(first_hw.name)
                _, fend = parse_address(last_hw.name)
                bvar = _MEM_TO_BLOCK.get(mem_type, mem_type.lower())
                per_field_hw[fn] = _FieldHw(block_var=bvar, start=fstart, end=fend)
                collection.used_blocks.add(bvar)

        # For named_array, use overall span; for udt, use per-field
        first_field_block = runtime._blocks[field_names[0]]
        first_slot = first_field_block[1]
        hw_tag = _resolve_hw_tag(first_slot)
        if hw_tag is not None:
            mem_type, addr = parse_address(hw_tag.name)
            hw_start = addr
            hw_block_var = _MEM_TO_BLOCK.get(mem_type, mem_type.lower())

        last_field_block = runtime._blocks[field_names[-1]]
        last_slot = last_field_block[si.count]
        last_hw_tag = _resolve_hw_tag(last_slot)
        if last_hw_tag is not None:
            _, hw_end = parse_address(last_hw_tag.name)

        decl = _StructureDecl(
            name=si.name,
            structure_type=si.kind,
            base_type=base_type,
            count=si.count,
            stride=si.stride,
            fields=fields,
            hw_block_var=hw_block_var,
            hw_start=hw_start,
            hw_end=hw_end,
            field_retentive=field_retentive,
            field_hw=per_field_hw,
        )
        seen_structures[si.name] = decl
        collection.structures.append(decl)

        # Ensure types from structure fields are imported
        if si.kind == "udt":
            for _, type_name, _ in fields:
                collection.used_types.add(type_name)
        # Ensure hw block var is in used_blocks
        if hw_block_var:
            collection.used_blocks.add(hw_block_var)


def _scan_token_for_operands(
    token: str,
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
) -> None:
    """Scan a single token string for operand references."""
    # Check for condition wrappers
    match = _FUNC_RE.match(token)
    if match:
        func_name = match.group(2)
        args_str = match.group(3) or ""
        if func_name in _CONDITION_WRAPPERS:
            collection.used_conditions.add(func_name)
            # Scan the inner argument
            _register_operands_from_text(args_str, collection, nicknames)
            return

    # Check for comparison
    cmp_match = _COMPARE_RE.match(token)
    if cmp_match:
        _register_operands_from_text(cmp_match.group(1), collection, nicknames)
        _register_operands_from_text(cmp_match.group(3), collection, nicknames)
        return

    # Check for negation prefix
    if token.startswith("~"):
        _register_operands_from_text(token[1:], collection, nicknames)
        return

    # Plain operand
    _register_operands_from_text(token, collection, nicknames)


def _scan_af_token(
    token: str,
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
) -> None:
    """Scan an AF token for instruction name and operands."""
    if not token:
        return

    # Bare NOP — becomes an empty rung (pass), not an instruction import.
    if token == "NOP":
        return

    match = _FUNC_RE.match(token)
    if not match:
        return

    func_name = match.group(2)
    args_str = match.group(3) or ""

    if func_name in _INSTRUCTION_NAMES:
        collection.used_instructions.add(func_name)

    if func_name in {"send", "receive"}:
        if "ModbusTcpTarget(" in args_str:
            collection.has_modbus_target = True
        if "ModbusRtuTarget(" in args_str:
            collection.has_modbus_rtu_target = True
        if "ModbusAddress(" in args_str:
            collection.has_modbus_address = True

    if func_name == "call":
        collection.has_subroutine = True

    # raw() args are class name + field specs, not operands — skip scanning.
    if func_name == "raw":
        return

    # Strip quoted strings before scanning for operands
    clean_args = _strip_quoted_strings(args_str)

    # Check for condition wrappers inside AF args (e.g. out(immediate(Y001)))
    for cw in _CONDITION_WRAPPERS:
        if cw + "(" in clean_args:
            collection.used_conditions.add(cw)

    # Check for time units
    for tu in _TIME_UNITS:
        if tu in clean_args:
            collection.used_time_units.add(tu)

    # Check for copy converters
    for cc in _COPY_CONVERTERS:
        if f"convert={cc}" in clean_args:
            collection.used_copy_converters.add(cc)

    # Scan for operands
    _register_operands_from_text(clean_args, collection, nicknames)


def _register_operands_from_text(
    text: str,
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
) -> None:
    """Find and register all operands in a text fragment."""
    # Check for ranges first — collect range spans to suppress individual tags
    range_spans: set[str] = set()
    for range_match in _RANGE_RE.finditer(text):
        prefix1 = range_match.group(1)
        num1 = int(range_match.group(2))
        prefix2 = range_match.group(3)
        num2 = int(range_match.group(4))
        if prefix1 == prefix2:
            range_str = range_match.group(0)
            if range_str not in collection.ranges:
                parsed = _parse_operand_prefix(f"{prefix1}{num1}")
                if parsed:
                    _, tag_type, block_var, _ = parsed
                    # IEC type constants for Block declaration
                    iec_type = tag_type.upper()
                    collection.ranges[range_str] = _RangeDecl(
                        var_name=range_str.replace("..", "_to_"),
                        block_var=block_var,
                        tag_type=iec_type,
                        prefix=prefix1,
                        start=num1,
                        end=num2,
                        operand_str=range_str,
                    )
                    collection.used_blocks.add(block_var)
            # Mark all addresses in this range to suppress individual tags
            for i in range(num1, num2 + 1):
                range_spans.add(f"{prefix1}{i}")

    # Find individual operands (skip those covered by a range)
    for op_match in _OPERAND_RE.finditer(text):
        operand = op_match.group(0)
        if operand in collection.tags:
            continue
        if operand in range_spans:
            continue
        if operand in SYSTEM_OPERAND_PATHS:
            collection.has_system_operands = True
            continue

        parsed = _parse_operand_prefix(operand)
        if parsed is None:
            continue

        prefix, tag_type, block_var, index = parsed
        collection.used_types.add(tag_type)
        collection.used_blocks.add(block_var)

        nick = nicknames.get(operand) if nicknames else None
        var_name = nick if nick else operand
        tag_name = nick if nick else operand
        comment_str = f"  # {operand}" if nick else ""

        collection.tags[operand] = _TagDecl(
            var_name=var_name,
            tag_type=tag_type,
            tag_name=tag_name,
            operand=operand,
            block_var=block_var,
            block_index=index,
            comment=comment_str,
        )


# ---------------------------------------------------------------------------
# Per-file reference scanning (for multi-file project output)
# ---------------------------------------------------------------------------


def _scan_file_refs(
    rungs: list[_AnalyzedRung],
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
    *,
    call_func_map: dict[str, str] | None = None,
) -> _FileRefs:
    """Scan rungs and record which symbols from *collection* they reference.

    Unlike :func:`_collect_operands` (which builds the global collection),
    this function only records hits against an already-populated collection.
    """

    refs = _FileRefs()

    for rung in rungs:
        if _tree_has_parallel(rung.condition_tree):
            refs.has_any_of = True
        if _tree_has_all_of(rung.condition_tree):
            refs.has_all_of = True
        if rung.comment:
            refs.has_comment = True
        if rung.is_forloop_start:
            refs.has_forloop = True

        for cond in _walk_tree_labels(rung.condition_tree):
            _ref_token(cond, collection, refs)

        for instr in rung.instructions:
            _ref_af_token(instr.af_token, collection, refs, call_func_map=call_func_map)
            for cond in _walk_tree_labels(instr.branch_tree):
                _ref_token(cond, collection, refs)
            if _tree_has_parallel(instr.branch_tree):
                refs.has_any_of = True
            if _tree_has_all_of(instr.branch_tree):
                refs.has_all_of = True
            if instr.branch_tree is not None:
                refs.has_branch = True
            for pin in instr.pins:
                for cond in pin.conditions:
                    _ref_token(cond, collection, refs)
                if pin.arg:
                    _ref_token(pin.arg, collection, refs)

    return refs


def _ref_token(
    token: str,
    collection: _OperandCollection,
    refs: _FileRefs,
) -> None:
    """Record operand references in a condition token."""
    match = _FUNC_RE.match(token)
    if match:
        func_name = match.group(2)
        args_str = match.group(3) or ""
        if func_name in _CONDITION_WRAPPERS:
            refs.used_conditions.add(func_name)
            _ref_operands_in_text(args_str, collection, refs)
            return

    cmp_match = _COMPARE_RE.match(token)
    if cmp_match:
        _ref_operands_in_text(cmp_match.group(1), collection, refs)
        _ref_operands_in_text(cmp_match.group(3), collection, refs)
        return

    if token.startswith("~"):
        _ref_operands_in_text(token[1:], collection, refs)
        return

    _ref_operands_in_text(token, collection, refs)


def _ref_af_token(
    token: str,
    collection: _OperandCollection,
    refs: _FileRefs,
    *,
    call_func_map: dict[str, str] | None = None,
) -> None:
    """Record references in an AF (instruction) token."""
    if not token:
        return

    # Bare NOP — becomes an empty rung (pass), not an instruction import.
    if token == "NOP":
        return

    match = _FUNC_RE.match(token)
    if not match:
        return

    func_name = match.group(2)
    args_str = match.group(3) or ""

    if func_name in _INSTRUCTION_NAMES:
        refs.used_instructions.add(func_name)

    if func_name in {"send", "receive"}:
        if "ModbusTcpTarget(" in args_str:
            refs.has_modbus_target = True
        if "ModbusRtuTarget(" in args_str:
            refs.has_modbus_rtu_target = True
        if "ModbusAddress(" in args_str:
            refs.has_modbus_address = True

    if func_name == "call":
        # Extract subroutine name for cross-file import tracking
        sub_name = args_str.strip().strip('"')
        if sub_name:
            if call_func_map and sub_name in call_func_map:
                refs.subroutine_func_names.add(call_func_map[sub_name])
            else:
                from pyrung.click.codegen.utils import _slugify as slugify

                refs.subroutine_func_names.add(slugify(sub_name))

    if func_name == "raw":
        return

    clean_args = _strip_quoted_strings(args_str)

    for cw in _CONDITION_WRAPPERS:
        if cw + "(" in clean_args:
            refs.used_conditions.add(cw)

    for tu in _TIME_UNITS:
        if tu in clean_args:
            refs.used_time_units.add(tu)

    for cc in _COPY_CONVERTERS:
        if f"convert={cc}" in clean_args:
            refs.used_copy_converters.add(cc)

    _ref_operands_in_text(clean_args, collection, refs)


def _ref_operands_in_text(
    text: str,
    collection: _OperandCollection,
    refs: _FileRefs,
) -> None:
    """Record which tags/ranges/structures from *collection* appear in *text*."""
    # Check ranges
    for range_match in _RANGE_RE.finditer(text):
        range_str = range_match.group(0)
        if range_str in collection.ranges:
            refs.range_var_names.add(collection.ranges[range_str].var_name)

    # Check individual operands
    for op_match in _OPERAND_RE.finditer(text):
        operand = op_match.group(0)
        if operand in SYSTEM_OPERAND_PATHS:
            refs.has_system_import = True
            continue
        if operand in collection.structure_owned_operands:
            # Find which structure owns it
            for sdecl in collection.structures:
                # Check if operand falls within structure's address space
                parsed = _parse_operand_prefix(operand)
                if parsed is None:
                    continue
                _, _, block_var, index = parsed
                if sdecl.structure_type == "named_array":
                    if block_var == sdecl.hw_block_var and sdecl.hw_start <= index <= sdecl.hw_end:
                        refs.structure_names.add(sdecl.name)
                        break
                elif sdecl.structure_type == "udt":
                    for fhw in sdecl.field_hw.values():
                        if block_var == fhw.block_var and fhw.start <= index <= fhw.end:
                            refs.structure_names.add(sdecl.name)
                            break
        elif operand in collection.tags:
            refs.tag_var_names.add(collection.tags[operand].var_name)
