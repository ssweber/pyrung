"""When how() is about to stop, one move may run with no reason, and the plan says so."""

from __future__ import annotations

from pyrung import PLC, Bool, Int, Program, Rung, Timer, calc, latch, on_delay, out, reset
from pyrung.core.analysis.graph import PlanStatus
from pyrung.core.analysis.pilot import pilot_events


def test_an_input_past_the_depth_of_the_trace_is_a_stated_move() -> None:
    """Twenty coils in a row: the trace stops before it reaches Go."""
    go = Bool("Go", external=True)
    links = [Bool(f"L{i}") for i in range(20)]
    with Program(strict=False) as logic:
        with Rung(go):
            out(links[0])
        for before, after in zip(links, links[1:], strict=False):
            with Rung(before):
                out(after)
    plc = PLC(logic, dt=0.01)
    plc.step()
    scan = plc.state.scan_id

    plan = plc.how(links[-1], max_scans=3000)

    assert plan.status is PlanStatus.REACHED, plan.reason
    assert plan.last_resort.tag == "Go"
    assert plan.last_resort.value is True
    assert plan.last_resort.tries == 1
    assert plan.anchor_scan == scan
    assert plan.changes["Go"] is True
    assert plan.replay().state.tags["L19"] is True
    text = str(plan)
    assert "1. Set Go=True before the next scan." in text
    assert "Nothing in the trace pointed to this." in text
    assert plc.state.scan_id == scan


def test_a_count_the_program_runs_every_other_scan_is_waited_for_without_the_last_resort() -> None:
    """The trace unrolls Count's own calc to its depth and stops: a projection reads the motion.

    This was a last-resort rescue: no advance profile owns a calc fed its own result.
    """
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
    assert list(pilot_events(plc, hit, max_scans=3000))[-1].data["reached"] is True

    plan = plc.how(hit, max_scans=3000)

    assert plan.status is PlanStatus.REACHED, plan.reason
    assert plan.last_resort is None
    assert plan.replay().state.tags["Hit"] is True


def test_a_count_the_program_runs_for_itself_is_a_stated_wait() -> None:
    """Count moves once in ten scans, longer than the projection that reads self-motion."""
    ring = [Bool(f"R{i}") for i in range(5)]
    count, hit = Int("Count"), Bool("Hit")
    with Program(strict=False) as logic:
        # A Johnson ring: each bit takes the one before it, the first the last one's inverse.
        for i in range(4, 0, -1):
            with Rung(ring[i - 1]):
                out(ring[i])
        with Rung(~ring[-1]):
            out(ring[0])
        with Rung(ring[0], ~ring[1]):
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


def test_a_coil_above_the_timer_it_reads_is_waited_for_without_the_last_resort() -> None:
    """Block reads T.Done above the timer, so it drops the scan after Done: the pilot waits it.

    This was a last-resort rescue: the coil's writer had its one condition already true,
    and only a writer with no conditions counted as ready to run.
    """
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
    assert list(pilot_events(plc, hit, max_scans=3000))[-1].data["reached"] is True

    plan = plc.how(hit, max_scans=3000)

    assert plan.status is PlanStatus.REACHED, plan.reason
    assert plan.last_resort is None
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
