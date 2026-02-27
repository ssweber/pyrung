"""CircuitPython code generation (feature-complete v1)."""

from __future__ import annotations

import hashlib
import inspect
import textwrap
from dataclasses import dataclass, field
from typing import Any

from pyrung.circuitpy.codegen._util import _first_defined_name, _io_kind, _mangle_symbol
from pyrung.circuitpy.hardware import P1AM
from pyrung.core.condition import (
    Condition,
    FallingEdgeCondition,
    RisingEdgeCondition,
)
from pyrung.core.copy_modifiers import CopyModifier
from pyrung.core.expression import (
    Expression,
)
from pyrung.core.memory_block import (
    Block,
    BlockRange,
    IndirectBlockRange,
    IndirectExprRef,
    IndirectRef,
    InputBlock,
    OutputBlock,
)
from pyrung.core.program import Program
from pyrung.core.rung import Rung as LogicRung
from pyrung.core.tag import Tag, TagType
from pyrung.core.validation.walker import _INSTRUCTION_FIELDS, _condition_children


@dataclass(frozen=True)
class SlotBinding:
    slot_number: int
    part_number: str
    input_block_id: int | None
    output_block_id: int | None
    input_kind: str | None
    output_kind: str | None
    input_count: int
    output_count: int


@dataclass(frozen=True)
class BlockBinding:
    block: Block
    block_id: int
    logical_name: str
    start: int
    end: int
    valid_addresses: tuple[int, ...] | None
    tag_type: TagType
    slot_number: int | None
    part_number: str | None
    direction: str | None
    channel_count: int | None


@dataclass
class CodegenContext:
    program: Program
    hw: P1AM
    target_scan_ms: float
    watchdog_ms: int | None

    slot_bindings: list[SlotBinding] = field(default_factory=list)
    block_bindings: dict[int, BlockBinding] = field(default_factory=dict)
    scalar_tags: dict[str, Tag] = field(default_factory=dict)
    referenced_tags: dict[str, Tag] = field(default_factory=dict)
    retentive_tags: dict[str, Tag] = field(default_factory=dict)
    edge_prev_tags: set[str] = field(default_factory=set)

    subroutine_names: list[str] = field(default_factory=list)
    function_sources: dict[str, str] = field(default_factory=dict)
    function_globals: dict[str, set[str]] = field(default_factory=dict)
    used_helpers: set[str] = field(default_factory=set)
    symbol_table: dict[str, str] = field(default_factory=dict)

    block_symbols: dict[int, str] = field(default_factory=dict)
    tag_block_addresses: dict[str, tuple[int, int]] = field(default_factory=dict)
    used_indirect_blocks: set[int] = field(default_factory=set)
    function_symbols_by_obj: dict[int, str] = field(default_factory=dict)
    _current_function: str | None = None
    _name_counters: dict[str, int] = field(default_factory=dict)
    _state_key_counter: int = 0
    _state_keys_by_obj: dict[int, str] = field(default_factory=dict)

    def collect_hw_bindings(self) -> None:
        self.slot_bindings.clear()
        for slot_number, (spec, configured) in sorted(self.hw._slots.items()):
            input_block: InputBlock | None = None
            output_block: OutputBlock | None = None
            if isinstance(configured, tuple):
                first, second = configured
                if isinstance(first, InputBlock):
                    input_block = first
                if isinstance(first, OutputBlock):
                    output_block = first
                if isinstance(second, InputBlock):
                    input_block = second
                if isinstance(second, OutputBlock):
                    output_block = second
            elif isinstance(configured, InputBlock):
                input_block = configured
            elif isinstance(configured, OutputBlock):
                output_block = configured

            in_id = self._ensure_block_binding(
                input_block,
                slot_number=slot_number,
                part_number=spec.part_number,
                direction="input",
                channel_count=spec.input_group.count if spec.input_group is not None else None,
            )
            out_id = self._ensure_block_binding(
                output_block,
                slot_number=slot_number,
                part_number=spec.part_number,
                direction="output",
                channel_count=spec.output_group.count if spec.output_group is not None else None,
            )

            self.slot_bindings.append(
                SlotBinding(
                    slot_number=slot_number,
                    part_number=spec.part_number,
                    input_block_id=in_id,
                    output_block_id=out_id,
                    input_kind=_io_kind(spec.input_group.tag_type) if spec.input_group else None,
                    output_kind=_io_kind(spec.output_group.tag_type) if spec.output_group else None,
                    input_count=spec.input_group.count if spec.input_group else 0,
                    output_count=spec.output_group.count if spec.output_group else 0,
                )
            )

    def collect_program_references(self) -> None:
        self.referenced_tags.clear()
        self.edge_prev_tags.clear()
        self.subroutine_names = sorted(self.program.subroutines)
        seen_values: set[int] = set()

        def walk_rung(rung: LogicRung) -> None:
            for cond in rung._conditions:
                walk_value(cond)
            for item in rung._execution_items:
                if isinstance(item, LogicRung):
                    walk_rung(item)
                else:
                    walk_instruction(item)

        def walk_instruction(instr: Any) -> None:
            fields = _INSTRUCTION_FIELDS.get(type(instr).__name__)
            if fields is None:
                for key in sorted(vars(instr)):
                    if key.startswith("_"):
                        continue
                    walk_value(getattr(instr, key))
            else:
                for key in fields:
                    walk_value(getattr(instr, key))
            if hasattr(instr, "oneshot"):
                walk_value(instr.oneshot)
            if hasattr(instr, "_ins"):
                ins = instr._ins
                if isinstance(ins, dict):
                    for key in sorted(ins, key=repr):
                        walk_value(ins[key])
            if hasattr(instr, "_outs"):
                outs = instr._outs
                if isinstance(outs, dict):
                    for key in sorted(outs, key=repr):
                        walk_value(outs[key])
            child_instructions = getattr(instr, "instructions", None)
            if isinstance(child_instructions, list):
                for child in child_instructions:
                    walk_instruction(child)

        def walk_value(value: Any) -> None:
            value_id = id(value)
            if value_id in seen_values:
                return
            seen_values.add(value_id)

            if isinstance(value, Tag):
                self.referenced_tags.setdefault(value.name, value)
                self._associate_tag_with_known_block(value)
                return

            if isinstance(value, IndirectRef):
                self._ensure_block_binding(value.block)
                walk_value(value.pointer)
                return

            if isinstance(value, IndirectExprRef):
                self._ensure_block_binding(value.block)
                walk_value(value.expr)
                return

            if isinstance(value, BlockRange):
                self._ensure_block_binding(value.block)
                for addr in value.addresses:
                    tag = value.block._get_tag(addr)
                    self.tag_block_addresses[tag.name] = (id(value.block), addr)
                    self.referenced_tags.setdefault(tag.name, tag)
                return

            if isinstance(value, IndirectBlockRange):
                self._ensure_block_binding(value.block)
                walk_value(value.start_expr)
                walk_value(value.end_expr)
                return

            if isinstance(value, CopyModifier):
                walk_value(value.source)
                return

            if isinstance(value, Condition):
                if isinstance(value, (RisingEdgeCondition, FallingEdgeCondition)):
                    self.edge_prev_tags.add(value.tag.name)
                for _, child in _condition_children(value):
                    walk_value(child)
                return

            if isinstance(value, Expression):
                for key in sorted(vars(value)):
                    if key.startswith("_"):
                        continue
                    walk_value(getattr(value, key))
                return

            if isinstance(value, dict):
                for key in sorted(value, key=repr):
                    walk_value(value[key])
                return

            if isinstance(value, (list, tuple, set, frozenset)):
                for item in value:
                    walk_value(item)

        for rung in self.program.rungs:
            walk_rung(rung)
        for sub_name in self.subroutine_names:
            for rung in self.program.subroutines[sub_name]:
                walk_rung(rung)

    def collect_retentive_tags(self) -> None:
        self.retentive_tags = {
            name: self.referenced_tags[name]
            for name in sorted(self.referenced_tags)
            if self.referenced_tags[name].retentive
        }

    def assign_symbols(self) -> None:
        self.symbol_table.clear()
        self.block_symbols.clear()
        self.scalar_tags.clear()
        used: set[str] = set()

        block_items = sorted(
            self.block_bindings.values(),
            key=lambda b: (b.logical_name, b.block_id),
        )
        for binding in block_items:
            key = f"block:{binding.logical_name}:{binding.block_id}"
            symbol = _mangle_symbol(binding.logical_name, "_b_", used)
            self.symbol_table[key] = symbol
            self.block_symbols[binding.block_id] = symbol

        for tag_name in sorted(self.referenced_tags):
            if tag_name in self.tag_block_addresses:
                continue
            tag = self.referenced_tags[tag_name]
            symbol = _mangle_symbol(tag_name, "_t_", used)
            self.symbol_table[tag_name] = symbol
            self.scalar_tags[tag_name] = tag

    def compute_retentive_schema_hash(self) -> str:
        lines = [
            f"{name}:{self.retentive_tags[name].type.name}" for name in sorted(self.retentive_tags)
        ]
        joined = "\n".join(lines).encode("utf-8")
        return hashlib.sha256(joined).hexdigest()

    def mark_helper(self, helper_name: str) -> None:
        self.used_helpers.add(helper_name)

    def mark_function_global(self, fn_name: str, symbol: str) -> None:
        self.function_globals.setdefault(fn_name, set()).add(symbol)

    def globals_for_function(self, fn_name: str) -> list[str]:
        return sorted(self.function_globals.get(fn_name, set()))

    def symbol_for_tag(self, tag: Tag) -> str:
        block_info = self.tag_block_addresses.get(tag.name)
        if block_info is not None:
            block_id, addr = block_info
            binding = self.block_bindings.get(block_id)
            symbol = self.block_symbols.get(block_id)
            if binding is None or symbol is None:
                raise RuntimeError(f"Missing block binding for tag {tag.name!r}")
            index = addr - binding.start
            if index < 0 or index > (binding.end - binding.start):
                raise RuntimeError(f"Tag address mapping out of range for {tag.name!r}")
            if self._current_function is not None:
                self.mark_function_global(self._current_function, symbol)
            return f"{symbol}[{index}]"

        symbol = self.symbol_table.get(tag.name)
        if symbol is None:
            raise RuntimeError(f"Missing scalar symbol for tag {tag.name!r}")
        if self._current_function is not None:
            self.mark_function_global(self._current_function, symbol)
        return symbol

    def symbol_for_block(self, block: Block) -> str:
        block_id = id(block)
        symbol = self.block_symbols.get(block_id)
        if symbol is None:
            raise RuntimeError(f"Missing block symbol for {block.name!r}")
        if self._current_function is not None:
            self.mark_function_global(self._current_function, symbol)
        return symbol

    def set_current_function(self, fn_name: str | None) -> None:
        self._current_function = fn_name
        if fn_name is not None:
            self.function_globals.setdefault(fn_name, set())

    def next_name(self, prefix: str) -> str:
        n = self._name_counters.get(prefix, 0) + 1
        self._name_counters[prefix] = n
        return f"{prefix}_{n}"

    def reset_name_counters(self) -> None:
        self._name_counters.clear()

    def state_key_for(self, obj: Any) -> str:
        obj_id = id(obj)
        existing = self._state_keys_by_obj.get(obj_id)
        if existing is not None:
            return existing
        self._state_key_counter += 1
        key = f"i{self._state_key_counter}"
        self._state_keys_by_obj[obj_id] = key
        return key

    def index_helper_name(self, block_id: int) -> str:
        symbol = self.block_symbols.get(block_id)
        if symbol is None:
            raise RuntimeError(f"Missing block symbol for block id {block_id}")
        name = symbol.lstrip("_") or symbol
        return f"_resolve_index_{name}"

    def use_indirect_block(self, block_id: int) -> str:
        self.used_indirect_blocks.add(block_id)
        return self.index_helper_name(block_id)

    def symbol_if_referenced(self, tag_name: str) -> str | None:
        tag = self.referenced_tags.get(tag_name)
        if tag is None:
            return None
        return self.symbol_for_tag(tag)

    def register_function_source(self, fn: Any) -> str:
        key = id(fn)
        existing = self.function_symbols_by_obj.get(key)
        if existing is not None:
            return existing

        try:
            source = inspect.getsource(fn)
        except (OSError, TypeError) as exc:
            raise ValueError(f"Could not inspect source for callable {fn!r}") from exc
        rendered = textwrap.dedent(source).rstrip()
        if not rendered:
            raise ValueError(f"Could not inspect source for callable {fn!r}")
        if _first_defined_name(rendered) is None:
            raise ValueError(f"Callable source is not inspectable for embedding: {fn!r}")

        fn_name = getattr(fn, "__name__", type(fn).__name__)
        symbol = _mangle_symbol(fn_name, "_fn_", set(self.function_sources))
        self.function_sources[symbol] = rendered
        self.function_symbols_by_obj[key] = symbol
        return symbol

    def _ensure_block_binding(
        self,
        block: Block | None,
        *,
        slot_number: int | None = None,
        part_number: str | None = None,
        direction: str | None = None,
        channel_count: int | None = None,
    ) -> int | None:
        if block is None:
            return None
        block_id = id(block)
        if block_id in self.block_bindings:
            return block_id
        valid_addresses: tuple[int, ...] | None = None
        if block.valid_ranges is not None:
            window = block._window_addresses(block.start, block.end)
            valid_addresses = tuple(window) if isinstance(window, tuple) else tuple(window)
        binding = BlockBinding(
            block=block,
            block_id=block_id,
            logical_name=block.name,
            start=block.start,
            end=block.end,
            valid_addresses=valid_addresses,
            tag_type=block.type,
            slot_number=slot_number,
            part_number=part_number,
            direction=direction,
            channel_count=channel_count,
        )
        self.block_bindings[block_id] = binding
        for addr, tag in block._tag_cache.items():
            self.tag_block_addresses[tag.name] = (block_id, addr)
        return block_id

    def _associate_tag_with_known_block(self, tag: Tag) -> None:
        if tag.name in self.tag_block_addresses:
            return
        for binding in sorted(
            self.block_bindings.values(), key=lambda b: (b.logical_name, b.block_id)
        ):
            for addr, cached_tag in binding.block._tag_cache.items():
                if cached_tag is tag:
                    self.tag_block_addresses[tag.name] = (binding.block_id, addr)
                    return
