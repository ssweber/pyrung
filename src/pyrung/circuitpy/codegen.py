"""CircuitPython code generation (feature-complete v1)."""

from __future__ import annotations

import hashlib
import inspect
import math as _math
import re
import textwrap
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
from pyrung.core.instruction import (
    BlockCopyInstruction,
    CalcInstruction,
    CallInstruction,
    CopyInstruction,
    CountDownInstruction,
    CountUpInstruction,
    EnabledFunctionCallInstruction,
    FillInstruction,
    ForLoopInstruction,
    FunctionCallInstruction,
    LatchInstruction,
    OffDelayInstruction,
    OnDelayInstruction,
    OutInstruction,
    PackBitsInstruction,
    PackTextInstruction,
    PackWordsInstruction,
    ResetInstruction,
    ReturnInstruction,
    SearchInstruction,
    ShiftInstruction,
    UnpackToBitsInstruction,
    UnpackToWordsInstruction,
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
from pyrung.core.time_mode import TimeUnit
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
    "_ascii_char_from_code",
    "_as_single_ascii_char",
    "_text_from_source_value",
    "_store_numeric_text_digit",
    "_format_int_text",
    "_render_text_from_numeric",
    "_termination_char",
    "_parse_pack_text_value",
    "_store_copy_value_to_type",
)
_INT_MIN = -32768
_INT_MAX = 32767
_DINT_MIN = -2147483648
_DINT_MAX = 2147483647

_SD_READY_TAG = "storage.sd.ready"
_SD_WRITE_STATUS_TAG = "storage.sd.write_status"
_SD_ERROR_TAG = "storage.sd.error"
_SD_ERROR_CODE_TAG = "storage.sd.error_code"
_SD_SAVE_CMD_TAG = "storage.sd.save_cmd"
_SD_EJECT_CMD_TAG = "storage.sd.eject_cmd"
_SD_DELETE_ALL_CMD_TAG = "storage.sd.delete_all_cmd"
_FAULT_OUT_OF_RANGE_TAG = "fault.out_of_range"

_SD_MOUNT_ERROR = 1
_SD_LOAD_ERROR = 2
_SD_SAVE_ERROR = 3


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
        lines = [f"{' ' * indent}if {enabled_expr}:"]
        lines.extend(_compile_target_write_lines(instr.target, "True", ctx, indent + 4))
        return lines
    if isinstance(instr, ResetInstruction):
        default_expr = _coil_target_default(instr.target, ctx)
        lines = [f"{' ' * indent}if {enabled_expr}:"]
        lines.extend(_compile_target_write_lines(instr.target, default_expr, ctx, indent + 4))
        return lines
    if isinstance(instr, OnDelayInstruction):
        return _compile_on_delay_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, OffDelayInstruction):
        return _compile_off_delay_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, CountUpInstruction):
        return _compile_count_up_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, CountDownInstruction):
        return _compile_count_down_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, CopyInstruction):
        return _compile_copy_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, CalcInstruction):
        return _compile_calc_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, BlockCopyInstruction):
        return _compile_blockcopy_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, FillInstruction):
        return _compile_fill_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, SearchInstruction):
        return _compile_search_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, ShiftInstruction):
        return _compile_shift_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, PackBitsInstruction):
        return _compile_pack_bits_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, PackWordsInstruction):
        return _compile_pack_words_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, PackTextInstruction):
        return _compile_pack_text_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, UnpackToBitsInstruction):
        return _compile_unpack_bits_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, UnpackToWordsInstruction):
        return _compile_unpack_words_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, FunctionCallInstruction):
        return _compile_function_call_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, EnabledFunctionCallInstruction):
        return _compile_enabled_function_call_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, CallInstruction):
        return _compile_call_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, ReturnInstruction):
        return _compile_return_instruction(enabled_expr, indent)
    if isinstance(instr, ForLoopInstruction):
        return _compile_for_loop_instruction(instr, enabled_expr, ctx, indent)

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
        lines = [f"{' ' * indent}{enabled_var} = {cond_expr}"]
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
        raise TypeError(
            f"target_scan_ms must be a finite number > 0, got {type(target_scan_ms).__name__}"
        )
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
        lines = [f"{len(report.errors)} error(s)."]
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
            "import json",
            "import math",
            "import os",
            "import re",
            "import struct",
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
    if ctx.watchdog_ms is not None:
        lines.extend(
            [
                '_wd_config = getattr(base, "config_watchdog", None)',
                '_wd_start = getattr(base, "start_watchdog", None)',
                '_wd_pet = getattr(base, "pet_watchdog", None)',
                "if _wd_config is None or _wd_start is None or _wd_pet is None:",
                '    raise RuntimeError("P1AM snake_case watchdog API not found on Base() instance")',
                "_wd_config(WATCHDOG_MS)",
                "_wd_start()",
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
    block_bindings = sorted(
        ctx.block_bindings.values(), key=lambda b: (ctx.block_symbols[b.block_id], b.block_id)
    )
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
            "_sd_save_cmd = False",
            "_sd_eject_cmd = False",
            "_sd_delete_all_cmd = False",
            "",
        ]
    )

    # 7) SD mount + load memory startup call
    ret_globals = [ctx.symbol_for_tag(tag) for _, tag in sorted(ctx.retentive_tags.items())]
    load_globals = ", ".join(ret_globals + ["_sd_write_status", "_sd_error", "_sd_error_code"])
    save_globals = load_globals
    lines.extend(
        [
            "def _mount_sd():",
            "    global _sd_available, _sd_spi, _sd, _sd_vfs, _sd_error, _sd_error_code",
            "    try:",
            "        _sd_spi = busio.SPI(board.SD_SCK, board.SD_MOSI, board.SD_MISO)",
            "        _sd = sdcardio.SDCard(_sd_spi, board.SD_CS)",
            "        _sd_vfs = storage.VfsFat(_sd)",
            '        storage.mount(_sd_vfs, "/sd")',
            "        _sd_available = True",
            "        _sd_error = False",
            "        _sd_error_code = 0",
            "    except Exception as exc:",
            "        _sd_available = False",
            "        _sd_error = True",
            f"        _sd_error_code = {_SD_MOUNT_ERROR}",
            '        print(f"Retentive storage unavailable: {exc}")',
            "",
            "def load_memory():",
        ]
    )
    if load_globals:
        lines.append(f"    global {load_globals}")
    lines.extend(
        [
            "    if not _sd_available:",
            '        print("Retentive load skipped: SD unavailable")',
            "        return",
            "    _sd_write_status = True",
            "    if microcontroller is not None and len(microcontroller.nvm) > 0 and microcontroller.nvm[0] == 1:",
            "        _sd_error = True",
            f"        _sd_error_code = {_SD_LOAD_ERROR}",
            "        _sd_write_status = False",
            '        print("Retentive load skipped: interrupted previous save detected")',
            "        return",
            "    try:",
            '        with open(_MEMORY_PATH, "r", encoding="utf-8") as f:',
            "            payload = json.load(f)",
            "    except Exception as exc:",
            "        _sd_error = True",
            f"        _sd_error_code = {_SD_LOAD_ERROR}",
            "        _sd_write_status = False",
            '        print(f"Retentive load skipped: {exc}")',
            "        return",
            '    if payload.get("schema") != _RET_SCHEMA:',
            "        _sd_error = True",
            f"        _sd_error_code = {_SD_LOAD_ERROR}",
            "        _sd_write_status = False",
            '        print("Retentive load skipped: schema mismatch")',
            "        return",
            '    values = payload.get("values", {})',
        ]
    )
    for name, tag in sorted(ctx.retentive_tags.items()):
        symbol = ctx.symbol_for_tag(tag)
        load_expr = _load_cast_expr('_entry.get("value", ' + symbol + ")", tag.type.name)
        lines.extend(
            [
                f'    _entry = values.get("{name}")',
                f'    if isinstance(_entry, dict) and _entry.get("type") == "{tag.type.name}":',
                "        try:",
                f"            {symbol} = {load_expr}",
                "        except Exception:",
                "            pass",
            ]
        )
    lines.extend(
        [
            "    _sd_error = False",
            "    _sd_error_code = 0",
            "    _sd_write_status = False",
            "",
            "def save_memory():",
        ]
    )
    if save_globals:
        lines.append(f"    global {save_globals}")
    lines.extend(
        [
            "    if not _sd_available:",
            "        return",
            "    _sd_write_status = True",
            "    values = {}",
        ]
    )
    for name, tag in sorted(ctx.retentive_tags.items()):
        symbol = ctx.symbol_for_tag(tag)
        lines.extend(
            [
                f'    if {symbol} != _RET_DEFAULTS["{name}"]:',
                f'        values["{name}"] = {{"type": "{tag.type.name}", "value": {symbol}}}',
            ]
        )
    lines.extend(
        [
            '    payload = {"schema": _RET_SCHEMA, "values": values}',
            "    dirty_armed = False",
            "    if microcontroller is not None and len(microcontroller.nvm) > 0:",
            "        microcontroller.nvm[0] = 1",
            "        dirty_armed = True",
            "    try:",
            '        with open(_MEMORY_TMP_PATH, "w", encoding="utf-8") as f:',
            "            json.dump(payload, f)",
            "        os.replace(_MEMORY_TMP_PATH, _MEMORY_PATH)",
            "    except Exception as exc:",
            "        _sd_error = True",
            f"        _sd_error_code = {_SD_SAVE_ERROR}",
            "        _sd_write_status = False",
            '        print(f"Retentive save failed: {exc}")',
            "        return",
            "    if dirty_armed:",
            "        microcontroller.nvm[0] = 0",
            "    _sd_error = False",
            "    _sd_error_code = 0",
            "    _sd_write_status = False",
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
        "    global _sd_write_status, _sd_error, _sd_error_code",
        "    global _sd_save_cmd, _sd_eject_cmd, _sd_delete_all_cmd",
        "    global _sd_available, _sd_spi, _sd, _sd_vfs",
        "    if not (_sd_save_cmd or _sd_eject_cmd or _sd_delete_all_cmd):",
        "        return",
        "    _do_delete = bool(_sd_delete_all_cmd)",
        "    _do_save = bool(_sd_save_cmd)",
        "    _do_eject = bool(_sd_eject_cmd)",
        "    _sd_save_cmd = False",
        "    _sd_eject_cmd = False",
        "    _sd_delete_all_cmd = False",
        "    _sd_write_status = True",
        "    _command_failed = False",
        "    if _do_delete:",
        "        try:",
        "            for _path in (_MEMORY_PATH, _MEMORY_TMP_PATH):",
        "                try:",
        "                    os.remove(_path)",
        "                except OSError:",
        "                    pass",
        "        except Exception as exc:",
        "            _command_failed = True",
        "            _sd_error = True",
        f"            _sd_error_code = {_SD_SAVE_ERROR}",
        '            print(f"SD delete_all command failed: {exc}")',
        "    if _do_save:",
        "        try:",
        "            save_memory()",
        f"            if _sd_error and _sd_error_code == {_SD_SAVE_ERROR}:",
        "                _command_failed = True",
        "        except Exception as exc:",
        "            _command_failed = True",
        "            _sd_error = True",
        f"            _sd_error_code = {_SD_SAVE_ERROR}",
        '            print(f"SD save command failed: {exc}")',
        "    if _do_eject:",
        "        try:",
        "            if _sd_available:",
        '                storage.umount("/sd")',
        "            _sd_available = False",
        "            _sd_spi = None",
        "            _sd = None",
        "            _sd_vfs = None",
        "        except Exception as exc:",
        "            _command_failed = True",
        "            _sd_error = True",
        f"            _sd_error_code = {_SD_SAVE_ERROR}",
        '            print(f"SD eject command failed: {exc}")',
        "    if not _command_failed:",
        "        _sd_error = False",
        "        _sd_error_code = 0",
        "    # SC69 pulses for this serviced-command scan; reset occurs at next scan start.",
        "    _sd_write_status = True",
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
            '    return struct.unpack("<f", struct.pack("<I", int(n) & 0xFFFFFFFF))[0]',
            "",
        ],
        "_float_to_int_bits": [
            "def _float_to_int_bits(f):",
            '    return struct.unpack("<I", struct.pack("<f", float(f)))[0]',
            "",
        ],
        "_ascii_char_from_code": [
            "def _ascii_char_from_code(code):",
            "    if code < 0 or code > 127:",
            '        raise ValueError("ASCII code out of range")',
            "    return chr(code)",
            "",
        ],
        "_as_single_ascii_char": [
            "def _as_single_ascii_char(value):",
            "    if not isinstance(value, str):",
            '        raise ValueError("CHAR value must be a string")',
            '    if value == "":',
            "        return value",
            "    if len(value) != 1 or ord(value) > 127:",
            '        raise ValueError("CHAR value must be blank or one ASCII character")',
            "    return value",
            "",
        ],
        "_text_from_source_value": [
            "def _text_from_source_value(value):",
            "    if isinstance(value, str):",
            "        return value",
            '    raise ValueError("text conversion source must resolve to str")',
            "",
        ],
        "_store_numeric_text_digit": [
            "def _store_numeric_text_digit(char, mode):",
            "    _char = _as_single_ascii_char(char)",
            '    if _char == "":',
            '        raise ValueError("empty CHAR cannot be converted to numeric")',
            '    if mode == "value":',
            '        if _char < "0" or _char > "9":',
            '            raise ValueError("Copy Character Value accepts only digits 0-9")',
            '        return ord(_char) - ord("0")',
            '    if mode == "ascii":',
            "        return ord(_char)",
            '    raise ValueError(f"Unsupported text->numeric mode: {mode}")',
            "",
        ],
        "_format_int_text": [
            "def _format_int_text(value, width, suppress_zero, signed=True):",
            "    if suppress_zero:",
            "        return str(value)",
            "    if not signed:",
            '        return f"{value:0{width}X}"',
            "    if value < 0:",
            '        return f"-{abs(value):0{width}d}"',
            '    return f"{value:0{width}d}"',
            "",
        ],
        "_render_text_from_numeric": [
            "def _render_text_from_numeric(",
            "    value,",
            "    *,",
            "    source_type=None,",
            "    suppress_zero=True,",
            "    pad=None,",
            "    exponential=False,",
            "):",
            '    if source_type == "REAL" or isinstance(value, float):',
            "        numeric = float(value)",
            "        if not math.isfinite(numeric):",
            '            raise ValueError("REAL source is not finite")',
            '        return f"{numeric:.7E}" if exponential else f"{numeric:.7f}"',
            "",
            "    number = int(value)",
            "    effective_suppress_zero = suppress_zero if pad is None else False",
            "    signed_width = max(pad - 1, 0) if pad is not None and number < 0 else pad",
            "",
            '    if source_type == "WORD":',
            "        width = 4 if pad is None else pad",
            "        return _format_int_text(number & 0xFFFF, width, effective_suppress_zero, False)",
            '    if source_type == "DINT":',
            "        width = 10 if signed_width is None else signed_width",
            "        return _format_int_text(number, width, effective_suppress_zero)",
            '    if source_type == "INT":',
            "        width = 5 if signed_width is None else signed_width",
            "        return _format_int_text(number, width, effective_suppress_zero)",
            "",
            "    if pad is None:",
            '        return str(number) if suppress_zero else f"{number:05d}"',
            "    width = 5 if signed_width is None else signed_width",
            "    return _format_int_text(number, width, False)",
            "",
        ],
        "_termination_char": [
            "def _termination_char(termination_code):",
            "    if termination_code is None:",
            '        return ""',
            "    if isinstance(termination_code, str):",
            "        if len(termination_code) != 1:",
            '            raise ValueError("termination_code must be one character or int ASCII code")',
            "        return _as_single_ascii_char(termination_code)",
            "    if not isinstance(termination_code, int):",
            '        raise TypeError("termination_code must be int, str, or None")',
            "    return _ascii_char_from_code(termination_code)",
            "",
        ],
        "_parse_pack_text_value": [
            "def _parse_pack_text_value(text, dest_type):",
            '    if text == "":',
            '        raise ValueError("empty text cannot be parsed")',
            '    if dest_type in {"INT", "DINT"}:',
            '        if not re.fullmatch(r"[+-]?\\d+", text):',
            '            raise ValueError("integer parse failed")',
            "        parsed = int(text, 10)",
            '        if dest_type == "INT" and (parsed < -32768 or parsed > 32767):',
            '            raise ValueError("integer out of INT range")',
            '        if dest_type == "DINT" and (parsed < -2147483648 or parsed > 2147483647):',
            '            raise ValueError("integer out of DINT range")',
            "        return parsed",
            '    if dest_type == "WORD":',
            '        if not re.fullmatch(r"[0-9A-Fa-f]+", text):',
            '            raise ValueError("hex parse failed")',
            "        parsed = int(text, 16)",
            "        if parsed < 0 or parsed > 0xFFFF:",
            '            raise ValueError("hex out of WORD range")',
            "        return parsed",
            '    if dest_type == "REAL":',
            "        parsed = float(text)",
            "        if not math.isfinite(parsed):",
            '            raise ValueError("REAL parse produced non-finite value")',
            '        struct.pack("<f", parsed)',
            "        return parsed",
            '    raise TypeError(f"Unsupported pack_text destination type: {dest_type}")',
            "",
        ],
        "_store_copy_value_to_type": [
            "def _store_copy_value_to_type(value, dest_type):",
            "    if isinstance(value, float) and not math.isfinite(value):",
            "        value = 0",
            '    if dest_type == "INT":',
            f"        return max({_INT_MIN}, min({_INT_MAX}, int(value)))",
            '    if dest_type == "DINT":',
            f"        return max({_DINT_MIN}, min({_DINT_MAX}, int(value)))",
            '    if dest_type == "WORD":',
            "        return int(value) & 0xFFFF",
            '    if dest_type == "REAL":',
            "        return float(value)",
            '    if dest_type == "BOOL":',
            "        return bool(value)",
            '    if dest_type == "CHAR":',
            "        if not isinstance(value, str):",
            '            raise ValueError("CHAR value must be a string")',
            '        if value == "":',
            "            return value",
            "        if len(value) != 1 or ord(value) > 127:",
            '            raise ValueError("CHAR value must be blank or one ASCII character")',
            "        return value",
            "    return value",
            "",
        ],
    }
    needed_helpers = set(ctx.used_helpers)
    if "_store_numeric_text_digit" in needed_helpers:
        needed_helpers.add("_as_single_ascii_char")
    if "_termination_char" in needed_helpers:
        needed_helpers.update({"_as_single_ascii_char", "_ascii_char_from_code"})
    if "_render_text_from_numeric" in needed_helpers:
        needed_helpers.add("_format_int_text")

    for helper in _HELPER_ORDER:
        if helper in needed_helpers:
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
        fn_name = _first_defined_name(src)
        if fn_name is not None and fn_name != symbol:
            lines.append(f"{symbol} = {fn_name}")
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
    sd_ready_symbol = ctx.symbol_if_referenced(_SD_READY_TAG)
    sd_write_symbol = ctx.symbol_if_referenced(_SD_WRITE_STATUS_TAG)
    sd_error_symbol = ctx.symbol_if_referenced(_SD_ERROR_TAG)
    sd_error_code_symbol = ctx.symbol_if_referenced(_SD_ERROR_CODE_TAG)
    sd_save_symbol = ctx.symbol_if_referenced(_SD_SAVE_CMD_TAG)
    sd_eject_symbol = ctx.symbol_if_referenced(_SD_EJECT_CMD_TAG)
    sd_delete_symbol = ctx.symbol_if_referenced(_SD_DELETE_ALL_CMD_TAG)

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
    ]

    if sd_save_symbol is not None:
        lines.append(f"    _sd_save_cmd = bool({sd_save_symbol})")
    if sd_eject_symbol is not None:
        lines.append(f"    _sd_eject_cmd = bool({sd_eject_symbol})")
    if sd_delete_symbol is not None:
        lines.append(f"    _sd_delete_all_cmd = bool({sd_delete_symbol})")
    lines.extend(
        [
            "    _service_sd_commands()",
        ]
    )
    if sd_save_symbol is not None:
        lines.append(f"    {sd_save_symbol} = _sd_save_cmd")
    if sd_eject_symbol is not None:
        lines.append(f"    {sd_eject_symbol} = _sd_eject_cmd")
    if sd_delete_symbol is not None:
        lines.append(f"    {sd_delete_symbol} = _sd_delete_all_cmd")
    if sd_ready_symbol is not None:
        lines.append(f"    {sd_ready_symbol} = bool(_sd_available)")
    if sd_write_symbol is not None:
        lines.append(f"    {sd_write_symbol} = bool(_sd_write_status)")
    if sd_error_symbol is not None:
        lines.append(f"    {sd_error_symbol} = bool(_sd_error)")
    if sd_error_code_symbol is not None:
        lines.append(f"    {sd_error_code_symbol} = int(_sd_error_code)")

    lines.extend(
        [
            "    _read_inputs()",
            "    _run_main_rungs()",
            "    _write_outputs()",
            "",
        ]
    )
    for tag_name in sorted(ctx.edge_prev_tags):
        tag = ctx.referenced_tags[tag_name]
        lines.append(f'    _prev["{tag_name}"] = {ctx.symbol_for_tag(tag)}')
    lines.append("")
    if ctx.watchdog_ms is not None:
        lines.append("    _wd_pet()")
        lines.append("")
    lines.extend(
        [
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
        lines.append(f"{' ' * indent}{branch_var} = ({enabled_expr} and ({local_expr}))")

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
    parts = [compile_condition(cond, ctx) for cond in conditions]
    return " and ".join(parts)


def _compile_out_instruction(
    instr: OutInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    lines: list[str] = []
    sp = " " * indent
    enabled_literal = _bool_literal(enabled_expr)
    if instr.oneshot:
        key = f"_oneshot:{ctx.state_key_for(instr)}"
        if ctx._current_function is not None:
            ctx.mark_function_global(ctx._current_function, "_mem")
        if enabled_literal is False:
            lines.append(f"{sp}_mem[{key!r}] = False")
            lines.extend(_compile_target_write_lines(instr.target, "False", ctx, indent))
            return lines
        if enabled_literal is True:
            lines.append(f"{sp}if not bool(_mem.get({key!r}, False)):")
            lines.extend(_compile_target_write_lines(instr.target, "True", ctx, indent + 4))
            lines.append(f"{' ' * (indent + 4)}_mem[{key!r}] = True")
            return lines
        lines.append(f"{sp}if not ({enabled_expr}):")
        lines.append(f"{' ' * (indent + 4)}_mem[{key!r}] = False")
        lines.extend(_compile_target_write_lines(instr.target, "False", ctx, indent + 4))
        lines.append(f"{sp}elif not bool(_mem.get({key!r}, False)):")
        lines.extend(_compile_target_write_lines(instr.target, "True", ctx, indent + 4))
        lines.append(f"{' ' * (indent + 4)}_mem[{key!r}] = True")
        return lines

    if enabled_literal is True:
        return _compile_target_write_lines(instr.target, "True", ctx, indent)
    if enabled_literal is False:
        return _compile_target_write_lines(instr.target, "False", ctx, indent)

    lines.append(f"{sp}if {enabled_expr}:")
    lines.extend(_compile_target_write_lines(instr.target, "True", ctx, indent + 4))
    lines.append(f"{sp}else:")
    lines.extend(_compile_target_write_lines(instr.target, "False", ctx, indent + 4))
    return lines


def _compile_guarded_instruction(
    instr: Any,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
    enabled_body: list[str],
    *,
    disabled_body: list[str] | None = None,
) -> list[str]:
    sp = " " * indent
    lines: list[str] = []
    enabled_literal = _bool_literal(enabled_expr)
    if getattr(instr, "oneshot", False):
        key = f"_oneshot:{ctx.state_key_for(instr)}"
        if ctx._current_function is not None:
            ctx.mark_function_global(ctx._current_function, "_mem")
        if enabled_literal is False:
            lines.append(f"{sp}_mem[{key!r}] = False")
            if disabled_body:
                lines.extend(f"{sp}{line}" for line in disabled_body)
            return lines
        if enabled_literal is True:
            lines.append(f"{sp}if not bool(_mem.get({key!r}, False)):")
            lines.extend(f"{' ' * (indent + 4)}{line}" for line in enabled_body)
            lines.append(f"{' ' * (indent + 4)}_mem[{key!r}] = True")
            return lines
        lines.append(f"{sp}if not ({enabled_expr}):")
        lines.append(f"{' ' * (indent + 4)}_mem[{key!r}] = False")
        if disabled_body:
            lines.extend(f"{' ' * (indent + 4)}{line}" for line in disabled_body)
        lines.append(f"{sp}elif not bool(_mem.get({key!r}, False)):")
        lines.extend(f"{' ' * (indent + 4)}{line}" for line in enabled_body)
        lines.append(f"{' ' * (indent + 4)}_mem[{key!r}] = True")
        return lines

    if enabled_literal is True:
        lines.extend(f"{sp}{line}" for line in enabled_body)
        return lines
    if enabled_literal is False:
        if disabled_body is not None:
            lines.extend(f"{sp}{line}" for line in disabled_body)
        return lines

    lines.append(f"{sp}if {enabled_expr}:")
    lines.extend(f"{' ' * (indent + 4)}{line}" for line in enabled_body)
    if disabled_body is not None:
        lines.append(f"{sp}else:")
        lines.extend(f"{' ' * (indent + 4)}{line}" for line in disabled_body)
    return lines


def _compile_call_instruction(
    instr: CallInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    sp = " " * indent
    fn = _subroutine_symbol(instr.subroutine_name)
    enabled_literal = _bool_literal(enabled_expr)
    if enabled_literal is False:
        return []
    if enabled_literal is True:
        return [f"{sp}{fn}()"]
    return [f"{sp}if {enabled_expr}:", f"{sp}    {fn}()"]


def _compile_return_instruction(enabled_expr: str, indent: int) -> list[str]:
    sp = " " * indent
    enabled_literal = _bool_literal(enabled_expr)
    if enabled_literal is False:
        return []
    if enabled_literal is True:
        return [f"{sp}return"]
    return [f"{sp}if {enabled_expr}:", f"{sp}    return"]


def _compile_on_delay_instruction(
    instr: OnDelayInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    done = ctx.symbol_for_tag(instr.done_bit)
    acc = ctx.symbol_for_tag(instr.accumulator)
    frac_key = f"_frac:{instr.accumulator.name}"
    preset = _compile_value(instr.preset, ctx)
    unit_expr = _timer_dt_to_units_expr(instr.unit, "_dt", "_frac")
    if ctx._current_function is not None:
        ctx.mark_function_global(ctx._current_function, "_mem")
    sp = " " * indent
    lines: list[str] = [
        f'{sp}_frac = float(_mem.get("{frac_key}", 0.0))',
    ]
    if instr.reset_condition is not None:
        reset_expr = compile_condition(instr.reset_condition, ctx)
        lines.extend(
            [
                f"{sp}if {reset_expr}:",
                f'{" " * (indent + 4)}_mem["{frac_key}"] = 0.0',
                f"{' ' * (indent + 4)}{done} = False",
                f"{' ' * (indent + 4)}{acc} = 0",
                f"{sp}else:",
            ]
        )
        inner = indent + 4
    else:
        inner = indent
    isp = " " * inner
    lines.extend(
        [
            f"{isp}if {enabled_expr}:",
            f'{" " * (inner + 4)}_dt = float(_mem.get("_dt", 0.0))',
            f"{' ' * (inner + 4)}_acc = int({acc})",
            f"{' ' * (inner + 4)}_dt_units = {unit_expr}",
            f"{' ' * (inner + 4)}_int_units = int(_dt_units)",
            f"{' ' * (inner + 4)}_new_frac = _dt_units - _int_units",
            f"{' ' * (inner + 4)}_acc = min(_acc + _int_units, {_INT_MAX})",
            f"{' ' * (inner + 4)}_preset = int({preset})",
            f'{" " * (inner + 4)}_mem["{frac_key}"] = _new_frac',
            f"{' ' * (inner + 4)}{done} = (_acc >= _preset)",
            f"{' ' * (inner + 4)}{acc} = _acc",
        ]
    )
    if not instr.has_reset:
        lines.extend(
            [
                f"{isp}else:",
                f'{" " * (inner + 4)}_mem["{frac_key}"] = 0.0',
                f"{' ' * (inner + 4)}{done} = False",
                f"{' ' * (inner + 4)}{acc} = 0",
            ]
        )
    return lines


def _compile_off_delay_instruction(
    instr: OffDelayInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    done = ctx.symbol_for_tag(instr.done_bit)
    acc = ctx.symbol_for_tag(instr.accumulator)
    frac_key = f"_frac:{instr.accumulator.name}"
    preset = _compile_value(instr.preset, ctx)
    unit_expr = _timer_dt_to_units_expr(instr.unit, "_dt", "_frac")
    if ctx._current_function is not None:
        ctx.mark_function_global(ctx._current_function, "_mem")
    sp = " " * indent
    return [
        f'{sp}_frac = float(_mem.get("{frac_key}", 0.0))',
        f"{sp}if {enabled_expr}:",
        f'{" " * (indent + 4)}_mem["{frac_key}"] = 0.0',
        f"{' ' * (indent + 4)}{done} = True",
        f"{' ' * (indent + 4)}{acc} = 0",
        f"{sp}else:",
        f'{" " * (indent + 4)}_dt = float(_mem.get("_dt", 0.0))',
        f"{' ' * (indent + 4)}_acc = int({acc})",
        f"{' ' * (indent + 4)}_dt_units = {unit_expr}",
        f"{' ' * (indent + 4)}_int_units = int(_dt_units)",
        f"{' ' * (indent + 4)}_new_frac = _dt_units - _int_units",
        f"{' ' * (indent + 4)}_acc = min(_acc + _int_units, {_INT_MAX})",
        f"{' ' * (indent + 4)}_preset = int({preset})",
        f'{" " * (indent + 4)}_mem["{frac_key}"] = _new_frac',
        f"{' ' * (indent + 4)}{done} = (_acc < _preset)",
        f"{' ' * (indent + 4)}{acc} = _acc",
    ]


def _compile_count_up_instruction(
    instr: CountUpInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    done = ctx.symbol_for_tag(instr.done_bit)
    acc = ctx.symbol_for_tag(instr.accumulator)
    preset = _compile_value(instr.preset, ctx)
    sp = " " * indent
    lines: list[str] = []
    if instr.reset_condition is not None:
        reset_expr = compile_condition(instr.reset_condition, ctx)
        lines.extend(
            [
                f"{sp}if {reset_expr}:",
                f"{' ' * (indent + 4)}{done} = False",
                f"{' ' * (indent + 4)}{acc} = 0",
                f"{sp}else:",
            ]
        )
        inner = indent + 4
    else:
        inner = indent
    isp = " " * inner
    lines.extend(
        [
            f"{' ' * inner}_acc = int({acc})",
            f"{' ' * inner}_delta = 0",
            f"{isp}if {enabled_expr}:",
            f"{' ' * (inner + 4)}_delta += 1",
        ]
    )
    if instr.down_condition is not None:
        down_expr = compile_condition(instr.down_condition, ctx)
        lines.extend(
            [
                f"{isp}if {down_expr}:",
                f"{' ' * (inner + 4)}_delta -= 1",
            ]
        )
    lines.extend(
        [
            f"{' ' * inner}_acc = max({_DINT_MIN}, min({_DINT_MAX}, _acc + _delta))",
            f"{' ' * inner}_preset = int({preset})",
            f"{' ' * inner}{done} = (_acc >= _preset)",
            f"{' ' * inner}{acc} = _acc",
        ]
    )
    return lines


def _compile_count_down_instruction(
    instr: CountDownInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    done = ctx.symbol_for_tag(instr.done_bit)
    acc = ctx.symbol_for_tag(instr.accumulator)
    preset = _compile_value(instr.preset, ctx)
    sp = " " * indent
    lines: list[str] = []
    if instr.reset_condition is not None:
        reset_expr = compile_condition(instr.reset_condition, ctx)
        lines.extend(
            [
                f"{sp}if {reset_expr}:",
                f"{' ' * (indent + 4)}{done} = False",
                f"{' ' * (indent + 4)}{acc} = 0",
                f"{sp}else:",
            ]
        )
        inner = indent + 4
    else:
        inner = indent
    isp = " " * inner
    lines.extend(
        [
            f"{' ' * inner}_acc = int({acc})",
            f"{isp}if {enabled_expr}:",
            f"{' ' * (inner + 4)}_acc -= 1",
            f"{' ' * inner}_acc = max({_DINT_MIN}, min({_DINT_MAX}, _acc))",
            f"{' ' * inner}_preset = int({preset})",
            f"{' ' * inner}{done} = (_acc <= -_preset)",
            f"{' ' * inner}{acc} = _acc",
        ]
    )
    return lines


def _compile_copy_instruction(
    instr: CopyInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    if isinstance(instr.source, CopyModifier):
        return _compile_copy_modifier_instruction(instr, enabled_expr, ctx, indent)
    target_type = _value_type_name(instr.target)
    ctx.mark_helper("_store_copy_value_to_type")
    source_expr = _compile_value(instr.source, ctx)
    value_expr = f'_store_copy_value_to_type({source_expr}, "{target_type}")'
    enabled_body = _compile_assignment_lines(instr.target, value_expr, ctx, indent=0)
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)


def _compile_copy_modifier_instruction(
    instr: CopyInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    modifier = instr.source
    if not isinstance(modifier, CopyModifier):
        raise TypeError("copy modifier compiler requires CopyModifier source")

    stem = ctx.next_name("copymod")
    fault_body = _compile_set_out_of_range_fault_body(ctx)
    mode = modifier.mode
    source_expr = _compile_value(modifier.source, ctx)

    setup, target_kind, target_symbol, target_start_var, target_type = _copy_modifier_target_info(
        instr.target, ctx, stem
    )
    values_var = f"_{stem}_values"
    write_lines = _copy_modifier_write_lines(
        values_var=values_var,
        target_kind=target_kind,
        target_symbol=target_symbol,
        target_start_var=target_start_var,
        fault_body=fault_body,
    )

    enabled_body: list[str] = [*setup]
    if mode in {"value", "ascii"}:
        ctx.mark_helper("_text_from_source_value")
        ctx.mark_helper("_store_numeric_text_digit")
        ctx.mark_helper("_store_copy_value_to_type")
        enabled_body.extend(
            [
                "try:",
                f"    _copy_text = _text_from_source_value({source_expr})",
                f"    {values_var} = []",
                "    for _copy_char in _copy_text:",
                f'        _copy_numeric = _store_numeric_text_digit(_copy_char, "{mode}")',
                f'        {values_var}.append(_store_copy_value_to_type(_copy_numeric, "{target_type}"))',
                *_indent_body(write_lines, 4),
                "except (IndexError, TypeError, ValueError, OverflowError):",
                *_indent_body(fault_body, 4),
            ]
        )
    elif mode == "text":
        ctx.mark_helper("_render_text_from_numeric")
        ctx.mark_helper("_termination_char")
        ctx.mark_helper("_store_copy_value_to_type")
        source_type = _optional_value_type_name(modifier.source)
        enabled_body.extend(
            [
                "try:",
                "    _rendered = _render_text_from_numeric(",
                f"        {source_expr},",
                f"        source_type={source_type!r},",
                f"        suppress_zero={modifier.suppress_zero!r},",
                f"        pad={modifier.pad!r},",
                f"        exponential={modifier.exponential!r},",
                "    )",
                f"    _rendered += _termination_char({modifier.termination_code!r})",
                f'    {values_var} = [_store_copy_value_to_type(_ch, "{target_type}") for _ch in _rendered]',
                *_indent_body(write_lines, 4),
                "except (IndexError, TypeError, ValueError, OverflowError):",
                *_indent_body(fault_body, 4),
            ]
        )
    elif mode == "binary":
        ctx.mark_helper("_ascii_char_from_code")
        ctx.mark_helper("_store_copy_value_to_type")
        enabled_body.extend(
            [
                "try:",
                f"    _copy_char = _ascii_char_from_code(int({source_expr}) & 0xFF)",
                f'    {values_var} = [_store_copy_value_to_type(_copy_char, "{target_type}")]',
                *_indent_body(write_lines, 4),
                "except (IndexError, TypeError, ValueError, OverflowError):",
                *_indent_body(fault_body, 4),
            ]
        )
    else:
        enabled_body.extend(fault_body)
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)


def _compile_calc_instruction(
    instr: CalcInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    value_expr = _compile_value(instr.expression, ctx)
    store_expr = _calc_store_expr("_calc_value", instr.dest.type.name, instr.mode, ctx)
    enabled_body = [
        "try:",
        f"    _calc_value = {value_expr}",
        "except ZeroDivisionError:",
        "    _calc_value = 0",
        "if isinstance(_calc_value, float) and not math.isfinite(_calc_value):",
        "    _calc_value = 0",
        f"{ctx.symbol_for_tag(instr.dest)} = {store_expr}",
    ]
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)


def _compile_blockcopy_instruction(
    instr: BlockCopyInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    if isinstance(instr.source, CopyModifier):
        return _compile_blockcopy_modifier_instruction(instr, enabled_expr, ctx, indent)
    ctx.mark_helper("_store_copy_value_to_type")
    stem = ctx.next_name("blockcopy")
    src_setup, src_symbol, src_indices, _ = _compile_range_setup(
        instr.source, ctx, stem=f"{stem}_src", include_addresses=False
    )
    dst_setup, dst_symbol, dst_indices, _ = _compile_range_setup(
        instr.dest, ctx, stem=f"{stem}_dst", include_addresses=False
    )
    enabled_body = [
        *src_setup,
        *dst_setup,
        f"if len({src_indices}) != len({dst_indices}):",
        f'    raise ValueError(f"BlockCopy length mismatch: source has {{len({src_indices})}} elements, dest has {{len({dst_indices})}} elements")',
        f"for _src_idx, _dst_idx in zip({src_indices}, {dst_indices}):",
        f"    _raw = {src_symbol}[_src_idx]",
        f'    {dst_symbol}[_dst_idx] = _store_copy_value_to_type(_raw, "{_range_type_name(instr.dest)}")',
    ]
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)


def _compile_blockcopy_modifier_instruction(
    instr: BlockCopyInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    modifier = instr.source
    if not isinstance(modifier, CopyModifier):
        raise TypeError("blockcopy modifier compiler requires CopyModifier source")

    stem = ctx.next_name("blockcopymod")
    src_setup, src_symbol, src_indices, _ = _compile_range_setup(
        modifier.source, ctx, stem=f"{stem}_src", include_addresses=False
    )
    dst_setup, dst_symbol, dst_indices, _ = _compile_range_setup(
        instr.dest, ctx, stem=f"{stem}_dst", include_addresses=False
    )
    mode = modifier.mode
    dst_type = _range_type_name(instr.dest)
    fault_body = _compile_set_out_of_range_fault_body(ctx)
    enabled_body: list[str] = [
        *src_setup,
        *dst_setup,
        f"if len({src_indices}) != len({dst_indices}):",
        f'    raise ValueError(f"BlockCopy length mismatch: source has {{len({src_indices})}} elements, dest has {{len({dst_indices})}} elements")',
    ]

    if mode in {"value", "ascii"}:
        ctx.mark_helper("_text_from_source_value")
        ctx.mark_helper("_store_numeric_text_digit")
        ctx.mark_helper("_store_copy_value_to_type")
        enabled_body.extend(
            [
                "try:",
                "    _converted = []",
                f"    for _src_idx in {src_indices}:",
                f"        _raw_char = _text_from_source_value({src_symbol}[_src_idx])",
                "        if len(_raw_char) != 1:",
                '            raise ValueError("BlockCopy text->numeric conversion requires single CHAR values")',
                f'        _numeric = _store_numeric_text_digit(_raw_char, "{mode}")',
                f'        _converted.append(_store_copy_value_to_type(_numeric, "{dst_type}"))',
                f"    for _dst_idx, _converted_value in zip({dst_indices}, _converted):",
                f"        {dst_symbol}[_dst_idx] = _converted_value",
                "except (IndexError, TypeError, ValueError, OverflowError):",
                *_indent_body(fault_body, 4),
            ]
        )
    elif mode == "text":
        ctx.mark_helper("_render_text_from_numeric")
        ctx.mark_helper("_termination_char")
        ctx.mark_helper("_store_copy_value_to_type")
        source_type = _optional_range_type_name(modifier.source)
        enabled_body.extend(
            [
                "try:",
                "    _rendered_parts = []",
                f"    for _src_idx in {src_indices}:",
                "        _rendered_parts.append(",
                "            _render_text_from_numeric(",
                f"                {src_symbol}[_src_idx],",
                f"                source_type={source_type!r},",
                f"                suppress_zero={modifier.suppress_zero!r},",
                f"                pad={modifier.pad!r},",
                f"                exponential={modifier.exponential!r},",
                "            )",
                "        )",
                "    _rendered = ''.join(_rendered_parts)",
                f"    _rendered += _termination_char({modifier.termination_code!r})",
                f"    if len(_rendered) != len({dst_indices}):",
                '        raise ValueError("formatted text length does not match destination range")',
                f"    for _dst_idx, _char in zip({dst_indices}, _rendered):",
                f'        {dst_symbol}[_dst_idx] = _store_copy_value_to_type(_char, "{dst_type}")',
                "except (IndexError, TypeError, ValueError, OverflowError):",
                *_indent_body(fault_body, 4),
            ]
        )
    elif mode == "binary":
        ctx.mark_helper("_ascii_char_from_code")
        ctx.mark_helper("_store_copy_value_to_type")
        enabled_body.extend(
            [
                "try:",
                "    _converted = []",
                f"    for _src_idx in {src_indices}:",
                f"        _char = _ascii_char_from_code(int({src_symbol}[_src_idx]) & 0xFF)",
                f'        _converted.append(_store_copy_value_to_type(_char, "{dst_type}"))',
                f"    for _dst_idx, _converted_value in zip({dst_indices}, _converted):",
                f"        {dst_symbol}[_dst_idx] = _converted_value",
                "except (IndexError, TypeError, ValueError, OverflowError):",
                *_indent_body(fault_body, 4),
            ]
        )
    else:
        enabled_body.extend(fault_body)
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)


def _compile_fill_instruction(
    instr: FillInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    if isinstance(instr.value, CopyModifier):
        return _compile_fill_modifier_instruction(instr, enabled_expr, ctx, indent)
    ctx.mark_helper("_store_copy_value_to_type")
    stem = ctx.next_name("fill")
    dst_setup, dst_symbol, dst_indices, _ = _compile_range_setup(
        instr.dest, ctx, stem=f"{stem}_dst", include_addresses=False
    )
    value_expr = _compile_value(instr.value, ctx)
    enabled_body = [
        *dst_setup,
        f"_fill_value = {value_expr}",
        f"for _dst_idx in {dst_indices}:",
        f'    {dst_symbol}[_dst_idx] = _store_copy_value_to_type(_fill_value, "{_range_type_name(instr.dest)}")',
    ]
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)


def _compile_fill_modifier_instruction(
    instr: FillInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    modifier = instr.value
    if not isinstance(modifier, CopyModifier):
        raise TypeError("fill modifier compiler requires CopyModifier value")

    stem = ctx.next_name("fillmod")
    dst_setup, dst_symbol, dst_indices, _ = _compile_range_setup(
        instr.dest, ctx, stem=f"{stem}_dst", include_addresses=False
    )
    mode = modifier.mode
    dst_type = _range_type_name(instr.dest)
    source_expr = _compile_value(modifier.source, ctx)
    enabled_body: list[str] = [
        *dst_setup,
        f"if not {dst_indices}:",
        "    pass",
        "else:",
    ]

    inner: list[str] = []
    if mode in {"value", "ascii"}:
        ctx.mark_helper("_text_from_source_value")
        ctx.mark_helper("_store_numeric_text_digit")
        ctx.mark_helper("_store_copy_value_to_type")
        inner.extend(
            [
                f"_fill_text = _text_from_source_value({source_expr})",
                "if len(_fill_text) != 1:",
                '    raise ValueError("fill text->numeric conversion requires a single source character")',
                f'_fill_numeric = _store_numeric_text_digit(_fill_text, "{mode}")',
                f'_fill_value = _store_copy_value_to_type(_fill_numeric, "{dst_type}")',
                f"for _dst_idx in {dst_indices}:",
                f"    {dst_symbol}[_dst_idx] = _fill_value",
            ]
        )
    elif mode == "text":
        ctx.mark_helper("_render_text_from_numeric")
        ctx.mark_helper("_termination_char")
        ctx.mark_helper("_store_copy_value_to_type")
        source_type = _optional_value_type_name(modifier.source)
        inner.extend(
            [
                f'if "{dst_type}" != "CHAR":',
                '    raise TypeError("fill(as_text(...)) requires CHAR destination range")',
                "_fill_text = _render_text_from_numeric(",
                f"    {source_expr},",
                f"    source_type={source_type!r},",
                f"    suppress_zero={modifier.suppress_zero!r},",
                f"    pad={modifier.pad!r},",
                f"    exponential={modifier.exponential!r},",
                ")",
                f"_fill_text += _termination_char({modifier.termination_code!r})",
                f"if len(_fill_text) > len({dst_indices}):",
                '    raise ValueError("formatted fill text exceeds destination range")',
                f"for _fill_offset, _dst_idx in enumerate({dst_indices}):",
                "    if _fill_offset < len(_fill_text):",
                f'        {dst_symbol}[_dst_idx] = _store_copy_value_to_type(_fill_text[_fill_offset], "CHAR")',
                "    else:",
                f"        {dst_symbol}[_dst_idx] = ''",
            ]
        )
    elif mode == "binary":
        ctx.mark_helper("_ascii_char_from_code")
        ctx.mark_helper("_store_copy_value_to_type")
        inner.extend(
            [
                f"_fill_char = _ascii_char_from_code(int({source_expr}) & 0xFF)",
                f'_fill_value = _store_copy_value_to_type(_fill_char, "{dst_type}")',
                f"for _dst_idx in {dst_indices}:",
                f"    {dst_symbol}[_dst_idx] = _fill_value",
            ]
        )
    else:
        inner.append(f'raise ValueError("Unsupported fill modifier mode: {mode}")')

    enabled_body.extend(_indent_body(inner, 4))
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)


def _compile_search_instruction(
    instr: SearchInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    stem = ctx.next_name("search")
    range_setup, range_symbol, range_indices, range_addrs = _compile_range_setup(
        instr.search_range,
        ctx,
        stem=f"{stem}_rng",
        include_addresses=True,
    )
    result_symbol = ctx.symbol_for_tag(instr.result)
    found_symbol = ctx.symbol_for_tag(instr.found)
    value_expr = _compile_value(instr.value, ctx)
    range_type = _range_type_name(instr.search_range)
    compare_expr = _search_compare_expr(instr.condition, "_candidate", "_rhs")
    miss_body = [f"{result_symbol} = -1", f"{found_symbol} = False"]

    text_path = range_type == "CHAR"
    enabled_body: list[str] = [
        *range_setup,
        f"if not {range_addrs}:",
        "    _cursor_index = None",
        "else:",
    ]
    if instr.continuous:
        enabled_body.extend(
            [
                f"    _current_result = int({result_symbol})",
                "    if _current_result == 0:",
                "        _cursor_index = 0",
                "    elif _current_result == -1:",
                "        _cursor_index = None",
                "    else:",
            ]
        )
        if _range_reverse(instr.search_range):
            enabled_body.extend(
                [
                    "        _cursor_index = None",
                    f"        for _idx, _addr in enumerate({range_addrs}):",
                    "            if _addr < _current_result:",
                    "                _cursor_index = _idx",
                    "                break",
                ]
            )
        else:
            enabled_body.extend(
                [
                    "        _cursor_index = None",
                    f"        for _idx, _addr in enumerate({range_addrs}):",
                    "            if _addr > _current_result:",
                    "                _cursor_index = _idx",
                    "                break",
                ]
            )
    else:
        enabled_body.append("    _cursor_index = 0")

    enabled_body.extend(
        [
            "if _cursor_index is None:",
            *[f"    {line}" for line in miss_body],
            "else:",
        ]
    )

    if text_path:
        enabled_body.extend(
            [
                f'    if {instr.condition!r} not in ("==", "!="):',
                "        raise ValueError(\"Text search only supports '==' and '!=' conditions\")",
                f"    _rhs = str({value_expr})",
                '    if _rhs == "":',
                '        raise ValueError("Text search value cannot be empty")',
                "    _window_len = len(_rhs)",
                f"    if _window_len > len({range_indices}):",
                *[f"        {line}" for line in miss_body],
                "    else:",
                f"        _last_start = len({range_indices}) - _window_len",
                "        if _cursor_index > _last_start:",
                *[f"            {line}" for line in miss_body],
                "        else:",
                "            _matched = None",
                "            for _start in range(_cursor_index, _last_start + 1):",
                "                _candidate = ''.join(str("
                f"{range_symbol}[{range_indices}[_start + _off]]) for _off in range(_window_len))",
                f"                if ({'(_candidate == _rhs)' if instr.condition == '==' else '(_candidate != _rhs)'}):",
                "                    _matched = _start",
                "                    break",
                "            if _matched is None:",
                *[f"                {line}" for line in miss_body],
                "            else:",
                f"                {result_symbol} = {range_addrs}[_matched]",
                f"                {found_symbol} = True",
            ]
        )
    else:
        enabled_body.extend(
            [
                f"    _rhs = {value_expr}",
                "    _matched = None",
                f"    for _idx in range(_cursor_index, len({range_indices})):",
                f"        _candidate = {range_symbol}[{range_indices}[_idx]]",
                f"        if {compare_expr}:",
                "            _matched = _idx",
                "            break",
                "    if _matched is None:",
                *[f"        {line}" for line in miss_body],
                "    else:",
                f"        {result_symbol} = {range_addrs}[_matched]",
                f"        {found_symbol} = True",
            ]
        )
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)


def _compile_shift_instruction(
    instr: ShiftInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    if _range_type_name(instr.bit_range) != "BOOL":
        raise TypeError("shift bit_range must contain BOOL tags")
    if ctx._current_function is not None:
        ctx.mark_function_global(ctx._current_function, "_mem")
    key = f"_shift_prev_clock:{ctx.state_key_for(instr)}"
    stem = ctx.next_name("shift")
    range_setup, range_symbol, range_indices, _ = _compile_range_setup(
        instr.bit_range, ctx, stem=f"{stem}_rng", include_addresses=False
    )
    clock_expr = compile_condition(instr.clock_condition, ctx)
    reset_expr = compile_condition(instr.reset_condition, ctx)
    lines = [
        *range_setup,
        f"if not {range_indices}:",
        '    raise ValueError("shift bit_range resolved to an empty range")',
        f"_clock_curr = {clock_expr}",
        f"_clock_prev = bool(_mem.get({key!r}, False))",
        "_rising_edge = _clock_curr and not _clock_prev",
        "if _rising_edge:",
        f"    _prev_values = [bool({range_symbol}[_idx]) for _idx in {range_indices}]",
        f"    {range_symbol}[{range_indices}[0]] = bool({enabled_expr})",
        f"    for _pos in range(1, len({range_indices})):",
        f"        {range_symbol}[{range_indices}[_pos]] = _prev_values[_pos - 1]",
        f"if {reset_expr}:",
        f"    for _idx in {range_indices}:",
        f"        {range_symbol}[_idx] = False",
        f"_mem[{key!r}] = _clock_curr",
    ]
    return [" " * indent + line for line in lines]


def _compile_pack_bits_instruction(
    instr: PackBitsInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    dest_type = _value_type_name(instr.dest)
    if dest_type not in {"INT", "WORD", "DINT", "REAL"}:
        raise TypeError("pack_bits destination must be INT, WORD, DINT, or REAL")
    if _range_type_name(instr.bit_block) != "BOOL":
        raise TypeError("pack_bits source range must contain BOOL tags")
    width = 16 if dest_type in {"INT", "WORD"} else 32
    stem = ctx.next_name("packbits")
    src_setup, src_symbol, src_indices, _ = _compile_range_setup(
        instr.bit_block, ctx, stem=f"{stem}_src", include_addresses=False
    )
    enabled_body = [
        *src_setup,
        f"if len({src_indices}) > {width}:",
        f'    raise ValueError(f"pack_bits destination width is {width} bits but block has {{len({src_indices})}} tags")',
        "_packed = 0",
        f"for _bit_index, _src_idx in enumerate({src_indices}):",
        f"    if bool({src_symbol}[_src_idx]):",
        "        _packed |= (1 << _bit_index)",
        f"_packed_value = {_pack_store_expr('_packed', dest_type, ctx)}",
        *_compile_assignment_lines(instr.dest, "_packed_value", ctx, indent=0),
    ]
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)


def _compile_pack_words_instruction(
    instr: PackWordsInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    dest_type = _value_type_name(instr.dest)
    if dest_type not in {"DINT", "REAL"}:
        raise TypeError("pack_words destination must be DINT or REAL")
    if _range_type_name(instr.word_block) not in {"INT", "WORD"}:
        raise TypeError("pack_words source range must contain INT/WORD tags")
    stem = ctx.next_name("packwords")
    src_setup, src_symbol, src_indices, _ = _compile_range_setup(
        instr.word_block, ctx, stem=f"{stem}_src", include_addresses=False
    )
    enabled_body = [
        *src_setup,
        f"if len({src_indices}) != 2:",
        f'    raise ValueError(f"pack_words requires exactly 2 source tags; got {{len({src_indices})}}")',
        f"_lo_value = int({src_symbol}[{src_indices}[0]])",
        f"_hi_value = int({src_symbol}[{src_indices}[1]])",
        "_packed = ((_hi_value << 16) | (_lo_value & 0xFFFF))",
        f"_packed_value = {_pack_store_expr('_packed', dest_type, ctx)}",
        *_compile_assignment_lines(instr.dest, "_packed_value", ctx, indent=0),
    ]
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)


def _compile_pack_text_instruction(
    instr: PackTextInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    source_type = _range_type_name(instr.source_range)
    if source_type != "CHAR":
        raise TypeError("pack_text source range must contain CHAR tags")
    dest_type = _value_type_name(instr.dest)
    if dest_type not in {"INT", "DINT", "WORD", "REAL"}:
        raise TypeError("pack_text destination must be INT, DINT, WORD, or REAL")
    ctx.mark_helper("_parse_pack_text_value")
    ctx.mark_helper("_store_copy_value_to_type")
    stem = ctx.next_name("packtext")
    src_setup, src_symbol, src_indices, _ = _compile_range_setup(
        instr.source_range, ctx, stem=f"{stem}_src", include_addresses=False
    )
    fault_body = _compile_set_out_of_range_fault_body(ctx)
    enabled_body = [
        *src_setup,
        f"_text = ''.join(str({src_symbol}[_idx]) for _idx in {src_indices})",
    ]
    if instr.allow_whitespace:
        enabled_body.append("_text = _text.strip()")
    else:
        enabled_body.extend(
            [
                "if _text != _text.strip():",
                *_indent_body(fault_body, 4),
                "else:",
                "    try:",
                f'        _parsed = _parse_pack_text_value(_text, "{dest_type}")',
                f'        _packed_value = _store_copy_value_to_type(_parsed, "{dest_type}")',
                *_indent_body(
                    _compile_assignment_lines(instr.dest, "_packed_value", ctx, indent=0), 8
                ),
                "    except (TypeError, ValueError, OverflowError):",
                *_indent_body(fault_body, 8),
            ]
        )
        return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)
    enabled_body.extend(
        [
            "try:",
            f'    _parsed = _parse_pack_text_value(_text, "{dest_type}")',
            f'    _packed_value = _store_copy_value_to_type(_parsed, "{dest_type}")',
            *_indent_body(_compile_assignment_lines(instr.dest, "_packed_value", ctx, indent=0), 4),
            "except (TypeError, ValueError, OverflowError):",
            *_indent_body(fault_body, 4),
        ]
    )
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)


def _compile_set_out_of_range_fault_body(ctx: CodegenContext) -> list[str]:
    fault_symbol = ctx.symbol_if_referenced(_FAULT_OUT_OF_RANGE_TAG)
    if fault_symbol is None:
        return ["pass"]
    return [f"{fault_symbol} = True"]


def _compile_unpack_bits_instruction(
    instr: UnpackToBitsInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    source_type = _value_type_name(instr.source)
    if source_type not in {"INT", "WORD", "DINT", "REAL"}:
        raise TypeError("unpack_to_bits source must be INT, WORD, DINT, or REAL")
    if _range_type_name(instr.bit_block) != "BOOL":
        raise TypeError("unpack_to_bits destination range must contain BOOL tags")
    width = 16 if source_type in {"INT", "WORD"} else 32
    stem = ctx.next_name("unpackbits")
    dst_setup, dst_symbol, dst_indices, _ = _compile_range_setup(
        instr.bit_block, ctx, stem=f"{stem}_dst", include_addresses=False
    )
    if source_type == "REAL":
        ctx.mark_helper("_float_to_int_bits")
        bits_expr = f"_float_to_int_bits({_compile_value(instr.source, ctx)})"
    elif source_type in {"INT", "WORD"}:
        bits_expr = f"(int({_compile_value(instr.source, ctx)}) & 0xFFFF)"
    else:
        bits_expr = f"(int({_compile_value(instr.source, ctx)}) & 0xFFFFFFFF)"
    enabled_body = [
        *dst_setup,
        f"if len({dst_indices}) > {width}:",
        f'    raise ValueError(f"unpack_to_bits source width is {width} bits but block has {{len({dst_indices})}} tags")',
        f"_bits = {bits_expr}",
        f"for _bit_index, _dst_idx in enumerate({dst_indices}):",
        f"    {dst_symbol}[_dst_idx] = bool((_bits >> _bit_index) & 1)",
    ]
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)


def _compile_unpack_words_instruction(
    instr: UnpackToWordsInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    source_type = _value_type_name(instr.source)
    if source_type not in {"DINT", "REAL"}:
        raise TypeError("unpack_to_words source must be DINT or REAL")
    dst_type = _range_type_name(instr.word_block)
    if dst_type not in {"INT", "WORD"}:
        raise TypeError("unpack_to_words destination range must contain INT/WORD tags")
    stem = ctx.next_name("unpackwords")
    dst_setup, dst_symbol, dst_indices, _ = _compile_range_setup(
        instr.word_block, ctx, stem=f"{stem}_dst", include_addresses=False
    )
    if source_type == "REAL":
        ctx.mark_helper("_float_to_int_bits")
        bits_expr = f"_float_to_int_bits({_compile_value(instr.source, ctx)})"
    else:
        bits_expr = f"(int({_compile_value(instr.source, ctx)}) & 0xFFFFFFFF)"
    lo_expr = "_lo_word"
    hi_expr = "_hi_word"
    if dst_type == "INT":
        ctx.mark_helper("_wrap_int")
        lo_store = f"_wrap_int({lo_expr}, 16, True)"
        hi_store = f"_wrap_int({hi_expr}, 16, True)"
    else:
        lo_store = f"({lo_expr} & 0xFFFF)"
        hi_store = f"({hi_expr} & 0xFFFF)"
    enabled_body = [
        *dst_setup,
        f"if len({dst_indices}) != 2:",
        f'    raise ValueError(f"unpack_to_words requires exactly 2 destination tags; got {{len({dst_indices})}}")',
        f"_bits = {bits_expr}",
        "_lo_word = (_bits & 0xFFFF)",
        "_hi_word = ((_bits >> 16) & 0xFFFF)",
        f"{dst_symbol}[{dst_indices}[0]] = {lo_store}",
        f"{dst_symbol}[{dst_indices}[1]] = {hi_store}",
    ]
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)


def _compile_function_call_instruction(
    instr: FunctionCallInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    fn_symbol = ctx.register_function_source(instr._fn)
    fn_name = getattr(instr._fn, "__name__", type(instr._fn).__name__)
    result_var = f"_{ctx.next_name('fn_result')}"
    kwargs = ", ".join(
        f"{name}={_compile_value(value, ctx)}" for name, value in sorted(instr._ins.items())
    )
    call_expr = f"{fn_symbol}({kwargs})" if kwargs else f"{fn_symbol}()"
    enabled_body = [f"{result_var} = {call_expr}"]
    if instr._outs:
        enabled_body.extend(
            [
                f"if {result_var} is None:",
                f'    raise TypeError("run_function: {fn_name!r} returned None but outs were declared")',
            ]
        )
        ctx.mark_helper("_store_copy_value_to_type")
        for key, target in sorted(instr._outs.items()):
            target_type = _value_type_name(target)
            enabled_body.extend(
                [
                    f"if {key!r} not in {result_var}:",
                    "    raise KeyError(",
                    f'        f"run_function: {fn_name!r} missing key {key!r}; got {{sorted({result_var})}}"',
                    "    )",
                ]
            )
            value_expr = f'_store_copy_value_to_type({result_var}[{key!r}], "{target_type}")'
            enabled_body.extend(_compile_assignment_lines(target, value_expr, ctx, indent=0))
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)


def _compile_enabled_function_call_instruction(
    instr: EnabledFunctionCallInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    fn_symbol = ctx.register_function_source(instr._fn)
    result_var = f"_{ctx.next_name('fn_result')}"
    kwargs = ", ".join(
        f"{name}={_compile_value(value, ctx)}" for name, value in sorted(instr._ins.items())
    )
    call_args = enabled_expr
    if kwargs:
        call_args += f", {kwargs}"
    lines = [f"{' ' * indent}{result_var} = {fn_symbol}({call_args})"]
    if instr._outs:
        fn_name = getattr(instr._fn, "__name__", type(instr._fn).__name__)
        ctx.mark_helper("_store_copy_value_to_type")
        lines.extend(
            [
                f"{' ' * indent}if {result_var} is None:",
                f'{" " * (indent + 4)}raise TypeError("run_enabled_function: {fn_name!r} returned None but outs were declared")',
            ]
        )
        for key, target in sorted(instr._outs.items()):
            target_type = _value_type_name(target)
            lines.extend(
                [
                    f"{' ' * indent}if {key!r} not in {result_var}:",
                    f"{' ' * (indent + 4)}raise KeyError(",
                    f'{" " * (indent + 8)}f"run_enabled_function: {fn_name!r} missing key {key!r}; got {{sorted({result_var})}}"',
                    f"{' ' * (indent + 4)})",
                ]
            )
            value_expr = f'_store_copy_value_to_type({result_var}[{key!r}], "{target_type}")'
            lines.extend(_compile_assignment_lines(target, value_expr, ctx, indent=indent))
    return lines


def _compile_for_loop_instruction(
    instr: ForLoopInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    count_expr = _compile_value(instr.count, ctx)
    idx_symbol = ctx.symbol_for_tag(instr.idx_tag)
    disabled_children = _compile_instruction_list(instr.instructions, "False", ctx, indent=0)
    enabled_children = _compile_instruction_list(instr.instructions, "True", ctx, indent=0)
    body = [
        f"_iterations = max(0, int({count_expr}))",
        "for _for_i in range(_iterations):",
        f"    {idx_symbol} = _for_i",
        *_indent_body(enabled_children, 4),
    ]
    return _compile_guarded_instruction(
        instr,
        enabled_expr,
        ctx,
        indent,
        body,
        disabled_body=disabled_children,
    )


def _indent_body(lines: list[str], spaces: int) -> list[str]:
    prefix = " " * spaces
    return [f"{prefix}{line}" if line else line for line in lines]


def _bool_literal(expr: str) -> bool | None:
    stripped = expr.strip()
    if stripped == "True":
        return True
    if stripped == "False":
        return False
    return None


def _compile_instruction_list(
    instructions: list[Any],
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    lines: list[str] = []
    for instruction in instructions:
        lines.extend(compile_instruction(instruction, enabled_expr, ctx, indent))
    return lines


def _copy_modifier_target_info(
    target: Tag | IndirectRef | IndirectExprRef,
    ctx: CodegenContext,
    stem: str,
) -> tuple[list[str], str, str, str | None, str]:
    if isinstance(target, Tag):
        block_info = ctx.tag_block_addresses.get(target.name)
        if block_info is None:
            return [], "scalar", ctx.symbol_for_tag(target), None, target.type.name
        block_id, addr = block_info
        binding = ctx.block_bindings.get(block_id)
        if binding is None:
            raise RuntimeError(f"Missing block binding for tag-backed target {target.name!r}")
        symbol = ctx.symbol_for_block(binding.block)
        start_var = f"_{stem}_start_idx"
        return [f"{start_var} = {addr - binding.start}"], "block", symbol, start_var, target.type.name

    if isinstance(target, IndirectRef):
        binding = ctx.block_bindings.get(id(target.block))
        if binding is None:
            raise RuntimeError(f"Missing block binding for indirect target {target.block.name!r}")
        symbol = ctx.symbol_for_block(target.block)
        helper = ctx.use_indirect_block(binding.block_id)
        start_var = f"_{stem}_start_idx"
        ptr = _compile_value(target.pointer, ctx)
        return [f"{start_var} = {helper}(int({ptr}))"], "block", symbol, start_var, binding.tag_type.name

    if isinstance(target, IndirectExprRef):
        binding = ctx.block_bindings.get(id(target.block))
        if binding is None:
            raise RuntimeError(
                f"Missing block binding for indirect expression target {target.block.name!r}"
            )
        symbol = ctx.symbol_for_block(target.block)
        helper = ctx.use_indirect_block(binding.block_id)
        start_var = f"_{stem}_start_idx"
        expr = compile_expression(target.expr, ctx)
        return (
            [f"{start_var} = {helper}(int({expr}))"],
            "block",
            symbol,
            start_var,
            binding.tag_type.name,
        )

    raise TypeError(f"Unsupported copy modifier target type: {type(target).__name__}")


def _copy_modifier_write_lines(
    *,
    values_var: str,
    target_kind: str,
    target_symbol: str,
    target_start_var: str | None,
    fault_body: list[str],
) -> list[str]:
    if target_kind == "scalar":
        return [
            f"if len({values_var}) > 1:",
            *_indent_body(fault_body, 4),
            f"elif len({values_var}) == 1:",
            f"    {target_symbol} = {values_var}[0]",
        ]

    if target_start_var is None:
        raise RuntimeError("copy modifier block target is missing start index")
    return [
        f"_copy_count = len({values_var})",
        "if _copy_count == 0:",
        "    pass",
        f"elif ({target_start_var} < 0) or (({target_start_var} + _copy_count) > len({target_symbol})):",
        *_indent_body(fault_body, 4),
        "else:",
        f"    for _copy_offset, _copy_value in enumerate({values_var}):",
        f"        {target_symbol}[{target_start_var} + _copy_offset] = _copy_value",
    ]


def _optional_value_type_name(value: Any) -> str | None:
    if isinstance(value, Tag):
        return value.type.name
    if isinstance(value, (IndirectRef, IndirectExprRef)):
        return value.block.type.name
    return None


def _optional_range_type_name(range_value: Any) -> str | None:
    if isinstance(range_value, (BlockRange, IndirectBlockRange)):
        return range_value.block.type.name
    return None


def _value_type_name(value: Any) -> str:
    if isinstance(value, Tag):
        return value.type.name
    if isinstance(value, (IndirectRef, IndirectExprRef)):
        return value.block.type.name
    raise TypeError(f"Unsupported typed value target: {type(value).__name__}")


def _range_type_name(range_value: Any) -> str:
    if isinstance(range_value, (BlockRange, IndirectBlockRange)):
        return range_value.block.type.name
    raise TypeError(f"Expected BlockRange or IndirectBlockRange, got {type(range_value).__name__}")


def _range_reverse(range_value: Any) -> bool:
    if isinstance(range_value, (BlockRange, IndirectBlockRange)):
        return bool(range_value.reverse_order)
    return False


def _compile_assignment_lines(
    target: Tag | IndirectRef | IndirectExprRef,
    value_expr: str,
    ctx: CodegenContext,
    *,
    indent: int,
) -> list[str]:
    lvalue = _compile_lvalue(target, ctx)
    return [f"{' ' * indent}{lvalue} = {value_expr}"]


def _compile_lvalue(target: Tag | IndirectRef | IndirectExprRef, ctx: CodegenContext) -> str:
    if isinstance(target, Tag):
        return ctx.symbol_for_tag(target)
    if isinstance(target, IndirectRef):
        block_id = id(target.block)
        binding = ctx.block_bindings.get(block_id)
        if binding is None:
            raise RuntimeError(f"Missing block binding for indirect target {target.block.name!r}")
        block_symbol = ctx.symbol_for_block(target.block)
        helper = ctx.use_indirect_block(binding.block_id)
        ptr = _compile_value(target.pointer, ctx)
        return f"{block_symbol}[{helper}(int({ptr}))]"
    if isinstance(target, IndirectExprRef):
        block_id = id(target.block)
        binding = ctx.block_bindings.get(block_id)
        if binding is None:
            raise RuntimeError(
                f"Missing block binding for indirect expression target {target.block.name!r}"
            )
        block_symbol = ctx.symbol_for_block(target.block)
        helper = ctx.use_indirect_block(binding.block_id)
        expr = compile_expression(target.expr, ctx)
        return f"{block_symbol}[{helper}(int({expr}))]"
    raise TypeError(f"Unsupported assignment target: {type(target).__name__}")


def _compile_range_setup(
    range_value: Any,
    ctx: CodegenContext,
    *,
    stem: str,
    include_addresses: bool,
) -> tuple[list[str], str, str, str]:
    if not isinstance(range_value, (BlockRange, IndirectBlockRange)):
        raise TypeError(
            f"Expected BlockRange or IndirectBlockRange, got {type(range_value).__name__}"
        )
    binding = ctx.block_bindings[id(range_value.block)]
    symbol = ctx.symbol_for_block(range_value.block)
    if isinstance(range_value, BlockRange):
        addresses = [int(addr) for addr in range_value.addresses]
        indices = [addr - binding.start for addr in addresses]
        indices_expr = _sequence_expr(indices)
        addrs_expr = _sequence_expr(addresses)
        return [], symbol, indices_expr, addrs_expr

    helper = ctx.use_indirect_block(binding.block_id)
    name = ctx.next_name(stem)
    start_var = f"_{name}_start"
    end_var = f"_{name}_end"
    addr_var = f"_{name}_addr"
    idx_var = f"_{name}_idx"
    indices_var = f"_{name}_indices"
    addrs_var = f"_{name}_addrs"
    start_expr = _compile_address_expr(range_value.start_expr, ctx)
    end_expr = _compile_address_expr(range_value.end_expr, ctx)
    lines = [
        f"{start_var} = int({start_expr})",
        f"{end_var} = int({end_expr})",
        f"if {start_var} > {end_var}:",
        '    raise ValueError("Indirect range start must be <= end")',
        f"{indices_var} = []",
        f"{addrs_var} = []",
        f"for {addr_var} in range({start_var}, {end_var} + 1):",
        f"    {idx_var} = {helper}(int({addr_var}))",
        f"    {indices_var}.append({idx_var})",
        f"    {addrs_var}.append(int({addr_var}))",
    ]
    if range_value.reverse_order:
        lines.extend([f"{indices_var}.reverse()", f"{addrs_var}.reverse()"])
    return lines, symbol, indices_var, addrs_var


def _sequence_expr(values: list[int]) -> str:
    if not values:
        return "[]"
    if len(values) == 1:
        return repr(range(values[0], values[0] + 1))

    step = values[1] - values[0]
    if step != 0 and all(values[i + 1] - values[i] == step for i in range(len(values) - 1)):
        return repr(range(values[0], values[-1] + step, step))
    return repr(values)


def _search_compare_expr(condition: str, left_expr: str, right_expr: str) -> str:
    if condition == "==":
        return f"({left_expr} == {right_expr})"
    if condition == "!=":
        return f"({left_expr} != {right_expr})"
    if condition == "<":
        return f"({left_expr} < {right_expr})"
    if condition == "<=":
        return f"({left_expr} <= {right_expr})"
    if condition == ">":
        return f"({left_expr} > {right_expr})"
    if condition == ">=":
        return f"({left_expr} >= {right_expr})"
    raise ValueError(f"Unsupported search comparison: {condition!r}")


def _pack_store_expr(value_expr: str, dest_type: str, ctx: CodegenContext) -> str:
    if dest_type == "REAL":
        ctx.mark_helper("_int_to_float_bits")
        return f"_int_to_float_bits({value_expr})"
    if dest_type == "INT":
        ctx.mark_helper("_wrap_int")
        return f"_wrap_int(int({value_expr}), 16, True)"
    if dest_type == "DINT":
        ctx.mark_helper("_wrap_int")
        return f"_wrap_int(int({value_expr}), 32, True)"
    if dest_type == "WORD":
        return f"(int({value_expr}) & 0xFFFF)"
    raise TypeError(f"Unsupported pack destination type: {dest_type}")


def _calc_store_expr(value_expr: str, dest_type: str, mode: str, ctx: CodegenContext) -> str:
    if mode == "hex":
        return f"(int({value_expr}) & 0xFFFF)"
    if dest_type == "BOOL":
        return f"bool({value_expr})"
    if dest_type == "REAL":
        return f"float({value_expr})"
    if dest_type == "CHAR":
        return value_expr
    if dest_type == "WORD":
        return f"(int({value_expr}) & 0xFFFF)"
    if dest_type == "INT":
        ctx.mark_helper("_wrap_int")
        return f"_wrap_int(int({value_expr}), 16, True)"
    if dest_type == "DINT":
        ctx.mark_helper("_wrap_int")
        return f"_wrap_int(int({value_expr}), 32, True)"
    return value_expr


def _timer_dt_to_units_expr(unit: TimeUnit, dt_expr: str, frac_expr: str) -> str:
    if unit == TimeUnit.Tms:
        return f"(({dt_expr} * 1000.0) + {frac_expr})"
    if unit == TimeUnit.Ts:
        return f"(({dt_expr}) + {frac_expr})"
    if unit == TimeUnit.Tm:
        return f"(({dt_expr} / 60.0) + {frac_expr})"
    if unit == TimeUnit.Th:
        return f"(({dt_expr} / 3600.0) + {frac_expr})"
    if unit == TimeUnit.Td:
        return f"(({dt_expr} / 86400.0) + {frac_expr})"
    raise ValueError(f"Unsupported timer unit: {unit}")


def _load_cast_expr(value_expr: str, tag_type: str) -> str:
    if tag_type == "BOOL":
        return f"bool({value_expr})"
    if tag_type == "INT":
        return f"max({_INT_MIN}, min({_INT_MAX}, int({value_expr})))"
    if tag_type == "DINT":
        return f"max({_DINT_MIN}, min({_DINT_MAX}, int({value_expr})))"
    if tag_type == "WORD":
        return f"(int({value_expr}) & 0xFFFF)"
    if tag_type == "REAL":
        return f"float({value_expr})"
    if tag_type == "CHAR":
        return f"({value_expr} if isinstance({value_expr}, str) else '')"
    return value_expr


def _first_defined_name(source: str) -> str | None:
    match = re.search(r"^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", source, flags=re.MULTILINE)
    if match is not None:
        return match.group(1)
    match = re.search(
        r"^\s*async\s+def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", source, flags=re.MULTILINE
    )
    if match is not None:
        return match.group(1)
    return None


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
            raise RuntimeError(
                f"Missing block binding for indirect expression ref {value.block.name!r}"
            )
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
    return f"{' ' * indent}global {', '.join(symbols)}"


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
