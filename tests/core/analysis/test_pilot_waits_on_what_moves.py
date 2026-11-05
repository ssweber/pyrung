"""how() when all it needs to do is let the program run: it waits on what is really moving."""

from __future__ import annotations

from typing import Any

import pytest

from pyrung import PLC, Bool, Program, Rung, Timer, latch, on_delay
from pyrung.core.harness import Harness
from pyrung.core.physical import Physical, Pulse


def _lands_on(plan: Any, goal: str) -> None:
    """The plan reached the goal, and its own recording replays onto it."""
    assert plan.reachable, plan.reason
    assert plan.replay().state.tags[goal] is True


def _chain(*, first_running: bool, second_running: bool = False) -> tuple[PLC, Any]:
    enable, lamp = Bool("En"), Bool("Lamp")
    first, second = Timer.clone("Tw1"), Timer.clone("Tw2")
    with Program(strict=False) as prog:
        with Rung(enable):
            on_delay(first, 3, "min")
        with Rung(first.Done):
            on_delay(second, 2, "min")
        with Rung(second.Done):
            latch(lamp)
    plc = PLC(prog, dt=0.01)
    if first_running:
        plc.patch({"En": True})
        plc.step()
    if second_running:
        plc.run_until(first.Done, max_cycles=20000)
    return plc, lamp


def test_a_chain_waits_on_the_timer_that_is_running_not_the_first_it_reads() -> None:
    plc, lamp = _chain(first_running=True)
    plan = plc.how(lamp)
    _lands_on(plan, "Lamp")
    assert plan.ordered_steps == []
    assert plan.fork.state.tags["Tw1_Done"] is True


def test_a_chain_part_way_through_waits_on_the_second_timer() -> None:
    plc, lamp = _chain(first_running=True, second_running=True)
    assert plc.state.tags["Tw1_Done"] is True
    before = plc.state.scan_id
    plan = plc.how(lamp)
    _lands_on(plan, "Lamp")
    assert plan.ordered_steps == []
    assert plan.fork.state.scan_id - before <= 12001


def test_a_chain_nothing_has_started_still_presses_the_input() -> None:
    plc, lamp = _chain(first_running=False)
    plc.step()
    plan = plc.how(lamp)
    _lands_on(plan, "Lamp")
    assert plan.fork.state.tags["En"] is True


def _pulse_program(*, on_ms: int = 500, off_ms: int = 500) -> tuple[Any, Any, Any]:
    enable = Bool("En", external=True)
    feedback = Bool(
        "Fb",
        physical=Physical("Fb", profile=Pulse(on_dwell=f"{on_ms}ms", off_dwell=f"{off_ms}ms")),
        link="En",
    )
    hit = Bool("Hit")
    with Program(strict=False) as prog:
        with Rung(enable, feedback):
            latch(hit)
    return prog, enable, hit


def test_a_pulse_feedback_is_waited_for_with_its_enable_on() -> None:
    prog, _enable, hit = _pulse_program()
    plc = PLC(prog, dt=0.01)
    Harness(plc).install()
    plc.patch({"En": True})
    plc.step()
    plan = plc.how(hit)
    _lands_on(plan, "Hit")
    assert plan.ordered_steps == []


def test_a_pulse_feedback_has_its_enable_turned_on_first() -> None:
    prog, _enable, hit = _pulse_program()
    plc = PLC(prog, dt=0.01)
    Harness(plc).install()
    plan = plc.how(hit)
    _lands_on(plan, "Hit")
    assert plan.fork.state.tags["En"] is True


@pytest.mark.parametrize("wanted", [True, False])
def test_a_pulse_owns_both_of_its_edges_and_nothing_else(wanted: bool) -> None:
    from pyrung.core.analysis.pilot.advance import build_advance_index
    from pyrung.core.crossing import Cmp, Eq

    prog, _enable, _hit = _pulse_program()
    plc = PLC(prog, dt=0.01)
    harness = Harness(plc)
    harness.install()
    owner = build_advance_index(prog, harness).resolve("Fb")
    assert owner is not None
    step = owner.profile.plan(Eq("Fb", frozenset((wanted,))), {"Fb": not wanted, "En": True})
    assert step is not None
    assert step.until == Eq("Fb", frozenset((wanted,)))
    assert owner.profile.plan(Cmp("Fb", ">=", 1, False), {"Fb": False, "En": True}) is None
