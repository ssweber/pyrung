"""What never() carries back from a comparison, against what a scan leaves on the target.

A target's store changes what it was given.  A WORD holds 65535 for the -1 an INT copies to it,
a calc to an INT holds -32768 for the 32767 of ``I + 1``.  The source has to try the value that
gets there, so each row here is stepped on both engines as well as asked of the verifier: a
Proven is checked against a sweep and a witness is replayed.  A decline is an honest answer,
and no row may be Proven where a scan fires.
"""

from __future__ import annotations

import operator
from collections.abc import Callable
from typing import Any

import pytest

from pyrung.core import (
    Block,
    Bool,
    Dint,
    Int,
    Program,
    Real,
    Rung,
    TagType,
    Word,
    blockcopy,
    calc,
    copy,
    fill,
    out,
)
from pyrung.core.analysis.prove import Counterexample, Intractable, Proven, never
from pyrung.core.compiled_plc import CompiledPLC
from pyrung.core.runner import PLC

ENGINES = (PLC, CompiledPLC)
TYPES = {"Int": Int, "Word": Word, "Dint": Dint, "Real": Real}
OPS: dict[str, Callable[[Any, Any], bool]] = {
    "==": operator.eq,
    "!=": operator.ne,
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
}
EDGES: dict[str, Callable[[Any, Any], Any] | None] = {
    "copy": copy,
    "calc+1": lambda s, d: calc(s + 1, d),
    "calc-1": lambda s, d: calc(s - 1, d),
    "calc+5": lambda s, d: calc(s + 5, d),
    "5-": lambda s, d: calc(5 - s, d),
    "neg": lambda s, d: calc(-s, d),
    "fill": None,
    "blockcopy": None,
}


def _build(
    source: str, target: str, edge: str, compare: tuple[str, Any] | None = None
) -> tuple[Program, str, str, Bool]:
    tag = TYPES[source]("S", external=True)
    hit = Bool("Hit")
    with Program(strict=False) as program:
        if edge == "fill":
            into = Block("Into", getattr(TagType, target.upper()), 1, 3)
            with Rung():
                fill(tag, into.select(1, 3))
            dest = into[2]
        elif edge == "blockcopy":
            start = Block("Start", getattr(TagType, source.upper()), 1, 3)
            into = Block("Into", getattr(TagType, target.upper()), 1, 3)
            tag = start[2]
            with Rung():
                blockcopy(start.select(1, 3), into.select(1, 3))
            dest = into[2]
        else:
            dest = TYPES[target]("D")
            edge_fn = EDGES[edge]
            assert edge_fn is not None
            with Rung():
                edge_fn(tag, dest)
        if compare is not None:
            with Rung(OPS[compare[0]](dest, compare[1])):
                out(hit)
    return program, tag.name, dest.name, hit


def _fires_at(program: Program, source: str, value: Any, hit: Bool) -> bool:
    """Whether a scan with *source* at *value* turns the coil on, the engines agreeing."""
    answers = []
    for engine in ENGINES:
        plc = engine(program, dt=0.01)
        plc.patch({source: value})
        plc.step()
        answers.append(bool(plc.current_state.tags.get(hit.name)))
    assert answers[0] == answers[1]
    return answers[0]


def _witness_fires(program: Program, verdict: Counterexample, hit: Bool) -> bool:
    for engine in ENGINES:
        plc = engine(program, dt=0.01)
        for step in verdict.trace:
            if step.inputs:
                plc.patch(dict(step.inputs))
            for _ in range(max(step.scans, 1)):
                plc.step()
        if not plc.current_state.tags.get(hit.name):
            return False
    return True


# The sweep goes through both ends of each store, and either side of each alias of them.
SWEEP = sorted(
    {
        *range(-33000, 33000, 3989),
        *range(-32772, -32764),
        *range(-8, 8),
        *range(32764, 32772),
        *range(39996, 40004),
        *range(65531, 65540),
        40000 - 65536,
        -40000,
        70000,
        4464,
        -65536,
        131072,
    }
)
FIRES = [
    pytest.param("Int", "Word", "copy", "==", 65535, -1, id="int to word, the wrap's top"),
    pytest.param("Int", "Word", "copy", ">=", 40000, -1, id="int to word, past the int"),
    pytest.param("Int", "Word", "copy", "==", 32768, -32768, id="int to word, one past the int"),
    pytest.param("Int", "Word", "copy", ">", 32767, -1, id="int to word, above the int"),
    pytest.param("Int", "Int", "calc+1", "==", -32768, 32767, id="i + 1 rolls over"),
    pytest.param("Int", "Int", "calc-1", "==", 32767, -32768, id="i - 1 rolls under"),
    pytest.param("Int", "Word", "calc+5", "==", 65535, -6, id="i + 5 to a word"),
    pytest.param("Int", "Word", "5-", "==", 65535, 6, id="5 - i to a word"),
    pytest.param("Int", "Word", "neg", "==", 65535, 1, id="-i to a word"),
    pytest.param("Word", "Int", "calc+1", "==", -1, 65534, id="word + 1 to an int, wraps"),
    pytest.param("Word", "Int", "copy", "==", 32767, 40000, id="word to int, clamps at the top"),
    pytest.param("Int", "Word", "fill", "==", 65535, -1, id="fill int to word"),
    pytest.param("Int", "Word", "blockcopy", "==", 65535, -1, id="blockcopy int to word"),
    pytest.param("Int", "Word", "blockcopy", ">=", 40000, -1, id="blockcopy, past the int"),
    pytest.param("Dint", "Word", "copy", "==", 65535, 65535, id="dint to word, as it is"),
    pytest.param("Dint", "Word", "copy", "==", 65535, -1, id="dint to word, by the wrap"),
    pytest.param("Dint", "Int", "calc+1", "==", -32768, 32767, id="dint to int, wraps"),
]


@pytest.mark.parametrize(("source", "target", "edge", "op", "bound", "value"), FIRES)
def test_a_value_only_the_store_can_reach_is_found(source, target, edge, op, bound, value) -> None:
    program, name, _, hit = _build(source, target, edge, (op, bound))
    assert _fires_at(program, name, value, hit)

    verdict = never(program, hit)
    assert isinstance(verdict, Counterexample)
    assert _witness_fires(program, verdict, hit)


UNREACHABLE = [
    pytest.param("Int", "Word", "copy", "==", 70000, id="past the word"),
    pytest.param("Int", "Word", "copy", ">", 65535, id="above the word"),
    pytest.param("Int", "Word", "copy", "<", 0, id="under the word"),
    pytest.param("Int", "Dint", "copy", ">", 40000, id="past the int, on a wider target"),
    pytest.param("Int", "Dint", "copy", "==", 32768, id="one past the int, on a wider target"),
    pytest.param("Word", "Int", "copy", "==", -1, id="word clamps, nothing negative"),
    pytest.param("Word", "Int", "copy", "<", 0, id="word clamps, under zero"),
    pytest.param("Int", "Int", "calc+1", ">", 32767, id="a wrap leaves nothing above"),
    pytest.param("Int", "Word", "fill", "==", 70000, id="fill, past the word"),
    pytest.param("Int", "Word", "blockcopy", "==", 70000, id="blockcopy, past the word"),
]


@pytest.mark.parametrize(("source", "target", "edge", "op", "bound"), UNREACHABLE)
def test_a_value_no_scan_leaves_is_still_proven(source, target, edge, op, bound) -> None:
    program, name, _, hit = _build(source, target, edge, (op, bound))
    assert not [value for value in SWEEP if _fires_at(program, name, value, hit)]
    assert isinstance(never(program, hit), Proven)


def _through_a_mid_tag(mid: str, target: str, op: str, bound: Any) -> tuple[Program, Bool]:
    source = Int("S", external=True)
    middle = TYPES[mid]("M")
    last = TYPES[target]("D")
    hit = Bool("Hit")
    with Program(strict=False) as program:
        with Rung():
            copy(source, middle)
        with Rung():
            copy(middle, last)
        with Rung(OPS[op](last, bound)):
            out(hit)
    return program, hit


@pytest.mark.parametrize(
    ("mid", "target", "op", "bound", "value"),
    [
        pytest.param("Dint", "Word", "==", 65535, -1, id="through a dint"),
        pytest.param("Dint", "Word", ">=", 40000, -1, id="past the int, through a dint"),
        pytest.param("Real", "Word", "==", 65535, -1, id="through a real"),
        pytest.param("Word", "Word", "==", 65535, -1, id="through a word"),
    ],
)
def test_a_wide_tag_on_the_way_passes_on_what_the_source_must_reach(
    mid, target, op, bound, value
) -> None:
    program, hit = _through_a_mid_tag(mid, target, op, bound)
    assert _fires_at(program, "S", value, hit)

    verdict = never(program, hit)
    assert isinstance(verdict, Counterexample)
    assert _witness_fires(program, verdict, hit)


def test_a_chain_of_stores_can_leave_nothing_to_find() -> None:
    """INT to WORD to INT: the WORD holds nothing under 0, and a copy to the INT clamps."""
    program, hit = _through_a_mid_tag("Word", "Int", "==", -1)
    assert not [value for value in SWEEP if _fires_at(program, "S", value, hit)]
    assert isinstance(never(program, hit), Proven)


def test_a_multiply_the_inverse_has_no_answer_for_is_not_proven_where_a_scan_fires() -> None:
    """``S * 3`` wraps to 1 at -21845, and the inverse of 1 has no whole answer."""
    source = Int("S", external=True)
    dest = Int("D")
    hit = Bool("Hit")
    with Program(strict=False) as program:
        with Rung():
            calc(source * 3, dest)
        with Rung(dest == 1):
            out(hit)
    assert _fires_at(program, "S", -21845, hit)

    verdict = never(program, hit)
    assert not isinstance(verdict, Proven)
    if isinstance(verdict, Counterexample):
        assert _witness_fires(program, verdict, hit)


MATRIX_BOUNDS = [-32768, -1, 0, 3.5, 32767, 32768, 40000, 65535, 70000]


@pytest.mark.parametrize("edge", list(EDGES))
@pytest.mark.parametrize("target", ["Int", "Word", "Dint", "Real"])
@pytest.mark.parametrize("source", ["Int", "Word"])
def test_every_verdict_agrees_with_a_scan(source: str, target: str, edge: str) -> None:
    """The whole grid, either way round: every Proven swept, every witness replayed."""
    program, name, dest, _ = _build(source, target, edge)
    plc = PLC(program, dt=0.01)
    compiled = CompiledPLC(program, dt=0.01)
    left = {}
    for value in SWEEP:
        held = []
        for runner in (plc, compiled):
            runner.patch({name: value})
            runner.step()
            held.append((runner.current_state.tags[name], runner.current_state.tags.get(dest)))
        assert held[0] == held[1]
        left[held[0][0]] = held[0][1]

    for op, compare in OPS.items():
        for bound in MATRIX_BOUNDS:
            built, _, _, hit = _build(source, target, edge, (op, bound))
            verdict = never(built, hit)
            label = f"{source}->{target} {edge} {op} {bound}"
            if isinstance(verdict, Proven):
                fires = [held for held, stored in left.items() if compare(stored, bound)]
                assert not fires, f"{label}: Proven, and a scan fires at {fires[:3]}"
            elif isinstance(verdict, Counterexample):
                assert _witness_fires(built, verdict, hit), f"{label}: {verdict.trace[-1].inputs}"
            else:
                assert isinstance(verdict, Intractable), label
