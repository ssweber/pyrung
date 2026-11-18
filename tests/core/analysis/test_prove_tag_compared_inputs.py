"""Test that an input compared with a changing value gets that value's points.

The input keeps its literal partition as well. It does not get every value in its range.

The expression partition gives an input one value for each class that its literal comparisons
split. Suppose the input is compared with another changing value. That value may be a timer or
counter accumulator, a Math-stepped Int, another input or an expression. The input's classes
are then split at points that no literal names. For example, ``T.Acc == In`` needs ``In`` to
equal whatever the accumulator holds.
"""

from __future__ import annotations

from typing import Any

from pyrung.core import (
    Bool,
    Int,
    Program,
    Timer,
    latch,
    on_delay,
    out,
    rung,
)
from pyrung.core.analysis.prove import (
    Intractable,
    _build_prior_facts,
)

# -- the reviewer's shape, scaled down: an accumulator equal to an input ----


def _timer_equals_input() -> tuple[Program, Bool, list[tuple[dict[str, Any], int]]]:
    # The input's own literals (5, 36) give it no value in the middle third of the window.
    # The three latches need a value in each third.
    en = Bool("KEn", external=True)
    value = Int("KIn", external=True, min=0, max=39)
    timer = Timer.clone("KT")
    window, first, second, bad = Bool("KY"), Bool("KA"), Bool("KB"), Bool("KBad")
    with Program(strict=False) as logic:
        with rung(en):
            on_delay(timer, 40)
        with rung(value > 5, value < 36):
            out(window)
        with rung(timer.Acc == value, timer.Acc < 15, window):
            latch(first)
        with rung(first, timer.Acc == value, timer.Acc > 15, timer.Acc < 25, window):
            latch(second)
        with rung(second, timer.Acc == value, timer.Acc > 25, window):
            latch(bad)
    # The accumulator moves 10 ms a scan, so it reads 10, then 20, then 30.
    seq = [({"KEn": True, "KIn": 10}, 1), ({"KIn": 20}, 1), ({"KIn": 30}, 1)]
    return logic, bad, seq


# -- how()'s prior keeps the partition ---------------------------------------


def test_domains_only_prior_keeps_the_partition() -> None:
    logic, _bad, _seq = _timer_equals_input()
    ctx = _build_prior_facts(logic)
    assert not isinstance(ctx, Intractable)
    # KIn takes the Acc's values. Each gap between two of them gets two more values, one for
    # each tag compared. A domain of every value from 0 to 39 would have 40 values.
    assert len(ctx.nondeterministic_dims["KIn"]) < 40
