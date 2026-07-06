"""Tests for pilot steer — Act instrument mechanics.

Coverage targets:
- _settle_cone: dwell control, fixpoint detection
- _letrun_zoom: governing-register coast, ejection guard, settle fallback
- _try_zoom / _try_terminal_letrun: full-context wrappers (stubbed — exercised
  through the pilot_how integration path rather than direct unit calls)
"""

from __future__ import annotations

import pytest

from pyrung import Bool, Int, Program, Rung, Timer, copy, on_delay, out
from pyrung.core.analysis.pilot.steer import _letrun_zoom, _settle_cone
from pyrung.core.runner import PLC

# ---------------------------------------------------------------------------
# Programs
# ---------------------------------------------------------------------------


def _follow_program() -> Program:
    """Out follows In with no internal state — settles to a fixpoint at once."""
    In = Bool("In", external=True)
    Out = Bool("Out")
    with Program() as prog:
        with Rung(In):
            out(Out)
    return prog


def _stage_program(target_val: int) -> Program:
    """A governing register Stage jumps to *target_val* when a timer fires."""
    Enable = Bool("Enable", external=True)
    Tmr = Timer.clone("Tmr")
    Stage = Int("Stage", default=0)
    with Program() as prog:
        with Rung(Enable):
            on_delay(Tmr, 30, "ms")
        with Rung(Tmr.Done):
            copy(target_val, Stage)
    return prog


def _timer_program() -> Program:
    Enable = Bool("Enable", external=True)
    Tmr = Timer.clone("Tmr")
    Done = Bool("Done")
    with Program() as prog:
        with Rung(Enable):
            on_delay(Tmr, 100, "ms")
        with Rung(Tmr.Done):
            out(Done)
    return prog


# ---------------------------------------------------------------------------
# Cone settlement
# ---------------------------------------------------------------------------


class TestSettleCone:
    """_settle_cone: coast until cone tags stop moving."""

    def test_fixpoint_within_ceiling(self):
        plc = PLC(_follow_program(), dt=0.010)
        plc.force("In", True)
        plc.step()
        snaps = _settle_cone(plc, frozenset({"Out"}), floor=2, ceiling=16)
        # Out is steady, so settle stops at the floor — well under the ceiling.
        assert len(snaps) < 16
        assert snaps[-1]["Out"] == snaps[-2]["Out"]

    def test_floor_minimum_respected(self):
        plc = PLC(_follow_program(), dt=0.010)
        plc.force("In", True)
        plc.step()
        # Already at a fixpoint, but the floor forces a minimum dwell.
        snaps = _settle_cone(plc, frozenset({"Out"}), floor=5, ceiling=16)
        assert len(snaps) == 5


# ---------------------------------------------------------------------------
# Zoom
# ---------------------------------------------------------------------------


class TestZoom:
    """_letrun_zoom: coast past timer/step-counter plateaus."""

    def test_governing_tag_reaches_target(self):
        plc = PLC(_stage_program(5), dt=0.010)
        plc.patch({"Enable": True})
        plc.step()
        snaps = _letrun_zoom(plc, "Stage", 5, frozenset({"Stage"}))
        assert snaps[-1]["Stage"] == 5
        assert plc.state.tags["Stage"] == 5

    def test_ejection_guard_stops_zoom(self):
        # Target 9, but the program drives Stage to 5 — a third value that is
        # neither the start (0) nor the target (9), so the guard ejects.
        plc = PLC(_stage_program(5), dt=0.010)
        plc.patch({"Enable": True})
        plc.step()
        assert plc.state.tags["Stage"] == 0  # zoom start value
        snaps = _letrun_zoom(plc, "Stage", 9, frozenset({"Stage"}))
        assert snaps[-1]["Stage"] != 9
        assert snaps[-1]["Stage"] == 5

    def test_no_governing_tag_falls_back_to_settle(self):
        plc = PLC(_timer_program(), dt=0.010)
        plc.patch({"Enable": True})
        plc.step()
        snaps = _letrun_zoom(plc, None, None, frozenset({"Done"}))
        # Settle fallback returns the per-scan trajectory (>= floor), not the
        # single final snapshot a governing coast returns.
        assert len(snaps) >= 2


# ---------------------------------------------------------------------------
# Terminal let-run — needs full _PilotContext/_PilotState/_IterationFrame
# fixtures; best exercised through the pilot_how integration path.
# ---------------------------------------------------------------------------


class TestTerminalLetrun:
    """_try_terminal_letrun: generalized bottom-of-loop fallback."""

    @pytest.mark.skip(reason="stub — needs full pilot context; cover via integration")
    def test_target_reached_is_confirmed(self): ...

    @pytest.mark.skip(reason="stub — needs full pilot context; cover via integration")
    def test_role_ejection_is_ambient_drift(self): ...

    @pytest.mark.skip(reason="stub — needs full pilot context; cover via integration")
    def test_stall_is_dead_end(self): ...

    @pytest.mark.skip(reason="stub — needs full pilot context; cover via integration")
    def test_liveness_holds_animated_during_coast(self): ...


# ---------------------------------------------------------------------------
# Pulse execution & try-verify wrappers — full-context behaviors not yet
# covered by any stub.  These need _PilotContext/_PilotState/_IterationFrame
# fixtures; best driven through the pilot_how integration path.
# ---------------------------------------------------------------------------


class TestPulseActions:
    """_apply_actions: rising-edge release, wait settle cone, delayed effects."""

    @pytest.mark.skip(reason="stub — needs full pilot context; cover via integration")
    def test_rising_edge_release_then_apply(self): ...

    @pytest.mark.skip(reason="stub — needs full pilot context; cover via integration")
    def test_wait_settle_cone_recorded(self): ...

    @pytest.mark.skip(reason="stub — needs full pilot context; cover via integration")
    def test_delayed_effects_settled(self): ...


class TestTryCandidate:
    """_try_candidate: single candidate plus its trace-context pulse."""

    @pytest.mark.skip(reason="stub — needs full pilot context; cover via integration")
    def test_candidate_with_influence_context(self): ...

    @pytest.mark.skip(reason="stub — needs full pilot context; cover via integration")
    def test_single_candidate_no_context(self): ...


class TestTryWidening:
    """_try_widening: progressively widen the trace-action batch."""

    @pytest.mark.skip(reason="stub — needs full pilot context; cover via integration")
    def test_progressive_width_increase(self): ...

    @pytest.mark.skip(reason="stub — needs full pilot context; cover via integration")
    def test_first_success_stops_loop(self): ...

    @pytest.mark.skip(reason="stub — needs full pilot context; cover via integration")
    def test_all_widths_fail_accumulates_nogoods(self): ...


class TestActionCausedChange:
    """_action_caused_change: separate control-driven from ambient changes."""

    @pytest.mark.skip(reason="stub — needs causal history fixture")
    def test_control_change_accepted(self): ...

    @pytest.mark.skip(reason="stub — needs causal history fixture")
    def test_ambient_change_filtered(self): ...


class TestCompassObservations:
    """_compass_observations: transitions, no-change, ambient filtering."""

    @pytest.mark.skip(reason="stub — needs compass + frame fixture")
    def test_observes_transitions(self): ...

    @pytest.mark.skip(reason="stub — needs compass + frame fixture")
    def test_no_change_contradicts_when_enabled(self): ...

    @pytest.mark.skip(reason="stub — needs compass + frame fixture")
    def test_ambient_changes_filtered_with_fork(self): ...
