"""Read exact occurrence facts from one immutable executed attempt.

These observers consume owned execution projections and return detached facts.
They do not decide verification gates, mutate a world, or grant execution
authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pyrung.core.analysis.pdg import resolve_rung
from pyrung.core.analysis.pilot.execution import (
    IntrascanActReceipt,
    InvestigationProducerReceipt,
)
from pyrung.core.analysis.pilot.intrascan import (
    IntrascanRequirementDisposition,
    build_intrascan_requirement_evidence,
    observe_intrascan_requirement,
)
from pyrung.core.analysis.pilot.navigation_contracts import (
    IntrascanPulse,
    ProgramScan,
    _ActionPair,
)
from pyrung.core.analysis.pilot.overlay import (
    _pilot_rung_execution_receipt,
    project_pilot_overlay,
)
from pyrung.core.analysis.pilot.types import _ExecutedAttempt
from pyrung.core.analysis.pilot.world_key import _rung_identity, _semantic_key
from pyrung.core.analysis.pilot.writer_selection import _can_produce
from pyrung.core.analysis.prove.expr import _eval_expr_from_state
from pyrung.core.analysis.simplified import _sp_to_expr
from pyrung.core.analysis.sp_values import _values_match, _written_value_for_tag


@dataclass(frozen=True)
class _ProgramScanOccurrenceReceipt:
    """Exact producer observation owned by one evidence-selected scan."""

    projection_available: bool
    matching_writes: int
    retained: bool
    receipt: IntrascanActReceipt | None = None

    @property
    def witnessed(self) -> bool:
        return self.receipt is not None


@dataclass(frozen=True)
class _InvestigationProducerReceipt:
    """Exact ordinary writer which discharged one selected frontier goal."""

    projection_available: bool = False
    matching_writes: int = 0
    retained: bool = False
    receipt: InvestigationProducerReceipt | None = None

    @property
    def witnessed(self) -> bool:
        return self.receipt is not None


def _observe_investigation_producer(
    attempt: _ExecutedAttempt,
) -> _InvestigationProducerReceipt:
    """Receipt a frontier goal without weakening ordinary attempt gates."""

    selection = attempt.bearing.investigation_selection
    goal = selection.producer_goal if selection is not None else None
    if selection is None or goal is None or goal.identity != selection.producer_goal_id:
        return _InvestigationProducerReceipt()
    projection = attempt.projection_at(attempt.assertion_scan)
    if projection is None:
        return _InvestigationProducerReceipt()
    matches = tuple(
        write
        for write in projection.writes
        if write.rung_id == goal.rung_id
        and tuple(write.branch_path) == tuple(goal.branch_path)
        and write.transition.tag_name == goal.tag
        and _values_match(write.transition.to_value, goal.value)
    )
    retained = _values_match(attempt.pulse.snap.get(goal.tag), goal.value)
    receipt = (
        InvestigationProducerReceipt(
            frontier_id=selection.frontier_id,
            producer_goal_id=goal.identity,
            assertion_scan=attempt.assertion_scan,
            write_identity=_semantic_key(matches[0]),
            retained_assignment=(goal.tag, goal.value),
        )
        if len(matches) == 1 and retained
        else None
    )
    return _InvestigationProducerReceipt(
        projection_available=True,
        matching_writes=len(matches),
        retained=retained,
        receipt=receipt,
    )


def _observe_intrascan_act_occurrence(
    attempt: _ExecutedAttempt,
) -> _ProgramScanOccurrenceReceipt:
    """Read the exact stage occurrence without granting execution authority."""

    act = attempt.bearing.act
    if not isinstance(act, (ProgramScan, IntrascanPulse)):
        raise TypeError("intrascan occurrence observation requires a typed intrascan act")
    projection = attempt.projection_at(attempt.pulse.scan_before + 1)
    matching = (
        tuple(write for write in projection.writes if act.expected_write.matches(write))
        if projection is not None
        else ()
    )
    retained = _values_match(
        attempt.pulse.snap.get(act.expected_write.tag),
        act.expected_write.after,
    )
    receipt = (
        IntrascanActReceipt(
            evidence_identity=act.evidence_identity,
            kind="consumer" if isinstance(act, IntrascanPulse) else "stage",
            assertion_scan=attempt.pulse.scan_before + 1,
            expected_write_identity=_semantic_key(act.expected_write),
            matched_write_identity=_semantic_key(matching[0]),
            retained_assignment=(act.expected_write.tag, act.expected_write.after),
        )
        if len(matching) == 1 and retained
        else None
    )
    return _ProgramScanOccurrenceReceipt(
        projection_available=projection is not None,
        matching_writes=len(matching),
        retained=retained,
        receipt=receipt,
    )


@dataclass(frozen=True)
class _RouteBlockerCrossing:
    """One exact write that made a selected landing prerequisite false."""

    tag: str
    predicate: Any
    projection: Any
    write: Any


@dataclass(frozen=True)
class _TemporalSetupOccurrenceReceipt:
    """Exact requirement reads which establish one endpoint-invisible setup."""

    consumed_actions: tuple[_ActionPair, ...] = ()
    requirements_observed: bool = False
    observations: tuple[Any, ...] = ()


def _observe_exact_regression_prevention(
    attempt: _ExecutedAttempt,
    requirements: tuple[Any, ...],
    applied_actions: tuple[_ActionPair, ...],
    pilot_rungs: tuple[Any, ...],
) -> _TemporalSetupOccurrenceReceipt | None:
    """Prove the exact corrective rungs own their first installed scan.

    This is deliberately not a replay-equivalence check.  A corrective rung
    may make its owner complete sooner; later ordinary execution proves the
    resulting route.  The installation scan only has to show that every exact
    correction retained by the requirement is present in the executed overlay.
    It may legitimately be dormant on this scan: a self-guarded operation can
    wait for the program to clear its destination, then become effective at the
    next input boundary.
    """

    if not requirements or not all(
        getattr(requirement, "provenance", "").startswith("exact-regression-")
        and getattr(requirement, "obstruction_occurrence", None) is not None
        for requirement in requirements
    ):
        return None
    assertion_projection = attempt.projection_at(attempt.assertion_scan)
    if assertion_projection is None:
        return _TemporalSetupOccurrenceReceipt(
            observations=(("assertion-projection-unavailable",),)
        )

    installed = {_rung_identity(rung): rung for rung in pilot_rungs}
    overlay = _pilot_rung_execution_receipt(pilot_rungs, assertion_projection.entry_tags)
    effective = {_rung_identity(rung) for rung in overlay.effective}
    consumed: list[_ActionPair] = []
    observations: list[Any] = []
    for requirement in requirements:
        required = tuple(getattr(requirement, "corrective_pilot_rungs", ()))
        required_ids = tuple(_rung_identity(rung) for rung in required)
        owned = tuple(identity for identity in required_ids if identity in installed)
        active = tuple(identity for identity in required_ids if identity in effective)
        observations.append(("corrective-pilot-rungs", required_ids, owned, active))
        if not required_ids or len(owned) != len(required_ids):
            return _TemporalSetupOccurrenceReceipt(observations=tuple(observations))
        consumed.extend((rung.dest, rung.value) for rung in required)
    if tuple(consumed) != applied_actions:
        return _TemporalSetupOccurrenceReceipt(observations=tuple(observations))
    return _TemporalSetupOccurrenceReceipt(
        tuple(consumed),
        requirements_observed=True,
        observations=tuple(observations),
    )


def _observe_temporal_setup_occurrences(
    attempt: _ExecutedAttempt,
    requirements: tuple[Any, ...],
    applied_actions: tuple[_ActionPair, ...],
    ctx: Any,
    *,
    pilot_rungs: tuple[Any, ...] = (),
) -> _TemporalSetupOccurrenceReceipt:
    """Prove each setup action at its relocated demanding guard surface."""

    if not applied_actions or not requirements:
        return _TemporalSetupOccurrenceReceipt()
    regression = _observe_exact_regression_prevention(
        attempt,
        requirements,
        applied_actions,
        pilot_rungs,
    )
    if regression is not None:
        return regression
    assertion_projection = attempt.projection_at(attempt.assertion_scan)
    if assertion_projection is None:
        return _TemporalSetupOccurrenceReceipt(
            observations=(("assertion-projection-unavailable",),)
        )

    requirement_observations = []
    observation_receipts: list[Any] = []
    for requirement in requirements:
        projector = getattr(
            requirement.execution_owner,
            "pilot_rung_write_projection_at",
            None,
        )
        source_projection = (
            projector(requirement.deadline.scan_id) if projector is not None else None
        )
        if source_projection is None:
            observation_receipts.append(
                ("source-projection-unavailable", requirement.deadline.scan_id)
            )
            return _TemporalSetupOccurrenceReceipt(observations=tuple(observation_receipts))
        evidence = build_intrascan_requirement_evidence(
            requirement,
            source_projection,
            steerable=ctx.steerable,
            program_written=frozenset(ctx.pdg.writers_of),
            configured_inputs=ctx.configured_inputs,
        )
        observation = observe_intrascan_requirement(evidence, assertion_projection)
        requirement_observations.append(observation)
        observation_receipts.append(
            (
                observation.disposition.value,
                evidence.complete,
                evidence.detail,
                observation.detail,
                tuple((read.tag, read.values, read.rung) for read in observation.observed_reads),
            )
        )

    requirements_observed = all(
        observation.disposition is IntrascanRequirementDisposition.SATISFIED
        for observation in requirement_observations
    )
    consumed_actions: tuple[_ActionPair, ...] = ()
    if requirements_observed:
        observed_reads = tuple(
            read for observation in requirement_observations for read in observation.observed_reads
        )
        consumed_actions = tuple(
            (tag, value)
            for tag, value in applied_actions
            if any(
                read.tag == tag and len(read.values) == 1 and _values_match(read.values[0], value)
                for read in observed_reads
            )
        )
    return _TemporalSetupOccurrenceReceipt(
        consumed_actions,
        requirements_observed,
        tuple(observation_receipts),
    )


def _route_blocker_crossings(
    attempt: _ExecutedAttempt,
    frame: Any,
    ctx: Any,
    *,
    pilot_rungs: Any = (),
    resting: Any = None,
) -> tuple[_RouteBlockerCrossing, ...]:
    """Bind newly-false selected-route predicates to writes this act owns.

    A stable setup can reach its local channel boundary while its S1/S2 window
    makes a downstream anti-clobber condition false. The selected route names
    that condition; the ordered projection names the exact harmful write.
    Neither endpoint distance nor a speculative execution of the next steer is
    sufficient evidence on its own.
    """

    if ctx.target.predicate is not None or frame.tree.writer_rung is None:
        return ()
    result: list[_RouteBlockerCrossing] = []
    selected_writer = frame.tree.writer_rung
    for rung_index in sorted(ctx.pdg.writers_of.get(ctx.target.tag, frozenset())):
        if rung_index == selected_writer:
            continue
        rung_node = ctx.pdg.rung_nodes[rung_index]
        rung = resolve_rung(ctx.program, rung_node)
        if rung is None:
            continue
        written = _written_value_for_tag(rung, ctx.target.tag)
        if _can_produce(written, ctx.target.value):
            continue
        sp = rung.sp_tree()
        if sp is None:
            continue
        predicate = _sp_to_expr(sp)
        prospective_landing = project_pilot_overlay(
            {
                **attempt.pulse.snap,
                ctx.target.tag: ctx.target.value,
            },
            pilot_rungs,
            resting or {},
        )
        if _eval_expr_from_state(predicate, prospective_landing) is not True:
            continue
        crossings: list[_RouteBlockerCrossing] = []
        for scan_id in attempt.pulse.kernel_scan_ids:
            if not (attempt.pulse.scan_before < scan_id <= attempt.pulse.fork.state.scan_id):
                continue
            projection = attempt.projection_at(scan_id)
            if projection is None:
                continue
            rolling = dict(projection.entry_tags)
            for write in projection.writes:
                before = {**rolling, ctx.target.tag: ctx.target.value}
                rolling[write.transition.tag_name] = write.transition.to_value
                after = {**rolling, ctx.target.tag: ctx.target.value}
                if (
                    _eval_expr_from_state(predicate, before) is False
                    and _eval_expr_from_state(predicate, after) is True
                ):
                    crossings.append(
                        _RouteBlockerCrossing(
                            write.transition.tag_name,
                            predicate,
                            projection,
                            write,
                        )
                    )
        if len(crossings) == 1:
            result.append(crossings[0])
    return tuple(result)
