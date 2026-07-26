from __future__ import annotations

import pytest

from pyrung.core import (
    PLC,
    Bool,
    Int,
    Program,
    Rung,
    Timer,
    calc,
    call,
    copy,
    on_delay,
    out,
    rise,
    subroutine,
    system,
)
from pyrung.core.context import RungId


def test_replay_capture_uses_shared_state_slab_and_restores_force_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable = Bool("Enable")
    light = Bool("Light")

    with Program(strict=False) as program:
        with Rung(enable):
            out(light)

    source = PLC(program, dt=0.01)
    source.force("Enable", True)
    source.step()
    source.patch({"Enable": False})
    source.step()

    source._recent_state_cache.clear()
    source._recent_state_cache_bytes = 0
    source._cache_state(source.current_state)

    def _boom_replay_to(_scan_id: int) -> PLC:
        raise AssertionError("capture positioning should use the shared state slab")

    monkeypatch.setattr(source, "replay_to", _boom_replay_to)

    views = source._replay_node_views_at(2)

    assert views[RungId(None, 0)].get_tag("Enable") is True


def test_sparse_replay_seek_folds_to_exact_endpoint_across_recorded_events() -> None:
    enable = Bool("Enable", external=True)
    marker = Bool("Marker", external=True)
    timer = Timer.clone("ReplayFoldTmr")
    seen = Bool("Seen")

    with Program(strict=False) as program:
        with Rung(enable):
            on_delay(timer, 5000, "ms")
        with Rung(marker):
            out(seen)

    source = PLC(program, dt=0.01, checkpoint_interval=10_000)
    source.force(enable, True)
    for scan_id in range(1, 3001):
        if scan_id == 421:
            source.patch({marker.name: True})
        source.step()

    # Force both reconstructions to start at the initial state.  Checkpoint
    # force snapshots remain in the scan log and must not become false
    # boundaries when the effective force map is unchanged.
    source._checkpoints.clear()
    kernel = source._compiled_replay_supported_kernel()
    assert kernel is not None

    expected = source._replay_to_compiled(2999, kernel).state
    actual = source._replay_seek(2999).state

    assert actual.scan_id == expected.scan_id
    assert actual.timestamp == pytest.approx(expected.timestamp)
    assert dict(actual.tags) == dict(expected.tags)
    assert dict(actual.memory) == dict(expected.memory)
    assert source._last_replay_seek_stats["logical_scans"] == 2999
    assert source._last_replay_seek_stats["folded_scans"] > 2950
    assert source._last_replay_seek_stats["kernel_scans"] < 30
    assert not source._replay_slabs

    source._recent_state_cache.clear()
    source._recent_state_cache_bytes = 0
    source._cache_state(source.current_state)
    slab_state = source._state_at(2999)

    assert dict(slab_state.tags) == dict(expected.tags)
    assert len(next(iter(source._replay_slabs.values()))) == 1024
    assert source._last_replay_slab_stats == {
        "runup_scans": 1975,
        "materialized_states": 1024,
        "folded_runup": 1,
    }
    assert source._state_at(2998).scan_id == 2998


def test_sparse_replay_seek_preserves_clock_edge_memory() -> None:
    pulses = Int("ClockPulses")

    with Program(strict=False) as program:
        with Rung(rise(system.sys.clock_1s)):
            calc(pulses + 1, pulses)

    source = PLC(program, dt=0.01)
    source.run(333)
    source._checkpoints.clear()
    kernel = source._compiled_replay_supported_kernel()
    assert kernel is not None

    expected = source._replay_to_compiled(332, kernel).state
    actual = source._replay_seek(332).state

    assert actual.timestamp == pytest.approx(expected.timestamp)
    assert dict(actual.tags) == dict(expected.tags)
    assert dict(actual.memory) == dict(expected.memory)
    assert source._last_replay_seek_stats["folded_scans"] > 250


def test_replay_capture_reuses_source_pdg(monkeypatch: pytest.MonkeyPatch) -> None:
    enable = Bool("Enable")
    light = Bool("Light")

    with Program(strict=False) as program:
        with Rung(enable):
            out(light)

    source = PLC(program)
    source.step()
    source._ensure_pdg()

    def _unexpected_rebuild(_program: Program):
        raise AssertionError("reconstructed replay should reuse the source PDG")

    monkeypatch.setattr("pyrung.core.analysis.pdg.build_program_graph", _unexpected_rebuild)

    assert source._replay_node_views_at(1)


def test_replay_capture_does_not_commit_disposable_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable = Bool("Enable")
    light = Bool("Light")

    with Program(strict=False) as program:
        with Rung(enable):
            out(light)

    source = PLC(program)
    source.patch({"Enable": True})
    source.step()

    def _unexpected_commit(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("observed evidence is complete before commit")

    monkeypatch.setattr(PLC, "_commit_scan", _unexpected_commit)

    runs = source._replay_rung_runs_at(1)

    assert len(runs) == 1
    assert runs[0].enabled is True
    assert dict(runs[0].writes) == {"Light": True}


def test_replay_capture_preserves_repeated_subroutine_occurrences() -> None:
    source = Int("Source")
    result = Int("Result")

    @subroutine("Shared")
    def shared():
        with Rung():
            copy(source, result)

    with Program(strict=False) as program:
        with Rung():
            copy(1, source)
            call(shared)
            copy(2, source)
            call(shared)

    plc = PLC(program)
    plc.step()

    runs = [
        run
        for run in plc._replay_rung_runs_at(plc.state.scan_id)
        if run.rung_id == RungId("Shared", 0)
    ]

    assert len(runs) == 2
    assert [run.caller_rung for run in runs] == [0, 0]
    assert [run.view.get_tag(source.name) for run in runs] == [1, 2]
    assert [dict(run.writes)[result.name] for run in runs] == [1, 2]
    # The compact compatibility view intentionally remains last-occurrence.
    assert (
        plc._replay_node_views_at(plc.state.scan_id)[RungId("Shared", 0)].get_tag(source.name) == 2
    )
