"""CircuitPython code generation (feature-complete v1)."""

from __future__ import annotations

import builtins
import hashlib
import math
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from fractions import Fraction
from typing import Any

from pyrung.circuitpy.codegen._constants import (
    _BOARD_LED_TAG,
    _BOARD_NEOPIXEL_B_TAG,
    _BOARD_NEOPIXEL_G_TAG,
    _BOARD_NEOPIXEL_R_TAG,
    _BOARD_SAVE_MEMORY_CMD_TAG,
    _BOARD_SWITCH_TAG,
    _TYPE_DEFAULTS,
)
from pyrung.circuitpy.codegen._util import (
    _default_literal,
    _first_defined_name,
    _io_kind,
    _mangle_symbol,
)
from pyrung.circuitpy.hardware import P1AM
from pyrung.circuitpy.modbus import ModbusClientConfig, ModbusServerConfig
from pyrung.circuitpy.p1am import RunStopConfig
from pyrung.core.function_source import embedded_source, is_the_function
from pyrung.core.instruction.data_transfer import CopyInstruction
from pyrung.core.memory_block import (
    UNSET,
    Block,
    BlockRange,
    IndirectBlockRange,
    IndirectExprRef,
    IndirectRef,
    InputBlock,
    OutputBlock,
)
from pyrung.core.program import Program
from pyrung.core.system_points import system
from pyrung.core.tag import ImmediateRef, Tag, TagType
from pyrung.core.validation.walker import walk_rung_operands


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


@dataclass(frozen=True)
class StaticRangeLayout:
    """The ordered semantic tags and backing addresses of one static range."""

    tags: tuple[Tag, ...]
    addresses: tuple[int, ...]
    uses_backing_storage: bool


def _name_for_the_lock(fn: Any) -> str:
    """What a lock sees of a callable with no source: its names, and no address."""
    kind = type(fn)
    return f"{kind.__module__}.{kind.__qualname__} {getattr(fn, '__qualname__', '')}".rstrip()


def _text_for_the_lock(value: Any, seen: dict[int, int] | None = None) -> str:
    """What a lock sees of a constant: its value wherever text says it, the same in any process.

    A kernel holds the constant itself, so this is read and never run, and it can say what
    ``repr`` alone wouldn't: a set's items in order, a list held twice.  What has no text
    gets its names.
    """
    kind = type(value)
    if value is None or kind in (bool, int, float, complex, str, bytes, bytearray, range):
        return repr(value)
    if kind in (Decimal, Fraction):
        return repr(value)
    if isinstance(value, Enum):
        return f"{kind.__qualname__}.{value.name}"
    if isinstance(value, Tag):
        return f"{kind.__qualname__}({value.name!r})"
    if kind in (set, frozenset):
        # Their items are hashable, so nothing in one can be seen elsewhere as a list.
        items = ", ".join(sorted(_text_for_the_lock(item) for item in value))
        return f"{kind.__name__}({{{items}}})"
    if kind not in (list, tuple, dict):
        return _name_for_the_lock(value)
    seen = {} if seen is None else seen
    if kind is tuple:
        return "(" + "".join(f"{_text_for_the_lock(item, seen)}, " for item in value) + ")"
    if id(value) in seen:
        return f"<as {seen[id(value)]}>"
    seen[id(value)] = len(seen)
    if kind is list:
        return "[" + ", ".join(_text_for_the_lock(item, seen) for item in value) + "]"
    pairs = (
        f"{_text_for_the_lock(key, seen)}: {_text_for_the_lock(item, seen)}"
        for key, item in value.items()
    )
    return "{" + ", ".join(pairs) + "}"


def _is_plain_literal(value: Any) -> bool:
    """Whether ``repr(value)`` is *value*, and no call can tell one copy from another."""
    kind = type(value)
    if kind is float:
        return math.isfinite(value)
    if kind is tuple:
        return all(_is_plain_literal(item) for item in value)
    return value is None or kind in (bool, int, str, bytes)


def _renders_faithfully(value: Any, seen: set[int] | None = None) -> bool:
    """Whether ``repr(value)`` rebuilds *value*: one object, sharing nothing inside itself."""
    if _is_plain_literal(value):
        return True
    kind = type(value)
    if kind not in (list, tuple, dict):
        return False
    seen = set() if seen is None else seen
    if id(value) in seen:
        return False
    seen.add(id(value))
    items = [*value.keys(), *value.values()] if kind is dict else value
    return all(_renders_faithfully(item, seen) for item in items)


def _mutables_inside(value: Any) -> Iterator[Any]:
    """Each list and dict a constant holds, itself included: what a call can change."""
    kind = type(value)
    if kind not in (list, tuple, dict):
        return
    if kind is not tuple:
        yield value
    for item in [*value.keys(), *value.values()] if kind is dict else value:
        yield from _mutables_inside(item)


def describe_static_range(range_value: BlockRange) -> StaticRangeLayout:
    """Return the semantic elements and physical span of a static range."""

    addresses = tuple(int(addr) for addr in range_value.addresses)
    tags = tuple(range_value.tags())
    uses_backing_storage = len(tags) == len(addresses) and all(
        tag is range_value.block._get_tag(addr) for tag, addr in zip(tags, addresses, strict=True)
    )
    return StaticRangeLayout(
        tags=tags,
        addresses=addresses,
        uses_backing_storage=uses_backing_storage,
    )


@dataclass(frozen=True)
class ModbusClientSymbolSpec:
    symbol: str
    owner: str
    tag_type: str | None = None


@dataclass(frozen=True)
class ModbusClientJobSpec:
    var_name: str
    kind: str
    target_name: str
    bank: str | None
    plc_start: int
    modbus_start: int
    modbus_quantity: int
    function_code: int
    item_count: int
    items: tuple[ModbusClientSymbolSpec, ...]
    busy: ModbusClientSymbolSpec
    success: ModbusClientSymbolSpec
    error: ModbusClientSymbolSpec
    exception_response: ModbusClientSymbolSpec
    is_coil: bool = False
    word_order: str | None = None


@dataclass
class CodegenContext:
    program: Program
    hw: P1AM
    target_scan_ms: float
    watchdog_ms: int | None
    force_rung_enable: bool = False
    blockless: bool = False
    kernel_runtime: bool = False
    proof_metadata: bool = False
    # Prover-only: every timed tick becomes a choice (see render_kernel
    # ``_BB_TICK_HELPER``) instead of advancing by the scan's dt.
    abstract_timers: bool = False
    modbus_server: ModbusServerConfig | None = None
    modbus_client: ModbusClientConfig | None = None
    tag_map: Any = None

    @classmethod
    def for_kernel(
        cls,
        program: Program,
        *,
        force_rung_enable: bool = False,
        blockless: bool = False,
        proof_metadata: bool = False,
        abstract_timers: bool = False,
    ) -> CodegenContext:
        """Create a hardware-free context for kernel compilation."""
        ctx = cls(
            program=program,
            hw=P1AM(),
            target_scan_ms=0.0,
            watchdog_ms=None,
            force_rung_enable=force_rung_enable,
            blockless=blockless,
            kernel_runtime=True,
            proof_metadata=proof_metadata,
            abstract_timers=abstract_timers,
        )
        ctx.collect_program_references()
        ctx.resolve_block_aliases()
        ctx.assign_symbols()
        ctx.build_compact_block_maps()
        return ctx

    slot_bindings: list[SlotBinding] = field(default_factory=list)
    # A proof_metadata kernel's capped For loops: (where, count, tags), by index.
    for_cap_sites: list[tuple[str, str, tuple[str, ...]]] = field(default_factory=list)
    block_bindings: dict[int, BlockBinding] = field(default_factory=dict)
    scalar_tags: dict[str, Tag] = field(default_factory=dict)
    referenced_tags: dict[str, Tag] = field(default_factory=dict)
    retentive_tags: dict[str, Tag] = field(default_factory=dict)
    dict_backed_tag_names: set[str] = field(default_factory=set)

    subroutine_names: list[str] = field(default_factory=list)
    function_sources: dict[str, str] = field(default_factory=dict)
    function_globals: dict[str, set[str]] = field(default_factory=dict)
    used_helpers: set[str] = field(default_factory=set)
    # Nanoseconds to a unit of each accumulator the compiled timers tick.
    timer_unit_ns: dict[str, int] = field(default_factory=dict)
    symbol_table: dict[str, str] = field(default_factory=dict)

    block_symbols: dict[int, str] = field(default_factory=dict)
    tag_block_addresses: dict[str, tuple[int, int]] = field(default_factory=dict)
    used_indirect_blocks: set[int] = field(default_factory=set)
    # Tags a Copy writes several elements from (a text, its digits): their block is
    # never compacted, outside blockless mode.
    multi_element_store_targets: set[str] = field(default_factory=set)
    # Blocks a rung condition indexes: their ``_cond_index_*`` helper reads an
    # address outside the block as 0 rather than raising.
    used_condition_index_blocks: set[int] = field(default_factory=set)
    used_read_index_blocks: set[int] = field(default_factory=set)
    used_window_addrs_blocks: set[int] = field(default_factory=set)
    # While a follow instruction's body compiles (``fault_follow_scope``): what
    # a source reading a followed flag reads, the flag as the instruction found
    # it, since its first line clears the flag.
    fault_entry_symbols: dict[str, str] = field(default_factory=dict)
    fault_entry_reads: set[str] = field(default_factory=set)
    # logical block id -> (bank block id, {logical addr: bank addr}). An aliased
    # block is a named window onto the bank's array, not storage of its own.
    block_alias: dict[int, tuple[int, dict[int, int]]] = field(default_factory=dict)
    function_symbols_by_obj: dict[int, str] = field(default_factory=dict)
    #: A kernel's globals: each symbol bound to the object itself.
    function_objects: dict[str, Any] = field(default_factory=dict)
    function_constants: dict[str, Any] = field(default_factory=dict)
    function_constant_symbols: dict[int, str] = field(default_factory=dict)
    #: A code.py's constants: each list and dict one holds -> the symbol that holds it.
    function_constant_owners: dict[int, str] = field(default_factory=dict)
    runstop: RunStopConfig | None = None
    board_tag_names: set[str] = field(default_factory=set)
    uses_board_switch: bool = False
    uses_board_led: bool = False
    uses_board_neopixel: bool = False
    uses_random: bool = False
    _current_function: str | None = None
    # Open forloop nesting while compiling, so nested loops get their own
    # counter names.
    _for_loop_depth: int = 0
    _name_counters: dict[str, int] = field(default_factory=dict)
    has_io_gaps: bool = False
    modbus_client_specs: list[ModbusClientJobSpec] = field(default_factory=list)
    modbus_client_specs_by_instruction: dict[int, ModbusClientJobSpec] = field(default_factory=dict)
    # The locals holding each box's helper legs, by ``id(instr)`` then field,
    # bound just before the box's code (see compile._core._bind_helper_legs).
    _helper_condition_snapshots: dict[int, dict[str, str | list[str]]] = field(default_factory=dict)
    # ``(memory key, edge condition)`` per row (a rung's trunk, branch or box,
    # a For-body rung or bare For-body box), by ``id``, for the rows being
    # compiled (see site_memory.edge_rows).
    _edge_rows: dict[int, tuple[tuple[str, Any], ...]] = field(default_factory=dict)
    compact_block_map: dict[int, dict[int, int]] = field(default_factory=dict)
    _static_range_layouts: dict[int, StaticRangeLayout] = field(default_factory=dict)
    _power_on_literals_by_block: dict[int, dict[int, str]] = field(default_factory=dict)
    _slot_addresses_by_block: dict[int, dict[str, int]] = field(default_factory=dict)

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

    def _note_multi_element_store(self, instr: Any) -> None:
        """Note the target of a Copy that writes elements from a fixed one on.

        A text and its digits go into the elements after the target, by address, so
        the block that target lives in keeps every address (``build_compact_block_maps``).
        """
        if not isinstance(instr, CopyInstruction):
            return
        target = instr.target
        if isinstance(target, ImmediateRef):
            target = target.value
        if not isinstance(target, Tag):
            return
        if instr.convert is not None or (isinstance(instr.source, str) and len(instr.source) > 1):
            self.multi_element_store_targets.add(target.name)

    def collect_program_references(self) -> None:
        self.referenced_tags.clear()
        self.multi_element_store_targets.clear()
        self.dict_backed_tag_names.clear()
        self._static_range_layouts.clear()
        self.subroutine_names = sorted(self.program.subroutines)

        def visit(value: Any) -> None:
            if isinstance(value, Tag):
                self.referenced_tags.setdefault(value.name, value)
                self._associate_tag_with_known_block(value)
            elif isinstance(value, IndirectRef | IndirectExprRef | IndirectBlockRange):
                self._ensure_block_binding(value.block)
                self.used_indirect_blocks.add(id(value.block))
            elif isinstance(value, BlockRange):
                layout = self.static_range_layout(value)
                if layout.uses_backing_storage:
                    self._ensure_block_binding(value.block)
                elif self.blockless:
                    names = {tag.name for tag in layout.tags}
                    self.dict_backed_tag_names.update(names)
                    for name in names:
                        self.tag_block_addresses.pop(name, None)
                for tag in layout.tags:
                    self.referenced_tags.setdefault(tag.name, tag)
                    if not layout.uses_backing_storage and not self.blockless:
                        annotated_block = getattr(tag, "_pyrung_block", None)
                        if annotated_block is not None:
                            self._ensure_block_binding(annotated_block)
                    if layout.uses_backing_storage or not self.blockless:
                        self._associate_tag_with_known_block(tag)

        walk_rung_operands(
            [
                *self.program.rungs,
                *(
                    rung
                    for sub_name in self.subroutine_names
                    for rung in self.program.subroutines[sub_name]
                ),
            ],
            visit,
            on_instruction=self._note_multi_element_store,
        )
        for fault_tag in (
            system.fault.division_error,
            system.fault.out_of_range,
            system.fault.address_error,
            system.fault.math_operation_error,
        ):
            self.referenced_tags.setdefault(fault_tag.name, fault_tag)
        # The walk meets a tag before it may meet the block holding it.
        for tag in list(self.referenced_tags.values()):
            self._associate_tag_with_known_block(tag)
        self._refresh_board_usage()

    def collect_retentive_tags(self) -> None:
        self.retentive_tags = {
            name: self.referenced_tags[name]
            for name in sorted(self.referenced_tags)
            if self.referenced_tags[name].retentive
        }

    def resolve_block_aliases(self) -> None:
        """Fold blocks mapped onto a hardware bank into the bank's storage.

        ``Block.map_to`` makes the bank the owner of each mapped register, so the
        logical block and the bank name the *same* tag.  Without this pass both
        would be emitted as separate arrays and the tag would live in two cells —
        the split that makes an indirect ``bank[expr]`` read a blank 0.

        Only folds into a bank the program already uses as storage.  A P1AM build
        maps logical blocks onto Click banks purely to name Modbus addresses — the
        bank is an address space there, not memory, and must not be materialized.
        A block used indirectly also keeps its own storage: redirecting it would
        mean translating its pointer arithmetic into the bank's address space.
        """
        self.block_alias.clear()
        for block_id in sorted(self.block_bindings):
            binding = self.block_bindings[block_id]
            alias = getattr(binding.block, "_hw_alias", None)
            if not alias:
                continue

            bank_ids = {id(bank) for bank, _ in alias.values()}
            if len(bank_ids) != 1:
                continue
            bank_id = next(iter(bank_ids))
            if bank_id == block_id or bank_id not in self.block_bindings:
                continue

            if block_id in self.used_indirect_blocks:
                bank = self.block_bindings[bank_id]
                raise ValueError(
                    f"Block {binding.logical_name!r} is mapped onto bank "
                    f"{bank.logical_name!r} and both are addressed indirectly. "
                    f"The mapped registers are one memory, but indirect access "
                    f"through each would compile to separate arrays. Address them "
                    f"indirectly through one of the two, not both."
                )

            self.block_alias[block_id] = (
                bank_id,
                {logical: hw for logical, (_bank, hw) in alias.items()},
            )

        # Every tag now lives at its bank address; retarget the logical claims so
        # both access paths agree on one cell.
        for tag_name, (block_id, addr) in list(self.tag_block_addresses.items()):
            aliased = self.block_alias.get(block_id)
            if aliased is None:
                continue
            bank_id, addr_map = aliased
            hw_addr = addr_map.get(addr)
            if hw_addr is not None:
                self.tag_block_addresses[tag_name] = (bank_id, hw_addr)

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
            if binding.block_id in self.block_alias:
                continue
            key = f"block:{binding.logical_name}:{binding.block_id}"
            symbol = _mangle_symbol(binding.logical_name, "_b_", used)
            self.symbol_table[key] = symbol
            self.block_symbols[binding.block_id] = symbol

        # An aliased block borrows the bank's array symbol — same storage.
        for block_id, (bank_id, _addr_map) in self.block_alias.items():
            self.block_symbols[block_id] = self.block_symbols[bank_id]

        for tag_name in sorted(self.referenced_tags):
            if tag_name in self.tag_block_addresses or tag_name in self.dict_backed_tag_names:
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

    def emitted_bindings(self) -> list[BlockBinding]:
        """Bindings that own storage — aliased blocks live in their bank's array."""
        return [
            binding
            for binding in self.block_bindings.values()
            if binding.block_id not in self.block_alias
        ]

    def static_range_layout(self, range_value: BlockRange) -> StaticRangeLayout:
        """Describe a static range without confusing its address span with its tags.

        Most ranges name every slot in one backing block.  Specialized ranges
        may instead expose an ordered semantic tag list over that span (for
        example, a named-array instance selection skips stride padding and
        interleaves fields).  Code generation must preserve that tag identity.
        """

        range_id = id(range_value)
        layout = self._static_range_layouts.get(range_id)
        if layout is None:
            layout = describe_static_range(range_value)
            self._static_range_layouts[range_id] = layout
        return layout

    def build_compact_block_maps(self) -> None:
        """Build compact index mappings for blocks with only static access."""
        self.compact_block_map = {}
        # A block a multi-element store writes into keeps every address, for the
        # elements after its target.  Blockless code stores those by name, and its
        # template holds only the tags the program names, so it compacts as before.
        # ``tag_block_addresses`` is alias-resolved: the block the slot is in.
        whole = (
            set()
            if self.blockless
            else {
                self.tag_block_addresses[name][0]
                for name in self.multi_element_store_targets
                if name in self.tag_block_addresses
            }
        )
        for block_id in self.block_bindings:
            if (
                block_id in self.used_indirect_blocks
                or block_id in self.block_alias
                or block_id in whole
            ):
                continue
            addrs = sorted(
                addr
                for _tag_name, (bid, addr) in self.tag_block_addresses.items()
                if bid == block_id
            )
            self.compact_block_map[block_id] = {addr: i for i, addr in enumerate(addrs)}

    def block_index(self, block_id: int, addr: int) -> int:
        aliased = self.block_alias.get(block_id)
        if aliased is not None:
            bank_id, addr_map = aliased
            return self.block_index(bank_id, addr_map[addr])
        compact = self.compact_block_map.get(block_id)
        if compact is not None:
            return compact[addr]
        return addr - self.block_bindings[block_id].start

    def global_symbol_for_tag(self, tag: Tag | ImmediateRef) -> str:
        if isinstance(tag, ImmediateRef):
            tag = tag.tag
        block_info = self.tag_block_addresses.get(tag.name)
        if block_info is not None:
            block_id, _addr = block_info
            symbol = self.block_symbols.get(block_id)
            if symbol is None:
                raise RuntimeError(f"Missing block symbol for tag {tag.name!r}")
            return symbol

        symbol = self.symbol_table.get(tag.name)
        if symbol is None:
            raise RuntimeError(f"Missing scalar symbol for tag {tag.name!r}")
        return symbol

    def symbol_for_tag(self, tag: Tag | ImmediateRef) -> str:
        if isinstance(tag, ImmediateRef):
            tag = tag.tag
        block_info = self.tag_block_addresses.get(tag.name)
        if block_info is not None:
            block_id, addr = block_info
            binding = self.block_bindings.get(block_id)
            symbol = self.block_symbols.get(block_id)
            if binding is None or symbol is None:
                raise RuntimeError(f"Missing block binding for tag {tag.name!r}")
            index = self.block_index(block_id, addr)
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

    def block_layout_tag_names(self, block_id: int) -> tuple[str, ...]:
        binding = self.block_bindings.get(block_id)
        if binding is None:
            raise RuntimeError(f"Missing block binding for block id {block_id}")
        compact = self.compact_block_map.get(block_id)
        if compact is not None:
            addresses = sorted(compact)
        else:
            addresses = list(range(binding.start, binding.end + 1))
        return tuple(binding.block._get_tag(addr).name for addr in addresses)

    def block_name_tuple_symbol(self, block_id: int) -> str:
        symbol = self.block_symbols.get(block_id)
        if symbol is None:
            raise RuntimeError(f"Missing block symbol for block id {block_id}")
        name = f"{symbol}_names"
        if self._current_function is not None:
            self.mark_function_global(self._current_function, name)
        return name

    def next_name(self, prefix: str) -> str:
        n = self._name_counters.get(prefix, 0) + 1
        self._name_counters[prefix] = n
        return f"{prefix}_{n}"

    def state_key_for(self, obj: Any) -> str:
        """The site key ``bind_sites`` gave *obj*; raises for an unbound object.

        Generated code keys instruction memory by site, so a key handed out
        here (a counter or ``id()``) could collide with the program's own
        ``i{N}`` keys or differ from the interpreter's.
        """
        assigned = getattr(obj, "_state_key", None)
        if assigned is None:
            raise RuntimeError(
                f"{type(obj).__name__} has no site key: it was never bound into a program "
                "(pyrung.core.site_memory.bind_sites)."
            )
        return assigned

    def index_helper_name(self, block_id: int) -> str:
        symbol = self.block_symbols.get(block_id)
        if symbol is None:
            raise RuntimeError(f"Missing block symbol for block id {block_id}")
        name = symbol.lstrip("_") or symbol
        return f"_resolve_index_{name}"

    def use_indirect_block(self, block_id: int) -> str:
        self.used_indirect_blocks.add(block_id)
        return self.index_helper_name(block_id)

    def condition_index_helper_name(self, block_id: int) -> str:
        return self.index_helper_name(block_id).replace("_resolve_index_", "_cond_index_", 1)

    def use_condition_index_block(self, block_id: int) -> str:
        """The helper a condition indexes *block_id* through: -1 when out of range."""
        self.used_indirect_blocks.add(block_id)
        self.used_condition_index_blocks.add(block_id)
        return self.condition_index_helper_name(block_id)

    def read_index_helper_name(self, block_id: int) -> str:
        return self.index_helper_name(block_id).replace("_resolve_index_", "_read_index_", 1)

    def use_read_index_block(self, block_id: int) -> str:
        """The helper an instruction's source indexes *block_id* through."""
        self.used_indirect_blocks.add(block_id)
        self.used_read_index_blocks.add(block_id)
        return self.read_index_helper_name(block_id)

    def window_addrs_helper_name(self, block_id: int) -> str:
        return self.index_helper_name(block_id).replace("_resolve_index_", "_window_addrs_", 1)

    def use_window_addrs_block(self, block_id: int) -> str:
        """The helper a dynamic window (``block.select(S, E)``) gets its addresses from."""
        self.used_indirect_blocks.add(block_id)
        self.used_window_addrs_blocks.add(block_id)
        return self.window_addrs_helper_name(block_id)

    def symbol_if_referenced(self, tag_name: str) -> str | None:
        tag = self.referenced_tags.get(tag_name)
        if tag is None:
            return None
        return self.symbol_for_tag(tag)

    def ensure_tag_referenced(self, tag: Tag) -> None:
        self.referenced_tags.setdefault(tag.name, tag)
        self._associate_tag_with_known_block(tag)
        self._refresh_board_usage()

    def register_function_source(self, fn: Any) -> str:
        key = id(fn)
        existing = self.function_symbols_by_obj.get(key)
        if existing is not None:
            return existing
        if self.kernel_runtime:
            return self._call_by_reference(fn)

        try:
            rendered = embedded_source(fn)
        except (OSError, TypeError) as exc:
            raise ValueError(f"Could not inspect source for callable {fn!r}") from exc
        if not rendered:
            raise ValueError(f"Could not inspect source for callable {fn!r}")
        defined = _first_defined_name(rendered)
        if defined is None:
            raise ValueError(f"Callable source is not inspectable for embedding: {fn!r}")
        if not is_the_function(rendered, fn):
            raise ValueError(
                f"Callable source is not the function it stands for: {fn!r}. Embedding copies "
                "the text of a plain def: the file may have changed since the import, or the "
                "function uses a closure, a decorator or a wrapper."
            )
        if defined == "math" or hasattr(builtins, defined):
            # The def lands in the generated module, over the name its other code calls.
            raise ValueError(
                f"Callable name would shadow a name the generated code uses: {fn!r}. "
                "Rename the function."
            )

        fn_name = getattr(fn, "__name__", type(fn).__name__)
        symbol = _mangle_symbol(fn_name, "_fn_", set(self.function_sources))
        self.function_sources[symbol] = rendered
        self.function_symbols_by_obj[key] = symbol
        return symbol

    def function_constant(self, value: Any) -> str:
        """A ``run_function`` constant as generated code names it.

        Stepping hands the function one object, scan after scan, so what the function does
        to it lasts.  A plain literal's text will do.  Anything else a kernel holds by
        reference, and a code.py binds once at module level if its text rebuilds it.  Text
        can't say that two constants hold one list, so a code.py refuses that as well.
        """
        if _is_plain_literal(value):
            return repr(value)
        existing = self.function_constant_symbols.get(id(value))
        if existing is not None:
            return existing
        if not self.kernel_runtime and not _renders_faithfully(value):
            raise ValueError(
                f"A run_function constant a code.py can't rebuild from its text: {value!r}. "
                "Pass a number, a string, or a list or dict of them."
            )
        symbol = f"_fc_{len(self.function_constants)}"
        if not self.kernel_runtime:
            inside = {id(item) for item in _mutables_inside(value)}
            if any(item in self.function_constant_owners for item in inside):
                raise ValueError(
                    f"A run_function constant shares a list or dict with another: {value!r}. "
                    "A code.py rebuilds each from its text, so a change made through one "
                    "would not show in the other. Pass one object, or a copy to each."
                )
            self.function_constant_owners.update(dict.fromkeys(inside, symbol))
        self.function_constants[symbol] = value
        self.function_constant_symbols[id(value)] = symbol
        return symbol

    def function_constant_lines(self) -> list[str]:
        """Each constant's binding: a code.py runs these, a kernel's lock reads them."""
        return [
            f"{symbol} = {repr(value) if _renders_faithfully(value) else _text_for_the_lock(value)}"
            for symbol, value in self.function_constants.items()
        ]

    def _call_by_reference(self, fn: Any) -> str:
        """Bind *fn* itself to a symbol: a kernel lives in this process and calls the function.

        Its text is kept for the lock only, so a lock sees an edit to the function, and one
        with no text is known by name.  Whether that text is what *fn* runs doesn't matter
        here: a kernel takes any callable.  Whether it may be called is for the kernel's
        gate and the verifier's, ``calls_that_depend_on_more_than_their_inputs``.
        """
        fn_name = getattr(fn, "__name__", type(fn).__name__)
        symbol = _mangle_symbol(fn_name, "_fn_", set(self.function_sources))
        try:
            text = embedded_source(fn)
        except (OSError, SyntaxError, TypeError):
            text = ""
        self.function_sources[symbol] = text or f"# {_name_for_the_lock(fn)}"
        self.function_objects[symbol] = fn
        self.function_symbols_by_obj[id(fn)] = symbol
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
        return block_id

    def _slot_addresses(self, binding: BlockBinding) -> dict[str, int]:
        """The address of each slot of a block, by the name its slot resolves to.

        Memory is keyed by name, so a tag naming a slot is that slot however it
        got the name: ``map_to``, ``slot(name=...)`` or the same spelling.  Read
        from the slot configuration, not ``Block._tag_cache``: the cache holds
        whichever slots something has indexed so far, and an indirect read only
        indexes a slot when a scan reaches it.  What compiles must not depend on
        that, or a kernel compiled before the pointer arrives keeps the name and
        the slot in two cells.
        """
        addresses = self._slot_addresses_by_block.get(binding.block_id)
        if addresses is None:
            block = binding.block
            addresses = {}
            for addr in binding.valid_addresses or range(binding.start, binding.end + 1):
                # The same precedence as ``Block._get_tag``.
                mapped = block._mapped_tags.get(addr)
                name = mapped.name if mapped is not None else block._effective_slot_name(addr)
                addresses[name] = addr
            self._slot_addresses_by_block[binding.block_id] = addresses
        return addresses

    def _slot_holders(self, binding: BlockBinding) -> dict[int, Tag]:
        """The tag already known to hold each slot, by address, without indexing any.

        A name the program references wins: the engines seed the name, and a pointer
        reads the same cell.  Otherwise what `Block._get_tag` has cached or would
        return for a mapped tag.
        """
        holders: dict[int, Tag] = {**binding.block._mapped_tags, **binding.block._tag_cache}
        for name, (block_id, addr) in self.tag_block_addresses.items():
            if block_id == binding.block_id and name in self.referenced_tags:
                holders[addr] = self.referenced_tags[name]
        return holders

    def _factory_power_on_values(self, binding: BlockBinding, answered: set[int]) -> dict[int, Any]:
        """What a block's ``default_factory`` gives the slots nothing else answers for.

        The interpreter asks the factory once, when a scan first reaches a slot, and
        the cache keeps the answer.  Generated code has to know sooner, so the answer
        is only taken here from a factory that repeats it.  One that does not (a
        counter) has no power-on value to copy for a slot not reached yet: those stay
        at zero, and a slot already reached is in the cache and is not asked about.  A
        slot the factory raises for stays at zero too; the interpreter cannot read it.
        """
        factory = binding.block.default_factory
        if factory is None:
            return {}
        values: dict[int, Any] = {}
        for addr in binding.valid_addresses or range(binding.start, binding.end + 1):
            if addr in answered:
                continue
            try:
                value = factory(addr)
            except Exception:
                continue
            try:
                if factory(addr) != value:
                    return {}
            except Exception:
                return {}
            values[addr] = value
        return values

    def block_power_on_literals(self, binding: BlockBinding) -> dict[int, str]:
        """Source for the slots of a block that do not power on at the type's zero.

        A slot with no holder powers on as `Block._get_tag` would build it.  One no
        literal can spell stays at zero.
        """
        literals = self._power_on_literals_by_block.get(binding.block_id)
        if literals is not None:
            return literals
        block = binding.block
        holders = self._slot_holders(binding)
        configured = {
            addr: block._slot_field(addr, "default", UNSET) for addr in block._slot_config
        }
        configured = {addr: value for addr, value in configured.items() if value is not UNSET}
        values = self._factory_power_on_values(binding, {*holders, *configured})
        values.update(configured)
        values.update((addr, tag.default) for addr, tag in holders.items())
        zero = repr(_TYPE_DEFAULTS[binding.tag_type])
        literals = {}
        for addr in sorted(values):
            literal = _default_literal(values[addr])
            # By spelling: a Bool's False on an INT block is not the block's 0.
            if literal is not None and literal != zero:
                literals[addr] = literal
        self._power_on_literals_by_block[binding.block_id] = literals
        return literals

    def block_retentive_overrides(self, binding: BlockBinding) -> dict[int, bool]:
        """Each slot whose retentive flag may not be its block's, by address."""
        block = binding.block
        overrides = {
            addr: block._slot_field(addr, "retentive", block.retentive)
            for addr in block._slot_config
        }
        overrides.update((addr, tag.retentive) for addr, tag in self._slot_holders(binding).items())
        return overrides

    def _associate_tag_with_known_block(self, tag: Tag) -> None:
        if tag.name in self.tag_block_addresses or tag.name in self.dict_backed_tag_names:
            return
        if self.blockless:
            annotated_block = getattr(tag, "_pyrung_block", None)
            annotated_addr = getattr(tag, "_pyrung_block_addr", None)
            if annotated_block is not None and isinstance(annotated_addr, int):
                block_id = self._ensure_block_binding(annotated_block)
                if block_id is not None:
                    self.tag_block_addresses[tag.name] = (block_id, annotated_addr)
                    return
        for binding in sorted(
            self.block_bindings.values(), key=lambda b: (b.logical_name, b.block_id)
        ):
            addr = self._slot_addresses(binding).get(tag.name)
            if addr is not None:
                self.tag_block_addresses[tag.name] = (binding.block_id, addr)
                return

    def _refresh_board_usage(self) -> None:
        names = set(self.referenced_tags)
        self.board_tag_names = {
            name
            for name in names
            if name
            in {
                _BOARD_SWITCH_TAG,
                _BOARD_LED_TAG,
                _BOARD_NEOPIXEL_R_TAG,
                _BOARD_NEOPIXEL_G_TAG,
                _BOARD_NEOPIXEL_B_TAG,
                _BOARD_SAVE_MEMORY_CMD_TAG,
            }
        }
        self.uses_board_switch = _BOARD_SWITCH_TAG in names
        self.uses_board_led = _BOARD_LED_TAG in names
        self.uses_board_neopixel = bool(
            {_BOARD_NEOPIXEL_R_TAG, _BOARD_NEOPIXEL_G_TAG, _BOARD_NEOPIXEL_B_TAG} & names
        )
