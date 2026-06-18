from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pdg import resolve_rung
from pyrung.core.analysis.sp_tree import attribute, evaluate_sp
from pyrung.core.context import RungId

from .crossings_recorded import recorded_read_changes
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
    _TimelineView,
)

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.condition import Condition
    from pyrung.core.history import History
    from pyrung.core.program import Program
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
    program: Program | None = None,
    scan_log: Any = None,  # ScanLog | None
    initial_tags: Any = None,  # Mapping[str, Any] for timeline-resolved attribution
    node_firings_fn: Any = None,  # Callable[[int], PMap[RungId, PMap]] | None
    node_views_fn: Any = None,  # Callable[[int], dict[RungId, ConditionView]] | None
    node_reads_fn: Any = None,  # Callable[[int], dict[RungId, set[str]]] | None
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
        program: Full Program for subroutine rung resolution.

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
        scan_log=scan_log,
        initial_tags=initial_tags,
    )
    if transition is None:
        return None

    steps: list[ChainStep] = []
    conjunctive_roots: list[Transition] = []
    ambiguous_roots: list[Transition] = []
    visited: set[str] = set()
    # Per-cause() memoization of the on-demand replay views, keyed by
    # scan.  The backward walk revisits the same scan for each writer at
    # a transition, and across recursion may revisit a scan repeatedly;
    # one replay per distinct scan is enough.
    node_views_cache: dict[int, dict[RungId, Any]] = {}
    # Companion cache for the Tier-2 per-node data reads — same replay, same
    # per-scan memoization as ``node_views_cache``.
    node_reads_cache: dict[int, dict[RungId, Any]] = {}

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
        program=program,
        scan_log=scan_log,
        initial_tags=initial_tags,
        node_firings_fn=node_firings_fn,
        node_views_fn=node_views_fn,
        node_views_cache=node_views_cache,
        node_reads_fn=node_reads_fn,
        node_reads_cache=node_reads_cache,
    )

    return CausalChain(
        effect=transition,
        mode="recorded",
        steps=steps,
        conjunctive_roots=conjunctive_roots,
        ambiguous_roots=ambiguous_roots,
    )


def _writer_footprint(
    pdg: ProgramGraph, tag_name: str, rung_idx: int, sub_name: str | None
) -> frozenset[str]:
    """The data-read footprint of the resolved writer ``(rung_idx, sub_name)``.

    ``writers_of[tag]`` already narrows to nodes that write *tag_name*, so a
    main-body, branch, or subroutine writer is matched the same way — on
    ``(rung_index, subroutine)``.  When a rung writes the tag from more than one
    branch (each a distinct PDG node with its own ``data_reads``), the recorded
    firing log rolls the branches up under the main rung, so we can't tell which
    branch fired — the footprints are **unioned** (over-approximate) so the
    read-diff never misses the operand the firing branch actually read.
    """
    footprint: set[str] = set()
    for n_idx in pdg.writers_of.get(tag_name, frozenset()):
        node = pdg.rung_nodes[n_idx]
        if node.rung_index == rung_idx and node.subroutine == sub_name:
            footprint |= node.data_reads
    return frozenset(footprint)


def _rung_static_reads(pdg: ProgramGraph, rung_idx: int, sub_name: str | None) -> frozenset[str]:
    """Union of every node's static data reads at ``(rung_idx, sub_name)``.

    The operands the static analysis *could* enumerate for the whole rung (all
    branches, all instructions).  Captured reads outside this set are
    runtime-resolved indirect addresses the PDG dropped — Tier 2 keeps them.
    """
    reads: set[str] = set()
    for node in pdg.rung_nodes:
        if node.rung_index == rung_idx and node.subroutine == sub_name:
            reads |= node.data_reads
    return frozenset(reads)


def _node_reads_at(
    scan_id: int,
    node_reads_fn: Any,
    node_reads_cache: dict[int, dict[RungId, Any]] | None,
) -> dict[RungId, Any] | None:
    """Per-node captured data reads for ``scan_id`` (Crossings Tier 2), memoized.

    Returns ``None`` when no interpreted replay is wired (projected/legacy
    callers); an empty map when the scan has no replay (logic-list PLC with no
    Program, or a scan out of replay range).  Mirrors ``_writer_fire_view``'s
    per-scan memoization so one replay per distinct scan is enough.
    """
    if node_reads_fn is None:
        return None
    if node_reads_cache is not None and scan_id in node_reads_cache:
        return node_reads_cache[scan_id]
    reads = node_reads_fn(scan_id) or {}
    if node_reads_cache is not None:
        node_reads_cache[scan_id] = reads
    return reads


def _cross_opaque_data_reads(
    *,
    pdg: ProgramGraph | None,
    history: History,
    tag_name: str,
    rung_idx: int,
    sub_name: str | None,
    scan_id: int,
    timelines: RungFiringTimelines | None,
    scan_log: Any,
    initial_tags: Any,
    fire_view: Any = None,
    node_reads_fn: Any = None,
    node_reads_cache: dict[int, dict[RungId, Any]] | None = None,
) -> tuple[tuple[Transition, ...], tuple[EnablingCondition, ...]] | None:
    """Cross an opaque writer via the recorded read-diff (Crossings Phase 1/Tier 2).

    Returns ``(triggers, enablers)`` derived from the writer's observed data
    reads — operands that *changed* this scan are triggers, operands that are
    merely *non-zero now* are enablers — or ``None`` when the writer has no
    crossable data reads or nothing in the footprint changed or is non-zero
    (the caller keeps its existing bare-root behaviour).
    """
    if pdg is None:
        return None
    static_footprint = _writer_footprint(pdg, tag_name, rung_idx, sub_name)
    if not static_footprint:
        # Not a data writer with crossable operands (e.g. a timer/counter, or a
        # literal-source copy).  Tier 1 returns here; the captured reads of such
        # an instruction are internal state (accumulator/preset) the PDG rightly
        # excludes, so the Tier-2 refinement only applies once there is at least
        # one statically-known operand (always true for an indirect ref — the
        # pointer itself is a static read).
        return None
    captured = _node_reads_at(scan_id, node_reads_fn, node_reads_cache)
    node_reads = captured.get(RungId(sub_name, rung_idx)) if captured is not None else None
    if node_reads:
        # Tier 2: scope the operands the writer *actually* read at fire time to
        # this tag.  Keep the fired reads the static analysis attributes to this
        # writer (``& static_footprint`` — drops a non-firing branch's operands,
        # so gate-precise) and add reads the static analysis could not enumerate
        # at all (``- rung_static`` — runtime-resolved indirect addresses).
        # Sound: every true read of the writer is retained, and nothing the
        # writer never read is introduced; never less precise than the static
        # footprint alone.
        rung_static = _rung_static_reads(pdg, rung_idx, sub_name)
        footprint = frozenset((node_reads & static_footprint) | (node_reads - rung_static))
    else:
        # Tier 1 fallback: no interpreted replay (no Program / out of replay
        # range / nothing captured for this writer node — e.g. literal source).
        footprint = static_footprint
    if not footprint:
        return None
    # The operand value that matters is what the writer *read* when its rung
    # fired — not end-of-scan state, which would record an operand reset later
    # the same scan (consume-on-read) as the change.  Mirror the contact split's
    # at-fire-time view (see _writer_fire_view); fall back to end-of-scan only
    # when no replay produced a view.
    read_values = (
        {tag: fire_view.get_tag(tag) for tag in footprint} if fire_view is not None else None
    )
    diff = recorded_read_changes(history, footprint, scan_id, read_values=read_values)
    if diff.empty:
        return None
    changed_tags = {t for t, _before, _after in diff.changed}
    triggers = tuple(Transition(t, scan_id, before, after) for (t, before, after) in diff.changed)
    state = history.at(scan_id)
    enablers = tuple(
        EnablingCondition(
            tag_name=t,
            value=state.tags.get(t),
            held_since_scan=_find_last_transition_scan(
                history,
                t,
                scan_id,
                timelines=timelines,
                pdg=pdg,
                scan_log=scan_log,
                initial_tags=initial_tags,
            ),
        )
        for t in diff.nonzero_now
        if t not in changed_tags
    )
    return triggers, enablers


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
    program: Program | None = None,
    scan_log: Any = None,
    initial_tags: Any = None,
    node_firings_fn: Any = None,
    node_views_fn: Any = None,
    node_views_cache: dict[int, dict[RungId, Any]] | None = None,
    node_reads_fn: Any = None,
    node_reads_cache: dict[int, dict[RungId, Any]] | None = None,
) -> None:
    """Recursive backward walk from a single transition."""
    tag_name = transition.tag_name
    scan_id = transition.scan_id

    if tag_name in visited:
        return  # cycle guard
    visited.add(tag_name)

    # Resolved writers: (rung_index, rung, subroutine).  The node-level
    # firing timeline names the precise subroutine writer rung; the
    # main-rung firing log names main-scope (and branch) writers.
    resolved_writers = _recorded_writers_from_firings(
        pdg=pdg,
        program=program,
        logic=logic,
        history=history,
        rung_firings_fn=rung_firings_fn,
        node_firings_fn=node_firings_fn,
        tag_name=tag_name,
        scan_id=scan_id,
        to_value=transition.to_value,
    )

    if not resolved_writers and pdg is not None:
        # The firing log has been PDG-filtered — writes to tags no rung
        # reads never landed.  Recover the writer by evaluating each
        # static candidate from ``writers_of`` against the historical
        # state at ``scan_id``.  A rung whose SP tree was true at that
        # scan is treated as the writer; unconditional rungs (no SP
        # tree) always qualify.
        resolved_writers = _fallback_writers_from_pdg(
            pdg=pdg,
            program=program,
            logic=logic,
            history=history,
            tag_name=tag_name,
            scan_id=scan_id,
        )

    if not resolved_writers:
        # No rung wrote this value — root cause (external input / patch)
        conjunctive_roots.append(transition)
        return

    for rung_idx, rung, sub_name in resolved_writers:
        sp_tree = rung.sp_tree()

        # At-fire-time view: for a subroutine writer, the rung's contacts
        # may have flipped later the same scan (a command gate consumed
        # downstream).  Reconstruct the writer rung's entry-time
        # ConditionView via on-demand replay so triggers/enablers reflect
        # what the rung *actually read* — not end-of-scan state.
        fire_view = _writer_fire_view(
            sub_name,
            rung_idx,
            scan_id,
            node_views_fn=node_views_fn,
            node_views_cache=node_views_cache,
        )

        if sp_tree is None:
            # Unconditional rung — no conditions to attribute.  Phase 1: cross
            # the writer's data reads (calc/sum/copy operands) so the walk
            # continues from the changed/non-zero operands instead of stopping
            # at the opaque written tag.
            crossed = _cross_opaque_data_reads(
                pdg=pdg,
                history=history,
                tag_name=tag_name,
                rung_idx=rung_idx,
                sub_name=sub_name,
                scan_id=scan_id,
                timelines=timelines,
                scan_log=scan_log,
                initial_tags=initial_tags,
                fire_view=fire_view,
                node_reads_fn=node_reads_fn,
                node_reads_cache=node_reads_cache,
            )
            if crossed is not None:
                triggers, enablers = crossed
                step = ChainStep(
                    transition=transition,
                    rung_index=rung_idx,
                    triggers=triggers,
                    enablers=enablers,
                    subroutine=sub_name,
                )
                steps.append(_with_caller_gate(step, sub_name, fire_view, pdg, program))
                for p in triggers:
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
                        program=program,
                        scan_log=scan_log,
                        initial_tags=initial_tags,
                        node_firings_fn=node_firings_fn,
                        node_views_fn=node_views_fn,
                        node_views_cache=node_views_cache,
                        node_reads_fn=node_reads_fn,
                        node_reads_cache=node_reads_cache,
                    )
                continue
            step = ChainStep(
                transition=transition,
                rung_index=rung_idx,
                triggers=(),
                enablers=(),
                subroutine=sub_name,
            )
            steps.append(_with_caller_gate(step, sub_name, fire_view, pdg, program))
            conjunctive_roots.append(transition)
            continue

        # Check if state is cached for full-fidelity SP-tree attribution.
        cached = state_in_cache_fn is None or state_in_cache_fn(scan_id)

        if cached:
            # Full fidelity: SP-tree attribution classifies contacts as
            # proximate (transitioned) vs enabling (held steady).  Read
            # against the writer's at-fire-time view when available, else
            # fall back to end-of-scan state.
            state = history.at(scan_id)
            view: Any = fire_view if fire_view is not None else _HistoricalView(state)

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
                    scan_log=scan_log,
                    initial_tags=initial_tags,
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
                        scan_log=scan_log,
                        initial_tags=initial_tags,
                    )
                    enabling.append(
                        EnablingCondition(
                            tag_name=cond_tag,
                            value=(
                                view.get_tag(cond_tag)
                                if fire_view is not None
                                else state.tags.get(cond_tag)
                            ),
                            held_since_scan=held_since,
                        )
                    )

            step = ChainStep(
                transition=transition,
                rung_index=rung_idx,
                triggers=tuple(proximate),
                enablers=tuple(enabling),
                subroutine=sub_name,
            )
            steps.append(_with_caller_gate(step, sub_name, fire_view, pdg, program))
        elif initial_tags is not None and timelines is not None and pdg is not None:
            # Timeline-resolved attribution: reconstruct tag values from
            # timelines + ScanLog without expensive state replay.
            from .history import resolve_tag_at_scan

            view = _TimelineView(
                scan_id,
                timelines=timelines,
                pdg=pdg,
                scan_log=scan_log,
                initial_tags=initial_tags,
            )

            def _eval(cond: Condition, _v: Any = view) -> bool:
                return cond.evaluate(_v)  # type: ignore[arg-type]

            attributions = attribute(sp_tree, _eval)

            proximate_tl: list[Transition] = []
            enabling_tl: list[EnablingCondition] = []

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
                    scan_log=scan_log,
                    initial_tags=initial_tags,
                )
                if cond_transition is not None:
                    proximate_tl.append(cond_transition)
                else:
                    held_since = _find_last_transition_scan(
                        history,
                        cond_tag,
                        scan_id,
                        timelines=timelines,
                        pdg=pdg,
                        scan_log=scan_log,
                        initial_tags=initial_tags,
                    )
                    enabling_tl.append(
                        EnablingCondition(
                            tag_name=cond_tag,
                            value=resolve_tag_at_scan(
                                cond_tag,
                                scan_id,
                                timelines=timelines,
                                pdg=pdg,
                                scan_log=scan_log,
                                initial_tags=initial_tags,
                            ),
                            held_since_scan=held_since,
                        )
                    )

            steps.append(
                ChainStep(
                    transition=transition,
                    rung_index=rung_idx,
                    triggers=tuple(proximate_tl),
                    enablers=tuple(enabling_tl),
                    subroutine=sub_name,
                )
            )
            proximate = proximate_tl
        else:
            # Structural-only fallback: no state or timeline data for
            # full attribution.
            proximate_st: list[Transition] = []
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
                    scan_log=scan_log,
                    initial_tags=initial_tags,
                )
                if cond_transition is not None:
                    proximate_st.append(cond_transition)

            steps.append(
                ChainStep(
                    transition=transition,
                    rung_index=rung_idx,
                    triggers=tuple(proximate_st),
                    enablers=(),
                    fidelity="timeline",
                    subroutine=sub_name,
                )
            )
            proximate = proximate_st

        if not proximate:
            # Conditioned writer with no proximate cause — explained only by
            # held gate conditions (or nothing).  Phase 1: cross the writer's
            # data reads so a gated calc/sum/copy continues from its changed/
            # non-zero operands, folding them into the step alongside the held
            # gate enablers instead of dead-ending at the written tag.
            crossed = _cross_opaque_data_reads(
                pdg=pdg,
                history=history,
                tag_name=tag_name,
                rung_idx=rung_idx,
                sub_name=sub_name,
                scan_id=scan_id,
                timelines=timelines,
                scan_log=scan_log,
                initial_tags=initial_tags,
                fire_view=fire_view,
                node_reads_fn=node_reads_fn,
                node_reads_cache=node_reads_cache,
            )
            if crossed is not None:
                dr_triggers, dr_enablers = crossed
                steps[-1] = replace(
                    steps[-1],
                    triggers=steps[-1].triggers + dr_triggers,
                    enablers=steps[-1].enablers + dr_enablers,
                )
                for p in dr_triggers:
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
                        program=program,
                        scan_log=scan_log,
                        initial_tags=initial_tags,
                        node_firings_fn=node_firings_fn,
                        node_views_fn=node_views_fn,
                        node_views_cache=node_views_cache,
                        node_reads_fn=node_reads_fn,
                        node_reads_cache=node_reads_cache,
                    )
            # If a rung was explained only by held enablers, do not
            # invent the written tag as its own root; callers can fall
            # through to the enabler set as the remaining choices.
            elif not steps[-1].enablers:
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
                    program=program,
                    scan_log=scan_log,
                    initial_tags=initial_tags,
                    node_firings_fn=node_firings_fn,
                    node_views_fn=node_views_fn,
                    node_views_cache=node_views_cache,
                    node_reads_fn=node_reads_fn,
                    node_reads_cache=node_reads_cache,
                )


def _fallback_writers_from_pdg(
    *,
    pdg: ProgramGraph,
    program: Program | None,
    logic: list[Rung],
    history: History,
    tag_name: str,
    scan_id: int,
) -> list[tuple[int, Rung, str | None]]:
    """Recover candidate writers of ``tag_name`` at ``scan_id`` from the PDG.

    Used when the firing log has dropped the write under PDG filtering —
    the structural ``writers_of`` set tells us which rungs *can* write
    the tag; re-evaluating each rung's SP tree against the historical
    state narrows to those that *did* fire at ``scan_id``.

    Returns resolved ``(rung_index, rung, subroutine)`` tuples so the
    caller gets correct rung objects for subroutine and branch writers.
    """
    candidates = pdg.writers_of.get(tag_name, frozenset())
    if not candidates:
        return []
    return _semantic_writers_from_pdg(
        pdg=pdg,
        program=program,
        logic=logic,
        history=history,
        tag_name=tag_name,
        scan_id=scan_id,
        candidates=candidates,
        capture_rung_index=None,
    )


def _recorded_writers_from_firings(
    *,
    pdg: ProgramGraph | None,
    program: Program | None,
    logic: list[Rung],
    history: History,
    rung_firings_fn: Any,
    node_firings_fn: Any,
    tag_name: str,
    scan_id: int,
    to_value: Any,
) -> list[tuple[int, Rung, str | None]]:
    """Resolve recorded firing writes to semantic writer rungs.

    Two firing logs cooperate:

    - The **node-level** timeline (``node_firings_fn``) records each
      subroutine rung's own write slice, keyed by ``RungId(sub, idx)``.
      It names the precise subroutine writer directly — no end-of-scan
      SP re-evaluation, which mis-fires when a single-scan command gate
      is consumed before the scan ends.
    - The **main-rung** timeline (``rung_firings_fn``) rolls up the whole
      subtree under each top-level rung.  A main-rung firing that matches
      the value is a main-scope (or branch) writer *unless* it is just
      the rolled-up call site of a subroutine already named above.
    """
    resolved: list[tuple[int, Rung, str | None]] = []
    seen: set[tuple[str | None, int]] = set()

    # 1. Subroutine writers — named precisely from the node-level timeline.
    if node_firings_fn is not None and program is not None:
        node_firings = node_firings_fn(scan_id)
        for rung_id in sorted(node_firings, key=lambda r: (r.subroutine or "", r.rung_index)):
            if rung_id.subroutine is None:
                continue
            writes = node_firings[rung_id]
            if tag_name not in writes or writes[tag_name] != to_value:
                continue
            rung = _resolve_subroutine_rung(program, rung_id.subroutine, rung_id.rung_index)
            if rung is None:
                continue
            key = (rung_id.subroutine, rung_id.rung_index)
            if key in seen:
                continue
            seen.add(key)
            resolved.append((rung_id.rung_index, rung, rung_id.subroutine))

    # 2. Main-scope (and branch) writers — from the main-rung firing log.
    firings = rung_firings_fn(scan_id)
    main_rungs = program.rungs if program is not None else logic
    candidates = pdg.writers_of.get(tag_name, frozenset()) if pdg is not None else frozenset()

    for rung_idx in sorted(firings):
        writes = firings[rung_idx]
        if tag_name not in writes or writes[tag_name] != to_value:
            continue
        if pdg is not None and candidates:
            # A main-scope node at this capture index that writes the tag.
            # If there is none, this firing is a rolled-up subroutine call
            # site whose precise writer was already named above — skip it
            # rather than mis-naming the call-site rung.
            for w_idx, w_rung, _ in _semantic_main_writers_from_pdg(
                pdg=pdg,
                program=program,
                logic=logic,
                candidates=candidates,
                capture_rung_index=rung_idx,
            ):
                key = (None, w_idx)
                if key in seen:
                    continue
                seen.add(key)
                resolved.append((w_idx, w_rung, None))
        elif rung_idx < len(main_rungs):
            # No PDG (or no known writers): name the firing rung directly.
            key = (None, rung_idx)
            if key in seen:
                continue
            seen.add(key)
            resolved.append((rung_idx, main_rungs[rung_idx], None))

    return resolved


def _resolve_subroutine_rung(
    program: Program | None, subroutine: str, rung_index: int
) -> Rung | None:
    """Resolve a node-timeline ``RungId`` to its top-level subroutine rung.

    Subroutine rung firings are captured per top-level rung (branches roll
    up), so the writer rung is ``program.subroutines[sub][idx]``.
    """
    if program is None:
        return None
    rungs = program.subroutines.get(subroutine)
    if rungs is None or rung_index >= len(rungs):
        return None
    return rungs[rung_index]


def _semantic_main_writers_from_pdg(
    *,
    pdg: ProgramGraph,
    program: Program | None,
    logic: list[Rung],
    candidates: frozenset[int],
    capture_rung_index: int,
) -> list[tuple[int, Rung, str | None]]:
    """Main-scope (incl. branch) writer rungs captured under *capture_rung_index*.

    No SP re-evaluation: the firing log already recorded that this rung
    wrote the matching value, so naming is purely structural.  Subroutine
    writers are handled separately from the node-level firing timeline.
    """
    writers: list[tuple[int, Rung, str | None]] = []
    for node_idx in sorted(candidates):
        node = pdg.rung_nodes[node_idx]
        if node.subroutine is not None:
            continue
        if node.rung_index != capture_rung_index:
            continue
        if program is not None:
            rung = resolve_rung(program, node)
        elif node.rung_index < len(logic):
            rung = logic[node.rung_index]
        else:
            rung = None
        if rung is None:
            continue
        writers.append((node.rung_index, rung, None))
    return writers


def _writer_fire_view(
    sub_name: str | None,
    rung_idx: int,
    scan_id: int,
    *,
    node_views_fn: Any,
    node_views_cache: dict[int, dict[RungId, Any]] | None,
) -> Any:
    """Return the writer rung's at-fire-time ``ConditionView``, or ``None``.

    **Every** writer (main-scope, branch, and subroutine) uses the replayed
    at-fire-time view: a rung's contacts can be consumed later the same scan
    (a command gate reset downstream), so end-of-scan state mis-classifies the
    writer's proximate-vs-enabler split.  The same on-demand replay Tier 2 runs
    for the read-diff already produces every rung's view, so this is the
    consistent (and now near-free) choice.  Falls back to ``None`` (caller uses
    end-of-scan state) when there is no replay — a logic-list PLC with no
    Program, or a scan out of replay range.  Memoized per distinct scan.
    """
    if node_views_fn is None:
        return None
    if node_views_cache is not None and scan_id in node_views_cache:
        views = node_views_cache[scan_id]
    else:
        views = node_views_fn(scan_id) or {}
        if node_views_cache is not None:
            node_views_cache[scan_id] = views
    return views.get(RungId(sub_name, rung_idx))


def _with_caller_gate(
    step: ChainStep,
    sub_name: str | None,
    fire_view: Any,
    pdg: ProgramGraph | None,
    program: Program | None,
) -> ChainStep:
    """Surface the call-site caller gate as a lever on a subroutine writer.

    A subroutine only runs when its call-site rung is enabled, so the
    caller gate is a first-class enabler of every write the subroutine
    makes: reversing it disables the whole subtree.  Adds the caller
    rung's condition contacts (held True at fire time) as enablers and
    records ``caller_rung_index`` for traceability.  Only applies to
    subroutine writers with a single unambiguous call site.
    """
    if sub_name is None or pdg is None or program is None:
        return step
    call_sites = pdg.call_site_rung_indices().get(sub_name, frozenset())
    if len(call_sites) != 1:
        # Zero (can't happen for a fired sub) or several (ambiguous without
        # per-call attribution) — leave the proximate writer alone.
        return step
    caller_idx = next(iter(call_sites))
    if caller_idx >= len(program.rungs):
        return step
    caller_rung = program.rungs[caller_idx]
    sp_tree = caller_rung.sp_tree()
    if sp_tree is None:
        # Unconditional call site — no gate to reverse.
        return step.with_caller(caller_idx)

    existing = {e.tag_name for e in step.enablers} | {t.tag_name for t in step.triggers}
    view = fire_view if fire_view is not None else None
    caller_enablers: list[EnablingCondition] = list(step.enablers)
    for leaf in _collect_sp_leaves(sp_tree):
        cond_tag = _condition_tag_name(leaf.condition)
        if cond_tag is None or cond_tag in existing:
            continue
        existing.add(cond_tag)
        value = view.get_tag(cond_tag) if view is not None else None
        caller_enablers.append(
            EnablingCondition(tag_name=cond_tag, value=value, held_since_scan=None)
        )
    return step.with_caller(caller_idx, tuple(caller_enablers))


def _semantic_writers_from_pdg(
    *,
    pdg: ProgramGraph,
    program: Program | None,
    logic: list[Rung],
    history: History,
    tag_name: str,
    scan_id: int,
    candidates: frozenset[int],
    capture_rung_index: int | None,
) -> list[tuple[int, Rung, str | None]]:
    """Return PDG writer rungs that were enabled at *scan_id*.

    If *capture_rung_index* is provided, candidates are limited to nodes
    whose writes are captured under that main-rung timeline key.
    """
    state = history.at(scan_id)
    view = _HistoricalView(state)

    def _eval(cond: Condition, _v: Any = view) -> bool:
        return cond.evaluate(_v)  # type: ignore[arg-type]

    writers: list[tuple[int, Rung, str | None]] = []
    for node_idx in sorted(candidates):
        if (
            capture_rung_index is not None
            and capture_rung_index not in pdg.timeline_capture_indices_for_node(node_idx)
        ):
            continue
        node = pdg.rung_nodes[node_idx]
        if program is not None:
            rung = resolve_rung(program, node)
        elif node.subroutine is None and node.rung_index < len(logic):
            rung = logic[node.rung_index]
        else:
            rung = None
        if rung is None:
            continue
        sp_tree = rung.sp_tree()
        if sp_tree is None or evaluate_sp(sp_tree, _eval):
            writers.append((node.rung_index, rung, node.subroutine))
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
    program: Program | None = None,
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
        main_rungs = program.rungs if program is not None else logic
        if pdg is not None:
            main_node_map = pdg.main_node_by_rung()
            rung_nodes = pdg.rung_nodes
            rung_count = len(main_rungs)
        else:
            main_node_map = {}
            rung_nodes = ()
            rung_count = 0
        rung_range = range(rung_count) if pdg is not None else sorted(firings.keys())

        for rung_idx in rung_range:
            rung = main_rungs[rung_idx]
            node_idx = main_node_map.get(rung_idx)
            if rung_idx in firings:
                writes: Any = firings[rung_idx]
                if not writes and node_idx is not None:
                    # Rung fired but filter emptied its writes — synthesize
                    # candidate written tags from the PDG so the history
                    # lookup below can recover real transitions.
                    writes = rung_nodes[node_idx].all_writes
            elif node_idx is not None:
                node = rung_nodes[node_idx]
                reads = node.condition_reads | node.data_reads
                if reads.isdisjoint(frontier):
                    continue
                writes = node.all_writes
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
                            triggers=(cause_trans,),
                            enablers=tuple(enabling),
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
