"""What never() carries back from a comparison, against what a scan leaves on the target.

A target's store changes what it was given.  A WORD holds 65535 for the -1 an INT copies to it,
a calc to an INT holds -32768 for the 32767 of ``I + 1``.  The source has to try the value that
gets there, so each row here is stepped on both engines as well as asked of the verifier: a
Proven is checked against a sweep and a witness is replayed.  A decline is an honest answer,
and no row may be Proven where a scan fires.

The brute force at the end runs every value of an INT or WORD source through the store path,
over programs generated from the literals that have got this wrong before.
"""

from __future__ import annotations

import math as pymath
import operator
from collections.abc import Callable, Iterable
from typing import Any, NamedTuple

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
    copy,
    fill,
    latch,
    math,
    out,
    unpack_to_bits,
)
from pyrung.core.analysis.prove import Counterexample, Intractable, Proven, always, never
from pyrung.core.compiled_plc import CompiledPLC
from pyrung.core.expression import BinaryExpr, LiteralExpr, TagExpr, UnaryExpr
from pyrung.core.instruction.calc import CalcInstruction
from pyrung.core.instruction.conversions import (
    _store_copy_value_to_tag_type,
    _truncate_to_tag_type,
    copy_source_kind,
)
from pyrung.core.instruction.data_transfer import CopyInstruction
from pyrung.core.runner import PLC
from pyrung.core.validation._common import walk_instructions

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
    "calc+1": lambda s, d: math(s + 1, d),
    "calc-1": lambda s, d: math(s - 1, d),
    "calc+5": lambda s, d: math(s + 5, d),
    "5-": lambda s, d: math(5 - s, d),
    "neg": lambda s, d: math(-s, d),
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
    pytest.param("Word", "Int", "copy", "==", -25536, 40000, id="word to int, keeps its bits"),
    pytest.param("Word", "Int", "copy", "==", -1, 65535, id="word to int, the top is -1"),
    pytest.param("Word", "Int", "copy", "<", 0, 40000, id="word to int, under zero"),
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
    pytest.param("Word", "Int", "copy", "==", 40000, id="word to int, nothing past the int"),
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
        pytest.param("Word", "Int", "==", -1, -1, id="through a word, back into an int"),
    ],
)
def test_a_chain_with_a_wrap_on_it_says_no_scan_only_with_what_it_tried(
    mid, target, op, bound, value
) -> None:
    program, hit = _through_a_mid_tag(mid, target, op, bound)
    assert _fires_at(program, "S", value, hit)

    verdict = never(program, hit)
    assert not isinstance(verdict, Proven)
    if isinstance(verdict, Counterexample):
        assert _witness_fires(program, verdict, hit)
    else:
        assert verdict.tags == ["S"]


def test_a_chain_of_stores_that_leaves_nothing_to_find_is_not_claimed() -> None:
    """INT to WORD to DINT: nothing fires, but a chain's values aren't known to be all of them."""
    program, hit = _through_a_mid_tag("Word", "Dint", "==", -1)
    assert not [value for value in SWEEP if _fires_at(program, "S", value, hit)]
    verdict = never(program, hit)
    assert isinstance(verdict, Intractable)
    assert verdict.tags == ["S"]
    assert "S" in verdict.reason and "M" in verdict.reason


@pytest.mark.parametrize(
    ("source", "bound"), [pytest.param(Int, 7, id="int"), pytest.param(Dint, 7, id="dint")]
)
def test_a_feeder_of_a_declined_hop_has_no_answer_either(source, bound) -> None:
    """Its comparison gives a tag a domain, and that is no reason to believe the search."""
    feeder = source("F", external=True)
    middle = Real("M")
    last = Int("D")
    hit = Bool("Hit")
    with Program(strict=False) as program:
        with Rung():
            copy(feeder, middle)
        with Rung():
            math(middle * 3, last)
        with Rung(last == bound, feeder != 0):
            out(hit)
    assert isinstance(never(program, hit), Intractable)


def test_a_declared_real_is_not_all_its_whole_numbers() -> None:
    """``R * 2`` reaches 5 at 2.5, between the values a declared range gives it."""
    source = Real("R", external=True, min=0.0, max=10.0)
    last = Word("D")
    hit = Bool("Hit")
    with Program(strict=False) as program:
        with Rung():
            math(source * 2, last)
        with Rung(last == 5):
            out(hit)
    assert _fires_at(program, "R", 2.5, hit)
    assert not isinstance(never(program, hit), Proven)


@pytest.mark.parametrize(
    ("into_the_bit", "off_at"),
    [
        pytest.param(copy, 0, id="copy"),
        pytest.param(lambda s, d: math(s + 0, d), 0, id="math"),
        pytest.param(lambda s, d: math(s - 1000, d), 1000, id="math, off at 1000"),
    ],
)
def test_a_number_through_a_bit_is_not_proven_where_a_scan_fires(into_the_bit, off_at) -> None:
    """A bit is on for every nonzero number, so the source has two classes past it, and
    ``Hit`` needs both: ``D > 0.5`` latches ``Seen``, then ``D <= 0.5`` needs the source
    back at its one off value.

    Defence in depth for the ``"bool"`` decline in ``_through_the_store``: with it removed
    these still pass, as did every variant tried (offsets, slopes 2, 3, -1, 0.5 and 1/100,
    a Real source, ``fall(Bit)``): a slope-1 inverse of a boundary between 0 and 1, give or
    take one, lands on the off value, and the other slopes tried were declined elsewhere.
    """
    source = Int("S", external=True)
    bit = Bool("Bit")
    last = Real("D")
    seen = Bool("Seen")
    hit = Bool("Hit")
    with Program(strict=False) as program:
        with Rung():
            into_the_bit(source, bit)
        with Rung():
            copy(bit, last)
        with Rung(last > 0.5):
            latch(seen)
        with Rung(seen, last <= 0.5):
            out(hit)
    for engine in ENGINES:
        plc = engine(program, dt=0.01)
        plc.patch({"S": off_at + 7})
        plc.step()
        plc.patch({"S": off_at})
        plc.step()
        assert plc.current_state.tags.get(hit.name)

    verdict = never(program, hit)
    assert not isinstance(verdict, Proven)
    if isinstance(verdict, Counterexample):
        assert _witness_fires(program, verdict, hit)


@pytest.mark.parametrize(
    ("dest_type", "build", "compare"),
    [
        pytest.param(Bool, lambda s, d: math(s - 0.5, d), lambda d: ~d, id="math into a bit"),
        pytest.param(Int, lambda s, d: math(s * s * 4 - 1, d), lambda d: d == 0, id="square"),
    ],
)
def test_a_declared_real_into_a_calc_with_no_way_back_is_not_proven(
    dest_type, build, compare
) -> None:
    """Both fire at 0.5, inside the declared range: the whole numbers of it are not enough."""
    source = Real("S", external=True, min=0.0, max=10.0)
    dest = dest_type("D")
    hit = Bool("Hit")
    with Program(strict=False) as program:
        with Rung():
            build(source, dest)
        with Rung(compare(dest)):
            out(hit)
    assert _fires_at(program, "S", 0.5, hit)
    assert not isinstance(never(program, hit), Proven)


def test_a_multiply_the_inverse_has_no_answer_for_is_not_proven_where_a_scan_fires() -> None:
    """``S * 3`` wraps to 1 at -21845, and the inverse of 1 has no whole answer."""
    source = Int("S", external=True)
    dest = Int("D")
    hit = Bool("Hit")
    with Program(strict=False) as program:
        with Rung():
            math(source * 3, dest)
        with Rung(dest == 1):
            out(hit)
    assert _fires_at(program, "S", -21845, hit)

    verdict = never(program, hit)
    assert not isinstance(verdict, Proven)
    if isinstance(verdict, Counterexample):
        assert _witness_fires(program, verdict, hit)


# Each of these was Proven with the witness firing.  Asked of the verifier and stepped on both
# engines: the witness has to fire, and the verdict has to be anything but Proven.
_I = Int("I", external=True)
_DI = Int("DI")
_DD = Dint("DD")
_DA = Real("DA")
_DB = Real("DB")
_A = Int("A")
_B = Int("B")
_T = Int("T")
_J = Int("J", external=True)
_R = Real("R", external=True)
_RB = Real("Rb", external=True, min=0.0, max=100.0)
_MODE = Bool("Mode", external=True)
_HIT = Bool("Hit")


def _rungs(*pairs: tuple[Any, ...]) -> Program:
    """A program of ``(body, *conditions)`` rungs."""
    with Program(strict=False) as program:
        for body, *conditions in pairs:
            with Rung(*conditions):
                body()
    return program


def _hit() -> None:
    out(_HIT)


def _scale_select() -> Program:
    return _rungs(
        (lambda: copy(_I, _DI), _MODE),
        (lambda: math(_I * 10, _DI), ~_MODE),
        (_hit, ~_MODE, _DI == 500),
    )


def _copy_then_calc() -> Program:
    return _rungs((lambda: copy(_I, _DI),), (lambda: math(_I + 100, _DI),), (_hit, _DI == 5000))


def _calc_then_copy() -> Program:
    return _rungs((lambda: math(_I + 100, _DI),), (lambda: copy(_I, _DI),), (_hit, _DI == 5000))


def _copy_then_double() -> Program:
    return _rungs((lambda: copy(_I, _DI),), (lambda: math(_I * 2, _DI),), (_hit, _DI == 6))


def _diamond() -> Program:
    return _rungs(
        (lambda: copy(_J, _A),),
        (lambda: math(_J + 100, _B),),
        (lambda: copy(_A, _T), _MODE),
        (lambda: copy(_B, _T), ~_MODE),
        (_hit, ~_MODE, _T == 5000),
    )


def _through_a_step(source: Any, dest: Any, *conditions: Any) -> Program:
    return _rungs((lambda: copy(source, dest),), (_hit, *conditions))


def _two_hops(source: Any, first: float, second: float) -> Program:
    return _rungs(
        (lambda: copy(source, _DA),),
        (lambda: copy(source, _DB),),
        (_hit, _DA > first, _DB < second),
    )


def _a_sum_and_a_copy() -> Program:
    return _rungs(
        (lambda: math(_R + 1, _DA),),
        (lambda: copy(_R, _DB),),
        (_hit, _DA > 2.5, _DB < 1.6),
    )


def _a_copy_and(calc_of: Callable[[Any], Any], source: Any, compared: Any) -> Program:
    """A hop gives the source its domain, and the other edge has no way back."""
    return _rungs(
        (lambda: copy(source, _DI),),
        (lambda: math(calc_of(source), _DD),),
        (_hit, _DI > 0, _DD == compared),
    )


def _a_real_copied_to_two_tags(source: Any, whole: Any, other: Any) -> Program:
    """5.5 is 5 on the INT and still 5.5 on the REAL, and nothing compares it there."""
    return _rungs(
        (lambda: copy(source, whole),),
        (lambda: copy(source, other),),
        (_hit, other > 5, whole == 5),
    )


def _a_real_copied_to_two_tags_across_rungs() -> Program:
    z = Bool("Z")
    return _rungs(
        (lambda: copy(_RB, _DI),),
        (lambda: copy(_RB, _DB),),
        (lambda: out(z), _DB > 10),
        (_hit, z, _DI <= 10),
    )


def _a_square_compared_a_hop_later(source: Any, then: Callable[[Any, Any], None]) -> Program:
    """The tag the calc writes is compared by way of another, and so was carried back to."""
    later = Dint("Later")
    return _rungs(
        (lambda: copy(source, _DI),),
        (lambda: math(source * source, _DD),),
        (lambda: then(_DD, later),),
        (_hit, _DI > 0, later == 49),
    )


def _a_square_through_a_chain() -> Program:
    return _rungs(
        (lambda: copy(_I, _A),),
        (lambda: copy(_A, _DI),),
        (lambda: math(_A * _A, _DD),),
        (_hit, _DI > 0, _DD == 49),
    )


UNEVALUATED = [
    pytest.param(lambda: _a_copy_and(lambda s: s * s, _I, 49), "I", 7, id="copy-and-square"),
    pytest.param(
        lambda: _a_copy_and(lambda s: s * s + s * s, _I, 98), "I", 7, id="copy-and-no-tag-on-a-side"
    ),
    pytest.param(
        lambda: _a_copy_and(lambda s: (s + 1) * (s + 1), _I, 64), "I", 7, id="copy-and-nested"
    ),
    pytest.param(
        lambda: _a_copy_and(lambda s: s * s, Int("Ib2", external=True, min=0, max=100), 49),
        "Ib2",
        7,
        id="copy-and-square-declared",
    ),
    pytest.param(
        lambda: _rungs(
            (lambda: copy(_R, _DI),),
            (lambda: math(_R * _R, _DD),),
            (_hit, _DD > 30, _DI == 5),
        ),
        "R",
        5.7,
        id="copy-and-square-real",
    ),
    pytest.param(_a_square_through_a_chain, "I", 7, id="square-through-a-chain"),
    pytest.param(lambda: _a_square_compared_a_hop_later(_I, copy), "I", 7, id="square-then-copy"),
    pytest.param(
        lambda: _a_square_compared_a_hop_later(_I, lambda s, d: math(s + 0, d)),
        "I",
        7,
        id="square-then-sum",
    ),
    pytest.param(
        lambda: _a_square_compared_a_hop_later(Int("Ib2", external=True, min=0, max=100), copy),
        "Ib2",
        7,
        id="square-then-copy-declared",
    ),
    pytest.param(lambda: _a_real_copied_to_two_tags(_R, _DI, _DB), "R", 5.5, id="real-two-tags"),
    pytest.param(_a_real_copied_to_two_tags_across_rungs, "Rb", 10.5, id="real-two-tags-rungs"),
    pytest.param(_scale_select, "I", 50, id="scale-select"),
    pytest.param(_copy_then_calc, "I", 4900, id="copy-then-calc"),
    pytest.param(_calc_then_copy, "I", 5000, id="calc-then-copy"),
    pytest.param(_copy_then_double, "I", 3, id="copy-then-double"),
    pytest.param(_diamond, "J", 4900, id="diamond"),
    pytest.param(lambda: _through_a_step(_R, _DI, _R > 5.5, _DI == 5), "R", 5.7, id="step-above"),
    pytest.param(
        lambda: _through_a_step(_R, _DI, _R < -5.5, _DI == -5), "R", -5.7, id="step-below"
    ),
    pytest.param(lambda: _through_a_step(_R, _DD, _R > 5.5, _DD == 5), "R", 5.7, id="step-dint"),
    pytest.param(
        lambda: _through_a_step(_RB, _DI, _RB > 5.5, _DI == 5), "Rb", 5.7, id="step-declared"
    ),
    pytest.param(lambda: _through_a_step(_R, _DI, _R > 5, _DI == 5), "R", 5.5, id="step-whole"),
    pytest.param(lambda: _two_hops(_R, 1.5, 1.6), "R", 1.55, id="two-hops"),
    pytest.param(_a_sum_and_a_copy, "R", 1.55, id="sum-and-copy"),
    pytest.param(lambda: _two_hops(_RB, 1.5, 1.6), "Rb", 1.55, id="two-hops-declared"),
]


@pytest.mark.parametrize(("build", "source", "value"), UNEVALUATED)
def test_a_carried_set_no_edge_answered_for_is_not_proven(build, source, value) -> None:
    program = build()
    assert _fires_at(program, source, value, _HIT)
    verdict = never(program, _HIT, max_states=3000, depth_budget=6)
    assert not isinstance(verdict, Proven)
    if isinstance(verdict, Counterexample):
        assert _witness_fires(program, verdict, _HIT)


def _a_square_squared() -> Program:
    """The calc's target is not compared and nothing is carried back to it, only to what it feeds."""
    squared = Dint("Squared")
    return _rungs(
        (lambda: copy(_I, _DI),),
        (lambda: math(_I * _I, _DD),),
        (lambda: math(_DD * _DD, squared),),
        (_hit, _DI > 0, squared == 2401),
    )


def _unpacked_too() -> Program:
    bits = Block("Bits", TagType.BOOL, 1, 16)
    return _rungs(
        (lambda: copy(_I, _DI),),
        (lambda: unpack_to_bits(_I, bits.select(1, 16)),),
        (_hit, _DI > 0, bits[4]),
    )


def _a_pointer_too() -> Program:
    table = Block("Arr", TagType.INT, 1, 5)
    return _rungs(
        (lambda: copy(42, table[3]),),
        (lambda: copy(_I, _DI),),
        (lambda: copy(table[_I], _DD),),
        (_hit, _DI > 0, _DD == 42),
    )


# A carried source's other way to the comparison goes through something the carried set
# does not look at: the source reaches more than its own comparisons with a literal.
ANOTHER_WAY_THERE = pytest.mark.parametrize(
    ("build", "value"),
    [
        pytest.param(_a_square_squared, 7, id="a-calc-of-a-calc"),
        pytest.param(_unpacked_too, 8, id="unpack-to-bits"),
        pytest.param(_a_pointer_too, 3, id="a-pointer"),
    ],
)


@ANOTHER_WAY_THERE
def test_the_scans_another_way_there_reaches_do_fire(build, value) -> None:
    assert _fires_at(build(), "I", value, _HIT)


@ANOTHER_WAY_THERE
def test_a_carried_source_with_another_way_to_the_comparison_is_not_proven(build, value) -> None:
    verdict = never(build(), _HIT, max_states=3000, depth_budget=6)
    assert not isinstance(verdict, Proven)


def _unread_by_the_rungs() -> Program:
    return _rungs((lambda: out(Bool("Y")), _I > 100))


def _read_after_the_store() -> Program:
    return _rungs(
        (lambda: out(Bool("Z")), _I > 100),
        (lambda: copy(_I, Word("DW")),),
    )


def _a_real_it_has_only_tried_whole_numbers_of() -> Program:
    return _rungs((lambda: out(Bool("Y")), _RB > 100))


def _copied_above_a_comparison() -> Program:
    return _rungs((lambda: copy(_I, _DI), _I > 100))


def _copied_and_compared() -> Program:
    return _rungs((lambda: copy(_I, _DI),), (lambda: out(Bool("Z")), _DI > 5))


def _copied_through_a_tag() -> Program:
    return _rungs((lambda: copy(_I, _A),), (lambda: copy(_A, _DI),))


SCOPED = [
    pytest.param(_copied_above_a_comparison, 150, ["DI"], lambda s: s["DI"] != 150, id="guarded"),
    pytest.param(_copied_and_compared, 7, ["DI", "Z"], lambda s: s["DI"] != 7, id="compared"),
    pytest.param(_copied_through_a_tag, 7, ["DI"], lambda s: s["DI"] != 7, id="chain"),
]


@pytest.mark.parametrize(("build", "value", "scope", "holds"), SCOPED)
def test_a_scoped_callable_answers_for_the_inputs_a_tag_in_it_is_given(
    build, value, scope, holds
) -> None:
    """``scope=["DI"]`` reads no input by name, and ``s["DI"]`` tells I apart all the same."""
    program = build()
    for engine in ENGINES:
        plc = engine(program, dt=0.01)
        plc.patch({"I": value})
        plc.step()
        plc.patch({"I": value})
        plc.step()
        assert not holds(dict(plc.current_state.tags))
    verdict = always(program, holds, scope=scope, max_states=3000, depth_budget=6)
    assert not isinstance(verdict, Proven)


def test_a_scoped_callable_on_a_bool_a_comparison_decides_still_proves() -> None:
    """A comparison decides it, and comparisons are carried back: what is tried answers."""
    z = Bool("Z")
    direct = _rungs((lambda: out(z), _I > 100))
    through_a_copy = _rungs((lambda: copy(_I, _DI),), (lambda: out(z), _DI > 5))
    for program in (direct, through_a_copy):
        verdict = always(
            program, lambda s: s.get("Z") in (True, False), scope=["Z"], max_states=3000
        )
        assert isinstance(verdict, Proven)


def test_a_scoped_callable_on_a_tag_given_a_declared_input_still_proves() -> None:
    small = Int("Small", external=True, min=0, max=20)
    program = _rungs((lambda: copy(small, _DI),))
    verdict = always(program, lambda s: s.get("DI", 0) <= 20, scope=["DI"], max_states=3000)
    assert isinstance(verdict, Proven)


OPAQUE = [
    pytest.param(_unread_by_the_rungs, "I", 7, lambda s: s["I"] != 7, id="direct"),
    pytest.param(_read_after_the_store, "I", -1, lambda s: s.get("DW") != 65535, id="stored"),
    pytest.param(
        _a_real_it_has_only_tried_whole_numbers_of,
        "Rb",
        7.5,
        lambda s: s["Rb"] != 7.5,
        id="declared-real",
    ),
]


@pytest.mark.parametrize(("build", "source", "value", "holds"), OPAQUE)
def test_a_callable_is_not_answered_for_by_a_partition(build, source, value, holds) -> None:
    """It names no comparison to carry back, so the search has no reason to try its value."""
    program = build()
    for engine in ENGINES:
        plc = engine(program, dt=0.01)
        plc.patch({source: value})
        plc.step()
        assert not holds(dict(plc.current_state.tags))
    verdict = always(program, holds, max_states=3000, depth_budget=6)
    assert not isinstance(verdict, Proven)


def test_the_reason_a_callable_is_declined_names_the_input_and_what_to_do() -> None:
    verdict = always(_unread_by_the_rungs(), lambda s: s["I"] != 7, max_states=3000)
    assert isinstance(verdict, Intractable)
    assert "callable property" in verdict.reason
    assert verdict.tags == ["I"]
    assert "choices=" in verdict.hints[0]


def test_what_the_hint_says_narrowing_does_is_what_it_does() -> None:
    """Every value is tried under choices=, or under min=/max= no comparison narrows."""
    hint = always(_unread_by_the_rungs(), lambda s: s["I"] != 7, max_states=3000).hints[0]
    assert "at most 1000 values" in hint
    assert "no comparison narrows" in hint

    within = Int("Within", external=True, min=0, max=999)
    over_the_cap = Int("Over", external=True, min=0, max=1000)
    chosen = Int("Chosen", external=True, choices={0: "a", 5: "b", 20: "c"})
    assert isinstance(
        always(_rungs((lambda: copy(within, _DI),)), lambda s: s["Within"] != 7, max_states=3000),
        Counterexample,
    )
    assert isinstance(
        always(_rungs((lambda: copy(over_the_cap, _DI),)), lambda s: s["Over"] != 7),
        Intractable,
    )
    assert isinstance(
        always(_rungs((_hit, chosen > 5)), lambda s: s["Chosen"] != 20, max_states=3000),
        Counterexample,
    )


def test_a_callable_is_still_answered_for_where_every_value_was_tried() -> None:
    """Nothing narrows a small declared INT that is only copied, nor a tag's choices."""
    small = Int("Small", external=True, min=0, max=20)
    copied = _rungs((lambda: copy(small, Int("Z")),))
    assert isinstance(always(copied, lambda s: s["Small"] <= 20, max_states=3000), Proven)
    assert isinstance(always(copied, lambda s: s["Small"] != 7, max_states=3000), Counterexample)

    chosen = Int("Chosen", external=True, choices={0: "a", 1: "b", 2: "c"})
    compared = _rungs((_hit, chosen == 1))
    assert isinstance(always(compared, lambda s: s["Chosen"] in (0, 1, 2)), Proven)
    assert isinstance(always(compared, lambda s: s["Chosen"] != 2), Counterexample)


def test_a_comparison_narrows_what_a_callable_can_be_answered_for() -> None:
    """``Small > 5`` leaves the search 4, 5, 6 and the ends, and 7 is not among them."""
    small = Int("Small", external=True, min=0, max=20)
    program = _rungs((_hit, small > 5))
    assert isinstance(always(program, lambda s: s["Small"] != 7, max_states=3000), Intractable)


def test_the_same_edge_twice_still_proves() -> None:
    """A second rung copying the same way asks for the values the first one tried."""
    program = _rungs(
        (lambda: copy(_I, _DI), _MODE),
        (lambda: copy(_I, _DI), ~_MODE),
        (_hit, _I > 5, _DI < 3),
    )
    assert isinstance(never(program, _HIT, max_states=3000, depth_budget=6), Proven)


def test_a_declared_real_compared_once_still_proves() -> None:
    """One hop and no comparison of its own: nothing here needs a cell the search skips."""
    scaled = Real("ScaledR")
    program = _rungs((lambda: math(_RB * 2, scaled),), (_hit, scaled > 250))
    assert isinstance(never(program, _HIT, max_states=3000, depth_budget=6), Proven)


def test_two_hops_on_a_real_with_room_between_them_still_prove() -> None:
    """Over the two hops every cell has a value, and none of them fires."""
    program = _rungs(
        (lambda: copy(_RB, _DA),),
        (lambda: copy(_RB, _DB),),
        (_hit, _DA > 60, _DB < 50),
    )
    assert isinstance(never(program, _HIT, max_states=3000, depth_budget=6), Proven)


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


class Edge(NamedTuple):
    label: str
    write: Callable[[Any, Any], Any]
    literal: Any = None


def _copy_edge() -> Edge:
    return Edge("copy", copy)


def _edges(literals: Iterable[Any], factors: Iterable[Any]) -> list[Edge]:
    edges = [_copy_edge(), Edge("-s", lambda s, d: math(-s, d))]
    for k in literals:
        edges.append(Edge(f"s+{k}", lambda s, d, k=k: math(s + k, d), k))
        edges.append(Edge(f"s-{k}", lambda s, d, k=k: math(s - k, d), k))
        edges.append(Edge(f"{k}-s", lambda s, d, k=k: math(k - s, d), k))
    for k in factors:
        edges.append(Edge(f"s*{k}", lambda s, d, k=k: math(s * k, d), k))
    return edges


def _evaluate(node: Any, value: Any) -> Any:
    if isinstance(node, TagExpr):
        return value
    if isinstance(node, LiteralExpr):
        return node.value
    if isinstance(node, BinaryExpr):
        return node.op(_evaluate(node.left, value), _evaluate(node.right, value))
    if isinstance(node, UnaryExpr):
        return node.op(_evaluate(node.operand, value))
    return value if hasattr(node, "name") else node


def _stored(instruction: Any, value: Any) -> Any:
    """What *instruction* leaves on its target for *value*, by the store functions it calls."""
    if isinstance(instruction, CalcInstruction):
        given = _evaluate(instruction.expression, value)
        return _truncate_to_tag_type(given, instruction.dest, instruction.mode)
    assert isinstance(instruction, CopyInstruction)
    return _store_copy_value_to_tag_type(
        value, instruction.dest, copy_source_kind(instruction.source)
    )


class Built(NamedTuple):
    program: Program
    source: str
    last: str
    hit: Bool
    writes: tuple[Any, ...]


def _build_hops(
    source: str, hops: tuple[tuple[Edge, str], ...], compare: tuple[str, Any] | None = None
) -> Built:
    tag = TYPES[source]("S", external=True)
    hit = Bool("Hit")
    with Program(strict=False) as program:
        current: Any = tag
        for index, (edge, target) in enumerate(hops):
            into = TYPES[target](f"T{index}")
            with Rung():
                edge.write(current, into)
            current = into
        if compare is not None:
            with Rung(OPS[compare[0]](current, compare[1])):
                out(hit)
    writes = tuple(
        instruction
        for instruction in walk_instructions(program)
        if isinstance(instruction, CalcInstruction | CopyInstruction)
    )
    return Built(program, tag.name, current.name, hit, writes)


def _source_values(source: str, hops: tuple[tuple[Edge, str], ...]) -> Iterable[Any]:
    """Every value of an INT or WORD; for a DINT or REAL the edges, the turns and the literals."""
    if source == "Int":
        return range(-32768, 32768)
    if source == "Word":
        return range(65536)
    picks: set[Any] = {0, 1, -1, 5, -5, 32767, 32768, -32768, 65535, 65536, 2**31 - 1, -(2**31)}
    for turn in (-3, -2, -1, 1, 2, 3):
        for side in (-2, -1, 0, 1, 2):
            picks.update((turn * 65536 + side, turn * 32768 + side, turn * 2**31 + side))
    for edge, _ in hops:
        if isinstance(edge.literal, int | float) and not isinstance(edge.literal, bool):
            for turn in (-2, -1, 0, 1, 2):
                for near in (0, 5, -1, 65535):
                    picks.add(near + turn * 65536 - edge.literal)
                    picks.add(near + turn * 65536 + edge.literal)
    if source == "Dint":
        return sorted(int(pick) for pick in picks if -(2**31) <= pick < 2**31)
    return sorted({*(float(pick) for pick in picks), 0.5, -0.5, 2.5, 3.5, 1e10, -1e10})


def _left_on_the_target(built: Built, values: Iterable[Any]) -> dict[Any, Any]:
    left = {}
    for value in values:
        held = value
        for write in built.writes:
            held = _stored(write, held)
        left[value] = held
    return left


def _bounds_of(left: dict[Any, Any]) -> list[Any]:
    """Comparison values a target can hold at its ends, and the ones a store has got wrong."""
    held = [value for value in left.values() if isinstance(value, int | float)]
    finite = [value for value in held if pymath.isfinite(value)]
    if not finite:
        return [5]
    low, high = min(finite), max(finite)
    return sorted({low, low + 1, high, high - 1, 5, -1, 3.5, 32768, 65535, 70000})


def _verdict_row(
    source: str,
    hops: tuple[tuple[Edge, str], ...],
    compare: tuple[str, Any],
    left: dict[Any, Any],
) -> str:
    """One row's verdict, against what the store leaves for each value of the source."""
    program, _, _, hit, _ = _build_hops(source, hops, compare)
    fires = [value for value, held in left.items() if OPS[compare[0]](held, compare[1])]
    verdict = never(program, hit, max_states=3000, depth_budget=6)
    if isinstance(verdict, Proven):
        assert not fires, f"{compare}: Proven, and a scan fires at {source}={fires[:3]}"
        return "proven"
    if isinstance(verdict, Counterexample):
        assert _witness_fires(program, verdict, hit), f"{compare}: {verdict.trace}"
        return "counterexample"
    assert isinstance(verdict, Intractable)
    return "declined firing" if fires else "declined nothing fires"


def _check_store_against_stepping(
    built: Built, left: dict[Any, Any], values: Iterable[Any]
) -> None:
    """The store functions the sweep calls are the ones a scan ends up with, on both engines."""
    for engine in ENGINES:
        plc = engine(built.program, dt=0.01)
        for value in values:
            plc.patch({built.source: value})
            plc.step()
            held = plc.current_state.tags[built.last]
            assert held == left[value], f"{engine.__name__} {built.source}={value}: {held}"


def _sample_of(left: dict[Any, Any]) -> list[Any]:
    values = list(left)
    step = max(len(values) // 8, 1)
    return [values[0], values[-1], *values[step // 2 :: step]]


def run_hops(source: str, hops: tuple[tuple[Edge, str], ...], rows: int | None = None) -> list[str]:
    """Every (or the first *rows*) comparison on one program: a verdict for each."""
    built = _build_hops(source, hops)
    left = _left_on_the_target(built, _source_values(source, hops))
    _check_store_against_stepping(built, left, _sample_of(left))
    compares = [(op, bound) for bound in _bounds_of(left) for op in ("==", "<", ">")]
    return [_verdict_row(source, hops, compare, left) for compare in compares[:rows]]


LITERALS = (1, -3, 32767, 65536, 70000, 2147483547, 1.5, -0.5, 100000.5)
FACTORS = (3, -1, 1000, 0.5)


@pytest.mark.parametrize("source", ["Int", "Word", "Dint", "Real"])
def test_one_hop_has_no_false_proven(source: str) -> None:
    verdicts: list[str] = []
    for edge in _edges(LITERALS, FACTORS)[:: 2 if source in ("Dint", "Real") else 3]:
        for target in ("Int", "Word", "Dint", "Real"):
            verdicts += run_hops(source, ((edge, target),), rows=9)
    assert {"proven", "counterexample"} <= set(verdicts)


TWO_HOPS = [
    pytest.param("Int", (("s+100000", "Dint"), ("copy", "Word")), id="i + 100000, dint, word"),
    pytest.param("Int", (("copy", "Real"), ("s+100000", "Word")), id="i, real, + 100000, word"),
    pytest.param("Int", (("s+200000", "Dint"), ("s+1", "Int")), id="i + 200000, dint, + 1, int"),
    pytest.param("Int", (("s*3", "Dint"), ("copy", "Word")), id="i * 3, dint, word"),
    pytest.param("Word", (("s+0.5", "Real"), ("copy", "Dint")), id="w + 0.5, real, dint"),
    pytest.param("Word", (("s*0.5", "Real"), ("copy", "Int")), id="w * 0.5, real, int"),
    pytest.param("Int", (("copy", "Word"), ("copy", "Int")), id="i, word, int"),
    pytest.param("Int", (("copy", "Dint"), ("s+1", "Int")), id="i, dint, + 1, int"),
    pytest.param("Int", (("s+1", "Int"), ("s+1", "Int")), id="i + 1, + 1"),
    pytest.param("Int", (("copy", "Dint"), ("copy", "Int")), id="i, dint, int: no wrap"),
    pytest.param("Word", (("s+1", "Int"), ("copy", "Word")), id="w + 1, int, word"),
    pytest.param("Word", (("copy", "Dint"), ("s+70000", "Int")), id="w, dint, + 70000, int"),
    pytest.param("Dint", (("s+1", "Dint"), ("copy", "Int")), id="d + 1, dint, int"),
    pytest.param("Real", (("copy", "Dint"), ("copy", "Word")), id="r, dint, word"),
]


@pytest.mark.parametrize(("source", "hops"), TWO_HOPS)
def test_two_hops_have_no_false_proven(source: str, hops) -> None:
    named = {edge.label: edge for edge in _edges((1, 100000, 200000, 70000, 0.5), (3, 0.5))}
    run_hops(source, tuple((named[label], target) for label, target in hops))


def test_a_plain_chain_still_proves() -> None:
    """Nothing on it wraps, so what it carries back is all there is."""
    source = Int("S", external=True)
    middle = Dint("M")
    last = Int("D")
    hit = Bool("Hit")
    with Program(strict=False) as program:
        with Rung():
            copy(source, middle)
        with Rung():
            copy(middle, last)
        with Rung(last == 70000):
            out(hit)
    assert isinstance(never(program, hit), Proven)


@pytest.mark.parametrize(
    ("write", "bound", "value"),
    [
        pytest.param(lambda s, d: math(s + 1.5, d), -32768, 32767, id="i + 1.5, rolls over"),
        pytest.param(lambda s, d: math(s - 0.5, d), -1, -1, id="i - 0.5 truncates to nothing"),
        pytest.param(lambda s, d: math(s + 65536.5, d), 5, 5, id="i + 65536.5"),
        pytest.param(lambda s, d: math(s + 100000.5, d), 5, 31077, id="i + 100000.5"),
    ],
)
def test_a_fractional_literal_truncates_before_it_wraps(write, bound, value) -> None:
    source = Int("S", external=True)
    last = Int("D")
    hit = Bool("Hit")
    with Program(strict=False) as program:
        with Rung():
            write(source, last)
        with Rung(last == bound):
            out(hit)
    assert _fires_at(program, "S", value, hit)
    assert not isinstance(never(program, hit), Proven)
