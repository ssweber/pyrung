"""CLK_PTR_PAST_BANK: a pointer whose known values can run past the end of its bank."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyclickplc.banks import BANKS

from pyrung.core.analysis.value_sets import _scan, _ValueSets
from pyrung.core.analysis.write_sites import instruction_write_targets
from pyrung.core.expression import format_expr
from pyrung.core.instruction.control import iter_loop_body_rungs
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
    A For loop with a constant count gives its index 0..count-1.  The value sets
    are built on the first pointer asked about, so a program with no pointer pays nothing.
    """

    def __init__(self, program: Program) -> None:
        self._program = program
        self._sets: _ValueSets | None = None
        self._lookup: dict[str, frozenset[Any] | None] = {}

    def _build(self) -> _ValueSets:
        from pyrung.core.analysis.pdg import build_program_graph
        from pyrung.core.instruction import ForLoopInstruction
        from pyrung.core.validation._common import walk_instructions

        graph = build_program_graph(self._program)
        unwritten = frozenset(
            name
            for name, tag in graph.tags.items()
            if name not in graph.writers_of and not tag.readonly
        )
        instructions = list(walk_instructions(self._program))
        sets = _ValueSets(
            instructions,
            graph.tags,
            unwritten,
            None,
            follow_indirect_reads=True,
            declared_inputs=True,
            written_first=frozenset(
                name for name in graph.writers_of if graph.unconditional_write_before_read(name)
            ),
        )
        for name, tag in graph.tags.items():
            known = sets.values(name)
            if known is None:
                known = sets.declared(tag)
            if known is None and type(tag.max) is int and tag.choices is None:
                # Only the past-the-end side is reported, so a lone max is a bound.
                known = frozenset({tag.max, tag.default})
            self._lookup[name] = known
        for instruction in instructions:
            count = getattr(instruction, "count", None)
            if isinstance(instruction, ForLoopInstruction) and type(count) is int and count > 0:
                self._lookup[instruction.idx_tag.name] = frozenset(range(count))
        return sets

    def reach(self, ref: Any) -> frozenset[int] | None:
        """Every address *ref* can use, or None when its values are not bounded."""
        if self._sets is None:
            self._sets = self._build()
        if isinstance(ref, IndirectBlockRange):
            window = self._sets.window(ref, self._lookup)
            if window is None:
                # A span over the cap has no window, but its highest end still shows.
                ends = self._sets.integers(ref.end_expr, self._lookup)
                return frozenset({max(ends)}) if ends else None
            return window
        return self._sets.addresses(ref, self._lookup) or None


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


def _check_use(
    ref: Any,
    write: bool,
    copy_write: bool,
    location_text: str,
    tag_map: TagMap,
    bounds: _PointerBounds,
    mode: ValidationMode,
    resolver: _ResolutionCache,
) -> ClickFinding | None:
    block = ref.block
    memory_type = resolver.resolve_block_memory_type(block.name)
    bank = BANKS.get(memory_type) if memory_type else None
    if bank is None:
        return None
    entry = tag_map._block_entry_by_name(block.name)
    try:
        offset = 0 if entry is None else tag_map._offset_for(entry.logical)
    except (KeyError, ValueError):
        return None
    reached = bounds.reach(ref)
    if reached is None:
        return None
    past = sorted(addr for addr in reached if addr + offset > bank.max_addr)
    if not past:
        return None

    if isinstance(ref, IndirectBlockRange):
        used = f"The indirect range on {block.name}"
        reaches = f"ends at {memory_type}{past[-1] + offset}"
    else:
        index = ref.pointer.name if isinstance(ref, IndirectRef) else format_expr(ref.expr)
        used = f"{block.name}[{index}]"
        reaches = f"can reach {memory_type}{past[0] + offset}"
    outcome = "pyrung drops the write." if write else "pyrung reads 0 there."
    if copy_write and memory_type == "DD":
        outcome = "A CLICK Copy writes there and raises no flag. pyrung drops the write."
    return ClickFinding(
        code=CLK_PTR_PAST_BANK,
        severity="error" if mode == "strict" else "warning",
        message=(
            f"{used} at {location_text} {reaches}. "
            f"{memory_type} ends at {memory_type}{bank.max_addr}. {outcome}"
        ),
        location=location_text,
        suggestion=(
            f"Keep the pointer inside {memory_type}, with min and max or choices on the tag, "
            "or clamp it before the instruction."
        ),
    )


def _evaluate_pointer_past_bank(
    instruction: Any,
    base_location: ProgramLocation,
    tag_map: TagMap,
    bounds: _PointerBounds,
    mode: ValidationMode,
    resolver: _ResolutionCache,
) -> list[ClickFinding]:
    """CLK_PTR_PAST_BANK: a pointer, index or indirect range in an instruction whose
    known values can address memory past the end of its block's bank.

    Below the first address both engines set the address error flag, so that is
    not reported.
    """
    findings: list[ClickFinding] = []
    for field, ref in _uses(instruction):
        write = _is_write(instruction, ref)
        copy_write = write and type(instruction).__name__ == "CopyInstruction"
        location_text = _format_location(
            _instruction_location(base_location, f"instruction.{field}")
        )
        finding = _check_use(ref, write, copy_write, location_text, tag_map, bounds, mode, resolver)
        if finding is not None:
            findings.append(finding)
    return findings


def _evaluate_condition_pointer_past_bank(
    program: Program,
    tag_map: TagMap,
    bounds: _PointerBounds,
    mode: ValidationMode,
    resolver: _ResolutionCache,
) -> list[ClickFinding]:
    """CLK_PTR_PAST_BANK for a pointer read in a rung's contact conditions."""
    findings: list[ClickFinding] = []

    def walk(rung: Any, scope: Any, subroutine: str | None, index: int, path: tuple[int, ...]):
        seen: set[int] = set()
        for cond in rung._conditions:
            for ref in _scan(cond)[1]:
                if id(ref) in seen:
                    continue
                seen.add(id(ref))
                where = _format_location(
                    ProgramLocation(scope, subroutine, index, path, None, None, "condition")
                )
                finding = _check_use(ref, False, False, where, tag_map, bounds, mode, resolver)
                if finding is not None:
                    findings.append(finding)
        for branch_index, branch in enumerate(rung._branches):
            walk(branch, scope, subroutine, index, (*path, branch_index))
        for body_rung in iter_loop_body_rungs(rung._instructions):
            walk(body_rung, scope, subroutine, index, path)

    for rung_index, rung in enumerate(program.rungs):
        walk(rung, "main", None, rung_index, ())
    for name in sorted(program.subroutines):
        for rung_index, rung in enumerate(program.subroutines[name]):
            walk(rung, "subroutine", name, rung_index, ())
    return findings
