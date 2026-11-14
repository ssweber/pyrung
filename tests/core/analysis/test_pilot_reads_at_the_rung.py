"""how() judges a need by what the rung reads, not only by what the scan ended with."""

from __future__ import annotations

from typing import Any

from pyrung import PLC, Bool, Program, Rung, Timer, latch, on_delay, reset
from pyrung.core.harness import Harness
from pyrung.core.physical import Physical


def _reset_at_the_top(*, go: bool) -> tuple[PLC, Any]:
    """Off when the timer's rung reads it, on when the plant does."""
    go_tag, enable, hit = Bool("Go", external=True), Bool("En"), Bool("Hit")
    feedback = Bool("Fb", physical=Physical("Fb", on_delay="100ms", off_delay="100ms"), link="En")
    watch = Timer.clone("W")
    with Program(strict=False) as prog:
        with Rung():
            reset(enable)
        with Rung(~enable, feedback):
            on_delay(watch, 3000)
        with Rung(go_tag):
            latch(enable)
        with Rung(watch.Done):
            latch(hit)
    plc = PLC(prog, dt=0.01)
    Harness(plc).install()
    if go:
        plc.patch({"Go": True})
    plc.step()
    return plc, hit


def test_a_bit_reset_above_the_rung_is_already_off_for_it() -> None:
    plc, hit = _reset_at_the_top(go=True)
    assert plc.state.tags["En"] is True

    plan = plc.how(hit, max_scans=30000)

    assert plan.reachable, plan.reason
    assert plan.ordered_steps == []
    assert list(plan.hold_log) == []
    assert plan.replay().state.tags["Hit"] is True


def test_the_input_is_pressed_when_the_plant_needs_it() -> None:
    """The plant needs En=True at the scan's end while the rung reads it reset to False.

    The trace reaches Go through the plant's own on-delay, and holds it until the timer
    that reads Fb is done, not only until Fb first comes on.  This was a last-resort rescue.
    """
    plc, hit = _reset_at_the_top(go=False)
    assert plc.state.tags["En"] is False

    plan = plc.how(hit, max_scans=30000)

    assert plan.reachable, plan.reason
    assert plan.last_resort is None
    assert plan.fork.state.tags["Go"] is True
    assert plan.replay().state.tags["Hit"] is True


def test_a_read_of_the_scans_entry_value_says_nothing_about_the_next_scan() -> None:
    """The timer's rung read off only because the latch below it had not run yet."""
    go, clear, enable, hit = (
        Bool("Go", external=True),
        Bool("Clear", external=True),
        Bool("En"),
        Bool("Hit"),
    )
    watch = Timer.clone("W")
    with Program(strict=False) as prog:
        with Rung(clear):
            reset(enable)
        with Rung(~enable):
            on_delay(watch, 500)
        with Rung(go):
            latch(enable)
        with Rung(watch.Done):
            latch(hit)
    plc = PLC(prog, dt=0.01)
    plc.patch({"Go": True})
    plc.step()
    assert plc.state.tags["En"] is True

    plan = plc.how(hit, max_scans=30000)

    assert plan.reachable, plan.reason
    assert plan.ordered_steps != []
    assert plan.replay().state.tags["Hit"] is True
