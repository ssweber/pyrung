"""The temporary logic's rungs drop only terms that cannot change them."""

from __future__ import annotations

import itertools
import random
from typing import Any

from pyrung import Bool, Int, rise
from pyrung.core.condition import AllCondition, AnyCondition
from pyrung.core.synthesis import _ALWAYS, _NEVER, _simplified

A, B, C = Bool("A"), Bool("B"), Bool("C")
N = Int("N")


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
        ]
    )


def test_a_simplified_condition_has_the_same_truth_table():
    rng = random.Random(20261107)
    states = [
        {"A": a, "B": b, "C": c, "N": n}
        for a, b, c, n in itertools.product(
            (False, True), (False, True), (False, True), range(-1, 4)
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
