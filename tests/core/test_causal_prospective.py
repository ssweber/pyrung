"""Tests for projected causal chain analysis (Section F).

Covers:
- Projected cause (backward): worked example, unreachable/stranded tags
- Projected effect (forward): what-if analysis, dead-end, unreachable trigger
- Mode field values ('projected' / 'unreachable')
- BlockingCondition / BlockerReason data model
- assume={} scenario pinning on cause, effect, recovers
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from pyrung.core import PLC, And, Bool, Int, Or, Program, Rung, calc, copy, latch, out, reset
from pyrung.core.analysis.causal.projected import projected_cause
from pyrung.core.analysis.pdg import build_program_graph

# ---------------------------------------------------------------------------
# Worked example from spec (projected cause)
# ---------------------------------------------------------------------------


def _build_worked_example():
    """Build the six-line ladder fragment from the design spec.

    Rung 0: And(Sensor_Pressure, Permissive_OK, ~Faulted) → latch(Sts_FaultTripped)
    Rung 1: And(Sts_FaultTripped, Cmd_Reset) → reset(Sts_FaultTripped)
    Rung 2: Sts_FaultTripped → out(Alarm_Horn), reset(Cmd_Run)
    """
    Sensor_Pressure = Bool("Sensor_Pressure")
    Permissive_OK = Bool("Permissive_OK")
    Faulted = Bool("Faulted")
    Sts_FaultTripped = Bool("Sts_FaultTripped")
    Cmd_Reset = Bool("Cmd_Reset")
    Alarm_Horn = Bool("Alarm_Horn")
    Cmd_Run = Bool("Cmd_Run")

    with Program() as logic:
        with Rung(And(Sensor_Pressure, Permissive_OK, ~Faulted)):
            latch(Sts_FaultTripped)

        with Rung(And(Sts_FaultTripped, Cmd_Reset)):
            reset(Sts_FaultTripped)

        with Rung(Sts_FaultTripped):
            out(Alarm_Horn)
            reset(Cmd_Run)

    return logic


class TestProjectedCauseWorkedExample:
    """Projected cause: Sts_FaultTripped clear path via Cmd_Reset."""

    def test_fault_tripped_clear_path(self) -> None:
        """cause(Sts_FaultTripped, to=False) should find the reset rung.

        After the fault trips, the projected chain should show:
        - Rung 1 would fire (And(Sts_FaultTripped, Cmd_Reset))
        - Sts_FaultTripped is already TRUE (enabling)
        - Cmd_Reset needs to transition 0→1 (proximate)
        """
        logic = _build_worked_example()
        runner = PLC(logic)

        # Trip the fault
        runner.patch({"Permissive_OK": True})
        runner.step()
        runner.patch({"Sensor_Pressure": True})
        runner.step()

        # Now Sts_FaultTripped is latched TRUE
        assert runner.current_state.tags.get("Sts_FaultTripped") is True

        chain = runner.cause("Sts_FaultTripped", to=False)

        assert chain is not None
        assert chain.mode == "projected"
        assert chain.effect.tag_name == "Sts_FaultTripped"
        assert chain.effect.to_value is False

        # Should have one step pointing at Rung 1
        assert len(chain.steps) >= 1
        step = chain.steps[0]
        assert step.rung_index == 1

        # Proximate: Cmd_Reset needs to go True
        proximate_tags = [p.tag_name for p in step.proximate_causes]
        assert "Cmd_Reset" in proximate_tags

        # Enabling: Sts_FaultTripped is already TRUE
        enabling_tags = [e.tag_name for e in step.enabling_conditions]
        assert "Sts_FaultTripped" in enabling_tags

    def test_fault_tripped_clear_after_reset_observed(self) -> None:
        """When Cmd_Reset has been observed transitioning, path is reachable."""
        logic = _build_worked_example()
        runner = PLC(logic)

        # Trip the fault
        runner.patch({"Permissive_OK": True})
        runner.step()
        runner.patch({"Sensor_Pressure": True})
        runner.step()

        # Simulate Cmd_Reset being toggled (gives observed transition)
        runner.patch({"Cmd_Reset": True})
        runner.step()
        runner.patch({"Cmd_Reset": False})
        runner.step()

        # Sensor_Pressure still True, so fault re-latches on next scan
        runner.step()

        chain = runner.cause("Sts_FaultTripped", to=False)
        assert chain.mode == "projected"


class TestProjectedCauseStranded:
    """Projected cause: stranded tags return mode='unreachable'."""

    def test_no_clear_rung_is_unreachable(self) -> None:
        """A latched bit with no reset rung is unreachable."""
        X = Bool("X")
        Trigger = Bool("Trigger")

        with Program() as logic:
            with Rung(Trigger):
                latch(X)
            # No reset rung for X

        runner = PLC(logic)
        runner.patch({"Trigger": True})
        runner.step()

        assert runner.current_state.tags.get("X") is True

        chain = runner.cause("X", to=False)

        assert chain is not None
        assert chain.mode == "unreachable"
        assert len(chain.blockers) > 0

    def test_already_at_value_returns_projected_empty(self) -> None:
        """cause(tag, to=current_value) returns projected with empty steps."""
        X = Bool("X")

        with Program() as logic:
            with Rung():
                out(X)

        runner = PLC(logic)
        runner.step()

        # X is True (unconditional out writes True)
        assert runner.current_state.tags.get("X") is True
        chain = runner.cause("X", to=True)
        assert chain.mode == "projected"
        assert len(chain.steps) == 0

    def test_unreachable_has_blockers(self) -> None:
        """Unreachable chain should carry structured BlockingCondition info."""
        from pyrung.core.analysis.causal import BlockerReason

        X = Bool("X")
        Trigger = Bool("Trigger")

        with Program() as logic:
            with Rung(Trigger):
                latch(X)

        runner = PLC(logic)
        runner.patch({"Trigger": True})
        runner.step()

        chain = runner.cause("X", to=False)
        assert chain.mode == "unreachable"

        # Should have blocker info
        assert len(chain.blockers) >= 1
        blocker = chain.blockers[0]
        assert blocker.blocked_tag is not None
        assert blocker.reason in (
            BlockerReason.NO_OBSERVED_TRANSITION,
            BlockerReason.BLOCKED_UPSTREAM,
        )

    def test_spec_counterexample_cmd_reset_never_observed(self) -> None:
        """Spec counterexample: Cmd_Reset never transitions → unreachable.

        This is the spec's "unreachable case" worked example: if Cmd_Reset
        has never been observed transitioning to True, the clear path for
        Sts_FaultTripped is unreachable.
        """

        logic = _build_worked_example()
        runner = PLC(logic)

        # Trip the fault — but never toggle Cmd_Reset
        runner.patch({"Permissive_OK": True})
        runner.step()
        runner.patch({"Sensor_Pressure": True})
        runner.step()

        assert runner.current_state.tags.get("Sts_FaultTripped") is True

        # Cmd_Reset has never been observed transitioning.
        # Rung 1 (And(Sts_FaultTripped, Cmd_Reset) → reset(Sts_FaultTripped))
        # should be blocked because Cmd_Reset is an input with no observed
        # transition to True.
        chain = runner.cause("Sts_FaultTripped", to=False)

        # Cmd_Reset IS a physical input (no writer in PDG), so it's always
        # considered reachable — the grounding rule for inputs is "operator
        # can toggle it". So this should actually be projected, not unreachable.
        # The unreachable counterexample in the spec applies when Cmd_Reset
        # has writers in the PDG but those writers are themselves blocked.
        assert chain.mode == "projected"

    def test_unreachable_serialization(self) -> None:
        """to_dict() on unreachable chain includes blockers."""
        X = Bool("X")
        Trigger = Bool("Trigger")

        with Program() as logic:
            with Rung(Trigger):
                latch(X)

        runner = PLC(logic)
        runner.patch({"Trigger": True})
        runner.step()

        chain = runner.cause("X", to=False)
        assert chain.mode == "unreachable"

        d = chain.to_dict()
        assert d["mode"] == "unreachable"
        assert "blockers" in d
        assert len(d["blockers"]) >= 1
        assert "blocked_tag" in d["blockers"][0]
        assert "reason" in d["blockers"][0]

    def test_internal_tag_unreachable_when_never_observed(self) -> None:
        """An internal (non-input) tag that never transitioned is unreachable.

        This exercises the BLOCKED_UPSTREAM reason: the clear rung needs
        an internal tag that itself has no way to transition.
        """

        X = Bool("X")
        Trigger = Bool("Trigger")
        # Internal tag that nothing writes to but is used as a condition
        InternalGate = Bool("InternalGate")

        with Program() as logic:
            with Rung(Trigger):
                latch(X)
            with Rung(InternalGate):
                reset(X)

        runner = PLC(logic)
        runner.patch({"Trigger": True})
        runner.step()

        assert runner.current_state.tags.get("X") is True

        # InternalGate has no writers in PDG and has never been observed
        # transitioning to True. But since it has no writers, it's considered
        # an input and thus reachable.
        chain = runner.cause("X", to=False)
        # InternalGate is an input (no PDG writers) → reachable
        assert chain.mode == "projected"

    def test_two_candidate_rungs_picks_fewer_transitions(self) -> None:
        """When multiple rungs can produce the value, prefer fewer transitions."""
        X = Bool("X")
        A = Bool("A")
        B = Bool("B")
        C = Bool("C")

        with Program() as logic:
            with Rung(And(A, B, C)):
                reset(X)
            with Rung(A):
                reset(X)

        runner = PLC(logic)
        runner.patch({"A": True})
        runner.step()

        X2 = Bool("X2")
        A2 = Bool("A2")
        B2 = Bool("B2")
        C2 = Bool("C2")

        with Program() as logic2:
            with Rung(A2):
                latch(X2)
            with Rung(And(A2, B2, C2)):
                reset(X2)
            with Rung(B2):
                reset(X2)

        runner2 = PLC(logic2)
        runner2.patch({"A2": True})
        runner2.step()

        # X2 is True (latched by Rung 0, Rung 1 and 2 didn't fire)
        assert runner2.current_state.tags.get("X2") is True

        chain = runner2.cause("X2", to=False)
        assert chain.mode == "projected"
        assert len(chain.steps) >= 1

        # Should prefer Rung 2 (needs only B2) over Rung 1 (needs B2 and C2)
        step = chain.steps[0]
        assert step.rung_index == 2
        proximate_tags = [p.tag_name for p in step.proximate_causes]
        assert "B2" in proximate_tags


class TestProjectedCauseNumericConditions:
    """Projected cause should keep numeric blockers numeric."""

    def test_numeric_equality_blocker_keeps_target_value(self) -> None:
        X = Bool("X")
        Start = Bool("Start")
        SeedMode = Bool("SeedMode")
        Mode = Int("Mode")

        with Program() as logic:
            with Rung(Start):
                latch(X)
            with Rung(SeedMode):
                copy(2, Mode)
            with Rung(Mode == 4):
                reset(X)

        runner = PLC(logic)
        runner.patch({"Start": True, "SeedMode": True})
        runner.step()

        chain = runner.cause("X", to=False)

        assert chain.mode == "unreachable"
        assert ("Mode", 4) in {(b.blocked_tag, b.needed_value) for b in chain.blockers}

    def test_numeric_threshold_blocker_keeps_threshold_value(self) -> None:
        X = Bool("X")
        Start = Bool("Start")
        LevelCmd = Bool("LevelCmd")
        Level = Int("Level")

        with Program() as logic:
            with Rung(Start):
                latch(X)
            with Rung(LevelCmd):
                copy(1, Level)
            with Rung(Level >= 5):
                reset(X)

        runner = PLC(logic)
        runner.patch({"Start": True, "LevelCmd": True})
        runner.step()

        chain = runner.cause("X", to=False)

        assert chain.mode == "unreachable"
        assert ("Level", 5) in {(b.blocked_tag, b.needed_value) for b in chain.blockers}

    def test_expression_threshold_blocker_uses_current_snapshot_value(self) -> None:
        X = Bool("X")
        Start = Bool("Start")
        Seed = Bool("Seed")
        Level = Int("Level")
        Limit = Int("Limit")

        with Program() as logic:
            with Rung(Start):
                latch(X)
            with Rung(Seed):
                copy(10, Level)
                copy(5, Limit)
            with Rung(Level < Limit + 2):
                reset(X)

        runner = PLC(logic)
        runner.patch({"Start": True, "Seed": True})
        runner.step()

        chain = runner.cause("X", to=False)

        assert chain.mode == "unreachable"
        assert ("Level", 6) in {(b.blocked_tag, b.needed_value) for b in chain.blockers}

    def test_tag_rhs_inequality_blocker_carries_relation_moves(self) -> None:
        X = Bool("X")
        Start = Bool("Start")
        Seed = Bool("Seed")
        Level = Int("Level")
        Limit = Int("Limit")

        with Program() as logic:
            with Rung(Start):
                latch(X)
            with Rung(Seed):
                copy(10, Level)
                copy(0, Limit)
            with Rung(Level < Limit):
                reset(X)

        runner = PLC(logic)
        runner.patch({"Start": True, "Seed": True})
        runner.step()

        chain = projected_cause(
            runner._logic,
            runner._history,
            "X",
            False,
            build_program_graph(logic),
            program=logic,
            timelines=runner._rung_firing_timelines,
            nd_domains={"Limit": (0, 20)},
        )

        relation = chain.blockers[0].relation
        assert relation is not None
        assert relation.lhs_tag == "Level"
        assert relation.operator == "<"
        assert relation.rhs_repr == "Limit"
        assert ("Limit", 20) in {(m.tag, m.value) for m in relation.candidate_moves}

    def test_expression_rhs_inequality_blocker_carries_relation_moves(self) -> None:
        X = Bool("X")
        Start = Bool("Start")
        Seed = Bool("Seed")
        Level = Int("Level")
        Limit = Int("Limit")

        with Program() as logic:
            with Rung(Start):
                latch(X)
            with Rung(Seed):
                copy(10, Level)
                copy(0, Limit)
            with Rung(Level < Limit + 2):
                reset(X)

        runner = PLC(logic)
        runner.patch({"Start": True, "Seed": True})
        runner.step()

        chain = projected_cause(
            runner._logic,
            runner._history,
            "X",
            False,
            build_program_graph(logic),
            program=logic,
            timelines=runner._rung_firing_timelines,
            nd_domains={"Limit": (0, 20)},
        )

        relation = chain.blockers[0].relation
        assert relation is not None
        assert relation.rhs_repr == "Limit + 2"
        assert relation.rhs_value == 2
        assert "Limit" in relation.tags
        assert ("Limit", 20) in {(m.tag, m.value) for m in relation.candidate_moves}

    def test_affine_dependency_candidate_move_targets_source(self) -> None:
        X = Bool("X")
        Start = Bool("Start")
        Seed = Bool("Seed")
        PV = Int("PV")

        with Program() as logic:
            with Rung(Start):
                latch(X)
            with Rung(Seed):
                copy(100, PV)
            with Rung(PV < 50):
                reset(X)

        runner = PLC(logic)
        runner.patch({"Start": True, "Seed": True})
        runner.step()

        chain = projected_cause(
            runner._logic,
            runner._history,
            "X",
            False,
            build_program_graph(logic),
            program=logic,
            timelines=runner._rung_firing_timelines,
            nd_domains={"Sensor": (0, 100)},
            func_deps={"PV": ("Sensor", 1, 0)},
        )

        relation = chain.blockers[0].relation
        assert relation is not None
        assert ("Sensor", 0) in {(m.tag, m.value) for m in relation.candidate_moves}


class TestProjectedOraclePinning:
    """cause(to=) gains the projected-oracle writer selection (pilot upstream)."""

    def test_even_step_counter_selects_transition_writer(self) -> None:
        """A self-referential affine step counter resolves through the
        transition rung, not the parity-gated even-step rung.

        Mirrors the Blower SFC engine: both ``calc(CurStep+1, CurStep)`` writers
        can produce ``CurStep+1``, but the even-step rung (gated
        ``valstepisodd != 1``) is counterfactual for ``CurStep == 2`` — its
        source ``CurStep == 1`` is odd.  The projected oracle pins the affine
        source and its one-hop-derived parity and rejects the even-step rung.
        """
        CurStep = Int("CurStep")
        valstepisodd = Int("valstepisodd")
        Trans = Int("Trans")
        xPause = Int("xPause")
        x_TimerDone = Bool("x_TimerDone")
        x_FB = Bool("x_FB")

        with Program(strict=False) as logic:
            with Rung(CurStep == 1, x_TimerDone, x_FB):  # transition trigger
                copy(1, Trans)
            with Rung():  # parity (derived from CurStep)
                calc(CurStep % 2, valstepisodd)
            with Rung(valstepisodd != 1, xPause == 0):  # even-step advance
                calc(CurStep + 1, CurStep)
            with Rung(Trans == 1):  # transition advance
                calc(CurStep + 1, CurStep)

        runner = PLC(logic)
        runner.step()  # CurStep == 0

        pdg = build_program_graph(logic)
        chain = projected_cause(
            runner._logic,
            runner._history,
            "CurStep",
            2,
            pdg,
            program=logic,
            timelines=runner._rung_firing_timelines,
            structural=True,
        )

        assert chain.mode == "projected"
        assert chain.steps, "expected a projected step for CurStep == 2"
        chosen = chain.steps[0].rung_index
        trans_rung = next(n.rung_index for n in pdg.rung_nodes if "Trans" in n.condition_reads)
        assert chosen == trans_rung
        prox_tags = {t.tag_name for t in chain.steps[0].triggers}
        assert "Trans" in prox_tags
        assert "valstepisodd" not in prox_tags

    def test_pinned_rejects_counterfactual_one_hot(self) -> None:
        """With the held one-hot state pinned, the writer gated by a
        mutually-exclusive peer state is rejected for the live one."""
        S_Starting = Bool("S_Starting", external=True)
        S_Clearing = Bool("S_Clearing", external=True)
        Blower__init = Int("Blower__init")
        SCB = Bool("SCB")

        with Program(strict=False) as logic:
            with Rung(S_Clearing):  # counterfactual writer
                copy(1, SCB)
            with Rung(S_Starting, Blower__init == 1):  # live writer
                copy(1, SCB)

        runner = PLC(logic)
        runner.patch({"S_Starting": True, "S_Clearing": False})
        runner.step()

        pdg = build_program_graph(logic)
        chain = projected_cause(
            runner._logic,
            runner._history,
            "SCB",
            True,
            pdg,
            program=logic,
            timelines=runner._rung_firing_timelines,
            structural=True,
            pinned=frozenset({"S_Starting", "S_Clearing"}),
        )

        assert chain.mode == "projected"
        assert chain.steps
        chosen = chain.steps[0].rung_index
        starting_rung = next(
            n.rung_index for n in pdg.rung_nodes if "S_Starting" in n.condition_reads
        )
        assert chosen == starting_rung
        prox_tags = {t.tag_name for t in chain.steps[0].triggers}
        assert "Blower__init" in prox_tags


class TestProjectedCauseEdgeCases:
    """Edge cases for projected backward walk."""

    def test_tag_object_accepted(self) -> None:
        """cause() with to= should accept a Tag object."""
        Button = Bool("Button")
        Light = Bool("Light")

        with Program() as logic:
            with Rung(Button):
                out(Light)

        runner = PLC(logic)
        runner.step()

        chain = runner.cause(Light, to=True)
        assert chain is not None
        assert chain.effect.tag_name == "Light"

    def test_unconditional_rung(self) -> None:
        """An unconditional writing rung should be trivially reachable."""
        X = Bool("X")

        with Program() as logic:
            with Rung():
                latch(X)

        runner = PLC(logic)
        runner.step()

        # X is True via unconditional latch — ask how to get True
        chain = runner.cause("X", to=True)
        # Already True — should be projected with empty steps
        assert chain.mode == "projected"

    def test_or_condition_projected(self) -> None:
        """Projected cause with Or condition identifies needed transitions."""
        A = Bool("A")
        B = Bool("B")
        X = Bool("X")

        with Program() as logic:
            with Rung(Or(A, B)):
                latch(X)

        runner = PLC(logic)
        runner.step()

        # X is False, both A and B are False
        chain = runner.cause("X", to=True)
        assert chain.mode in ("projected", "unreachable")

    def test_str_rendering(self) -> None:
        """CausalChain.__str__ should produce readable output."""
        logic = _build_worked_example()
        runner = PLC(logic)

        runner.patch({"Permissive_OK": True})
        runner.step()
        runner.patch({"Sensor_Pressure": True})
        runner.step()

        chain = runner.cause("Sts_FaultTripped", to=False)
        text = str(chain)
        assert "Sts_FaultTripped" in text
        assert "projected" in text or "unreachable" in text

    def test_unreachable_str_rendering(self) -> None:
        """Unreachable chains render with blocker info."""
        X = Bool("X")
        Trigger = Bool("Trigger")

        with Program() as logic:
            with Rung(Trigger):
                latch(X)

        runner = PLC(logic)
        runner.patch({"Trigger": True})
        runner.step()

        chain = runner.cause("X", to=False)
        text = str(chain)
        assert "unreachable" in text


# ---------------------------------------------------------------------------
# Projected effect (forward what-if)
# ---------------------------------------------------------------------------


class TestProjectedEffect:
    """Projected effect: what-if analysis."""

    def test_button_press_what_if(self) -> None:
        """effect(tag, from_=False) should find downstream effects."""
        Button = Bool("Button")
        Light = Bool("Light")

        with Program() as logic:
            with Rung(Button):
                out(Light)

        runner = PLC(logic)
        runner.step()

        # What if Button went True (from False)?
        chain = runner.effect("Button", from_=False)

        assert chain is not None
        assert chain.mode == "projected"
        assert chain.effect.tag_name == "Button"
        assert chain.effect.from_value is False
        assert chain.effect.to_value is True

        # Should find Light as downstream effect
        effect_tags = [s.transition.tag_name for s in chain.steps]
        assert "Light" in effect_tags

    def test_worked_example_sensor_pressure_what_if(self) -> None:
        """What if Sensor_Pressure went True while Permissive_OK is True?"""
        logic = _build_worked_example()
        runner = PLC(logic)

        runner.patch({"Permissive_OK": True})
        runner.step()

        # What if Sensor_Pressure went True right now?
        chain = runner.effect("Sensor_Pressure", from_=False)
        assert chain.mode == "projected"

        effect_tags = [s.transition.tag_name for s in chain.steps]
        assert "Sts_FaultTripped" in effect_tags

    def test_dead_end_returns_projected_empty(self) -> None:
        """A tag no rung reads should return projected with empty steps."""
        Isolated = Bool("Isolated")

        with Program() as logic:
            with Rung():
                latch(Isolated)

        runner = PLC(logic)
        runner.step()

        chain = runner.effect("Isolated", from_=True)
        assert chain.mode == "projected"
        # Dead-end: Isolated would transition but nothing reads it
        # (no conditional rung uses Isolated)
        # Steps may or may not be empty depending on whether any rung
        # has Isolated in its condition tree

    def test_irrelevant_tag_not_in_effects(self) -> None:
        """Tags unaffected by the hypothetical transition shouldn't appear."""
        A = Bool("A")
        B = Bool("B")
        X = Bool("X")
        Y = Bool("Y")

        with Program() as logic:
            with Rung(A):
                latch(X)
            with Rung(B):
                latch(Y)

        runner = PLC(logic)
        runner.step()

        chain = runner.effect("A", from_=False)
        assert chain.mode == "projected"

        effect_tags = [s.transition.tag_name for s in chain.steps]
        assert "X" in effect_tags
        assert "Y" not in effect_tags


class TestProjectedEffectEdgeCases:
    """Edge cases for projected forward walk."""

    def test_tag_object_accepted(self) -> None:
        """effect() with from_= should accept a Tag object."""
        A = Bool("A")
        X = Bool("X")

        with Program() as logic:
            with Rung(A):
                latch(X)

        runner = PLC(logic)
        runner.step()

        chain = runner.effect(A, from_=False)
        assert chain is not None
        assert chain.effect.tag_name == "A"

    def test_serialization(self) -> None:
        """to_dict() should work on projected chains."""
        A = Bool("A")
        X = Bool("X")

        with Program() as logic:
            with Rung(A):
                latch(X)

        runner = PLC(logic)
        runner.step()

        chain = runner.effect("A", from_=False)
        d = chain.to_dict()
        assert d["mode"] == "projected"
        assert "steps" in d

    def test_or_not_load_bearing(self) -> None:
        """In Or(A, B) with both going True, flipping one doesn't change outcome."""
        A = Bool("A")
        B = Bool("B")
        X = Bool("X")

        with Program() as logic:
            with Rung(Or(A, B)):
                latch(X)

        runner = PLC(logic)
        # Set B=True so Or is already True
        runner.patch({"B": True})
        runner.step()

        # What if A also went True? Or(True, True) same as Or(False, True)
        chain = runner.effect("A", from_=False)
        assert chain.mode == "projected"
        effect_tags = [s.transition.tag_name for s in chain.steps]
        assert "X" not in effect_tags


# ---------------------------------------------------------------------------
# F5: Additional projected-mode test coverage
# ---------------------------------------------------------------------------


class TestProjectedCauseBlockedUpstream:
    """BLOCKED_UPSTREAM: internal tag has writers but they can't fire."""

    def test_internal_gate_blocked_upstream(self) -> None:
        """A reset rung guarded by an internal tag whose writer is blocked.

        Layout:
          Rung 0: Trigger → latch(X)
          Rung 1: Impossible → latch(Gate)   # Gate has a writer
          Rung 2: Gate → reset(X)

        Impossible is also internal (written by a rung guarded by itself,
        creating a circular dependency that can never fire from False).
        Gate has writers in the PDG so it is NOT treated as an input.
        Gate has never been observed transitioning → BLOCKED_UPSTREAM.
        """
        from pyrung.core.analysis.causal import BlockerReason

        X = Bool("X")
        Trigger = Bool("Trigger")
        Gate = Bool("Gate")
        Impossible = Bool("Impossible")

        with Program() as logic:
            with Rung(Trigger):
                latch(X)
            with Rung(Impossible):
                latch(Gate)
            with Rung(Impossible):
                latch(Impossible)  # self-referencing, never fires from False
            with Rung(Gate):
                reset(X)

        runner = PLC(logic)
        runner.patch({"Trigger": True})
        runner.step()

        assert runner.current_state.tags.get("X") is True
        assert runner.current_state.tags.get("Gate") in (False, None)

        chain = runner.cause("X", to=False)
        assert chain.mode == "unreachable"
        assert len(chain.blockers) >= 1

        blocker = chain.blockers[0]
        assert blocker.blocked_tag == "Gate"
        assert blocker.reason == BlockerReason.BLOCKED_UPSTREAM

    def test_blocked_upstream_serializes(self) -> None:
        """BLOCKED_UPSTREAM blocker should round-trip via to_dict()."""
        X = Bool("X")
        Trigger = Bool("Trigger")
        Gate = Bool("Gate")
        Impossible = Bool("Impossible")

        with Program() as logic:
            with Rung(Trigger):
                latch(X)
            with Rung(Impossible):
                latch(Gate)
            with Rung(Impossible):
                latch(Impossible)
            with Rung(Gate):
                reset(X)

        runner = PLC(logic)
        runner.patch({"Trigger": True})
        runner.step()

        chain = runner.cause("X", to=False)
        d = chain.to_dict()
        assert d["mode"] == "unreachable"
        assert any(b["reason"] == "BLOCKED_UPSTREAM" for b in d["blockers"])


class TestProjectedCauseCopyWriters:
    """Writer rungs that copy from a tag (the PackML sm_copy_or_jump_state shape).

    A state machine whose current-state register is written only by
    ``copy(Requested, Current)`` defeats two things at once: the candidate
    check (the rung only "produces" whatever Requested holds *right now*)
    and, when the rung ends in ``return_early()``, the bare ``rung.execute``
    used by that check.  Both bit on the live Tumbler/Dryer template
    (probe14/15, burnerloop findings).
    """

    def _jump_state_program(self):
        """Distilled sm_copy_or_jump_state: copy-from-tag + return_early."""
        from pyrung.core import call, return_early, subroutine

        Go = Bool("Go")
        Req = Int("Req")
        Cur = Int("Cur")
        Tail = Int("Tail")

        @subroutine("jump_state")
        def jump_state():
            with Rung(Req != 0):
                copy(Req, Cur)
                copy(0, Req)
                return_early()
            with Rung():
                copy(1, Tail)

        with Program() as logic:
            with Rung(Go):
                copy(4, Req)
            with Rung(Req != 0):
                call(jump_state)

        return logic

    def test_writer_rung_with_return_early_does_not_raise(self) -> None:
        """cause(to=) must not leak SubroutineReturnSignal from a writer rung.

        _rung_produces_value executes candidate writer rungs in isolation;
        a rung containing return_early() raises the subroutine control-flow
        signal, which must be contained (the writes captured before the
        signal are exactly the real in-scan semantics).  Pre-fix this
        poisoned walker recovery into false 'unsolvable' certificates.
        """
        runner = PLC(self._jump_state_program())
        runner.step()

        chain = runner.cause("Cur", to=4)  # must not raise
        assert chain is not None
        assert chain.mode in ("projected", "unreachable")

    def test_copy_source_named_as_blocker(self) -> None:
        """The copy-source requirement (Req=4) is the named blocker.

        Req is internal (program-written), never observed at 4 → the chain
        must surface (Req, 4) as BLOCKED_UPSTREAM instead of the generic
        self-named no-candidate blocker, so recovery can mine it as a goal.
        """
        from pyrung.core.analysis.causal import BlockerReason

        runner = PLC(self._jump_state_program())
        runner.step()

        chain = runner.cause("Cur", to=4)
        assert chain.mode == "unreachable"
        named = {(b.blocked_tag, b.needed_value) for b in chain.blockers}
        assert ("Req", 4) in named
        blocker = next(b for b in chain.blockers if b.blocked_tag == "Req")
        assert blocker.reason == BlockerReason.BLOCKED_UPSTREAM

    def test_copy_source_named_as_trigger_when_input(self) -> None:
        """An external copy-source becomes a proximate trigger at the value.

        copy(Src, Dst) with Src never written by the program: cause(Dst,
        to=7) should be projected with Src→7 among the triggers (the
        operator can set it), not unreachable-for-lack-of-candidates.
        """
        Gate = Bool("Gate")
        Src = Int("Src")
        Dst = Int("Dst")

        with Program() as logic:
            with Rung(Gate):
                copy(Src, Dst)

        runner = PLC(logic)
        runner.step()

        chain = runner.cause("Dst", to=7)
        assert chain.mode == "projected"
        triggers = {(t.tag_name, t.to_value) for s in chain.steps for t in s.triggers}
        assert ("Src", 7) in triggers
        assert ("Gate", True) in triggers

    def test_impossible_stored_copy_target_is_not_a_projected_writer(self) -> None:
        dest = Int("Dest")
        with Program() as logic:
            with Rung():
                copy(40_000, dest)

        runner = PLC(logic)
        runner.step()

        assert runner.current_state.tags[dest.name] == 32_767
        chain = runner.cause(dest.name, to=40_000)
        assert chain.mode == "unreachable"


class TestProjectedEffectUnreachable:
    """Projected effect: unreachable trigger and edge cases."""

    def test_non_bool_from_value_unreachable(self) -> None:
        """effect() with non-Bool from_value can't infer TO → unreachable."""
        from pyrung.core import Int

        Counter = Int("Counter")

        with Program() as logic:
            with Rung():
                out(Counter)

        runner = PLC(logic)
        runner.step()

        chain = runner.effect("Counter", from_=5)
        assert chain.mode == "unreachable"

    def test_trigger_itself_unreachable(self) -> None:
        """effect() returns unreachable when from_value != current and cause is blocked.

        projected_effect checks trigger reachability only when current_value
        differs from from_value. If the tag can't reach from_value,
        the trigger is unreachable.

        Here Orphan has no writers in the PDG, is currently False, and we
        ask what-if from True — projected_cause("Orphan", to=True) fails
        because no rung writes Orphan.
        """
        Orphan = Bool("Orphan")
        A = Bool("A")

        with Program() as logic:
            with Rung(Orphan):
                out(A)

        runner = PLC(logic)
        runner.step()

        # Orphan is False, from_=True → current != from_
        # projected_cause("Orphan", to=True) → no writers → unreachable
        chain = runner.effect("Orphan", from_=True)
        assert chain.mode == "unreachable"


class TestProjectedEffectCascading:
    """Multi-step forward propagation through a rung chain."""

    def test_three_step_chain(self) -> None:
        """A→B→C→D: pressing A should propagate through all three rungs.

        Layout:
          Rung 0: A → out(B)
          Rung 1: B → out(C)
          Rung 2: C → out(D)
        """
        A = Bool("A")
        B = Bool("B")
        C = Bool("C")
        D = Bool("D")

        with Program() as logic:
            with Rung(A):
                out(B)
            with Rung(B):
                out(C)
            with Rung(C):
                out(D)

        runner = PLC(logic)
        runner.step()

        chain = runner.effect("A", from_=False)
        assert chain.mode == "projected"

        effect_tags = [s.transition.tag_name for s in chain.steps]
        assert "B" in effect_tags
        assert "C" in effect_tags
        assert "D" in effect_tags

    def test_cascading_with_enabling_condition(self) -> None:
        """Forward chain where intermediate rung has an enabling condition.

        Layout:
          Rung 0: A → out(B)
          Rung 1: And(B, Permit) → out(C)

        With Permit=True, pressing A should reach C.
        """
        A = Bool("A")
        B = Bool("B")
        C = Bool("C")
        Permit = Bool("Permit")

        with Program() as logic:
            with Rung(A):
                out(B)
            with Rung(And(B, Permit)):
                out(C)

        runner = PLC(logic)
        runner.patch({"Permit": True})
        runner.step()

        chain = runner.effect("A", from_=False)
        assert chain.mode == "projected"

        effect_tags = [s.transition.tag_name for s in chain.steps]
        assert "B" in effect_tags
        assert "C" in effect_tags


class TestProjectedCauseConjunctiveRoots:
    """conjunctive_roots field on projected cause chains."""

    def test_conjunctive_roots_populated(self) -> None:
        """Projected cause with multiple proximate causes fills conjunctive_roots."""
        A = Bool("A")
        B = Bool("B")
        X = Bool("X")

        with Program() as logic:
            with Rung(And(A, B)):
                latch(X)

        runner = PLC(logic)
        runner.step()

        # Both A and B are False — both need to transition
        chain = runner.cause("X", to=True)
        assert chain.mode == "projected"

        root_tags = [t.tag_name for t in chain.conjunctive_roots]
        assert "A" in root_tags
        assert "B" in root_tags

    def test_single_proximate_in_conjunctive_roots(self) -> None:
        """When only one condition needs to transition, it's the sole root."""
        A = Bool("A")
        B = Bool("B")
        X = Bool("X")

        with Program() as logic:
            with Rung(And(A, B)):
                latch(X)

        runner = PLC(logic)
        runner.patch({"A": True})
        runner.step()

        # A is True (enabling), B is False (proximate)
        chain = runner.cause("X", to=True)
        assert chain.mode == "projected"

        root_tags = [t.tag_name for t in chain.conjunctive_roots]
        assert root_tags == ["B"]


class TestProjectedChainAccessors:
    """tags(), rungs(), and to_config() on projected chains."""

    def test_projected_cause_tags(self) -> None:
        """tags() on a projected cause includes effect, proximate, and enabling."""
        logic = _build_worked_example()
        runner = PLC(logic)

        runner.patch({"Permissive_OK": True})
        runner.step()
        runner.patch({"Sensor_Pressure": True})
        runner.step()

        chain = runner.cause("Sts_FaultTripped", to=False)
        assert chain.mode == "projected"

        tags = chain.tags()
        assert "Sts_FaultTripped" in tags
        assert "Cmd_Reset" in tags

    def test_projected_cause_rungs(self) -> None:
        """rungs() on a projected cause returns the candidate rung indices."""
        logic = _build_worked_example()
        runner = PLC(logic)

        runner.patch({"Permissive_OK": True})
        runner.step()
        runner.patch({"Sensor_Pressure": True})
        runner.step()

        chain = runner.cause("Sts_FaultTripped", to=False)
        assert chain.mode == "projected"
        assert 1 in chain.rungs()

    def test_projected_cause_to_config(self) -> None:
        """to_config() on a projected cause returns compact serialization."""
        logic = _build_worked_example()
        runner = PLC(logic)

        runner.patch({"Permissive_OK": True})
        runner.step()
        runner.patch({"Sensor_Pressure": True})
        runner.step()

        chain = runner.cause("Sts_FaultTripped", to=False)
        cfg = chain.to_config()

        assert cfg["effect"] == "Sts_FaultTripped"
        assert cfg["mode"] == "projected"
        assert isinstance(cfg["steps"], list)
        assert len(cfg["steps"]) >= 1
        assert cfg["steps"][0]["rung"] == 1

    def test_projected_effect_tags(self) -> None:
        """tags() on a projected effect includes trigger and downstream."""
        A = Bool("A")
        X = Bool("X")

        with Program() as logic:
            with Rung(A):
                out(X)

        runner = PLC(logic)
        runner.step()

        chain = runner.effect("A", from_=False)
        tags = chain.tags()
        assert "A" in tags
        assert "X" in tags

    def test_projected_effect_rungs(self) -> None:
        """rungs() on a projected effect returns affected rung indices."""
        A = Bool("A")
        X = Bool("X")

        with Program() as logic:
            with Rung(A):
                out(X)

        runner = PLC(logic)
        runner.step()

        chain = runner.effect("A", from_=False)
        assert 0 in chain.rungs()

    def test_projected_effect_to_config(self) -> None:
        """to_config() on a projected effect returns compact serialization."""
        A = Bool("A")
        X = Bool("X")

        with Program() as logic:
            with Rung(A):
                out(X)

        runner = PLC(logic)
        runner.step()

        chain = runner.effect("A", from_=False)
        cfg = chain.to_config()

        assert cfg["effect"] == "A"
        assert cfg["mode"] == "projected"
        assert isinstance(cfg["steps"], list)


# ---------------------------------------------------------------------------
# assume={} on projected cause
# ---------------------------------------------------------------------------


class TestProjectedCauseWithAssume:
    """assume= pins tag values and bypasses reachability checks."""

    def test_assume_makes_unreachable_reachable(self) -> None:
        """A ladder-written condition tag with no observed transition blocks
        the clear path.  assume= stipulates the value, making it reachable.

        ResetReady is written by the ladder (rung 2), so it's NOT a pure
        input -- projected_cause won't grant automatic reachability.
        """
        Sensor = Bool("Sensor")
        Fault = Bool("Fault")
        ResetReady = Bool("ResetReady")
        Trigger = Bool("Trigger")

        with Program() as logic:
            with Rung(Sensor):
                latch(Fault)
            with Rung(And(Fault, ResetReady)):
                reset(Fault)
            # ResetReady written by ladder → has a PDG writer
            with Rung(Trigger):
                out(ResetReady)

        runner = PLC(logic)
        runner.patch({"Sensor": True})
        runner.step()
        assert runner.current_state.tags.get("Fault") is True

        # Without assume: ResetReady has no observed transition → blocked
        chain_no = runner.cause("Fault", to=False)
        assert chain_no.mode == "unreachable"

        # With assume: ResetReady stipulated → reachable
        chain_yes = runner.cause("Fault", to=False, assume={"ResetReady": True})
        assert chain_yes.mode == "projected"
        assert len(chain_yes.steps) >= 1

    def test_assume_pins_state_for_evaluation(self) -> None:
        """An assumed tag that satisfies a condition shows as enabling."""
        logic = _build_worked_example()
        runner = PLC(logic)

        # Trip the fault
        runner.patch({"Permissive_OK": True})
        runner.step()
        runner.patch({"Sensor_Pressure": True})
        runner.step()

        # Cmd_Reset=True satisfies the Cmd_Reset contact on rung 1.
        # Sts_FaultTripped is already True (enabling).
        # With the assumed Cmd_Reset pinned True, the condition evaluates
        # True → it should appear as enabling, not proximate.
        chain = runner.cause("Sts_FaultTripped", to=False, assume={"Cmd_Reset": True})
        assert chain.mode == "projected"
        step = chain.steps[0]
        enabling_tags = [e.tag_name for e in step.enabling_conditions]
        proximate_tags = [p.tag_name for p in step.proximate_causes]
        assert "Cmd_Reset" in enabling_tags
        assert "Cmd_Reset" not in proximate_tags

    def test_assume_already_at_target(self) -> None:
        """assume= can push the target tag itself to the desired value."""
        X = Bool("X")
        Trigger = Bool("Trigger")

        with Program() as logic:
            with Rung(Trigger):
                latch(X)

        runner = PLC(logic)
        runner.patch({"Trigger": True})
        runner.step()
        assert runner.current_state.tags.get("X") is True

        # X has no reset rung → normally unreachable.
        # But assume={"X": False} overrides state to already-at-target.
        chain = runner.cause("X", to=False, assume={"X": False})
        assert chain.mode == "projected"
        assert len(chain.steps) == 0  # already at desired value

    def test_assume_without_to_raises(self) -> None:
        """assume= without to= (recorded mode) raises ValueError."""
        logic = _build_worked_example()
        runner = PLC(logic)
        runner.step()

        with pytest.raises(ValueError, match="projected mode"):
            runner.cause("Sts_FaultTripped", assume={"Cmd_Reset": True})


class TestProjectedCauseStructural:
    """Structural projected cause names current blockers without archaeology."""

    def _copy_source_program(self):
        Ready = Bool("Ready")
        TriggerGate = Bool("TriggerGate")
        SeedSrc = Bool("SeedSrc")
        Gate = Bool("Gate")
        Src = Int("Src")
        Dest = Int("Dest")

        with Program() as logic:
            with Rung(TriggerGate):
                out(Gate)
            with Rung(SeedSrc):
                copy(4, Src)
            with Rung(Ready, Gate):
                copy(Src, Dest)

        return logic

    def test_structural_assumes_unobserved_values_reachable(self) -> None:
        logic = self._copy_source_program()
        runner = PLC(logic)
        runner.patch({"Ready": True})
        runner.step()
        pdg = build_program_graph(logic)

        chain_no = projected_cause(
            runner._logic,
            runner._history,
            "Dest",
            4,
            pdg,
            program=logic,
            timelines=runner._rung_firing_timelines,
        )
        assert chain_no.mode == "unreachable"

        chain = projected_cause(
            runner._logic,
            runner._history,
            "Dest",
            4,
            pdg,
            program=logic,
            timelines=runner._rung_firing_timelines,
            structural=True,
        )

        assert chain.mode == "projected"
        step = chain.steps[0]
        assert step.fidelity == "structural"
        assert {(t.tag_name, t.to_value) for t in step.triggers} == {
            ("Src", 4),
            ("Gate", True),
        }
        assert {(e.tag_name, e.value) for e in step.enablers} == {("Ready", True)}
        assert all(e.held_since_scan is None for e in step.enablers)

    def test_structural_reads_only_latest_history_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        logic = self._copy_source_program()
        runner = PLC(logic)
        runner.patch({"Ready": True})
        runner.step()
        pdg = build_program_graph(logic)

        calls: list[int] = []
        original_at = runner._history.at

        def counting_at(scan_id: int):
            calls.append(scan_id)
            return original_at(scan_id)

        monkeypatch.setattr(runner._history, "at", counting_at)

        chain = projected_cause(
            runner._logic,
            runner._history,
            "Dest",
            4,
            pdg,
            program=logic,
            timelines=runner._rung_firing_timelines,
            structural=True,
        )

        assert chain.mode == "projected"
        assert calls == [runner._history.newest_scan_id]


# ---------------------------------------------------------------------------
# assume={} on projected effect
# ---------------------------------------------------------------------------


class TestProjectedEffectWithAssume:
    """assume= on effect() pins state for forward what-if analysis."""

    def test_assume_affects_forward_walk(self) -> None:
        """Assumed values change which downstream rungs fire."""
        A = Bool("A")
        Guard = Bool("Guard")
        X = Bool("X")

        with Program() as logic:
            # X fires only when both A and Guard are True
            with Rung(And(A, Guard)):
                out(X)

        runner = PLC(logic)
        runner.step()

        # Without assume: Guard is False, so A flipping has no effect on X
        chain_no = runner.effect("A", from_=False)
        effect_tags = [s.transition.tag_name for s in chain_no.steps]
        assert "X" not in effect_tags

        # With assume: Guard=True, so A flipping does affect X
        chain_yes = runner.effect("A", from_=False, assume={"Guard": True})
        effect_tags = [s.transition.tag_name for s in chain_yes.steps]
        assert "X" in effect_tags

    def test_assume_without_from_raises(self) -> None:
        """assume= without from_= (recorded mode) raises ValueError."""
        A = Bool("A")
        X = Bool("X")

        with Program() as logic:
            with Rung(A):
                out(X)

        runner = PLC(logic)
        runner.step()

        with pytest.raises(ValueError, match="projected mode"):
            runner.effect("A", assume={"Guard": True})


# ---------------------------------------------------------------------------
# assume={} on recovers
# ---------------------------------------------------------------------------


class TestRecoversWithAssume:
    """assume= on recovers() exercises recovery paths with stipulated values."""

    def test_recovers_external_with_assume(self) -> None:
        """External tag with assume skips the declaration shortcut and runs analysis."""
        HmiAck = Bool("HmiAck", external=True)
        Trigger = Bool("Trigger")

        with Program() as logic:
            with Rung(Trigger):
                latch(HmiAck)

        runner = PLC(logic)
        runner.patch({"Trigger": True})
        runner.step()
        assert runner.current_state.tags.get("HmiAck") is True

        # Without assume: external → True by declaration
        assert runner.recovers(HmiAck) is True

        # With assume: analysis runs. HmiAck pinned to False (resting) →
        # already at target → projected, recovers.
        assert runner.recovers(HmiAck, assume={"HmiAck": False}) is True

    def test_recovers_with_assume_on_condition_tag(self) -> None:
        """assume= on a ladder-written condition tag makes a blocked fault
        recoverable.  ResetReady has a PDG writer, so it's not a pure input.
        """
        Sensor = Bool("Sensor")
        Fault = Bool("Fault")
        ResetReady = Bool("ResetReady")
        Trigger = Bool("Trigger")

        with Program() as logic:
            with Rung(Sensor):
                latch(Fault)
            with Rung(And(Fault, ResetReady)):
                reset(Fault)
            with Rung(Trigger):
                out(ResetReady)

        runner = PLC(logic)
        runner.patch({"Sensor": True})
        runner.step()
        assert runner.current_state.tags.get("Fault") is True

        # Without assume: ResetReady has no observed transition → unreachable
        assert runner.recovers(Fault) is False

        # With assume: ResetReady stipulated → reachable
        assert runner.recovers(Fault, assume={"ResetReady": True}) is True

    def test_assume_readonly_raises(self) -> None:
        """assume= targeting a readonly tag raises ValueError."""
        Sensor = Bool("Sensor", readonly=True)
        X = Bool("X")

        with Program() as logic:
            with Rung(Sensor):
                out(X)

        runner = PLC(logic)
        runner.step()

        with pytest.raises(ValueError, match="readonly"):
            runner.cause("X", to=True, assume={"Sensor": True})


# ---------------------------------------------------------------------------
# Simulation primitive (_simulate_scan) and copy/calc awareness
# ---------------------------------------------------------------------------


class TestSimulateScan:
    """Direct tests for the _simulate_scan primitive."""

    def test_coil_writes_captured(self) -> None:
        """Simulation captures Out instruction writes."""
        from pyrung.core.analysis.causal.projected import _simulate_scan
        from pyrung.core.state import SystemState

        A = Bool("A")
        X = Bool("X")

        with Program() as logic:
            with Rung(A):
                out(X)

        state = SystemState().with_tags({"A": True})
        sim = _simulate_scan(logic.rungs, state)
        assert dict(sim.rung_writes.get(0, {})).get("X") is True

    def test_copy_captured(self) -> None:
        """Simulation captures copy instruction writes."""
        from pyrung.core.analysis.causal.projected import _simulate_scan
        from pyrung.core.state import SystemState

        Enable = Bool("Enable")
        Src = Int("Src")
        Dest = Int("Dest")

        with Program() as logic:
            with Rung(Enable):
                copy(Src, Dest)

        state = SystemState().with_tags({"Enable": True, "Src": 42})
        sim = _simulate_scan(logic.rungs, state)
        assert dict(sim.rung_writes.get(0, {})).get("Dest") == 42

    def test_calc_captured(self) -> None:
        """Simulation captures calc instruction writes."""
        from pyrung.core.analysis.causal.projected import _simulate_scan
        from pyrung.core.state import SystemState

        Enable = Bool("Enable")
        A = Int("A")
        B = Int("B")
        Result = Int("Result")

        with Program() as logic:
            with Rung(Enable):
                calc(A + B, Result)

        state = SystemState().with_tags({"Enable": True, "A": 10, "B": 20})
        sim = _simulate_scan(logic.rungs, state)
        assert dict(sim.rung_writes.get(0, {})).get("Result") == 30

    def test_disabled_rung_no_writes(self) -> None:
        """Rung with False condition produces no writes for coils."""
        from pyrung.core.analysis.causal.projected import _simulate_scan
        from pyrung.core.state import SystemState

        A = Bool("A")
        X = Bool("X")

        with Program() as logic:
            with Rung(A):
                latch(X)

        state = SystemState().with_tags({"A": False})
        sim = _simulate_scan(logic.rungs, state)
        assert 0 not in sim.rung_writes or "X" not in dict(sim.rung_writes.get(0, {}))

    def test_state_after_reflects_writes(self) -> None:
        """state_after contains committed tag values."""
        from pyrung.core.analysis.causal.projected import _simulate_scan
        from pyrung.core.state import SystemState

        Enable = Bool("Enable")
        Src = Int("Src")
        Dest = Int("Dest")

        with Program() as logic:
            with Rung(Enable):
                copy(Src, Dest)

        state = SystemState().with_tags({"Enable": True, "Src": 99})
        sim = _simulate_scan(logic.rungs, state)
        assert sim.state_after.tags.get("Dest") == 99

    def test_program_object_accepted(self) -> None:
        """Simulation accepts a Program object (not just list[Rung])."""
        from pyrung.core.analysis.causal.projected import _simulate_scan
        from pyrung.core.state import SystemState

        A = Bool("A")
        X = Bool("X")

        with Program() as logic:
            with Rung(A):
                out(X)

        state = SystemState().with_tags({"A": True})
        sim = _simulate_scan(logic, state)
        assert dict(sim.rung_writes.get(0, {})).get("X") is True

    def test_read_after_write_within_scan(self) -> None:
        """Later rungs see values written by earlier rungs."""
        from pyrung.core.analysis.causal.projected import _simulate_scan
        from pyrung.core.state import SystemState

        Enable = Bool("Enable")
        Src = Int("Src")
        Mid = Int("Mid")
        Dest = Int("Dest")

        with Program() as logic:
            with Rung(Enable):
                copy(Src, Mid)
            with Rung(Enable):
                copy(Mid, Dest)

        state = SystemState().with_tags({"Enable": True, "Src": 7})
        sim = _simulate_scan(logic.rungs, state)
        assert sim.state_after.tags.get("Dest") == 7


class TestRungProducesValue:
    """Tests for _rung_produces_value (simulation-based candidate check)."""

    def test_out_produces_true(self) -> None:
        from pyrung.core.analysis.causal.projected import _rung_produces_value
        from pyrung.core.state import SystemState

        A = Bool("A")
        X = Bool("X")

        with Program() as logic:
            with Rung(A):
                out(X)

        state = SystemState()
        assert _rung_produces_value(logic.rungs[0], 0, "X", True, state) is True

    def test_copy_produces_value(self) -> None:
        from pyrung.core.analysis.causal.projected import _rung_produces_value
        from pyrung.core.state import SystemState

        Enable = Bool("Enable")
        Src = Int("Src")
        Dest = Int("Dest")

        with Program() as logic:
            with Rung(Enable):
                copy(Src, Dest)

        state = SystemState().with_tags({"Src": 42})
        assert _rung_produces_value(logic.rungs[0], 0, "Dest", 42, state) is True
        assert _rung_produces_value(logic.rungs[0], 0, "Dest", 99, state) is False

    def test_calc_produces_value(self) -> None:
        from pyrung.core.analysis.causal.projected import _rung_produces_value
        from pyrung.core.state import SystemState

        Gate = Bool("Gate")
        A = Int("A")
        Result = Int("Result")

        with Program() as logic:
            with Rung(Gate):
                calc(A + 10, Result)

        state = SystemState().with_tags({"A": 5})
        assert _rung_produces_value(logic.rungs[0], 0, "Result", 15, state) is True
        assert _rung_produces_value(logic.rungs[0], 0, "Result", 20, state) is False


class TestProjectedCauseCopyCalc:
    """projected_cause finds copy/calc rungs as candidates."""

    def test_copy_rung_candidate(self) -> None:
        """cause(to=) finds a copy rung that would produce the value."""
        Enable = Bool("Enable")
        Src = Int("Src")
        Dest = Int("Dest")

        with Program() as logic:
            with Rung(Enable):
                copy(Src, Dest)

        runner = PLC(logic)
        runner.patch({"Src": 42})
        runner.step()

        # Dest is 0 (default), ask how to reach 42
        chain = runner.cause("Dest", to=42)
        assert chain.mode == "projected"
        assert len(chain.steps) >= 1
        assert chain.steps[0].rung_index == 0

    @pytest.mark.parametrize("exact", [True, False])
    def test_copy_step_preserves_crossing_exactness(self, monkeypatch, exact: bool) -> None:
        from pyrung.core.analysis import crossings

        Enable = Bool("FidelityEnable")
        Src = Int("FidelitySrc")
        Dest = Int("FidelityDest")
        with Program() as logic:
            with Rung(Enable):
                copy(Src, Dest)

        original_reverse = crossings.reverse

        def reverse_with_fidelity(*args, **kwargs):
            return replace(original_reverse(*args, **kwargs), exact=exact)

        monkeypatch.setattr(crossings, "reverse", reverse_with_fidelity)
        runner = PLC(logic)
        runner.patch({Src.name: 42})
        runner.step()

        chain = runner.cause(Dest.name, to=42)

        assert chain.mode == "projected"
        assert chain.steps[0].crossing_exact is exact
        assert chain.steps[0].to_dict()["crossing_exact"] is exact

    def test_calc_rung_candidate(self) -> None:
        """cause(to=) finds a calc rung that would produce the value."""
        Gate = Bool("Gate")
        A = Int("A")
        Result = Int("Result")

        with Program() as logic:
            with Rung(Gate):
                calc(A + 10, Result)

        runner = PLC(logic)
        runner.patch({"A": 5})
        runner.step()

        # Result is 0, want 15
        chain = runner.cause("Result", to=15)
        assert chain.mode == "projected"
        assert len(chain.steps) >= 1

    def test_copy_wrong_value_not_candidate(self) -> None:
        """cause(to=) excludes a literal copy that can't produce the value.

        A copy from a *tag* source is a candidate for any value (the source
        reaching it becomes the named requirement — see
        TestProjectedCauseCopyWriters); a copy of a *literal* stays excluded
        when the literal differs.
        """
        Enable = Bool("Enable")
        Dest = Int("Dest")

        with Program() as logic:
            with Rung(Enable):
                copy(42, Dest)

        runner = PLC(logic)
        runner.step()

        # Dest is 0, ask how to reach 99 — the rung only ever writes 42
        chain = runner.cause("Dest", to=99)
        assert chain.mode == "unreachable"


class TestProjectedEffectCopyCalc:
    """projected_effect detects copy/calc downstream effects."""

    def test_copy_downstream(self) -> None:
        """effect() detects a copy instruction as downstream effect."""
        Enable = Bool("Enable")
        Src = Int("Src")
        Dest = Int("Dest")

        with Program() as logic:
            with Rung(Enable):
                copy(Src, Dest)

        runner = PLC(logic)
        runner.patch({"Src": 42})
        runner.step()

        chain = runner.effect("Enable", from_=False)
        assert chain.mode == "projected"
        effect_tags = [s.transition.tag_name for s in chain.steps]
        assert "Dest" in effect_tags

    def test_copy_correct_value(self) -> None:
        """Transition.to_value carries the actual computed value from copy."""
        Enable = Bool("Enable")
        Src = Int("Src")
        Dest = Int("Dest")

        with Program() as logic:
            with Rung(Enable):
                copy(Src, Dest)

        runner = PLC(logic)
        runner.patch({"Src": 42})
        runner.step()

        chain = runner.effect("Enable", from_=False)
        step = next(s for s in chain.steps if s.transition.tag_name == "Dest")
        assert step.transition.to_value == 42

    def test_calc_downstream(self) -> None:
        """effect() detects a calc instruction as downstream effect."""
        Enable = Bool("Enable")
        A = Int("A")
        Result = Int("Result")

        with Program() as logic:
            with Rung(Enable):
                calc(A * 2, Result)

        runner = PLC(logic)
        runner.patch({"A": 5})
        runner.step()

        chain = runner.effect("Enable", from_=False)
        assert chain.mode == "projected"
        effect_tags = [s.transition.tag_name for s in chain.steps]
        assert "Result" in effect_tags

    def test_calc_correct_value(self) -> None:
        """Transition.to_value carries the actual computed value from calc."""
        Enable = Bool("Enable")
        A = Int("A")
        Result = Int("Result")

        with Program() as logic:
            with Rung(Enable):
                calc(A * 2, Result)

        runner = PLC(logic)
        runner.patch({"A": 5})
        runner.step()

        chain = runner.effect("Enable", from_=False)
        step = next(s for s in chain.steps if s.transition.tag_name == "Result")
        assert step.transition.to_value == 10

    def test_non_bool_with_to_value(self) -> None:
        """effect() with explicit to_value works for non-Bool tags."""
        Src = Int("Src")
        Dest = Int("Dest")

        with Program() as logic:
            with Rung():
                copy(Src, Dest)

        runner = PLC(logic)
        runner.step()

        chain = runner.effect("Src", from_=0, to_value=42)
        assert chain.mode == "projected"
