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
the way the program graph widens it, and an index no declared range bounds
(``DS[P]`` with ``P`` unbounded) reads or writes any element of its bank, so it
meets a write to, or a read of, any of the bank's tags.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
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
    never reach its own reads.  *indexed_reads* and *indexed_writes* name the
    banks the row reads or writes through an index no declared range bounds:
    any element of the bank.
    """

    reads: frozenset[str] = frozenset()
    writes: frozenset[str] = frozenset()
    indexed_reads: frozenset[str] = frozenset()
    indexed_writes: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ReadAfterWrite:
    """Row *reader* reads *tag*, which row *writer* (the latest earlier one) wrote.

    With *indexed*, *tag* is a bank the reader reads through an unbounded index.
    """

    reader: int
    tag: str
    writer: int
    indexed: bool = False


def _latest(*rows: int | None) -> int | None:
    known = [row for row in rows if row is not None]
    return max(known) if known else None


def reads_after_write(
    rows: Sequence[SolveRow],
    shared_reads: Iterable[str] = (),
    bank_of: Mapping[str, str] | None = None,
) -> tuple[ReadAfterWrite, ...]:
    """Every read, in solve order, of a tag that an earlier row wrote.

    *rows* are in solve order.  *shared_reads* are tags every row re-reads on
    top of its own (a path a split repeats on each CLICK rung it makes).
    *bank_of* names the bank of each tag, so that an indexed access to a bank
    meets a named access to one of its tags; without it indexed accesses meet
    only each other.  Each hit names the reading row, the tag (the bank, for an
    indexed read) and the latest earlier row that wrote it.  Hits come in row
    order, then name order.
    """
    shared = frozenset(shared_reads)
    banks = bank_of or {}
    last_writer: dict[str, int] = {}
    last_indexed: dict[str, int] = {}  # bank -> latest row writing it through an index
    last_member: dict[str, int] = {}  # bank -> latest row writing one of its tags by name
    hits: list[ReadAfterWrite] = []
    for index, row in enumerate(rows):
        found: list[ReadAfterWrite] = []
        for tag in row.reads | shared:
            bank = banks.get(tag)
            writer = _latest(last_writer.get(tag), None if bank is None else last_indexed.get(bank))
            if writer is not None:
                found.append(ReadAfterWrite(reader=index, tag=tag, writer=writer))
        for bank in row.indexed_reads:
            writer = _latest(last_indexed.get(bank), last_member.get(bank))
            if writer is not None:
                found.append(ReadAfterWrite(reader=index, tag=bank, writer=writer, indexed=True))
        hits.extend(sorted(found, key=lambda hit: (hit.tag, hit.indexed)))
        for tag in row.writes:
            last_writer[tag] = index
            bank = banks.get(tag)
            if bank is not None:
                last_member[bank] = index
        for bank in row.indexed_writes:
            last_indexed[bank] = index
    return tuple(hits)


# ---------------------------------------------------------------------------
# Rows of a program's rungs
# ---------------------------------------------------------------------------

_INDEXED = "[...]"
"""Suffix naming a bank read through an index (``DS[...]``) in a finding."""

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


_Writes = tuple[frozenset[str], frozenset[str]]
_NO_WRITES: _Writes = (frozenset(), frozenset())


def _merge(parts: Iterable[_Writes]) -> _Writes:
    names: set[str] = set()
    banks: set[str] = set()
    for part_names, part_banks in parts:
        names |= part_names
        banks |= part_banks
    return frozenset(names), frozenset(banks)


def _is_unbounded(ref: Any) -> bool:
    """Whether *ref* is an index that no declared pointer range bounds."""
    from pyrung.core.analysis.pdg import _indirect_expr_tags, _indirect_ref_tags
    from pyrung.core.memory_block import IndirectExprRef, IndirectRef

    if isinstance(ref, IndirectRef):
        return _indirect_ref_tags(ref.block, ref.pointer) is None
    if isinstance(ref, IndirectExprRef):
        return _indirect_expr_tags(ref.block, ref.expr) is None
    return False


class _RowBuilder:
    def __init__(self, program: Program) -> None:
        self._program = program
        self._tag_refs: dict[str, Tag] = {}
        self._sub_writes: dict[str, _Writes] = {}
        self._instr_writes_memo: dict[int, _Writes] = {}
        self._indexed_blocks: dict[str, Any] = {}

    # -- reads -------------------------------------------------------------

    def condition_reads(self, conditions: Iterable[Any]) -> SolveRow:
        """The reads of *conditions*: named tags, and banks read through an index."""
        from pyrung.core.analysis.pdg import _extract_reads_from_condition, _visit_indirect_refs

        reads: set[str] = set()
        banks: set[str] = set()
        seen: set[int] = set()

        def visit(ref: Any) -> None:
            if _is_unbounded(ref):
                banks.add(self._note_bank(ref.block))
            expr = getattr(ref, "expr", None)
            if expr is not None:  # an index that indexes: DS[DS[P] + 1]
                _visit_indirect_refs(expr, visit, seen)

        for cond in conditions:
            reads |= _extract_reads_from_condition(cond, self._tag_refs)
            _visit_indirect_refs(cond, visit, seen)
        return SolveRow(reads=frozenset(reads), indexed_reads=frozenset(banks))

    def _note_bank(self, block: Any) -> str:
        self._indexed_blocks.setdefault(block.name, block)
        return block.name

    def bank_of(self, *, every_bank: bool = False) -> dict[str, str]:
        """The bank of every tag seen so far that sits in a bank something indexes.

        With *every_bank*, of every tag seen in any bank: a finding's display
        needs it to find ``DS3`` in ``DS[PB]``, a bounded index.
        """
        out: dict[str, str] = {}
        blocks = dict(self._indexed_blocks)
        for name, tag in self._tag_refs.items():
            block = getattr(tag, "_pyrung_block", None)
            if block is not None and (every_bank or block.name in self._indexed_blocks):
                out[name] = block.name
                blocks.setdefault(block.name, block)
        for name, block in blocks.items():
            if every_bank or name in self._indexed_blocks:
                for tag in block._mapped_tags.values():
                    out.setdefault(tag.name, name)
        return out

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

    def instr_writes(self, instr: Any) -> _Writes:
        """``(named tags, banks written through an index)`` that *instr* can write."""
        memo = self._instr_writes_memo.get(id(instr))
        if memo is not None:
            return memo
        from pyrung.core.analysis.pdg import _extract_instruction_writes, _extract_tag_names
        from pyrung.core.analysis.write_sites import instruction_write_targets
        from pyrung.core.instruction.control import CallInstruction, ForLoopInstruction
        from pyrung.core.tag import ImmediateRef

        writes, _target_reads, implicit = _extract_instruction_writes(instr, self._tag_refs)
        out = set(writes) | implicit
        banks: set[str] = set()
        for dest in instruction_write_targets(instr):
            if isinstance(dest, ImmediateRef):
                dest = dest.value
            if _is_unbounded(dest):
                banks.add(self._note_bank(dest.block))
        children: list[_Writes] = []
        if isinstance(instr, ForLoopInstruction):
            out |= _extract_tag_names(getattr(instr, "idx_tag", None), self._tag_refs)
            children.extend(self.instr_writes(child) for child, _local in instr.iter_body())
        if isinstance(instr, CallInstruction):
            children.append(self.subroutine_writes(instr.subroutine_name))
        for names, child_banks in children:
            out |= names
            banks |= child_banks
        result = (frozenset(out), frozenset(banks))
        self._instr_writes_memo[id(instr)] = result
        return result

    def subroutine_writes(self, name: str) -> _Writes:
        known = self._sub_writes.get(name)
        if known is not None:
            return known
        self._sub_writes[name] = _NO_WRITES  # a recursive call adds nothing new
        self._sub_writes[name] = _merge(
            self.rung_writes(rung) for rung in self._program.subroutines.get(name, ())
        )
        return self._sub_writes[name]

    def rung_writes(self, rung: Any) -> _Writes:
        from pyrung.core.rung import Rung as RungLogic

        return _merge(
            self.rung_writes(item) if isinstance(item, RungLogic) else self.instr_writes(item)
            for item in rung._execution_items
        )

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
                    self.condition_reads(rung._conditions),
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
                        self.condition_reads(local),
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
        writes, banks = self.instr_writes(instr)
        return _Placed(
            SolveRow(
                reads=reads.reads,
                writes=writes,
                indexed_reads=reads.indexed_reads,
                indexed_writes=banks,
            ),
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
                        self.condition_reads(local),
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
    """``(start, length)`` of *token* in *line* as a whole name, else its first match.

    An indexed token (``DS[...]``) matches its bank's name where it is indexed.
    """
    if not token:
        return None
    if token.endswith(_INDEXED):
        bank = token[: -len(_INDEXED)]
        match = re.search(rf"(?<![\w.]){re.escape(bank)}\[", line)
        return (match.start(), len(bank)) if match is not None else None
    match = re.search(rf"(?<![\w.]){re.escape(token)}(?![\w\[])", line)
    if match is not None:
        return match.start(), len(token)
    index = line.find(token)
    return (index, len(token)) if index >= 0 else None


def _tag_span(line: str, tag: str, banks: Mapping[str, str]) -> tuple[int, int] | None:
    """Where *line* names *tag*, or else indexes the bank *tag* belongs to.

    A bounded index (``DS[PB]``, ``PB`` in 2..4) reads or writes its elements by
    name, but the code spells the index, not ``DS3``.
    """
    span = _token_span(line, tag)
    bank = banks.get(tag)
    if span is None and bank is not None and not tag.endswith(_INDEXED):
        span = _token_span(line, bank + _INDEXED)
    return span


def _leg_name(field_name: str) -> str:
    return field_name.replace("_condition", "")


def _reader_lines(
    placed: _Placed, tags: tuple[str, ...], banks: Mapping[str, str]
) -> tuple[str, ...]:
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
        if any(_tag_span(text, tag, banks) is not None for tag in tags):
            legs.append(f"    .{_leg_name(name)}({text})")
    return (code, *legs)


def _caret(
    lines: tuple[str, ...], tag: str, banks: Mapping[str, str]
) -> tuple[int, int, int] | None:
    # A box's own line comes first; its legs (where the read is) below it.
    order = range(len(lines)) if len(lines) == 1 else range(1, len(lines))
    for index in order:
        span = _tag_span(lines[index], tag, banks)
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


def _writer_frame(placed: _Placed, tag: str, banks: Mapping[str, str]) -> Frame:
    code = _writer_code(placed.instr, tag)
    span = None
    if tag.endswith(_INDEXED):  # an indexed read meets a named write to its bank
        bank = tag[: -len(_INDEXED)]
        for name in sorted(placed.row.writes):
            if banks.get(name) == bank:
                span = _tag_span(code, name, banks)
                if span is not None:
                    break
    if span is None:
        span = _tag_span(code, tag, banks)
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


def _display(
    reader: _Placed, tags: tuple[str, ...], writer: _Placed, bank_of: Mapping[str, str]
) -> FindingDisplay:
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
    lines = _reader_lines(reader, tags, bank_of)
    return FindingDisplay(
        code=RUNG_ORDER_DEPENDENT,
        severity="warning",
        frames=(
            Frame(location=reader.location, lines=lines, caret=_caret(lines, tags[0], bank_of)),
            _writer_frame(writer, tags[0], bank_of),
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
    """The tags read; a bank read through an index is named ``DS[...]``."""
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

    bank_of = builder.bank_of()
    display_banks = builder.bank_of(every_bank=True)
    findings: list[OrderDependenceFinding] = []
    for sequence in sequences:
        placed = sequence.rows
        hits = reads_after_write([p.row for p in placed], bank_of=bank_of)
        by_reader: dict[int, list[ReadAfterWrite]] = {}
        for hit in hits:
            by_reader.setdefault(hit.reader, []).append(hit)
        for reader_index, reader_hits in by_reader.items():
            reader = placed[reader_index]
            tags = tuple(hit.tag + _INDEXED if hit.indexed else hit.tag for hit in reader_hits)
            writer = placed[reader_hits[0].writer]
            findings.append(
                OrderDependenceFinding(
                    code=RUNG_ORDER_DEPENDENT,
                    target_name=reader.location,
                    display=_display(reader, tags, writer, display_banks),
                    severity="warning",
                    tags=tags,
                    reader=reader.kind,
                )
            )
    return OrderDependenceReport(findings=tuple(findings))
