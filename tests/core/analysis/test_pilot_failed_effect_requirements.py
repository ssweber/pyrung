from __future__ import annotations

from dataclasses import dataclass, replace
from types import MappingProxyType, SimpleNamespace
from typing import Any, cast

import pytest

from pyrung import PLC, And, Bool, Int, Or, Program, copy, out, rise, rung
from pyrung.core.analysis.causal._rung_writes import (
    RungRead,
    RungWrite,
    ScanRungWriteProjection,
)
from pyrung.core.analysis.causal.models import Transition
from pyrung.core.analysis.pilot.advance import (
    AdvanceIndex,
    AdvanceOwner,
    build_advance_index,
)
from pyrung.core.analysis.pilot.api import pilot_events
from pyrung.core.analysis.pilot.effect_observation import observe_execution_window
from pyrung.core.analysis.pilot.effects import (
    EffectExpectation,
    EffectObligation,
)
from pyrung.core.analysis.pilot.requirement_derivation import (
    _residualize_guard_requirement,
    derive_advance_operand_requirement,
    derive_advance_requirement_from_effect,
    derive_guard_requirement_from_effect,
    explain_selected_absence,
)
from pyrung.core.analysis.pilot.requirements import (
    FailureExplanationKind,
    GuardLogic,
    GuardRequirementAtom,
    GuardRequirementExpr,
    OperandAuthority,
    classify_bound_operand_authority,
)
from pyrung.core.context import RungId
from pyrung.core.crossing import Cmp
from pyrung.core.executor import ReadOccurrence, WriteOccurrence
from pyrung.core.instruction.timers import OnDelayInstruction
from tests.fixtures.pilot_alarm_presets import (
    aborted_on_first_scan,
    alarmed_at_start,
    conditional_negative,
)


@dataclass(frozen=True)
class _Evidence:
    index: AdvanceIndex
    projection: ScanRungWriteProjection
    definition: RungWrite
    enable_read: RungRead
    acc_read: RungRead
    preset_read: RungRead
    demanding_read: RungRead
    epoch: Any
    epoch_owner: Any
    checkpoint: Any


def test_target_self_guard_is_removed_only_as_an_independent_or_alternative() -> None:
    deadline = cast(Any, object())
    self_guard = GuardRequirementAtom(Cmp("State", "!=", 4), (), deadline, (0,))
    external_guard = GuardRequirementAtom(Cmp("Permit", "==", False), (), deadline, (1,))

    residual = _residualize_guard_requirement(
        GuardRequirementExpr(GuardLogic.ANY, (self_guard, external_guard)),
        (("State", 4),),
    )

    assert residual is external_guard
    assert (
        _residualize_guard_requirement(
            GuardRequirementExpr(GuardLogic.ALL, (self_guard, external_guard)),
            (("State", 4),),
        )
        is None
    )
    compatible_same_tag = GuardRequirementAtom(Cmp("State", ">", 20), (), deadline, (2,))
    assert (
        _residualize_guard_requirement(compatible_same_tag, (("State", 81),)) is compatible_same_tag
    )


def _evidence() -> _Evidence:
    owner_instruction = OnDelayInstruction(
        Bool("Watchdog.Done"),
        Int("Watchdog.Acc"),
        Int("WatchdogPresetMs"),
        Bool("Watchdog.Enable"),
        unit="ms",
    )
    timer_instruction_run = SimpleNamespace(instruction=owner_instruction)
    alarm_instruction_run = SimpleNamespace(instruction=object())
    timer_run = SimpleNamespace(
        kind="main",
        caller_rung=-1,
        call_stack=(),
        depth=0,
        enabled=True,
        body=(),
    )
    alarm_run = SimpleNamespace(
        kind="main",
        caller_rung=-1,
        call_stack=(),
        depth=0,
        enabled=True,
        body=(),
    )
    timer_rung = RungId(None, 3)
    alarm_rung = RungId(None, 4)

    enable_read = RungRead(
        1,
        20,
        0,
        None,
        timer_rung,
        cast(Any, timer_run),
        None,
        ReadOccurrence(20, "tag", "Watchdog.Enable", True, "entry"),
    )
    acc_read = RungRead(
        1,
        24,
        0,
        None,
        timer_rung,
        cast(Any, timer_run),
        cast(Any, timer_instruction_run),
        ReadOccurrence(24, "tag", "Watchdog.Acc", 0, "entry"),
    )
    preset_read = RungRead(
        1,
        29,
        0,
        None,
        timer_rung,
        cast(Any, timer_run),
        cast(Any, timer_instruction_run),
        ReadOccurrence(29, "tag", "WatchdogPresetMs", 0, "entry"),
    )
    done_occurrence = WriteOccurrence(
        33,
        "tag",
        "Watchdog.Done",
        False,
        True,
    )
    definition = RungWrite(
        1,
        33,
        0,
        None,
        timer_rung,
        cast(Any, timer_run),
        cast(Any, timer_instruction_run),
        done_occurrence,
        Transition("Watchdog.Done", 1, False, True, 33),
    )
    accumulator_occurrence = WriteOccurrence(
        34,
        "tag",
        "Watchdog.Acc",
        0,
        10,
    )
    accumulator_write = RungWrite(
        1,
        34,
        0,
        None,
        timer_rung,
        cast(Any, timer_run),
        cast(Any, timer_instruction_run),
        accumulator_occurrence,
        Transition("Watchdog.Acc", 1, 0, 10, 34),
    )
    demanding_read = RungRead(
        1,
        35,
        1,
        None,
        alarm_rung,
        cast(Any, alarm_run),
        cast(Any, alarm_instruction_run),
        ReadOccurrence(35, "tag", "Watchdog.Done", True, done_occurrence),
    )
    projection = ScanRungWriteProjection(
        scan_id=1,
        entry_tags={"Watchdog.Acc": 0, "WatchdogPresetMs": 0},
        exit_tags={"Watchdog.Acc": 10, "Watchdog.Done": True},
        runs=(cast(Any, timer_run), cast(Any, alarm_run)),
        writes=(definition, accumulator_write),
        reads=(enable_read, acc_read, preset_read, demanding_read),
    )
    owner = AdvanceOwner(owner_instruction.advance_profile(), owner_instruction)
    epoch = SimpleNamespace(first_scan=1, last_scan=1)
    epoch_owner = SimpleNamespace(epoch=epoch)
    checkpoint = SimpleNamespace(owner=object())
    return _Evidence(
        AdvanceIndex(
            MappingProxyType({"Watchdog.Done": owner}),
            MappingProxyType({}),
        ),
        projection,
        definition,
        enable_read,
        acc_read,
        preset_read,
        demanding_read,
        epoch,
        epoch_owner,
        checkpoint,
    )


def _derive(
    evidence: _Evidence,
    *,
    desired_completion: bool = False,
    authority: OperandAuthority = OperandAuthority.ADJUSTABLE,
    **overrides: Any,
):
    arguments = {
        "projection": evidence.projection,
        "definition_write": evidence.definition,
        "operand_read": evidence.preset_read,
        "demanding_read": evidence.demanding_read,
        "operand_authority": authority,
        "execution_epoch": evidence.epoch,
        "execution_owner": evidence.epoch_owner,
        "selected_writer": (None, 2, ()),
        "source_world_key": ("world", 0),
        "source_checkpoint": evidence.checkpoint,
        "explanation_kind": FailureExplanationKind.OVERWRITTEN,
    }
    arguments.update(overrides)
    return derive_advance_operand_requirement(
        evidence.index,
        "Watchdog.Done",
        desired_completion=desired_completion,
        **arguments,
    )


def test_watchdog_noncompletion_transposes_to_preset_with_operand_deadline() -> None:
    result = _derive(_evidence())

    assert result.requirement is not None
    assert result.requirement.condition == Cmp("WatchdogPresetMs", ">", 10)
    assert result.requirement.deadline.ordinal == 29
    assert result.requirement.demanding_occurrence.ordinal == 35
    assert result.explanation.kind is FailureExplanationKind.OVERWRITTEN
    assert [item.ordinal for item in result.explanation.supporting_occurrences] == [
        29,
        34,
        35,
    ]


def test_on_delay_profile_declares_exact_completion_controls() -> None:
    evidence = _evidence()
    owner = evidence.index.resolve("Watchdog.Done")
    assert owner is not None

    controls = owner.profile.completion_controls
    assert controls is not None
    assert [(demand.condition, demand.value) for demand in controls] == [
        (owner.instruction.enable_condition, True),
    ]


def test_profile_without_completion_controls_fails_closed() -> None:
    evidence = _evidence()
    owner = evidence.index.resolve("Watchdog.Done")
    assert owner is not None
    unsupported = replace(
        evidence,
        index=AdvanceIndex(
            MappingProxyType(
                {
                    "Watchdog.Done": replace(
                        owner,
                        profile=replace(owner.profile, completion_controls=None),
                    )
                }
            ),
            MappingProxyType({}),
        ),
    )

    result = _derive(unsupported)

    assert result.requirement is None
    assert "does not declare exact completion controls" in result.explanation.detail


def test_consequential_done_false_can_require_completion_in_opposite_direction() -> None:
    evidence = _evidence()
    preset_read = replace(
        evidence.preset_read,
        occurrence=ReadOccurrence(29, "tag", "WatchdogPresetMs", 20, "entry"),
    )
    false_definition_occurrence = WriteOccurrence(
        33,
        "tag",
        "Watchdog.Done",
        True,
        False,
    )
    false_definition = replace(
        evidence.definition,
        occurrence=false_definition_occurrence,
        transition=Transition("Watchdog.Done", 1, True, False, 33),
    )
    false_demand = replace(
        evidence.demanding_read,
        occurrence=ReadOccurrence(
            35,
            "tag",
            "Watchdog.Done",
            False,
            false_definition_occurrence,
        ),
    )
    projection = replace(
        evidence.projection,
        writes=(false_definition, evidence.projection.writes[1]),
        entry_tags={"Watchdog.Acc": 0, "WatchdogPresetMs": 20},
        exit_tags={
            "Watchdog.Acc": 10,
            "WatchdogPresetMs": 20,
            "Watchdog.Done": False,
        },
        reads=(evidence.enable_read, evidence.acc_read, preset_read, false_demand),
    )

    result = _derive(
        evidence,
        desired_completion=True,
        projection=projection,
        definition_write=false_definition,
        operand_read=preset_read,
        demanding_read=false_demand,
        explanation_kind=FailureExplanationKind.DISPLACED,
    )

    assert result.requirement is not None
    assert result.requirement.condition == Cmp("WatchdogPresetMs", "<=", 10)
    assert result.requirement.deadline.ordinal == 29


def test_disabled_owner_write_is_not_treated_as_completion_comparison() -> None:
    evidence = _evidence()
    cast(Any, evidence.definition.run).enabled = False
    disabled_enable = replace(
        evidence.enable_read,
        occurrence=ReadOccurrence(20, "tag", "Watchdog.Enable", False, "entry"),
    )
    evidence.projection.reads = (
        disabled_enable,
        evidence.acc_read,
        evidence.preset_read,
        evidence.demanding_read,
    )
    evidence.projection.__post_init__()

    result = _derive(evidence)

    assert result.requirement is None
    assert result.explanation.kind is FailureExplanationKind.UNKNOWN
    assert "completion control" in result.explanation.detail


def test_reset_owner_write_is_not_treated_as_completion_comparison() -> None:
    evidence = _evidence()
    reset = Bool("Watchdog.Reset")
    instruction = OnDelayInstruction(
        Bool("Watchdog.Done"),
        Int("Watchdog.Acc"),
        Int("WatchdogPresetMs"),
        Bool("Watchdog.Enable"),
        reset_condition=reset,
        unit="ms",
    )
    instruction_run = SimpleNamespace(instruction=instruction)
    timer_run = evidence.definition.run
    reset_read = RungRead(
        1,
        23,
        0,
        None,
        evidence.definition.rung_id,
        timer_run,
        cast(Any, instruction_run),
        ReadOccurrence(23, "tag", reset.name, True, "entry"),
    )
    acc_read = replace(
        evidence.acc_read,
        instruction=cast(Any, instruction_run),
    )
    preset_read = replace(
        evidence.preset_read,
        instruction=cast(Any, instruction_run),
    )
    definition = replace(
        evidence.definition,
        instruction=cast(Any, instruction_run),
        occurrence=WriteOccurrence(33, "tag", "Watchdog.Done", True, False),
        transition=Transition("Watchdog.Done", 1, True, False, 33),
    )
    accumulator_write = replace(
        evidence.projection.writes[1],
        instruction=cast(Any, instruction_run),
        occurrence=WriteOccurrence(34, "tag", "Watchdog.Acc", 10, 0),
        transition=Transition("Watchdog.Acc", 1, 10, 0, 34),
    )
    demanding = replace(
        evidence.demanding_read,
        occurrence=ReadOccurrence(
            35,
            "tag",
            "Watchdog.Done",
            False,
            definition.occurrence,
        ),
    )
    projection = replace(
        evidence.projection,
        entry_tags={
            "Watchdog.Acc": 10,
            "WatchdogPresetMs": 20,
            reset.name: True,
        },
        exit_tags={
            "Watchdog.Acc": 0,
            "WatchdogPresetMs": 20,
            "Watchdog.Done": False,
            reset.name: True,
        },
        reads=(evidence.enable_read, reset_read, acc_read, preset_read, demanding),
        writes=(definition, accumulator_write),
    )
    owner = AdvanceOwner(instruction.advance_profile(), instruction)
    reset_evidence = replace(
        evidence,
        index=AdvanceIndex(
            MappingProxyType({"Watchdog.Done": owner}),
            MappingProxyType({}),
        ),
        projection=projection,
        definition=definition,
        acc_read=acc_read,
        preset_read=preset_read,
        demanding_read=demanding,
    )

    result = _derive(reset_evidence, desired_completion=True)

    assert result.requirement is None
    assert result.explanation.kind is FailureExplanationKind.UNKNOWN
    assert "completion control" in result.explanation.detail


def test_entry_accumulator_read_is_not_a_requirement_without_delta_inverse() -> None:
    evidence = _evidence()

    result = _derive(
        evidence,
        operand_read=evidence.acc_read,
    )

    assert result.requirement is None
    assert result.explanation.kind is FailureExplanationKind.UNKNOWN
    assert "cannot be transposed" in result.explanation.detail


def test_configured_preset_retains_constraint_without_assignment_permission() -> None:
    result = _derive(_evidence(), authority=OperandAuthority.CONFIGURED)

    assert result.requirement is not None
    assert result.requirement.condition == Cmp("WatchdogPresetMs", ">", 10)
    assert not result.requirement.permits_assignment


def test_nonzero_or_program_written_preset_is_authoritative() -> None:
    common = {
        "tag": "WatchdogPresetMs",
        "declared_default": 0,
        "steerable": frozenset(("WatchdogPresetMs",)),
    }

    assert (
        classify_bound_operand_authority(
            **common,
            source_value=25,
            program_written=frozenset(),
        )
        is OperandAuthority.CONFIGURED
    )
    assert (
        classify_bound_operand_authority(
            **common,
            source_value=0,
            program_written=frozenset(("WatchdogPresetMs",)),
        )
        is OperandAuthority.PROGRAM_WRITTEN
    )
    assert (
        classify_bound_operand_authority(
            **common,
            source_value=0,
            program_written=frozenset(),
        )
        is OperandAuthority.ADJUSTABLE
    )
    assert (
        classify_bound_operand_authority(
            **common,
            source_value=0,
            program_written=frozenset(),
            configured=frozenset(("WatchdogPresetMs",)),
        )
        is OperandAuthority.CONFIGURED
    )
    assert (
        classify_bound_operand_authority(
            **common,
            source_value=25,
            program_written=frozenset(),
            provisional=frozenset(("WatchdogPresetMs",)),
        )
        is OperandAuthority.ADJUSTABLE
    )
    assert (
        classify_bound_operand_authority(
            **common,
            source_value=25,
            program_written=frozenset(),
            configured=frozenset(("WatchdogPresetMs",)),
            provisional=frozenset(("WatchdogPresetMs",)),
        )
        is OperandAuthority.CONFIGURED
    )


def test_projection_rejects_unowned_operand_occurrence_at_construction() -> None:
    evidence = _evidence()
    unrelated = replace(
        evidence.preset_read,
        run=cast(
            Any,
            SimpleNamespace(
                kind="main",
                caller_rung=-1,
                call_stack=(),
                depth=0,
                enabled=True,
                body=(),
            ),
        ),
    )
    with pytest.raises(ValueError, match="run identity"):
        replace(
            evidence.projection,
            reads=(
                evidence.enable_read,
                evidence.acc_read,
                unrelated,
                evidence.demanding_read,
            ),
        )


def test_later_operand_cannot_be_an_exact_deadline() -> None:
    evidence = _evidence()
    late = replace(
        evidence.preset_read,
        ordinal=40,
        occurrence=ReadOccurrence(40, "tag", "WatchdogPresetMs", 0, "entry"),
    )
    projection = replace(
        evidence.projection,
        reads=(evidence.enable_read, evidence.acc_read, evidence.demanding_read, late),
    )

    result = _derive(evidence, projection=projection, operand_read=late)

    assert result.requirement is None
    assert result.explanation.kind is FailureExplanationKind.UNKNOWN


def test_missing_epoch_or_checkpoint_fails_closed() -> None:
    evidence = _evidence()

    assert _derive(evidence, execution_epoch=None).requirement is None
    assert _derive(evidence, source_checkpoint=None).requirement is None


def test_distinct_same_range_epochs_have_distinct_requirement_identity() -> None:
    evidence = _evidence()
    first = _derive(evidence).requirement
    other_epoch = SimpleNamespace(first_scan=1, last_scan=1)
    second = _derive(
        evidence,
        execution_epoch=other_epoch,
        execution_owner=SimpleNamespace(epoch=other_epoch),
    ).requirement

    assert first is not None and second is not None
    assert first.identity != second.identity
    assert (
        first.diagnostic_snapshot().causal_identity != second.diagnostic_snapshot().causal_identity
    )


def test_ambiguous_owner_fails_closed() -> None:
    evidence = _evidence()
    owner = evidence.index.resolve("Watchdog.Done")
    assert owner is not None
    ambiguous = replace(
        evidence,
        index=AdvanceIndex(
            MappingProxyType({}),
            MappingProxyType({"Watchdog.Done": (owner, owner)}),
        ),
    )

    result = _derive(ambiguous)

    assert result.requirement is None
    assert result.explanation.kind is FailureExplanationKind.UNKNOWN


def test_effect_adapter_uses_only_exact_consequential_owner_read() -> None:
    evidence = _evidence()
    observation = SimpleNamespace(
        disposition="OVERWRITTEN",
        observed_reads=(evidence.demanding_read,),
    )

    result = derive_advance_requirement_from_effect(
        evidence.index,
        evidence.projection,
        observation,
        operand_authorities={"WatchdogPresetMs": OperandAuthority.ADJUSTABLE},
        execution_epoch=evidence.epoch,
        execution_owner=evidence.epoch_owner,
        selected_writer=(None, 2, ()),
        source_world_key=("world", 0),
        source_checkpoint=evidence.checkpoint,
    )

    assert result.requirement is not None
    assert result.requirement.condition == Cmp("WatchdogPresetMs", ">", 10)


def test_effect_adapter_uses_exact_displacement_ancestry_before_local_reads() -> None:
    evidence = _evidence()
    observation = SimpleNamespace(
        disposition="OVERWRITTEN",
        observed_reads=(evidence.enable_read,),
        displacement_enabling_reads=(evidence.demanding_read,),
    )

    result = derive_advance_requirement_from_effect(
        evidence.index,
        evidence.projection,
        observation,
        operand_authorities={"WatchdogPresetMs": OperandAuthority.ADJUSTABLE},
        execution_epoch=evidence.epoch,
        execution_owner=evidence.epoch_owner,
        selected_writer=(None, 2, ()),
        source_world_key=("world", 0),
        source_checkpoint=evidence.checkpoint,
    )

    assert result.requirement is not None
    assert result.requirement.demanding_occurrence.ordinal == evidence.demanding_read.ordinal


def test_effect_adapter_keeps_exact_predecessor_reads_beyond_displacement_ancestry() -> None:
    evidence = _evidence()
    observation = SimpleNamespace(
        disposition="OVERWRITTEN",
        observed_reads=(evidence.demanding_read,),
        displacement_enabling_reads=(evidence.enable_read,),
    )

    result = derive_advance_requirement_from_effect(
        evidence.index,
        evidence.projection,
        observation,
        operand_authorities={"WatchdogPresetMs": OperandAuthority.ADJUSTABLE},
        execution_epoch=evidence.epoch,
        execution_owner=evidence.epoch_owner,
        selected_writer=(None, 2, ()),
        source_world_key=("world", 0),
        source_checkpoint=evidence.checkpoint,
    )

    assert result.requirement is not None
    assert result.requirement.condition == Cmp("WatchdogPresetMs", ">", 10)
    assert result.requirement.demanding_occurrence.ordinal == evidence.demanding_read.ordinal


def test_effect_adapter_does_not_invent_writability() -> None:
    evidence = _evidence()
    observation = SimpleNamespace(
        disposition="OVERWRITTEN",
        observed_reads=(evidence.demanding_read,),
    )

    result = derive_advance_requirement_from_effect(
        evidence.index,
        evidence.projection,
        observation,
        operand_authorities={},
        execution_epoch=evidence.epoch,
        execution_owner=evidence.epoch_owner,
        selected_writer=(None, 2, ()),
        source_world_key=("world", 0),
        source_checkpoint=evidence.checkpoint,
    )

    assert result.requirement is None
    assert result.explanation.kind is FailureExplanationKind.UNKNOWN


def test_scan_zero_observation_emits_exact_preset_requirement() -> None:
    events = pilot_events(
        PLC(aborted_on_first_scan.logic, dt=0.010),
        aborted_on_first_scan.ProcessStep == aborted_on_first_scan.AT_TARGET,
        max_scans=1,
    )
    try:
        started = next(events)
        activated = next(
            event
            for event in events
            if event.kind == "requirement_activated"
            and getattr(event.data["requirement"].condition, "tag", None)
            == aborted_on_first_scan.WatchdogPresetMs.name
        )
    finally:
        events.close()

    # Boundary zero contains no executed program evidence.  The one-scan
    # ObserveScan bearing owns the first projection; only its receipt may emit
    # the corrective while retaining scan 0 as the exact executable source.
    assert started.data["active_requirements"] == ()
    requirement = activated.data["requirement"]
    assert requirement.condition == Cmp("FirstScanWatchdogPresetMs", ">", 10)
    assert requirement.deadline.ordinal == 27
    assert requirement.demanding_occurrence.ordinal == 33
    assert requirement.operand_authority is OperandAuthority.ADJUSTABLE
    assert requirement.source_scan == 0


def test_failed_alarm_effect_derives_exact_preset_requirement() -> None:
    plc = PLC(alarmed_at_start.logic, dt=0.010)
    plc.force(alarmed_at_start.Reset, True)
    plc.force(alarmed_at_start.AtTarget, True)
    plc.step()
    projection = plc._replay_rung_write_projection_at(1)
    assert projection is not None
    complete_write = next(
        write
        for write in projection.writes
        if write.transition.tag_name == alarmed_at_start.ProcessStep.name
        and write.transition.to_value == alarmed_at_start.COMPLETE
    )
    obligation = EffectObligation(
        alarmed_at_start.ProcessStep.name,
        alarmed_at_start.COMPLETE,
        (None, 1, (0,)),
        None,
        (),
        producer_rung=complete_write.run.rung,
    )
    observation = observe_execution_window(
        EffectExpectation((obligation,)),
        plc,
        scan_before=0,
        kernel_scan_ids=(1,),
        action_scan=1,
    )[0]
    assert observation.execution_owner is not None
    assert observation.execution_epoch is observation.execution_owner.epoch

    result = derive_advance_requirement_from_effect(
        build_advance_index(alarmed_at_start.logic),
        projection,
        observation,
        operand_authorities={"WatchdogPresetMs": OperandAuthority.ADJUSTABLE},
        execution_epoch=observation.execution_epoch,
        execution_owner=observation.execution_owner,
        selected_writer=obligation.producer,
        source_world_key=("source",),
        source_checkpoint=SimpleNamespace(owner=object()),
        provenance="steer",
    )

    assert result.requirement is not None
    requirement = result.requirement
    assert requirement.condition == Cmp("WatchdogPresetMs", ">", 10)
    assert requirement.deadline.ordinal == 29
    assert requirement.demanding_occurrence.ordinal == 35
    assert requirement.operand_authority is OperandAuthority.ADJUSTABLE


def test_committed_expectation_event_retains_exact_source_and_occurrences() -> None:
    command = Bool("ReceiptCommand", external=True)
    target = Bool("ReceiptTarget")
    with Program() as program:
        with rung(command):
            out(target)

    events = list(pilot_events(PLC(program), target == bool(1), max_scans=10))
    committed = [event for event in events if event.kind == "expectation_committed"]

    assert committed
    assert not any(event.kind == "failed_effect_explained" for event in events)
    receipt = committed[0].data["receipt"]
    assert receipt.source_scan == 1
    assert len(receipt.causal_identity) == 3
    assert receipt.act_identity[0] == "pulse"
    assert receipt.producer_occurrences
    assert receipt.producer_occurrences[0].tag == target.name


def test_absent_selected_writer_explains_exact_false_guard() -> None:
    enable = Bool("AbsentGuardEnable", external=True)
    effect = Int("AbsentGuardEffect")
    with Program() as program:
        with rung(enable):
            copy(1, effect)
    plc = PLC(program)
    source = plc.fork()
    plc.step()
    projection = plc._replay_rung_write_projection_at(1)
    assert projection is not None
    obligation = EffectObligation(
        effect.name,
        1,
        (None, 0, ()),
        None,
        (),
        producer_rung=program.rungs[0],
    )
    observation = observe_execution_window(
        EffectExpectation((obligation,)),
        plc,
        scan_before=0,
        kernel_scan_ids=(1,),
        action_scan=1,
    )[0]

    explanation = explain_selected_absence(
        observation,
        projection,
        SimpleNamespace(world=SimpleNamespace(work=source), owner=object()),
    )

    assert explanation.kind is FailureExplanationKind.GUARD_FALSE
    assert [(item.tag, item.values) for item in explanation.supporting_occurrences] == [
        (enable.name, (False,))
    ]


def test_absent_guard_requirement_preserves_or_and_short_circuit_frontier() -> None:
    first_path = Bool("AbsentFirstPath", external=True)
    first_blocker = Bool("AbsentFirstBlocker", external=True)
    second_blocker = Bool("AbsentSecondBlocker", external=True)
    unobserved_suffix = Bool("AbsentUnobservedSuffix", external=True)
    alternate_writer = Bool("AbsentAlternateWriter", external=True)
    effect = Int("AbsentCompoundEffect")
    with Program() as program:
        with rung(
            Or(
                And(first_path, first_blocker),
                And(second_blocker, unobserved_suffix),
            )
        ):
            copy(1, effect)
        with rung(alternate_writer):
            copy(1, effect)

    plc = PLC(program)
    plc.force(first_path, True)
    source = plc.fork()
    plc.step()
    projection = plc._replay_rung_write_projection_at(1)
    assert projection is not None
    obligation = EffectObligation(
        effect.name,
        1,
        (None, 0, ()),
        None,
        (),
        producer_rung=program.rungs[0],
    )
    observation = observe_execution_window(
        EffectExpectation((obligation,)),
        plc,
        scan_before=0,
        kernel_scan_ids=(1,),
        action_scan=1,
    )[0]
    checkpoint = SimpleNamespace(
        owner=object(),
        world=SimpleNamespace(work=source),
    )

    result = derive_guard_requirement_from_effect(
        observation,
        projection,
        execution_epoch=observation.execution_epoch,
        execution_owner=observation.execution_owner,
        selected_writer=obligation.producer,
        source_world_key=("compound-source",),
        source_checkpoint=checkpoint,
        provenance="test",
    )

    assert result.requirement is not None
    condition = result.requirement.condition
    assert isinstance(condition, GuardRequirementExpr)
    assert condition.logic is GuardLogic.ANY
    assert all(isinstance(term, GuardRequirementAtom) for term in condition.terms)
    atoms = cast(tuple[GuardRequirementAtom, ...], condition.terms)
    assert [atom.condition for atom in atoms] == [
        Cmp(first_blocker.name, "==", True),
        Cmp(second_blocker.name, "==", True),
    ]
    assert [atom.source_path for atom in atoms] == [(0, 0, 1), (0, 1, 0)]
    assert [atom.deadline.tag for atom in atoms] == [
        first_blocker.name,
        second_blocker.name,
    ]
    assert atoms[0].deadline.ordinal < atoms[1].deadline.ordinal
    assert result.requirement.deadline == atoms[1].deadline
    assert result.requirement.demanding_occurrence == atoms[1].deadline
    assert [item.tag for item in result.explanation.supporting_occurrences] == [
        first_path.name,
        first_blocker.name,
        second_blocker.name,
    ]
    assert unobserved_suffix.name not in {
        item.tag for item in result.explanation.supporting_occurrences
    }
    assert result.requirement.selected_writer == (None, 0, ())
    assert result.requirement.operand_authority is OperandAuthority.UNKNOWN
    assert not result.requirement.permits_assignment


def test_false_or_retains_exact_arm_when_sibling_inverse_is_opaque() -> None:
    opaque_edge = Bool("AbsentOpaqueEdge", external=True)
    exact_arm = Bool("AbsentExactArm", external=True)
    effect = Int("AbsentPartialOrEffect")
    with Program() as program:
        with rung(Or(rise(opaque_edge), exact_arm)):
            copy(1, effect)

    plc = PLC(program)
    source = plc.fork()
    plc.step()
    projection = plc._replay_rung_write_projection_at(1)
    assert projection is not None
    obligation = EffectObligation(
        effect.name,
        1,
        (None, 0, ()),
        None,
        (),
        producer_rung=program.rungs[0],
    )
    observation = observe_execution_window(
        EffectExpectation((obligation,)),
        plc,
        scan_before=0,
        kernel_scan_ids=(1,),
        action_scan=1,
    )[0]

    result = derive_guard_requirement_from_effect(
        observation,
        projection,
        execution_epoch=observation.execution_epoch,
        execution_owner=observation.execution_owner,
        selected_writer=obligation.producer,
        source_world_key=("partial-or-source",),
        source_checkpoint=SimpleNamespace(
            owner=object(),
            world=SimpleNamespace(work=source),
        ),
    )

    assert result.requirement is not None
    condition = result.requirement.condition
    assert isinstance(condition, GuardRequirementExpr)
    assert condition.logic is GuardLogic.ANY
    assert not condition.exhaustive
    assert len(condition.terms) == 1
    atom = condition.terms[0]
    assert isinstance(atom, GuardRequirementAtom)
    assert atom.condition == Cmp(exact_arm.name, "==", True)
    assert result.requirement.deadline == atom.deadline


def test_stranded_exact_consumer_guard_becomes_active_requirement() -> None:
    consumer_enable = Bool("StrandedConsumerEnable", external=True)
    effect = Int("StrandedGuardEffect")
    target = Bool("StrandedGuardTarget")
    with Program() as program:
        with rung():
            copy(1, effect)
        with rung(effect == 1, consumer_enable):
            out(target)

    plc = PLC(program)
    source = plc.fork()
    plc.step()
    projection = plc._replay_rung_write_projection_at(1)
    assert projection is not None
    obligation = EffectObligation(
        effect.name,
        1,
        (None, 0, ()),
        (None, 1, ()),
        ((effect.name, 1),),
        producer_rung=program.rungs[0],
        consumer_rung=program.rungs[1],
    )
    observation = observe_execution_window(
        EffectExpectation((obligation,)),
        plc,
        scan_before=0,
        kernel_scan_ids=(1,),
        action_scan=1,
    )[0]
    assert observation.disposition == "STRANDED"

    result = derive_guard_requirement_from_effect(
        observation,
        projection,
        execution_epoch=observation.execution_epoch,
        execution_owner=observation.execution_owner,
        selected_writer=obligation.producer,
        source_world_key=("stranded-source",),
        source_checkpoint=SimpleNamespace(
            owner=object(),
            world=SimpleNamespace(work=source),
        ),
        provenance="test",
    )

    assert result.requirement is not None
    condition = result.requirement.condition
    assert isinstance(condition, GuardRequirementAtom)
    assert condition.condition == Cmp(consumer_enable.name, "==", True)
    assert condition.source_path == (1,)
    assert condition.deadline.tag == consumer_enable.name
    assert [item.tag for item in result.explanation.supporting_occurrences] == [
        effect.name,
        consumer_enable.name,
    ]
    assert result.requirement.selected_writer == obligation.producer
    assert result.requirement.scope[-1] == ("consumer_guard", obligation.consumer)


def test_absent_selected_oneshot_reads_spentness_from_source_checkpoint() -> None:
    plc = PLC(alarmed_at_start.logic, dt=0.010)
    plc.force(alarmed_at_start.Reset, True)
    plc.force(alarmed_at_start.AtTarget, True)
    plc.step()
    source = plc.fork()
    plc.force(
        alarmed_at_start.WatchdogPresetMs,
        alarmed_at_start.SAFE_WATCHDOG_PRESET_MS,
    )
    plc.step()
    projection = plc._replay_rung_write_projection_at(2)
    assert projection is not None
    obligation = EffectObligation(
        alarmed_at_start.ProcessStep.name,
        alarmed_at_start.RUNNING,
        (None, 0, ()),
        None,
        (),
        producer_rung=alarmed_at_start.logic.rungs[0],
    )
    observation = observe_execution_window(
        EffectExpectation((obligation,)),
        plc,
        scan_before=1,
        kernel_scan_ids=(2,),
        action_scan=2,
    )[0]
    assert observation.disposition == "ABSENT"

    explanation = explain_selected_absence(
        observation,
        projection,
        SimpleNamespace(world=SimpleNamespace(work=source), owner=object()),
    )

    assert explanation.kind is FailureExplanationKind.SPENT
    assert "_oneshot" in explanation.detail


def test_conditional_negative_uses_upstream_deadline_but_honors_set_preset() -> None:
    plc = PLC(conditional_negative.logic, dt=0.010)
    source = plc.fork()
    plc.step()
    projection = plc._replay_rung_write_projection_at(1)
    assert projection is not None
    definition = next(
        write
        for write in projection.writes
        if write.transition.tag_name == conditional_negative.Watchdog.Done.name
    )
    preset_read = next(
        read
        for read in projection.reads
        if read.occurrence.name == conditional_negative.PresetMs.name
    )
    demanding_read = next(
        read
        for read in projection.reads
        if read.occurrence.name == conditional_negative.Watchdog.Done.name
    )
    epoch, epoch_owner = next(
        pair
        for pair in plc._causal_lineage.seal_through(1)
        if pair[0].first_scan <= 1 <= pair[0].last_scan
    )
    checkpoint = SimpleNamespace(
        owner=object(),
        world=SimpleNamespace(work=source),
    )

    result = derive_advance_operand_requirement(
        build_advance_index(conditional_negative.logic),
        conditional_negative.Watchdog.Done.name,
        desired_completion=True,
        projection=projection,
        definition_write=definition,
        operand_read=preset_read,
        demanding_read=demanding_read,
        operand_authority=OperandAuthority.CONFIGURED,
        execution_epoch=epoch,
        execution_owner=epoch_owner,
        selected_writer=(None, 1, ()),
        source_world_key=("conditional-source",),
        source_checkpoint=checkpoint,
        explanation_kind=FailureExplanationKind.DISPLACED,
    )

    assert result.requirement is not None
    requirement = result.requirement
    assert requirement.condition == Cmp("ConditionalNegativePresetMs", "<=", 10)
    assert requirement.deadline.ordinal == 17
    assert requirement.demanding_occurrence.ordinal == 23
    assert not requirement.permits_assignment


def test_public_overwritten_effect_event_keeps_selected_writer_and_source() -> None:
    events = list(
        pilot_events(
            PLC(alarmed_at_start.logic, dt=0.010),
            alarmed_at_start.ProcessStep == alarmed_at_start.COMPLETE,
            max_scans=20,
        )
    )
    receipts = [
        event.data["receipt"] for event in events if event.kind == "failed_effect_explained"
    ]

    overwritten = next(
        receipt
        for receipt in receipts
        if receipt.explanation.kind is FailureExplanationKind.OVERWRITTEN
    )
    assert overwritten.observation.disposition == "OVERWRITTEN"
    assert overwritten.selected_writer == (None, 0, ())
    assert overwritten.source_scan == 1
