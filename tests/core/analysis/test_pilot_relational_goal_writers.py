"""A comparison goal on a tag the program writes: how() looks at what its writers write."""

from __future__ import annotations

import operator
from collections.abc import Callable
from typing import Any

import pytest

from pyrung import PLC, Bool, Int, Program, Real, Rung, call, copy, latch, math, subroutine
from pyrung.core.analysis.graph import PlanStatus
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot.trace import trace_back
from pyrung.core.analysis.pilot.trace_tree import frontier_pairs
from pyrung.core.analysis.steerable import compute_steerable


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
    # Stepping gives the expected answer.  Hold Go and the count meets the goal in a few scans.
    stepped = plc.fork()
    stepped.patch({"Go": True})
    stepped.run_until(goal, max_cycles=20)
    assert compare(stepped.state.tags["Count"], bound)

    plan = plc.how(goal, max_scans=600)

    assert plan.status is PlanStatus.REACHED, plan.reason
    assert plan.fork.state.tags["Go"] is True
    assert compare(plan.replay().state.tags["Count"], bound)
    # The trace names Go for every comparison.  An == goal still reaches only through the last resort.
    if compare is not operator.eq:
        assert plan.last_resort is None


def test_a_constant_copy_that_differs_meets_a_not_equal_goal() -> None:
    go, level = Bool("Go", external=True), Int("Level")
    with Program(strict=False) as prog:
        with Rung(go):
            copy(5, level)
    plc = PLC(prog, dt=0.01)
    plc.step()

    plan = plc.how(level != 0, max_scans=600)

    assert plan.status is PlanStatus.REACHED, plan.reason
    assert plan.last_resort is None
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


def test_a_real_count_at_inf_takes_the_reset_writer() -> None:
    go, rst, count = Bool("Go", external=True), Bool("Rst", external=True), Real("Count")
    with Program(strict=False) as prog:
        with Rung(go):
            math(count - 1, count)
        with Rung(rst):
            copy(0, count)
    plc = PLC(prog, dt=0.01)
    plc.step()
    plc.patch({"Count": float("inf")})
    plc.step()
    stepped = plc.fork()
    stepped.patch({"Rst": True})
    stepped.step()
    assert stepped.state.tags["Count"] < 3

    plan = plc.how(count < 3, max_scans=600)

    assert plan.status is PlanStatus.REACHED, plan.reason
    assert plan.replay().state.tags["Count"] < 3


def test_a_count_against_a_limit_at_inf_still_reaches() -> None:
    go, low, n, lim = Bool("Go", external=True), Bool("Low", external=True), Int("N"), Real("Lim")
    with Program(strict=False) as prog:
        with Rung(go):
            math(n + 1, n)
        with Rung(low):
            copy(2.0, lim)
    plc = PLC(prog, dt=0.01)
    plc.step()
    plc.patch({"Lim": float("inf")})
    plc.step()
    stepped = plc.fork()
    stepped.patch({"Low": True, "Go": True})
    stepped.run_until(n > lim, max_cycles=20)
    assert stepped.state.tags["N"] > stepped.state.tags["Lim"]

    plan = plc.how(n > lim, max_scans=600)

    assert plan.status is PlanStatus.REACHED, plan.reason
    replayed = plan.replay().state.tags
    assert replayed["N"] > replayed["Lim"]


def test_a_real_count_at_nan_is_not_reached() -> None:
    go, count = Bool("Go", external=True), Real("Count")
    with Program(strict=False) as prog:
        with Rung(go):
            math(count - 1, count)
    plc = PLC(prog, dt=0.01)
    plc.step()
    plc.patch({"Count": float("nan")})
    plc.step()
    stepped = plc.fork()
    stepped.patch({"Go": True})
    stepped.run(cycles=10)
    assert not stepped.state.tags["Count"] < 3

    plan = plc.how(count < 3, max_scans=600)

    assert plan.status is not PlanStatus.REACHED


def test_a_count_stops_at_the_top_of_its_type() -> None:
    go, count = Bool("Go", external=True), Int("Count")
    with Program(strict=False) as prog:
        with Rung(go):
            math(count + 1, count)
    plc = PLC(prog, dt=0.01)
    plc.patch({"Count": 32760})
    plc.step()
    # An Int holds 32767 at most, so the next add wraps to -32768 and never passes it.
    stepped = plc.fork()
    stepped.patch({"Go": True})
    stepped.run(cycles=20)
    assert stepped.state.tags["Count"] < 0

    top = plc.how(count >= 32767, max_scans=600)
    past = plc.how(count > 32767, max_scans=600)

    assert top.status is PlanStatus.REACHED, top.reason
    assert top.last_resort is None
    assert top.replay().state.tags["Count"] == 32767
    assert past.status is not PlanStatus.REACHED


def test_a_real_count_with_a_fractional_step_is_reached() -> None:
    go, level = Bool("Go", external=True), Real("Level")
    with Program(strict=False) as prog:
        with Rung(go):
            math(level + 0.5, level)
    plc = PLC(prog, dt=0.01)
    plc.step()
    stepped = plc.fork()
    stepped.patch({"Go": True})
    stepped.run_until(level > 2.2, max_cycles=20)
    assert stepped.state.tags["Level"] == 2.5

    plan = plc.how(level > 2.2, max_scans=600)

    assert plan.status is PlanStatus.REACHED, plan.reason
    assert plan.last_resort is None
    assert plan.replay().state.tags["Level"] > 2.2


def test_a_call_gate_beside_the_copy_source_adds_no_need() -> None:
    start, requested, current = Bool("Start", external=True), Int("Requested"), Int("Current")
    with Program(strict=False) as prog:
        with subroutine("finish"):
            with Rung():
                copy(requested, current)
        with Rung(start):
            copy(6, requested)
        with Rung(requested != 0):
            call("finish")
    plc = PLC(prog, dt=0.01)
    plc.step()
    snapshot = dict(plc.current_state.tags)
    pdg = build_program_graph(prog)
    steerable = frozenset(compute_steerable(pdg, plc._known_tags_by_name, prog))

    tree = trace_back("Current", 6, snapshot, pdg, prog, steerable)

    # Requested = 6 already meets the call gate Requested != 0.
    assert frontier_pairs(tree, snapshot) == (("Current", 6), ("Requested", 6))
    assert tree.steerable_leaves() == [("Start", True)]
    plan = plc.how(current == 6, max_scans=300)
    assert plan.status is PlanStatus.REACHED, plan.reason
    assert plan.replay().state.tags["Current"] == 6


def test_two_not_equal_goals_together_are_reached() -> None:
    go, bump = Bool("Go", external=True), Bool("Bump", external=True)
    level, count = Int("Level"), Int("Count")
    with Program(strict=False) as prog:
        with Rung(go):
            copy(5, level)
        with Rung(bump):
            math(count + 1, count)
    plc = PLC(prog, dt=0.01)
    plc.step()

    plan = plc.how(level != 0, count != 0, max_scans=300)

    assert plan.status is PlanStatus.REACHED, plan.reason
    assert plan.last_resort is None
    final = plan.replay().state.tags
    assert final["Level"] == 5
    assert final["Count"] != 0
