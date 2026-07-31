"""Regressions for causal identity across multiple writes in one PLC scan."""

from __future__ import annotations

from typing import Any

from pyrung import PLC, Bool, Int, Program, call, copy, latch, rung, subroutine
from pyrung.core.analysis.causal import (
    CausalChain,
    ChainStep,
    EnablingCondition,
    Transition,
)
from pyrung.core.analysis.pilot.causal import occurrence_external_supports


def _multiple_writer_program() -> tuple[Program, dict[str, Any]]:
    trigger = Bool("Trigger", external=True)
    state = Int("State")
    observed_first = Bool("ObservedFirst")
    observed_last = Bool("ObservedLast")

    with Program() as program:
        with rung(trigger):
            copy(1, state)
        with rung(state == 1):
            latch(observed_first)
        with rung(trigger):
            copy(2, state)
        with rung(state == 2):
            latch(observed_last)

    return program, {
        "trigger": trigger,
        "state": state,
        "first": observed_first,
        "last": observed_last,
    }


def _run(program: Program, tags: dict[str, Any]) -> PLC:
    plc = PLC(program)
    plc.step()
    plc.patch({tags["trigger"].name: True})
    plc.step()
    assert plc.state.tags[tags["state"].name] == 2
    assert plc.state.tags[tags["first"].name] is True
    assert plc.state.tags[tags["last"].name] is True
    return plc


def test_cause_of_scan_endpoint_selects_final_write() -> None:
    program, tags = _multiple_writer_program()
    plc = _run(program, tags)

    writes = tuple(
        writes[tags["state"].name]
        for _rung_index, writes in sorted(plc.rung_firings().items())
        if tags["state"].name in writes
    )
    assert writes == (1, 2)

    cause = plc.cause(tags["state"], scan=plc.state.scan_id, deep=True)
    assert cause is not None
    # The public effect remains the committed scan-boundary transition, while
    # its writer attribution must select the final in-scan writer.
    assert (cause.effect.from_value, cause.effect.to_value) == (0, 2)
    assert cause.steps[0].rung_index == 2


def test_cause_of_reader_uses_previous_in_scan_occurrence() -> None:
    program, tags = _multiple_writer_program()
    plc = _run(program, tags)

    cause = plc.cause(tags["last"], scan=plc.state.scan_id, deep=True)
    assert cause is not None
    state_step = next(
        step for step in cause.steps if step.transition.tag_name == tags["state"].name
    )

    # The last reader observed the second write, whose predecessor was the
    # first write in the same scan, not the previous committed scan.
    assert (state_step.transition.from_value, state_step.transition.to_value) == (1, 2)


def test_recorded_effect_preserves_all_ordered_occurrences() -> None:
    program, tags = _multiple_writer_program()
    plc = _run(program, tags)

    effect = plc.effect(tags["trigger"], scan=plc.state.scan_id)
    assert effect is not None
    transitions = tuple(
        (
            step.transition.tag_name,
            step.transition.from_value,
            step.transition.to_value,
        )
        for step in effect.steps
    )

    assert transitions == (
        (tags["state"].name, 0, 1),
        (tags["first"].name, False, True),
        (tags["state"].name, 1, 2),
        (tags["last"].name, False, True),
    )


def test_recorded_effect_follows_unconditional_data_reads() -> None:
    trigger = Bool("Trigger", external=True)
    source = Int("Source")
    state = Int("State")
    observed = Bool("Observed")

    with Program() as program:
        with rung(trigger):
            copy(1, source)
        with rung():
            copy(source, state)
        with rung(state == 1):
            latch(observed)

    plc = PLC(program)
    plc.step()
    plc.patch({trigger.name: True})
    plc.step()

    effect = plc.effect(trigger, scan=plc.state.scan_id)
    assert effect is not None
    assert tuple(step.transition.tag_name for step in effect.steps) == (
        source.name,
        state.name,
        observed.name,
    )


def test_recorded_effect_follows_conditional_call_into_unconditional_subroutine() -> None:
    trigger = Bool("Trigger", external=True)
    result = Bool("Result")

    with Program() as program:
        with subroutine("Apply"):
            with rung():
                latch(result)

        with rung(trigger):
            call("Apply")

    plc = PLC(program)
    plc.step()
    plc.patch({trigger.name: True})
    plc.step()

    effect = plc.effect(trigger, scan=plc.state.scan_id)
    assert effect is not None
    assert tuple(step.transition.tag_name for step in effect.steps) == (result.name,)
    assert effect.steps[0].subroutine == "Apply"


def _repeated_subroutine_program() -> tuple[Program, dict[str, Any]]:
    trigger = Bool("Trigger", external=True)
    source = Int("Source")
    state = Int("State")
    observed_first = Bool("ObservedFirst")
    observed_last = Bool("ObservedLast")

    with Program() as program:
        with subroutine("ApplySource"):
            with rung():
                copy(source, state)

        with rung(trigger):
            copy(1, source)
            call("ApplySource")
        with rung(state == 1):
            latch(observed_first)
        with rung(trigger):
            copy(2, source)
            call("ApplySource")
        with rung(state == 2):
            latch(observed_last)

    return program, {
        "trigger": trigger,
        "source": source,
        "state": state,
        "first": observed_first,
        "last": observed_last,
    }


def test_cause_distinguishes_repeated_subroutine_occurrences() -> None:
    program, tags = _repeated_subroutine_program()
    plc = _run(program, tags)

    cause = plc.cause(tags["last"], scan=plc.state.scan_id, deep=True)
    assert cause is not None
    transitions = {
        step.transition.tag_name: (
            step.transition.from_value,
            step.transition.to_value,
        )
        for step in cause.steps
        if step.transition.tag_name in {tags["source"].name, tags["state"].name}
    }

    # Both values were written twice by the same subroutine occurrence path.
    # The causal chain needs the second occurrence's immediate predecessors.
    assert transitions == {
        tags["source"].name: (1, 2),
        tags["state"].name: (1, 2),
    }


def test_external_support_walk_does_not_follow_a_later_endpoint_write() -> None:
    """A held value must resolve through the occurrence visible to its reader."""
    state_before = Transition("State", 12, 0, 6, occurrence_ordinal=4)
    interlock = Transition("Interlock", 13, False, False, occurrence_ordinal=0)
    alarm = Transition("Alarm", 13, False, True, occurrence_ordinal=10)
    permission = Transition("Permission", 13, False, True, occurrence_ordinal=31)
    state_after = Transition("State", 13, 6, 10, occurrence_ordinal=34)
    chain = CausalChain(
        effect=state_after,
        mode="recorded",
        steps=[
            ChainStep(
                transition=state_after,
                rung_index=20,
                triggers=(permission,),
                enablers=(),
            ),
            ChainStep(
                transition=permission,
                rung_index=19,
                triggers=(),
                enablers=(EnablingCondition("Mode", 1, None),),
            ),
            ChainStep(
                transition=alarm,
                rung_index=5,
                triggers=(interlock,),
                enablers=(EnablingCondition("State", 6, 12),),
            ),
            ChainStep(
                transition=interlock,
                rung_index=1,
                triggers=(),
                enablers=(EnablingCondition("Door", False, None),),
            ),
            ChainStep(
                transition=state_before,
                rung_index=4,
                triggers=(Transition("Progress", 12, False, True, 3),),
                enablers=(),
            ),
        ],
    )

    assert occurrence_external_supports(
        chain,
        producer_rungs=frozenset({5}),
        steerable=frozenset({"Door", "Mode"}),
        accomplishments=frozenset({"Progress"}),
    ) == (("Door", False),)


def test_capture_preserves_two_same_tag_writes_inside_one_rung() -> None:
    trigger = Bool("Trigger", external=True)
    state = Int("State")

    with Program() as program:
        with rung(trigger):
            copy(1, state)
            copy(2, state)

    plc = PLC(program)
    plc.step()
    plc.patch({trigger.name: True})
    plc.step()

    effect = plc.effect(trigger, scan=plc.state.scan_id)
    assert effect is not None
    assert tuple(
        (step.transition.from_value, step.transition.to_value)
        for step in effect.steps
        if step.transition.tag_name == state.name
    ) == ((0, 1), (1, 2))


def test_capture_preserves_parent_writes_around_child_same_tag_write() -> None:
    trigger = Bool("Trigger", external=True)
    state = Int("State")

    with Program() as program:
        with subroutine("ApplyMiddle"):
            with rung():
                copy(2, state)

        with rung(trigger):
            copy(1, state)
            call("ApplyMiddle")
            copy(3, state)

    plc = PLC(program)
    plc.step()
    plc.patch({trigger.name: True})
    plc.step()

    effect = plc.effect(trigger, scan=plc.state.scan_id)
    assert effect is not None
    assert tuple(
        (step.transition.from_value, step.transition.to_value)
        for step in effect.steps
        if step.transition.tag_name == state.name
    ) == ((0, 1), (1, 2), (2, 3))


def test_cause_uses_data_read_after_an_earlier_same_rung_write() -> None:
    trigger = Bool("Trigger", external=True)
    source = Int("Source")
    result = Int("Result")

    with Program() as program:
        with rung(trigger):
            copy(1, source)
            copy(source, result)

    plc = PLC(program)
    plc.step()
    plc.patch({trigger.name: True})
    plc.step()

    cause = plc.cause(result, scan=plc.state.scan_id, deep=True)
    assert cause is not None
    assert any(
        step.transition.tag_name == source.name
        and step.transition.from_value == 0
        and step.transition.to_value == 1
        for step in cause.steps
    )


def test_cause_scopes_data_reads_to_the_selected_writer_instruction() -> None:
    trigger = Bool("Trigger", external=True)
    earlier_source = Int("EarlierSource")
    final_source = Int("FinalSource")
    result = Int("Result")

    with Program() as program:
        with rung(trigger):
            copy(1, earlier_source)
            copy(2, final_source)
        with rung():
            copy(earlier_source, result)
            copy(final_source, result)

    plc = PLC(program)
    plc.step()
    plc.patch({trigger.name: True})
    plc.step()

    cause = plc.cause(result, scan=plc.state.scan_id, deep=True)
    assert cause is not None
    causal_tags = {step.transition.tag_name for step in cause.steps}
    assert final_source.name in causal_tags
    assert earlier_source.name not in causal_tags


def test_cause_does_not_follow_write_hidden_by_continued_view() -> None:
    state = Int("State")
    observed = Bool("Observed")

    with Program() as program:
        with rung():
            copy(1, state)
            copy(0, state)
        with rung(state == 0).continued():
            latch(observed)

    plc = PLC(program)
    plc.step()

    cause = plc.cause(observed, scan=plc.state.scan_id, deep=True)
    assert cause is not None
    assert all(trigger.tag_name != state.name for step in cause.steps for trigger in step.triggers)
