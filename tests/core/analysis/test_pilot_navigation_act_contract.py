"""The navigation boundary has no historical-prefix replay act."""

from __future__ import annotations

from types import SimpleNamespace
from typing import get_args

import pytest

from pyrung import PLC, Int, Program, Rung, copy
from pyrung.core.analysis.pilot import navigation_contracts, steer
from pyrung.core.analysis.pilot.navigation_contracts import (
    ActPolicy,
    ActSource,
    BatchPulse,
    Bearing,
    BearingObjective,
    Coast,
    Dwell,
    IntrascanPulse,
    NavigationAct,
    ObserveScan,
    OrientationWorld,
    ProgramScan,
    Pulse,
    PulseHorizon,
    TargetSpec,
)
from pyrung.core.analysis.pilot.working_theory import ScanEntryConfiguration
from pyrung.core.condition import CompareEq


def test_consumer_bound_horizon_requires_its_exact_receipt() -> None:
    with pytest.raises(ValueError, match="requires exactly one consumer boundary"):
        ActPolicy(
            source=ActSource.TRACE,
            pulse_horizon=PulseHorizon.CONSUMER_BOUNDARY,
        )

    with pytest.raises(ValueError, match="requires exactly one consumer boundary"):
        ActPolicy(
            source=ActSource.TRACE,
            consumer_boundary=object(),  # type: ignore[arg-type]
        )


def test_navigation_act_cannot_name_a_historical_replay() -> None:
    assert get_args(NavigationAct) == (
        Pulse,
        BatchPulse,
        IntrascanPulse,
        Coast,
        Dwell,
        ObserveScan,
        ProgramScan,
    )
    assert not hasattr(navigation_contracts, "RetainedReplay")
    assert not hasattr(navigation_contracts, "RetainedOccurrence")


def test_execution_rejects_a_historical_replay_shaped_act(monkeypatch) -> None:
    """Execution cannot silently replay an old prefix through an unknown act."""

    class _HistoricalReplay:
        pass

    key = ("current-world",)
    state = SimpleNamespace(
        key_config=object(),
        work=SimpleNamespace(state=SimpleNamespace(tags={})),
        pilot_rungs=(),
        active_requirements=(),
    )
    world = OrientationWorld(
        world_key=key,
        snapshot={},
        frame=SimpleNamespace(),
        state=state,
        context=SimpleNamespace(),
    )
    bearing = Bearing(
        world_key=key,
        act=_HistoricalReplay(),  # type: ignore[arg-type]
        objective=BearingObjective(TargetSpec("Target", True)),
    )
    monkeypatch.setattr(steer, "_pilot_world_key", lambda *_args: key)

    with pytest.raises(TypeError, match="unsupported navigation act _HistoricalReplay"):
        steer.execute(bearing, world)


def test_program_scan_executes_exactly_one_disposable_scan(monkeypatch) -> None:
    staged = Int("ProgramScanStaged")
    with Program() as program:
        with Rung():
            copy(98, staged)

    source = PLC(program)
    key = ("current-world",)
    state = SimpleNamespace(
        key_config=object(),
        work=source,
        pilot_rungs=(),
        active_requirements=(),
        watch_tags=(),
    )
    context = SimpleNamespace(
        program=program,
        pipeline_roles=(),
        avoid_pred=None,
        target=TargetSpec(staged.name, 999),
    )
    world = OrientationWorld(
        world_key=key,
        snapshot=dict(source.state.tags),
        frame=SimpleNamespace(key=key),
        state=state,
        context=context,
    )
    act = ProgramScan(
        expected_write=SimpleNamespace(tag=staged.name, after=98),
        evidence_identity=("traceback", 1),
    )
    bearing = Bearing(
        world_key=key,
        act=act,
        objective=BearingObjective(context.target),
    )
    monkeypatch.setattr(steer, "_pilot_world_key", lambda *_args: key)
    monkeypatch.setattr(steer, "verify_gates", lambda attempt, *_args: attempt)

    executed = steer.execute(bearing, world)

    assert source.state.scan_id == 0
    assert executed.pulse.fork.state.scan_id == 1
    assert executed.pulse.kernel_scan_ids == (1,)
    assert executed.pulse.snap[staged.name] == 98


def test_scan_entry_configuration_is_applied_only_to_the_execution_fork(monkeypatch) -> None:
    preset = Int("ConfiguredPreset", external=True)
    result = Int("ConfiguredResult")
    with Program() as program:
        with Rung(CompareEq(preset, 11)):
            copy(81, result)

    source = PLC(program)
    key = ("configured-world",)
    state = SimpleNamespace(
        key_config=object(),
        work=source,
        pilot_rungs=(),
        active_requirements=(),
        watch_tags=(),
    )
    context = SimpleNamespace(
        program=program,
        pipeline_roles=(),
        avoid_pred=None,
        target=TargetSpec(result.name, 999),
    )
    world = OrientationWorld(
        world_key=key,
        snapshot=dict(source.state.tags),
        frame=SimpleNamespace(key=key),
        state=state,
        context=context,
    )
    configuration = ScanEntryConfiguration(((preset.name, 11),))
    bearing = Bearing(
        world_key=key,
        act=ProgramScan(
            expected_write=SimpleNamespace(tag=result.name, after=81),
            evidence_identity=("configured-program-scan",),
        ),
        objective=BearingObjective(context.target),
        entry_configurations=(configuration,),
    )
    monkeypatch.setattr(steer, "_pilot_world_key", lambda *_args: key)
    monkeypatch.setattr(steer, "verify_gates", lambda attempt, *_args: attempt)

    executed = steer.execute(bearing, world)

    assert source.state.tags[preset.name] == 0
    assert source.state.tags[result.name] == 0
    assert executed.pulse.source_snap[preset.name] == 11
    assert executed.pulse.snap[result.name] == 81
    assert executed.pulse.applied_configurations == (configuration,)
    assert executed.execution is not None
    assert executed.execution.applied_configurations == (configuration,)
    assert executed.execution.kernel_scan_ids == (1,)
    assert executed.execution.owner_at(1) is not None
