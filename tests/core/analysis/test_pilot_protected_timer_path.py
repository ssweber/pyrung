"""Minimal regressions for same-scan cause and target-preserving repair."""

from __future__ import annotations

from typing import Any

from pyrung import PLC, Bool, Int, Program, Timer, copy, on_delay, rung
from pyrung.core.analysis.pilot import pilot_events


def _protected_path_program(
    *,
    completion_ms: int = 30,
) -> tuple[Program, dict[str, Any]]:
    """The only completion path must keep a zero-preset watchdog enabled."""

    run = Bool("RunCommand", external=True, default=True)
    step = Int("ProcessStep", default=0)
    watchdog_preset = Int("WatchdogPresetMs", external=True, default=0)
    completion = Timer.clone("CompletionDelay")
    watchdog = Timer.clone("ProcessWatchdog")

    with Program() as program:
        # This rung intentionally precedes the start transition. Entering step
        # 1 commits cleanly; the timers first execute on the following scan.
        with rung(step == 1, run):
            on_delay(completion, completion_ms, "ms")
            on_delay(watchdog, watchdog_preset, "ms")

        with rung(step == 1, completion.Done):
            copy(2, step)

        with rung(watchdog.Done):
            copy(90, step)

        with rung(step == 0, run):
            copy(1, step)

    return program, {
        "run": run,
        "step": step,
        "preset": watchdog_preset,
        "completion": completion,
        "watchdog": watchdog,
    }


def test_repair_preserves_a_complete_target_path() -> None:
    """A locally valid alarm correction must not destroy every target path."""

    program, tags = _protected_path_program()

    # Ground truth: changing only the timer boundary preserves the sequence.
    control = PLC(program, dt=0.010)
    control.patch(
        {
            tags["run"].name: True,
            tags["preset"].name: 40,
        }
    )
    for _ in range(5):
        control.patch(
            {
                tags["run"].name: True,
                tags["preset"].name: 40,
            }
        )
        control.step()
        if control.state.tags[tags["step"].name] == 2:
            break
    assert control.state.tags[tags["step"].name] == 2

    # PILOT must suppress the alarm without disabling every route to step 2.
    events = tuple(
        pilot_events(
            PLC(program, dt=0.010),
            tags["step"] == 2,
            max_scans=200,
        )
    )
    finished = next(event for event in events if event.kind == "finished")

    investigations = tuple(
        event.data["investigation"]
        for event in events
        if event.kind == "trend_regression"
        and (event.data.get("investigation") or {}).get("requirement") is not None
    )
    assert all(item["working_theory"] is True for item in investigations)
    assert all(item["private_replay"] is False for item in investigations)
    holds = tuple(
        hold
        for item in investigations
        for hold in item["requirement"].corrective_pilot_rungs
    )
    configurations = tuple(
        pair
        for event in events
        if event.kind == "theory_correction_composed"
        for pair in event.data["configuration"]
    )
    assert any(
        hold.dest == tags["preset"].name and "ProcessStep == 0" in repr(hold.guard)
        for hold in holds
    ), holds
    assert any(
        tag == tags["preset"].name and float(value) > 30.0
        for tag, value in configurations
    ), configurations
    assert finished.data["work"].state.tags[tags["step"].name] == 2
    assert finished.data["reached"] is True


def test_relational_refinement_has_its_own_bounded_search() -> None:
    """A long safe path may require more refinements than causal closure."""

    program, tags = _protected_path_program(completion_ms=100)

    events = tuple(
        pilot_events(
            PLC(program, dt=0.010),
            tags["step"] == 2,
            max_scans=300,
        )
    )
    finished = next(event for event in events if event.kind == "finished")

    investigations = tuple(
        event.data["investigation"]
        for event in events
        if event.kind == "trend_regression"
        and (event.data.get("investigation") or {}).get("requirement") is not None
    )
    assert all(item["working_theory"] is True for item in investigations)
    assert all(item["private_replay"] is False for item in investigations)
    holds = tuple(
        hold
        for item in investigations
        for hold in item["requirement"].corrective_pilot_rungs
    )
    configurations = tuple(
        pair
        for event in events
        if event.kind == "theory_correction_composed"
        for pair in event.data["configuration"]
    )

    assert any(
        hold.dest == tags["preset"].name and "ProcessStep == 0" in repr(hold.guard)
        for hold in holds
    ), holds
    assert any(
        tag == tags["preset"].name and float(value) > 100.0
        for tag, value in configurations
    ), configurations
    assert finished.data["work"].state.tags[tags["step"].name] == 2
    assert finished.data["reached"] is True
