from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pdg import resolve_rung
from pyrung.core.analysis.sp_tree import attribute, evaluate_sp
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
    CausalChain,
    ChainStep,
    EnablingCondition,
    Transition,
)
from .support import (
    _collect_sp_leaves,
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
    ctx = ScanContext(state)
    with ctx.capturing_rung(rung_idx):
        rung.execute(ctx, enabled=True)
    writes = ctx._rung_firings.get(rung_idx, {})
    return writes.get(tag_name) == value


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
    candidate_rungs: list[tuple[int, Rung, str | None]] = []
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
            candidate_rungs.append((node.rung_index, rung, node.subroutine))

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

    for rung_idx, rung, sub_name in candidate_rungs:
        sp_tree = rung.sp_tree()

        if sp_tree is None:
            # Unconditional rung — trivially reachable
            steps = [
                ChainStep(
                    transition=effect_transition,
                    rung_index=rung_idx,
                    triggers=(),
                    enablers=(),
                    subroutine=sub_name,
                )
            ]
            if best_steps is None:
                best_steps = steps
                best_proximate = []
            continue

        # Collect ALL leaf conditions from the SP tree.  Unlike the
        # retrospective walk (which uses four-rule attribution to find
        # what mattered for the *current* evaluation), the projected walk
        # needs every contact because we're asking what would need to be
        # true for the rung to fire.
        view = _HistoricalView(state)

        def _eval(cond: Condition, _v: Any = view) -> bool:
            return cond.evaluate(_v)  # type: ignore[arg-type]

        leaves = _collect_sp_leaves(sp_tree)

        proximate: list[Transition] = []
        enabling: list[EnablingCondition] = []
        rung_blockers: list[BlockingCondition] = []
        seen_tags: set[str] = set()

        for leaf in leaves:
            cond_tag = _condition_tag_name(leaf.condition)
            if cond_tag is None or cond_tag in seen_tags:
                continue
            seen_tags.add(cond_tag)

            cond_value = state.tags.get(cond_tag)
            leaf_result = _eval(leaf.condition)

            if leaf_result:
                # Contact already evaluates TRUE → enabling
                enabling.append(
                    EnablingCondition(
                        tag_name=cond_tag,
                        value=cond_value,
                        held_since_scan=_find_last_transition_scan(
                            history, cond_tag, latest_scan + 1
                        ),
                    )
                )
            else:
                # Contact evaluates FALSE → needs to transition
                needed_value = not cond_value if cond_value is not None else True

                # Check reachability: has this tag ever transitioned to
                # the needed value in recorded history?  Tags in the
                # assume dict are reachable by stipulation.
                is_input = not pdg.writers_of.get(cond_tag, frozenset())
                reachable = (assume and cond_tag in assume) or _has_observed_transition(
                    history,
                    cond_tag,
                    needed_value,
                    timelines=timelines,
                    pdg=pdg,
                )

                if reachable or is_input:
                    proximate.append(
                        Transition(
                            tag_name=cond_tag,
                            scan_id=latest_scan,
                            from_value=cond_value,
                            to_value=needed_value,
                        )
                    )
                else:
                    reason = (
                        BlockerReason.NO_OBSERVED_TRANSITION
                        if is_input
                        else BlockerReason.BLOCKED_UPSTREAM
                    )
                    rung_blockers.append(
                        BlockingCondition(
                            rung_index=rung_idx,
                            blocked_tag=cond_tag,
                            needed_value=needed_value,
                            reason=reason,
                        )
                    )

        if not rung_blockers:
            # All conditions are reachable — viable path
            step = ChainStep(
                transition=effect_transition,
                rung_index=rung_idx,
                triggers=tuple(proximate),
                enablers=tuple(enabling),
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
