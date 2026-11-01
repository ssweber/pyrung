"""When a CLICK rung reads each contact, so the generated code reads it there too.

Each output solves only the path from the rail to it.  A contact is read when
an output first needs it: on the row of the first output whose wire runs through it.  A
contact two outputs share (one cell, teed off) is read once; two cells with the
same label are two reads, and a row in between may write what the second one
reads.

The importer groups outputs by label, and a contact it factors into a shared
``with rung(...)`` or ``with branch(...)`` is read once, where the group's
first output is.  :class:`ReadOrder` keeps that from moving a read across a
write: it records the output each cell is first read at and what each output
may write, and asks
:func:`~pyrung.core.validation.order_dependence.reads_after_write` about the
outputs a move would cross.

What an output writes is read off its AF token: every operand it names (reads
included), both the Done bit and the accumulator of a timer or counter it names
by either, a whole bank for a pointer or for a word view of a bit bank (``XD``,
``YD``), and everything for ``call(...)``.  Addresses are compared in one
spelling (``DS001`` is ``DS1``).  A system contact (SC, SD)
counts as written by every output.  The guard over-approximates: it may keep
two equal contacts apart that could have shared, never the reverse.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from pyrung.click._topology import Leaf, SPNode, as_series_children
from pyrung.click.codegen.constants import _RANGE_RE
from pyrung.click.codegen.utils import _strip_quoted_strings
from pyrung.core.validation.order_dependence import SolveRow, reads_after_write

_ADDRESS_RE = re.compile(r"(CTD|CT|TD|TXT|SC|SD|DS|DD|DH|DF|XD|YD|X|Y|C|T)(\d+)u?")
_POINTER_RE = re.compile(r"([A-Z]+)\[")
_BANK_RE = re.compile(r"[A-Z]+")
_PAIR_RE = re.compile(r"(CTD|CT|TD|T)(\d+)")
_SYSTEM = ("SC", "SD")
_EVERYTHING = "*"
"""A write of every tag: ``call(...)`` runs a subroutine that may write any."""
_WORD_VIEWS = {"XD": "X", "YD": "Y"}
"""Word views of a bit bank: ``XD0`` reads sixteen X bits at once."""
_PAIRS = {"CTD": "CT", "CT": "CTD", "TD": "T", "T": "TD"}
"""A timer or counter's Done bit and accumulator, written together."""


def _bank(name: str) -> str:
    match = _BANK_RE.match(name)
    return match.group(0) if match else name


def _operands(text: str) -> tuple[set[str], set[str]]:
    """Named operands of *text* (ranges expanded), and banks it reaches whole.

    Names come normalized (``DS001`` and ``DS1`` are one address).  A bank
    is reached whole through a pointer (``DS[DD1]``) or through a word view
    of a bit bank (``XD0`` is sixteen X bits; the guard takes it as any).
    """
    text = _strip_quoted_strings(text)
    names: set[str] = set()
    banks: set[str] = set()

    def add(prefix: str, number: int) -> None:
        if prefix in _WORD_VIEWS:
            banks.add(_WORD_VIEWS[prefix])
        else:
            names.add(f"{prefix}{number}")

    for match in _RANGE_RE.finditer(text):
        prefix, start, end = match.group(1), int(match.group(2)), int(match.group(4))
        if prefix == match.group(3) and start <= end:
            if prefix in _WORD_VIEWS:
                banks.add(_WORD_VIEWS[prefix])
            else:
                names.update(f"{prefix}{number}" for number in range(start, end + 1))
    for prefix, digits in _ADDRESS_RE.findall(text):
        add(prefix, int(digits))
    for bank in _POINTER_RE.findall(text):
        banks.add(_WORD_VIEWS.get(bank, bank))
    return names, banks


def _token_writes(af_token: str) -> SolveRow:
    if af_token.startswith("call("):
        return SolveRow(writes=frozenset({_EVERYTHING}))
    names, banks = _operands(af_token)
    for name in list(names):
        # A token may name only the accumulator (an Out timer variant), or
        # only the Done bit (a reset), and writes both.
        match = _PAIR_RE.fullmatch(name)
        if match:
            names.add(_PAIRS[match.group(1)] + match.group(2))
    return SolveRow(writes=frozenset(names), indexed_writes=frozenset(banks))


def _cells(node: SPNode) -> set[tuple[int, int]]:
    if isinstance(node, Leaf):
        return {(node.row, node.col)}
    cells: set[tuple[int, int]] = set()
    for child in node.children:
        cells |= _cells(child)
    return cells


def _labels(node: SPNode) -> list[str]:
    if isinstance(node, Leaf):
        return [str(node.label)]
    return [label for child in node.children for label in _labels(child)]


@dataclass(frozen=True)
class _Reads:
    row: SolveRow
    system: bool


class ReadOrder:
    """Where one raw CLICK rung reads each contact cell, and what its outputs write.

    *trees* are the full condition trees of the rung's outputs in solve order
    (row order), *af_tokens* their AF tokens.
    """

    def __init__(self, trees: Sequence[SPNode | None], af_tokens: Sequence[str]) -> None:
        self._first: dict[tuple[int, int], int] = {}
        for index, tree in enumerate(trees):
            if tree is None:
                continue
            for cell in _cells(tree):
                self._first.setdefault(cell, index)
        self._writes = [_token_writes(token) for token in af_tokens]
        self._bank_of: dict[str, str] = {}
        for row in self._writes:
            for name in row.writes:
                self._bank_of[name] = _bank(name)

    def _reads(self, node: SPNode) -> _Reads:
        names: set[str] = set()
        banks: set[str] = set()
        for label in _labels(node):
            found, pointed = _operands(label)
            names |= found
            banks |= pointed
        for name in names:
            self._bank_of.setdefault(name, _bank(name))
        system = any(_bank(name) in _SYSTEM for name in names) or any(
            bank in _SYSTEM for bank in banks
        )
        return _Reads(
            SolveRow(reads=frozenset(names | {_EVERYTHING}), indexed_reads=frozenset(banks)),
            system,
        )

    def crosses(self, node: SPNode, at: int) -> bool:
        """Whether reading *node* (a contact or OR block) at output *at* crosses a write."""
        cells = _cells(node)
        was = min((self._first[cell] for cell in cells if cell in self._first), default=at)
        if was == at:
            return False
        lo, hi = min(was, at), max(was, at)
        reads = self._reads(node)
        if reads.system:
            return True
        return bool(reads_after_write([*self._writes[lo:hi], reads.row], bank_of=self._bank_of))

    def shared_limit(
        self, trees: Sequence[SPNode | None], positions: Sequence[int], shared: int
    ) -> int:
        """How many of the *shared* leading contacts of *trees* may be read as one.

        *positions* are the outputs' places in the raw rung; the shared node
        is read at the first.
        """
        if not shared or not positions:
            return shared
        at = positions[0]
        children = [as_series_children(tree) for tree in trees]
        for index in range(shared):
            for child in children:
                if self.crosses(child[index], at):
                    return index
        return shared


def shared_limit(
    order: ReadOrder | None,
    trees: Sequence[SPNode | None],
    positions: Sequence[int | None],
    shared: int,
) -> int:
    """:meth:`ReadOrder.shared_limit`, or *shared* where the order is unknown."""
    if order is None or any(position is None for position in positions):
        return shared
    return order.shared_limit(trees, [p for p in positions if p is not None], shared)


__all__ = ["ReadOrder", "shared_limit"]
