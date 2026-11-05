"""When how() is about to stop, one move may run with no reason, and the plan says so."""

from __future__ import annotations

from pyrung import PLC, Bool, Int, Program, Rung, Timer, calc, latch, on_delay, out, reset
from pyrung.core.analysis.graph import PlanStatus
from pyrung.core.analysis.pilot import pilot_events
from tests.core.analysis.test_pilot_reads_at_the_rung import _reset_at_the_top


def test_the_input_pressed_when_the_plant_needs_it_is_a_stated_move() -> None:
    plc, hit = _reset_at_the_top(go=False)
    scan = plc.state.scan_id

    plan = plc.how(hit, max_scans=30000)

    assert plan.status is PlanStatus.REACHED, plan.reason
    assert plan.last_resort.tag == "Go"
    assert plan.last_resort.value is True
    assert plan.last_resort.tries == 1
    assert plan.anchor_scan == scan
    assert plan.changes["Go"] is True
    assert plan.replay().state.tags["Hit"] is True
    text = str(plan)
    assert "1. Set Go=True before the next scan." in text
    assert "Nothing in the trace pointed to this." in text
    assert plc.state.scan_id == scan


def test_a_count_the_program_runs_for_itself_is_a_stated_wait() -> None:
    osc, count, hit = Bool("Osc"), Int("Count"), Bool("Hit")
    with Program(strict=False) as logic:
        with Rung(~osc):
            out(osc)
        with Rung(osc):
            calc(count + 1, count)
        with Rung(count == 40):
            latch(hit)
    plc = PLC(logic, dt=0.01)
    plc.step()
    assert list(pilot_events(plc, hit, max_scans=3000))[-1].data["reached"] is False

    plan = plc.how(hit, max_scans=3000)

    assert plan.status is PlanStatus.REACHED, plan.reason
    assert plan.last_resort.tag is None
    assert f"1. Wait {plan.last_resort.scans} scans." in str(plan)
    assert plan.replay().state.tags["Hit"] is True


def test_a_wait_is_asked_where_a_bit_moves_and_not_after_a_count_that_runs_on() -> None:
    """The timer's Acc never stops, so nothing here settles, and one second is what it takes."""
    timer, block, hit, go = Timer.clone("T"), Bool("Block"), Bool("Hit"), Bool("Go")
    with Program(strict=False) as logic:
        with Rung(~timer.Done):
            out(block)
        with Rung():
            on_delay(timer, 1, "s")
        with Rung(block):
            reset(hit)
        with Rung(go, ~block):
            latch(hit)
    plc = PLC(logic, dt=0.01)
    plc.step()
    assert list(pilot_events(plc, hit, max_scans=3000))[-1].data["reached"] is False

    plan = plc.how(hit, max_scans=3000)

    assert plan.status is PlanStatus.REACHED, plan.reason
    assert plan.last_resort.tag is None
    assert plan.last_resort.tries == 1
    assert plan.last_resort.scans <= 100
    assert plan.replay().state.tags["Hit"] is True


def test_a_plan_the_pilot_had_a_reason_for_has_no_last_resort() -> None:
    go, hit = Bool("Go"), Bool("Hit")
    with Program(strict=False) as logic:
        with Rung(go):
            latch(hit)
    plc = PLC(logic, dt=0.01)
    plc.step()

    plan = plc.how(hit, max_scans=500)

    assert plan.status is PlanStatus.REACHED
    assert plan.last_resort is None


def test_nothing_is_tried_for_a_goal_no_input_can_reach() -> None:
    lamp, other = Bool("Lamp"), Bool("Other", external=True)
    with Program(strict=False) as logic:
        with Rung(lamp):
            out(other)
    plc = PLC(logic, dt=0.01)
    plc.step()

    plan = plc.how(lamp, max_scans=500)

    assert plan.last_resort is None
