"""When each contact of a rung is read, so an export keeps it there.

A CLICK solves a rung row by row, and so does pyrung: the trunk's contacts are
read as the rung starts, and a branch reads its own contacts when the solve
reaches it, after the rows above it ran.  The exporter redraws a rung two
ways that move a contact to another row:

* it factors label-equal contacts into a shared node (``branch(C, D)`` beside
  ``branch(C, E)`` becomes ``branch(C)`` holding both), which reads them
  earlier;
* it splits a rung at each box with pins (CLICK C000A), and every CLICK rung
  it makes repeats the path in front of its outputs, which reads it later.

A move is harmless unless a row in between writes a tag the contact reads.
:class:`ReadOrder` records where the source rung reads each contact and asks
:func:`~pyrung.core.validation.order_dependence.reads_after_write` about the
rows a move would cross.

Positions count outputs: a contact read at position *p* sees the writes of
outputs ``0..p-1`` of the rung (in solve order) and none after.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.rung import Rung
from pyrung.core.validation.order_dependence import (
    ReadAfterWrite,
    SolveRow,
    _RowBuilder,
    reads_after_write,
)

if TYPE_CHECKING:
    from pyrung.core.program import Program


def contact_positions(rung: Rung, *, start: int = 0) -> tuple[list[list[Any]], list[list[int]]]:
    """Each output's contacts in path order, and the position each is read at.

    Outputs come in solve order (as ``_collect_instruction_trees`` lists them).
    A row's own contacts are read at the position of the first output after
    it: everything above has run, nothing below has.
    """
    conditions: list[list[Any]] = []
    positions: list[list[int]] = []

    def walk(row: Rung, inherited: list[Any], at: list[int]) -> None:
        local = row._conditions[row._branch_condition_start :]
        here = start + len(conditions)
        path = [*inherited, *local]
        read = [*at, *([here] * len(local))]
        for item in row._execution_items:
            if isinstance(item, Rung):
                walk(item, path, read)
            else:
                conditions.append(path)
                positions.append(read)

    walk(rung, [], [])
    return conditions, positions


@dataclass(frozen=True)
class MovedRead:
    """Output *output*'s contact *contact* would be read across a write of *tag*.

    *writer* is the output (by position in the rung) whose write it crosses.
    """

    output: int
    contact: Any
    tag: str
    writer: int


class ReadOrder:
    """Where the source rung reads each output's contacts, and what each output writes."""

    def __init__(self, program: Program, rung: Rung, instructions: Sequence[Any]) -> None:
        self._builder = _RowBuilder(program)
        self.conditions, self.positions = contact_positions(rung)
        if len(self.conditions) != len(instructions):  # pragma: no cover - defensive
            raise AssertionError("contact walk and output walk disagree")
        self._writes: list[SolveRow] = []
        for instr in instructions:
            names, banks = self._builder.instr_writes(instr)
            self._writes.append(SolveRow(writes=names, indexed_writes=banks))
        self._reads: dict[int, SolveRow] = {}
        for path in self.conditions:
            for condition in path:
                if id(condition) not in self._reads:
                    self._reads[id(condition)] = self._builder.condition_reads([condition])
        self._bank_of = self._builder.bank_of()

    def crossing(self, output: int, index: int, at: int) -> MovedRead | None:
        """The first write a move of contact *index* of *output* to *at* crosses."""
        was = self.positions[output][index]
        if was == at:
            return None
        lo, hi = min(was, at), max(was, at)
        condition = self.conditions[output][index]
        rows = [*self._writes[lo:hi], self._reads[id(condition)]]
        hits: tuple[ReadAfterWrite, ...] = reads_after_write(rows, bank_of=self._bank_of)
        if not hits:
            return None
        hit = hits[0]
        return MovedRead(output=output, contact=condition, tag=hit.tag, writer=lo + hit.writer)

    def check(self, pieces: Sequence[Rung]) -> MovedRead | None:
        """The first contact *pieces* (the rung as exported, in order) would move across a write."""
        offset = 0
        for piece in pieces:
            _conditions, positions = contact_positions(piece, start=offset)
            for local, read_at in enumerate(positions):
                output = offset + local
                if len(read_at) != len(self.positions[output]):  # pragma: no cover - defensive
                    raise AssertionError("exported path differs from the source path")
                for index, at in enumerate(read_at):
                    moved = self.crossing(output, index, at)
                    if moved is not None:
                        return moved
            offset += len(positions)
        return None
