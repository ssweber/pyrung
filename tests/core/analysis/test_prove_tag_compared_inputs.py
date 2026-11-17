"""Inputs compared with a changing value get every value, not a literal partition.

The expression partition gives an input one value per class its literal
comparisons split.  Compared with another changing value (a timer or
counter accumulator, a calc-stepped Int, another input, an expression), the
input's classes are split at points no literal names: ``T.Acc == In`` needs
``In`` equal to whatever the accumulator holds.
"""

from __future__ import annotations

from typing import Any

from pyrung.core import (
    Bool,
    Int,
    Program,
    Rung,
    Timer,
    latch,
    on_delay,
    out,
)
from pyrung.core.analysis.prove import (
    Intractable,
    _build_prior_facts,
)

# -- the reviewer's shape, scaled down: an accumulator equal to an input ----


def _timer_equals_input() -> tuple[Program, Bool, list[tuple[dict[str, Any], int]]]:
    # The input's own literals (5, 36) give it no value in the window's middle
    # third, and the three latches need one in each.
    en = Bool("KEn", external=True)
    value = Int("KIn", external=True, min=0, max=39)
    timer = Timer.clone("KT")
    window, first, second, bad = Bool("KY"), Bool("KA"), Bool("KB"), Bool("KBad")
    with Program(strict=False) as logic:
        with Rung(en):
            on_delay(timer, 40)
        with Rung(value > 5, value < 36):
            out(window)
        with Rung(timer.Acc == value, timer.Acc < 15, window):
            latch(first)
        with Rung(first, timer.Acc == value, timer.Acc > 15, timer.Acc < 25, window):
            latch(second)
        with Rung(second, timer.Acc == value, timer.Acc > 25, window):
            latch(bad)
    # The accumulator moves 10 ms a scan: 10, then 20, then 30.
    seq = [({"KEn": True, "KIn": 10}, 1), ({"KIn": 20}, 1), ({"KIn": 30}, 1)]
    return logic, bad, seq


# -- how()'s prior keeps the partition ---------------------------------------


def test_domains_only_prior_keeps_the_partition() -> None:
    logic, _bad, _seq = _timer_equals_input()
    ctx = _build_prior_facts(logic)
    assert not isinstance(ctx, Intractable)
    # KIn shares the Acc's values, and each gap between two of them gets two more, one for
    # each tag compared.  A domain of every value from 0 to 39 would have 40.
    assert len(ctx.nondeterministic_dims["KIn"]) < 40
