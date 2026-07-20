from __future__ import annotations

import pytest

from pyrung.core import PLC, Bool, Int, Program, Rung, call, copy, out, subroutine
from pyrung.core.context import RungId


def test_replay_capture_uses_state_slab_and_restores_force_map(
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
        raise AssertionError("capture positioning should use state slabs")

    monkeypatch.setattr(source, "replay_to", _boom_replay_to)

    views = source._replay_node_views_at(2)

    assert views[RungId(None, 0)].get_tag("Enable") is True


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
