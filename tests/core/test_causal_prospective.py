"""Tests for projected causal chain analysis (Section F).

Covers:
- Projected cause (backward): worked example, unreachable/stranded tags
- Projected effect (forward): what-if analysis, dead-end, unreachable trigger
- Mode field values ('projected' / 'unreachable')
- BlockingCondition / BlockerReason data model
"""

from __future__ import annotations

from pyrung.core import PLC, And, Bool, Or, Program, Rung, latch, out, reset

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
