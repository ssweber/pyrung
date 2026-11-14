"""The temporary logic's rungs drop only terms that cannot change them."""

from __future__ import annotations

import itertools
import math
import random
from typing import Any

from pyrung import Bool, Int, Real, rise
from pyrung.core.condition import AllCondition, AnyCondition
from pyrung.core.synthesis import _ALWAYS, _NEVER, _simplified

A, B, C = Bool("A"), Bool("B"), Bool("C")
N = Int("N")
R = Real("R")


class _Values:
    def __init__(self, values: dict[str, Any]):
        self.values = values

    def get_tag(self, name: str, default: Any = None) -> Any:
        return self.values.get(name, default)


def _truth(condition: Any, values: dict[str, Any]) -> bool:
    if condition is _ALWAYS:
        return True
    if condition is _NEVER:
        return False
    return bool(condition.evaluate(_Values(values)))


def _random_condition(rng: random.Random, depth: int) -> Any:
    if depth == 0 or rng.random() < 0.3:
        return _leaf(rng)
    parts = [_random_condition(rng, depth - 1) for _ in range(rng.randint(2, 3))]
    return AllCondition(*parts) if rng.random() < 0.5 else AnyCondition(*parts)


def _leaf(rng: random.Random) -> Any:
    from pyrung.core.condition import BitCondition, NormallyClosedCondition

    return rng.choice(
        [
            BitCondition(A),
            NormallyClosedCondition(A),
            BitCondition(B),
            NormallyClosedCondition(B),
            BitCondition(C),
            NormallyClosedCondition(C),
            A == True,  # noqa: E712
            A != True,  # noqa: E712
            B == False,  # noqa: E712
            C != False,  # noqa: E712
            N == 1,
            N != 1,
            N < 2,
            N >= 2,
            N > 0,
            N <= 0,
            R < 2,
            R >= 2,
            R > 0,
            R <= 0,
            R == 1.0,
            R != 1.0,
        ]
    )


def test_a_simplified_condition_has_the_same_truth_table():
    rng = random.Random(20261107)
    # A Real holding NaN fails every ordered compare, R < 2 and R >= 2 alike.
    states = [
        {"A": a, "B": b, "C": c, "N": n, "R": r}
        for a, b, c, n, r in itertools.product(
            (False, True), (False, True), (False, True), range(-1, 4), (math.nan, -1.0, 1.0, 3.0)
        )
    ]
    shrunk = 0
    for _ in range(400):
        condition = _random_condition(rng, 3)
        simpler = _simplified(condition)
        for values in states:
            assert _truth(simpler, values) == _truth(condition, values), (condition, values)
        shrunk += simpler is not condition
    assert shrunk > 100


def test_a_contact_required_beside_an_or_drops_the_or():
    from pyrung.core.condition import BitCondition, NormallyClosedCondition

    condition = AllCondition(AnyCondition(N != 6, NormallyClosedCondition(A)), N == 6, ~A)
    simpler = _simplified(condition)

    assert type(simpler) is AllCondition
    assert len(simpler.conditions) == 2
    assert not isinstance(simpler.conditions[0], BitCondition)


def test_an_edge_contact_leaves_the_condition_as_it_is():
    condition = AllCondition(rise(A), AnyCondition(rise(A), B))

    assert _simplified(condition) is condition


def test_an_ordered_compare_on_a_real_is_no_complement_of_its_opposite():
    from pyrung.core.condition import CompareGe
    from pyrung.core.synthesis import _condition_complement

    either = AnyCondition(R < 5, R >= 5)
    nan = {"A": False, "B": True, "C": False, "N": 0, "R": math.nan}

    assert _simplified(either) is not _ALWAYS
    assert _truth(_simplified(either), nan) is False
    kept = AnyCondition(R < 5, AllCondition(R >= 5, B))
    assert _truth(_simplified(kept), nan) is False
    assert type(_condition_complement(N < 5)) is CompareGe


def test_a_compare_that_may_read_nan_complements_to_its_opposite_or_a_nan_check():
    from pyrung import Block, Char, TagType
    from pyrung.core.condition import CompareGe, CompareLt
    from pyrung.core.synthesis import _condition_complement

    S, M, Text = Real("S"), Int("M"), Char("Text")
    reals = Block("DF", TagType.REAL, 1, 3)
    ints = Block("DS", TagType.INT, 1, 3)
    conditions = [
        R < 5,
        R >= 5.0,
        N < S,
        R <= S,
        N > M * S,
        N < reals[M],
        AllCondition(B, R >= 5),
        AnyCondition(A, R > S),
    ]
    numbers = [math.nan, -1.0, 5.0, 7.5]
    for condition in conditions:
        complement = _condition_complement(condition)
        assert complement is not None, condition
        for r, s, at in itertools.product(numbers, numbers, numbers):
            values = {"A": False, "B": True, "N": 5, "M": 1, "R": r, "S": s, "DF1": at}
            assert _truth(complement, values) is not _truth(condition, values), (condition, values)
    assert type(_condition_complement(CompareLt(Text, "b"))) is CompareGe
    assert type(_condition_complement(N < M + 1)) is CompareGe
    assert type(_condition_complement(N < ints[M])) is CompareGe
    assert _condition_complement(CompareLt(R, math.nan)) is None
    # Two big finite products can overflow to inf, and inf - inf is NaN.
    overflow = N < M * 1e308 - M * 1e308
    complement = _condition_complement(overflow)
    assert type(complement) is AnyCondition
    big = {"N": 5, "M": 10}
    assert _truth(overflow, big) is False
    assert _truth(complement, big) is True


def test_a_nan_check_goes_beside_an_ordered_compare_on_the_same_tag():
    from pyrung.core.condition import CompareEq
    from pyrung.core.synthesis import _condition_complement

    # The complement of the complement of R < 5 holds R == R, which R < 5 already implies.
    twice = _simplified(_condition_complement(_condition_complement(R < 5)))
    assert repr(twice) == "R < 5"
    # With no ordered compare on R beside it, R == R is the only NaN check and stays.
    assert "R == R" in repr(_simplified(AllCondition(B, CompareEq(R, R))))
    for r in (math.nan, 1.0, 7.0):
        values = {"A": False, "B": True, "C": False, "N": 0, "R": r}
        assert _truth(twice, values) is (r < 5)


def test_a_real_holding_nan_fails_both_rules_and_the_tag_follows_the_other():
    from pyrung import PLC, Program, Rung, copy
    from pyrung.core.analysis.pilot.overlay import _set_synth_holds
    from pyrung.core.synthesis import guarded_copy_rung

    D = Bool("D")
    with Program(strict=False) as prog:
        with Rung(B, D, R > 100):
            copy(1, Int("Seen"))
    plc = PLC(prog, dt=0.01)
    plc.patch({"R": math.nan, "B": True})
    plc.step()
    _set_synth_holds(
        plc, [guarded_copy_rung([(D, True, B), (D, False, AnyCondition(R < 5, R >= 5))])]
    )

    plc.step()

    assert plc.state.tags["D"] is True
