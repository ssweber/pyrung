"""Production-equivalence tests for Stage 4 shadow theory recording."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any

from pyrung import PLC, Bool, Int, Program, copy, latch, rung, system
from pyrung.core.analysis.pilot import pilot as pilot_module
from pyrung.core.analysis.pilot.working_theory import (
    AdvanceTheory,
    OpenTheory,
    ProveTheory,
    RecordTheoryAttempt,
    RecordUnattributedEvidence,
    RefineTheory,
)


def _direct_producer_program() -> tuple[Program, Bool]:
    producer = Bool("producer", external=True)
    consumer = Bool("consumer")
    with Program() as logic:
        with rung(producer):
            latch(consumer)
    return logic, consumer


def _stable_public_value(value: Any, *, field_name: str = "") -> Any:
    """Retain every stable diagnostic field while naming opaque owner roles."""

    if field_name == "causal_identity":
        assert len(value) == 3
        return ("execution-epoch", "execution-owner", "checkpoint-owner")
    if value is None or isinstance(value, bool | int | float | str | bytes):
        return value
    if isinstance(value, Enum):
        return (type(value).__module__, type(value).__qualname__, value.name)
    if isinstance(value, PLC):
        return (
            "PLC",
            value.state.scan_id,
            _stable_public_value(dict(value.state.tags)),
        )
    if isinstance(value, Mapping):
        return tuple(
            sorted(
                (
                    (_stable_public_value(key), _stable_public_value(member))
                    for key, member in value.items()
                ),
                key=repr,
            )
        )
    if isinstance(value, tuple | list):
        return tuple(_stable_public_value(item) for item in value)
    if isinstance(value, set | frozenset):
        return tuple(sorted((_stable_public_value(item) for item in value), key=repr))
    if is_dataclass(value):
        return (
            type(value).__module__,
            type(value).__qualname__,
            tuple(
                (
                    item.name,
                    _stable_public_value(getattr(value, item.name), field_name=item.name),
                )
                for item in fields(value)
            ),
        )
    snapshot = getattr(value, "diagnostic_snapshot", None)
    if callable(snapshot):
        return (
            type(value).__module__,
            type(value).__qualname__,
            _stable_public_value(snapshot()),
        )
    return (
        type(value).__module__,
        type(value).__qualname__,
        _stable_public_value(pilot_module._semantic_key(value)),
    )


def _stable_public_event(event: Any) -> tuple[Any, ...]:
    return (event.kind, event.scan, _stable_public_value(event.data))


def _stable_plan_result(plan: Any) -> tuple[Any, ...]:
    return (
        plan.status,
        plan.reachable,
        plan.target_tag,
        plan.target_value,
        plan.reason,
        _stable_public_value(plan.route),
        _stable_public_value(plan.journal),
        plan.anchor_scan,
        _stable_public_value(plan.journey),
        _stable_public_value(plan.hold_log),
        _stable_public_value(plan.lever_notes),
        plan.avoid_names,
        plan.total_scans,
        _stable_public_value(plan.changes),
        _stable_public_value(plan.ordered_steps),
        _stable_public_value(dict(plan.tags)),
    )


def test_shadow_theory_records_existing_decisions_without_changing_them(
    monkeypatch: Any,
) -> None:
    logic, consumer = _direct_producer_program()
    recorded_facts: list[Any] = []
    recorded_events: list[Any] = []
    original = pilot_module._record_shadow_theory_fact

    def record(state: Any, fact: Any) -> None:
        recorded_facts.append(fact)
        original(state, fact)

    monkeypatch.setattr(pilot_module, "_record_shadow_theory_fact", record)
    with_shadow = PLC(logic).how(consumer, max_scans=30, on_event=recorded_events.append)

    no_shadow_events: list[Any] = []
    monkeypatch.setattr(
        pilot_module,
        "_run_shadow_hook",
        lambda *_args, **_kwargs: None,
    )
    without_shadow = PLC(logic).how(consumer, max_scans=30, on_event=no_shadow_events.append)

    assert any(isinstance(fact, OpenTheory) for fact in recorded_facts)
    assert any(isinstance(fact, RecordTheoryAttempt) for fact in recorded_facts)
    assert any(isinstance(fact, AdvanceTheory) for fact in recorded_facts)
    assert any(isinstance(fact, ProveTheory) for fact in recorded_facts)
    assert not any("theory" in event.kind for event in recorded_events)

    assert _stable_plan_result(with_shadow) == _stable_plan_result(without_shadow)
    assert tuple(map(_stable_public_event, recorded_events)) == tuple(
        map(_stable_public_event, no_shadow_events)
    )


def test_bootstrap_overwrite_local_repair_records_shadow_evidence(
    monkeypatch: Any,
) -> None:
    stepper = Int("shadow_stepper", default=0)
    consumer_guard = Bool("shadow_consumer_guard", external=True)
    with Program() as logic:
        with rung(system.sys.first_scan):
            copy(1, stepper)
        with rung(~consumer_guard):
            copy(9, stepper, oneshot=True)

    facts: list[Any] = []
    original = pilot_module._record_shadow_theory_fact

    def record(state: Any, fact: Any) -> None:
        facts.append(fact)
        original(state, fact)

    monkeypatch.setattr(pilot_module, "_record_shadow_theory_fact", record)
    events: list[Any] = []
    result = PLC(logic).how(stepper == 1, max_scans=20, on_event=events.append)

    assert result.reachable
    assert any(event.kind == "requirement_locally_repaired" for event in events)
    lifecycle = tuple(
        fact
        for fact in facts
        if isinstance(
            fact,
            OpenTheory | RecordTheoryAttempt | RefineTheory | AdvanceTheory | ProveTheory,
        )
    )
    unresolved = tuple(fact for fact in facts if isinstance(fact, RecordUnattributedEvidence))
    assert lifecycle or any(
        "exact-source-world-key-unavailable" in fact.observation.evidence for fact in unresolved
    ), "the bootstrap repair must record lifecycle or explicit unresolved evidence"


def test_requirement_shadow_snapshot_preserves_exact_scan_call_and_deadline(
    monkeypatch: Any,
) -> None:
    stepper = Int("identity_stepper", default=0)
    consumer_guard = Bool("identity_consumer_guard", external=True)
    with Program() as logic:
        with rung(system.sys.first_scan):
            copy(1, stepper)
        with rung(~consumer_guard):
            copy(9, stepper, oneshot=True)

    captured: list[Any] = []
    original = pilot_module._record_shadow_repair_result

    def record_requirement(state: Any, **kwargs: Any) -> None:
        if kwargs["requirement"] is not None:
            captured.append(kwargs["requirement"])
        original(state, **kwargs)

    monkeypatch.setattr(pilot_module, "_record_shadow_repair_result", record_requirement)
    result = PLC(logic).how(stepper == 1, max_scans=20)

    assert result.reachable
    assert len(captured) == 1
    requirement = captured[0]
    snapshot = pilot_module._theory_requirement_snapshot(requirement)
    assert snapshot.source_scan == 0
    assert snapshot.demanding_occurrence == (
        requirement.demanding_occurrence.kind,
        requirement.demanding_occurrence.tag,
        requirement.demanding_occurrence.scan_id,
        requirement.demanding_occurrence.dynamic_address,
        requirement.demanding_occurrence.values,
        requirement.demanding_occurrence.enabled,
    )
    assert snapshot.deadline_occurrence == (
        requirement.deadline.kind,
        requirement.deadline.tag,
        requirement.deadline.scan_id,
        requirement.deadline.dynamic_address,
        requirement.deadline.values,
        requirement.deadline.enabled,
    )
    assert snapshot.deadline_occurrence[3][5] == requirement.deadline.call_invocation
    assert snapshot.checkpoint_token
    assert snapshot.execution_owner_token


def test_shadow_reducer_failure_cannot_change_production_result(monkeypatch: Any) -> None:
    logic, consumer = _direct_producer_program()
    baseline = PLC(logic).how(consumer, max_scans=30)

    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("shadow-only failure")

    monkeypatch.setattr(pilot_module, "reduce_theory", fail)
    observed = PLC(logic).how(consumer, max_scans=30)

    assert observed.status is baseline.status
    assert observed.reason == baseline.reason
    assert observed.changes == baseline.changes
    assert observed.ordered_steps == baseline.ordered_steps
    assert observed.total_scans == baseline.total_scans
    assert dict(observed.tags) == dict(baseline.tags)


def test_shadow_attempt_adapter_adds_no_projection_replay(monkeypatch: Any) -> None:
    """Shadow interpretation reuses the projection paid for by the steer."""

    logic, consumer = _direct_producer_program()
    original_projection = PLC._replay_pilot_rung_write_projection_at
    projection_calls: list[tuple[PLC, int]] = []

    def counted_projection(plc: PLC, scan_id: int):
        projection_calls.append((plc, scan_id))
        return original_projection(plc, scan_id)

    monkeypatch.setattr(
        PLC,
        "_replay_pilot_rung_write_projection_at",
        counted_projection,
    )
    with_shadow = PLC(logic).how(consumer, max_scans=30)
    with_shadow_count = len(projection_calls)

    projection_calls.clear()
    monkeypatch.setattr(
        pilot_module,
        "_shadow_transition_from_attempt",
        lambda *_args, **_kwargs: None,
    )
    without_shadow = PLC(logic).how(consumer, max_scans=30)

    assert with_shadow.reachable and without_shadow.reachable
    assert with_shadow_count > 0
    assert len(projection_calls) == with_shadow_count
