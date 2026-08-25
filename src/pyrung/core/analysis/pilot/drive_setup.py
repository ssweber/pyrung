"""Shared runtime and static setup for one PILOT drive."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pyrung.core.analysis.pilot.target_route as _target_route
from pyrung.core.analysis.graph import RouteTaken
from pyrung.core.analysis.pilot.compass import Compass, NavigationCatalog
from pyrung.core.analysis.pilot.navigation_contracts import TargetSpec
from pyrung.core.analysis.pilot.overlay import fork_with_pilot_rungs
from pyrung.core.analysis.pilot.physical import install_harness
from pyrung.core.analysis.pilot.pipeline_graph import (
    detect_opaque_loop,
    detect_opaque_pipelines,
)
from pyrung.core.analysis.pilot.program_facts import (
    compute_edge_tags,
    compute_reference_constants,
    compute_resting_values,
)
from pyrung.core.analysis.pilot.requirement_evidence import _configured_input_names
from pyrung.core.analysis.pilot.trace_read import DomainPrior
from pyrung.core.analysis.pilot.types import _PilotContext
from pyrung.core.analysis.pilot.world_key import _StateKeyConfig
from pyrung.core.analysis.steerable import compute_clear_only, compute_steerable

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.analysis.pilot.evidence import PipelineRoles, TransitionEvidence
    from pyrung.core.analysis.pilot.pipeline_graph import StaticTransitionGraph
    from pyrung.core.runner import PLC

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DriveSetup:
    """Static/runtime preparation shared by every target driven on one PLC."""

    work: PLC
    program: Any
    pdg: ProgramGraph
    steerable: frozenset[str]
    edge_tags: set[str]
    resting: dict[str, Any]
    anchor_scan: int
    diag_snapshot: dict[str, Any]
    nd_domains: dict[str, tuple[Any, ...]] | None
    stateful_domains: dict[str, tuple[Any, ...]] | None
    key_config: _StateKeyConfig | None
    evidence: TransitionEvidence | None
    compass: Compass
    opaque_loop: frozenset[str]
    configured_inputs: frozenset[str]


@dataclass(frozen=True)
class ProverContext:
    """Best-effort static evidence shared by drive setup and target context."""

    nd_domains: dict[str, tuple[Any, ...]] | None = None
    stateful_domains: dict[str, tuple[Any, ...]] | None = None
    key_config: _StateKeyConfig | None = None
    evidence: TransitionEvidence | None = None


def _make_pilot_context(
    setup: DriveSetup,
    work: PLC,
    target_tag: str,
    target_value: Any,
    target_predicate: Any,
    *,
    max_scans: int,
    avoid_pred: Any,
    compass: Compass | None = None,
) -> _PilotContext:
    from pyrung.core.analysis.pilot.evidence import discover_chart_roles

    pipeline_roles = infer_opaque_pipeline_roles(
        setup.pdg,
        setup.program,
        setup.steerable,
        setup.opaque_loop,
        setup.evidence,
    )
    chart_roles = discover_chart_roles(
        setup.pdg,
        setup.program,
        setup.steerable,
        setup.opaque_loop,
        setup.evidence,
    )
    pipeline_internal_tags = frozenset(
        tag for role in pipeline_roles for tag in role.trace_internal_tags
    )
    prior_compass = setup.compass if compass is None else compass
    graphs = _build_static_transition_graphs(
        pipeline_roles,
        setup.pdg,
        setup.program,
        setup.steerable,
        setup.opaque_loop,
        setup.evidence,
    )
    chart_graphs = _build_static_transition_graphs(
        chart_roles,
        setup.pdg,
        setup.program,
        setup.steerable,
        setup.opaque_loop,
        setup.evidence,
    )
    compass = Compass(
        catalog=NavigationCatalog(
            slices=prior_compass.catalog.slices,
            graphs=graphs,
            chart_graphs=chart_graphs,
        ),
        knowledge=prior_compass.knowledge,
    )
    # Domain prior for trace's inequality resolution: nondeterministic domains
    # for free inputs, stateful domains for program-owned tags, and affine
    # func-deps for derived tags. All are receipts from the same ExploreContext.
    domain_prior = DomainPrior(
        nd_domains=setup.nd_domains,
        stateful_domains=setup.stateful_domains,
        func_deps=(setup.evidence.affine_projections() if setup.evidence is not None else None),
    )
    # Clear-only (ack-cleared momentary) command tags: a subset of ``steerable``
    # kept off prerequisite holds and off preferred init/reset writer selection.
    clear_only = compute_clear_only(
        setup.pdg,
        work._known_tags_by_name,
        setup.program,
    )
    return _PilotContext(
        target=TargetSpec(target_tag, target_value, target_predicate),
        pdg=setup.pdg,
        program=setup.program,
        steerable=setup.steerable,
        edge_tags=setup.edge_tags,
        clear_only=clear_only,
        resting=setup.resting,
        nd_domains=setup.nd_domains,
        domain_prior=domain_prior,
        evidence=setup.evidence,
        key_config=setup.key_config,
        compass=compass,
        opaque_loop=setup.opaque_loop,
        pipeline_roles=pipeline_roles,
        pipeline_internal_tags=pipeline_internal_tags,
        route=None,
        blocked_actions=frozenset(),
        max_scans=max_scans,
        avoid_pred=avoid_pred,
        configured_inputs=setup.configured_inputs,
        chart_roles=chart_roles,
    )


def prepare_drive(
    plc: PLC,
    *,
    unlink: list[str] | None,
) -> DriveSetup:
    """Build the shared program/runtime analysis for one public drive."""

    from pyrung.core.analysis.pdg import build_program_graph

    configured_inputs = _configured_input_names(plc)
    work = fork_with_pilot_rungs(plc, (), history_budget=math.inf)
    program = plc._program
    pdg = build_program_graph(program)
    harness_fb = install_harness(work, unlink=unlink)
    ref_consts = compute_reference_constants(pdg, program, work._known_tags_by_name)
    steerable = compute_steerable(pdg, work._known_tags_by_name, program) - harness_fb - ref_consts
    edge_tags = compute_edge_tags(pdg, program)
    resting = compute_resting_values(steerable, work._known_tags_by_name, pdg, program)
    diag_snapshot = dict(work.state.tags)
    prover = build_prover_context(
        program,
        diag_snapshot,
    )
    opaque_slices = detect_opaque_pipelines(pdg, program, steerable)
    return DriveSetup(
        work=work,
        program=program,
        pdg=pdg,
        steerable=steerable,
        edge_tags=edge_tags,
        resting=resting,
        anchor_scan=work.state.scan_id,
        diag_snapshot=diag_snapshot,
        nd_domains=prover.nd_domains,
        stateful_domains=prover.stateful_domains,
        key_config=prover.key_config,
        evidence=prover.evidence,
        compass=Compass(NavigationCatalog(slices=tuple(opaque_slices))),
        opaque_loop=detect_opaque_loop(pdg, program),
        configured_inputs=configured_inputs,
    )


def prepare_target_context(
    setup: DriveSetup,
    target_tag: str,
    target_value: Any,
    target_predicate: Any,
    *,
    max_scans: int,
    avoid_pred: Any,
    compass: Compass | None = None,
    work: PLC | None = None,
) -> tuple[_PilotContext, RouteTaken | None]:
    """Bind one target and its initial route report to a prepared drive."""

    target_work = setup.work if work is None else work
    route_taken = _target_route.prepare_target_route(
        target_work,
        target_tag,
        target_value,
        setup.pdg,
        setup.program,
        setup.steerable,
        setup.opaque_loop,
        target_predicate=target_predicate,
        avoid_pred=avoid_pred,
    )
    ctx = _make_pilot_context(
        setup,
        target_work,
        target_tag,
        target_value,
        target_predicate,
        max_scans=max_scans,
        avoid_pred=avoid_pred,
        compass=compass,
    )
    return ctx, route_taken


def infer_opaque_pipeline_roles(
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    opaque_loop: frozenset[str],
    evidence: TransitionEvidence | None,
) -> tuple[PipelineRoles, ...]:
    if not opaque_loop:
        return ()

    from pyrung.core.analysis.pilot.evidence import infer_pipeline_roles

    roles: list[PipelineRoles] = []
    for tag in sorted(opaque_loop):
        if evidence is not None and not evidence.is_stepping(tag):
            continue
        role = infer_pipeline_roles(tag, pdg, program, steerable, opaque_loop, evidence)
        if role.request_tags:
            roles.append(role)
    return tuple(roles)


def _build_static_transition_graphs(
    pipeline_roles: tuple[PipelineRoles, ...],
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    opaque_loop: frozenset[str],
    evidence: TransitionEvidence | None,
) -> tuple[StaticTransitionGraph, ...]:
    if not pipeline_roles:
        return ()
    from pyrung.core.analysis.pilot.pipeline_graph import build_static_transition_graphs

    return build_static_transition_graphs(
        pipeline_roles,
        pdg,
        program,
        steerable,
        opaque_loop,
        evidence,
    )


# ---------------------------------------------------------------------------
# Prover context — value domains + state key config
# ---------------------------------------------------------------------------


def build_prover_context(
    program: Any,
    snapshot: dict[str, Any],
) -> ProverContext:
    """Build prover context for value domains and state key projection.

    Fields are ``None`` on failure, so PILOT falls back to Bool-only probing,
    pivot-tag state keys, and local static evidence.
    """
    try:
        from dataclasses import replace as _replace

        from pyrung.circuitpy.codegen import compile_kernel as _compile_kernel
        from pyrung.core.analysis.pilot.evidence import build_transition_evidence
        from pyrung.core.analysis.prove import _build_explore_context
        from pyrung.core.analysis.prove.passes import _OptConfig
        from pyrung.core.analysis.prove.results import Intractable

        opt = _replace(_OptConfig(), domains_only=True)
        compiled = _compile_kernel(program, blockless=True, proof_metadata=True)
        ctx = _build_explore_context(
            program,
            _opt_config=opt,
            compiled=compiled,
            initial_state=snapshot,
            allow_partial=True,
        )
        if isinstance(ctx, Intractable):
            return ProverContext()
        nd = getattr(ctx, "nondeterministic_dims", None)
        stateful = getattr(ctx, "stateful_dims", None)
        evidence = build_transition_evidence(ctx)
        if nd:
            logger.info("pilot: nd_domains ready (%d dims)", len(nd))

        # Build state key config from ExploreContext.
        #
        # The pilot's macro-state key needs the *pre-elision* stateful set.
        # Elision drops scan-local registers because BFS enumerates inputs, so a
        # register that is a pure function of the inputs each scan is redundant in
        # the BFS key.  The pilot does the opposite — it *holds* inputs and
        # *observes* registers — so a scan-local channel (e.g. a config/mode
        # register decoded from a command) is the observable proxy for its own
        # steering; dropping it makes an establish move (change the channel) read
        # as SPIN.  Restore the elided tags, appended after the originals so the
        # done/threshold spec indices (which point into the original positions)
        # stay valid.
        stateful_names = ctx.stateful_names + tuple(
            sorted(set(ctx.elided_tags) - set(ctx.stateful_names))
        )
        done_specs = ctx.state_key_done_specs
        threshold_vector_specs = ctx.threshold_vector_specs

        acc_names: set[str] = set()
        for spec in done_specs:
            acc_names.add(spec.acc_name)
        for spec in threshold_vector_specs:
            acc_names.add(spec.acc_name)
        acc_indices = frozenset(i for i, name in enumerate(stateful_names) if name in acc_names)

        if not stateful_names:
            logger.info("pilot: stateful_names empty, falling back to pivot_tags")
            return ProverContext(
                nd_domains=nd,
                stateful_domains=stateful,
                evidence=evidence,
            )

        key_config = _StateKeyConfig(
            stateful_names=stateful_names,
            done_specs=done_specs,
            threshold_vector_specs=threshold_vector_specs,
            acc_indices=acc_indices,
        )
        logger.info(
            "pilot: state key ready (%d dims, %d done, %d threshold, %d acc masked)",
            len(stateful_names),
            len(done_specs),
            len(threshold_vector_specs),
            len(acc_indices),
        )
        return ProverContext(
            nd_domains=nd,
            stateful_domains=stateful,
            key_config=key_config,
            evidence=evidence,
        )
    except Exception:  # noqa: BLE001
        logger.debug("pilot: context build failed", exc_info=True)
        return ProverContext()
