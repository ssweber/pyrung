from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.sp_tree import attribute, evaluate_sp

from .history import (
    _find_last_transition_scan,
    _find_recent_transition,
    _find_transition,
    _find_transition_at_scan,
)
from .models import CausalChain, ChainStep, EnablingCondition, Transition
from .support import (
    _collect_sp_leaves,
    _condition_tag_name,
    _counterfactual_changes_outcome,
    _HistoricalView,
)

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.condition import Condition
    from pyrung.core.history import History
    from pyrung.core.rung import Rung
    from pyrung.core.rung_firings import RungFiringTimelines
    from pyrung.core.tag import Tag


def recorded_cause(
    logic: list[Rung],
    history: History,
    rung_firings_fn: Any,  # Callable[[int], PMap]
    tag: Tag | str,
    scan_id: int | None = None,
    *,
    pdg: ProgramGraph | None = None,
    timelines: RungFiringTimelines | None = None,
    state_in_cache_fn: Any = None,  # Callable[[int], bool] | None
) -> CausalChain | None:
    """Build a retrospective causal chain for a tag transition.

    Args:
        logic: The program's rung list (``plc._logic``).
        history: The runner's ``History`` instance.
        rung_firings_fn: Callable that returns ``PMap[int, PMap[str, Any]]``
            for a given scan_id.
        tag: The tag (or tag name) whose transition to explain.
        scan_id: Specific scan to examine, or ``None`` for most recent.
        pdg: Static program graph used as a fallback when the firing
            log has been PDG-filtered.  Terminal outputs (tags no rung
            reads) have their rung-firing writes dropped from the log;
            this fallback recovers the writing rung by evaluating each
            candidate from ``writers_of`` against the historical state.
        timelines: Per-rung firing timelines for O(log S) transition
            detection without state reads.

    Returns:
        A ``CausalChain``, or ``None`` if no transition was found.
    """
    tag_name = tag if isinstance(tag, str) else tag.name

    transition = _find_transition(
        history,
        tag_name,
        scan_id,
        timelines=timelines,
        pdg=pdg,
    )
    if transition is None:
        return None

    steps: list[ChainStep] = []
    conjunctive_roots: list[Transition] = []
    ambiguous_roots: list[Transition] = []
    visited: set[str] = set()

    _walk_backward(
        logic=logic,
        history=history,
        rung_firings_fn=rung_firings_fn,
        transition=transition,
        steps=steps,
        conjunctive_roots=conjunctive_roots,
        ambiguous_roots=ambiguous_roots,
        visited=visited,
        pdg=pdg,
        timelines=timelines,
        state_in_cache_fn=state_in_cache_fn,
    )

    return CausalChain(
        effect=transition,
        mode="recorded",
        steps=steps,
        conjunctive_roots=conjunctive_roots,
        ambiguous_roots=ambiguous_roots,
    )


def _walk_backward(
    *,
    logic: list[Rung],
    history: History,
    rung_firings_fn: Any,
    transition: Transition,
    steps: list[ChainStep],
    conjunctive_roots: list[Transition],
    ambiguous_roots: list[Transition],
    visited: set[str],
    pdg: ProgramGraph | None = None,
    timelines: RungFiringTimelines | None = None,
    state_in_cache_fn: Any = None,  # Callable[[int], bool] | None
) -> None:
    """Recursive backward walk from a single transition."""
    tag_name = transition.tag_name
    scan_id = transition.scan_id

    if tag_name in visited:
        return  # cycle guard
    visited.add(tag_name)

    # Find rungs that wrote this tag at this scan
    firings = rung_firings_fn(scan_id)
    writing_rungs: list[int] = []
    for rung_idx in firings:
        writes = firings[rung_idx]
        if tag_name in writes and writes[tag_name] == transition.to_value:
            writing_rungs.append(rung_idx)

    if not writing_rungs and pdg is not None:
        # The firing log has been PDG-filtered — writes to tags no rung
        # reads never landed.  Recover the writer by evaluating each
        # static candidate from ``writers_of`` against the historical
        # state at ``scan_id``.  A rung whose SP tree was true at that
        # scan is treated as the writer; unconditional rungs (no SP
        # tree) always qualify.
        writing_rungs = _fallback_writers_from_pdg(
            pdg=pdg,
            logic=logic,
            history=history,
            tag_name=tag_name,
            scan_id=scan_id,
        )

    if not writing_rungs:
        # No rung wrote this value — root cause (external input / patch)
        conjunctive_roots.append(transition)
        return

    for rung_idx in writing_rungs:
        rung = logic[rung_idx]
        sp_tree = rung.sp_tree()

        if sp_tree is None:
            # Unconditional rung — no conditions to attribute
            steps.append(
                ChainStep(
                    transition=transition,
                    rung_index=rung_idx,
                    proximate_causes=(),
                    enabling_conditions=(),
                )
            )
            conjunctive_roots.append(transition)
            continue

        # Check if state is cached for full-fidelity SP-tree attribution.
        cached = state_in_cache_fn is None or state_in_cache_fn(scan_id)

        if cached:
            # Full fidelity: SP-tree attribution classifies contacts as
            # proximate (transitioned) vs enabling (held steady).
            state = history.at(scan_id)
            view = _HistoricalView(state)

            def _eval(cond: Condition, _v: Any = view) -> bool:
                return cond.evaluate(_v)  # type: ignore[arg-type]

            attributions = attribute(sp_tree, _eval)

            proximate: list[Transition] = []
            enabling: list[EnablingCondition] = []

            for attr in attributions:
                cond_tag = _condition_tag_name(attr.condition)
                if cond_tag is None:
                    continue

                cond_transition = _find_recent_transition(
                    history,
                    cond_tag,
                    scan_id,
                    timelines=timelines,
                    pdg=pdg,
                )
                if cond_transition is not None:
                    proximate.append(cond_transition)
                else:
                    held_since = _find_last_transition_scan(
                        history,
                        cond_tag,
                        scan_id,
                        timelines=timelines,
                        pdg=pdg,
                    )
                    enabling.append(
                        EnablingCondition(
                            tag_name=cond_tag,
                            value=state.tags.get(cond_tag),
                            held_since_scan=held_since,
                        )
                    )

            steps.append(
                ChainStep(
                    transition=transition,
                    rung_index=rung_idx,
                    proximate_causes=tuple(proximate),
                    enabling_conditions=tuple(enabling),
                )
            )
        else:
            # Timeline-only fidelity: no state available, so no SP-tree
            # attribution.  Proximate candidates = rung contacts whose
            # writers fired at N or N-1 (timeline + structural).
            # Enabling conditions are empty (require state).
            proximate_tl: list[Transition] = []
            leaves = _collect_sp_leaves(sp_tree)
            for leaf in leaves:
                cond_tag = _condition_tag_name(leaf.condition)
                if cond_tag is None:
                    continue
                cond_transition = _find_recent_transition(
                    history,
                    cond_tag,
                    scan_id,
                    timelines=timelines,
                    pdg=pdg,
                )
                if cond_transition is not None:
                    proximate_tl.append(cond_transition)

            steps.append(
                ChainStep(
                    transition=transition,
                    rung_index=rung_idx,
                    proximate_causes=tuple(proximate_tl),
                    enabling_conditions=(),
                    fidelity="timeline",
                )
            )
            proximate = proximate_tl  # for recursion check below

        if not proximate:
            # All contacts were enabling — the transition itself is a root
            conjunctive_roots.append(transition)
        else:
            # Recurse on each proximate cause
            for p in proximate:
                _walk_backward(
                    logic=logic,
                    history=history,
                    rung_firings_fn=rung_firings_fn,
                    transition=p,
                    steps=steps,
                    conjunctive_roots=conjunctive_roots,
                    ambiguous_roots=ambiguous_roots,
                    visited=visited,
                    pdg=pdg,
                    timelines=timelines,
                    state_in_cache_fn=state_in_cache_fn,
                )


def _fallback_writers_from_pdg(
    *,
    pdg: ProgramGraph,
    logic: list[Rung],
    history: History,
    tag_name: str,
    scan_id: int,
) -> list[int]:
    """Recover candidate writers of ``tag_name`` at ``scan_id`` from the PDG.

    Used when the firing log has dropped the write under PDG filtering —
    the structural ``writers_of`` set tells us which rungs *can* write
    the tag; re-evaluating each rung's SP tree against the historical
    state narrows to those that *did* fire at ``scan_id``.
    """
    candidates = pdg.writers_of.get(tag_name, frozenset())
    if not candidates:
        return []
    state = history.at(scan_id)
    view = _HistoricalView(state)

    def _eval(cond: Condition, _v: Any = view) -> bool:
        return cond.evaluate(_v)  # type: ignore[arg-type]

    writers: list[int] = []
    for rung_idx in sorted(candidates):
        rung = logic[rung_idx]
        sp_tree = rung.sp_tree()
        if sp_tree is None or evaluate_sp(sp_tree, _eval):
            writers.append(rung_idx)
    return writers


# ---------------------------------------------------------------------------
# Counterfactual SP evaluation
# ---------------------------------------------------------------------------


def recorded_effect(
    logic: list[Rung],
    history: History,
    rung_firings_fn: Any,  # Callable[[int], PMap]
    tag: Tag | str,
    scan_id: int | None = None,
    *,
    steady_state_k: int = 3,
    max_scans: int = 1000,
    pdg: ProgramGraph | None = None,
    timelines: RungFiringTimelines | None = None,
) -> CausalChain | None:
    """Build a retrospective forward chain from a tag transition.

    Walks history forward from the transition, using counterfactual SP
    evaluation to identify which downstream tags were causally affected.

    Args:
        logic: The program's rung list.
        history: The runner's History instance.
        rung_firings_fn: Returns ``PMap[int, PMap[str, Any]]`` for a scan_id.
        tag: The tag (or tag name) whose downstream effects to trace.
        scan_id: Specific scan of the transition, or ``None`` for most recent.
        steady_state_k: Stop after this many consecutive scans with no new
            tags entering the chain (default 3).
        max_scans: Hard cap on scans to walk forward (default 1000).
        pdg: Static program graph used to widen the per-scan candidate
            rung set when firings are PDG-filtered.  Rungs that fired
            but wrote only unconsumed tags are missing from the log;
            ``readers_of`` recovers them by flagging rungs that read any
            frontier tag.  Downstream tag values are resolved via
            history regardless of whether the rung was in the log.
        timelines: Per-rung firing timelines for O(log S) transition
            detection without state reads.
    """
    tag_name = tag if isinstance(tag, str) else tag.name

    transition = _find_transition(
        history,
        tag_name,
        scan_id,
        timelines=timelines,
        pdg=pdg,
    )
    if transition is None:
        return None

    # Frontier: tags whose downstream effects we're still tracing.
    # Maps tag_name → Transition.
    frontier: dict[str, Transition] = {tag_name: transition}

    steps: list[ChainStep] = []
    seen_effects: set[str] = {tag_name}  # don't re-add the cause itself

    ids = list(history.scan_ids())
    try:
        start_idx = ids.index(transition.scan_id)
    except ValueError:
        return None

    consecutive_empty = 0

    for scan_offset in range(len(ids) - start_idx):
        if scan_offset >= max_scans:
            break

        current_scan = ids[start_idx + scan_offset]
        firings = rung_firings_fn(current_scan)
        new_effects_this_scan = False

        # Iterate rungs in index order: within a single scan the frontier
        # grows as earlier rungs produce effects (e.g. Rung 0 writes
        # Sts_FaultTripped, then Rung 2 reads it), and the per-rung
        # reads-vs-frontier check must be against the *current* frontier.
        # Rungs not in ``firings`` may have fired with all writes dropped
        # by PDG filtering — we consider them only if they statically read
        # some frontier tag, and we synthesize candidate writes from the
        # PDG node so the downstream history lookup can pick up real
        # transitions.
        rung_count = len(logic) if pdg is not None else 0
        rung_range = range(rung_count) if pdg is not None else sorted(firings.keys())

        for rung_idx in rung_range:
            rung = logic[rung_idx]
            if rung_idx in firings:
                writes: Any = firings[rung_idx]
                if not writes and pdg is not None:
                    # Rung fired but filter emptied its writes — synthesize
                    # candidate written tags from the PDG so the history
                    # lookup below can recover real transitions.
                    writes = pdg.rung_nodes[rung_idx].writes
            elif pdg is not None:
                node = pdg.rung_nodes[rung_idx]
                reads = node.condition_reads | node.data_reads
                if reads.isdisjoint(frontier):
                    continue
                writes = node.writes
            else:
                continue
            sp_tree = rung.sp_tree()

            if sp_tree is None:
                continue

            state = history.at(current_scan)

            # Check each frontier tag for counterfactual relevance
            for cause_tag, cause_trans in list(frontier.items()):
                if not _counterfactual_changes_outcome(
                    sp_tree, state, cause_tag, cause_trans.from_value
                ):
                    continue

                # This frontier tag was load-bearing for this rung.
                # Record each new tag transition the rung wrote.
                for written_tag in writes:
                    if written_tag in seen_effects:
                        continue

                    effect_trans = _find_transition_at_scan(
                        history,
                        written_tag,
                        current_scan,
                        timelines=timelines,
                        pdg=pdg,
                    )
                    if effect_trans is None:
                        continue

                    seen_effects.add(written_tag)
                    new_effects_this_scan = True

                    # Get enabling conditions via attribution
                    view = _HistoricalView(state)

                    def _eval(cond: Condition, _v: Any = view) -> bool:
                        return cond.evaluate(_v)  # type: ignore[arg-type]

                    attributions = attribute(sp_tree, _eval)
                    enabling: list[EnablingCondition] = []
                    for attr in attributions:
                        attr_tag = _condition_tag_name(attr.condition)
                        if attr_tag is None or attr_tag == cause_tag:
                            continue
                        held_since = _find_last_transition_scan(
                            history,
                            attr_tag,
                            current_scan,
                            timelines=timelines,
                            pdg=pdg,
                        )
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

                    # Add to frontier for further propagation
                    frontier[written_tag] = effect_trans

                # Only count first matching frontier tag per rung to avoid
                # duplicating steps.
                break

        if new_effects_this_scan:
            consecutive_empty = 0
        else:
            consecutive_empty += 1
            if consecutive_empty >= steady_state_k:
                break

    return CausalChain(
        effect=transition,
        mode="recorded",
        steps=steps,
    )


# ---------------------------------------------------------------------------
# Projected helpers
# ---------------------------------------------------------------------------
