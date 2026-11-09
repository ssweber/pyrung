"""When how() is about to stop, one move may run with no reason, and the plan says so."""

from __future__ import annotations

import pytest

from pyrung import (
    PLC,
    Bool,
    Int,
    Physical,
    Program,
    Rung,
    Timer,
    call,
    copy,
    forloop,
    latch,
    math,
    on_delay,
    out,
    reset,
    subroutine,
    system,
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


def _call_in_a_loop() -> tuple[PLC, Bool, Bool]:
    """A For of Dc (0 at the start) calls the subroutine that latches Y."""
    count = Int("Dc", external=True, min=0, max=5)
    btn = Bool("Btn", external=True)
    y, alarm = Bool("Y"), Bool("Alarm")
    with Program(strict=False) as logic:
        with Rung():
            with forloop(count):
                call("work")
        with subroutine("work"):
            with Rung(btn):
                latch(y)
            with Rung(~btn):
                out(alarm)
    plc = PLC(logic, dt=0.01)
    plc.step()
    return plc, y, alarm


def test_a_call_in_a_loop_is_traced_to_the_loop_count() -> None:
    """The call runs only when Dc != 0, and the trace asks for it.

    This was a last-resort rescue: the call gate read only the For rung's own
    contacts, which are none.
    """
    plc, y, _alarm = _call_in_a_loop()

    plan = plc.how(y, max_scans=500)

    assert plan.status is PlanStatus.REACHED, plan.reason
    assert plan.last_resort is None
    assert "Satisfies: Dc != 0" in str(plan)
    assert plan.replay().state.tags["Y"] is True


def test_a_call_in_a_loop_is_reached_without_what_it_avoids() -> None:
    """Dc alone would run the subroutine with Btn off and set Alarm: both go in one step."""
    plc, y, alarm = _call_in_a_loop()

    plan = plc.how(y, avoid=alarm, max_scans=500)

    assert plan.status is PlanStatus.REACHED, plan.reason
    assert plan.last_resort is None
    assert plan.replay().state.tags["Alarm"] is False


def _door_guards_the_run(shape: str) -> tuple[PLC, Bool]:
    """Start latches Run, Run with Door open latches Alarm, Alarm resets Run; Run times Done."""
    count = Int("Dc", external=True, min=1, max=5, default=1)
    start, door = Bool("Start", external=True), Bool("Door", external=True)
    run, alarm, done = Bool("Run"), Bool("Alarm"), Bool("Done")
    timer = Timer.clone("Tr")
    with Program(strict=False) as logic:
        if shape == "inline":
            with Rung(run, ~door):
                latch(alarm)
        elif shape == "call":
            with Rung():
                call("faults")
        else:
            with Rung():
                with forloop(count):
                    call("faults")
        with Rung(start, ~alarm):
            latch(run)
        with Rung(alarm):
            reset(run)
        with Rung(run):
            on_delay(timer, 500)
        with Rung(timer.Done):
            latch(done)
        if shape != "inline":
            with subroutine("faults"):
                with Rung(run, ~door):
                    latch(alarm)
    plc = PLC(logic, dt=0.01)
    plc.step()
    return plc, done


@pytest.mark.parametrize("shape", ["inline", "call", "for"])
def test_the_input_that_keeps_a_reset_off_is_held_without_the_last_resort(shape: str) -> None:
    """Start alone runs the timer for one scan: Door open latches Alarm, which resets Run.

    This was a last-resort rescue: the reset's guard, Alarm, is the program's to write, so
    the requirement gave nothing to set until it was read back to the rung that latches it.
    """
    plc, done = _door_guards_the_run(shape)

    plan = plc.how(done, max_scans=400)

    assert plan.status is PlanStatus.REACHED, plan.reason
    assert plan.last_resort is None
    # The Set of Door would come 10 ms after Start's, under the 1 s floor,
    # so the plan keeps Door held.
    assert "with rung(~Done):" in str(plan)
    assert "out(Door)" in str(plan)
    replayed = plan.replay().state.tags
    assert replayed["Done"] is True
    assert replayed["Alarm"] is False


def test_the_press_that_would_trip_the_alarm_is_let_go_without_the_last_resort() -> None:
    """Run with Start still held latches Alarm, which resets Run: Start is pressed, then let go.

    On dev before this, how() stopped here: the reset's guard, Alarm, gave nothing to set.
    """
    start = Bool("Start", external=True)
    run, alarm, done = Bool("Run"), Bool("Alarm"), Bool("Done")
    timer = Timer.clone("Tr")
    with Program(strict=False) as logic:
        with Rung(run, start):
            latch(alarm)
        with Rung(start, ~alarm):
            latch(run)
        with Rung(alarm):
            reset(run)
        with Rung(run):
            on_delay(timer, 500)
        with Rung(timer.Done):
            latch(done)
    plc = PLC(logic, dt=0.01)
    plc.step()

    plan = plc.how(done, max_scans=400)

    assert plan.status is PlanStatus.REACHED, plan.reason
    assert plan.last_resort is None
    replayed = plan.replay().state.tags
    assert replayed["Done"] is True
    assert replayed["Alarm"] is False


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


def test_a_wait_for_a_goal_on_a_tag_no_scan_has_written_runs_until_the_program_writes_it() -> None:
    """No scan has rtc.new_second until the copy writes it, so it doesn't hold before then."""
    from pyrung.core.analysis.pilot import api, last_resort

    count = Int("Count")
    with Program(strict=False) as logic:
        with Rung():
            math(count + 1, count)
        with Rung(count == 50):
            copy(30, system.rtc.new_second)
    plc = PLC(logic, dt=0.01)
    goal = api._goal_check((system.rtc.new_second == 30,))

    assert last_resort._waited(plc, goal, None, lambda: 5.0, 1000)

    assert plc.state.tags[system.rtc.new_second.name] == 30
    assert plc.state.tags["Count"] == 50
