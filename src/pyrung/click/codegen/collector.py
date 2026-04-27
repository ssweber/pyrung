from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, cast

from pyclickplc.addresses import format_address_display

from pyrung.click._topology import Leaf, Parallel, Series, SPNode
from pyrung.click.codegen.constants import (
    _COMPARE_RE,
    _CONDITION_WRAPPERS,
    _COPY_CONVERTERS,
    _FUNC_RE,
    _INSTRUCTION_NAMES,
    _OPERAND_RE,
    _RANGE_RE,
)
from pyrung.click.codegen.models import (
    RungRole,
    _AnalyzedRung,
    _BlockSlotDecl,
    _FieldHw,
    _FileRefs,
    _OperandCollection,
    _PhysicalDecl,
    _PhysicalSpec,
    _PlainBlockDecl,
    _RangeDecl,
    _SemanticRender,
    _StructureDecl,
    _TagDecl,
    _TagMetadata,
    _TimerCounterCloneDecl,
)
from pyrung.click.codegen.utils import (
    _POINTER_RE,
    _PREFIX_TO_BLOCK,
    _make_safe_identifier,
    _parse_operand_prefix,
    _strip_quoted_strings,
)
from pyrung.click.system_mappings import SYSTEM_OPERAND_PATHS

if TYPE_CHECKING:
    from pyrung.click.tag_map import OwnerInfo, TagMap


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


def _tree_uses_Or(node: SPNode | None) -> bool:
    """Check if tree requires an ``Or(...)`` helper in emitted code."""
    if node is None:
        return False
    if isinstance(node, Leaf):
        return False
    if isinstance(node, Series):
        return any(_tree_uses_Or(c) for c in node.children)
    # Any Parallel node now always emits as Or(...)
    return True


def _tree_has_And(node: SPNode | None) -> bool:
    """Check if tree has a multi-child Series inside a Parallel."""
    if node is None:
        return False
    if isinstance(node, Parallel):
        for child in node.children:
            if isinstance(child, Series) and len(child.children) > 1:
                return True
            if _tree_has_And(child):
                return True
    if isinstance(node, Series):
        return any(_tree_has_And(c) for c in node.children)
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
        if _tree_uses_Or(rung.condition_tree):
            collection.has_Or = True
        if _tree_has_And(rung.condition_tree):
            collection.has_And = True

        if rung.comment:
            collection.has_comment = True
        if rung.role is RungRole.FORLOOP_START:
            collection.has_forloop = True

        # Scan conditions from tree
        for cond in _walk_tree_labels(rung.condition_tree):
            _scan_token_for_operands(cond, collection, nicknames)

        # Scan instructions
        for instr in rung.instructions:
            _scan_af_token(instr.af_token, collection, nicknames)
            for cond in _walk_tree_labels(instr.branch_tree):
                _scan_token_for_operands(cond, collection, nicknames)
            if _tree_uses_Or(instr.branch_tree):
                collection.has_Or = True
            if _tree_has_And(instr.branch_tree):
                collection.has_And = True
            if instr.branch_tree is not None:
                collection.has_branch = True
            for pin in instr.pins:
                if pin.condition_tree is not None:
                    for cond in _walk_tree_labels(pin.condition_tree):
                        _scan_token_for_operands(cond, collection, nicknames)
                    if _tree_uses_Or(pin.condition_tree):
                        collection.has_Or = True
                    if _tree_has_And(pin.condition_tree):
                        collection.has_And = True
                else:
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

    def _spec_from_physical(physical: Any) -> _PhysicalSpec | None:
        if physical is None:
            return None
        name = getattr(physical, "name", None)
        if not isinstance(name, str) or name == "":
            return None
        return _PhysicalSpec(
            name=name,
            on_delay=getattr(physical, "on_delay", None),
            off_delay=getattr(physical, "off_delay", None),
            profile=getattr(physical, "profile", None),
            system=getattr(physical, "system", None),
        )

    def _metadata_from_tag(tag: Any) -> _TagMetadata:
        return _TagMetadata(
            choices=getattr(tag, "choices", None),
            readonly=getattr(tag, "readonly", False),
            external=getattr(tag, "external", False),
            final=getattr(tag, "final", False),
            public=getattr(tag, "public", False),
            physical=_spec_from_physical(getattr(tag, "physical", None)),
            link=getattr(tag, "link", None),
            min=getattr(tag, "min", None),
            max=getattr(tag, "max", None),
            uom=getattr(tag, "uom", None),
        )

    def _metadata_is_empty(meta: _TagMetadata) -> bool:
        return (
            meta.choices is None
            and not meta.readonly
            and not meta.external
            and not meta.final
            and not meta.public
            and meta.physical is None
            and meta.link is None
            and meta.min is None
            and meta.max is None
            and meta.uom is None
        )

    def _register_physical(meta: _TagMetadata) -> None:
        if meta.physical is None or meta.physical in collection.physical_decls:
            return
        raw_name = f"{meta.physical.name}_physical"
        var_name = _make_safe_identifier(
            raw_name, used_names=used_symbol_names, fallback="physical"
        )
        used_symbol_names.add(var_name)
        collection.physical_decls[meta.physical] = _PhysicalDecl(
            var_name=var_name, spec=meta.physical
        )

    def _register_metadata(meta: _TagMetadata) -> _TagMetadata:
        _register_physical(meta)
        return meta

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
        field_metadata: dict[str, _TagMetadata] = {}
        field_slot_metadata: dict[tuple[str, int], _TagMetadata] = {}
        for fn in field_names:
            block = runtime._blocks[fn]
            type_name = _TAG_TYPE_MAP.get(block.type.name, block.type.name)
            sv = block.slot(1)
            fields.append((fn, type_name, sv.default))
            field_retentive[fn] = sv.retentive
            slot_metadata = tuple(_metadata_from_tag(block.slot(i)) for i in range(1, si.count + 1))
            nonempty_metadata = [meta for meta in slot_metadata if not _metadata_is_empty(meta)]
            if nonempty_metadata and all(meta == slot_metadata[0] for meta in slot_metadata):
                field_metadata[fn] = _register_metadata(slot_metadata[0])
            else:
                for idx, metadata in enumerate(slot_metadata, start=1):
                    if not _metadata_is_empty(metadata):
                        field_slot_metadata[(fn, idx)] = _register_metadata(metadata)

        base_type: str | None = None
        if si.kind == "named_array":
            base_type = _TAG_TYPE_MAP.get(runtime.type.name, runtime.type.name)

        hw_block_var = ""
        hw_start: int | None = None
        hw_end: int | None = None

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

        effective_stride = si.stride

        # Compute hw_end: use runtime.hardware_span when we have a mapped
        # named_array, or fall back to last-field address lookup.
        if hw_start is not None and effective_stride is not None and si.kind == "named_array":
            _, hw_end = runtime.hardware_span(hw_start)
        else:
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
            stride=effective_stride,
            fields=fields,
            hw_block_var=hw_block_var,
            hw_start=hw_start,
            hw_end=hw_end,
            field_retentive=field_retentive,
            field_metadata=field_metadata,
            field_slot_metadata=field_slot_metadata,
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

        entry = structured_map._block_entry_by_name(block_name)
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
            metadata = _register_metadata(_metadata_from_tag(slot))
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
                choices=metadata.choices,
                choices_overridden=slot.choices_overridden,
                readonly=metadata.readonly,
                readonly_overridden=slot.readonly_overridden,
                external=metadata.external,
                external_overridden=slot.external_overridden,
                final=metadata.final,
                final_overridden=slot.final_overridden,
                public=metadata.public,
                public_overridden=slot.public_overridden,
                physical=metadata.physical,
                physical_overridden=slot.physical_overridden,
                link=metadata.link,
                link_overridden=slot.link_overridden,
                min=metadata.min,
                min_overridden=slot.min_overridden,
                max=metadata.max,
                max_overridden=slot.max_overridden,
                uom=metadata.uom,
                uom_overridden=slot.uom_overridden,
            )

        seen_plain_blocks[block_name] = decl
        collection.plain_blocks.append(decl)
        collection.used_blocks.add(decl.hw_block_var)
        return decl

    for operand in list(collection.tags):
        owner = structured_map._owner_of(operand)
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
        start_owner = structured_map._owner_of(
            format_address_display(range_decl.prefix, range_decl.start)
        )
        end_owner = structured_map._owner_of(
            format_address_display(range_decl.prefix, range_decl.end)
        )
        if start_owner is None:
            continue

        resolved = False

        # Named-array whole-instance ranges (end may land on a gap slot for sparse)
        if start_owner.structure_type == "named_array":
            decl = _ensure_structure_decl(start_owner.structure_name)
            if decl is not None and decl.stride is not None:
                first_field = decl.fields[0][0]
                start_instance = start_owner.instance or 1
                range_len = range_decl.end - range_decl.start + 1
                if start_owner.field == first_field and range_len % decl.stride == 0:
                    n_instances = range_len // decl.stride
                    end_instance = start_instance + n_instances - 1
                    ok = end_instance <= decl.count
                    if ok and end_owner is not None and end_owner.structure_type == "named_array":
                        ok = end_owner.structure_name == start_owner.structure_name
                    if ok:
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
                        resolved = True

        if not resolved and end_owner is not None:
            if start_owner.structure_type == "block" and end_owner.structure_type == "block":
                if (
                    start_owner.structure_name == end_owner.structure_name
                    and start_owner.instance is not None
                    and end_owner.instance is not None
                    and start_owner.instance <= end_owner.instance
                ):
                    decl = _ensure_plain_block_decl(start_owner.structure_name)
                    if decl is not None:
                        collection.semantic_ranges[range_str] = _SemanticRender(
                            expr=f"{decl.var_name}.select({start_owner.instance}, {end_owner.instance})",
                            import_kind="block",
                            import_name=decl.var_name,
                        )
                        resolved = True

            if (
                not resolved
                and start_owner.structure_type == "udt"
                and end_owner.structure_type == "udt"
                and start_owner.structure_name == end_owner.structure_name
                and start_owner.field == end_owner.field
                and start_owner.instance is not None
                and end_owner.instance is not None
                and start_owner.instance <= end_owner.instance
            ):
                _ensure_structure_decl(start_owner.structure_name)
                collection.semantic_ranges[range_str] = _SemanticRender(
                    expr=(
                        f"{start_owner.structure_name}.{start_owner.field}.select("
                        f"{start_owner.instance}, {end_owner.instance})"
                    ),
                    import_kind="structure",
                    import_name=start_owner.structure_name,
                )
                resolved = True

        # Partial-structure range: keep raw ds.select() but add a comment
        if not resolved:
            comment = _build_partial_range_comment(start_owner, end_owner)
            if comment is not None:
                collection.range_comments[range_str] = comment

    tag_by_hardware = {entry.hardware.name: entry.logical for entry in structured_map.tags()}
    for operand, decl in collection.tags.items():
        if operand in collection.semantic_operands or operand in collection.timer_counter_operands:
            continue
        logical_tag = tag_by_hardware.get(operand)
        if logical_tag is None:
            continue
        decl.metadata = _register_metadata(_metadata_from_tag(logical_tag))


def _build_partial_range_comment(
    start_owner: OwnerInfo,
    end_owner: OwnerInfo | None,
) -> str | None:
    """Build an inline comment for a range that falls within a structure."""
    name = start_owner.structure_name
    start_field = start_owner.field
    end_field = end_owner.field if end_owner is not None else None

    # Both endpoints in the same structure
    if end_owner is not None and end_owner.structure_name == name:
        if start_field == end_field:
            return f"# {name}.{start_field}"
        return f"# {name}: {start_field}..{end_field}"

    # Only start is owned
    if start_field is not None:
        return f"# {name}.{start_field}.."

    return None


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


# Suffixes to strip from timer/counter nicknames so that
# Timer.clone("OvenTimer") doesn't produce OvenTimer_Done_Done.
_TC_NICK_SUFFIXES = ("_Done", "_Acc")


def _register_timer_counter_clone(
    done_operand: str,
    acc_operand: str,
    nick: str | None,
    func_name: str,
    collection: _OperandCollection,
) -> None:
    """Register a Timer.clone() / Counter.clone() declaration."""
    parsed = _parse_operand_prefix(done_operand)
    if parsed is None:
        return
    prefix, _, _, index = parsed
    kind = "Timer" if prefix == "T" else "Counter"

    # Avoid duplicate declarations for the same timer/counter
    if any(d.done_operand == done_operand for d in collection.timer_counter_clones):
        return

    # Determine the clone name: nickname (suffix-stripped) or operand prefix+index
    if nick:
        for suffix in _TC_NICK_SUFFIXES:
            if nick.endswith(suffix) and len(nick) > len(suffix):
                nick = nick[: -len(suffix)]
                break
        raw_name = nick
    else:
        raw_name = f"{prefix}{index}"

    used_var_names = (
        {decl.var_name for decl in collection.tags.values()}
        | {decl.var_name for decl in collection.ranges.values()}
        | {d.var_name for d in collection.timer_counter_clones}
    )
    var_name = _make_safe_identifier(raw_name, used_names=used_var_names)

    collection.timer_counter_clones.append(
        _TimerCounterCloneDecl(
            var_name=var_name,
            kind=kind,
            index=index,
            done_operand=done_operand,
            acc_operand=acc_operand,
        )
    )
    # Register semantic operands so condition references resolve:
    # T1 → OvenTimer.Done, TD1 → OvenTimer.Acc
    collection.semantic_operands[done_operand] = _SemanticRender(
        expr=f"{var_name}.Done",
        import_kind="tc_clone",
        import_name=var_name,
    )
    collection.semantic_operands[acc_operand] = _SemanticRender(
        expr=f"{var_name}.Acc",
        import_kind="tc_clone",
        import_name=var_name,
    )
    # Ensure hardware blocks are imported for TagMap entries
    if kind == "Timer":
        collection.used_blocks.add("t")
        collection.used_blocks.add("td")
    else:
        collection.used_blocks.add("ct")
        collection.used_blocks.add("ctd")


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

    # Track timer/counter done_bit + accumulator operands and register a
    # clone declaration for each used pair.
    if func_name in {"on_delay", "off_delay", "count_up", "count_down"}:
        from pyrung.click.codegen.utils import _parse_af_args

        tc_args, _ = _parse_af_args(args_str)
        if len(tc_args) >= 2:
            collection.timer_counter_operands.add(tc_args[0])
            collection.timer_counter_operands.add(tc_args[1])

            nick = nicknames.get(tc_args[0]) if nicknames else None
            _register_timer_counter_clone(tc_args[0], tc_args[1], nick, func_name, collection)

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

    # Pointer/indirect addressing: DH[DS134] → register the block variable
    for ptr_match in _POINTER_RE.finditer(text):
        prefix = ptr_match.group(1)
        collection.used_blocks.add(_PREFIX_TO_BLOCK[prefix])
        # Also register the inner operand
        _register_operands_from_text(ptr_match.group(2), collection, nicknames)

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
        if _tree_uses_Or(rung.condition_tree):
            refs.has_Or = True
        if _tree_has_And(rung.condition_tree):
            refs.has_And = True
        if rung.comment:
            refs.has_comment = True
        if rung.role is RungRole.FORLOOP_START:
            refs.has_forloop = True

        for cond in _walk_tree_labels(rung.condition_tree):
            _ref_token(cond, collection, refs)

        for instr in rung.instructions:
            _ref_af_token(instr.af_token, collection, refs, call_func_map=call_func_map)
            for cond in _walk_tree_labels(instr.branch_tree):
                _ref_token(cond, collection, refs)
            if _tree_uses_Or(instr.branch_tree):
                refs.has_Or = True
            if _tree_has_And(instr.branch_tree):
                refs.has_And = True
            if instr.branch_tree is not None:
                refs.has_branch = True
            for pin in instr.pins:
                if pin.condition_tree is not None:
                    for cond in _walk_tree_labels(pin.condition_tree):
                        _ref_token(cond, collection, refs)
                    if _tree_uses_Or(pin.condition_tree):
                        refs.has_Or = True
                    if _tree_has_And(pin.condition_tree):
                        refs.has_And = True
                else:
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

    # For timer/counter instructions, strip the done_bit + accumulator args
    # from the text before scanning refs — they're rendered as Timer[n]/Counter[n]
    # and don't need tag-level imports.
    if func_name in {"on_delay", "off_delay", "count_up", "count_down"}:
        from pyrung.click.codegen.utils import _parse_af_args

        tc_args, tc_kwargs = _parse_af_args(args_str)
        if len(tc_args) >= 2:
            # Track tc_clone ref for project-mode imports
            done_operand = tc_args[0]
            render = collection.semantic_operands.get(done_operand)
            if render is not None and render.import_kind == "tc_clone":
                refs.tc_clone_var_names.add(render.import_name)
            # Rebuild args_str without the first two positional args
            remaining = [f"{k}={v}" for k, v in tc_kwargs]
            args_str = ",".join(remaining)

    clean_args = _strip_quoted_strings(args_str)

    for cw in _CONDITION_WRAPPERS:
        if cw + "(" in clean_args:
            refs.used_conditions.add(cw)

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
        elif render.import_kind == "tc_clone":
            refs.tc_clone_var_names.add(render.import_name)

    # Check ranges
    for range_match in _RANGE_RE.finditer(text):
        range_str = range_match.group(0)
        render = collection.semantic_ranges.get(range_str)
        if render is not None:
            _record_semantic_ref(render)
            continue
        # Non-semantic range → emitted as block_var.select(); track the Click block
        range_decl = collection.ranges.get(range_str)
        if range_decl is not None:
            refs.used_click_blocks.add(range_decl.block_var)
        else:
            # Fallback: parse prefix from the range text
            first_operand = range_str.split("..")[0] if ".." in range_str else range_str
            parsed = _parse_operand_prefix(first_operand)
            if parsed is not None:
                refs.used_click_blocks.add(parsed[2])

    # Pointer/indirect addressing: DH[DS134] → need to import dh
    for ptr_match in _POINTER_RE.finditer(text):
        prefix = ptr_match.group(1)
        refs.used_click_blocks.add(_PREFIX_TO_BLOCK[prefix])
        # Also scan the inner operand for tag references
        _ref_operands_in_text(ptr_match.group(2), collection, refs)

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
