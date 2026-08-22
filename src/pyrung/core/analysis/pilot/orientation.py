"""Consider current-world readings and select one typed disposition.

Candidate instruments read the world once. WorkingTheory may first declare an
exact causal question; ordinary instruments then nominate acts through the
declared ``_ORDINARY_PROPOSERS`` precedence and one shared admission gate. An
actionless program scan requires a positive continuation reading. Otherwise
Orientation requests bounded evidence or returns ``Stuck``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace
from typing import Any

import pyrung.core.analysis.pilot.orientation_reading as _orientation_reading
import pyrung.core.analysis.pilot.theory_orientation as _theory_orientation
from pyrung.core.analysis.pilot.execution import (
    MotionKind,
)
from pyrung.core.analysis.pilot.navigation_contracts import (
    ActPolicy,
    ActSource,
    BatchPulse,
    Bearing,
    BearingCoast,
    ComposeCorrection,
    ExpectationExemption,
    NavigationConstraints,
    NeedIntrascanBoundaryRealization,
    NeedIntrascanTraceback,
    NeedProbe,
    NeedResearch,
    ObserveScan,
    OrientationRead,
    OrientationResult,
    OrientationWorld,
    ProgramContinuation,
    Pulse,
    Stuck,
    TargetSpec,
    act_identity,
)
from pyrung.core.analysis.pilot.options import read_candidates
from pyrung.core.analysis.pilot.working_theory import (
    TheoryTemporalIntent,
)
from pyrung.core.analysis.pilot.world_key import (
    wait_edge_nogood,
)

_ActProposal = tuple[Any, str]
_ActProposer = Callable[[Any, OrientationRead], Iterator[_ActProposal]]


def _awaited_action_proposals(_compass: Any, read: OrientationRead) -> Iterator[_ActProposal]:
    """Yield structural program handshakes before inferred program motion."""

    world = read.world
    candidates = read.candidates
    for option in candidates.options:
        if option.source is not ActSource.AWAITED_ACTION:
            continue
        if _theory_orientation._candidate_is_pending_configuration(option, world):
            continue
        applied = _theory_orientation._current_candidate_applied(option, candidates, world)
        yield (
            Pulse(_orientation_reading._pulse_policy(option, applied, world)),
            option.awaited_action_note or "program-awaited action",
        )


def _wait_proposals(compass: Any, read: OrientationRead) -> Iterator[_ActProposal]:
    """Yield the one selected chart/program wait when its edge remains open."""

    world = read.world
    candidates = read.candidates
    prescription = candidates.wait.prescription if candidates.wait is not None else None
    if prescription is None:
        return
    heading = prescription.heading
    route = heading.route if heading is not None else None
    wait_channel = (
        route.channel_tag
        if route is not None
        else (heading.channel_tag if heading is not None else None)
    )
    wait_nogood = (
        wait_edge_nogood(
            wait_channel,
            route.from_value if route is not None else world.snapshot.get(wait_channel),
            route.target_value
            if route is not None
            else heading.target_value
            if heading is not None
            else None,
        )
        if wait_channel is not None
        else None
    )
    if wait_nogood is not None and wait_nogood in compass.knowledge.nogood_pairs(world.world_key):
        return
    expectation = prescription.expectation
    yield (
        BearingCoast(
            ActPolicy(
                source=ActSource.ROUTE if route is not None else ActSource.PROGRAM,
                nogood_pair=wait_nogood,
                heading=heading,
                motion=MotionKind.COAST_TO_BEARING,
                expectation=expectation,
                expectation_exemption=(
                    ExpectationExemption.UNRESOLVED_EFFECT if expectation is None else None
                ),
                landing_receipt_authority=prescription.landing_receipt_authority,
            )
        ),
        prescription.reason or "charted completion edge",
    )


def _learned_batch_proposals(_compass: Any, read: OrientationRead) -> Iterator[_ActProposal]:
    candidate = read.candidates.learned_batch
    if candidate is None:
        return
    expectation = candidate.expectation
    yield (
        BatchPulse(
            ActPolicy(
                source=ActSource.LEARNED_BATCH,
                action_pairs=candidate.actions,
                applied=candidate.actions,
                expectation=expectation,
                expectation_exemption=(
                    ExpectationExemption.UNRESOLVED_EFFECT if expectation is None else None
                ),
            )
        ),
        "learned joint transition",
    )


def _crossing_proposals(_compass: Any, read: OrientationRead) -> Iterator[_ActProposal]:
    for branch in read.candidates.crossing_batches:
        expectation = branch.expectation
        policy = ActPolicy(
            source=ActSource.CROSSING,
            action_pairs=branch.actions,
            applied=branch.actions,
            note=branch.reason,
            expectation=expectation,
            expectation_exemption=(
                ExpectationExemption.UNRESOLVED_EFFECT if expectation is None else None
            ),
        )
        fidelity = branch.fidelity
        act = (
            Pulse(policy, crossing=fidelity)
            if len(branch.actions) == 1
            else BatchPulse(policy, crossing=fidelity)
        )
        yield (
            act,
            branch.reason or "verify crossing proposal"
            if branch.proposed
            else "follow grouped reverse crossing",
        )


def _scalar_proposals(_compass: Any, read: OrientationRead) -> Iterator[_ActProposal]:
    world = read.world
    candidates = read.candidates
    for option in candidates.options:
        # Structural awaited actions already own the first precedence slot.
        if option.source is ActSource.AWAITED_ACTION:
            continue
        if _theory_orientation._candidate_is_pending_configuration(option, world):
            continue
        applied = _theory_orientation._current_candidate_applied(option, candidates, world)
        yield (
            Pulse(_orientation_reading._pulse_policy(option, applied, world)),
            option.awaited_action_note
            or getattr(option, "program_note", None)
            or ("static route edge" if option.source is ActSource.ROUTE else "")
            or ("learned transition" if option.source is ActSource.LEARNED_ACTION else "")
            or "ranked trace action",
        )


def _widening_proposals(_compass: Any, read: OrientationRead) -> Iterator[_ActProposal]:
    candidates = read.candidates
    active = candidates.trace.active_actions
    for width in range(2, len(active) + 1):
        actions = active[:width]
        expectation = next(
            (
                promised
                for artifact, promised in candidates.widening_expectations
                if artifact == actions
            ),
            None,
        )
        yield (
            BatchPulse(
                ActPolicy(
                    source=ActSource.WIDENING,
                    action_pairs=actions,
                    applied=actions,
                    expectation=expectation,
                    expectation_exemption=(
                        ExpectationExemption.UNRESOLVED_EFFECT if expectation is None else None
                    ),
                )
            ),
            f"widen trace context to {width} atomic actions",
        )


def _continuation_proposals(compass: Any, read: OrientationRead) -> Iterator[_ActProposal]:
    """Lower only positively-read program motion to its named bounded act."""

    continuation = read.candidates.continuation
    if continuation is None:
        return
    world = read.world
    if compass.knowledge.continuation_receipt(world.world_key) is None:
        mode = "seek"
        rationale = continuation.reason
    else:
        mode = "settle"
        rationale = f"{continuation.reason}; the first coast was already observed"
    act = ProgramContinuation(
        mode,
        ActPolicy(
            source=ActSource.PROGRAM,
            motion=MotionKind.COAST_HOLDING_WORLD,
            provenance=continuation.provenance,
            expectation_exemption=ExpectationExemption.UNRESOLVED_EFFECT,
        ),
    )
    yield act, rationale


_ORDINARY_PROPOSERS: tuple[_ActProposer, ...] = (
    _awaited_action_proposals,
    _wait_proposals,
    _learned_batch_proposals,
    _crossing_proposals,
    _scalar_proposals,
    _widening_proposals,
    _continuation_proposals,
)


def _ordinary_proposals(compass: Any, read: OrientationRead) -> Iterator[_ActProposal]:
    """Consider each current-world act source in one declared precedence."""

    for proposer in _ORDINARY_PROPOSERS:
        yield from proposer(compass, read)


def _admit_ordinary_proposal(
    compass: Any,
    read: OrientationRead,
    target: TargetSpec,
    proposal: _ActProposal,
) -> Bearing | None:
    """Apply ordinary setup, requirement, and whole-act admission exactly once."""

    act, rationale = proposal
    setup = _orientation_reading._activation_setup_act(act, read.world)
    if setup is not None:
        act = setup
        rationale = "establish selected ladder activation before rereading Compass"
    if not _theory_orientation._act_preserves_requirements(read.world, act):
        return None
    if compass.knowledge.act_is_nogood(read.world_key, act_identity(act)):
        return None
    return _orientation_reading._bearing(
        read,
        act,
        target=target,
        rationale=rationale,
    )


def _ordinary_disposition(
    compass: Any,
    read: OrientationRead,
    target: TargetSpec,
) -> OrientationResult:
    """Consider the current world's ordinary readings exactly once."""

    for proposal in _ordinary_proposals(compass, read):
        bearing = _admit_ordinary_proposal(compass, read, target, proposal)
        if bearing is not None:
            return bearing
    diagnosis = read.candidates.diagnosis
    reason = diagnosis.reason if diagnosis is not None else "all_rejected"
    return _orientation_reading._probe_or_stuck(compass, read, reason)


def _orient_read(
    compass: Any,
    world: OrientationWorld,
    target: TargetSpec,
    *,
    _allow_theory: bool = True,
) -> OrientationResult:
    """Materialize one alternative in act-precedence order.

    A selected wait is considered before learned batches and individual action
    options; widening, diagnosis, and positive continuation follow. Each exact
    act is checked against the current world's nogoods before it becomes a
    bearing.
    """

    if world.frame is None:
        raise ValueError("single-alternative orientation requires a complete frame")
    read = OrientationRead(
        world_key=world.world_key,
        world=world,
        candidates=read_candidates(world),
    )
    candidates = read.candidates

    # Boundary zero has no executed program scan to read yet.  Make that one
    # observation an ordinary Compass-selected bearing; its landing is always
    # reread before any target-relative judgment is made.
    execution_state = getattr(getattr(world.state, "work", None), "state", None)
    if (
        getattr(execution_state, "scan_id", None) == 0
        and getattr(world.state, "bootstrap_execution", None) is None
    ):
        return _orientation_reading._bearing(
            read,
            ObserveScan(),
            target=target,
            rationale="observe exactly one entry scan",
        )

    if not _allow_theory:
        return _ordinary_disposition(compass, read, target)

    ordinary_cache: OrientationResult | None = None

    def ordinary_result() -> OrientationResult:
        nonlocal ordinary_cache
        if ordinary_cache is None:
            ordinary_cache = _ordinary_disposition(compass, read, target)
        return ordinary_cache

    view = getattr(world.context, "theory_view", None)
    if _allow_theory:
        boundary_realization = _theory_orientation._theory_intrascan_boundary_realization(
            read,
        )
        if boundary_realization is not None:
            return boundary_realization
        if view is not None and view.temporal_intent in {
            TheoryTemporalIntent.RETRY_TOGETHER,
            TheoryTemporalIntent.RETRY_THROUGH_DEADLINE,
        }:
            scope = getattr(view, "investigation_scope", None)
            if (
                view.temporal_intent is TheoryTemporalIntent.RETRY_THROUGH_DEADLINE
                and scope is not None
                and getattr(scope, "transaction_rearmed", False)
            ):
                ordinary = ordinary_result()
                retry = _theory_orientation._theory_temporal_retry_bearing(
                    read,
                    target,
                    ordinary=ordinary if isinstance(ordinary, Bearing) else None,
                )
                if retry is not None:
                    return retry
            research = compass.conductivity_research(view)
            if research is not None and not _theory_orientation._untried_pending_theory_pairs(
                world,
                research,
            ):
                frontier = _orientation_reading._frontier(read)
                return NeedResearch(
                    world_key=world.world_key,
                    frontier=frontier,
                    request=research,
                    rationale=research.reason,
                    orientation=read,
                )
            completed_research = (
                compass.completed_conductivity_research(view)
                if getattr(view, "research_findings", ())
                else None
            )
            composition = _theory_orientation._theory_correction_composition(
                read,
                target,
                research_finding_identity=(
                    completed_research.identity if completed_research is not None else None
                ),
            )
            if composition is not None:
                return composition
            continuation_traceback = _theory_orientation._theory_intrascan_continuation_traceback(
                read,
            )
            if continuation_traceback is not None:
                return continuation_traceback
            stage = _theory_orientation._theory_intrascan_bearing(
                read,
                target,
            )
            if stage is not None:
                return stage
            frontier_stage = _theory_orientation._theory_intrascan_frontier_bearing(
                world,
                target,
                orient_read=_orient_read,
            )
            if frontier_stage is not None:
                return frontier_stage
            ordinary = ordinary_result()
            composed = _theory_orientation._theory_temporal_retry_bearing(
                read,
                target,
                ordinary=ordinary if isinstance(ordinary, Bearing) else None,
            )
            if composed is not None:
                return composed
            configured_scan = _theory_orientation._theory_pending_configuration_bearing(
                read,
                target,
            )
            if configured_scan is not None:
                return configured_scan
            return _orientation_reading._probe_or_stuck(
                compass,
                read,
                "temporal_retry_unresolved",
            )

        setup_first = view is not None and view.temporal_intent is TheoryTemporalIntent.SETUP_FIRST
        prescription = candidates.wait.prescription if candidates.wait is not None else None
        if setup_first and prescription is None:
            rearm = _theory_orientation._theory_rearm_bearing(read, target)
            if rearm is not None:
                return rearm

        if setup_first:
            composition = _theory_orientation._theory_correction_composition(
                read,
                target,
            )
            if composition is not None:
                return composition
            configured_scan = _theory_orientation._theory_pending_configuration_bearing(
                read,
                target,
            )
            if configured_scan is not None:
                return configured_scan

        theory_setup = _theory_orientation._theory_setup_bearing(read, target)
        continuation_traceback = _theory_orientation._theory_intrascan_continuation_traceback(
            read,
        )
        if continuation_traceback is not None:
            return continuation_traceback
        if theory_setup is not None:
            return theory_setup
        if setup_first and prescription is None:
            # SETUP_FIRST is sequential: establish/rearm the prerequisite in
            # one accepted scan, then steer afresh. Once no setup remains, the
            # original trigger is now a legitimate next transaction even
            # though its action identity is unchanged; the provisional tip is
            # what makes it new work rather than replay at the old source.
            stage = _theory_orientation._theory_intrascan_bearing(read, target)
            if stage is not None:
                return stage
            frontier_stage = _theory_orientation._theory_intrascan_frontier_bearing(
                world,
                target,
                orient_read=_orient_read,
            )
            if frontier_stage is not None:
                return frontier_stage
            ordinary_disposition = ordinary_result()
            ordinary = ordinary_disposition if isinstance(ordinary_disposition, Bearing) else None
            traceback = _theory_orientation._theory_setup_traceback(
                read,
                ordinary,
            )
            if traceback is not None:
                return traceback
            retry = _theory_orientation._theory_temporal_retry_bearing(
                read,
                target,
                ordinary=ordinary,
            )
            if retry is not None:
                return retry
            return _orientation_reading._probe_or_stuck(
                compass,
                read,
                "temporal_setup_unresolved",
            )

    return ordinary_result()


def _is_maintenance(result: OrientationResult) -> bool:
    """Whether a read has no concrete continuation and can only let time pass."""

    return isinstance(result, Bearing) and isinstance(result.act, ProgramContinuation)


def _read_group(
    compass: Any,
    worlds: tuple[OrientationWorld, ...],
    target: TargetSpec,
    *,
    maintenance_owns: bool = False,
) -> tuple[OrientationResult | None, tuple[OrientationResult, ...]]:
    """Read alternatives once under the caller's work-ownership disposition.

    Alternative order remains the trace reader's deterministic order. There is
    no cross-alternative score and no retained cursor. With
    ``maintenance_owns=True``, an open operation's first bearing wins even when
    it is bounded program-continuation maintenance. Fresh alternatives instead
    look past maintenance for a concrete bearing and use the first maintenance
    result only as their fallback.
    """

    results: list[OrientationResult] = []
    maintenance: OrientationResult | None = None
    for world in worlds:
        result = _orient_read(compass, world, target)
        results.append(result)
        if isinstance(
            result,
            Bearing
            | ComposeCorrection
            | NeedResearch
            | NeedIntrascanTraceback
            | NeedIntrascanBoundaryRealization,
        ):
            if maintenance_owns or not _is_maintenance(result):
                return result, tuple(results)
            if maintenance is None:
                maintenance = result
    return maintenance, tuple(results)


def _combined_nonbearing(results: tuple[OrientationResult, ...]) -> OrientationResult:
    """Return one complete probe/stop after every current alternative was read."""

    frontier = tuple(
        dict.fromkeys(pair for result in results for pair in getattr(result, "frontier", ()))
    )
    probe = next((result for result in results if isinstance(result, NeedProbe)), None)
    if probe is not None:
        return replace(
            probe,
            frontier=frontier,
            request=replace(probe.request, frontier=frontier),
        )
    stuck = next((result for result in results if isinstance(result, Stuck)), None)
    if stuck is None:
        raise RuntimeError("current-world alternatives produced no disposition")
    return replace(stuck, frontier=frontier)


def orient(
    compass: Any,
    world: OrientationWorld,
    target: TargetSpec,
    constraints: NavigationConstraints,
) -> OrientationResult:
    """Read all live work, choose its smallest continuation, and forget the read.

    An open operation is read before fresh work. Within that operation the
    ordinary single-read Orientation selects one act. No root alternative,
    suffix, score, or "next route" position survives the observation.
    """

    if world.context.compass is not compass:
        raise ValueError("orientation world is bound to a different Compass value")
    context_changes = {
        "target": target,
        "blocked_actions": constraints.blocked_actions,
        "avoid_pred": constraints.avoid_predicate,
    }
    # Orientation also serves narrow structural test/navigation contexts.
    # Preserve that protocol while passing requirements through every context
    # which declares the Phase-4 view explicitly.
    if hasattr(world.context, "active_requirements"):
        context_changes["active_requirements"] = constraints.active_requirements
    if hasattr(world.context, "theory_view"):
        context_changes["theory_view"] = constraints.theory_view
    if hasattr(world.context, "temporal_requirements"):
        context_changes["temporal_requirements"] = constraints.temporal_requirements
    if hasattr(world.context, "temporal_trigger_requirements"):
        context_changes["temporal_trigger_requirements"] = constraints.temporal_trigger_requirements
    if hasattr(world.context, "temporal_source_anchor"):
        context_changes["temporal_source_anchor"] = constraints.temporal_source_anchor
    if hasattr(world.context, "defer_program_input_receipts"):
        context_changes["defer_program_input_receipts"] = constraints.defer_program_input_receipts
    read_context = replace(world.context, **context_changes)
    seed = replace(world, context=read_context)
    worlds = (
        (seed,)
        if seed.frame is not None
        else _orientation_reading._read_worlds(seed, target, constraints)
    )
    open_worlds: list[OrientationWorld] = []
    fresh_worlds: list[OrientationWorld] = []
    for alternative in worlds:
        evidence = _theory_orientation._current_work_evidence(
            alternative.frame,
            alternative.state,
            alternative.root_route,
        )
        (open_worlds if evidence else fresh_worlds).append(alternative)

    # Live work owns the next move. If it has no concrete lever, its coast or
    # dwell is still maintenance of that operation. If every apparent open
    # residual yields no Bearing, the operation has closed; stale established
    # facts do not prevent a fresh current-world read.
    open_results: tuple[OrientationResult, ...] = ()
    if open_worlds:
        selected, open_results = _read_group(
            compass,
            tuple(open_worlds),
            target,
            maintenance_owns=True,
        )
        if selected is not None:
            return selected

    selected, fresh_results = _read_group(
        compass,
        tuple(fresh_worlds),
        target,
    )
    if selected is not None:
        return selected
    return _combined_nonbearing((*open_results, *fresh_results))
