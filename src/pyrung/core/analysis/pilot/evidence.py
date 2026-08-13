"""Infer transition-pipeline roles and expand static transition routes.

This module combines PDG writer analysis with the functional dependencies,
elided tags, and stepping tags already established by ``ExploreContext``. It
canonicalizes pipeline aliases, identifies channel, action, stepping, and
internal tags, and expands ingress paths into ``TransitionRoute`` values with
separate source constraints and enablers.

``pilot._infer_pipeline_roles_for_context`` owns the legacy opaque admission:
it visits opaque-loop tags and retains only roles with request tags. Static
chart discovery is separate: :func:`discover_chart_roles` visits
prover-confirmed stepping tags, but folds one-way copied projections into the
structural carrier they observe. ``infer_pipeline_roles`` owns the common role
partition.

The result is static evidence consumed by trace and graph construction; no
program execution or runtime transition learning occurs here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pilot.static_expressions import (
    _channel_from_values,
)
from pyrung.core.analysis.pilot.tide_tables import _read_table, table_operand_from_copy

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph

logger = logging.getLogger(__name__)
_MISSING = object()


@dataclass(frozen=True)
class CanonicalForm:
    """Affine relation: tag = representative * scale + offset."""

    representative: str
    scale: int | float
    offset: int | float


@dataclass(frozen=True)
class TransitionRoute:
    """One statically-expanded ingress path for a target register.

    ``destination_value`` is ``None`` when the pipeline is opaque
    (indirect copy) and the final value cannot be determined statically.
    ``request_tag`` is set when the route goes through an intermediate
    pipeline register (e.g. ``S_StateRequested``).

    ``from_values`` are the channel-register values this route fires *from*,
    taken straight off the writer's own condition — including the **disjunction**
    (``Or(StateCurrent==STOPPED, ==COMPLETED)``) that ``source_constraints`` drops
    because ``_partition_conditions`` keeps only single-valued gates.  The compass
    value graph fans one edge per from-value, so every source arm of a
    command-gated hop remains charted. ``edge_gates`` are the steerable rise/fall tags
    that gate the writer (typically at the call site — ``rise(CmdChgRequest)``):
    co-actions that must fire *in the same scan* as the command, as
    ``(tag, level)`` where ``level`` is the post-edge value (``True`` for rise).
    """

    destination_tag: str
    destination_value: Any
    request_tag: str | None
    request_value: Any
    source_constraints: tuple[tuple[str, Any], ...]
    enablers: tuple[tuple[str, Any], ...]
    action_tags: frozenset[str]
    writer_node: int
    writer_subroutine: str | None
    call_site_gates: tuple[tuple[str, Any], ...]
    consumer_node: int | None = None
    from_values: tuple[Any, ...] = ()
    edge_gates: tuple[tuple[str, bool], ...] = ()

    @property
    def writer_effect(self) -> tuple[str, Any]:
        """Return the exact effect owned by ``writer_node``.

        Direct-route writers own the channel destination. Pipeline-route
        writers own the intermediate request; a later transfer owns the
        channel landing. A receipt must preserve that distinction.
        """

        if self.request_tag is not None:
            return self.request_tag, self.request_value
        return self.destination_tag, self.destination_value


@dataclass(frozen=True)
class PipelineRoles:
    """Generic roles inferred around one navigated structural carrier.

    ``channel_tag`` is the register being navigated. ``request_tags`` are
    actuation handoffs into that carrier. ``observation_tags`` are one-way
    copied projections of the carrier: useful receipt boundaries, but not
    independent routes. A PLC can have any number of carrier roles and any
    number of either handoff kind; none of these imply a global inner/outer
    nesting.  ``guard_internal_tags`` and ``scratch_internal_tags`` are
    implementation details of the writer pipeline and should not be
    recursively pursued as independent goals.
    """

    channel_tag: str
    request_tags: frozenset[str] = frozenset()
    observation_tags: frozenset[str] = frozenset()
    guard_internal_tags: frozenset[str] = frozenset()
    scratch_internal_tags: frozenset[str] = frozenset()

    @property
    def trace_internal_tags(self) -> frozenset[str]:
        return self.guard_internal_tags | self.scratch_internal_tags

    @property
    def participating_tags(self) -> frozenset[str]:
        return (
            frozenset((self.channel_tag,))
            | self.request_tags
            | self.guard_internal_tags
            | self.scratch_internal_tags
        )


# ---------------------------------------------------------------------------
# Static route expansion (works on any program, no ExploreContext needed)
# ---------------------------------------------------------------------------


def expand_routes(
    target_tag: str,
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    opaque_loop: frozenset[str],
    evidence: TransitionEvidence | None = None,
) -> list[TransitionRoute]:
    """Enumerate all static ingress paths into *target_tag*'s writer pipeline.

    Phase 1 scans writers of *target_tag* and classifies them:

    - **Literal** writers set the target to a constant — these become
      direct routes and also populate a ``dest_map`` for pipeline routes.
    - **Affine** copy-through writers (``copy(request, target)``) identify
      request tags whose own writers are enumerated in Phase 2.
    - **UNKNOWN** indirect-copy writers are expanded when their pointer can be
      canonicalized to a request tag through ExploreContext functional deps or
      a local single-calc definition.

    Phase 2 enumerates writers of each discovered request tag and builds
    pipeline routes, resolving destination values via the dest_map or
    Affine passthrough.
    """
    from pyrung.core.analysis.pdg import resolve_rung
    from pyrung.core.analysis.simplified import _sp_to_expr
    from pyrung.core.analysis.sp_values import (
        _extract_condition_values,
        _written_value_for_tag,
    )
    from pyrung.core.crossing import (
        UNKNOWN,
        Affine,
        Aggregate,
        Literal,
        evaluate_forward,
    )

    writer_nodes = sorted(pdg.writers_of.get(target_tag, frozenset()))
    if not writer_nodes:
        return []

    source_tags = {target_tag} | opaque_loop
    source_aliases = _source_aliases(target_tag, pdg, program)

    # Phase 1 ---------------------------------------------------------------
    direct_routes: list[TransitionRoute] = []
    request_tags: set[str] = set()
    # {candidate_request_tag: {request_value: destination_value}}
    dest_maps: dict[str, dict[Any, Any]] = {}
    affine_transforms: dict[str, list[Affine]] = {}
    indirect_sources: dict[str, list[Any]] = {}
    transfer_nodes: dict[str, list[int]] = {}

    for node_idx in writer_nodes:
        node = pdg.rung_nodes[node_idx]
        rung_obj = resolve_rung(program, node)
        if rung_obj is None:
            continue

        written = _written_value_for_tag(rung_obj, target_tag)
        sp = rung_obj.sp_tree()
        cond_expr = _sp_to_expr(sp) if sp is not None else None
        cond_values = _extract_condition_values(cond_expr) if cond_expr is not None else {}

        if isinstance(written, Literal):
            # Populate dest_map: condition tag value → destination value
            for cond_tag, cond_vals in cond_values.items():
                if len(cond_vals) == 1:
                    dest_maps.setdefault(cond_tag, {})[next(iter(cond_vals))] = written.value

            call_gates = _call_site_conditions(node, pdg, program)
            route_conds = _merge_condition_values(cond_values, call_gates)
            source_c, enabler_c, action_t = _partition_conditions(
                route_conds,
                source_tags,
                steerable,
                source_aliases,
            )
            direct_routes.append(
                TransitionRoute(
                    destination_tag=target_tag,
                    destination_value=written.value,
                    request_tag=None,
                    request_value=None,
                    source_constraints=source_c,
                    enablers=enabler_c,
                    action_tags=action_t,
                    writer_node=node_idx,
                    writer_subroutine=node.subroutine,
                    call_site_gates=call_gates,
                    from_values=_channel_from_values(cond_expr, target_tag, source_aliases),
                    edge_gates=_route_edge_gates(node, pdg, program, steerable),
                )
            )

        elif isinstance(written, Affine):
            request_tags.add(written.source)
            affine_transforms.setdefault(written.source, []).append(written)
            transfer_nodes.setdefault(written.source, []).append(node_idx)

        elif written is UNKNOWN:
            indirect = table_operand_from_copy(
                rung_obj,
                target_tag,
                {},
                pdg,
                program,
                evidence=evidence,
                single_mutable_index=False,
                live_snapshot=False,
                strict_hop_budget=False,
            )
            if indirect is not None and indirect.index_tag != target_tag:
                request_tags.add(indirect.index_tag)
                indirect_sources.setdefault(indirect.index_tag, []).append(indirect)
                transfer_nodes.setdefault(indirect.index_tag, []).append(node_idx)

        elif isinstance(written, Aggregate):
            # Honest punt: an aggregate writer produces a runtime sum/count over
            # a block, so its destination value is not statically knowable.
            # Route expansion is *value navigation* — a route needs a concrete
            # destination (Literal) to seed the compass value-graph or an affine
            # passthrough (Affine) to a request tag; an aggregate offers neither.
            # trace's ``_can_produce`` still admits an aggregate as
            # maybe-producible for backward preservation and ``_decompose_sum``
            # traces it element-wise, but that is *backward* tracing, not the
            # static value-jump graph this function feeds.  Drop it as a route
            # source rather than fabricate an edge with an unknown value.
            continue

    # Phase 2 ---------------------------------------------------------------
    pipeline_routes: list[TransitionRoute] = []
    for request_tag in sorted(request_tags):
        dest_map = dest_maps.get(request_tag, {})
        req_writer_nodes = sorted(
            pdg.writers_of.get(request_tag, frozenset()),
        )

        for req_node_idx in req_writer_nodes:
            req_node = pdg.rung_nodes[req_node_idx]

            # Transfer/clearing rungs also write the destination — skip
            if target_tag in req_node.writes:
                continue

            req_rung = resolve_rung(program, req_node)
            if req_rung is None:
                continue

            req_written = _written_value_for_tag(req_rung, request_tag)

            # Skip self-referential (copies destination into request)
            if isinstance(req_written, Affine) and req_written.source == target_tag:
                continue

            req_sp = req_rung.sp_tree()
            req_expr = _sp_to_expr(req_sp) if req_sp is not None else None
            req_conds = _extract_condition_values(req_expr) if req_expr is not None else {}

            req_value: Any = None
            dest_value: Any = None
            req_value_known, static_req_value = _static_request_value(req_written, pdg)
            if req_value_known:
                req_value = static_req_value
                # Resolve via Literal target writers, else Affine passthrough
                dest_value = dest_map.get(req_value, _MISSING)
                if dest_value is _MISSING:
                    invalid = object()
                    dest_value = None
                    for source in indirect_sources.get(request_tag, ()):
                        read = _read_table(
                            source,
                            req_value,
                            {},
                            coerce_index=True,
                            invalid=invalid,
                        )
                        if read is not invalid:
                            dest_value = read
                            break
                if dest_value is None:
                    transformed = {
                        result
                        for claim in affine_transforms.get(request_tag, ())
                        if (result := evaluate_forward(claim, {request_tag: req_value}))
                        is not UNKNOWN
                    }
                    if len(transformed) == 1:
                        dest_value = transformed.pop()

            call_gates = _call_site_conditions(req_node, pdg, program)
            route_conds = _merge_condition_values(req_conds, call_gates)
            source_c, enabler_c, action_t = _partition_conditions(
                route_conds,
                source_tags,
                steerable,
                source_aliases,
            )

            pipeline_routes.append(
                TransitionRoute(
                    destination_tag=target_tag,
                    destination_value=dest_value,
                    request_tag=request_tag,
                    request_value=req_value,
                    source_constraints=source_c,
                    enablers=enabler_c,
                    action_tags=action_t,
                    writer_node=req_node_idx,
                    writer_subroutine=req_node.subroutine,
                    call_site_gates=call_gates,
                    consumer_node=(
                        transfer_nodes[request_tag][0]
                        if len(transfer_nodes.get(request_tag, ())) == 1
                        else None
                    ),
                    from_values=_channel_from_values(req_expr, target_tag, source_aliases),
                    edge_gates=_route_edge_gates(req_node, pdg, program, steerable),
                )
            )

    all_routes = [*direct_routes, *pipeline_routes]
    if all_routes:
        logger.info(
            "evidence: %d routes for %s (%d direct, %d pipeline)",
            len(all_routes),
            target_tag,
            len(direct_routes),
            len(pipeline_routes),
        )
    return all_routes


def _static_request_value(written: Any, pdg: ProgramGraph) -> tuple[bool, Any]:
    """Resolve a request writer's static value when it is literal or tag-copy."""
    from pyrung.core.crossing import UNKNOWN, Affine, Literal, evaluate_forward

    if isinstance(written, Literal):
        return True, written.value
    if isinstance(written, Affine):
        tag = pdg.tags.get(written.source)
        if tag is None:
            return False, None
        produced = evaluate_forward(written, {written.source: tag.default})
        return (False, None) if produced is UNKNOWN else (True, produced)
    return False, None


def _source_aliases(
    target_tag: str,
    pdg: ProgramGraph,
    program: Any,
) -> dict[tuple[str, Any], tuple[str, Any]]:
    """Derived predicates that mean a source-register value.

    Example shape: ``with rung(StateCurrent == 3): out(S_Starting)`` means a
    later condition ``S_Starting=True`` is source context for
    ``StateCurrent=3`` rather than a subgoal to cause.

    Pure in ``(target_tag, pdg, program)`` — all stable across a pilot run — but
    ``expand_routes`` calls it once per iteration, so the result is memoized on
    the program graph keyed by *target_tag*.
    """
    cache = getattr(pdg, "_source_aliases_cache", None)
    if cache is None:
        cache = {}
        object.__setattr__(pdg, "_source_aliases_cache", cache)
    cached = cache.get(target_tag)
    if cached is not None:
        return cached

    from pyrung.core.analysis.sp_values import writer_value_facts

    # Project the shared writer-alias primitive by *target_tag*: a candidate
    # writer aliases ``target_tag == v`` when its gate constrains target_tag to a
    # single value ``v``.  The primitive already screens to combinational writers
    # (OTE bit coils + constant copies) and carries their invertible gate values.
    aliases: dict[tuple[str, Any], tuple[str, Any]] = {}
    for candidate_tag, cand_facts in writer_value_facts(program, pdg).items():
        if candidate_tag == target_tag:
            continue
        for fact in cand_facts:
            target_values = fact.cond_values.get(target_tag)
            if target_values is None or len(target_values) != 1:
                continue
            aliases[(candidate_tag, fact.written_value)] = (
                target_tag,
                next(iter(target_values)),
            )
    cache[target_tag] = aliases
    return aliases


def _merge_condition_values(
    cond_values: dict[str, frozenset[Any]],
    pairs: tuple[tuple[str, Any], ...],
) -> dict[str, frozenset[Any]]:
    """Merge singleton gate pairs into extracted condition-value sets."""
    if not pairs:
        return cond_values
    merged = {tag: frozenset(values) for tag, values in cond_values.items()}
    for tag, value in pairs:
        merged[tag] = merged.get(tag, frozenset()) | frozenset((value,))
    return merged


def _partition_conditions(
    cond_values: dict[str, frozenset[Any]],
    source_tags: set[str],
    steerable: frozenset[str],
    source_aliases: dict[tuple[str, Any], tuple[str, Any]],
) -> tuple[
    tuple[tuple[str, Any], ...],
    tuple[tuple[str, Any], ...],
    frozenset[str],
]:
    """Partition extracted condition values into source constraints vs enablers.

    Source constraints are conditions on *source_tags* (the target register
    itself or opaque-loop peers) — they identify which state the transition
    fires from, not something to cause.
    """
    source: list[tuple[str, Any]] = []
    enablers: list[tuple[str, Any]] = []
    actions: set[str] = set()

    for tag, values in sorted(cond_values.items()):
        if len(values) != 1:
            continue
        value = next(iter(values))
        alias = source_aliases.get((tag, value))
        if alias is not None:
            source.append(alias)
            continue

        if tag in source_tags:
            source.append((tag, value))
        else:
            enablers.append((tag, value))
            if tag in steerable:
                actions.add(tag)

    return tuple(source), tuple(enablers), frozenset(actions)


def _call_site_nodes(node: Any, pdg: ProgramGraph) -> tuple[Any, ...]:
    """Main-routine PDG nodes that call the writer node's subroutine."""

    if node.subroutine is None:
        return ()
    call_sites = pdg.call_site_rung_indices().get(node.subroutine, frozenset())
    main_by_rung: dict[int, Any] = {}
    for candidate in pdg.rung_nodes:
        if candidate.subroutine is None and not candidate.branch_path:
            main_by_rung.setdefault(candidate.rung_index, candidate)
    return tuple(
        main_by_rung[rung_index] for rung_index in sorted(call_sites) if rung_index in main_by_rung
    )


def _call_site_conditions(
    node: Any,
    pdg: ProgramGraph,
    program: Any,
) -> tuple[tuple[str, Any], ...]:
    """Extract conditions from call-site rungs for subroutine writers."""
    if node.subroutine is None:
        return ()

    from pyrung.core.analysis.pdg import resolve_rung
    from pyrung.core.analysis.simplified import _sp_to_expr
    from pyrung.core.analysis.sp_values import _extract_condition_values

    gates: list[tuple[str, Any]] = []

    for call_site in _call_site_nodes(node, pdg):
        cs_rung = resolve_rung(program, call_site)
        if cs_rung is None:
            continue
        cs_sp = cs_rung.sp_tree()
        if cs_sp is None:
            continue
        cs_conds = _extract_condition_values(_sp_to_expr(cs_sp))
        for tag, values in sorted(cs_conds.items()):
            if len(values) == 1:
                gates.append((tag, next(iter(values))))

    return tuple(gates)


def selected_chart_producer_guard_rungs(
    edge: Any,
    pdg: ProgramGraph,
    program: Any,
) -> tuple[Any, ...] | None:
    """Return the complete uniquely-owned guard surface for one chart edge.

    A main-routine producer owns only its rung guard. A subroutine producer
    also needs one exact caller guard. Multiple or unavailable callers cannot
    identify one dynamic producer occurrence and therefore fail closed.
    """

    from pyrung.core.analysis.pdg import resolve_rung

    node = pdg.rung_nodes[edge.route.writer_node]
    writer = resolve_rung(program, node)
    if writer is None:
        return None
    if node.subroutine is None:
        return (writer,)
    callers = tuple(
        caller
        for caller_node in _call_site_nodes(node, pdg)
        for caller in (resolve_rung(program, caller_node),)
        if caller is not None
    )
    if len(callers) != 1:
        return None
    return (writer, callers[0])


def _route_edge_gates(
    node: Any,
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
) -> tuple[tuple[str, bool], ...]:
    """Steerable rise/fall tags gating *node* — its condition and its call sites.

    A command pulse is gated by a one-shot edge (``rise(CmdChgRequest)``), almost
    always at the *call site* of the writer's subroutine rather than the writer
    rung itself.  ``_extract_condition_values`` drops edge atoms, so these gates
    never reach the route's enablers; recover them here as ``(tag, level)``
    co-actions (``level`` is the post-edge value: ``True`` for rise, ``False`` for
    fall) that must fire in the same scan as the command.
    """
    from pyrung.core.analysis.pdg import resolve_rung
    from pyrung.core.analysis.simplified import And, Atom, Or, _sp_to_expr

    gates: set[tuple[str, bool]] = set()

    def visit(expr: Any) -> None:
        if isinstance(expr, Atom):
            if expr.form in ("rise", "fall") and expr.tag in steerable:
                gates.add((expr.tag, expr.form == "rise"))
        elif isinstance(expr, (And, Or)):
            for term in expr.terms:
                visit(term)

    def visit_node(n: Any) -> None:
        ro = resolve_rung(program, n)
        if ro is None:
            return
        sp = ro.sp_tree()
        if sp is not None:
            visit(_sp_to_expr(sp))

    visit_node(node)

    for call_site in _call_site_nodes(node, pdg):
        visit_node(call_site)

    return tuple(sorted(gates))


# ---------------------------------------------------------------------------
# Pipeline role inference
# ---------------------------------------------------------------------------


def infer_pipeline_roles(
    target_tag: str,
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    opaque_loop: frozenset[str],
    evidence: TransitionEvidence | None = None,
) -> PipelineRoles:
    """Infer generic roles for a transition pipeline writing *target_tag*.

    This is intentionally structural. It does not know state machines, commands,
    or burner-specific names. Expanded routes identify request tags. A guard is
    pipeline-internal only when it participates in a route or target-writer
    condition and all of its readers are target-writer rungs. Request tags stay
    traceable because their writers often reveal meaningful transition causes;
    ``TransitionEvidence`` supplies the functionally dependent and elided
    scratch tags hidden behind them.
    """

    routes = expand_routes(target_tag, pdg, program, steerable, opaque_loop, evidence)
    request_tags = frozenset(route.request_tag for route in routes if route.request_tag is not None)
    target_writers = frozenset(pdg.writers_of.get(target_tag, frozenset()))
    guard_candidates: set[str] = set()

    for route in routes:
        pairs = (
            *route.source_constraints,
            *route.enablers,
            *route.call_site_gates,
        )
        for tag, _value in pairs:
            if tag == target_tag or tag in request_tags or tag in steerable:
                continue
            if _is_pipeline_local_guard(tag, target_writers, pdg):
                guard_candidates.add(tag)

    for tag in _target_writer_condition_tags(target_writers, pdg, program):
        if tag == target_tag or tag in request_tags or tag in steerable:
            continue
        if _is_pipeline_local_guard(tag, target_writers, pdg):
            guard_candidates.add(tag)

    scratch = _pipeline_scratch_tags(evidence, request_tags | guard_candidates)
    return PipelineRoles(
        channel_tag=target_tag,
        request_tags=request_tags,
        guard_internal_tags=frozenset(guard_candidates),
        scratch_internal_tags=scratch,
    )


def discover_chart_roles(
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    opaque_loop: frozenset[str],
    evidence: TransitionEvidence | None,
) -> tuple[PipelineRoles, ...]:
    """Discover deterministic read-only structural carriers from prover evidence.

    The prover's stepping classification is the admission receipt. Missing
    evidence therefore fails closed. Stepping is intentionally broader than
    navigation: it propagates through plain copies so state-space analysis can
    retain projections. A one-way copied projection is therefore attached to
    its source carrier as an observation handoff instead of becoming a second
    route. A copy that participates in feedback is not folded: request/current
    pipelines remain genuine structural carriers. This catalog is deliberately
    separate from ``pipeline_roles`` because only the latter defines Trace
    opacity and internal tags.
    """

    if evidence is None:
        return ()

    stepping = frozenset(evidence.stepping_tags())
    projections = {
        tag: source
        for tag in stepping
        if (source := _one_way_step_projection_source(tag, stepping, pdg, program)) is not None
    }

    def carrier(tag: str) -> str:
        seen: set[str] = set()
        while tag in projections and tag not in seen:
            seen.add(tag)
            tag = projections[tag]
        return tag

    observations: dict[str, set[str]] = {}
    for projection in projections:
        observations.setdefault(carrier(projection), set()).add(projection)

    roles: list[PipelineRoles] = []
    seen: set[tuple[Any, ...]] = set()
    for tag in evidence.stepping_tags():
        if tag in projections:
            continue
        role = infer_pipeline_roles(tag, pdg, program, steerable, opaque_loop, evidence)
        role = PipelineRoles(
            channel_tag=role.channel_tag,
            request_tags=role.request_tags,
            observation_tags=frozenset(observations.get(tag, ())),
            guard_internal_tags=role.guard_internal_tags,
            scratch_internal_tags=role.scratch_internal_tags,
        )
        identity = (
            role.channel_tag,
            tuple(sorted(role.request_tags)),
            tuple(sorted(role.observation_tags)),
            tuple(sorted(role.guard_internal_tags)),
            tuple(sorted(role.scratch_internal_tags)),
        )
        if identity in seen:
            continue
        seen.add(identity)
        roles.append(role)
    return tuple(roles)


def _one_way_step_projection_source(
    tag: str,
    stepping: frozenset[str],
    pdg: ProgramGraph,
    program: Any,
) -> str | None:
    """Return the sole copied stepping source when *tag* is only its projection.

    Copy-propagated stepping is not by itself channel ownership.  All writers
    must be affine copies of the same stepping source, and the destination must
    not influence that source again.  The feedback check is what preserves a
    real request/current pipeline while classifying an unconditional reporting
    copy as an observation handoff.
    """

    from pyrung.core.analysis.pdg import resolve_rung
    from pyrung.core.analysis.sp_values import _written_value_for_tag
    from pyrung.core.crossing import Affine

    writers = tuple(sorted(pdg.writers_of.get(tag, frozenset())))
    if not writers:
        return None

    sources: set[str] = set()
    for node_idx in writers:
        rung_obj = resolve_rung(program, pdg.rung_nodes[node_idx])
        written = _written_value_for_tag(rung_obj, tag)
        if not isinstance(written, Affine) or written.source == tag:
            return None
        sources.add(written.source)
    if len(sources) != 1:
        return None
    source = next(iter(sources))
    if source not in stepping:
        return None
    if source in pdg.downstream_slice(tag, follow_calls=True):
        return None
    return source


def _target_writer_condition_tags(
    writer_nodes: frozenset[int],
    pdg: ProgramGraph,
    program: Any,
) -> frozenset[str]:
    from pyrung.core.analysis.pdg import resolve_rung
    from pyrung.core.analysis.simplified import _sp_to_expr
    from pyrung.core.analysis.sp_values import _extract_condition_values

    tags: set[str] = set()
    for node_idx in sorted(writer_nodes):
        node = pdg.rung_nodes[node_idx]
        rung_obj = resolve_rung(program, node)
        if rung_obj is None:
            continue
        sp = rung_obj.sp_tree()
        if sp is not None:
            tags.update(_extract_condition_values(_sp_to_expr(sp)))
        for tag, _value in _call_site_conditions(node, pdg, program):
            tags.add(tag)
    return frozenset(tags)


def _is_pipeline_local_guard(
    tag: str,
    target_writers: frozenset[int],
    pdg: ProgramGraph,
) -> bool:
    readers = frozenset(pdg.readers_of.get(tag, frozenset()))
    return bool(readers) and readers <= target_writers


def _pipeline_scratch_tags(
    evidence: TransitionEvidence | None,
    representatives: frozenset[str],
) -> frozenset[str]:
    if evidence is None or not representatives:
        return frozenset()

    scratch: set[str] = set()
    for tag, canonical in evidence.functional_dependencies().items():
        if canonical.representative in representatives:
            scratch.add(tag)
    scratch.update(evidence.elided_tags() & representatives)
    return frozenset(scratch)


# ---------------------------------------------------------------------------
# ExploreContext-enhanced classification (optional layer)
# ---------------------------------------------------------------------------


class TransitionEvidence:
    """ExploreContext facts consumed by PILOT's compass.

    Functional dependencies support tag canonicalization and affine projection
    lookup. Elided tags identify scan-local/internal facts, while stepping tags
    identify dimensions known to visit multiple values.
    """

    def __init__(
        self,
        *,
        functional_deps: dict[str, tuple[str, int, int | float]],
        elided: frozenset[str],
        stepping: frozenset[str],
    ) -> None:
        self._functional_deps = functional_deps
        self._elided = elided
        self._stepping = stepping

    def canonicalize(self, tag: str) -> CanonicalForm | None:
        """If *tag* is a functional dep projection, return its canonical form."""
        entry = self._functional_deps.get(tag)
        if entry is None:
            return None
        rep, scale, offset = entry
        return CanonicalForm(representative=rep, scale=scale, offset=offset)

    def functional_dependencies(self) -> dict[str, CanonicalForm]:
        """Canonical forms for functional-dep projection tags."""
        return {
            tag: CanonicalForm(representative=rep, scale=scale, offset=offset)
            for tag, (rep, scale, offset) in self._functional_deps.items()
        }

    def affine_projections(self) -> dict[str, tuple[str, int, int | float]]:
        """Raw affine func-dep map ``{tag: (representative, scale, offset)}``.

        The tuple shape ``sp_values._chase_inequality_source`` expects, so the
        trace's inequality resolver can hop a domain-less compare tag to its
        steerable source.
        """
        return dict(self._functional_deps)

    def elided_tags(self) -> frozenset[str]:
        """Tags proven scan-local/internal by ExploreContext."""
        return self._elided

    def is_stepping(self, tag: str) -> bool:
        """True for tags known to visit multiple values (real state dims)."""
        return tag in self._stepping

    def stepping_tags(self) -> tuple[str, ...]:
        """Return prover-confirmed stepping dimensions in stable order."""

        return tuple(sorted(self._stepping))


def build_transition_evidence(explore_ctx: Any) -> TransitionEvidence | None:
    """Build a :class:`TransitionEvidence` from a prover ExploreContext."""
    if explore_ctx is None:
        return None
    try:
        return TransitionEvidence(
            functional_deps=dict(explore_ctx.functional_dep_projections),
            elided=frozenset(explore_ctx.elided_tags),
            stepping=frozenset(explore_ctx.stepping_tags),
        )
    except Exception:  # noqa: BLE001
        logger.debug("evidence: build failed", exc_info=True)
        return None
