"""Automatically generated nickname CSV helper extraction."""

from __future__ import annotations

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
from pyrung.core.tag import MappingEntry, _normalize_choices

from ._parsers import (
    TagMeta,
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


def _tag_meta_from_hints(
    *,
    choices: object,
    readonly: object,
    external: object = False,
    final: object = False,
    public: object = False,
    min: object = None,
    max: object = None,
    uom: object = None,
) -> TagMeta | None:
    r = bool(readonly)
    e = bool(external)
    f = bool(final)
    p = bool(public)
    if (
        choices is None
        and not r
        and not e
        and not f
        and not p
        and min is None
        and max is None
        and uom is None
    ):
        return None
    return TagMeta(
        readonly=r,
        choices=cast(dict[int | float | str, str] | None, choices),
        external=e,
        final=f,
        public=p,
        min=cast(int | float | None, min),
        max=cast(int | float | None, max),
        uom=cast(str | None, uom),
    )


def tag_map_from_nickname_file(
    cls,
    path: str | Path,
    *,
    mode: Literal["warn", "strict"] = "warn",
    reserved_system_hardware_keys: frozenset[int],
) -> TagMap:
    """Build a `TagMap` from a Click nickname CSV file.

    Reads the CSV produced by Click Programming Software and reconstructs
    logical-to-hardware mappings:

    - **Explicit semantic markers** (`:block`, `:udt`, `:named_array`) →
      semantic blocks and structures mapped to hardware ranges.
    - **Bare/group markers** (for example ``<Name>`` or ``<Base.field>``) →
      editor grouping only; inner nicknames import as standalone tags.
    - **Standalone nicknames** → individual ``Tag`` objects.
    - ``_D`` suffix pairs (timer/counter accumulators) are linked
      automatically.
    - Initial values and retentive flags are preserved.

    Args:
        path: Path to the Click nickname CSV file.
        mode: Behavior for explicit ``:udt`` grouping failures:
            ``"warn"`` (default) falls back to plain blocks and records
            ``structure_warnings``; ``"strict"`` raises ``ValueError``.

    Returns:
        A `TagMap` ready for use with `validate()` and `to_nickname_file()`.

    Raises:
        FileNotFoundError: If the path does not exist.
        ValueError: If the CSV contains conflicting block boundaries or
            mismatched memory types, or if ``mode`` is invalid.
    """
    if mode not in {"warn", "strict"}:
        raise ValueError(f"Invalid mode {mode!r}; expected 'warn' or 'strict'.")

    records = pyclickplc.read_csv(path)
    rows = sorted(
        records.values(),
        key=lambda row: (MEMORY_TYPE_BASES[row.memory_type], row.address),
    )
    ranges = compute_all_block_ranges(cast(list, rows))

    mappings: list[MappingEntry] = []
    structures: list[StructuredImport] = []
    structure_warnings: list[str] = []
    named_array_spans: dict[str, tuple[str, int, int]] = {}
    seen_names: dict[str, tuple[str, int]] = {}
    covered_rows: set[int] = set()
    seen_semantic_block_names: set[str] = set()
    udt_groups: dict[str, list[tuple[_BlockImportSpec, str]]] = defaultdict(list)

    from pyrung.core import Field, named_array, udt

    def register_logical_name(name: str, *, memory_type: str, address: int) -> None:
        existing = seen_names.get(name)
        if existing is None:
            seen_names[name] = (memory_type, address)
            return
        if existing == (memory_type, address):
            return

        existing_display = format_address_display(existing[0], existing[1])
        display = format_address_display(memory_type, address)
        raise ValueError(
            f"Duplicate logical name {name!r} at {display}; already used at {existing_display}."
        )

    def require_representable_block_nickname(*, memory_type: str, address: int, name: str) -> None:
        display = format_address_display(memory_type, address)
        if name == "":
            return
        is_valid, error = validate_nickname(name)
        if not is_valid:
            raise ValueError(f"Block row nickname at {display} is not representable: {error}.")
        existing = seen_names.get(name)
        if existing is not None and existing != (memory_type, address):
            existing_display = format_address_display(existing[0], existing[1])
            raise ValueError(
                f"Block row nickname at {display} is not representable: duplicate logical "
                f"name {name!r} already used at {existing_display}."
            )
        seen_names[name] = (memory_type, address)

    def apply_block_rows(logical_block: Block, spec: _BlockImportSpec) -> None:
        logical_block._pyrung_click_bg_color = spec.bg_color  # ty: ignore[unresolved-attribute]
        logical_addresses = tuple(
            logical_block.select(logical_block.start, logical_block.end).addresses
        )
        hardware_to_logical = dict(zip(spec.hardware_addresses, logical_addresses, strict=True))

        for row_idx in range(spec.start_idx, spec.end_idx + 1):
            row = rows[row_idx]
            if row.memory_type != spec.memory_type:
                continue
            logical_addr = hardware_to_logical.get(row.address)
            if logical_addr is None:
                continue
            sv = logical_block.slot(logical_addr)
            if _is_marker_only_boundary_row(row, block_name=spec.name):
                continue

            if row.nickname != "":
                require_representable_block_nickname(
                    memory_type=row.memory_type,
                    address=row.address,
                    name=row.nickname,
                )
                if row.nickname != sv.name:
                    logical_block.slot(logical_addr, name=row.nickname)

            default = _parse_default(row.initial_value, logical_block.type)
            comment, tag_meta, _ = _extract_address_comment(row.comment)
            choices = tag_meta.choices if tag_meta is not None else None
            readonly = tag_meta.readonly if tag_meta is not None else False
            external = tag_meta.external if tag_meta is not None else False
            final = tag_meta.final if tag_meta is not None else False
            public = tag_meta.public if tag_meta is not None else False
            min_val = tag_meta.min if tag_meta is not None else None
            max_val = tag_meta.max if tag_meta is not None else None
            uom = tag_meta.uom if tag_meta is not None else None
            retentive_changed = row.retentive != sv.retentive
            default_changed = default != sv.default
            comment_changed = comment != sv.comment
            choices_changed = choices != sv.choices
            readonly_changed = readonly != sv.readonly
            external_changed = external != sv.external
            final_changed = final != sv.final
            public_changed = public != sv.public
            min_changed = min_val != sv.min
            max_changed = max_val != sv.max
            uom_changed = uom != sv.uom
            if (
                retentive_changed
                or default_changed
                or comment_changed
                or choices_changed
                or readonly_changed
                or external_changed
                or final_changed
                or public_changed
                or min_changed
                or max_changed
                or uom_changed
            ):
                slot_kw: dict[str, Any] = {}
                if retentive_changed:
                    slot_kw["retentive"] = row.retentive
                if default_changed:
                    slot_kw["default"] = default
                if comment_changed:
                    slot_kw["comment"] = comment
                if choices_changed:
                    slot_kw["choices"] = choices
                if readonly_changed:
                    slot_kw["readonly"] = readonly
                if external_changed:
                    slot_kw["external"] = external
                if final_changed:
                    slot_kw["final"] = final
                if public_changed:
                    slot_kw["public"] = public
                if min_changed:
                    slot_kw["min"] = min_val
                if max_changed:
                    slot_kw["max"] = max_val
                if uom_changed:
                    slot_kw["uom"] = uom
                logical_block.slot(logical_addr, **slot_kw)

    def inferred_block_start(spec: _BlockImportSpec, explicit_start: int | None) -> int:
        if explicit_start is not None:
            return explicit_start
        return _default_logical_block_start(spec.hardware_addresses)

    def import_plain_block(
        spec: _BlockImportSpec,
        *,
        logical_name: str | None = None,
        explicit_start: int | None = None,
    ) -> None:
        if not spec.hardware_addresses:
            return

        block_start = inferred_block_start(spec, explicit_start)
        logical_block = Block(
            name=spec.name if logical_name is None else logical_name,
            type=_tag_type_for_memory_type(spec.memory_type),
            start=block_start,
            end=block_start + len(spec.hardware_addresses) - 1,
            retentive=DEFAULT_RETENTIVE[spec.memory_type],
        )
        apply_block_rows(logical_block, spec)
        mappings.append(logical_block.map_to(spec.hardware_range))

    seen_base_names: dict[str, str] = {}  # base_name → kind label

    def _check_base_name(name: str, kind_label: str) -> None:
        existing = seen_base_names.get(name)
        if existing is not None:
            raise ValueError(f"Name {name!r} used as both {existing} and {kind_label}.")
        seen_base_names[name] = kind_label

    def append_structure(structure: StructuredImport) -> None:
        if any(existing.name == structure.name for existing in structures):
            raise ValueError(f"Duplicate structured import name {structure.name!r}.")
        structures.append(structure)

    for block_range in ranges:
        spec = _build_block_spec(rows, block_range)
        (
            kind,
            base_name,
            field_name,
            count,
            stride,
            logical_block_name,
            explicit_start,
            explicit_always_number,
        ) = _parse_structured_block_name(spec.name)

        if kind != "group":
            if block_range.name in seen_semantic_block_names:
                raise ValueError(f"Duplicate block definition name {block_range.name!r}.")
            seen_semantic_block_names.add(block_range.name)
            covered_rows.update(range(block_range.start_idx, block_range.end_idx + 1))

        if kind == "plain":
            _check_base_name(logical_block_name, "block")
            import_plain_block(
                spec,
                logical_name=logical_block_name,
                explicit_start=explicit_start,
            )
            continue

        if kind == "group":
            continue

        if kind == "named_array":
            assert base_name is not None
            _check_base_name(base_name, "named_array")

            total_rows = len(spec.hardware_addresses)
            if count is None:
                count = 1
            if stride is None:
                if total_rows % count != 0:
                    raise ValueError(
                        f"Named array {base_name!r} has {total_rows} rows "
                        f"which is not divisible by count={count}."
                    )
                stride = total_rows // count

            expected_span = count * stride
            if total_rows != expected_span:
                raise ValueError(
                    f"Named array {base_name!r} expects span {expected_span}, got {total_rows}."
                )

            address_to_position = {
                address: position
                for position, address in enumerate(spec.hardware_addresses, start=0)
            }
            numbered_pattern = re.compile(
                rf"^{re.escape(base_name)}(?P<instance>[1-9][0-9]*)_(?P<field>[A-Za-z_][A-Za-z0-9_]*)$"
            )
            compact_pattern = (
                re.compile(rf"^{re.escape(base_name)}_(?P<field>[A-Za-z_][A-Za-z0-9_]*)$")
                if count == 1
                else None
            )

            # Determine which naming convention to use.
            if count > 1 or explicit_always_number is True:
                use_always_number = True
            elif explicit_always_number is False:
                use_always_number = False
            else:
                # count==1, no explicit flag — detect from CSV field names.
                has_numbered = False
                has_compact = False
                for row_idx in range(spec.start_idx, spec.end_idx + 1):
                    row = rows[row_idx]
                    if row.memory_type != spec.memory_type or row.nickname == "":
                        continue
                    if address_to_position.get(row.address) is None:
                        continue
                    if _is_marker_only_boundary_row(row, block_name=spec.name):
                        continue
                    if numbered_pattern.fullmatch(row.nickname):
                        has_numbered = True
                    elif compact_pattern is not None and compact_pattern.fullmatch(row.nickname):
                        has_compact = True
                use_always_number = has_numbered and not has_compact

            nickname_pattern = (
                numbered_pattern if use_always_number or count > 1 else compact_pattern
            )
            assert nickname_pattern is not None

            is_compact = not use_always_number and count == 1
            field_offsets: dict[str, int] = {}
            field_rows: dict[tuple[str, int], AddressRecord] = {}
            for row_idx in range(spec.start_idx, spec.end_idx + 1):
                row = rows[row_idx]
                if row.memory_type != spec.memory_type:
                    continue
                position = address_to_position.get(row.address)
                if position is None:
                    continue
                if row.nickname == "":
                    continue

                if _is_marker_only_boundary_row(row, block_name=spec.name):
                    continue

                match = nickname_pattern.fullmatch(row.nickname)
                display = format_address_display(row.memory_type, row.address)
                if match is None:
                    if is_compact:
                        expected_fmt = f"{base_name}_{{field}}"
                    else:
                        expected_fmt = f"{base_name}{{instance}}_{{field}}"
                    raise ValueError(
                        f"Named array {base_name!r} row at {display} has invalid nickname "
                        f"{row.nickname!r}; expected {expected_fmt}."
                    )

                if is_compact:
                    instance = 1
                else:
                    instance = int(match.group("instance"))
                field = match.group("field")
                if instance < 1 or instance > count:
                    raise ValueError(
                        f"Named array {base_name!r} row at {display} has instance {instance}; "
                        f"expected range 1..{count}."
                    )

                expected_instance = position // stride + 1
                if instance != expected_instance:
                    raise ValueError(
                        f"Named array {base_name!r} row at {display} maps to instance "
                        f"{expected_instance}, but nickname encodes {instance}."
                    )

                offset = position % stride
                existing_offset = field_offsets.get(field)
                if existing_offset is None:
                    field_offsets[field] = offset
                elif existing_offset != offset:
                    raise ValueError(
                        f"Named array {base_name!r} field {field!r} appears at offset "
                        f"{offset} and {existing_offset}."
                    )

                key = (field, instance)
                if key in field_rows:
                    raise ValueError(
                        f"Named array {base_name!r} has duplicate row for field {field!r} "
                        f"instance {instance} at {display}."
                    )
                field_rows[key] = row
                register_logical_name(
                    row.nickname, memory_type=row.memory_type, address=row.address
                )

            if not field_offsets:
                raise ValueError(
                    f"Named array {base_name!r} did not infer any fields from nicknames."
                )

            if count > 1:
                # Instance 1 defines the template — all fields must appear there.
                instance1_fields = {f for (f, inst) in field_rows if inst == 1}
                if not instance1_fields:
                    raise ValueError(
                        f"Named array {base_name!r} instance 1 has no named fields; "
                        "instance 1 must define the field template."
                    )
                for f, inst in field_rows:
                    if inst > 1 and f not in instance1_fields:
                        raise ValueError(
                            f"Named array {base_name!r} instance {inst} introduces field "
                            f"{f!r} not present in instance 1."
                        )

            ordered_fields_with_offsets = sorted(field_offsets.items(), key=lambda item: item[1])
            for field, offset in ordered_fields_with_offsets:
                for instance in range(1, count + 1):
                    key = (field, instance)
                    row = field_rows.get(key)
                    expected_address = spec.hardware_addresses[(instance - 1) * stride + offset]
                    if row is None:
                        continue
                    if row.address != expected_address:
                        display = format_address_display(row.memory_type, row.address)
                        expected_display = format_address_display(
                            spec.memory_type, expected_address
                        )
                        raise ValueError(
                            f"Named array {base_name!r} field {field!r} instance {instance} "
                            f"maps to {display}, expected {expected_display}."
                        )

            runtime_namespace: dict[str, object] = {"__module__": __name__}
            for field, _ in ordered_fields_with_offsets:
                runtime_namespace[field] = Field()
            runtime_type = cast(type[Any], type(base_name, (), runtime_namespace))
            inferred_always_number = use_always_number and count == 1
            runtime = named_array(
                _tag_type_for_memory_type(spec.memory_type),
                count=count,
                stride=stride,
                always_number=inferred_always_number,
            )(runtime_type)
            runtime._pyrung_click_bg_color = spec.bg_color  # ty: ignore[unresolved-attribute]
            runtime_blocks = cast(dict[str, Block], cast(Any, runtime)._blocks)

            for field, _ in ordered_fields_with_offsets:
                block = runtime_blocks[field]
                for instance in range(1, count + 1):
                    row = field_rows.get((field, instance))
                    if row is None:
                        continue
                    sv = block.slot(instance)
                    if row.nickname != sv.name:
                        block.slot(instance, name=row.nickname)

                    default = _parse_default(row.initial_value, block.type)
                    comment, tag_meta, _ = _extract_address_comment(row.comment)
                    choices = tag_meta.choices if tag_meta is not None else None
                    readonly = tag_meta.readonly if tag_meta is not None else False
                    external = tag_meta.external if tag_meta is not None else False
                    final = tag_meta.final if tag_meta is not None else False
                    public = tag_meta.public if tag_meta is not None else False
                    min_val = tag_meta.min if tag_meta is not None else None
                    max_val = tag_meta.max if tag_meta is not None else None
                    uom = tag_meta.uom if tag_meta is not None else None
                    retentive_changed = row.retentive != sv.retentive
                    default_changed = default != sv.default
                    comment_changed = comment != sv.comment
                    choices_changed = choices != sv.choices
                    readonly_changed = readonly != sv.readonly
                    external_changed = external != sv.external
                    final_changed = final != sv.final
                    public_changed = public != sv.public
                    min_changed = min_val != sv.min
                    max_changed = max_val != sv.max
                    uom_changed = uom != sv.uom
                    if (
                        retentive_changed
                        or default_changed
                        or comment_changed
                        or choices_changed
                        or readonly_changed
                        or external_changed
                        or final_changed
                        or public_changed
                        or min_changed
                        or max_changed
                        or uom_changed
                    ):
                        slot_kw: dict[str, Any] = {}
                        if retentive_changed:
                            slot_kw["retentive"] = row.retentive
                        if default_changed:
                            slot_kw["default"] = default
                        if comment_changed:
                            slot_kw["comment"] = comment
                        if choices_changed:
                            slot_kw["choices"] = choices
                        if readonly_changed:
                            slot_kw["readonly"] = readonly
                        if external_changed:
                            slot_kw["external"] = external
                        if final_changed:
                            slot_kw["final"] = final
                        if public_changed:
                            slot_kw["public"] = public
                        if min_changed:
                            slot_kw["min"] = min_val
                        if max_changed:
                            slot_kw["max"] = max_val
                        if uom_changed:
                            slot_kw["uom"] = uom
                        block.slot(instance, **slot_kw)

            mappings.extend(runtime.map_to(spec.hardware_range))
            append_structure(
                StructuredImport(
                    name=base_name,
                    kind="named_array",
                    runtime=runtime,
                    count=count,
                    stride=stride,
                )
            )
            named_array_spans[base_name] = (
                spec.memory_type,
                spec.hardware_addresses[0],
                spec.hardware_addresses[-1],
            )
            continue

        assert kind == "udt"
        assert base_name is not None
        assert field_name is not None
        if base_name not in udt_groups:
            _check_base_name(base_name, "udt")
        udt_groups[base_name].append((spec, field_name))

    for base_name, grouped_specs in udt_groups.items():
        fallback_reason: str | None = None

        field_names = [field_name for _, field_name in grouped_specs]
        if len(set(field_names)) != len(field_names):
            fallback_reason = "duplicate field names"

        logical_counts = {len(spec.hardware_addresses) for spec, _ in grouped_specs}
        if fallback_reason is None and len(logical_counts) != 1:
            fallback_reason = "field spans have different logical counts"

        if fallback_reason is None and 0 in logical_counts:
            fallback_reason = "one or more fields have an empty hardware span"

        if fallback_reason is None:
            try:
                runtime_annotations: dict[str, object] = {}
                for spec, field_name in grouped_specs:
                    runtime_annotations[field_name] = _tag_type_for_memory_type(spec.memory_type)

                runtime_type = cast(
                    type[Any],
                    type(
                        base_name,
                        (),
                        {"__annotations__": runtime_annotations, "__module__": __name__},
                    ),
                )
                count = next(iter(logical_counts))
                runtime = udt(count=count)(runtime_type)
                runtime_blocks = cast(dict[str, Block], cast(Any, runtime)._blocks)

                for spec, field_name in grouped_specs:
                    logical_block = runtime_blocks[field_name]
                    apply_block_rows(logical_block, spec)
                    mappings.append(logical_block.map_to(spec.hardware_range))

                append_structure(
                    StructuredImport(
                        name=base_name,
                        kind="udt",
                        runtime=runtime,
                        count=count,
                        stride=None,
                    )
                )
                continue
            except Exception as exc:  # pragma: no cover - defensive fallback
                fallback_reason = str(exc)

        assert fallback_reason is not None
        if mode == "strict":
            raise ValueError(f"UDT grouping failed for base {base_name!r}: {fallback_reason}.")

        structure_warnings.append(
            f"UDT grouping for {base_name!r} failed ({fallback_reason}); imported as plain blocks."
        )
        for spec, field_name in grouped_specs:
            import_plain_block(spec, logical_name=f"{base_name}.{field_name}")

    for idx, row in enumerate(rows):
        if idx in covered_rows:
            continue
        if row.nickname == "":
            continue
        if get_addr_key(row.memory_type, row.address) in reserved_system_hardware_keys:
            continue

        register_logical_name(row.nickname, memory_type=row.memory_type, address=row.address)

        memory_type = row.memory_type
        logical_type = _tag_type_for_memory_type(memory_type)
        comment, tag_meta, _ = _extract_address_comment(row.comment)
        logical = Tag(
            name=row.nickname,
            type=logical_type,
            retentive=row.retentive,
            default=_parse_default(row.initial_value, logical_type),
            comment=comment,
            choices=_normalize_choices(
                tag_meta.choices if tag_meta is not None else None,
                tag_type=logical_type,
                owner=f"{row.nickname!r} choices",
            ),
            readonly=tag_meta.readonly if tag_meta is not None else False,
            external=tag_meta.external if tag_meta is not None else False,
            final=tag_meta.final if tag_meta is not None else False,
            public=tag_meta.public if tag_meta is not None else False,
            min=tag_meta.min if tag_meta is not None else None,
            max=tag_meta.max if tag_meta is not None else None,
            uom=tag_meta.uom if tag_meta is not None else None,
        )
        hardware = _hardware_block_for(memory_type)[row.address]
        mappings.append(logical.map_to(hardware))

    mapping = cls(mappings)
    mapping._structures = tuple(structures)
    mapping._structure_by_name = {structure.name: structure for structure in structures}
    mapping._structure_warnings = tuple(structure_warnings)
    mapping._named_array_spans = named_array_spans
    return mapping


def write_tag_map_to_nickname_file(self, path: str | Path) -> int:
    """Write mapped addresses to a Click nickname CSV file.

    Emits one row per mapped hardware address.  Block entries produce
    rows with explicit semantic markers (``:block``, ``:udt``,
    ``:named_array(...)``) or raw grouping tags for non-semantic
    comments. Unmapped addresses are omitted.

    Args:
        path: Destination CSV path. Parent directories must exist.

    Returns:
        Number of rows written.
    """
    records: dict[int, AddressRecord] = {}

    def block_tag_name_for_entry(entry: _BlockEntry) -> str | None:
        provenance = self._structure_provenance(entry.logical)
        if provenance is not None:
            kind, name, _ = provenance
            if kind == "named_array":
                return None
            if kind == "udt":
                field = cast(str, entry.logical._pyrung_structure_field)  # ty: ignore[unresolved-attribute]
                return f"{name}.{field}:udt"

        default_start = _default_logical_block_start(entry.hardware_addresses)
        if entry.logical.start == default_start:
            return f"{entry.logical.name}:block"
        return f"{entry.logical.name}:block({entry.logical.start})"

    for entry in self._tag_entries_tuple:
        memory_type, address = self._parse_hardware_tag(entry.hardware)
        tag_meta = _tag_meta_from_hints(
            choices=getattr(entry.logical, "choices", None),
            readonly=getattr(entry.logical, "readonly", False),
            external=getattr(entry.logical, "external", False),
            final=getattr(entry.logical, "final", False),
            public=getattr(entry.logical, "public", False),
            min=getattr(entry.logical, "min", None),
            max=getattr(entry.logical, "max", None),
            uom=getattr(entry.logical, "uom", None),
        )
        records[get_addr_key(memory_type, address)] = AddressRecord(
            memory_type=memory_type,
            address=address,
            nickname=entry.logical.name,
            comment=_compose_address_comment(entry.logical.comment, tag_meta=tag_meta),
            initial_value=_format_default(entry.logical.default, entry.logical.type),
            retentive=entry.logical.retentive,
            data_type=BANKS[memory_type].data_type,
        )

    for entry in self._block_entries_tuple:
        if not entry.hardware_addresses:
            continue
        block_tag_name = block_tag_name_for_entry(entry)
        block_bg_color = getattr(entry.logical, "_pyrung_click_bg_color", None)
        memory_type, _ = self._parse_hardware_tag(entry.hardware.block[entry.hardware_addresses[0]])
        block_len = len(entry.hardware_addresses)

        for i, (logical_addr, hardware_addr) in enumerate(
            zip(entry.logical_addresses, entry.hardware_addresses, strict=True)
        ):
            slot = entry.logical[logical_addr]
            block_tag = ""
            if block_tag_name is not None:
                if block_len == 1:
                    block_tag = format_block_tag(
                        block_tag_name,
                        "self-closing",
                        bg_color=block_bg_color,
                    )
                elif i == 0:
                    block_tag = format_block_tag(block_tag_name, "open", bg_color=block_bg_color)
                elif i == block_len - 1:
                    block_tag = format_block_tag(block_tag_name, "close")

            tag_meta = _tag_meta_from_hints(
                choices=slot.choices,
                readonly=slot.readonly,
                external=slot.external,
                final=slot.final,
                public=slot.public,
                min=slot.min,
                max=slot.max,
                uom=slot.uom,
            )

            records[get_addr_key(memory_type, hardware_addr)] = AddressRecord(
                memory_type=memory_type,
                address=hardware_addr,
                nickname=slot.name,
                comment=_compose_address_comment(slot.comment, block_tag, tag_meta),
                initial_value=_format_default(slot.default, slot.type),
                retentive=slot.retentive,
                data_type=BANKS[memory_type].data_type,
            )

    def write_boundary_comment(memory_type: str, address: int, block_tag: str) -> None:
        addr_key = get_addr_key(memory_type, address)
        existing = records.get(addr_key)
        if existing is None:
            records[addr_key] = AddressRecord(
                memory_type=memory_type,
                address=address,
                nickname="",
                comment=block_tag,
                initial_value="",
                retentive=DEFAULT_RETENTIVE[memory_type],
                data_type=BANKS[memory_type].data_type,
            )
            return
        existing_comment, existing_meta, _ = _extract_address_comment(existing.comment)
        records[addr_key] = replace(
            existing,
            comment=_compose_address_comment(existing_comment, block_tag, existing_meta),
        )

    for structure in self._structures:
        if structure.kind != "named_array":
            continue
        span = self._named_array_spans.get(structure.name)
        if span is None:
            continue

        memory_type, start_address, end_address = span
        stride = cast(int, structure.stride)
        always_number_suffix = (
            ",always_number"
            if getattr(structure.runtime, "always_number", False) and structure.count == 1
            else ""
        )
        bg_color = getattr(structure.runtime, "_pyrung_click_bg_color", None)
        block_name = (
            f"{structure.name}:named_array({structure.count},{stride}{always_number_suffix})"
        )
        if structure.count * stride == 1:
            write_boundary_comment(
                memory_type,
                start_address,
                format_block_tag(block_name, "self-closing", bg_color=bg_color),
            )
            continue

        write_boundary_comment(
            memory_type,
            start_address,
            format_block_tag(block_name, "open", bg_color=bg_color),
        )
        write_boundary_comment(memory_type, end_address, format_block_tag(block_name, "close"))

    return pyclickplc.write_csv(path, records)


__all__ = ["tag_map_from_nickname_file", "write_tag_map_to_nickname_file"]
