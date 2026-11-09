"""A comparison goal on a tag the program writes: how() looks at what its writers write."""

from __future__ import annotations

import operator
from collections.abc import Callable
from typing import Any

import pytest

from pyrung import PLC, Bool, Int, Program, Rung, copy, latch, math
from pyrung.core.analysis.graph import PlanStatus


def test_a_goal_on_a_tag_nothing_reads_finds_the_writer() -> None:
    go, level = Bool("Go", external=True), Int("Level")
    with Program(strict=False) as prog:
        with Rung(go):
            copy(5, level)
    plc = PLC(prog, dt=0.01)
    plc.step()

    plan = plc.how(level >= 5)

    assert plan.reachable, plan.reason
    assert plan.fork.state.tags["Go"] is True
    assert plan.replay().state.tags["Level"] == 5


def test_it_takes_the_writer_whose_value_meets_the_goal() -> None:
    low, high, level = Bool("Low", external=True), Bool("High", external=True), Int("Level")
    with Program(strict=False) as prog:
        with Rung(low):
            copy(2, level)
        with Rung(high):
            copy(9, level)
    plc = PLC(prog, dt=0.01)
    plc.step()

    plan = plc.how(level >= 5)

    assert plan.reachable, plan.reason
    assert plan.fork.state.tags["High"] is True
    assert plan.replay().state.tags["Level"] == 9


def test_no_writer_meets_the_goal_and_how_does_not_claim_it() -> None:
    go, level = Bool("Go", external=True), Int("Level")
    with Program(strict=False) as prog:
        with Rung(go):
            copy(2, level)
    plc = PLC(prog, dt=0.01)
    plc.step()

    plan = plc.how(level >= 5)

    assert not plan.reachable


def test_a_goal_on_a_tag_the_program_also_reads_still_reaches() -> None:
    go, level, lamp = Bool("Go", external=True), Int("Level"), Bool("Lamp")
    with Program(strict=False) as prog:
        with Rung(go):
            copy(5, level)
        with Rung(level >= 5):
            latch(lamp)
    plc = PLC(prog, dt=0.01)
    plc.step()

    plan = plc.how(level >= 5)

    assert plan.reachable, plan.reason
    assert plan.replay().state.tags["Level"] == 5



def _counting(step: int) -> tuple[PLC, Int]:
    go, count = Bool("Go", external=True), Int("Count")
    with Program(strict=False) as prog:
        with Rung(go):
            math(count + step, count)
    plc = PLC(prog, dt=0.01)
    plc.step()
    return plc, count


@pytest.mark.parametrize(
    ("step", "compare", "bound"),
    [
        (1, operator.gt, 3),
        (1, operator.gt, 1),
        (1, operator.ge, 4),
        (1, operator.eq, 3),
        (1, operator.ne, 0),
        (3, operator.gt, 7),
        (-1, operator.lt, -2),
        (-1, operator.le, -3),
    ],
)
def test_a_threshold_on_a_count_is_reached_by_counting(
    step: int, compare: Callable[[Any, Any], Any], bound: int
) -> None:
    plc, count = _counting(step)
    goal = compare(count, bound)
    # Stepping is the oracle.  Hold Go and the count meets the goal within a few scans.
    stepped = plc.fork()
    stepped.patch({"Go": True})
    stepped.run_until(goal, max_cycles=20)
    assert compare(stepped.state.tags["Count"], bound)

    plan = plc.how(goal, max_scans=600)

    assert plan.status is PlanStatus.REACHED, plan.reason
    assert plan.fork.state.tags["Go"] is True
    assert compare(plan.replay().state.tags["Count"], bound)


def test_a_constant_copy_that_differs_meets_a_not_equal_goal() -> None:
    go, level = Bool("Go", external=True), Int("Level")
    with Program(strict=False) as prog:
        with Rung(go):
            copy(5, level)
    plc = PLC(prog, dt=0.01)
    plc.step()

    plan = plc.how(level != 0, max_scans=600)

    assert plan.status is PlanStatus.REACHED, plan.reason
    assert plan.replay().state.tags["Level"] == 5


@pytest.mark.parametrize(
    ("value", "compare", "bound"),
    [(1, operator.gt, 3), (0, operator.ne, 0)],
)
def test_a_goal_no_write_meets_is_not_reached(
    value: int, compare: Callable[[Any, Any], Any], bound: int
) -> None:
    go, level = Bool("Go", external=True), Int("Level")
    with Program(strict=False) as prog:
        with Rung(go):
            copy(value, level)
    plc = PLC(prog, dt=0.01)
    plc.step()
    stepped = plc.fork()
    stepped.patch({"Go": True})
    stepped.run(cycles=5)
    assert not compare(stepped.state.tags["Level"], bound)

    plan = plc.how(compare(level, bound), max_scans=600)

    assert plan.status is not PlanStatus.REACHED
