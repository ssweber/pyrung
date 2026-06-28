"""Tests for predicate breakpoints and snapshot labels."""

from __future__ import annotations

from datetime import datetime

import pytest

from pyrung import Or, Program, Rung, Timer, latch, on_delay, out
from pyrung.core import PLC, Bool, Int
from pyrung.core.state import SystemState


def _scan_ids(states: list[SystemState]) -> list[int]:
    return [state.scan_id for state in states]


def test_pause_breakpoint_stops_run_on_trigger_scan() -> None:
    runner = PLC(logic=[])
    runner.when(lambda state: state.scan_id >= 3).pause()

    runner.run(cycles=10)

    assert runner.current_state.scan_id == 3


def test_pause_breakpoint_halts_run_for() -> None:
    runner = PLC(logic=[], dt=0.1)
    runner.when(lambda state: state.scan_id >= 3).pause()

    runner.run_for(seconds=10.0)

    assert runner.current_state.scan_id == 3
    assert runner.simulation_time == pytest.approx(0.3)


def test_pause_breakpoint_halts_run_until_even_when_predicate_is_false() -> None:
    runner = PLC(logic=[])
    runner.when(lambda state: state.scan_id >= 2).pause()

    runner.run_until(lambda state: state.scan_id >= 10, max_cycles=20)

    assert runner.current_state.scan_id == 2


def test_snapshot_breakpoint_labels_history_and_run_continues() -> None:
    runner = PLC(logic=[])
    runner.when(lambda state: state.scan_id > 0 and state.scan_id % 2 == 0).snapshot("even")

    runner.run(cycles=5)

    assert runner.current_state.scan_id == 5
    latest_even = runner.history.find("even")
    assert latest_even is not None
    assert latest_even.scan_id == 4
    assert _scan_ids(runner.history.find_all("even")) == [2, 4]


def test_snapshot_and_pause_can_fire_together_on_same_scan() -> None:
    runner = PLC(logic=[])
    runner.when(lambda state: state.scan_id == 2).snapshot("hit")
    runner.when(lambda state: state.scan_id == 2).pause()

    runner.run(cycles=10)

    assert runner.current_state.scan_id == 2
    latest_hit = runner.history.find("hit")
    assert latest_hit is not None
    assert latest_hit.scan_id == 2
    assert _scan_ids(runner.history.find_all("hit")) == [2]


def test_snapshot_labels_deduplicate_same_label_on_same_scan() -> None:
    runner = PLC(logic=[])
    runner.when(lambda state: state.scan_id == 2).snapshot("dup")
    runner.when(lambda state: state.scan_id == 2).snapshot("dup")

    runner.run(cycles=3)

    assert _scan_ids(runner.history.find_all("dup")) == [2]


def test_snapshot_labels_survive_history_window_rotation() -> None:
    """Labels are decoupled from state storage — early snapshots stay
    findable even after the recent-state window has rotated past their
    scans."""
    runner = PLC(logic=[])
    runner.when(lambda state: state.scan_id in {1, 3}).snapshot("milestone")

    runner.run(cycles=40)

    assert _scan_ids(runner.history.find_all("milestone")) == [1, 3]
    latest_milestone = runner.history.find("milestone")
    assert latest_milestone is not None
    assert latest_milestone.scan_id == 3


def test_snapshot_breakpoint_captures_rtc_metadata() -> None:
    runner = PLC(logic=[], dt=0.1)
    runner.set_rtc(datetime(2026, 2, 24, 12, 34, 56))
    runner.when(lambda state: state.scan_id == 1).snapshot("tick")

    runner.run(cycles=1)

    labeled = runner.history.find_labeled("tick")
    assert labeled is not None
    assert labeled.scan_id == 1
    assert labeled.rtc_iso == "2026-02-24T12:34:56.100000"
    assert isinstance(labeled.rtc_offset_seconds, float)


def test_breakpoint_handle_disable_enable_and_remove() -> None:
    runner = PLC(logic=[])
    handle = runner.when(lambda state: state.scan_id >= 2).pause()

    handle.disable()
    runner.run(cycles=3)
    assert runner.current_state.scan_id == 3

    handle.enable()
    runner.run(cycles=5)
    assert runner.current_state.scan_id == 4

    handle.remove()
    handle.remove()
    handle.disable()
    handle.enable()
    runner.run(cycles=3)
    assert runner.current_state.scan_id == 7


def test_breakpoint_predicate_exceptions_propagate() -> None:
    runner = PLC(logic=[])

    def _boom(_state: SystemState) -> bool:
        raise RuntimeError("predicate boom")

    runner.when(_boom).pause()
    with pytest.raises(RuntimeError, match="predicate boom"):
        runner.step()


def test_pause_request_is_consumed_by_single_step() -> None:
    runner = PLC(logic=[])
    runner.when(lambda state: state.scan_id == 1).pause()

    runner.step()
    runner.step()

    assert runner.current_state.scan_id == 2


def test_expression_pause_breakpoint_accepts_tag_conditions() -> None:
    fault = Bool("Fault")
    runner = PLC(logic=[])
    runner.when(fault).pause()

    runner.patch({"Fault": True})
    runner.run(cycles=10)

    assert runner.current_state.scan_id == 1


def test_expression_snapshot_breakpoint_accepts_inverted_tag_conditions() -> None:
    fault = Bool("Fault")
    runner = PLC(logic=[])
    runner.when(~fault).snapshot("fault_clear")

    runner.step()

    latest = runner.history.find("fault_clear")
    assert latest is not None
    assert latest.scan_id == 1


def test_run_until_expression_supports_inverted_bool_tag() -> None:
    motor = Bool("Motor")
    runner = PLC(logic=[])

    result = runner.run_until(~motor, max_cycles=10)

    assert result.scan_id == 1


def test_run_until_expression_supports_comparisons() -> None:
    step = Int("Step")
    runner = PLC(logic=[])
    runner.patch({"Step": 2})

    result = runner.run_until(step >= 2, max_cycles=10)

    assert result.scan_id == 1


def test_when_accepts_callable_predicate() -> None:
    runner = PLC(logic=[])

    runner.when(lambda state: state.scan_id >= 1).pause()
    runner.run(cycles=5)
    assert runner.current_state.scan_id == 1


def test_run_until_accepts_callable_predicate() -> None:
    runner = PLC(logic=[])

    result = runner.run_until(lambda state: state.scan_id >= 3, max_cycles=10)
    assert result.scan_id == 3


# ---------------------------------------------------------------------------
# .do(callback) — reactive side-effect action (the hook for conditional holds)
# ---------------------------------------------------------------------------


def test_do_breakpoint_runs_callback_on_match_and_continues() -> None:
    runner = PLC(logic=[])
    hits: list[int] = []
    runner.when(lambda state: state.scan_id > 0 and state.scan_id % 2 == 0).do(
        lambda state: hits.append(state.scan_id)
    )

    runner.run(cycles=5)

    assert runner.current_state.scan_id == 5  # do() does not pause — run continues
    assert hits == [2, 4]


def test_do_breakpoint_can_force_a_tag() -> None:
    motor = Bool("Motor", external=True)
    lamp = Bool("Lamp")
    with Program() as prog:
        with Rung(motor):
            out(lamp)
    runner = PLC(prog)
    runner.when(lambda state: state.scan_id == 1).do(lambda state: runner.force("Motor", True))

    runner.run(cycles=3)

    assert runner.current_state.tags["Motor"] is True
    assert runner.current_state.tags["Lamp"] is True  # forced input flowed through logic


def test_do_breakpoint_handle_removable() -> None:
    runner = PLC(logic=[])
    hits: list[int] = []
    handle = runner.when(lambda state: True).do(lambda state: hits.append(state.scan_id))

    runner.run(cycles=2)
    handle.remove()
    runner.run(cycles=2)

    assert hits == [1, 2]  # nothing after remove


def _watchdog_delay_program() -> Program:
    """The liveness shape, minimal: a sensor under two opposite-edge watchdogs
    and a delay that only counts while neither has faulted."""
    Sensor = Bool("Sensor", external=True)
    OffWD = Timer.clone("OffWD")  # resets on Sensor -> counts while False
    OnWD = Timer.clone("OnWD")  # resets on ~Sensor -> counts while True
    RunDelay = Timer.clone("RunDelay")
    Fault = Bool("Fault")
    Running = Bool("Running")
    with Program() as prog:
        with Rung():
            on_delay(OffWD, 50, "ms").reset(Sensor)
        with Rung():
            on_delay(OnWD, 50, "ms").reset(~Sensor)
        with Rung(Or(OffWD.Done, OnWD.Done)):
            latch(Fault)
        with Rung(~Fault):
            on_delay(RunDelay, 1000, "ms")
        with Rung(RunDelay.Done):
            out(Running)
    return prog


def test_do_breakpoint_oscillation_survives_fold() -> None:
    # A do() oscillator flips the sensor every scan; folding cannot skip past it
    # because the flip changes visible state each scan (the window degrades to
    # scan-by-scan exactly during the oscillation).  So both watchdogs stay reset
    # and the 1 s RunDelay reaches Running with no fault, even under fold=True.
    plc = PLC(_watchdog_delay_program(), dt=0.010)
    plc.step()
    plc.when(lambda state: True).do(
        lambda state: plc.patch({"Sensor": state.tags.get("Sensor") is not True})
    )

    plc.run_until(lambda state: state.tags.get("Running") is True, fold=True, max_cycles=3000)

    assert plc.current_state.tags["Running"] is True
    assert plc.current_state.tags["Fault"] is False
