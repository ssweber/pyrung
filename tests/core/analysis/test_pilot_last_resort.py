"""When how() is about to stop, one move may run with no reason, and the plan says so."""

from __future__ import annotations

from pyrung import (
    PLC,
    Bool,
    Int,
    Physical,
    Program,
    Rung,
    Timer,
    latch,
    math,
    on_delay,
    out,
    reset,
)
from pyrung.core.analysis.graph import PlanStatus
from pyrung.core.analysis.pilot import pilot_events


def _coil_chain(length: int, *, plant: bool = False) -> tuple[PLC, Bool]:
    """Go starts *length* coils in a row, directly or through a linked valve's feedback."""
    go = Bool("Go", external=True)
    links = [Bool(f"L{i}") for i in range(length)]
    with Program(strict=False) as logic:
        if plant:
            enable = Bool("En")
            feedback = Bool(
                "Fb", physical=Physical("Fb", on_delay="100ms", off_delay="100ms"), link="En"
            )
            with Rung(go):
                out(enable)
            with Rung(feedback):
                out(links[0])
        else:
            with Rung(go):
                out(links[0])
        for before, after in zip(links, links[1:], strict=False):
            with Rung(before):
                out(after)
    plc = PLC(logic, dt=0.01)
    plc.step()
    return plc, links[-1]


def test_an_input_past_the_depth_of_the_trace_is_a_stated_move() -> None:
    """Seventy coils in a row: the trace stops before it reaches Go."""
    plc, goal = _coil_chain(70)
    scan = plc.state.scan_id

    plan = plc.how(goal, max_scans=3000)

    assert plan.status is PlanStatus.REACHED, plan.reason
    assert plan.last_resort.tag == "Go"
    assert plan.last_resort.value is True
    assert plan.last_resort.tries == 1
    assert plan.anchor_scan == scan
    assert plan.changes["Go"] is True
    assert plan.replay().state.tags["L69"] is True
    text = str(plan)
    assert "1. Set Go=True before the next scan." in text
    assert "Nothing in the trace pointed to this." in text
    assert plc.state.scan_id == scan


def test_a_long_chain_of_coils_is_traced_to_its_input() -> None:
    """Forty coils, and the same behind a valve's feedback: past the old depth of 15."""
    for plant in (False, True):
        plc, goal = _coil_chain(40, plant=plant)

        plan = plc.how(goal, max_scans=3000)

        assert plan.status is PlanStatus.REACHED, (plant, plan.reason)
        assert plan.last_resort is None, plant
        assert plan.replay().state.tags["L39"] is True


def test_a_count_the_program_runs_every_other_scan_is_waited_for_without_the_last_resort() -> None:
    """The trace unrolls Count's own calc to its depth and stops: a projection reads the motion.

    This was a last-resort rescue: no advance profile owns a calc fed its own result.
    """
    osc, count, hit = Bool("Osc"), Int("Count"), Bool("Hit")
    with Program(strict=False) as logic:
        with Rung(~osc):
            out(osc)
        with Rung(osc):
            math(count + 1, count)
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
            math(count + 1, count)
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


def test_an_input_that_reaches_the_goal_through_the_plant_is_in_its_cone() -> None:
    """Go drives En, the plant drives Fb from it, and Fb starts the chain to the goal."""
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.pilot.physical import install_harness, upstream_through_plant

    go, enable = Bool("Go", external=True), Bool("En")
    feedback = Bool("Fb", physical=Physical("Fb", on_delay="100ms", off_delay="100ms"), link="En")
    hit = Bool("Hit")
    with Program(strict=False) as logic:
        with Rung(go):
            out(enable)
        with Rung(feedback):
            out(hit)
    plc = PLC(logic, dt=0.01)
    work = plc.fork()
    install_harness(work)
    pdg = build_program_graph(logic)

    assert "Go" not in pdg.upstream_slice("Hit", follow_calls=True)
    assert {"Fb", "En", "Go"} <= upstream_through_plant(pdg, work._harness, ("Hit",))
    assert "Go" not in upstream_through_plant(pdg, None, ("Hit",))
