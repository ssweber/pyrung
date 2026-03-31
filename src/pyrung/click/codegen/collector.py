from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, cast

from pyclickplc.addresses import format_address_display

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
    _BlockSlotDecl,
    _FieldHw,
    _FileRefs,
    _OperandCollection,
    _PlainBlockDecl,
    _RangeDecl,
    _SemanticRender,
    _StructureDecl,
    _TagDecl,
)
from pyrung.click.codegen.utils import (
    _make_safe_identifier,
    _parse_operand_prefix,
    _strip_quoted_strings,
)
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


def _is_bool_operand_token(token: str, collection: _OperandCollection) -> bool:
    """Return True when a raw operand token resolves to a BOOL-style contact."""
    token = token.strip()
    if not token:
        return False
    tag_decl = collection.tags.get(token)
    if tag_decl is not None:
        return tag_decl.tag_type == "Bool"
    parsed = _parse_operand_prefix(token)
    return parsed is not None and parsed[1] == "Bool"


def _is_pipe_safe_or_token(token: str, collection: _OperandCollection) -> bool:
    """Return True when a condition token can be emitted safely with ``|``."""
    token = token.strip()
    if not token or _COMPARE_RE.match(token):
        return False
    if token.startswith("~"):
        return _is_pipe_safe_or_token(token[1:], collection)
    match = _FUNC_RE.match(token)
    if match:
        func_name = match.group(2)
        args_str = (match.group(3) or "").strip()
        return func_name in _CONDITION_WRAPPERS and _is_bool_operand_token(args_str, collection)
    return _is_bool_operand_token(token, collection)


def _parallel_renders_with_pipe(node: Parallel, collection: _OperandCollection) -> bool:
    """Return True when a parallel group should emit as ``a | b`` instead of ``any_of``."""
    return len(node.children) == 2 and all(
        isinstance(child, Leaf) and _is_pipe_safe_or_token(child.label, collection)
        for child in node.children
    )


def _tree_uses_any_of(node: SPNode | None, collection: _OperandCollection) -> bool:
    """Check if tree requires an ``any_of(...)`` helper in emitted code."""
    if node is None:
        return False
    if isinstance(node, Leaf):
        return False
    if isinstance(node, Series):
        return any(_tree_uses_any_of(c, collection) for c in node.children)
    if _parallel_renders_with_pipe(node, collection):
        return any(_tree_uses_any_of(c, collection) for c in node.children)
    return True


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
        if _tree_uses_any_of(rung.condition_tree, collection):
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
            if _tree_uses_any_of(instr.branch_tree, collection):
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

    # Enrich with semantic ownership metadata if available
    if structured_map is not None:
        _enrich_with_ownership(collection, structured_map)

    return collection


def _enrich_with_ownership(
    collection: _OperandCollection,
    structured_map: TagMap,
) -> None:
    """Build semantic ownership metadata for structures and plain named blocks."""

    seen_structures: dict[str, _StructureDecl] = {}
    seen_plain_blocks: dict[str, _PlainBlockDecl] = {}
    used_symbol_names = (
        {decl.var_name for decl in collection.tags.values()}
        | {decl.var_name for decl in collection.ranges.values()}
        | {structure.name for structure in structured_map.structures}
    )
    _TAG_TYPE_MAP = {
        "BOOL": "Bool",
        "INT": "Int",
        "DINT": "Dint",
        "REAL": "Real",
        "WORD": "Word",
        "CHAR": "Char",
    }
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

    def _ensure_structure_decl(structure_name: str) -> _StructureDecl | None:
        existing = seen_structures.get(structure_name)
        if existing is not None:
            return existing

        si = structured_map.structure_by_name(structure_name)
        if si is None:
            return None

        runtime = cast(Any, si.runtime)
        field_names = runtime.field_names

        fields: list[tuple[str, str, object]] = []
        field_retentive: dict[str, bool] = {}
        for fn in field_names:
            block = runtime._blocks[fn]
            type_name = _TAG_TYPE_MAP.get(block.type.name, block.type.name)
            sv = block.slot(1)
            fields.append((fn, type_name, sv.default))
            field_retentive[fn] = sv.retentive

        base_type: str | None = None
        if si.kind == "named_array":
            base_type = _TAG_TYPE_MAP.get(runtime.type.name, runtime.type.name)

        hw_block_var = ""
        hw_start = 0
        hw_end = 0

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
            always_number=getattr(runtime, "always_number", False),
        )
        seen_structures[si.name] = decl
        collection.structures.append(decl)

        if si.kind == "udt":
            for _, type_name, _ in fields:
                collection.used_types.add(type_name)
        if hw_block_var:
            collection.used_blocks.add(hw_block_var)

        return decl

    def _ensure_plain_block_decl(block_name: str) -> _PlainBlockDecl | None:
        existing = seen_plain_blocks.get(block_name)
        if existing is not None:
            return existing

        entry = structured_map.block_entry_by_name(block_name)
        if entry is None or not entry.hardware_addresses:
            return None

        logical_block = entry.logical
        first_hw = entry.hardware.block[entry.hardware_addresses[0]]
        last_hw = entry.hardware.block[entry.hardware_addresses[-1]]
        mem_type, hw_start = parse_address(first_hw.name)
        _, hw_end = parse_address(last_hw.name)
        var_name = _make_safe_identifier(
            logical_block.name,
            used_names=used_symbol_names,
            fallback="block",
        )
        used_symbol_names.add(var_name)

        decl = _PlainBlockDecl(
            name=logical_block.name,
            var_name=var_name,
            tag_type=logical_block.type.name,
            start=logical_block.start,
            end=logical_block.end,
            hw_block_var=_MEM_TO_BLOCK.get(mem_type, mem_type.lower()),
            hw_start=hw_start,
            hw_end=hw_end,
        )
        for logical_addr in entry.logical_addresses:
            slot = logical_block.slot(logical_addr)
            decl.slots[logical_addr] = _BlockSlotDecl(
                index=logical_addr,
                tag_name=slot.name,
                name_overridden=slot.name_overridden,
                retentive=slot.retentive,
                retentive_overridden=slot.retentive_overridden,
                default=slot.default,
                default_overridden=slot.default_overridden,
                comment=slot.comment,
                comment_overridden=slot.comment_overridden,
            )

        seen_plain_blocks[block_name] = decl
        collection.plain_blocks.append(decl)
        collection.used_blocks.add(decl.hw_block_var)
        return decl

    for operand in list(collection.tags):
        owner = structured_map.owner_of(operand)
        if owner is None:
            continue
        if owner.structure_type in ("named_array", "udt"):
            _ensure_structure_decl(owner.structure_name)
            if owner.instance is None:
                expr = f"{owner.structure_name}.{owner.field}"
            else:
                expr = f"{owner.structure_name}[{owner.instance}].{owner.field}"
            collection.semantic_operands[operand] = _SemanticRender(
                expr=expr,
                import_kind="structure",
                import_name=owner.structure_name,
            )
            continue

        if owner.structure_type != "block" or owner.instance is None:
            continue

        decl = _ensure_plain_block_decl(owner.structure_name)
        if decl is None:
            continue
        slot = decl.slots.get(owner.instance)
        if slot is not None and slot.name_overridden:
            if slot.alias_var_name is None:
                existing_decl = collection.tags.get(operand)
                if existing_decl is not None:
                    slot.alias_var_name = existing_decl.var_name
                else:
                    slot.alias_var_name = _make_safe_identifier(
                        slot.tag_name,
                        used_names=used_symbol_names,
                        fallback="tag",
                    )
                    used_symbol_names.add(slot.alias_var_name)
            collection.semantic_operands[operand] = _SemanticRender(
                expr=slot.alias_var_name,
                import_kind="tag",
                import_name=slot.alias_var_name,
            )
            continue

        collection.semantic_operands[operand] = _SemanticRender(
            expr=f"{decl.var_name}[{owner.instance}]",
            import_kind="block",
            import_name=decl.var_name,
        )

    for range_str, range_decl in collection.ranges.items():
        start_owner = structured_map.owner_of(
            format_address_display(range_decl.prefix, range_decl.start)
        )
        end_owner = structured_map.owner_of(
            format_address_display(range_decl.prefix, range_decl.end)
        )
        if start_owner is None:
            continue

        # Named-array whole-instance ranges (end may land on a gap slot for sparse)
        if start_owner.structure_type == "named_array":
            decl = _ensure_structure_decl(start_owner.structure_name)
            if decl is None:
                continue
            first_field = decl.fields[0][0]
            if start_owner.field != first_field:
                continue
            start_instance = start_owner.instance or 1
            range_len = range_decl.end - range_decl.start + 1
            if range_len % decl.stride != 0:
                continue
            n_instances = range_len // decl.stride
            end_instance = start_instance + n_instances - 1
            if end_instance > decl.count:
                continue
            if end_owner is not None and end_owner.structure_type == "named_array":
                if end_owner.structure_name != start_owner.structure_name:
                    continue
            expr = (
                f"{start_owner.structure_name}.instance({start_instance})"
                if start_instance == end_instance
                else f"{start_owner.structure_name}.instance_select({start_instance}, {end_instance})"
            )
            collection.semantic_ranges[range_str] = _SemanticRender(
                expr=expr,
                import_kind="structure",
                import_name=start_owner.structure_name,
            )
            continue

        if end_owner is None:
            continue
        if start_owner.structure_type == "block" and end_owner.structure_type == "block":
            if start_owner.structure_name != end_owner.structure_name:
                continue
            if start_owner.instance is None or end_owner.instance is None:
                continue
            if start_owner.instance > end_owner.instance:
                continue

            decl = _ensure_plain_block_decl(start_owner.structure_name)
            if decl is None:
                continue
            collection.semantic_ranges[range_str] = _SemanticRender(
                expr=f"{decl.var_name}.select({start_owner.instance}, {end_owner.instance})",
                import_kind="block",
                import_name=decl.var_name,
            )
            continue

        if start_owner.structure_type != "udt" or end_owner.structure_type != "udt":
            continue
        if start_owner.structure_name != end_owner.structure_name:
            continue
        if start_owner.field != end_owner.field:
            continue
        if start_owner.instance is None or end_owner.instance is None:
            continue
        if start_owner.instance > end_owner.instance:
            continue

        _ensure_structure_decl(start_owner.structure_name)
        collection.semantic_ranges[range_str] = _SemanticRender(
            expr=(
                f"{start_owner.structure_name}.{start_owner.field}.select("
                f"{start_owner.instance}, {end_owner.instance})"
            ),
            import_kind="structure",
            import_name=start_owner.structure_name,
        )


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
    used_var_names = {decl.var_name for decl in collection.tags.values()} | {
        decl.var_name for decl in collection.ranges.values()
    }

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
                    var_name = _make_safe_identifier(
                        range_str.replace("..", "_to_"), used_names=used_var_names
                    )
                    used_var_names.add(var_name)
                    collection.ranges[range_str] = _RangeDecl(
                        var_name=var_name,
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
        var_name = _make_safe_identifier(nick if nick else operand, used_names=used_var_names)
        used_var_names.add(var_name)
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
        if _tree_uses_any_of(rung.condition_tree, collection):
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
            if _tree_uses_any_of(instr.branch_tree, collection):
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

    def _record_semantic_ref(render: _SemanticRender) -> None:
        if render.import_kind == "tag":
            refs.tag_var_names.add(render.import_name)
        elif render.import_kind == "block":
            refs.block_var_names.add(render.import_name)
        elif render.import_kind == "structure":
            refs.structure_names.add(render.import_name)

    # Check ranges
    for range_match in _RANGE_RE.finditer(text):
        range_str = range_match.group(0)
        render = collection.semantic_ranges.get(range_str)
        if render is not None:
            _record_semantic_ref(render)
            continue

    # Check individual operands
    for op_match in _OPERAND_RE.finditer(text):
        operand = op_match.group(0)
        if operand in SYSTEM_OPERAND_PATHS:
            refs.has_system_import = True
            continue
        render = collection.semantic_operands.get(operand)
        if render is not None:
            _record_semantic_ref(render)
        elif operand in collection.tags:
            refs.tag_var_names.add(collection.tags[operand].var_name)
