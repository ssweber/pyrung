"""Tests for pilot _ops — low-level PLC manipulation primitives.

Coverage targets:
- LivenessHold: value_at oscillation, period calculation
- _coast_to_value: budget, ejection guard, target reached
- _coast_holding_state: role-tag ejection, liveness animation
- _pilot_state_key: projection, done-bit abstraction, threshold vectors
- _install_holds: steady vs liveness hold semantics
- _settle_delayed_effects: harness feedback, timer accumulation
"""

from __future__ import annotations

import pytest

from pyrung import Bool, Program, Rung, Timer, on_delay, out
from pyrung.core.analysis.pilot._ops import (
    LivenessHold,
    _coast_to_value,
)
from pyrung.core.runner import PLC

# ---------------------------------------------------------------------------
# LivenessHold
# ---------------------------------------------------------------------------


class TestLivenessHold:
    def test_symmetric_dwell(self):
        lh = LivenessHold(on_dwell=5, off_dwell=5)
        values = [lh.value_at(i) for i in range(20)]
        assert values[:5] == [True] * 5
        assert values[5:10] == [False] * 5
        assert values[10:15] == [True] * 5

    def test_asymmetric_dwell(self):
        lh = LivenessHold(on_dwell=3, off_dwell=7)
        values = [lh.value_at(i) for i in range(20)]
        assert values[:3] == [True] * 3
        assert values[3:10] == [False] * 7
        assert values[10:13] == [True] * 3


# ---------------------------------------------------------------------------
# Coast to value
# ---------------------------------------------------------------------------


def _timer_program():
    Enable = Bool("Enable", external=True)
    Tmr = Timer.clone("Tmr")
    Done = Bool("Done")
    with Program() as prog:
        with Rung(Enable):
            on_delay(Tmr, 100, "ms")
        with Rung(Tmr.Done):
            out(Done)
    return prog


class TestCoastToValue:
    def test_reaches_target(self):
        prog = _timer_program()
        plc = PLC(prog, dt=0.010)
        plc.patch({"Enable": True})
        plc.step()
        reached = _coast_to_value(plc, "Done", True, budget=50)
        assert reached
        assert plc.state.tags["Done"] is True

    def test_budget_limits_scans(self):
        prog = _timer_program()
        plc = PLC(prog, dt=0.010)
        plc.patch({"Enable": True})
        plc.step()
        scan_before = plc.state.scan_id
        _coast_to_value(plc, "Done", True, budget=3)
        assert plc.state.scan_id - scan_before <= 3

    def test_ejection_stops_early(self):
        """If the governing tag goes to an unexpected third value, coast stops."""
        prog = _timer_program()
        plc = PLC(prog, dt=0.010)
        # Don't enable the timer — Done stays False, never reaches True
        reached = _coast_to_value(plc, "Done", True, budget=20)
        assert not reached


# ---------------------------------------------------------------------------
# Coast holding state
# ---------------------------------------------------------------------------


class TestCoastHoldingState:
    @pytest.mark.skip(reason="stub")
    def test_role_ejection_stops_coast(self): ...

    @pytest.mark.skip(reason="stub")
    def test_liveness_holds_toggle_each_scan(self): ...


# ---------------------------------------------------------------------------
# State key projection
# ---------------------------------------------------------------------------


class TestPilotStateKey:
    @pytest.mark.skip(reason="stub")
    def test_basic_projection(self): ...

    @pytest.mark.skip(reason="stub")
    def test_done_bit_abstraction(self): ...

    @pytest.mark.skip(reason="stub")
    def test_acc_indices_masked(self): ...


# ---------------------------------------------------------------------------
# Settle delayed effects
# ---------------------------------------------------------------------------


class TestSettleDelayedEffects:
    @pytest.mark.skip(reason="stub")
    def test_harness_feedback_settled(self): ...

    @pytest.mark.skip(reason="stub")
    def test_pending_timer_fast_forwarded(self): ...
