"""CircuitPython code generation (Step 1 foundation)."""

from __future__ import annotations

import hashlib
import math as _math
import re
from dataclasses import dataclass, field
from typing import Any

from pyrung.circuitpy.hardware import P1AM
from pyrung.circuitpy.validation import validate_circuitpy_program
from pyrung.core.condition import (
    AllCondition,
    AnyCondition,
    BitCondition,
    CompareEq,
    CompareGe,
    CompareGt,
    CompareLe,
    CompareLt,
    CompareNe,
    Condition,
    FallingEdgeCondition,
    IndirectCompareEq,
    IndirectCompareGe,
    IndirectCompareGt,
    IndirectCompareLe,
    IndirectCompareLt,
    IndirectCompareNe,
    IntTruthyCondition,
    NormallyClosedCondition,
    RisingEdgeCondition,
)
from pyrung.core.copy_modifiers import CopyModifier
from pyrung.core.expression import (
    AbsExpr,
    AddExpr,
    AndExpr,
    DivExpr,
    ExprCompareEq,
    ExprCompareGe,
    ExprCompareGt,
    ExprCompareLe,
    ExprCompareLt,
    ExprCompareNe,
    Expression,
    FloorDivExpr,
    InvertExpr,
    LiteralExpr,
    LShiftExpr,
    MathFuncExpr,
    ModExpr,
    MulExpr,
    NegExpr,
    OrExpr,
    PosExpr,
    PowExpr,
    RShiftExpr,
    ShiftFuncExpr,
    SubExpr,
    TagExpr,
    XorExpr,
)
from pyrung.core.instruction import LatchInstruction, OutInstruction, ResetInstruction
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

_IDENT_RE = re.compile(r"[^A-Za-z0-9_]")
_TYPE_DEFAULTS: dict[TagType, Any] = {
    TagType.BOOL: False,
    TagType.INT: 0,
    TagType.DINT: 0,
    TagType.REAL: 0.0,
    TagType.WORD: 0,
    TagType.CHAR: "",
}
_HELPER_ORDER = (
    "_clamp_int",
    "_wrap_int",
    "_rise",
    "_fall",
    "_int_to_float_bits",
    "_float_to_int_bits",
    "_parse_pack_text_value",
    "_store_copy_value_to_type",
)


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

    subroutine_names: list[str] = field(default_factory=list)
    function_sources: dict[str, str] = field(default_factory=dict)
    function_globals: dict[str, set[str]] = field(default_factory=dict)
    used_helpers: set[str] = field(default_factory=set)
    symbol_table: dict[str, str] = field(default_factory=dict)

    block_symbols: dict[int, str] = field(default_factory=dict)
    tag_block_addresses: dict[str, tuple[int, int]] = field(default_factory=dict)
    used_indirect_blocks: set[int] = field(default_factory=set)
    _current_function: str | None = None
    _name_counters: dict[str, int] = field(default_factory=dict)

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
                    output_kind=_io_kind(spec.output_group.tag_type)
                    if spec.output_group
                    else None,
                    input_count=spec.input_group.count if spec.input_group else 0,
                    output_count=spec.output_group.count if spec.output_group else 0,
                )
            )

    def collect_program_references(self) -> None:
        self.referenced_tags.clear()
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

    def index_helper_name(self, block_id: int) -> str:
        symbol = self.block_symbols.get(block_id)
        if symbol is None:
            raise RuntimeError(f"Missing block symbol for block id {block_id}")
        name = symbol.lstrip("_") or symbol
        return f"_resolve_index_{name}"

    def use_indirect_block(self, block_id: int) -> str:
        self.used_indirect_blocks.add(block_id)
        return self.index_helper_name(block_id)

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
        for binding in sorted(self.block_bindings.values(), key=lambda b: (b.logical_name, b.block_id)):
            for addr, cached_tag in binding.block._tag_cache.items():
                if cached_tag is tag:
                    self.tag_block_addresses[tag.name] = (binding.block_id, addr)
                    return


def compile_condition(cond: Condition, ctx: CodegenContext) -> str:
    """Return a Python boolean expression string."""
    if isinstance(cond, BitCondition):
        return f"bool({ctx.symbol_for_tag(cond.tag)})"
    if isinstance(cond, NormallyClosedCondition):
        return f"(not bool({ctx.symbol_for_tag(cond.tag)}))"
    if isinstance(cond, IntTruthyCondition):
        return f"(int({ctx.symbol_for_tag(cond.tag)}) != 0)"
    if isinstance(cond, CompareEq):
        return f"({_compile_value(cond.tag, ctx)} == {_compile_value(cond.value, ctx)})"
    if isinstance(cond, CompareNe):
        return f"({_compile_value(cond.tag, ctx)} != {_compile_value(cond.value, ctx)})"
    if isinstance(cond, CompareLt):
        return f"({_compile_value(cond.tag, ctx)} < {_compile_value(cond.value, ctx)})"
    if isinstance(cond, CompareLe):
        return f"({_compile_value(cond.tag, ctx)} <= {_compile_value(cond.value, ctx)})"
    if isinstance(cond, CompareGt):
        return f"({_compile_value(cond.tag, ctx)} > {_compile_value(cond.value, ctx)})"
    if isinstance(cond, CompareGe):
        return f"({_compile_value(cond.tag, ctx)} >= {_compile_value(cond.value, ctx)})"
    if isinstance(cond, AllCondition):
        parts = [compile_condition(child, ctx) for child in cond.conditions]
        return "(" + " and ".join(parts) + ")" if parts else "True"
    if isinstance(cond, AnyCondition):
        parts = [compile_condition(child, ctx) for child in cond.conditions]
        return "(" + " or ".join(parts) + ")" if parts else "False"
    if isinstance(cond, RisingEdgeCondition):
        ctx.mark_helper("_rise")
        if ctx._current_function is not None:
            ctx.mark_function_global(ctx._current_function, "_prev")
        tag_expr = ctx.symbol_for_tag(cond.tag)
        return f'_rise(bool({tag_expr}), bool(_prev.get("{cond.tag.name}", False)))'
    if isinstance(cond, FallingEdgeCondition):
        ctx.mark_helper("_fall")
        if ctx._current_function is not None:
            ctx.mark_function_global(ctx._current_function, "_prev")
        tag_expr = ctx.symbol_for_tag(cond.tag)
        return f'_fall(bool({tag_expr}), bool(_prev.get("{cond.tag.name}", False)))'
    if isinstance(cond, IndirectCompareEq):
        return f"({_compile_indirect_value(cond.indirect_ref, ctx)} == {_compile_value(cond.value, ctx)})"
    if isinstance(cond, IndirectCompareNe):
        return f"({_compile_indirect_value(cond.indirect_ref, ctx)} != {_compile_value(cond.value, ctx)})"
    if isinstance(cond, IndirectCompareLt):
        return f"({_compile_indirect_value(cond.indirect_ref, ctx)} < {_compile_value(cond.value, ctx)})"
    if isinstance(cond, IndirectCompareLe):
        return f"({_compile_indirect_value(cond.indirect_ref, ctx)} <= {_compile_value(cond.value, ctx)})"
    if isinstance(cond, IndirectCompareGt):
        return f"({_compile_indirect_value(cond.indirect_ref, ctx)} > {_compile_value(cond.value, ctx)})"
    if isinstance(cond, IndirectCompareGe):
        return f"({_compile_indirect_value(cond.indirect_ref, ctx)} >= {_compile_value(cond.value, ctx)})"
    if isinstance(cond, ExprCompareEq):
        return f"({compile_expression(cond.left, ctx)} == {compile_expression(cond.right, ctx)})"
    if isinstance(cond, ExprCompareNe):
        return f"({compile_expression(cond.left, ctx)} != {compile_expression(cond.right, ctx)})"
    if isinstance(cond, ExprCompareLt):
        return f"({compile_expression(cond.left, ctx)} < {compile_expression(cond.right, ctx)})"
    if isinstance(cond, ExprCompareLe):
        return f"({compile_expression(cond.left, ctx)} <= {compile_expression(cond.right, ctx)})"
    if isinstance(cond, ExprCompareGt):
        return f"({compile_expression(cond.left, ctx)} > {compile_expression(cond.right, ctx)})"
    if isinstance(cond, ExprCompareGe):
        return f"({compile_expression(cond.left, ctx)} >= {compile_expression(cond.right, ctx)})"

    raise NotImplementedError(f"Unsupported condition type: {type(cond).__name__}")


def compile_expression(expr: Expression, ctx: CodegenContext) -> str:
    """Return a Python expression string with explicit parentheses."""
    if isinstance(expr, TagExpr):
        return ctx.symbol_for_tag(expr.tag)
    if isinstance(expr, LiteralExpr):
        return repr(expr.value)

    if isinstance(expr, AddExpr):
        return f"({compile_expression(expr.left, ctx)} + {compile_expression(expr.right, ctx)})"
    if isinstance(expr, SubExpr):
        return f"({compile_expression(expr.left, ctx)} - {compile_expression(expr.right, ctx)})"
    if isinstance(expr, MulExpr):
        return f"({compile_expression(expr.left, ctx)} * {compile_expression(expr.right, ctx)})"
    if isinstance(expr, DivExpr):
        return f"({compile_expression(expr.left, ctx)} / {compile_expression(expr.right, ctx)})"
    if isinstance(expr, FloorDivExpr):
        return f"({compile_expression(expr.left, ctx)} // {compile_expression(expr.right, ctx)})"
    if isinstance(expr, ModExpr):
        return f"({compile_expression(expr.left, ctx)} % {compile_expression(expr.right, ctx)})"
    if isinstance(expr, PowExpr):
        return f"({compile_expression(expr.left, ctx)} ** {compile_expression(expr.right, ctx)})"

    if isinstance(expr, NegExpr):
        return f"(-({compile_expression(expr.operand, ctx)}))"
    if isinstance(expr, PosExpr):
        return f"(+({compile_expression(expr.operand, ctx)}))"
    if isinstance(expr, AbsExpr):
        return f"abs({compile_expression(expr.operand, ctx)})"

    if isinstance(expr, AndExpr):
        return f"(int({compile_expression(expr.left, ctx)}) & int({compile_expression(expr.right, ctx)}))"
    if isinstance(expr, OrExpr):
        return f"(int({compile_expression(expr.left, ctx)}) | int({compile_expression(expr.right, ctx)}))"
    if isinstance(expr, XorExpr):
        return f"(int({compile_expression(expr.left, ctx)}) ^ int({compile_expression(expr.right, ctx)}))"
    if isinstance(expr, LShiftExpr):
        return f"(int({compile_expression(expr.left, ctx)}) << int({compile_expression(expr.right, ctx)}))"
    if isinstance(expr, RShiftExpr):
        return f"(int({compile_expression(expr.left, ctx)}) >> int({compile_expression(expr.right, ctx)}))"
    if isinstance(expr, InvertExpr):
        return f"(~int({compile_expression(expr.operand, ctx)}))"

    if isinstance(expr, MathFuncExpr):
        allowed = {
            "sqrt",
            "sin",
            "cos",
            "tan",
            "asin",
            "acos",
            "atan",
            "radians",
            "degrees",
            "log10",
            "log",
        }
        if expr.name not in allowed:
            raise TypeError(f"Unsupported expression type: {type(expr).__name__}")
        return f"math.{expr.name}({compile_expression(expr.operand, ctx)})"

    if isinstance(expr, ShiftFuncExpr):
        value = compile_expression(expr.value, ctx)
        count = compile_expression(expr.count, ctx)
        if expr.name == "lsh":
            return f"(int({value}) << int({count}))"
        if expr.name == "rsh":
            return f"(int({value}) >> int({count}))"
        if expr.name == "lro":
            return (
                f"((((int({value}) & 0xFFFF) << (int({count}) % 16)) | "
                f"((int({value}) & 0xFFFF) >> (16 - (int({count}) % 16)))) & 0xFFFF)"
            )
        if expr.name == "rro":
            return (
                f"((((int({value}) & 0xFFFF) >> (int({count}) % 16)) | "
                f"((int({value}) & 0xFFFF) << (16 - (int({count}) % 16)))) & 0xFFFF)"
            )
        raise TypeError(f"Unsupported expression type: {type(expr).__name__}")

    raise TypeError(f"Unsupported expression type: {type(expr).__name__}")


def compile_instruction(
    instr: Any,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    if isinstance(instr, OutInstruction):
        return _compile_out_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, LatchInstruction):
        lines = [f'{" " * indent}if {enabled_expr}:']
        lines.extend(_compile_target_write_lines(instr.target, "True", ctx, indent + 4))
        return lines
    if isinstance(instr, ResetInstruction):
        default_expr = _coil_target_default(instr.target, ctx)
        lines = [f'{" " * indent}if {enabled_expr}:']
        lines.extend(_compile_target_write_lines(instr.target, default_expr, ctx, indent + 4))
        return lines

    loc = _source_location(instr)
    raise NotImplementedError(f"Unsupported instruction type: {type(instr).__name__} at {loc}")


def compile_rung(rung: LogicRung, fn_name: str, ctx: CodegenContext, indent: int = 0) -> list[str]:
    """Compile one rung into Python source lines."""
    previous = ctx._current_function
    ctx.set_current_function(fn_name)
    try:
        rung_id = ctx.next_name("rung")
        enabled_var = f"_{rung_id}_enabled"
        cond_expr = _compile_condition_group(rung._conditions, ctx)
        lines = [f'{" " * indent}{enabled_var} = bool({cond_expr})']
        lines.extend(
            _compile_rung_items(
                rung=rung,
                enabled_expr=enabled_var,
                ctx=ctx,
                indent=indent,
                scope_key=rung_id,
            )
        )
        return lines
    finally:
        ctx.set_current_function(previous)


def generate_circuitpy(
    program: Program,
    hw: P1AM,
    *,
    target_scan_ms: float,
    watchdog_ms: int | None = None,
) -> str:
    if not isinstance(program, Program):
        raise TypeError(f"program must be Program, got {type(program).__name__}")
    if not isinstance(hw, P1AM):
        raise TypeError(f"hw must be P1AM, got {type(hw).__name__}")
    if not isinstance(target_scan_ms, (int, float)):
        raise TypeError(f"target_scan_ms must be a finite number > 0, got {type(target_scan_ms).__name__}")
    if not _math.isfinite(float(target_scan_ms)) or float(target_scan_ms) <= 0:
        raise ValueError("target_scan_ms must be finite and > 0")
    if watchdog_ms is not None:
        if not isinstance(watchdog_ms, int):
            raise TypeError(f"watchdog_ms must be int or None, got {type(watchdog_ms).__name__}")
        if watchdog_ms < 0:
            raise ValueError("watchdog_ms must be >= 0")

    if not hw._slots:
        raise ValueError("P1AM hardware config must include at least one configured slot")

    slot_numbers = sorted(hw._slots)
    expected = list(range(1, slot_numbers[-1] + 1))
    if slot_numbers != expected:
        raise ValueError(
            "Configured slots must be contiguous from 1..N for v1 roll-call generation"
        )

    report = validate_circuitpy_program(program, hw=hw, mode="strict")
    if report.errors:
        lines = [report.summary()]
        for err in report.errors:
            lines.append(f"{err.code} @ {err.location}: {err.message}")
        raise ValueError("\n".join(lines))

    ctx = CodegenContext(
        program=program,
        hw=hw,
        target_scan_ms=float(target_scan_ms),
        watchdog_ms=watchdog_ms,
    )
    ctx.collect_hw_bindings()
    ctx.collect_program_references()
    ctx.collect_retentive_tags()
    ctx.assign_symbols()

    source = _render_code(ctx)
    try:
        compile(source, "code.py", "exec")
    except SyntaxError as exc:
        raise RuntimeError(f"Generated source is invalid: {exc}") from exc
    return source


def _render_code(ctx: CodegenContext) -> str:
    main_fn_lines = _render_main_function(ctx)
    sub_fn_lines = _render_subroutine_functions(ctx)
    io_lines = _render_io_helpers(ctx)
    helper_lines = _render_helper_section(ctx)
    function_source_lines = _render_embedded_functions(ctx)

    lines: list[str] = []

    # 1) imports
    lines.extend(
        [
            "import hashlib",
            "import json",
            "import math",
            "import os",
            "import time",
            "",
            "import board",
            "import busio",
            "import P1AM",
            "import sdcardio",
            "import storage",
            "",
            "try:",
            "    import microcontroller",
            "except ImportError:",
            "    microcontroller = None",
            "",
        ]
    )

    # 2) config constants
    lines.extend(
        [
            f"TARGET_SCAN_MS = {ctx.target_scan_ms!r}",
            f"WATCHDOG_MS = {ctx.watchdog_ms!r}",
            "PRINT_SCAN_OVERRUNS = False",
            "",
            f"_SLOT_MODULES = {[slot.part_number for slot in ctx.slot_bindings]!r}",
            f"_RET_DEFAULTS = {_ret_defaults_literal(ctx)!r}",
            f"_RET_TYPES = {_ret_types_literal(ctx)!r}",
            f'_RET_SCHEMA = "{ctx.compute_retentive_schema_hash()}"',
            "",
        ]
    )

    # 3) hardware bootstrap + roll-call
    lines.extend(
        [
            "base = P1AM.Base()",
            "base.rollCall(_SLOT_MODULES)",
            "",
        ]
    )

    # 4) watchdog API binding + startup config
    lines.extend(
        [
            '_wd_config = getattr(base, "config_watchdog", None)',
            '_wd_start = getattr(base, "start_watchdog", None)',
            '_wd_pet = getattr(base, "pet_watchdog", None)',
            "if WATCHDOG_MS is not None:",
            "    if _wd_config is None or _wd_start is None or _wd_pet is None:",
            '        raise RuntimeError("P1AM snake_case watchdog API not found on Base() instance")',
            "    _wd_config(WATCHDOG_MS)",
            "    _wd_start()",
            "",
        ]
    )

    # 5) tag and block declarations
    lines.append("# Scalars (non-block tags).")
    if ctx.scalar_tags:
        for tag_name in sorted(ctx.scalar_tags):
            tag = ctx.scalar_tags[tag_name]
            symbol = ctx.symbol_table[tag_name]
            lines.append(f"{symbol} = {repr(tag.default)}")
    else:
        lines.append("pass")
    lines.append("")

    lines.append("# Blocks (list-backed; PLC addresses remain 1-based, list indexes are 0-based).")
    block_bindings = sorted(ctx.block_bindings.values(), key=lambda b: (ctx.block_symbols[b.block_id], b.block_id))
    for binding in block_bindings:
        symbol = ctx.block_symbols[binding.block_id]
        size = binding.end - binding.start + 1
        default = _TYPE_DEFAULTS[binding.tag_type]
        lines.append(f"{symbol} = [{repr(default)}] * {size}")
    lines.append("")

    # 6) runtime memory declarations (stubbed persistence state)
    lines.extend(
        [
            "_mem = {}",
            "_prev = {}",
            "_last_scan_ts = time.monotonic()",
            "_scan_overrun_count = 0",
            "",
            "_sd_available = False",
            '_MEMORY_PATH = "/sd/memory.json"',
            '_MEMORY_TMP_PATH = "/sd/_memory.tmp"',
            "_sd_spi = None",
            "_sd = None",
            "_sd_vfs = None",
            "_sd_write_status = False",
            "_sd_error = False",
            "_sd_error_code = 0",
            "_sd_eject_cmd = False",
            "_sd_delete_all_cmd = False",
            "_sd_copy_system_cmd = False",
            "",
        ]
    )

    # 7) SD mount + load memory startup call (stubs in foundation step)
    lines.extend(
        [
            "def _mount_sd():",
            "    global _sd_available, _sd_error, _sd_error_code",
            "    _sd_available = False",
            "    _sd_error = False",
            "    _sd_error_code = 0",
            "",
            "def load_memory():",
            "    global _sd_write_status",
            "    _sd_write_status = False",
            "    return",
            "",
            "_mount_sd()",
            "load_memory()",
            "",
        ]
    )

    # 8) helper definitions
    lines.extend(helper_lines)

    # 9) embedded user function sources
    lines.extend(function_source_lines)

    # 10) compiled subroutine functions
    lines.extend(sub_fn_lines)

    # 11) compiled main-rung function
    lines.extend(main_fn_lines)

    # 12) scan-time I/O read/write helpers
    lines.extend(io_lines)

    # 13) main scan loop
    lines.extend(_render_scan_loop(ctx))

    return "\n".join(lines).rstrip() + "\n"


def _render_helper_section(ctx: CodegenContext) -> list[str]:
    lines = [
        "def _service_sd_commands():",
        "    global _sd_write_status, _sd_eject_cmd, _sd_delete_all_cmd, _sd_copy_system_cmd",
        "    if not (_sd_eject_cmd or _sd_delete_all_cmd or _sd_copy_system_cmd):",
        "        return",
        "    _sd_write_status = True",
        "    _sd_eject_cmd = False",
        "    _sd_delete_all_cmd = False",
        "    _sd_copy_system_cmd = False",
        "",
        "def save_memory():",
        "    global _sd_write_status",
        "    if not _sd_available:",
        "        return",
        "    _sd_write_status = True",
        "    _sd_write_status = False",
        "",
    ]

    for binding in sorted(
        (ctx.block_bindings[bid] for bid in ctx.used_indirect_blocks),
        key=lambda b: ctx.index_helper_name(b.block_id),
    ):
        helper_name = ctx.index_helper_name(binding.block_id)
        lines.append(f"def {helper_name}(addr):")
        lines.append(f"    if addr < {binding.start} or addr > {binding.end}:")
        lines.append(
            f'        raise IndexError(f"Address {{addr}} out of range for {binding.logical_name} ({binding.start}-{binding.end})")'
        )
        if binding.valid_addresses is not None:
            lines.append(f"    if addr not in {binding.valid_addresses!r}:")
            lines.append(
                f'        raise IndexError(f"Address {{addr}} out of range for {binding.logical_name} ({binding.start}-{binding.end})")'
            )
        lines.append(f"    return int(addr) - {binding.start}")
        lines.append("")

    helper_defs = {
        "_clamp_int": [
            "def _clamp_int(value):",
            "    if value < -32768:",
            "        return -32768",
            "    if value > 32767:",
            "        return 32767",
            "    return int(value)",
            "",
        ],
        "_wrap_int": [
            "def _wrap_int(value, bits, signed):",
            "    mask = (1 << bits) - 1",
            "    v = int(value) & mask",
            "    if signed and v >= (1 << (bits - 1)):",
            "        v -= (1 << bits)",
            "    return v",
            "",
        ],
        "_rise": [
            "def _rise(curr, prev):",
            "    return bool(curr) and not bool(prev)",
            "",
        ],
        "_fall": [
            "def _fall(curr, prev):",
            "    return not bool(curr) and bool(prev)",
            "",
        ],
        "_int_to_float_bits": [
            "def _int_to_float_bits(n):",
            "    raise NotImplementedError('Step 2 helper not emitted in foundation step')",
            "",
        ],
        "_float_to_int_bits": [
            "def _float_to_int_bits(f):",
            "    raise NotImplementedError('Step 2 helper not emitted in foundation step')",
            "",
        ],
        "_parse_pack_text_value": [
            "def _parse_pack_text_value(text, dest_type):",
            "    raise NotImplementedError('Step 2 helper not emitted in foundation step')",
            "",
        ],
        "_store_copy_value_to_type": [
            "def _store_copy_value_to_type(value, dest_type):",
            "    raise NotImplementedError('Step 2 helper not emitted in foundation step')",
            "",
        ],
    }
    for helper in _HELPER_ORDER:
        if helper in ctx.used_helpers:
            lines.extend(helper_defs[helper])
    return lines


def _render_embedded_functions(ctx: CodegenContext) -> list[str]:
    lines: list[str] = []
    if not ctx.function_sources:
        lines.append("# Embedded function call targets.")
        lines.append("# None emitted in foundation step.")
        lines.append("")
        return lines
    for symbol in sorted(ctx.function_sources):
        src = ctx.function_sources[symbol].rstrip()
        lines.append(src)
        lines.append("")
    return lines


def _render_subroutine_functions(ctx: CodegenContext) -> list[str]:
    lines: list[str] = []
    for sub_name in ctx.subroutine_names:
        fn_name = _subroutine_symbol(sub_name)
        ctx.function_globals[fn_name] = set()
        ctx.set_current_function(fn_name)
        body: list[str] = []
        for rung in ctx.program.subroutines[sub_name]:
            body.extend(compile_rung(rung, fn_name, ctx, indent=4))
        ctx.set_current_function(None)
        globals_line = _global_line(ctx.globals_for_function(fn_name), indent=4)
        lines.append(f"def {fn_name}():")
        if globals_line is not None:
            lines.append(globals_line)
        if body:
            lines.extend(body)
        else:
            lines.append("    pass")
        lines.append("")
    return lines


def _render_main_function(ctx: CodegenContext) -> list[str]:
    fn_name = "_run_main_rungs"
    ctx.function_globals[fn_name] = set()
    ctx.set_current_function(fn_name)
    body: list[str] = []
    for rung in ctx.program.rungs:
        body.extend(compile_rung(rung, fn_name, ctx, indent=4))
    ctx.set_current_function(None)

    lines = [f"def {fn_name}():"]
    globals_line = _global_line(ctx.globals_for_function(fn_name), indent=4)
    if globals_line is not None:
        lines.append(globals_line)
    if body:
        lines.extend(body)
    else:
        lines.append("    pass")
    lines.append("")
    return lines


def _render_io_helpers(ctx: CodegenContext) -> list[str]:
    lines: list[str] = []

    read_fn = "_read_inputs"
    ctx.function_globals[read_fn] = set()
    ctx.set_current_function(read_fn)
    read_body: list[str] = []
    for slot in ctx.slot_bindings:
        if slot.input_block_id is None:
            continue
        binding = ctx.block_bindings[slot.input_block_id]
        symbol = ctx.symbol_for_block(binding.block)
        if slot.input_kind == "discrete":
            mask = ctx.next_name(f"_mask_s{slot.slot_number}")
            read_body.append(f"    {mask} = int(base.readDiscrete({slot.slot_number}))")
            for ch in range(1, slot.input_count + 1):
                index = ch - binding.start
                read_body.append(f"    {symbol}[{index}] = bool(({mask} >> {ch - 1}) & 1)")
        elif slot.input_kind == "analog":
            for ch in range(1, slot.input_count + 1):
                index = ch - binding.start
                read_body.append(
                    f"    {symbol}[{index}] = int(base.readAnalog({slot.slot_number}, {ch}))"
                )
        elif slot.input_kind == "temperature":
            for ch in range(1, slot.input_count + 1):
                index = ch - binding.start
                read_body.append(
                    f"    {symbol}[{index}] = float(base.readTemperature({slot.slot_number}, {ch}))"
                )
    ctx.set_current_function(None)
    lines.append(f"def {read_fn}():")
    read_globals = _global_line(ctx.globals_for_function(read_fn), indent=4)
    if read_globals is not None:
        lines.append(read_globals)
    if read_body:
        lines.extend(read_body)
    else:
        lines.append("    pass")
    lines.append("")

    write_fn = "_write_outputs"
    ctx.function_globals[write_fn] = set()
    ctx.set_current_function(write_fn)
    write_body: list[str] = []
    for slot in ctx.slot_bindings:
        if slot.output_block_id is None:
            continue
        binding = ctx.block_bindings[slot.output_block_id]
        symbol = ctx.symbol_for_block(binding.block)
        if slot.output_kind == "discrete":
            mask = ctx.next_name(f"_out_mask_s{slot.slot_number}")
            write_body.append(f"    {mask} = 0")
            for ch in range(1, slot.output_count + 1):
                index = ch - binding.start
                write_body.append(f"    if bool({symbol}[{index}]):")
                write_body.append(f"        {mask} |= (1 << {ch - 1})")
            write_body.append(f"    base.writeDiscrete({mask}, {slot.slot_number})")
        elif slot.output_kind == "analog":
            for ch in range(1, slot.output_count + 1):
                index = ch - binding.start
                write_body.append(
                    f"    base.writeAnalog(int({symbol}[{index}]), {slot.slot_number}, {ch})"
                )
    ctx.set_current_function(None)
    lines.append(f"def {write_fn}():")
    write_globals = _global_line(ctx.globals_for_function(write_fn), indent=4)
    if write_globals is not None:
        lines.append(write_globals)
    if write_body:
        lines.extend(write_body)
    else:
        lines.append("    pass")
    lines.append("")
    return lines


def _render_scan_loop(ctx: CodegenContext) -> list[str]:
    lines = [
        "while True:",
        "    scan_start = time.monotonic()",
        "    _sd_write_status = False",
        "    dt = scan_start - _last_scan_ts",
        "    if dt < 0:",
        "        dt = 0.0",
        "    _last_scan_ts = scan_start",
        '    _mem["_dt"] = dt',
        "",
        "    _service_sd_commands()",
        "    _read_inputs()",
        "    _run_main_rungs()",
        "    _write_outputs()",
        "",
    ]
    for tag_name in sorted(ctx.referenced_tags):
        tag = ctx.referenced_tags[tag_name]
        lines.append(f'    _prev["{tag_name}"] = {ctx.symbol_for_tag(tag)}')
    lines.extend(
        [
            "",
            "    if WATCHDOG_MS is not None:",
            "        _wd_pet()",
            "",
            "    elapsed_ms = (time.monotonic() - scan_start) * 1000.0",
            "    sleep_ms = TARGET_SCAN_MS - elapsed_ms",
            "    if sleep_ms > 0:",
            "        time.sleep(sleep_ms / 1000.0)",
            "    else:",
            "        _scan_overrun_count += 1",
            "        if PRINT_SCAN_OVERRUNS:",
            '            print(f"Scan overrun #{_scan_overrun_count}: {-sleep_ms:.3f} ms late")',
            "",
        ]
    )
    return lines


def _compile_rung_items(
    rung: LogicRung,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
    scope_key: str,
) -> list[str]:
    lines: list[str] = []
    branch_vars: dict[int, str] = {}
    branch_idx = 0
    for item in rung._execution_items:
        if not isinstance(item, LogicRung):
            continue
        local_conditions = item._conditions[item._branch_condition_start :]
        local_expr = _compile_condition_group(local_conditions, ctx)
        branch_var = f"_{scope_key}_branch_{branch_idx}"
        branch_idx += 1
        branch_vars[id(item)] = branch_var
        lines.append(f'{" " * indent}{branch_var} = bool({enabled_expr} and ({local_expr}))')

    branch_scope_idx = 0
    for item in rung._execution_items:
        if isinstance(item, LogicRung):
            branch_var = branch_vars[id(item)]
            child_scope = f"{scope_key}_b{branch_scope_idx}"
            branch_scope_idx += 1
            lines.extend(
                _compile_rung_items(
                    rung=item,
                    enabled_expr=branch_var,
                    ctx=ctx,
                    indent=indent,
                    scope_key=child_scope,
                )
            )
            continue
        lines.extend(compile_instruction(item, enabled_expr, ctx, indent))
    return lines


def _compile_condition_group(conditions: list[Condition], ctx: CodegenContext) -> str:
    if not conditions:
        return "True"
    parts = [f"({compile_condition(cond, ctx)})" for cond in conditions]
    return " and ".join(parts)


def _compile_out_instruction(
    instr: OutInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    lines: list[str] = []
    sp = " " * indent
    if instr.oneshot:
        key = f"_oneshot:{_source_location(instr)}"
        if ctx._current_function is not None:
            ctx.mark_function_global(ctx._current_function, "_mem")
        lines.append(f"{sp}if not ({enabled_expr}):")
        lines.append(f'{" " * (indent + 4)}_mem[{key!r}] = False')
        lines.extend(_compile_target_write_lines(instr.target, "False", ctx, indent + 4))
        lines.append(f"{sp}elif not bool(_mem.get({key!r}, False)):")
        lines.extend(_compile_target_write_lines(instr.target, "True", ctx, indent + 4))
        lines.append(f'{" " * (indent + 4)}_mem[{key!r}] = True')
        return lines

    lines.append(f"{sp}if {enabled_expr}:")
    lines.extend(_compile_target_write_lines(instr.target, "True", ctx, indent + 4))
    lines.append(f"{sp}else:")
    lines.extend(_compile_target_write_lines(instr.target, "False", ctx, indent + 4))
    return lines


def _compile_target_write_lines(
    target: Tag | BlockRange | IndirectBlockRange,
    value_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    sp = " " * indent
    if isinstance(target, Tag):
        return [f"{sp}{ctx.symbol_for_tag(target)} = {value_expr}"]

    if isinstance(target, BlockRange):
        binding = ctx.block_bindings[id(target.block)]
        symbol = ctx.symbol_for_block(target.block)
        lines: list[str] = []
        for addr in target.addresses:
            index = addr - binding.start
            lines.append(f"{sp}{symbol}[{index}] = {value_expr}")
        return lines or [f"{sp}pass"]

    binding = ctx.block_bindings[id(target.block)]
    symbol = ctx.symbol_for_block(target.block)
    helper = ctx.use_indirect_block(binding.block_id)
    name = ctx.next_name("irange")
    start_var = f"_{name}_start"
    end_var = f"_{name}_end"
    addr_list = f"_{name}_indices"
    addr_var = f"_{name}_addr"
    idx_var = f"_{name}_idx"
    start_expr = _compile_address_expr(target.start_expr, ctx)
    end_expr = _compile_address_expr(target.end_expr, ctx)
    lines = [
        f"{sp}{start_var} = int({start_expr})",
        f"{sp}{end_var} = int({end_expr})",
        f"{sp}if {start_var} > {end_var}:",
        f'{sp}    raise ValueError("Indirect range start must be <= end")',
        f"{sp}{addr_list} = []",
        f"{sp}for {addr_var} in range({start_var}, {end_var} + 1):",
        f"{sp}    {idx_var} = {helper}(int({addr_var}))",
        f"{sp}    {addr_list}.append({idx_var})",
    ]
    if target.reverse_order:
        lines.append(f"{sp}{addr_list}.reverse()")
    lines.extend(
        [
            f"{sp}for {idx_var} in {addr_list}:",
            f"{sp}    {symbol}[{idx_var}] = {value_expr}",
        ]
    )
    return lines


def _compile_address_expr(addr: int | Tag | Any, ctx: CodegenContext) -> str:
    if isinstance(addr, int):
        return repr(addr)
    if isinstance(addr, Tag):
        return ctx.symbol_for_tag(addr)
    if isinstance(addr, Expression):
        return compile_expression(addr, ctx)
    raise TypeError(f"Unsupported indirect address expression type: {type(addr).__name__}")


def _compile_indirect_value(indirect_ref: IndirectRef, ctx: CodegenContext) -> str:
    block_id = id(indirect_ref.block)
    binding = ctx.block_bindings.get(block_id)
    if binding is None:
        raise RuntimeError(f"Missing block binding for indirect ref {indirect_ref.block.name!r}")
    block_symbol = ctx.symbol_for_block(indirect_ref.block)
    helper = ctx.use_indirect_block(binding.block_id)
    ptr = _compile_value(indirect_ref.pointer, ctx)
    return f"{block_symbol}[{helper}(int({ptr}))]"


def _compile_value(value: Any, ctx: CodegenContext) -> str:
    if isinstance(value, Tag):
        return ctx.symbol_for_tag(value)
    if isinstance(value, IndirectRef):
        return _compile_indirect_value(value, ctx)
    if isinstance(value, IndirectExprRef):
        block_id = id(value.block)
        binding = ctx.block_bindings.get(block_id)
        if binding is None:
            raise RuntimeError(f"Missing block binding for indirect expression ref {value.block.name!r}")
        block_symbol = ctx.symbol_for_block(value.block)
        helper = ctx.use_indirect_block(binding.block_id)
        expr = compile_expression(value.expr, ctx)
        return f"{block_symbol}[{helper}(int({expr}))]"
    if isinstance(value, Expression):
        return compile_expression(value, ctx)
    return repr(value)


def _coil_target_default(target: Tag | BlockRange | IndirectBlockRange, ctx: CodegenContext) -> str:
    if isinstance(target, Tag):
        return repr(target.default)
    binding = ctx.block_bindings[id(target.block)]
    return repr(_TYPE_DEFAULTS[binding.tag_type])


def _global_line(symbols: list[str], indent: int) -> str | None:
    if not symbols:
        return None
    return f'{" " * indent}global {", ".join(symbols)}'


def _ret_defaults_literal(ctx: CodegenContext) -> dict[str, Any]:
    return {name: ctx.retentive_tags[name].default for name in sorted(ctx.retentive_tags)}


def _ret_types_literal(ctx: CodegenContext) -> dict[str, str]:
    return {name: ctx.retentive_tags[name].type.name for name in sorted(ctx.retentive_tags)}


def _source_location(obj: Any) -> str:
    src_file = getattr(obj, "source_file", None)
    src_line = getattr(obj, "source_line", None)
    if src_file is None or src_line is None:
        return "unknown"
    return f"{src_file}:{src_line}"


def _mangle_symbol(logical_name: str, prefix: str, used: set[str]) -> str:
    sanitized = _IDENT_RE.sub("_", logical_name)
    if not sanitized:
        sanitized = "_"
    if sanitized[0].isdigit():
        sanitized = f"_{sanitized}"
    candidate = f"{prefix}{sanitized}"
    if candidate not in used:
        used.add(candidate)
        return candidate
    n = 2
    while True:
        next_candidate = f"{candidate}_{n}"
        if next_candidate not in used:
            used.add(next_candidate)
            return next_candidate
        n += 1


def _subroutine_symbol(name: str) -> str:
    base = _IDENT_RE.sub("_", name)
    if not base:
        base = "_"
    if base[0].isdigit():
        base = f"_{base}"
    return f"_sub_{base}"


def _io_kind(tag_type: TagType) -> str:
    if tag_type == TagType.BOOL:
        return "discrete"
    if tag_type == TagType.INT:
        return "analog"
    if tag_type == TagType.REAL:
        return "temperature"
    raise ValueError(f"Unsupported CircuitPython I/O tag type: {tag_type.name}")


__all__ = [
    "BlockBinding",
    "CodegenContext",
    "SlotBinding",
    "compile_condition",
    "compile_expression",
    "compile_instruction",
    "compile_rung",
    "generate_circuitpy",
]
