"""PILOT runtime acceptance regressions for the PackML bench."""

from __future__ import annotations

import pytest

# ===================================================================
# Test 14: Hidden-event jump must fire only on a self-loop
# ===================================================================


class TestHiddenEventJumpSelfLoopOnly:
    """A hidden-event jump models time elapsing while the program stays on ONE
    plateau, so it must only fire as a self-loop.

    Root cause (distinct from the cross-product gap above): expanding STOPPED
    with an input assignment that *transitioned* to RESETTING — already visited,
    with a pending StateTimer — triggered a jump that fast-forwarded the timer
    and rode the unconditional RESETTING→IDLE auto-completion to IDLE. The
    jumped IDLE state was attributed to STOPPED, collapsing the intermediate
    transition out of the trace and clobbering the rising CmdChgRequest edge
    that drove it (the jump's edge-variant loop re-emits CmdChgRequest=False).
    ``how(IDLE)`` then returned a path that failed its own replay verification.
    """

    def test_how_idle_path_replays(self):
        from examples.packml_bench import S, StateCurrent, logic
        from pyrung.core.runner import PLC

        plc = PLC(logic, dt=0.010)
        path = plc.how(StateCurrent == "IDLE")
        assert path.reachable, f"how(IDLE) should be reachable, got: {path.reason}"

        # Two-oracle check: the abstract BFS trace must replay to IDLE on a
        # concrete PLC.  A jump mis-attributed across a transition produces a
        # trace whose inputs no longer reproduce the path.
        replay = path.replay()
        assert replay.state.tags["StateCurrent"] == S.IDLE.default, (
            f"replayed path ended at StateCurrent="
            f"{replay.state.tags['StateCurrent']}, expected IDLE ({S.IDLE.default})"
        )


# ===================================================================
# Test 15: Multi-scan drive from ABORTED to EXECUTE
# ===================================================================


class TestHowAbortedToExecute:
    """PILOT must steer a long multi-scan transition through the PackML state
    machine: ABORTED → CLEARING → STOPPED → RESETTING → IDLE → STARTING →
    EXECUTE.

    Regression for the hidden-event-jump bug (commit 8aa66f4) which caused
    ``how(EXECUTE)`` to return "path found but replay verification failed"
    because a jump across a state transition collapsed intermediate states
    out of the trace.
    """

    def test_how_execute_from_aborted_replays(self):
        from examples.packml_bench import (
            CmdAbort,
            CmdChgRequest,
            S,
            StateCurrent,
            logic,
        )
        from pyrung.core.runner import PLC

        # Seed PLC to ABORTED state
        plc = PLC(logic, dt=0.010)
        plc.step()  # init scan → STOPPED
        plc.patch({CmdAbort: True, CmdChgRequest: True})
        plc.step()  # ABORTING
        plc.step()  # ABORTED
        assert plc.state.tags["StateCurrent"] == S.ABORTED.default

        path = plc.how(StateCurrent == S.EXECUTE.default)
        assert path.reachable, f"how(EXECUTE) should be reachable, got: {path.reason}"
        assert path.total_changes >= 2, "ABORTED→EXECUTE requires multiple operator changes"

        # Two-oracle check: replay on a fresh PLC must reach EXECUTE
        replay = path.replay()
        assert replay.state.tags["StateCurrent"] == S.EXECUTE.default, (
            f"replayed path ended at StateCurrent="
            f"{replay.state.tags['StateCurrent']}, expected EXECUTE ({S.EXECUTE.default})"
        )


# ===================================================================
# Bench-fix regressions (spec §1.5) — pin the repaired packml_bench
# ===================================================================


def _seed_execute(plc):
    """Cold-start → EXECUTE via the edge-gated command path."""
    from examples.packml_bench import CmdChgRequest, CmdReset, CmdStart

    plc.step()  # init scan → STOPPED
    plc.patch({CmdReset: True, CmdChgRequest: True})
    plc.step()  # → RESETTING
    plc.patch({CmdReset: False, CmdChgRequest: False})
    plc.step()  # RESETTING → IDLE (auto-complete)
    plc.patch({CmdStart: True, CmdChgRequest: True})
    plc.step()  # → STARTING
    plc.patch({CmdStart: False, CmdChgRequest: False})
    plc.step()  # STARTING → EXECUTE (auto-complete)
    return plc


class TestHowHeldFromExecute:
    """§1.5: how(HELD) from EXECUTE — a coordinated level command (CmdHold)
    and a rising edge (rise(CmdChgRequest)) must land on the same scan, and
    CtrlCmd is both external and program-written, so ``how`` has two routes to
    ``CtrlCmd == 4``."""

    def test_how_held_replays(self):
        from examples.packml_bench import S, StateCurrent, logic
        from pyrung.core.runner import PLC

        plc = PLC(logic, dt=0.010)
        _seed_execute(plc)
        assert plc.state.tags["StateCurrent"] == S.EXECUTE.default

        path = plc.how(StateCurrent == S.HELD.default)
        assert path.reachable, f"how(HELD) should be reachable, got: {path.reason}"

        replay = path.replay()
        assert replay.state.tags["StateCurrent"] == S.HELD.default, (
            f"replayed path ended at StateCurrent="
            f"{replay.state.tags['StateCurrent']}, expected HELD ({S.HELD.default})"
        )


class TestHowCompletedFromColdStart:
    """§1.5 + §1.3: with the EXECUTE→COMPLETING row wired, COMPLETED is now
    reachable through the *internal* cycle (STARTING auto-advance, EXECUTE task
    dwell, COMPLETING → COMPLETED) from a cold start — not only via the external
    Complete command."""

    def test_how_completed_replays(self):
        from examples.packml_bench import S, StateCurrent, logic
        from pyrung.core.runner import PLC

        plc = PLC(logic, dt=0.010)
        path = plc.how(StateCurrent == S.COMPLETED.default)
        assert path.reachable, f"how(COMPLETED) should be reachable, got: {path.reason}"

        replay = path.replay()
        assert replay.state.tags["StateCurrent"] == S.COMPLETED.default, (
            f"replayed path ended at StateCurrent="
            f"{replay.state.tags['StateCurrent']}, expected COMPLETED ({S.COMPLETED.default})"
        )


@pytest.mark.xfail(
    strict=True,
    reason="Causal attribution of an overflow-driven ABORTED through the affine "
    "LoopIndex counter and the ds[150+StateRequested] jump table is not yet "
    "supported; why() reports 'no writer has fired' at the transition scan.",
)
class TestWhyAbortedViaOverflow:
    """§1.5: ``why StateCurrent == ABORTED`` must attribute the transition to the
    runaway guard (``LoopIndex > 10`` → copy(ABORTED, StateRequested)) and trace
    back through the affine counter and the jump table.  Uses the command path to
    reach ABORTED as a proxy for the attribution capability; the full overflow
    seeding additionally needs a never-resolving jump-table cycle that the current
    bench data does not produce."""

    def test_why_names_the_writer(self):
        from examples.packml_bench import CmdAbort, CmdChgRequest, logic
        from pyrung.core.runner import PLC

        plc = PLC(logic, dt=0.010)
        plc.step()
        plc.patch({CmdAbort: True, CmdChgRequest: True})
        plc.step()  # → ABORTING
        plc.patch({CmdAbort: False, CmdChgRequest: False})
        plc.step()  # → ABORTED

        chain = plc.why("StateCurrent")
        assert "no writer" not in str(chain), (
            "why(StateCurrent) at ABORTED must name the writer that set it, "
            "not report 'no writer has fired'"
        )


class TestHowIntoModeDisabledState:
    """§1.5: driven into Manual mode (DisabledStates=0x0224 blocks STARTING via
    the StateMask), ``how(STARTING)`` must either discover the mode-change route
    or fail loudly with a reason — never silently return an unreachable/wrong plan.
    """

    @staticmethod
    def _manual_idle_plc():
        from examples.packml_bench import (
            CmdChgRequest,
            CmdReset,
            ModeChgRequest,
            UnitModeCmd,
            logic,
        )
        from pyrung.core.runner import PLC

        plc = PLC(logic, dt=0.010)
        plc.step()  # init → STOPPED, Production
        # Switch to Manual mode via the external UnitModeCmd + request path.
        for _ in range(2):
            plc.patch({UnitModeCmd: 3, ModeChgRequest: True})
            plc.step()
        plc.patch({UnitModeCmd: 0, ModeChgRequest: False})
        plc.patch({CmdReset: True, CmdChgRequest: True})
        plc.step()  # → RESETTING
        plc.patch({CmdReset: False, CmdChgRequest: False})
        plc.step()  # → IDLE
        assert plc.state.tags["DisabledStates"] == 0x0224
        return plc

    def test_how_disabled_state_is_loud(self):
        """Never a silent unreachable: the loop names why it gave up.

        STARTING is blocked in Manual, so from Manual the transition needs a mode
        change first.  Even when the pilot can't complete that multi-phase plan it
        must surface a reason (``stuck: …`` / ``budget exhausted``), not
        ``reachable=False, reason=None``.
        """
        from examples.packml_bench import StateCurrent

        plc = self._manual_idle_plc()
        path = plc.how(StateCurrent == 3, max_scans=300)  # 3 == STARTING
        assert path.reachable or path.reason is not None, (
            "how() into a mode-disabled state returned a silent unreachable "
            "(reachable=False, reason=None) — the loop must report why it gave up"
        )

    def test_how_disabled_state_reaches(self):
        """The real goal: discover and drive the mode change, then reach STARTING.

        Staged bearings: ``how(STARTING)`` from Manual surfaces the mode change as a
        stage-0 ``establish`` prerequisite (the tide tables invert the disabled-
        state mask over the mode domain), drives ``UnitModeCmd``/``ModeChgRequest``
        to a mask-clearing mode and lets it settle, then — once the gate re-reads
        satisfied — pursues the deferred stage-1 command to land on STARTING.
        """
        from examples.packml_bench import StateCurrent

        plc = self._manual_idle_plc()
        path = plc.how(StateCurrent == 3, max_scans=400)
        assert path.reachable
        assert path.replay().state.tags["StateCurrent"] == 3
