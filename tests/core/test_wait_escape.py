"""Tests for the hang-forever survey (``query.wait_edges_without_escape``).

Synthetic step machines exercise the survey's decisions:
- a wait-shaped step with no fireable escape is flagged,
- an escape that covers the step (``~FB`` at ``Step >= 1``) suppresses it,
- a live timeout escape suppresses it,
- an unreadable escape guard fails closed (no invented verdict),
- the wording never guesses whether a tag is config or an operator's.

``TestIdioms`` covers *how the step machine is written* — the same bug expressed
four ways, each of which must be found, plus the shapes that must stay silent.

**Timers must be real.** A bare ``Int("Acc")`` nothing writes is an input, not a
timer: the ladder can never make ``Acc > 2`` true, so it is a wait and the survey
says so. Every test here that means "the timer runs" builds one with ``on_delay``.
``test_never_written_pseudo_timer_is_not_an_escape`` pins the distinction.

The tumbler's exact-bug assertion lives in ``tests/tumbler`` behind the
``tumbler`` marker; these run in the default suite.
"""

from __future__ import annotations

from pyrung.core import Bool, Int, Or, Program, Rung, Timer, calc, copy, on_delay, rise
from pyrung.core.analysis.query import WaitEscapeFinding, wait_edges_without_escape


def _find(program: Program) -> list[WaitEscapeFinding]:
    return wait_edges_without_escape(program)


class TestWaitWithoutEscape:
    def test_step_range_escape_that_excludes_the_wait_step_is_flagged(self) -> None:
        """The escape guards ``Step == 3`` while the wait sits at step 1 — no
        fireable escape, so the step can hang forever."""
        Step = Int("Step")
        Err = Int("Err")
        FB = Bool("FB")  # never written → the program cannot supply it
        Tmr = Timer.clone("Tmr")

        with Program() as prog:
            with Rung(Step == 1):
                on_delay(Tmr, preset=5000)
            with Rung(Step == 1, Tmr.Acc > 2, FB):  # advance waits on FB
                calc(Step + 1, Step)
            with Rung(Step == 3, ~FB):  # error escape — wrong step
                copy(1, Err)

        findings = _find(prog)
        assert len(findings) == 1
        f = findings[0]
        assert f.step_register == "Step"
        assert f.step_value == 1
        # only FB: the program drives Tmr.Acc itself, so it is not waited on
        assert f.wait_inputs == ("FB",)
        assert "FB" in f.message

    def test_covering_error_escape_is_not_flagged(self) -> None:
        """``Step >= 1, ~FB`` covers the wait step and fires when FB never
        arrives — a real self-escape (the blower shape)."""
        Step = Int("Step")
        Err = Int("Err")
        FB = Bool("FB")
        Tmr = Timer.clone("Tmr")

        with Program() as prog:
            with Rung(Step == 1):
                on_delay(Tmr, preset=5000)
            with Rung(Step == 1, Tmr.Acc > 2, FB):
                calc(Step + 1, Step)
            with Rung(Step >= 1, ~FB):
                copy(1, Err)

        assert _find(prog) == []

    def test_live_timeout_escape_is_not_flagged(self) -> None:
        """A real timer escape fires without the wait input, so nothing is reported.

        The timer must be a genuine ``on_delay`` — the program authors ``Tmr.Acc``
        every scan, which is exactly what makes the escape something the ladder can
        fire unaided.  A bare ``Int("Acc")`` nothing writes would be an *input*, and
        an escape waiting on an input is no escape at all (see
        ``test_never_written_pseudo_timer_is_not_an_escape``).
        """
        Step = Int("Step")
        Limit = Int("Limit")
        Err = Int("Err")
        FB = Bool("FB")
        Tmr = Timer.clone("Tmr")

        with Program() as prog:
            with Rung(Step == 1):
                on_delay(Tmr, preset=5000)
            with Rung(Step == 1, Tmr.Acc > 2, FB):
                calc(Step + 1, Step)
            with Rung(Tmr.Acc >= Limit):  # live timeout: the program drives Acc
                copy(1, Err)

        assert _find(prog) == []

    def test_never_written_pseudo_timer_is_not_an_escape(self) -> None:
        """An escape gated on a register nothing writes needs the world, not the program.

        ``Acc >= Limit`` looks like a timeout but no instruction advances ``Acc``;
        it sits at its default forever.  The ladder cannot fire this rung on its
        own, so it does not rescue the step.
        """
        Step = Int("Step")
        Acc = Int("Acc")  # never written — an input wearing a timer's name
        Limit = Int("Limit")
        Err = Int("Err")
        FB = Bool("FB")

        with Program() as prog:
            with Rung(Step == 1, Acc > 2, FB):
                calc(Step + 1, Step)
            with Rung(Acc >= Limit):
                copy(1, Err)

        assert len(_find(prog)) == 1

    def test_unmet_timeout_and_ranged_escape_message(self) -> None:
        """Both escapes fail (config-gated timeout + wrong-step error): flagged,
        and the message names each failed escape and why it cannot fire."""
        Step = Int("Step")
        Limit = Int("Limit")
        EnableLimit = Int("EnableLimit")  # never written → rests at 0
        Err = Int("Err")
        FB = Bool("FB")
        Tmr = Timer.clone("Tmr")

        with Program() as prog:
            with Rung(Step == 1):  # R1 timer
                on_delay(Tmr, preset=5000)
            with Rung(Step == 1, Tmr.Acc > 2, FB):  # R2 advance
                calc(Step + 1, Step)
            with Rung(Tmr.Acc >= Limit, EnableLimit == 1):  # R3 config-gated timeout
                copy(1, Err)
            with Rung(Step == 3, ~FB):  # R4 wrong-step error
                copy(1, Err)

        findings = _find(prog)
        assert len(findings) == 1
        f = findings[0]
        assert f.advance_rung == "R2"

        assert len(f.unmet_escapes) == 1
        unmet = f.unmet_escapes[0]
        assert (unmet.rung_label, unmet.tag, unmet.resting) == ("R3", "EnableLimit", 0)

        assert len(f.ranged_escapes) == 1
        ranged = f.ranged_escapes[0]
        assert (ranged.rung_label, ranged.step, ranged.op, ranged.bound) == ("R4", "Step", "eq", 3)

        assert "R4 guards Step == 3" in f.message
        assert "R3 needs EnableLimit, which nothing sets (rests at 0)" in f.message

    def test_message_never_guesses_config_versus_operator(self) -> None:
        """An operator button and a config register get the same neutral wording.

        Both rest where they rest and the ladder moves neither; the survey reports
        that fact and leaves the intent to the engineer (a Bool named EnableLimit
        would fool any type-based guess).
        """
        Step = Int("Step")
        Acc = Int("Acc")
        Err = Int("Err")
        FB = Bool("FB")
        Abort = Bool("i_AbortBtn")

        with Program() as prog:
            with Rung(Step == 1, Acc > 2, FB):
                calc(Step + 1, Step)
            with Rung(Step == 1, Abort):  # the only escape is a human
                copy(1, Err)

        findings = _find(prog)
        assert len(findings) == 1
        message = findings[0].message
        assert "R2 needs i_AbortBtn, which nothing sets (rests at False)" in message
        # never described as a disabled timeout — it is a button nobody pressed
        assert "timeout" not in message
        assert "disables" not in message

    def test_sole_escape_candidate_is_still_explained(self) -> None:
        """One escape rung in the whole program still earns its diagnostic.

        The fault-sink floor drops lone bookkeeping writes when several candidates
        compete; with a single candidate there is nothing to disambiguate, and the
        floor must not eat the only explanation the finding has.
        """
        Step = Int("Step")
        Acc = Int("Acc")
        Err = Int("Err")
        FB = Bool("FB")

        with Program() as prog:
            with Rung(Step == 1, Acc > 2, FB):
                calc(Step + 1, Step)
            with Rung(Step == 3, ~FB):  # the only escape rung, and it misses step 1
                copy(1, Err)

        findings = _find(prog)
        assert len(findings) == 1
        assert len(findings[0].ranged_escapes) == 1, "sole candidate must still be reported"
        assert "R2 guards Step == 3" in findings[0].message

    def test_unreadable_escape_guard_fails_closed(self) -> None:
        """An ``Or`` escape guard that touches the step register cannot be
        decoded — the survey must not invent a 'no escape' verdict."""
        Step = Int("Step")
        Err = Int("Err")
        FB = Bool("FB")
        Tmr = Timer.clone("Tmr")

        with Program() as prog:
            with Rung(Step == 1):
                on_delay(Tmr, preset=5000)
            with Rung(Step == 1, Tmr.Acc > 2, FB):
                calc(Step + 1, Step)
            with Rung(Or(Step == 1, Step == 3), ~FB):  # unreadable, step-scoped
                copy(1, Err)

        assert _find(prog) == []

    def test_autonomous_step_without_external_input_is_not_a_wait(self) -> None:
        """A step advanced by a real timer is not wait-shaped: it moves on its own.

        The timer must be genuine.  ``Int("Acc")`` that nothing writes would be an
        input, and ``Acc > 2`` a threshold the ladder can never cross — a wait, and
        correctly flagged as one.
        """
        Step = Int("Step")
        Tmr = Timer.clone("Tmr")

        with Program() as prog:
            with Rung(Step == 1):
                on_delay(Tmr, preset=5000)
            with Rung(Step == 1, Tmr.Acc > 2):  # nothing here comes from outside
                calc(Step + 1, Step)

        assert _find(prog) == []


class TestIdioms:
    """The same hang, written four ways — each must be recognized.

    A step machine can advance by incrementing a counter or by stamping the next
    state number in; it can be gated on a level or an edge; it can wait on a
    contact or on an analog threshold.  None of that changes whether the machine
    can sit there forever, so none of it may change the verdict.  Each test below
    is one wrong answer the survey used to give.
    """

    def test_copy_literal_sequencer_is_recognized(self) -> None:
        """``copy(2, Step)`` — the state machine that moves a state number in."""
        Step = Int("Step")
        Err = Int("Err")
        FB = Bool("FB")
        Tmr = Timer.clone("Tmr")

        with Program() as prog:
            with Rung(Step == 1):
                on_delay(Tmr, preset=5000)
            with Rung(Step == 1, Tmr.Acc > 2, FB):
                copy(2, Step)  # not an increment — a literal stamp
            with Rung(Step == 3, ~FB):
                copy(1, Err)

        findings = _find(prog)
        assert len(findings) == 1
        assert findings[0].step_register == "Step"
        assert findings[0].step_value == 1
        assert findings[0].wait_inputs == ("FB",)

    def test_edge_triggered_advance_is_recognized(self) -> None:
        """``rise(FB)`` needs FB *more* than ``FB`` does — still a wait."""
        Step = Int("Step")
        Err = Int("Err")
        FB = Bool("FB")
        Tmr = Timer.clone("Tmr")

        with Program() as prog:
            with Rung(Step == 1):
                on_delay(Tmr, preset=5000)
            with Rung(Step == 1, Tmr.Acc > 2, rise(FB)):
                calc(Step + 1, Step)
            with Rung(Step == 3, ~FB):
                copy(1, Err)

        findings = _find(prog)
        assert len(findings) == 1
        assert findings[0].wait_inputs == ("FB",)

    def test_analog_threshold_wait_is_recognized(self) -> None:
        """Waiting on ``S_Temp >= 200`` is waiting, the same as on a contact."""
        Step = Int("Step")
        Err = Int("Err")
        FB = Bool("FB")
        Temp = Int("S_Temp", external=True)
        Tmr = Timer.clone("Tmr")

        with Program() as prog:
            with Rung(Step == 1):
                on_delay(Tmr, preset=5000)
            with Rung(Step == 1, Tmr.Acc > 2, Temp >= 200):
                calc(Step + 1, Step)
            with Rung(Step == 3, ~FB):
                copy(1, Err)

        findings = _find(prog)
        assert len(findings) == 1
        assert findings[0].wait_inputs == ("S_Temp",)

    def test_copy_literal_to_a_non_step_register_is_not_a_sequencer(self) -> None:
        """``copy(1, Err)`` is a fault write; the survey must not read it as a step.

        The discriminator is self-reference: a sequencer READS the register it
        writes.  ``Err`` is only ever written, so it is not a step machine and its
        rungs are not advances.
        """
        Step = Int("Step")
        Err = Int("Err")
        FB = Bool("FB")
        Tmr = Timer.clone("Tmr")

        with Program() as prog:
            with Rung(Step == 1):
                on_delay(Tmr, preset=5000)
            with Rung(Step == 1, Tmr.Acc > 2, FB):
                calc(Step + 1, Step)
            with Rung(Step == 3, ~FB):
                copy(1, Err)

        findings = _find(prog)
        assert {f.step_register for f in findings} == {"Step"}, "Err must not be a step register"

    def test_falling_edge_advance_fails_closed(self) -> None:
        """``fall(X)`` needs X to have been true then drop — a resting value cannot
        say whether that happens, so the survey stays silent rather than guess."""
        Step = Int("Step")
        Err = Int("Err")
        FB = Bool("FB")
        Tmr = Timer.clone("Tmr")

        from pyrung.core import fall

        with Program() as prog:
            with Rung(Step == 1):
                on_delay(Tmr, preset=5000)
            with Rung(Step == 1, Tmr.Acc > 2, fall(FB)):
                calc(Step + 1, Step)
            with Rung(Step == 3, ~FB):
                copy(1, Err)

        assert _find(prog) == []
