from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.sp_tree import attribute, evaluate_sp

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
    from pyrung.core.rung import Rung
    from pyrung.core.rung_firings import RungFiringTimelines
    from pyrung.core.tag import Tag


def _get_tag_name(tag: Tag | str) -> str:
    return tag if isinstance(tag, str) else tag.name


def _rung_writes_value_when_enabled(
    rung: Rung,
    tag_name: str,
    value: Any,
) -> bool:
    """Check if *rung* would write *value* to *tag_name* when its conditions are TRUE.

    Handles the three core coil types:
    - ``LatchInstruction`` → writes ``True``
    - ``ResetInstruction`` → writes ``tag.default`` (``False`` for Bool)
    - ``OutInstruction`` → writes ``True`` when enabled
    """
    from pyrung.core.instruction.coils import (
        LatchInstruction,
        OutInstruction,
        ResetInstruction,
    )
    from pyrung.core.tag import ImmediateRef
    from pyrung.core.tag import Tag as TagClass

    for instr in rung._instructions:
        target = getattr(instr, "target", None)
        if target is None:
            continue

        # Resolve ImmediateRef wrappers
        raw_target = target
        if isinstance(raw_target, ImmediateRef):
            raw_target = object.__getattribute__(raw_target, "value")

        if not isinstance(raw_target, TagClass):
            continue

        if raw_target.name != tag_name:
            continue

        if isinstance(instr, LatchInstruction):
            return value is True or value == True  # noqa: E712
        if isinstance(instr, ResetInstruction):
            return value == raw_target.default
        if isinstance(instr, OutInstruction):
            return value is True or value == True  # noqa: E712

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


def projected_cause(
    logic: list[Rung],
    history: History,
    tag: Tag | str,
    to_value: Any,
    pdg: ProgramGraph,
    assume: dict[str, Any] | None = None,
    *,
    timelines: RungFiringTimelines | None = None,
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

    # Find candidate rungs: those whose instructions would produce to_value
    candidate_rungs: list[tuple[int, Rung]] = []
    for node_idx in writer_indices:
        node = pdg.rung_nodes[node_idx]
        rung_idx = node.rung_index
        if rung_idx < len(logic):
            rung = logic[rung_idx]
            if _rung_writes_value_when_enabled(rung, tag_name, to_value):
                candidate_rungs.append((rung_idx, rung))

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

    for rung_idx, rung in candidate_rungs:
        sp_tree = rung.sp_tree()

        if sp_tree is None:
            # Unconditional rung — trivially reachable
            steps = [
                ChainStep(
                    transition=effect_transition,
                    rung_index=rung_idx,
                    proximate_causes=(),
                    enabling_conditions=(),
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
                proximate_causes=tuple(proximate),
                enabling_conditions=tuple(enabling),
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


def projected_effect(
    logic: list[Rung],
    history: History,
    tag: Tag | str,
    from_value: Any,
    pdg: ProgramGraph,
    assume: dict[str, Any] | None = None,
) -> CausalChain:
    """Build a projected forward chain: what would happen if *tag*
    transitioned from *from_value*?

    Performs what-if analysis without mutating state. For Bool tags,
    the transition is ``from_value → not from_value``.

    Returns ``mode='projected'`` (possibly with empty steps for dead-end
    cases where nothing reads the tag), or ``mode='unreachable'`` if the
    trigger transition itself can't be reached.

    Args:
        logic: The program's rung list.
        history: The runner's History instance.
        tag: The tag (or tag name) to analyze.
        from_value: The value the tag would transition FROM (for Bool,
            the TO value is inferred as ``not from_value``).
        pdg: The program's static dependency graph.

    Returns:
        A ``CausalChain``.  Never returns ``None``.
    """
    tag_name = _get_tag_name(tag)

    # Infer the TO value (for Bool, flip)
    if isinstance(from_value, bool):
        to_value = not from_value
    else:
        # Non-Bool: can't infer TO — return unreachable
        return CausalChain(
            effect=Transition(tag_name, 0, from_value, from_value),
            mode="unreachable",
        )

    latest_scan = history.newest_scan_id
    state = history.at(latest_scan)

    # Apply assumption overrides to the state snapshot
    if assume:
        state = state.with_tags(assume)

    # Build the hypothetical transition
    cause_transition = Transition(
        tag_name=tag_name,
        scan_id=latest_scan,
        from_value=from_value,
        to_value=to_value,
    )

    # Check trigger reachability: is the from_→to_ transition itself
    # achievable? If the tag is at a different value than from_, the
    # trigger is not applicable from the current state.
    current_value = state.tags.get(tag_name)
    if current_value != from_value:
        # Trigger doesn't match current state — check if the trigger
        # is reachable via a projected cause walk
        trigger_chain = projected_cause(logic, history, tag, from_value, pdg, assume=assume)
        if trigger_chain.mode == "unreachable":
            return CausalChain(
                effect=cause_transition,
                mode="unreachable",
                blockers=trigger_chain.blockers,
            )

    steps: list[ChainStep] = []
    seen_effects: set[str] = {tag_name}

    # Frontier: tags whose effects we're tracing. Maps tag_name → Transition
    frontier: dict[str, Transition] = {tag_name: cause_transition}

    # Walk all rungs once per frontier expansion (single hypothetical scan)
    changed = True
    iterations = 0
    max_iterations = 10  # cap recursion depth for single-scan projection

    while changed and iterations < max_iterations:
        changed = False
        iterations += 1

        for rung_idx, rung in enumerate(logic):
            sp_tree = rung.sp_tree()
            if sp_tree is None:
                continue

            for cause_tag, cause_trans in list(frontier.items()):
                # Build views: hypothetical (with cause_tag at new value)
                # vs counterfactual (cause_tag at old value)
                hyp_view = _CounterfactualView(state, cause_tag, cause_trans.to_value)
                cf_view = _CounterfactualView(state, cause_tag, cause_trans.from_value)

                def _eval_hyp(cond: Condition, _v: Any = hyp_view) -> bool:
                    return cond.evaluate(_v)  # type: ignore[arg-type]

                def _eval_cf(cond: Condition, _v: Any = cf_view) -> bool:
                    return cond.evaluate(_v)  # type: ignore[arg-type]

                hyp_result = evaluate_sp(sp_tree, _eval_hyp)
                cf_result = evaluate_sp(sp_tree, _eval_cf)

                if hyp_result == cf_result:
                    continue  # cause_tag is not load-bearing for this rung

                # The transition changes this rung's outcome.
                # Determine what tags this rung writes.
                node = None
                for _ni, n in enumerate(pdg.rung_nodes):
                    if n.rung_index == rung_idx and not n.branch_path:
                        node = n
                        break

                if node is None:
                    continue

                for written_tag in node.writes:
                    if written_tag in seen_effects:
                        continue

                    seen_effects.add(written_tag)
                    changed = True

                    # Determine the hypothetical new value for the written tag
                    written_current = state.tags.get(written_tag)
                    written_new = _infer_written_value(rung, written_tag, hyp_result)
                    if written_new is None:
                        written_new = (
                            not written_current
                            if isinstance(written_current, bool)
                            else written_current
                        )

                    effect_trans = Transition(
                        tag_name=written_tag,
                        scan_id=latest_scan,
                        from_value=written_current,
                        to_value=written_new,
                    )

                    # Get enabling conditions via attribution
                    attributions = attribute(sp_tree, _eval_hyp)
                    enabling: list[EnablingCondition] = []
                    for attr in attributions:
                        attr_tag = _condition_tag_name(attr.condition)
                        if attr_tag is None or attr_tag == cause_tag:
                            continue
                        held_since = _find_last_transition_scan(history, attr_tag, latest_scan + 1)
                        enabling.append(
                            EnablingCondition(
                                tag_name=attr_tag,
                                value=state.tags.get(attr_tag),
                                held_since_scan=held_since,
                            )
                        )

                    steps.append(
                        ChainStep(
                            transition=effect_trans,
                            rung_index=rung_idx,
                            proximate_causes=(cause_trans,),
                            enabling_conditions=tuple(enabling),
                        )
                    )

                    frontier[written_tag] = effect_trans

                # Only count first matching frontier tag per rung
                break

    # Empty steps = dead-end (nothing reads the tag), still mode='projected'
    return CausalChain(
        effect=cause_transition,
        mode="projected",
        steps=steps,
    )


def _infer_written_value(rung: Rung, tag_name: str, rung_enabled: bool) -> Any | None:
    """Infer what value *rung* would write to *tag_name* given *rung_enabled*.

    Returns ``None`` if the value can't be determined (instruction doesn't
    write to this tag, or instruction type isn't recognized).
    """
    from pyrung.core.instruction.coils import (
        LatchInstruction,
        OutInstruction,
        ResetInstruction,
    )
    from pyrung.core.tag import ImmediateRef
    from pyrung.core.tag import Tag as TagClass

    for instr in rung._instructions:
        target = getattr(instr, "target", None)
        if target is None:
            continue

        raw_target = target
        if isinstance(raw_target, ImmediateRef):
            raw_target = object.__getattribute__(raw_target, "value")

        if not isinstance(raw_target, TagClass):
            continue

        if raw_target.name != tag_name:
            continue

        if isinstance(instr, LatchInstruction):
            return True if rung_enabled else None  # latch is no-op when disabled
        if isinstance(instr, ResetInstruction):
            return raw_target.default if rung_enabled else None
        if isinstance(instr, OutInstruction):
            return rung_enabled  # out writes True/False based on enabled

    return None
