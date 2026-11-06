"""Temporary logic a reached plan doesn't need is taken out, by running the plan without it."""

from __future__ import annotations

from pyrung import PLC, Bool, Dint, Int, Or, Program, Rung, Timer, copy, latch, on_delay, time_drum
from pyrung.core.analysis.pilot import api, trim
from pyrung.core.runner import _compile_avoid


def _enable_times_a_wait() -> tuple[PLC, Bool]:
    enable, hit = Bool("En", external=True), Bool("Hit")
    wait = Timer.clone("W")
    with Program(strict=False) as logic:
        with Rung(enable):
            on_delay(wait, 3000)
        with Rung(wait.Done):
            latch(hit)
    plc = PLC(logic, dt=0.01)
    plc.step()
    return plc, hit


def _untrimmed(monkeypatch, plc: PLC, *goal, **kwargs):
    monkeypatch.setattr(api._trim, "trim_holds", lambda plan, *_args: plan)
    plan = plc.how(*goal, **kwargs)
    monkeypatch.undo()
    return plan


def test_an_input_nothing_moves_is_set_rather_than_held_by_temporary_logic():
    plc, hit = _enable_times_a_wait()

    plan = plc.how(hit, max_scans=500)

    assert plan.reachable
    assert plan.total_scans == 300
    text = str(plan)
    assert "Set En=True before the next scan." in text
    assert "temporary logic" not in text.lower()
    assert plan.changes == {"En": True}
    assert not plan.hold_log
    replayed = plan.replay()
    assert replayed.state.scan_id == plan.fork.state.scan_id
    assert replayed.state.tags["Hit"] is True


def test_a_set_that_meets_a_state_to_avoid_leaves_the_temporary_logic_in(monkeypatch):
    plc, hit = _enable_times_a_wait()
    plan = _untrimmed(monkeypatch, plc, hit, max_scans=500)
    assert "Install temporary logic" in str(plan)
    goal = api._goal_check((hit,))

    unchecked = trim.trim_holds(plan, goal, None, frozenset({"En"}), None)
    assert "Install temporary logic" not in str(unchecked)

    # The Set leaves En on, and a state with En on is to be avoided: kept as it was.
    kept = trim.trim_holds(plan, goal, _compile_avoid(Bool("En")), frozenset({"En"}), None)
    assert kept is plan


def _dwell() -> tuple[PLC, object]:
    """Sensor on 30 ms and off 30 ms within the 70 ms watchdogs, then a 250 ms run: State 7."""
    sensor = Bool("HoldSensor", external=True)
    stable_on, stable_off = Timer.clone("HoldStableOn"), Timer.clone("HoldStableOff")
    on_wd, off_wd, run = Timer.clone("HoldOnWD"), Timer.clone("HoldOffWD"), Timer.clone("HoldRun")
    state = Int("HoldState", default=6)
    with Program(strict=False) as logic:
        with Rung(state == 6, sensor):
            on_delay(stable_on, 30, "ms")
        with Rung(state == 6, ~sensor):
            on_delay(stable_off, 30, "ms")
        with Rung(state == 6):
            on_delay(on_wd, 70, "ms").reset(stable_off.Done)
        with Rung(state == 6):
            on_delay(off_wd, 70, "ms").reset(stable_on.Done)
        with Rung(state == 6, Or(on_wd.Done, off_wd.Done)):
            copy(8, state)
        with Rung(state == 6):
            on_delay(run, 250, "ms")
        with Rung(state == 6, run.Done):
            copy(7, state)
    return PLC(logic, dt=0.01), state == 7


def test_logic_a_set_cannot_stand_in_for_stays_and_the_rest_goes(monkeypatch):
    plc, goal = _dwell()
    before = _untrimmed(monkeypatch, plc, goal)
    plc, goal = _dwell()

    plan = plc.how(goal)

    assert before.reachable and plan.reachable
    assert plan.total_scans == before.total_scans
    assert "latch(HoldStableOn_Done)" in str(before)
    text = str(plan)
    assert "latch(HoldStableOn_Done)" not in text
    assert "latch(HoldStableOff_Done)" not in text
    # On for three scans and then off for three is what the sensor's logic does; one Set
    # can't, so it stays.
    assert "latch(HoldSensor)" in text
    assert "reset(HoldSensor)" in text
    replayed = plan.replay()
    assert replayed.state.scan_id == plan.fork.state.scan_id
    assert replayed.state.tags["HoldState"] == 7


def test_a_reset_nothing_needed_goes_and_the_waits_either_side_of_it_are_one():
    start, reset = Bool("TrStart", external=True), Bool("TrReset", external=True)
    with Program(strict=False) as logic:
        with Rung(start):
            time_drum(
                outputs=[Bool("TrO1"), Bool("TrO2")],
                presets=[20, 40],
                unit="s",
                pattern=[[1, 0], [0, 1]],
                current_step=Int("TrStep"),
                accumulator=Dint("TrAcc"),
                completion_flag=Bool("TrDone"),
            ).reset(reset)
    plc = PLC(logic, dt=0.01)
    plc.step()

    plan = plc.how(Bool("TrDone"), wall_clock_s=60.0)

    assert plan.reachable
    assert plan.total_scans == 6002
    text = str(plan)
    assert "temporary logic" not in text.lower()
    assert "TrReset" not in text
    assert "Set TrStart=True before the next scan." in text
    assert "Wait ~60s (6001 scans)." in text
    replayed = plan.replay()
    assert replayed.state.scan_id == plan.fork.state.scan_id
    assert replayed.state.tags["TrDone"] is True
