"""Tests for pilot verify — gate pipeline for trial acceptance.

Coverage targets:
- verify_gates: the full gate sequence (avoid → target → spin → cycle → dead-end → outcome)
- _gate_spin: state-key change detection, excursion retry
- _gate_cycle: visited-key rejection, influence override
- _gate_dead_end: empty frontier, lateral detection, channel override
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pyrung import Bool, Program, Rung, out
from pyrung.core.analysis.pilot.types import _PulseState
from pyrung.core.analysis.pilot.verify import _gate_cycle, _gate_spin
from pyrung.core.runner import PLC

# ---------------------------------------------------------------------------
# Gate pipeline
# ---------------------------------------------------------------------------


class TestGateSpin:
    """Spin gate — trial must change the state key."""

    def test_no_change_is_spin(self):
        Source = Bool("SpinSource", external=True)
        Dest = Bool("SpinDest")
        with Program() as prog:
            with Rung(Source):
                out(Dest)
        plc = PLC(prog, dt=0.010)
        snap = dict(plc.state.tags)
        key = ("same",)
        trial = _PulseState(plc, 0, 0, snap, (), snap, key, snap, key)
        gates = []
        result = _gate_spin(
            trial,
            (("SpinSource", False),),
            SimpleNamespace(key=key, snap=snap),
            SimpleNamespace(key_config=object(), gauge=None, work=plc, rungs=[]),
            SimpleNamespace(),
            nogood_pair=("SpinSource", False),
            gate_events=gates,
            collected_nogoods=[],
            excursion_holds=[],
        )
        assert result is None
        assert gates[-1].event == "spin"
        assert gates[-1].evidence == {
            "frame_key": key,
            "trial_key": key,
            "post_pulse_key": key,
            "pending_effects": False,
            "ordinal_advanced": False,
            "actions": (("SpinSource", False),),
        }

    @pytest.mark.skip(reason="stub")
    def test_excursion_retried_with_holds(self): ...

    @pytest.mark.skip(reason="stub")
    def test_pending_effects_bypass_spin(self): ...


class TestGateCycle:
    """Cycle gate — new key must not have been visited."""

    def test_visited_key_rejected(self):
        key = ("visited",)
        trial = SimpleNamespace(key=key, snap={})
        gates = []
        accepted = _gate_cycle(
            trial,
            SimpleNamespace(snap={}),
            SimpleNamespace(seen_keys={key}, gauge=None),
            pending=False,
            influence_prescribed=False,
            nogood_pair=("Cmd", True),
            gate_events=gates,
            collected_nogoods=[],
        )
        assert accepted is False
        assert gates[-1].event == "cycle"
        assert gates[-1].evidence["trial_key"] == key
        assert gates[-1].evidence["seen"] is True
        assert gates[-1].evidence["influence_prescribed"] is False

    @pytest.mark.skip(reason="stub")
    def test_influence_prescribed_overrides_cycle(self): ...


class TestGateDeadEnd:
    """Dead-end gate — frontier must be non-empty or async pending."""

    @pytest.mark.skip(reason="stub")
    def test_empty_frontier_is_dead_end(self): ...

    @pytest.mark.skip(reason="stub")
    def test_channel_reached_overrides_dead_end(self): ...

    @pytest.mark.skip(reason="stub")
    def test_channel_ejected_overrides_dead_end(self): ...

    @pytest.mark.skip(reason="stub")
    def test_lateral_no_new_frontier_rejected(self): ...


class TestVerifyGates:
    """Full pipeline: target check -> spin -> cycle -> dead-end -> outcome."""

    @pytest.mark.skip(reason="stub")
    def test_target_reached_early_exit(self): ...

    @pytest.mark.skip(reason="stub")
    def test_avoid_predicate_rejects(self): ...

    @pytest.mark.skip(reason="stub")
    def test_zoom_result_routes_through_gates(self): ...
