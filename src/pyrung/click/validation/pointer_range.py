"""CLK_PTR_PAST_BANK: a pointer whose known values can run past the end of its bank."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyclickplc.banks import BANKS

from pyrung.core.analysis.value_sets import _scan, _ValueSets
from pyrung.core.analysis.write_sites import instruction_write_targets
from pyrung.core.expression import format_expr
from pyrung.core.memory_block import IndirectBlockRange, IndirectRef
from pyrung.core.tag import ImmediateRef
from pyrung.core.validation.walker import ProgramLocation, _instruction_fields

from .findings import CLK_PTR_PAST_BANK, ClickFinding, ValidationMode
from .resolve import _format_location, _instruction_location, _ResolutionCache

if TYPE_CHECKING:
    from pyrung.click.tag_map import TagMap
    from pyrung.core.program import Program


class _PointerBounds:
    """The addresses each pointer use can reach, from value_sets and the tags' declared values.

    A pointer the value sets leave unbounded keeps its declared ``choices`` or
    ``min``/``max``.  One with neither has no known bound and is never reported.
    """

    def __init__(self, program: Program) -> None:
        from pyrung.core.analysis.pdg import build_program_graph
        from pyrung.core.validation._common import walk_instructions

        graph = build_program_graph(program)
        unwritten = frozenset(
            name
            for name, tag in graph.tags.items()
            if name not in graph.writers_of and not tag.readonly
        )
        self._sets = _ValueSets(
            list(walk_instructions(program)),
            graph.tags,
            unwritten,
            None,
            follow_indirect_reads=True,
            declared_inputs=True,
            written_first=frozenset(
                name for name in graph.writers_of if graph.unconditional_write_before_read(name)
            ),
        )
        self._lookup: dict[str, frozenset[Any] | None] = {}
        for name, tag in graph.tags.items():
            known = self._sets.values(name)
            self._lookup[name] = known if known is not None else _ValueSets._declared(tag)

    def reach(self, ref: Any) -> frozenset[int] | None:
        """Every address *ref* can use, or None when its values are not bounded."""
        if isinstance(ref, IndirectBlockRange):
            starts = self._sets._integers(ref.start_expr, self._lookup)
            ends = self._sets._integers(ref.end_expr, self._lookup)
            if not starts or not ends:
                return None
            return starts | ends
        return self._sets._addresses_with(ref, self._lookup) or None


def _uses(instruction: Any) -> list[tuple[str, Any]]:
    """Each pointer an instruction's operands hold, with the field it is in."""
    found: list[tuple[str, Any]] = []
    seen: set[int] = set()
    for field in _instruction_fields(instruction) or ():
        _names, refs = _scan(getattr(instruction, field, None))
        for ref in refs:
            if id(ref) not in seen:
                seen.add(id(ref))
                found.append((field, ref))
    return found


def _is_write(instruction: Any, ref: Any) -> bool:
    for target in instruction_write_targets(instruction):
        while isinstance(target, ImmediateRef):
            target = target.value
        if target is ref:
            return True
    return False


def _evaluate_pointer_past_bank(
    instruction: Any,
    base_location: ProgramLocation,
    tag_map: TagMap,
    bounds: _PointerBounds,
    mode: ValidationMode,
    resolver: _ResolutionCache,
) -> list[ClickFinding]:
    """CLK_PTR_PAST_BANK: a pointer, index or indirect range whose known values can
    address memory past the end of its block's bank.

    A CLICK writes into the memory past the bank and pyrung drops the write (a read
    gives zero), so the program behaves differently on the two.  Below the first
    address both set the address error flag, so that is not reported.
    """
    findings: list[ClickFinding] = []
    for field, ref in _uses(instruction):
        block = ref.block
        memory_type = resolver.resolve_block_memory_type(block.name)
        bank = BANKS.get(memory_type) if memory_type else None
        if bank is None:
            continue
        entry = tag_map._block_entry_by_name(block.name)
        try:
            offset = 0 if entry is None else tag_map._offset_for(entry.logical)
        except (KeyError, ValueError):
            continue
        reached = bounds.reach(ref)
        if reached is None:
            continue
        past = sorted(addr for addr in reached if addr + offset > bank.max_addr)
        if not past:
            continue

        location_text = _format_location(
            _instruction_location(base_location, f"instruction.{field}")
        )
        if isinstance(ref, IndirectBlockRange):
            used = f"The indirect range on {block.name}"
            reaches = f"ends at {memory_type}{past[-1] + offset}"
        else:
            index = ref.pointer.name if isinstance(ref, IndirectRef) else format_expr(ref.expr)
            used = f"{block.name}[{index}]"
            reaches = f"can reach {memory_type}{past[0] + offset}"
        if type(instruction).__name__ == "CopyInstruction" and _is_write(instruction, ref):
            outcome = "a CLICK writes there and pyrung drops the write"
        else:
            outcome = "pyrung reads zero there and drops a write"
        findings.append(
            ClickFinding(
                code=CLK_PTR_PAST_BANK,
                severity="error" if mode == "strict" else "warning",
                message=(
                    f"{used} at {location_text} {reaches}, past "
                    f"{memory_type}{bank.max_addr} at the end of the bank: {outcome}."
                ),
                location=location_text,
                suggestion=(
                    f"Keep the pointer's values inside {memory_type}, with min and max or "
                    "choices on the tag, or clamp it before the instruction."
                ),
            )
        )
    return findings
