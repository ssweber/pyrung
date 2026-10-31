"""Intra-rung order dependence: rows that read a tag an earlier row wrote.

A CLICK solves a rung row by row, top to bottom (hardware rounds 1 and 2).  The
trunk's contacts are read once and its power is held for every row below it;
each branch reads its own contacts when the solve reaches it, after the rows
above it have run; a ``.continued()`` rung is one more row of the same CLICK
rung, from the rail; and a box reads its legs (reset, jump, ...) at box entry.
pyrung reads every one of those against a snapshot taken at rung entry, so a
row that reads a tag written by an earlier row of the same rung (or chain of
continued rungs) sees the old value in pyrung and the new one on a CLICK.  The
live row solve will make pyrung read it the CLICK way, and such a rung's result
changes.

Two layers:

* :func:`reads_after_write` is the predicate: given rows in solve order, every
  read of a tag that some earlier row wrote.  Rows are plain read/write sets, so
  export, import and ClickNick can ask it about the rows they build.
* :func:`validate_order_dependence` builds the rows of every rung of a program
  and reports ``RUNG_ORDER_DEPENDENT`` for each row that reads a tag written
  above it.

What counts as a row's read: the contacts the row adds (a top-level or
continued rung's conditions, a branch's local conditions) and a box's helper
legs.  Instruction operands are read live in both models, and a box's enable is
the power held from its rung, so neither counts.  What counts as a write: an
instruction's declared targets and status fields, the fault flags it can set,
everything a ``call()`` can write (transitively) and everything a For body can
write.  A For body rung reads its contacts when it runs in pyrung too, so it
starts a sequence of its own: only a box leg inside it can read an earlier body
row's write.  Tags are the statically known ones; an indirect write is widened
the way the program graph widens it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pyrung.core.validation._common import FactScope, compact_location
from pyrung.core.validation.display import FindingDisplay, Frame, _FindingTextMixin
from pyrung.core.validation.severity import Severity

if TYPE_CHECKING:
    from pyrung.core.program import Program
    from pyrung.core.tag import Tag
    from pyrung.core.validation.context import ValidationContext

RUNG_ORDER_DEPENDENT = "RUNG_ORDER_DEPENDENT"


# ---------------------------------------------------------------------------
# The predicate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SolveRow:
    """One row of a rung, as the solve reaches it: it reads, then it writes.

    Nothing writes within one row between its reads, so a row's own writes
    never reach its own reads.
    """

    reads: frozenset[str] = frozenset()
    writes: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ReadAfterWrite:
    """Row *reader* reads *tag*, which row *writer* (the latest earlier one) wrote."""

    reader: int
    tag: str
    writer: int


def reads_after_write(
    rows: Sequence[SolveRow], shared_reads: Iterable[str] = ()
) -> tuple[ReadAfterWrite, ...]:
    """Every read, in solve order, of a tag that an earlier row wrote.

    *rows* are in solve order.  *shared_reads* are tags every row re-reads on
    top of its own (a path a split repeats on each CLICK rung it makes).  Each
    hit names the reading row, the tag and the latest earlier row that wrote it.
    Hits come in row order, then tag name order.
    """
    shared = frozenset(shared_reads)
    last_writer: dict[str, int] = {}
    hits: list[ReadAfterWrite] = []
    for index, row in enumerate(rows):
        for tag in sorted(row.reads | shared):
            writer = last_writer.get(tag)
            if writer is not None:
                hits.append(ReadAfterWrite(reader=index, tag=tag, writer=writer))
        for tag in row.writes:
            last_writer[tag] = index
    return tuple(hits)


# ---------------------------------------------------------------------------
# Rows of a program's rungs
# ---------------------------------------------------------------------------

_TRUNK = "trunk"
_CONTINUED = "continued"
_BRANCH = "branch"
_BOX = "box"
_OUTPUT = "output"


@dataclass(frozen=True)
class _Placed:
    """A :class:`SolveRow` with where it came from, for the finding."""

    row: SolveRow
    kind: str
    rung: Any
    """The rung the row is (trunk, continued, branch), or the one holding its instruction."""
    location: str
    instr: Any = None
    legs: tuple[tuple[str, Any], ...] = ()
    """A box's helper legs, ``(field, condition)``."""
    body: int | None = None
    """1-based body rung of the For loop the row sits in, if any."""


@dataclass
class _Sequence:
    rows: list[_Placed] = field(default_factory=list)


def _iter_conditions(value: Any) -> Iterable[Any]:
    for item in value if isinstance(value, tuple) else (value,):
        if item is not None:
            yield item


class _RowBuilder:
    def __init__(self, program: Program) -> None:
        self._program = program
        self._tag_refs: dict[str, Tag] = {}
        self._sub_writes: dict[str, frozenset[str]] = {}
        self._instr_writes_memo: dict[int, frozenset[str]] = {}

    # -- reads -------------------------------------------------------------

    def condition_reads(self, conditions: Iterable[Any]) -> frozenset[str]:
        from pyrung.core.analysis.pdg import _extract_reads_from_condition

        reads: set[str] = set()
        for cond in conditions:
            reads |= _extract_reads_from_condition(cond, self._tag_refs)
        return frozenset(reads)

    @staticmethod
    def helper_legs(instr: Any) -> tuple[tuple[str, Any], ...]:
        """A box's helper legs (not the fields that carry its rung's power)."""
        from pyrung.core.site_memory import _is_power_field

        legs: list[tuple[str, Any]] = []
        for name in getattr(type(instr), "_conditions", ()):
            if _is_power_field(instr, name):
                continue
            value = getattr(instr, name, None)
            if isinstance(value, tuple):
                legs.extend((f"{name}[{i}]", c) for i, c in enumerate(value) if c is not None)
            elif value is not None:
                legs.append((name, value))
        return tuple(legs)

    # -- writes ------------------------------------------------------------

    def instr_writes(self, instr: Any) -> frozenset[str]:
        memo = self._instr_writes_memo.get(id(instr))
        if memo is not None:
            return memo
        from pyrung.core.analysis.pdg import _extract_instruction_writes, _extract_tag_names
        from pyrung.core.instruction.control import CallInstruction, ForLoopInstruction

        writes, _target_reads, implicit = _extract_instruction_writes(instr, self._tag_refs)
        out = set(writes) | implicit
        if isinstance(instr, ForLoopInstruction):
            out |= _extract_tag_names(getattr(instr, "idx_tag", None), self._tag_refs)
            for child, _local in instr.iter_body():
                out |= self.instr_writes(child)
        if isinstance(instr, CallInstruction):
            out |= self.subroutine_writes(instr.subroutine_name)
        result = frozenset(out)
        self._instr_writes_memo[id(instr)] = result
        return result

    def subroutine_writes(self, name: str) -> frozenset[str]:
        known = self._sub_writes.get(name)
        if known is not None:
            return known
        self._sub_writes[name] = frozenset()  # a recursive call adds nothing new
        out: set[str] = set()
        for rung in self._program.subroutines.get(name, ()):
            out |= self.rung_writes(rung)
        self._sub_writes[name] = frozenset(out)
        return self._sub_writes[name]

    def rung_writes(self, rung: Any) -> frozenset[str]:
        from pyrung.core.rung import Rung as RungLogic

        out: set[str] = set()
        for item in rung._execution_items:
            if isinstance(item, RungLogic):
                out |= self.rung_writes(item)
            else:
                out |= self.instr_writes(item)
        return frozenset(out)

    # -- sequences ---------------------------------------------------------

    def scope_sequences(
        self, rungs: Sequence[Any], scope: FactScope, subroutine: str | None
    ) -> list[_Sequence]:
        """The row sequences of one scope: one per chain of continued rungs, then
        one per For body rung inside them."""
        from pyrung.core.rung import Rung as RungLogic

        sequences: list[_Sequence] = []
        loops: list[tuple[Any, str]] = []
        current: _Sequence | None = None
        for rung_index, rung in enumerate(rungs):
            location = compact_location(scope, subroutine, rung_index)
            continued = bool(rung._use_prior_snapshot) and current is not None
            if not continued:
                current = _Sequence()
                sequences.append(current)
            assert current is not None
            current.rows.append(
                _Placed(
                    SolveRow(reads=self.condition_reads(rung._conditions)),
                    _CONTINUED if continued else _TRUNK,
                    rung,
                    location,
                )
            )
            self._walk_items(rung, scope, subroutine, rung_index, (), current, loops, None)
        while loops:
            loop, location = loops.pop(0)
            for number, body_rung in enumerate(loop.body_rungs, start=1):
                body = _Sequence()
                sequences.append(body)
                local = body_rung._conditions[body_rung._branch_condition_start :]
                body.rows.append(
                    _Placed(
                        SolveRow(reads=self.condition_reads(local)),
                        _TRUNK,
                        body_rung,
                        location,
                        body=number,
                    )
                )
                self._walk_body_rung(body_rung, location, number, body, loops)
            # Bare body instructions read their legs at their own row: nothing
            # changes for them.
            for item in loop.body:
                if not isinstance(item, RungLogic):
                    self._note_loop(item, location, loops)
        return sequences

    def _note_loop(self, instr: Any, location: str, loops: list[tuple[Any, str]]) -> None:
        from pyrung.core.instruction.control import ForLoopInstruction

        if isinstance(instr, ForLoopInstruction):
            loops.append((instr, location))

    def _instr_row(self, instr: Any, rung: Any, location: str, body: int | None = None) -> _Placed:
        legs = self.helper_legs(instr)
        reads = self.condition_reads(cond for _name, cond in legs)
        return _Placed(
            SolveRow(reads=reads, writes=self.instr_writes(instr)),
            _BOX if legs else _OUTPUT,
            rung,
            location,
            instr=instr,
            legs=legs,
            body=body,
        )

    def _walk_items(
        self,
        rung: Any,
        scope: FactScope,
        subroutine: str | None,
        rung_index: int,
        path: tuple[int, ...],
        sequence: _Sequence,
        loops: list[tuple[Any, str]],
        body: int | None,
    ) -> None:
        from pyrung.core.rung import Rung as RungLogic

        location = compact_location(scope, subroutine, rung_index, path)
        branch_index = {id(b): i for i, b in enumerate(rung._branches)}
        for item in rung._execution_items:
            if isinstance(item, RungLogic):
                sub_path = (*path, branch_index.get(id(item), len(branch_index)))
                local = item._conditions[item._branch_condition_start :]
                sequence.rows.append(
                    _Placed(
                        SolveRow(reads=self.condition_reads(local)),
                        _BRANCH,
                        item,
                        compact_location(scope, subroutine, rung_index, sub_path),
                    )
                )
                self._walk_items(
                    item, scope, subroutine, rung_index, sub_path, sequence, loops, body
                )
            else:
                sequence.rows.append(self._instr_row(item, rung, location, body))
                self._note_loop(item, location, loops)

    def _walk_body_rung(
        self,
        body_rung: Any,
        location: str,
        number: int,
        sequence: _Sequence,
        loops: list[tuple[Any, str]],
    ) -> None:
        from pyrung.core.rung import Rung as RungLogic

        for item in body_rung._execution_items:
            if isinstance(item, RungLogic):  # a branch: not allowed in a For body
                continue
            sequence.rows.append(self._instr_row(item, body_rung, location, number))
            self._note_loop(item, location, loops)


# ---------------------------------------------------------------------------
# Finding display
# ---------------------------------------------------------------------------


def _token_span(line: str, token: str) -> tuple[int, int] | None:
    """``(start, length)`` of *token* in *line* as a whole name, else its first match."""
    if not token:
        return None
    match = re.search(rf"(?<![\w.]){re.escape(token)}(?![\w\[])", line)
    if match is not None:
        return match.start(), len(token)
    index = line.find(token)
    return (index, len(token)) if index >= 0 else None


def _leg_name(field_name: str) -> str:
    return field_name.replace("_condition", "")


def _reader_lines(placed: _Placed, tags: tuple[str, ...]) -> tuple[str, ...]:
    from pyrung.core.validation.render import render_condition, render_instruction, render_rung_args

    if placed.kind == _BRANCH:
        local = placed.rung._conditions[placed.rung._branch_condition_start :]
        return (f"with branch({render_rung_args(local)}):",)
    if placed.kind == _CONTINUED:
        return (f"with rung({render_rung_args(placed.rung._conditions)}).continued():",)
    code, _caret = render_instruction(placed.instr, "...")
    legs = []
    for name, cond in placed.legs:
        text = render_condition(cond)
        if any(_token_span(text, tag) is not None for tag in tags):
            legs.append(f"    .{_leg_name(name)}({text})")
    return (code, *legs)


def _caret(lines: tuple[str, ...], token: str) -> tuple[int, int, int] | None:
    # A box's own line comes first; its legs (where the read is) below it.
    order = range(len(lines)) if len(lines) == 1 else range(1, len(lines))
    for index in order:
        span = _token_span(lines[index], token)
        if span is not None:
            return index, span[0], span[1]
    return None


def _writer_code(instr: Any, tag: str) -> str:
    from pyrung.core.instruction.control import CallInstruction, ForLoopInstruction
    from pyrung.core.validation.render import operand_name, render_instruction

    if isinstance(instr, CallInstruction):
        return f'call("{instr.subroutine_name}")'
    if isinstance(instr, ForLoopInstruction):
        return f"forloop({operand_name(instr.count)})"
    return render_instruction(instr, tag)[0]


def _writer_frame(placed: _Placed, tag: str) -> Frame:
    code = _writer_code(placed.instr, tag)
    span = _token_span(code, tag)
    return Frame(
        location=placed.location,
        lines=(code,),
        caret=(0, span[0], span[1]) if span else None,
    )


_READER_TEXT = {
    _BRANCH: "This branch reads",
    _CONTINUED: "This continued rung reads",
    _BOX: "This box's leg reads",
}

_ABOVE_TEXT = {
    _BRANCH: "a row above it in the same rung",
    _CONTINUED: "a row above it in the chain of continued rungs",
    _BOX: "a row above it in the same rung",
}


def _display(reader: _Placed, tags: tuple[str, ...], writer: _Placed) -> FindingDisplay:
    names = ", ".join(tags)
    noun = "it" if len(tags) == 1 else "them"
    where = f"In For body rung {reader.body}: " if reader.body is not None else ""
    lead = _READER_TEXT[reader.kind]
    if reader.body is not None:
        lead = lead[0].lower() + lead[1:]
    problem = (
        f"{where}{lead} {names} after {_ABOVE_TEXT[reader.kind]} writes {noun}. "
        f"pyrung reads {noun} as at rung entry; a CLICK reads the new value, "
        "and so will pyrung once the live row solve lands."
    )
    lines = _reader_lines(reader, tags)
    return FindingDisplay(
        code=RUNG_ORDER_DEPENDENT,
        severity="warning",
        frames=(
            Frame(location=reader.location, lines=lines, caret=_caret(lines, tags[0])),
            _writer_frame(writer, tags[0]),
        ),
        problem=problem,
        hint=(
            "move the read to a rung of its own below, or the write to a rung above, "
            "so the result does not depend on the solve order"
        ),
    )


# ---------------------------------------------------------------------------
# Public types and API
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderDependenceFinding(_FindingTextMixin):
    """A row that reads tags an earlier row of its rung wrote."""

    code: str
    target_name: str
    display: FindingDisplay
    severity: Severity
    tags: tuple[str, ...] = ()
    reader: str = ""
    """``branch``, ``continued`` or ``box``."""

    @property
    def message(self) -> str:
        return self.display.as_text()


@dataclass(frozen=True)
class OrderDependenceReport:
    findings: tuple[OrderDependenceFinding, ...]

    def summary(self) -> str:
        if not self.findings:
            return "No order-dependence findings."
        return f"{len(self.findings)} order-dependence finding(s)."


def validate_order_dependence(
    program: Program,
    *,
    _context: ValidationContext | None = None,
) -> OrderDependenceReport:
    """Report every row that reads a tag an earlier row of the same rung wrote.

    One ``RUNG_ORDER_DEPENDENT`` finding per reading row (a branch, a continued
    rung, or a box's helper legs), naming every such tag it reads.
    """
    builder = _RowBuilder(program)
    sequences = builder.scope_sequences(program.rungs, "main", None)
    for name in sorted(program.subroutines):
        sequences.extend(builder.scope_sequences(program.subroutines[name], "subroutine", name))

    findings: list[OrderDependenceFinding] = []
    for sequence in sequences:
        placed = sequence.rows
        hits = reads_after_write([p.row for p in placed])
        by_reader: dict[int, list[ReadAfterWrite]] = {}
        for hit in hits:
            by_reader.setdefault(hit.reader, []).append(hit)
        for reader_index, reader_hits in by_reader.items():
            reader = placed[reader_index]
            tags = tuple(hit.tag for hit in reader_hits)
            writer = placed[reader_hits[0].writer]
            findings.append(
                OrderDependenceFinding(
                    code=RUNG_ORDER_DEPENDENT,
                    target_name=reader.location,
                    display=_display(reader, tags, writer),
                    severity="warning",
                    tags=tags,
                    reader=reader.kind,
                )
            )
    return OrderDependenceReport(findings=tuple(findings))
