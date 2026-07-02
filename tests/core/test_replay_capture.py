from __future__ import annotations

import pytest

from pyrung.core import PLC, Bool, Program, Rung, out
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
