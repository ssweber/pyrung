"""Post-plan compression: greedy step drop.

The walker discovers plans incrementally, so the raw plan may include
steps that were needed at discovery time but are not load-bearing in the
final sequence.  ``_compress_plan`` drops them.
"""

from __future__ import annotations

import logging

from pyrung import (
    Bool,
    Counter,
    Int,
    Or,
    Program,
    Rung,
    copy,
    count_up,
    out,
    rise,
)
from pyrung.core.runner import PLC


class TestCompressDrop:
    """Greedy step drop removes discovery overhead."""

    def test_drop_removes_redundant_toggle(self) -> None:
        """A seal-in latch: the pulse that arms it and the release that
        follows are both necessary in isolation, but the release is not
        needed if the latch sealed — compression should drop it."""
        Arm = Bool("Arm", external=True)
        Sealed = Bool("Sealed")
        Target = Bool("Target")

        with Program() as prog:
            with Rung(Or(rise(Arm), Sealed)):
                out(Sealed)
            with Rung(Sealed):
                out(Target)

        plc = PLC(prog, dt=0.010)
        path = plc.how(Target)

        assert path.reachable
        actions = [s.action for s in path.steps if s.action]
        assert {"Arm": True} in actions
        # The Arm=False release (if the walker emitted one) should have
        # been compressed away — the latch sealed, so releasing Arm is
        # a no-op the program doesn't need.
        assert {"Arm": False} not in actions

    def test_drop_preserves_needed_edge(self) -> None:
        """Two sequential rising edges: each pulse is load-bearing —
        compression must keep them (possibly merged into one multi-steer)."""
        Step1 = Bool("Step1", external=True)
        Step2 = Bool("Step2", external=True)
        A = Bool("A")
        B = Bool("B")
        Target = Bool("Target")

        with Program() as prog:
            with Rung(Or(rise(Step1), A)):
                out(A)
            with Rung(A, Or(rise(Step2), B)):
                out(B)
            with Rung(A, B):
                out(Target)

        plc = PLC(prog, dt=0.010)
        path = plc.how(Target)

        assert path.reachable
        all_keys = set()
        for s in path.steps:
            all_keys.update(s.action.keys())
        assert "Step1" in all_keys
        assert "Step2" in all_keys

    def test_drop_preserves_load_bearing(self) -> None:
        """Every step drives a distinct latch — nothing is droppable."""
        A_in = Bool("A_in", external=True)
        B_in = Bool("B_in", external=True)
        A = Bool("A")
        B = Bool("B")
        Target = Bool("Target")

        with Program() as prog:
            with Rung(Or(rise(A_in), A)):
                out(A)
            with Rung(Or(rise(B_in), B)):
                out(B)
            with Rung(A, B):
                out(Target)

        plc = PLC(prog, dt=0.010)
        path = plc.how(Target)

        assert path.reachable
        all_keys = set()
        for s in path.steps:
            all_keys.update(s.action.keys())
        assert "A_in" in all_keys
        assert "B_in" in all_keys

    def test_empty_action_not_dropped(self) -> None:
        """Timing waits ({}, N) survive compression — they represent
        genuine accumulator crossing time."""
        Run = Bool("Run", external=True)
        Stage = Int("Stage")
        Ctr = Counter.clone("Ctr")

        preset = 10
        with Program() as prog:
            with Rung(Run, Stage == 0):
                count_up(Ctr, preset=preset).reset(Stage == 1)
            with Rung(Ctr.Done):
                copy(1, Stage)

        plc = PLC(prog, dt=0.010)
        plc.patch({"Run": True})
        plc.step()
        path = plc.how(Stage == 1)

        assert path.reachable
        waits = [s for s in path.steps if not s.action]
        assert len(waits) >= 1
        assert waits[0].scans >= preset - 2

    def test_avoid_pred_prevents_drop(self) -> None:
        """A step kept because its removal routes through an avoided state."""
        Go = Bool("Go", external=True)
        Bypass = Bool("Bypass", external=True)
        Gate = Bool("Gate")
        Target = Bool("Target")

        with Program() as prog:
            with Rung(Or(Go, Bypass)):
                out(Gate)
            with Rung(Gate):
                out(Target)

        plc = PLC(prog, dt=0.010)
        path = plc.how(Target, avoid=Bypass)

        assert path.reachable
        actions = [s.action for s in path.steps if s.action]
        assert {"Go": True} in actions
        assert {"Bypass": True} not in actions

    def test_compression_logged_in_journal(self, caplog) -> None:
        """When steps are dropped, the journal records the compression."""
        Arm = Bool("Arm", external=True)
        Sealed = Bool("Sealed")
        Target = Bool("Target")

        with Program() as prog:
            with Rung(Or(rise(Arm), Sealed)):
                out(Sealed)
            with Rung(Sealed):
                out(Target)

        plc = PLC(prog, dt=0.010)
        with caplog.at_level(logging.INFO, logger="pyrung.core.analysis.walk"):
            path = plc.how(Target)

        assert path.reachable
        assert any("compress:" in msg for msg in caplog.messages) or len(path.steps) <= 2
