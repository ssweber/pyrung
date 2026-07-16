"""Tests for the hang-forever survey (``query.wait_edges_without_escape``).

Synthetic step machines exercise the survey's four decisions:
- a wait-shaped step with no fireable escape is flagged,
- an escape that covers the step (``~FB`` at ``Step >= 1``) suppresses it,
- a live timeout escape suppresses it,
- an unreadable escape guard fails closed (no invented verdict).

The tumbler's exact-bug assertion lives in ``tests/tumbler`` behind the
``tumbler`` marker; these run in the default suite.
"""

from __future__ import annotations

from pyrung.core import Bool, Int, Or, Program, Rung, calc, copy
from pyrung.core.analysis.query import WaitEscapeFinding, wait_edges_without_escape


def _find(program: Program) -> list[WaitEscapeFinding]:
    return wait_edges_without_escape(program)


class TestWaitWithoutEscape:
    def test_step_range_escape_that_excludes_the_wait_step_is_flagged(self) -> None:
        """The escape guards ``Step == 3`` while the wait sits at step 1 — no
        fireable escape, so the step can hang forever."""
        Step = Int("Step")
        Acc = Int("Acc")
        Err = Int("Err")
        FB = Bool("FB")  # never written → external input

        with Program() as prog:
            with Rung(Step == 1, Acc > 2, FB):  # advance waits on FB
                calc(Step + 1, Step)
            with Rung(Step == 3, ~FB):  # error escape — wrong step
                copy(1, Err)

        findings = _find(prog)
        assert len(findings) == 1
        f = findings[0]
        assert f.step_register == "Step"
        assert f.step_value == 1
        assert f.wait_inputs == ("FB",)
        assert "FB" in f.message

    def test_covering_error_escape_is_not_flagged(self) -> None:
        """``Step >= 1, ~FB`` covers the wait step and fires when FB never
        arrives — a real self-escape (the blower shape)."""
        Step = Int("Step")
        Acc = Int("Acc")
        Err = Int("Err")
        FB = Bool("FB")

        with Program() as prog:
            with Rung(Step == 1, Acc > 2, FB):
                calc(Step + 1, Step)
            with Rung(Step >= 1, ~FB):
                copy(1, Err)

        assert _find(prog) == []

    def test_live_timeout_escape_is_not_flagged(self) -> None:
        """A timeout escape scoped to the step timer, live under config, fires
        without the wait input."""
        Step = Int("Step")
        Acc = Int("Acc")
        Limit = Int("Limit")
        Err = Int("Err")
        FB = Bool("FB")

        with Program() as prog:
            with Rung(Step == 1, Acc > 2, FB):
                calc(Step + 1, Step)
            with Rung(Acc >= Limit):  # tag-vs-tag compare → live timeout
                copy(1, Err)

        assert _find(prog) == []

    def test_dead_timeout_and_ranged_escape_message(self) -> None:
        """Both escapes fail (config-dead timeout + wrong-step error): flagged,
        and the message names each failed escape and the dead config value."""
        Step = Int("Step")
        Acc = Int("Acc")
        Limit = Int("Limit")
        EnableLimit = Int("EnableLimit")  # never written → constant 0
        Err = Int("Err")
        FB = Bool("FB")

        with Program() as prog:
            with Rung(Step == 1, Acc > 2, FB):  # R1 advance
                calc(Step + 1, Step)
            with Rung(Acc >= Limit, EnableLimit == 1):  # R2 dead timeout
                copy(1, Err)
            with Rung(Step == 3, ~FB):  # R3 wrong-step error
                copy(1, Err)

        findings = _find(prog)
        assert len(findings) == 1
        f = findings[0]
        assert f.advance_rung == "R1"
        assert f.dead_escapes == (("R2", "EnableLimit", 0),)
        assert f.ranged_escapes == (("R3", "Step", "eq", 3),)
        assert "R3 guards Step == 3" in f.message
        assert "EnableLimit = 0 disables the R2 timeout" in f.message

    def test_unreadable_escape_guard_fails_closed(self) -> None:
        """An ``Or`` escape guard that touches the step register cannot be
        decoded — the survey must not invent a 'no escape' verdict."""
        Step = Int("Step")
        Acc = Int("Acc")
        Err = Int("Err")
        FB = Bool("FB")

        with Program() as prog:
            with Rung(Step == 1, Acc > 2, FB):
                calc(Step + 1, Step)
            with Rung(Or(Step == 1, Step == 3), ~FB):  # unreadable, step-scoped
                copy(1, Err)

        assert _find(prog) == []

    def test_self_advancing_step_without_external_input_is_not_a_wait(self) -> None:
        """A step advanced on a timer (no external input) is not wait-shaped."""
        Step = Int("Step")
        Acc = Int("Acc")

        with Program() as prog:
            with Rung(Step == 1, Acc > 2):  # no external input in guard
                calc(Step + 1, Step)

        assert _find(prog) == []
