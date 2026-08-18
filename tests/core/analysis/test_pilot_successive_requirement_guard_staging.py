"""Deadline-staged recovery preserves later mandatory guard requirements."""

from __future__ import annotations

from itertools import islice
from types import SimpleNamespace

from pyrung.core.analysis.pilot import pilot_events
from pyrung.core.analysis.pilot.effects import EffectOccurrenceSnapshot
from pyrung.core.analysis.pilot.pilot import _mandatory_guard_blocker
from pyrung.core.analysis.pilot.requirements import (
    GuardLogic,
    GuardRequirementAtom,
    GuardRequirementExpr,
    OperandAuthority,
)
from pyrung.core.crossing import Cmp
from tests.fixtures import pilot_successive_requirements_then_guard as fixture


def _events(scenario):
    return tuple(
        islice(
            pilot_events(
                fixture.new_plc(scenario),
                scenario.SequenceState == fixture.TARGET,
                max_scans=40,
            ),
            160,
        )
    )


def _condition_tags(condition) -> frozenset[str]:
    tag = getattr(condition, "tag", None)
    if tag is not None:
        return frozenset((tag,))
    nested = getattr(condition, "condition", None)
    if nested is not None:
        return _condition_tags(nested)
    return frozenset(
        tag for term in getattr(condition, "terms", ()) for tag in _condition_tags(term)
    )


def _guard_read(tag: str, value: object, ordinal: int) -> EffectOccurrenceSnapshot:
    return EffectOccurrenceSnapshot(
        kind="read",
        ordinal=ordinal,
        scan_id=7,
        run_order=7,
        call_invocation=None,
        rung=(None, 7),
        execution_kind="rung",
        caller_rung=7,
        call_stack=(),
        depth=0,
        enabled=True,
        tag=tag,
        values=(value,),
    )


def test_program_owned_blocker_does_not_decline_with_missing_adjustable_work() -> None:
    program_read = _guard_read("ProgramGuard", False, 1)
    adjustable_read = _guard_read("AdjustableGuard", False, 2)
    program_atom = GuardRequirementAtom(
        Cmp("ProgramGuard", "!=", False),
        (program_read,),
        program_read,
        (0,),
        OperandAuthority.PROGRAM_WRITTEN,
    )
    adjustable_atom = GuardRequirementAtom(
        Cmp("AdjustableGuard", "!=", False),
        (adjustable_read,),
        adjustable_read,
        (1,),
        OperandAuthority.ADJUSTABLE,
    )
    requirement = SimpleNamespace(
        condition=GuardRequirementExpr(
            GuardLogic.ALL,
            (program_atom, adjustable_atom),
            exhaustive=True,
        ),
        scope=(("overwriter_guard", object()),),
    )

    assert (
        _mandatory_guard_blocker(
            (requirement,),
            {"ProgramGuard": False, "AdjustableGuard": False},
        )
        is None
    )


def test_same_source_requirements_compose_before_adjustable_final_guard() -> None:
    scenario = fixture.adjustable
    events = _events(scenario)
    requirements = tuple(
        event.data["requirement"] for event in events if event.kind == "requirement_activated"
    )
    repairs = tuple(event for event in events if event.kind == "requirement_locally_repaired")

    first_index = next(
        index
        for index, event in enumerate(events)
        if event.kind == "requirement_activated"
        and _condition_tags(event.data["requirement"].condition)
        == frozenset((scenario.FirstPresetMs.name,))
    )
    second_index = next(
        index
        for index, event in enumerate(events)
        if event.kind == "requirement_activated"
        and _condition_tags(event.data["requirement"].condition)
        == frozenset((scenario.SecondPresetMs.name,))
    )
    guard_index = next(
        index
        for index, event in enumerate(events)
        if event.kind == "requirement_activated"
        and _condition_tags(event.data["requirement"].condition)
        == frozenset((scenario.FinalGuard.name,))
    )
    assert first_index < second_index < guard_index
    assert any(
        _condition_tags(requirement.condition) == frozenset((scenario.FinalGuard.name,))
        for requirement in requirements
    )
    assert repairs == ()
    temporal_decisions = tuple(
        (
            event.kind,
            (
                event.data["configuration"][0]
                if event.kind == "theory_correction_composed"
                else tuple(event.data["applied"])
            ),
        )
        for event in events
        if event.kind in {"candidate_try", "theory_correction_composed"}
    )
    assert temporal_decisions == (
        ("candidate_try", ((scenario.StartCommand.name, True),)),
        (
            "theory_correction_composed",
            (scenario.FirstPresetMs.name, 11),
        ),
        ("candidate_try", ((scenario.StartCommand.name, True),)),
        (
            "theory_correction_composed",
            (scenario.SecondPresetMs.name, 11),
        ),
        ("candidate_try", ((scenario.StartCommand.name, True),)),
        (
            "candidate_try",
            ((scenario.FinalGuard.name, True),),
        ),
    )
    assert (
        sum(
            event.kind == "candidate_try"
            and (scenario.StartCommand.name, True) in tuple(event.data["applied"])
            for event in events
        )
        == 3
    )
    assert events[-1].kind == "finished"
    assert events[-1].data["reached"] is True

    plan = fixture.new_plc(scenario).how(
        scenario.SequenceState == fixture.TARGET,
        max_scans=40,
    )
    assert plan.reachable, plan.reason
    assert plan.state.tags[scenario.SequenceState.name] == fixture.TARGET
    assert plan.replay().state.tags[scenario.SequenceState.name] == fixture.TARGET


def test_program_owned_final_guard_remains_mandatory() -> None:
    scenario = fixture.program_owned
    events = _events(scenario)
    requirements = tuple(
        event.data["requirement"] for event in events if event.kind == "requirement_activated"
    )

    assert any(
        scenario.FinalGuard.name in _condition_tags(requirement.condition)
        for requirement in requirements
    )
    assert not any(
        tag == scenario.FinalGuard.name
        for event in events
        for key in ("assignments", "applied")
        for tag, _value in tuple(event.data.get(key, ()))
    )
    finished = tuple(event for event in events if event.kind == "finished")
    assert len(finished) == 1
    assert finished[0].data["reached"] is False
    assert finished[0].data["reason"] == (
        f"The machine has {scenario.FinalGuard.name}=True, but "
        f"{scenario.SequenceState.name}={fixture.TARGET!r} requires "
        f"{scenario.FinalGuard.name} != True; "
        f"{scenario.FinalGuard.name} is controlled by the program."
    )
