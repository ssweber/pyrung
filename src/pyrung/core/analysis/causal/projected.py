from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from pyrung.core.analysis.pdg import resolve_rung
from pyrung.core.analysis.sp_tree import attribute, evaluate_sp
from pyrung.core.analysis.sp_values import (
    _chase_inequality_source,
    _expr_tag_names,
    _extract_inequality_prereqs,
    _SnapshotView,
    copy_source_binding,
)
from pyrung.core.context import ScanContext

from .history import (
    _NO_WRITE,
    _find_last_transition_scan,
    _tag_value_at_scan,
    _writer_indices,
)
from .models import (
    BlockerReason,
    BlockingCondition,
    BlockingMove,
    BlockingRelation,
    CausalChain,
    ChainStep,
    EnablingCondition,
    Transition,
)
from .support import (
    _condition_tag_name,
    _CounterfactualView,
    _HistoricalView,
)

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.condition import Condition
    from pyrung.core.history import History
    from pyrung.core.program import Program
    from pyrung.core.rung import Rung
    from pyrung.core.rung_firings import RungFiringTimelines
    from pyrung.core.state import SystemState
    from pyrung.core.tag import Tag


def _get_tag_name(tag: Tag | str) -> str:
    return tag if isinstance(tag, str) else tag.name


@dataclass(frozen=True)
class SimulatedScan:
    """Result of a hypothetical single-scan simulation."""

    state_after: SystemState
    rung_writes: Any  # PMap[int, PMap[str, Any]]


_UNSUPPORTED_NUMERIC_INVERSION = object()

_COMPARE_OPERATORS = {
    "CompareEq": "==",
    "CompareNe": "!=",
    "CompareLt": "<",
    "CompareLe": "<=",
    "CompareGt": ">",
    "CompareGe": ">=",
}

_COMPARE_FORMS = {
    "CompareEq": "eq",
    "CompareNe": "ne",
    "CompareLt": "lt",
    "CompareLe": "le",
    "CompareGt": "gt",
    "CompareGe": "ge",
}


def _condition_needed_value(condition: Condition, state: SystemState, current_value: Any) -> Any:
    """Concrete value that would make a false leaf condition true.

    Bool contacts invert the current value as before.  Numeric comparisons
    carry a resolved threshold from the current state snapshot, so
    ``Mode == 1`` yields ``1`` instead of ``False`` and ``Level >= Limit``
    yields the current ``Limit`` value instead of ``True``.
    """
    from pyrung.core.condition import (
        CompareEq,
        CompareGe,
        CompareGt,
        CompareLe,
        CompareLt,
        CompareNe,
        IntTruthyCondition,
        _resolve_value,
    )

    def operand_value() -> Any:
        value = getattr(condition, "value", _UNSUPPORTED_NUMERIC_INVERSION)
        if value is _UNSUPPORTED_NUMERIC_INVERSION:
            return value
        try:
            return _resolve_value(value, cast(Any, _HistoricalView(state)))
        except Exception:
            return _UNSUPPORTED_NUMERIC_INVERSION

    def first_different(value: Any) -> Any:
        if isinstance(value, bool):
            return not value
        if isinstance(value, int) and not isinstance(value, bool):
            return value + 1
        if isinstance(value, float):
            return value + 1.0
        return _UNSUPPORTED_NUMERIC_INVERSION

    def satisfying_value(form: str, threshold: Any) -> Any:
        if isinstance(threshold, bool):
            return _UNSUPPORTED_NUMERIC_INVERSION
        if isinstance(threshold, int):
            if form == "gt":
                return threshold + 1
            if form == "ge":
                return threshold
            if form == "lt":
                return threshold - 1
            if form == "le":
                return threshold
        if isinstance(threshold, float):
            if form == "gt":
                return threshold + 1.0
            if form == "ge":
                return threshold
            if form == "lt":
                return threshold - 1.0
            if form == "le":
                return threshold
        return _UNSUPPORTED_NUMERIC_INVERSION

    if isinstance(condition, IntTruthyCondition):
        return 1

    if isinstance(condition, CompareEq):
        value = operand_value()
        if value is not _UNSUPPORTED_NUMERIC_INVERSION:
            return value

    if isinstance(condition, CompareNe):
        value = operand_value()
        if value is not _UNSUPPORTED_NUMERIC_INVERSION:
            different = first_different(value)
            if different is not _UNSUPPORTED_NUMERIC_INVERSION:
                return different

    forms = (
        (CompareGt, "gt"),
        (CompareGe, "ge"),
        (CompareLt, "lt"),
        (CompareLe, "le"),
    )
    for cls, form in forms:
        if isinstance(condition, cls):
            value = operand_value()
            if value is not _UNSUPPORTED_NUMERIC_INVERSION:
                needed = satisfying_value(form, value)
                if needed is not _UNSUPPORTED_NUMERIC_INVERSION:
                    return needed

    return not current_value if current_value is not None else True


def _condition_relation(
    condition: Condition,
    state: SystemState,
    *,
    nd_domains: dict[str, tuple[Any, ...]] | None,
    pdg: ProgramGraph,
    program: Program | None,
    func_deps: dict[str, tuple[str, int, Any]] | None,
) -> BlockingRelation | None:
    """Describe a false compare leaf as a relation plus candidate moves."""
    from pyrung.core.condition import _resolve_value
    from pyrung.core.expression import Expression
    from pyrung.core.tag import Tag

    cls_name = type(condition).__name__
    op = _COMPARE_OPERATORS.get(cls_name)
    form = _COMPARE_FORMS.get(cls_name)
    lhs = getattr(condition, "tag", None)
    rhs = getattr(condition, "value", None)
    lhs_tag = getattr(lhs, "name", None)
    if op is None or form is None or lhs_tag is None:
        return None

    try:
        rhs_value = _resolve_value(rhs, cast(Any, _HistoricalView(state)))
    except Exception:
        return None

    lhs_value = state.tags.get(lhs_tag)
    rhs_repr = _relation_rhs_repr(rhs)
    relation_tags = {lhs_tag}
    if isinstance(rhs, Tag):
        relation_tags.add(rhs.name)
    elif isinstance(rhs, Expression):
        names = _expr_tag_names(rhs)
        if names:
            relation_tags.update(names)

    moves: list[BlockingMove] = []
    seen_moves: set[tuple[str, Any]] = set()

    def add_move(tag: str, value: Any, source: str) -> None:
        key = (tag, value)
        if key in seen_moves or _values_equal(state.tags.get(tag), value):
            return
        seen_moves.add(key)
        moves.append(BlockingMove(tag=tag, value=value, source=source))

    if form in {"lt", "le", "gt", "ge"}:
        if nd_domains and program is not None:
            from pyrung.core.analysis.simplified import _condition_to_expr

            expr = _condition_to_expr(condition)
            for tag, value in _extract_inequality_prereqs(
                expr, dict(state.tags), nd_domains, pdg, program, func_deps
            ):
                add_move(tag, value, "condition")
        _add_rhs_only_moves(
            rhs,
            form,
            lhs_value,
            state,
            nd_domains,
            add_move,
        )
        if nd_domains:
            hit = _chase_inequality_source(lhs_tag, form, rhs_value, nd_domains, func_deps)
            if hit is not None:
                add_move(hit[0], hit[1], "functional_dep")

    return BlockingRelation(
        lhs_tag=lhs_tag,
        lhs_value=lhs_value,
        operator=op,
        rhs_repr=rhs_repr,
        rhs_value=rhs_value,
        candidate_moves=tuple(moves),
        tags=tuple(sorted(relation_tags)),
    )


def _relation_rhs_repr(rhs: Any) -> str:
    from pyrung.core.expression import Expression, format_expr
    from pyrung.core.tag import Tag

    if isinstance(rhs, Tag):
        return rhs.name
    if isinstance(rhs, Expression):
        return format_expr(rhs)
    return repr(rhs)


def _values_equal(left: Any, right: Any) -> bool:
    try:
        return left == right
    except Exception:
        return False


def _compare_value(form: str, lhs: Any, rhs: Any) -> bool:
    try:
        if form == "lt":
            return lhs < rhs
        if form == "le":
            return lhs <= rhs
        if form == "gt":
            return lhs > rhs
        if form == "ge":
            return lhs >= rhs
    except TypeError:
        return False
    return False


def _add_rhs_only_moves(
    rhs: Any,
    form: str,
    lhs_value: Any,
    state: SystemState,
    nd_domains: dict[str, tuple[Any, ...]] | None,
    add_move: Any,
) -> None:
    """Add RHS-only moves that make the current LHS satisfy the relation."""
    if not nd_domains:
        return
    from pyrung.core.expression import Expression
    from pyrung.core.tag import Tag

    rhs_tags: tuple[str, ...]
    if isinstance(rhs, Tag):
        rhs_tags = (rhs.name,)
    elif isinstance(rhs, Expression):
        names = _expr_tag_names(rhs)
        if not names:
            return
        rhs_tags = tuple(sorted(names))
    else:
        return

    for tag in rhs_tags:
        domain = nd_domains.get(tag)
        if not domain:
            continue
        for candidate in domain:
            try:
                if isinstance(rhs, Tag):
                    rhs_value = candidate
                else:
                    rhs_value = rhs.evaluate(_SnapshotView(dict(state.tags), {tag: candidate}))
            except Exception:
                continue
            if _compare_value(form, lhs_value, rhs_value):
                add_move(tag, candidate, "rhs")
                break


def _simulate_scan(
    logic: list[Rung] | Program,
    state: SystemState,
) -> SimulatedScan:
    """Run one hypothetical scan and return per-rung tag writes.

    The caller is responsible for all state preparation:

    * Injecting ``_prev:{tag}`` memory entries for edge-sensitive triggers.
    * Setting ``_dt`` in memory if timer behaviour matters (default is
      whatever the state already carries; hypothetical scans typically
      inherit ``_dt=0.0``).
    * Applying tag overrides via ``state.with_tags(...)``.
    """
    from pyrung.core.program import Program as ProgramClass

    ctx = ScanContext(state)

    if isinstance(logic, ProgramClass):
        from pyrung.core.executor import execute_program

        execute_program(logic, ctx, capture_rungs=True)
    else:
        for i, rung in enumerate(logic):
            with ctx.capturing_rung(i):
                rung.evaluate(ctx)

    state_after = ctx.commit(dt=0.0)
    return SimulatedScan(state_after=state_after, rung_writes=ctx.rung_firings)


def _rung_produces_value(
    rung: Rung,
    rung_idx: int,
    tag_name: str,
    value: Any,
    state: SystemState,
) -> bool:
    """Check if *rung* would write *value* to *tag_name* when enabled.

    Simulates execution with ``enabled=True`` against *state* and
    inspects the captured writes.
    """
    from pyrung.core.instruction.base import SubroutineReturnSignal

    ctx = ScanContext(state)
    with ctx.capturing_rung(rung_idx):
        try:
            rung.execute(ctx, enabled=True)
        except SubroutineReturnSignal:
            # return_early() in the rung: writes captured before the signal
            # are exactly the real in-scan semantics (the signal aborts the
            # rest of the subroutine, not this rung's earlier instructions).
            pass
    writes = ctx._rung_firings.get(rung_idx, {})
    if writes.get(tag_name) == value:
        return True
    # Timer/counter done_bit is temporal: the single-scan simulation may
    # write False (accumulator below preset) but the rung WILL produce True
    # after enough enabled scans.  Accept the rung as a candidate so
    # projected_cause classifies its enabling conditions correctly.
    if value is True and tag_name in writes:
        if any(
            (db := getattr(i, "done_bit", None)) is not None
            and getattr(db, "name", None) == tag_name
            for i in rung._instructions
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# Projected backward walk
# ---------------------------------------------------------------------------


def _has_observed_transition(
    history: History,
    tag_name: str,
    to_value: Any,
    *,
    timelines: RungFiringTimelines | None = None,
    pdg: ProgramGraph | None = None,
) -> bool:
    """Check whether *tag_name* has ever transitioned to *to_value* in history."""
    ids = list(history.scan_ids())
    writers = _writer_indices(pdg, tag_name) if pdg is not None else None
    if timelines is not None and writers is not None and writers:
        for i in range(1, len(ids)):
            cur_val = _tag_value_at_scan(timelines, writers, tag_name, ids[i])
            if cur_val is _NO_WRITE:
                continue
            prev_val = _tag_value_at_scan(timelines, writers, tag_name, ids[i - 1])
            if prev_val is _NO_WRITE:
                prev_val = history.at(ids[i - 1]).tags.get(tag_name)
            if cur_val != prev_val and cur_val == to_value:
                return True
        return False

    # State-based fallback (also used for external-input tags with no writers)
    for i in range(1, len(ids)):
        cur_val = history.at(ids[i]).tags.get(tag_name)
        prev_val = history.at(ids[i - 1]).tags.get(tag_name)
        if cur_val != prev_val and cur_val == to_value:
            return True
    return False


def _classify_sp_needs(
    node: Any,
    evaluate: Any,
    state: Any,
    history: Any,
    pdg: Any,
    rung_idx: int,
    latest_scan: int,
    seen_tags: set[str],
    *,
    assume: dict[str, Any] | None = None,
    timelines: Any = None,
    nd_domains: dict[str, tuple[Any, ...]] | None = None,
    program: Any = None,
    func_deps: dict[str, tuple[str, int, Any]] | None = None,
    structural: bool = False,
) -> tuple[list[Transition], list[EnablingCondition], list[BlockingCondition]]:
    """Walk an SP tree structurally to classify projected needs.

    For an SPSeries (AND), all children's needs are conjoined — any blocked
    child blocks the series.  For an SPParallel (OR), each child is an
    independent alternative — the best child (fewest blockers, then fewest
    proximate needs) wins.  A child with zero blockers makes the Or viable
    even when sibling branches are blocked.
    """
    from pyrung.core.analysis.sp_tree import SPLeaf, SPParallel, SPSeries

    if isinstance(node, SPLeaf):
        return _classify_leaf(
            node,
            evaluate,
            state,
            history,
            pdg,
            rung_idx,
            latest_scan,
            seen_tags,
            assume=assume,
            timelines=timelines,
            nd_domains=nd_domains,
            program=program,
            func_deps=func_deps,
            structural=structural,
        )

    def _recurse(
        child: Any,
        child_seen: set[str],
    ) -> tuple[list[Transition], list[EnablingCondition], list[BlockingCondition]]:
        return _classify_sp_needs(
            child,
            evaluate,
            state,
            history,
            pdg,
            rung_idx,
            latest_scan,
            child_seen,
            assume=assume,
            timelines=timelines,
            nd_domains=nd_domains,
            program=program,
            func_deps=func_deps,
            structural=structural,
        )

    if isinstance(node, SPSeries):
        all_prox: list[Transition] = []
        all_enab: list[EnablingCondition] = []
        all_block: list[BlockingCondition] = []
        for child in node.children:
            p, e, b = _recurse(child, seen_tags)
            all_prox.extend(p)
            all_enab.extend(e)
            all_block.extend(b)
        return all_prox, all_enab, all_block

    if isinstance(node, SPParallel):
        branches: list[
            tuple[list[Transition], list[EnablingCondition], list[BlockingCondition], set[str]]
        ] = []
        for child in node.children:
            child_seen = set(seen_tags)
            p, e, b = _recurse(child, child_seen)
            branches.append((p, e, b, child_seen))

        unblocked = [(p, e, b, s) for p, e, b, s in branches if not b]
        if unblocked:
            merged_prox: list[Transition] = []
            merged_enab: list[EnablingCondition] = []
            merged_names: set[str] = set()
            for p, e, _b, _s in unblocked:
                for t in p:
                    if t.tag_name not in merged_names:
                        merged_names.add(t.tag_name)
                        merged_prox.append(t)
                for ec in e:
                    if ec.tag_name not in merged_names:
                        merged_names.add(ec.tag_name)
                        merged_enab.append(ec)
            seen_tags.update(merged_names)
            return merged_prox, merged_enab, []

        all_prox_b: list[Transition] = []
        all_enab_b: list[EnablingCondition] = []
        all_block_b: list[BlockingCondition] = []
        all_names_b: set[str] = set()
        for p, e, b, _s in branches:
            for t in p:
                if t.tag_name not in all_names_b:
                    all_names_b.add(t.tag_name)
                    all_prox_b.append(t)
            for ec in e:
                if ec.tag_name not in all_names_b:
                    all_names_b.add(ec.tag_name)
                    all_enab_b.append(ec)
            for bc in b:
                if bc.blocked_tag not in all_names_b:
                    all_names_b.add(bc.blocked_tag)
                    all_block_b.append(bc)
        seen_tags.update(all_names_b)
        return all_prox_b, all_enab_b, all_block_b

    return [], [], []


def _classify_leaf(
    leaf: Any,
    evaluate: Any,
    state: Any,
    history: Any,
    pdg: Any,
    rung_idx: int,
    latest_scan: int,
    seen_tags: set[str],
    *,
    assume: dict[str, Any] | None = None,
    timelines: Any = None,
    nd_domains: dict[str, tuple[Any, ...]] | None = None,
    program: Any = None,
    func_deps: dict[str, tuple[str, int, Any]] | None = None,
    structural: bool = False,
) -> tuple[list[Transition], list[EnablingCondition], list[BlockingCondition]]:
    """Classify a single SP leaf as enabling, proximate, or blocked."""
    cond_tag = _condition_tag_name(leaf.condition)
    if cond_tag is None or cond_tag in seen_tags:
        return [], [], []
    seen_tags.add(cond_tag)

    cond_value = state.tags.get(cond_tag)
    leaf_result = evaluate(leaf.condition)

    if leaf_result:
        held_since = None
        if not structural:
            held_since = _find_last_transition_scan(history, cond_tag, latest_scan + 1)
        return (
            [],
            [
                EnablingCondition(
                    tag_name=cond_tag,
                    value=cond_value,
                    held_since_scan=held_since,
                )
            ],
            [],
        )

    needed_value = _condition_needed_value(leaf.condition, state, cond_value)
    relation = _condition_relation(
        leaf.condition,
        state,
        nd_domains=nd_domains,
        pdg=pdg,
        program=program,
        func_deps=func_deps,
    )

    is_input = not pdg.writers_of.get(cond_tag, frozenset())
    if structural:
        reachable = True
    else:
        reachable = (assume and cond_tag in assume) or _has_observed_transition(
            history,
            cond_tag,
            needed_value,
            timelines=timelines,
            pdg=pdg,
        )

    if reachable or is_input:
        return (
            [
                Transition(
                    tag_name=cond_tag,
                    scan_id=latest_scan,
                    from_value=cond_value,
                    to_value=needed_value,
                )
            ],
            [],
            [],
        )

    reason = BlockerReason.NO_OBSERVED_TRANSITION if is_input else BlockerReason.BLOCKED_UPSTREAM
    return (
        [],
        [],
        [
            BlockingCondition(
                rung_index=rung_idx,
                blocked_tag=cond_tag,
                needed_value=needed_value,
                reason=reason,
                relation=relation,
            )
        ],
    )


def projected_cause(
    logic: list[Rung],
    history: History,
    tag: Tag | str,
    to_value: Any,
    pdg: ProgramGraph,
    assume: dict[str, Any] | None = None,
    *,
    timelines: RungFiringTimelines | None = None,
    program: Program | None = None,
    nd_domains: dict[str, tuple[Any, ...]] | None = None,
    func_deps: dict[str, tuple[str, int, Any]] | None = None,
    structural: bool = False,
) -> CausalChain:
    """Build a projected causal chain: what would need to happen for *tag*
    to reach *to_value*?

    Walks the static PDG to find rungs that could write the desired value,
    then evaluates their SP trees against the current state to identify
    which conditions are already met (enabling) vs which need to transition
    (projected proximate causes).

    Returns ``mode='projected'`` when a reachable path exists, or
    ``mode='unreachable'`` with populated ``blockers`` when not.

    Args:
        logic: The program's rung list.
        history: The runner's History instance.
        tag: The tag (or tag name) to analyze.
        to_value: The desired target value.
        pdg: The program's static dependency graph.

    Returns:
        A ``CausalChain``.  Never returns ``None``.
    """
    tag_name = _get_tag_name(tag)

    latest_scan = history.newest_scan_id
    state = history.at(latest_scan)

    # Apply assumption overrides to the state snapshot
    if assume:
        state = state.with_tags(assume)

    current_value = state.tags.get(tag_name)
    step_fidelity = "structural" if structural else "full"

    # Hypothetical transition for the chain effect
    effect_transition = Transition(
        tag_name=tag_name,
        scan_id=latest_scan,
        from_value=current_value,
        to_value=to_value,
    )

    if current_value == to_value:
        # Already at desired value — projected with empty steps
        return CausalChain(effect=effect_transition, mode="projected")

    # Find rung indices that write to this tag (from PDG)
    writer_indices = pdg.writers_of.get(tag_name, frozenset())
    if not writer_indices:
        return CausalChain(
            effect=effect_transition,
            mode="unreachable",
            blockers=[
                BlockingCondition(
                    rung_index=-1,
                    blocked_tag=tag_name,
                    needed_value=to_value,
                    reason=BlockerReason.NO_OBSERVED_TRANSITION,
                )
            ],
        )

    # Find candidate rungs: those whose instructions would produce to_value.
    # Writers may live in subroutines — resolve via resolve_rung (pdg.py).
    # A copy-from-tag writer produces whatever its source holds *now*; it is
    # still a candidate for any to_value, carrying the source requirement
    # (source must reach to_value) as an extra condition to classify.
    candidate_rungs: list[tuple[int, Rung, str | None, tuple[str, Any] | None]] = []
    for node_idx in writer_indices:
        node = pdg.rung_nodes[node_idx]
        if program is not None:
            rung = resolve_rung(program, node)
        elif node.subroutine is None and node.rung_index < len(logic):
            rung = logic[node.rung_index]
        else:
            rung = None
        if rung is None:
            continue
        if _rung_produces_value(rung, node.rung_index, tag_name, to_value, state):
            candidate_rungs.append((node.rung_index, rung, node.subroutine, None))
            continue
        binding = copy_source_binding(rung, tag_name, to_value)
        if binding is not None:
            candidate_rungs.append((node.rung_index, rung, node.subroutine, binding))

    if not candidate_rungs:
        return CausalChain(
            effect=effect_transition,
            mode="unreachable",
            blockers=[
                BlockingCondition(
                    rung_index=-1,
                    blocked_tag=tag_name,
                    needed_value=to_value,
                    reason=BlockerReason.NO_OBSERVED_TRANSITION,
                )
            ],
        )

    # Try each candidate rung, collect the best viable path and blockers
    best_steps: list[ChainStep] | None = None
    best_proximate: list[Transition] | None = None
    all_blockers: list[BlockingCondition] = []

    for rung_idx, rung, sub_name, source_req in candidate_rungs:
        sp_tree = rung.sp_tree()

        proximate: list[Transition] = []
        enabling: list[EnablingCondition] = []
        rung_blockers: list[BlockingCondition] = []
        seen_tags: set[str] = set()

        if source_req is not None:
            # Copy-from-tag writer: the source reaching to_value is a
            # precondition exactly like a contact — classify it the same
            # way (data-flow half of the regression).
            src_tag, src_needed = source_req
            seen_tags.add(src_tag)
            src_value = state.tags.get(src_tag)
            if src_value == src_needed:
                held_since = None
                if not structural:
                    held_since = _find_last_transition_scan(history, src_tag, latest_scan + 1)
                enabling.append(
                    EnablingCondition(
                        tag_name=src_tag,
                        value=src_value,
                        held_since_scan=held_since,
                    )
                )
            else:
                src_is_input = not pdg.writers_of.get(src_tag, frozenset())
                if structural:
                    src_reachable = True
                else:
                    src_reachable = (assume and src_tag in assume) or _has_observed_transition(
                        history,
                        src_tag,
                        src_needed,
                        timelines=timelines,
                        pdg=pdg,
                    )
                if src_reachable or src_is_input:
                    proximate.append(
                        Transition(
                            tag_name=src_tag,
                            scan_id=latest_scan,
                            from_value=src_value,
                            to_value=src_needed,
                        )
                    )
                else:
                    rung_blockers.append(
                        BlockingCondition(
                            rung_index=rung_idx,
                            blocked_tag=src_tag,
                            needed_value=src_needed,
                            reason=BlockerReason.BLOCKED_UPSTREAM,
                        )
                    )

        if sp_tree is None:
            # Unconditional rung — reachable unless the source is blocked
            if rung_blockers:
                all_blockers.extend(rung_blockers)
                continue
            steps = [
                ChainStep(
                    transition=effect_transition,
                    rung_index=rung_idx,
                    triggers=tuple(proximate),
                    enablers=tuple(enabling),
                    fidelity=step_fidelity,
                    subroutine=sub_name,
                )
            ]
            if best_steps is None or (
                best_proximate is not None and len(proximate) < len(best_proximate)
            ):
                best_steps = steps
                best_proximate = proximate
            continue

        # Walk the SP tree respecting Or/And structure.  For an SPParallel
        # (Or) that is currently false, satisfying ANY child suffices — so
        # we try each branch independently and pick the best (fewest needs,
        # fewest blockers).  The previous flat _collect_sp_leaves approach
        # treated every leaf as a conjunctive requirement, making a single
        # blocked Or-branch block the whole rung even when a sibling branch
        # was fully reachable.
        view = _HistoricalView(state)

        def _eval(cond: Condition, _v: Any = view) -> bool:
            return cond.evaluate(_v)  # type: ignore[arg-type]

        sp_prox, sp_enab, sp_block = _classify_sp_needs(
            sp_tree,
            _eval,
            state,
            history,
            pdg,
            rung_idx,
            latest_scan,
            seen_tags,
            assume=assume,
            timelines=timelines,
            nd_domains=nd_domains,
            program=program,
            func_deps=func_deps,
            structural=structural,
        )
        proximate.extend(sp_prox)
        enabling.extend(sp_enab)
        rung_blockers.extend(sp_block)

        if not rung_blockers:
            # All conditions are reachable — viable path
            step = ChainStep(
                transition=effect_transition,
                rung_index=rung_idx,
                triggers=tuple(proximate),
                enablers=tuple(enabling),
                fidelity=step_fidelity,
                subroutine=sub_name,
            )
            if best_steps is None or (
                best_proximate is not None and len(proximate) < len(best_proximate)
            ):
                best_steps = [step]
                best_proximate = proximate
        else:
            all_blockers.extend(rung_blockers)

    if best_steps is not None:
        return CausalChain(
            effect=effect_transition,
            mode="projected",
            steps=best_steps,
            conjunctive_roots=list(best_proximate or []),
        )

    # No viable path — unreachable
    return CausalChain(
        effect=effect_transition,
        mode="unreachable",
        blockers=all_blockers,
    )


# ---------------------------------------------------------------------------
# Projected forward walk
# ---------------------------------------------------------------------------


_EFFECT_SENTINEL = object()


def projected_effect(
    logic: list[Rung],
    history: History,
    tag: Tag | str,
    from_value: Any,
    pdg: ProgramGraph,
    assume: dict[str, Any] | None = None,
    *,
    to_value: Any = _EFFECT_SENTINEL,
    program: Program | None = None,
) -> CausalChain:
    """Build a projected forward chain: what would happen if *tag*
    transitioned from *from_value*?

    Uses simulation (one hypothetical scan) to discover effects and their
    exact values, then SP-tree attribution for enabler extraction.

    For Bool tags the ``to_value`` is inferred as ``not from_value``.
    For non-Bool tags, pass ``to_value`` explicitly; without it the
    function returns ``mode='unreachable'``.

    Returns ``mode='projected'`` (possibly with empty steps for dead-end
    cases where nothing reads the tag), or ``mode='unreachable'`` if the
    trigger transition itself can't be reached.
    """
    tag_name = _get_tag_name(tag)

    if to_value is _EFFECT_SENTINEL:
        if isinstance(from_value, bool):
            to_value = not from_value
        else:
            return CausalChain(
                effect=Transition(tag_name, 0, from_value, from_value),
                mode="unreachable",
            )

    latest_scan = history.newest_scan_id
    base_state = history.at(latest_scan)

    if assume:
        base_state = base_state.with_tags(assume)

    cause_transition = Transition(
        tag_name=tag_name,
        scan_id=latest_scan,
        from_value=from_value,
        to_value=to_value,
    )

    # Check trigger reachability
    current_value = base_state.tags.get(tag_name)
    if current_value != from_value:
        trigger_chain = projected_cause(logic, history, tag, from_value, pdg, assume=assume)
        if trigger_chain.mode == "unreachable":
            return CausalChain(
                effect=cause_transition,
                mode="unreachable",
                blockers=trigger_chain.blockers,
            )

    # --- Two-pass: simulation then attribution ---

    use_logic: list[Rung] | Program = program if program is not None else logic

    # Counterfactual: one scan without the trigger (baseline)
    cf_sim = _simulate_scan(use_logic, base_state)

    steps: list[ChainStep] = []
    seen_effects: set[str] = {tag_name}
    frontier: dict[str, Transition] = {tag_name: cause_transition}

    # Build hypothetical state: inject trigger + _prev: for edge detection
    hyp_state = base_state.with_tags({tag_name: to_value}).with_memory(
        {f"_prev:{tag_name}": from_value}
    )

    changed = True
    iterations = 0
    max_iterations = 10

    while changed and iterations < max_iterations:
        changed = False
        iterations += 1

        # Hypothetical scan
        hyp_sim = _simulate_scan(use_logic, hyp_state)

        # Walk all rung indices that wrote in either simulation
        all_rung_indices = set(hyp_sim.rung_writes.keys()) | set(cf_sim.rung_writes.keys())

        for rung_idx in sorted(all_rung_indices):
            hyp_writes = dict(hyp_sim.rung_writes.get(rung_idx, {}))
            cf_writes = dict(cf_sim.rung_writes.get(rung_idx, {}))

            all_written_tags = set(hyp_writes.keys()) | set(cf_writes.keys())

            for written_tag in all_written_tags:
                if written_tag in seen_effects:
                    continue

                hyp_val = hyp_writes.get(written_tag)
                cf_val = cf_writes.get(written_tag)

                if hyp_val == cf_val:
                    continue

                original_value = base_state.tags.get(written_tag)
                written_value = hyp_val if hyp_val is not None else original_value

                if written_value == original_value:
                    continue

                seen_effects.add(written_tag)
                changed = True

                effect_trans = Transition(
                    tag_name=written_tag,
                    scan_id=latest_scan,
                    from_value=original_value,
                    to_value=written_value,
                )

                # Find which frontier tag caused this rung to fire differently
                matched_trigger = _match_frontier_trigger(
                    rung_idx,
                    logic,
                    frontier,
                    base_state,
                )

                # SP-tree attribution for enablers
                enabling = _extract_enablers(
                    rung_idx,
                    logic,
                    hyp_state,
                    matched_trigger.tag_name if matched_trigger else tag_name,
                    history,
                    latest_scan,
                )

                steps.append(
                    ChainStep(
                        transition=effect_trans,
                        rung_index=rung_idx,
                        triggers=(matched_trigger,) if matched_trigger else (),
                        enablers=tuple(enabling),
                    )
                )

                frontier[written_tag] = effect_trans

        # Update hypothetical state with newly discovered effects
        new_tags: dict[str, Any] = {}
        new_memory: dict[str, Any] = {}
        for ft_name, ft_trans in frontier.items():
            new_tags[ft_name] = ft_trans.to_value
            new_memory[f"_prev:{ft_name}"] = ft_trans.from_value
        hyp_state = base_state.with_tags(new_tags).with_memory(new_memory)

    return CausalChain(
        effect=cause_transition,
        mode="projected",
        steps=steps,
    )


def _match_frontier_trigger(
    rung_idx: int,
    logic: list[Rung],
    frontier: dict[str, Transition],
    base_state: SystemState,
) -> Transition | None:
    """Find which frontier tag made *rung_idx* fire differently."""
    if rung_idx >= len(logic):
        return next(iter(frontier.values()), None)

    rung = logic[rung_idx]
    sp_tree = rung.sp_tree()
    if sp_tree is None:
        return next(iter(frontier.values()), None)

    for cause_tag, cause_trans in frontier.items():
        hyp_view = _CounterfactualView(base_state, cause_tag, cause_trans.to_value)
        cf_view = _CounterfactualView(base_state, cause_tag, cause_trans.from_value)

        def _eval_hyp(cond: Condition, _v: Any = hyp_view) -> bool:
            return cond.evaluate(_v)  # type: ignore[arg-type]

        def _eval_cf(cond: Condition, _v: Any = cf_view) -> bool:
            return cond.evaluate(_v)  # type: ignore[arg-type]

        if evaluate_sp(sp_tree, _eval_hyp) != evaluate_sp(sp_tree, _eval_cf):
            return cause_trans

    return next(iter(frontier.values()), None)


def _extract_enablers(
    rung_idx: int,
    logic: list[Rung],
    hyp_state: SystemState,
    cause_tag: str,
    history: History,
    latest_scan: int,
) -> list[EnablingCondition]:
    """SP-tree attribution for enabling conditions on a rung."""
    if rung_idx >= len(logic):
        return []

    rung = logic[rung_idx]
    sp_tree = rung.sp_tree()
    if sp_tree is None:
        return []

    view = _HistoricalView(hyp_state)

    def _eval(cond: Condition, _v: Any = view) -> bool:
        return cond.evaluate(_v)  # type: ignore[arg-type]

    attributions = attribute(sp_tree, _eval)
    enablers: list[EnablingCondition] = []
    for attr in attributions:
        attr_tag = _condition_tag_name(attr.condition)
        if attr_tag is None or attr_tag == cause_tag:
            continue
        held_since = _find_last_transition_scan(history, attr_tag, latest_scan + 1)
        enablers.append(
            EnablingCondition(
                tag_name=attr_tag,
                value=hyp_state.tags.get(attr_tag),
                held_since_scan=held_since,
            )
        )
    return enablers
