"""The rungs every case gets: its comment, its reset, and its ladder.

A case's first rung carries the case's comment and is true every scan while
``Cmd == 0``: it zeroes the slot with one ``fill``, runs the case's ``init``,
and raises ``Ready``.  A case with ``ready`` conditions raises ``Ready`` in a
second rung, only once they hold.  The harness writes ``Cmd = 1`` only after
it reads ``Ready``, so every run starts from the state a fresh PLC has.

Both runners emit cases through :func:`emit_case`, so the in-process program
and the CLICK program are the same rungs.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from pyrung.conformance._case import Case, describe_expect
from pyrung.conformance._slot import SLOT_FIELDS, make_slot
from pyrung.core import Program, comment, copy, fill, rung
from pyrung.core.system_points import SYSTEM_TAGS_BY_NAME

if TYPE_CHECKING:
    from pyrung.core.structure import _NamedArrayRuntime


def case_label(group: str, c: Case) -> str:
    """Return ``group/id``, or the bare id for a case outside a group."""
    return f"{group}/{c.id}" if group else c.id


def case_comment(group: str, c: Case) -> str:
    """Return the comment on a case's first rung: its id, its sentence, and what it expects.

    It is part of the exported ladder, so it is part of the group hash: editing
    a sentence makes the group stale on the PLC until it is pushed again.
    """
    return f"Case {case_label(group, c)}\n{c.sentence}\nExpect: {describe_expect(c.checks)}"


def emit_case(group: str, c: Case, slots: _NamedArrayRuntime, index: int) -> None:
    """Add case *c*'s rungs, on slot *index* of *slots*, to the current program."""
    slot = slots[index]
    comment(case_comment(group, c))
    with rung(slot.Cmd == 0):
        fill(0, slots.instance(index))
        if c.init is not None:
            c.init(slot)
        if c.ready is None:
            copy(1, slot.Ready)
    if c.ready is not None:
        with rung(slot.Cmd == 0, *_conditions(c.ready(slot))):
            copy(1, slot.Ready)
    c.ladder(slot)


def _conditions(value: Any) -> tuple[Any, ...]:
    return tuple(value) if isinstance(value, (list, tuple)) else (value,)


def footprint(c: Case) -> tuple[frozenset[str], frozenset[str]]:
    """Return the tags that case *c* uses outside its slot, as two sets.

    The first set holds every tag that the case reads or writes.  The second holds the tags
    that it writes.

    System tags and pyrung's internal tags (a leading ``_``) are left out.  So are the
    characters a ``to_text()`` copy of a tag may write past its first.  The analyses
    count every character the source's type can write, up to 48 for a Real.  A case
    copies a value it sets, and its own TXTs hold that value's text.
    """
    from pyrung.core.analysis.pdg import _extract_write_targets, build_program_graph
    from pyrung.core.analysis.write_sites import (
        instruction_partial_write_targets,
        instruction_write_targets,
    )
    from pyrung.core.validation._common import walk_instructions

    slots = make_slot(1, name="Footprint")
    with Program(strict=False) as program:
        emit_case("", c, slots, 1)
    graph = build_program_graph(program)
    own = {getattr(slots[1], f).name for f in SLOT_FIELDS}

    may: set[str] = set()
    sure: set[str] = set(graph.readers_of)
    for instr in walk_instructions(program):
        partial = instruction_partial_write_targets(instr)
        for target in instruction_write_targets(instr):
            names = _extract_write_targets(target, {})[0]
            (may if any(target is p for p in partial) else sure).update(names)
    left_out = may - sure

    def outside(names: Any) -> frozenset[str]:
        return frozenset(
            n
            for n in names
            if n not in own
            and n not in left_out
            and n not in SYSTEM_TAGS_BY_NAME
            and not n.startswith("_")
        )

    return outside(graph.tags), outside(graph.writers_of)


def check_disjoint(cases: Sequence[tuple[str, Case]]) -> dict[str, frozenset[str]]:
    """Raise ``ValueError`` if a case writes memory another case reads or writes.

    Cases run side by side on one PLC, so shared memory is shared state.
    Returns each case's footprint (every tag it touches), keyed by its label.
    """
    prints = {case_label(g, c): footprint(c) for g, c in cases}
    labels = list(prints)
    clashes: list[str] = []
    for i, a in enumerate(labels):
        a_all, a_writes = prints[a]
        for b in labels[i + 1 :]:
            b_all, b_writes = prints[b]
            shared = (a_writes & b_all) | (b_writes & a_all)
            if shared:
                clashes.append(f"{a} and {b} share {', '.join(sorted(shared))}")
    if clashes:
        raise ValueError(
            "Conformance cases must not share memory (each case owns what it writes):\n  "
            + "\n  ".join(clashes)
        )
    return {label: everything for label, (everything, _) in prints.items()}
