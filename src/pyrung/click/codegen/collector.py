from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from pyrung.click.codegen.constants import (
    _COMPARE_RE,
    _CONDITION_WRAPPERS,
    _COPY_MODIFIERS,
    _FUNC_RE,
    _INSTRUCTION_NAMES,
    _OPERAND_RE,
    _RANGE_RE,
    _TIME_UNITS,
)
from pyrung.click.codegen.models import (
    _AnalyzedRung,
    _FieldHw,
    _OperandCollection,
    _OrLevel,
    _RangeDecl,
    _StructureDecl,
    _TagDecl,
)
from pyrung.click.codegen.utils import _parse_operand_prefix, _strip_quoted_strings

if TYPE_CHECKING:
    from pyrung.click.tag_map import TagMap


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
        if any(isinstance(e, _OrLevel) for e in rung.condition_seq):
            collection.has_any_of = True

        if rung.is_forloop_start:
            collection.has_forloop = True

        # Scan conditions
        all_conditions: list[str] = []
        for elem in rung.condition_seq:
            if isinstance(elem, str):
                all_conditions.append(elem)
            else:
                for group in elem.groups:
                    all_conditions.extend(group.conditions)

        for cond in all_conditions:
            _scan_token_for_operands(cond, collection, nicknames)

        # Scan instructions
        for instr in rung.instructions:
            _scan_af_token(instr.af_token, collection, nicknames)
            for cond in instr.branch_conditions:
                _scan_token_for_operands(cond, collection, nicknames)
            if instr.branch_conditions:
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

    match = _FUNC_RE.match(token)
    if not match:
        return

    func_name = match.group(2)
    args_str = match.group(3) or ""

    if func_name in _INSTRUCTION_NAMES:
        collection.used_instructions.add(func_name)

    if func_name in {"send", "receive"}:
        # Check for ModbusTarget
        if "ModbusTarget(" in args_str:
            collection.has_modbus_target = True

    if func_name == "call":
        collection.has_subroutine = True

    # raw() args are class name + hex blob, not operands — skip scanning.
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

    # Check for copy modifiers
    for cm in _COPY_MODIFIERS:
        if cm + "(" in clean_args:
            collection.used_copy_modifiers.add(cm)

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
