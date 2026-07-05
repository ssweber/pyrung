"""Transition evidence — static route expansion for the PILOT compass.

Bridges ExploreContext classifications and PDG writer analysis into
structured transition routes.  Given a target register, enumerates all
ingress paths through the writer pipeline, separating source constraints
(current-state facts) from enablers (things to cause) and identifying
steerable action tags.

Three capabilities:

1. **Canonicalize** — map scratch/index tags to their representative
   driver via functional dependency projections.

2. **Classify** — bucket tags as internal (don't chase), action
   (steerable), stepping (state dimension), etc.

3. **Expand routes** — enumerate all static ingress paths into a
   target register's writer pipeline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

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

    ``from_values`` are the governing-register values this route fires *from*,
    taken straight off the writer's own condition — including the **disjunction**
    (``Or(StateCurrent==STOPPED, ==COMPLETED)``) that ``source_constraints`` drops
    because ``_partition_conditions`` keeps only single-valued gates.  The compass
    value-graph fans one edge per from-value, so a command-gated hop whose source
    is an OR is no longer lost.  ``edge_gates`` are the steerable rise/fall tags
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
    from_values: tuple[Any, ...] = ()
    edge_gates: tuple[tuple[str, bool], ...] = ()


@dataclass(frozen=True)
class PipelineRoles:
    """Generic roles inferred for one opaque transition pipeline.

    ``governing_tag`` is the register being navigated. ``request_tags`` are
    transient pipeline inputs that can still expose useful cause chains.
    ``guard_internal_tags`` and ``scratch_internal_tags`` are implementation
    details of the writer pipeline and should not be recursively pursued as
    independent goals.
    """

    governing_tag: str
    request_tags: frozenset[str] = frozenset()
    guard_internal_tags: frozenset[str] = frozenset()
    scratch_internal_tags: frozenset[str] = frozenset()

    @property
    def trace_internal_tags(self) -> frozenset[str]:
        return self.guard_internal_tags | self.scratch_internal_tags

    @property
    def participating_tags(self) -> frozenset[str]:
        return (
            frozenset((self.governing_tag,))
            | self.request_tags
            | self.guard_internal_tags
            | self.scratch_internal_tags
        )


@dataclass(frozen=True)
class _IndirectPipelineSource:
    """An indirect-copy source whose pointer is driven by a request tag."""

    request_tag: str
    block: Any
    eval_addr: Any


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
    from pyrung.core.crossing import UNKNOWN, Affine, Literal

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
    indirect_sources: dict[str, list[_IndirectPipelineSource]] = {}

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
                    from_values=_governing_from_values(cond_expr, target_tag, source_aliases),
                    edge_gates=_route_edge_gates(node, pdg, program, steerable),
                )
            )

        elif isinstance(written, Affine):
            request_tags.add(written.source)

        elif written is UNKNOWN:
            indirect = _indirect_pipeline_source(
                rung_obj,
                target_tag,
                pdg,
                program,
                evidence,
            )
            if indirect is not None:
                request_tags.add(indirect.request_tag)
                indirect_sources.setdefault(indirect.request_tag, []).append(indirect)

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
                    dest_value = _destination_from_indirect(
                        req_value,
                        indirect_sources.get(request_tag, ()),
                    )
                if dest_value is None:
                    dest_value = req_value

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
                    from_values=_governing_from_values(req_expr, target_tag, source_aliases),
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
    from pyrung.core.crossing import Affine, Literal

    if isinstance(written, Literal):
        return True, written.value
    if isinstance(written, Affine):
        tag = pdg.tags.get(written.source)
        if tag is None:
            return False, None
        try:
            return True, tag.default * written.scale + written.offset
        except (TypeError, ValueError):
            return False, None
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


def _indirect_pipeline_source(
    rung_obj: Any,
    target_tag: str,
    pdg: ProgramGraph,
    program: Any,
    evidence: TransitionEvidence | None,
) -> _IndirectPipelineSource | None:
    """Return the request-tag view of an indirect copy writing *target_tag*."""
    from pyrung.core.analysis.sp_values import _expr_tag_names, _SnapshotView
    from pyrung.core.instruction.data_transfer import CopyInstruction
    from pyrung.core.memory_block import IndirectExprRef, IndirectRef

    src = None
    for instr in getattr(rung_obj, "_instructions", ()):
        if not isinstance(instr, CopyInstruction):
            continue
        if getattr(instr.dest, "name", None) != target_tag:
            continue
        if isinstance(instr.source, (IndirectRef, IndirectExprRef)):
            src = instr.source
        break
    if src is None:
        return None

    if isinstance(src, IndirectRef):
        idx_tag = src.pointer.name
        eval_addr: Any = lambda v: int(v)
    else:
        names = _expr_tag_names(src.expr)
        if names is None or len(names) != 1:
            return None
        idx_tag = next(iter(names))
        iexpr = src.expr
        itag = idx_tag
        eval_addr = lambda v: int(iexpr.evaluate(_SnapshotView({}, {itag: v})))

    request_tag, eval_addr = _canonical_index_source(
        idx_tag,
        eval_addr,
        pdg,
        program,
        evidence,
    )
    if request_tag == target_tag:
        return None
    return _IndirectPipelineSource(
        request_tag=request_tag,
        block=src.block,
        eval_addr=eval_addr,
    )


def _canonical_index_source(
    idx_tag: str,
    eval_addr: Any,
    pdg: ProgramGraph,
    program: Any,
    evidence: TransitionEvidence | None,
) -> tuple[str, Any]:
    """Hop pointer scratch back to the representative request tag."""
    from pyrung.core.analysis.sp_values import _SnapshotView

    tag = idx_tag
    for _ in range(3):
        canonical = evidence.canonicalize(tag) if evidence is not None else None
        if canonical is not None:
            prev = eval_addr
            rep = canonical.representative
            scale = canonical.scale
            offset = canonical.offset
            eval_addr = lambda v, _prev=prev, _scale=scale, _offset=offset: _prev(
                _scale * v + _offset
            )
            tag = rep
            continue

        calc_def = _single_calc_source(tag, pdg, program)
        if calc_def is None:
            break
        expr, rep = calc_def
        prev = eval_addr
        eval_addr = lambda v, _prev=prev, _expr=expr, _rep=rep: _prev(
            _expr.evaluate(_SnapshotView({}, {_rep: v}))
        )
        tag = rep
    return tag, eval_addr


def _single_calc_source(idx_tag: str, pdg: ProgramGraph, program: Any) -> tuple[Any, str] | None:
    """``(expression, source_tag)`` when *idx_tag* is single-calc scratch."""
    from pyrung.core.analysis.pdg import resolve_rung
    from pyrung.core.analysis.sp_values import _expr_tag_names
    from pyrung.core.instruction.calc import CalcInstruction

    writers = pdg.writers_of.get(idx_tag, frozenset())
    if len(writers) != 1:
        return None
    rung_obj = resolve_rung(program, pdg.rung_nodes[next(iter(writers))])
    if rung_obj is None:
        return None
    for instr in getattr(rung_obj, "_instructions", ()):
        if isinstance(instr, CalcInstruction) and getattr(instr.dest, "name", None) == idx_tag:
            names = _expr_tag_names(instr.expression)
            if names is not None and len(names) == 1:
                src = next(iter(names))
                if src != idx_tag:
                    return instr.expression, src
            return None
    return None


def _destination_from_indirect(
    request_value: Any,
    sources: Any,
) -> Any | None:
    """Read the destination value for one request value from an indirect table."""
    for source in sources:
        try:
            addr = int(source.eval_addr(request_value))
            source.block._validate_address(addr)
        except (IndexError, TypeError, ValueError, ZeroDivisionError):
            continue
        _retentive, value = source.block._effective_slot_policy(addr)
        return value
    return None


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

    call_sites = pdg.call_site_rung_indices().get(
        node.subroutine,
        frozenset(),
    )
    gates: list[tuple[str, Any]] = []

    main_by_rung: dict[int, int] = {}
    for idx, n in enumerate(pdg.rung_nodes):
        if n.subroutine is None and not n.branch_path:
            main_by_rung.setdefault(n.rung_index, idx)

    for cs_rung_idx in sorted(call_sites):
        cs_node_idx = main_by_rung.get(cs_rung_idx)
        if cs_node_idx is None:
            continue
        cs_rung = resolve_rung(program, pdg.rung_nodes[cs_node_idx])
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


def _governing_from_values(
    expr: Any,
    governing_tag: str,
    source_aliases: dict[tuple[str, Any], tuple[str, Any]] | None = None,
) -> tuple[Any, ...]:
    """Governing-register values a writer fires *from*, OR included.

    Read straight off the writer's own condition so a disjunctive source
    (``Or(StateCurrent==STOPPED, ==COMPLETED)``) survives as multiple from-values
    where :func:`_partition_conditions` would have dropped it for being
    multi-valued.

    Alias state-flags are resolved through *source_aliases*, so a disjunction
    written over *derived* flags (``Or(S_Execute, S_Suspended)`` meaning
    ``StateCurrent in {6, 5}``) fans out the same way a direct
    ``Or(StateCurrent==6, ==5)`` would.  This walks the condition tree directly
    rather than :func:`_extract_condition_values`' collapsed dict, which drops an
    ``Or`` whose branches constrain *different* tags — exactly the alias case —
    before the alias map can resolve them, leaving the compass with an unguarded
    ``ANY`` edge (Hold reachable from any state).

    Empty when the writer names no governing value (an init/clear/fault rung —
    not a navigable state transition).
    """
    constraint = _governing_constraint(expr, governing_tag, source_aliases or {})
    if not constraint:
        return ()
    try:
        return tuple(sorted(constraint))
    except TypeError:
        return tuple(constraint)


def _governing_constraint(
    expr: Any,
    governing_tag: str,
    source_aliases: dict[tuple[str, Any], tuple[str, Any]],
) -> frozenset[Any] | None:
    """Governing-register values satisfying *expr*, or ``None`` if unconstrained.

    ``None`` is the top element (fires from any state): an ``And`` narrows it (a
    term that constrains the governing register intersects), an ``Or`` widens it
    (branches union) — but an ``Or`` branch that is itself unconstrained makes the
    whole ``Or`` unconstrained, since the writer can then fire from any state via
    that branch.  Atoms resolve both direct (``StateCurrent==N``) and alias
    (``S_Execute`` → ``StateCurrent==6``) governing values.
    """
    from pyrung.core.analysis.simplified import And, Atom, Or
    from pyrung.core.analysis.sp_values import _required_from_atom

    if isinstance(expr, Atom):
        pairs = _required_from_atom(expr)
        if not pairs:
            return None
        vals: set[Any] = set()
        for tag, value in pairs:
            if tag == governing_tag:
                vals.add(value)
            else:
                alias = source_aliases.get((tag, value))
                if alias is not None and alias[0] == governing_tag:
                    vals.add(alias[1])
        return frozenset(vals) if vals else None
    if isinstance(expr, And):
        result: frozenset[Any] | None = None
        for term in expr.terms:
            c = _governing_constraint(term, governing_tag, source_aliases)
            if c is None:
                continue
            result = c if result is None else (result & c)
        return result
    if isinstance(expr, Or):
        union: frozenset[Any] = frozenset()
        for term in expr.terms:
            c = _governing_constraint(term, governing_tag, source_aliases)
            if c is None:
                return None
            union |= c
        return union
    return None


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

    if node.subroutine is not None:
        call_sites = pdg.call_site_rung_indices().get(node.subroutine, frozenset())
        main_by_rung: dict[int, int] = {}
        for idx, n in enumerate(pdg.rung_nodes):
            if n.subroutine is None and not n.branch_path:
                main_by_rung.setdefault(n.rung_index, idx)
        for cs_rung_idx in sorted(call_sites):
            cs_node_idx = main_by_rung.get(cs_rung_idx)
            if cs_node_idx is not None:
                visit_node(pdg.rung_nodes[cs_node_idx])

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
    or burner-specific names. A guard is considered pipeline-internal when it is
    part of a route condition and all of its readers are the target writer
    rungs. Request tags are retained as traceable because their writers often
    reveal the meaningful transition causes.
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
        governing_tag=target_tag,
        request_tags=request_tags,
        guard_internal_tags=frozenset(guard_candidates),
        scratch_internal_tags=scratch,
    )


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
    """Adapter bridging ExploreContext classifications into PILOT's compass.

    Constructed from prover context when available.  Provides tag-level
    canonicalization and classification that complement the route-level
    analysis in :func:`expand_routes`.
    """

    def __init__(
        self,
        *,
        functional_deps: dict[str, tuple[str, int, int | float]],
        elided: dict[str, str],
        stepping: frozenset[str],
        free_inputs: frozenset[str],
        combinational: frozenset[str],
        init_constants: frozenset[str],
    ) -> None:
        self._functional_deps = functional_deps
        self._elided = elided
        self._stepping = stepping
        self._free_inputs = free_inputs
        self._combinational = combinational
        self._init_constants = init_constants

    def canonicalize(self, tag: str) -> CanonicalForm | None:
        """If *tag* is a functional dep projection, return its canonical form."""
        entry = self._functional_deps.get(tag)
        if entry is None:
            return None
        rep, scale, offset = entry
        return CanonicalForm(representative=rep, scale=scale, offset=offset)

    def representative(self, tag: str) -> str:
        """Canonical representative for a tag (itself if not a projection)."""
        entry = self._functional_deps.get(tag)
        return entry[0] if entry is not None else tag

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
        return frozenset(self._elided)

    def is_internal(self, tag: str) -> bool:
        """True for scan-local scratch or functional dep projections."""
        return tag in self._elided

    def is_stepping(self, tag: str) -> bool:
        """True for tags known to visit multiple values (real state dims)."""
        return tag in self._stepping

    def classify(self, tag: str) -> str:
        """Classify a tag into a compass bucket.

        Returns one of: ``"internal"``, ``"free"``, ``"stepping"``,
        ``"combinational"``, ``"init_constant"``, ``"unknown"``.
        """
        if tag in self._elided:
            return "internal"
        if tag in self._free_inputs:
            return "free"
        if tag in self._stepping:
            return "stepping"
        if tag in self._combinational:
            return "combinational"
        if tag in self._init_constants:
            return "init_constant"
        return "unknown"


def build_transition_evidence(explore_ctx: Any) -> TransitionEvidence | None:
    """Build a :class:`TransitionEvidence` from a prover ExploreContext."""
    if explore_ctx is None:
        return None
    try:
        return TransitionEvidence(
            functional_deps=dict(explore_ctx.functional_dep_projections),
            elided=dict(explore_ctx.elided_tags),
            stepping=frozenset(explore_ctx.stepping_tags),
            free_inputs=frozenset(explore_ctx.free_input_names),
            combinational=frozenset(explore_ctx.combinational_tags),
            init_constants=frozenset(explore_ctx.init_constant_projections),
        )
    except Exception:  # noqa: BLE001
        logger.debug("evidence: build failed", exc_info=True)
        return None
