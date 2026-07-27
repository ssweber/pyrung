from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.observed import latest_writer_run, writer_runs_for_node
from pyrung.core.analysis.pdg import resolve_rung
from pyrung.core.analysis.sp_tree import attribute, evaluate_sp
from pyrung.core.context import RungId

from .crossings_recorded import recorded_read_changes, resolve_recorded_branches
from .history import (
    _find_last_transition_scan,
    _find_recent_transition,
    _find_transition,
    _find_transition_at_scan,
)
from .models import CausalChain, ChainStep, EnablingCondition, RootCause, RootKind, Transition
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


class _DeepSupport:
    """Mutable state for recursively explaining newly conductive enablers.

    ``roots`` collects classified terminals; ``root_seen`` deduplicates them
    by ``(tag, kind)``; ``absence_visited`` prevents cycles while following a
    steady enabler through the writer that actually supplied its value.
    """

    __slots__ = ("roots", "root_seen", "absence_visited")

    def __init__(self) -> None:
        self.roots: list[RootCause] = []
        self.root_seen: set[tuple[str, str]] = set()
        self.absence_visited: set[tuple[str, int, str]] = set()


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
    node_previous_firing_fn: Any = None,  # Callable[[frozenset[RungId], str, Any, int], int | None]
    node_rung_fn: Any = None,  # Callable[[RungId], Rung | None] | None
    node_views_fn: Any = None,  # Callable[[int], dict[RungId, ConditionView]] | None
    node_runs_fn: Any = None,  # Callable[[int], tuple[RungRun, ...]] | None
    node_reads_fn: Any = None,  # Callable[[int], dict[RungId, set[str]]] | None
    deep: bool = True,
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
        deep: Recursively explain each fired rung's enablers through their
            establishing transitions or observed value writers, and classify
            terminals into ``CausalChain.roots``. ``False`` restores the
            shallow trigger-only walk.

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
    visited: set[tuple[str, int]] = set()
    # Per-cause() memoization of the on-demand replay views, keyed by
    # scan.  The backward walk revisits the same scan for each writer at
    # a transition, and across recursion may revisit a scan repeatedly;
    # one replay per distinct scan is enough.
    node_views_cache: dict[int, dict[RungId, Any]] = {}
    node_runs_cache: dict[int, tuple[Any, ...]] = {}
    # Companion cache for the Tier-2 per-node data reads — same replay, same
    # per-scan memoization as ``node_views_cache``.
    node_reads_cache: dict[int, dict[RungId, Any]] = {}
    deep_state = _DeepSupport() if deep else None

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
        node_previous_firing_fn=node_previous_firing_fn,
        node_rung_fn=node_rung_fn,
        node_views_fn=node_views_fn,
        node_views_cache=node_views_cache,
        node_runs_fn=node_runs_fn,
        node_runs_cache=node_runs_cache,
        node_reads_fn=node_reads_fn,
        node_reads_cache=node_reads_cache,
        deep=deep_state,
    )

    return CausalChain(
        effect=transition,
        mode="recorded",
        steps=steps,
        conjunctive_roots=conjunctive_roots,
        ambiguous_roots=ambiguous_roots,
        roots=deep_state.roots if deep_state is not None else [],
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


def _node_runs_at(
    scan_id: int,
    node_runs_fn: Any,
    node_runs_cache: dict[int, tuple[Any, ...]] | None,
) -> tuple[Any, ...]:
    """Per-occurrence rung runs for ``scan_id``, memoized."""
    if node_runs_fn is None:
        return ()
    if node_runs_cache is not None and scan_id in node_runs_cache:
        return node_runs_cache[scan_id]
    runs = tuple(node_runs_fn(scan_id) or ())
    if node_runs_cache is not None:
        node_runs_cache[scan_id] = runs
    return runs


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
    rung: Any = None,
    fire_view: Any = None,
    node_reads_fn: Any = None,
    node_reads_cache: dict[int, dict[RungId, Any]] | None = None,
) -> tuple[tuple[Transition, ...], tuple[EnablingCondition, ...]] | None:
    """Cross an opaque writer: the recorded read-diff, then the crossings registry.

    First the instruction-agnostic footprint read-diff (Phase 1/Tier 2); when it
    finds nothing crossable (a counter/timer whose accumulator is a *write*, not
    a read footprint, so the footprint is empty), fall back to the projected
    registry resolved against the observed scan — e.g. a done bit crosses to its
    accumulator inequality.  Additive: it only fires where the footprint diff
    already dead-ended.
    """
    footprint = _cross_via_footprint(
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
    if footprint is not None:
        return footprint
    return _cross_via_registry(
        rung=rung,
        tag_name=tag_name,
        scan_id=scan_id,
        history=history,
        timelines=timelines,
        pdg=pdg,
        scan_log=scan_log,
        initial_tags=initial_tags,
    )


def _cross_via_footprint(
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
    """The instruction-agnostic read-diff crossing (Crossings Phase 1/Tier 2).

    Returns ``(triggers, enablers)`` derived from the writer's observed data
    reads — operands that *changed* this scan are triggers, operands that are
    merely *non-zero now* are enablers — or ``None`` when the writer has no
    crossable data reads or nothing in the footprint changed or is non-zero.
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


def _registry_writer_for_tag(rung: Any, tag_name: str) -> Any | None:
    """The first instruction in *rung* that writes *tag_name* (by its ``_writes``)."""
    from pyrung.core.analysis.sp_values import _writer_for_tag

    return _writer_for_tag(rung, tag_name)


def _cross_via_registry(
    *,
    rung: Any,
    tag_name: str,
    scan_id: int,
    history: History,
    timelines: RungFiringTimelines | None,
    pdg: ProgramGraph | None,
    scan_log: Any,
    initial_tags: Any,
) -> tuple[tuple[Transition, ...], tuple[EnablingCondition, ...]] | None:
    """Cross a writer the footprint diff missed via the projected registry.

    Reverses the writer for its *observed* value through ``crossings.reverse`` and
    discharges the result against history (``resolve_recorded``).  Conservative on
    purpose: only a single deterministic branch of value constraints (``Eq`` /
    ``Cmp``) is taken — e.g. a counter/timer done bit crossing to its accumulator
    inequality.  Disjunctive (shift/drum/latch) and condition/external/frontier
    results are left to the SP-tree path and the projected walker.
    """
    instr = _registry_writer_for_tag(rung, tag_name)
    if instr is None:
        return None

    from pyrung.core.analysis import crossings
    from pyrung.core.crossing import CrossingContext, eq_target

    observed = history.at(scan_id).tags.get(tag_name)
    result = crossings.reverse(instr, rung, eq_target(tag_name, observed), CrossingContext())
    branches = resolve_recorded_branches(result, history=history, scan_id=scan_id)
    if len(branches) != 1:
        return None  # only deterministic single-branch crossings; DNF is the walker's
    (resolved,) = branches
    if not resolved or any(rc.kind != "value" for rc in resolved):
        return None  # external / condition / frontier -> not a recorded value chase

    # Tags co-written by the same instruction are internal state (e.g. a timer's
    # accumulator alongside its done bit).  A transition on internal state is the
    # instruction's mechanism, not a user-visible cause — skip it.
    co_writes: set[str] = set()
    for field in getattr(instr, "_writes", ()):
        obj = getattr(instr, field, None)
        name = getattr(obj, "name", None)
        if name is not None:
            co_writes.add(name)

    triggers: list[Transition] = []
    enablers: list[EnablingCondition] = []
    for rc in resolved:
        tag, scan = rc.tag, rc.scan_id
        if tag is None or scan is None:
            return None  # a value chase always names a tag and scan; bail if not
        if tag in co_writes:
            continue  # internal state of the same instruction
        if rc.changed:
            triggers.append(Transition(tag, scan, rc.before, rc.after))
        else:
            enablers.append(
                EnablingCondition(
                    tag_name=tag,
                    value=rc.after,
                    held_since_scan=_find_last_transition_scan(
                        history,
                        tag,
                        scan,
                        timelines=timelines,
                        pdg=pdg,
                        scan_log=scan_log,
                        initial_tags=initial_tags,
                    ),
                )
            )
    if not triggers and not enablers:
        return None
    return tuple(triggers), tuple(enablers)


def _indexed_value_transition_scans(
    lookup: Any,
    candidate_rungs: frozenset[RungId],
    tag_name: str,
    to_value: Any,
    start_scan: int,
    oldest_scan: int,
) -> Iterator[int]:
    """Yield candidate-writer range positions that may establish a value.

    The lookup is authoritative when it returns a valid scan or ``None``.
    A malformed/legacy callback degrades to the old linear scan rather than
    risking a missing causal writer.
    """
    cursor = start_scan
    while cursor >= oldest_scan:
        try:
            scan = lookup(candidate_rungs, tag_name, to_value, cursor)
        except Exception:  # noqa: BLE001
            yield from range(cursor, oldest_scan - 1, -1)
            return
        if scan is None:
            return
        if not isinstance(scan, int) or scan < oldest_scan or scan > cursor:
            yield from range(cursor, oldest_scan - 1, -1)
            return
        yield scan
        cursor = scan - 1


def _walk_backward(
    *,
    logic: list[Rung],
    history: History,
    rung_firings_fn: Any,
    transition: Transition,
    steps: list[ChainStep],
    conjunctive_roots: list[Transition],
    ambiguous_roots: list[Transition],
    visited: set[tuple[str, int]],
    pdg: ProgramGraph | None = None,
    timelines: RungFiringTimelines | None = None,
    state_in_cache_fn: Any = None,  # Callable[[int], bool] | None
    program: Program | None = None,
    scan_log: Any = None,
    initial_tags: Any = None,
    node_firings_fn: Any = None,
    node_previous_firing_fn: Any = None,
    node_rung_fn: Any = None,
    node_views_fn: Any = None,
    node_views_cache: dict[int, dict[RungId, Any]] | None = None,
    node_runs_fn: Any = None,
    node_runs_cache: dict[int, tuple[Any, ...]] | None = None,
    node_reads_fn: Any = None,
    node_reads_cache: dict[int, dict[RungId, Any]] | None = None,
    deep: _DeepSupport | None = None,
    trail: tuple[str, ...] = (),
) -> None:
    """Recursive backward walk from a single transition.

    Beyond trigger recursion, the walk may recursively explain a fired rung's
    steady enablers. An enabler that transitioned earlier is followed to that
    establishing transition. A never-moved enabler is followed only through a
    writer observed supplying its current value at this frame. Static writers
    that did not fire are not part of the chain.

    Terminals are classified into ``deep.roots`` (external / never-written /
    system / unattributed).
    """
    tag_name = transition.tag_name
    scan_id = transition.scan_id

    visit_key = (tag_name, scan_id)
    if visit_key in visited:
        return  # cycle guard
    visited.add(visit_key)

    trail = (*trail, f"{tag_name}@{scan_id}")

    def _classify_terminal(name: str) -> RootKind | None:
        """Root kind for *name*, or ``None`` when program-written (walkable)."""
        if name.startswith(("sys.", "rtc.")):
            return "system"
        tag_obj = pdg.tags.get(name) if pdg is not None else None
        if tag_obj is not None and getattr(tag_obj, "external", False):
            return "external"
        if pdg is not None and pdg.writers_of.get(name, frozenset()):
            return None
        return "never_written"

    def _add_root(
        name: str,
        value: Any,
        kind: RootKind,
        held_since: int | None,
        base_trail: tuple[str, ...],
    ) -> None:
        if deep is None or (name, kind) in deep.root_seen:
            return
        deep.root_seen.add((name, kind))
        deep.roots.append(
            RootCause(
                tag_name=name,
                value=value,
                kind=kind,
                scan_id=scan_id,
                held_since_scan=held_since,
                via=base_trail,
            )
        )

    def _mirror_root(tr: Transition) -> None:
        """Record a trigger-walk terminal (no attributable writer) as a root."""
        kind = _classify_terminal(tr.tag_name) or "unattributed"
        _add_root(tr.tag_name, tr.to_value, kind, tr.scan_id, trail)

    def _held_since(name: str) -> int | None:
        return _find_last_transition_scan(
            history,
            name,
            scan_id,
            timelines=timelines,
            pdg=pdg,
            scan_log=scan_log,
            initial_tags=initial_tags,
        )

    def _chase_supports(
        supports: tuple[EnablingCondition, ...], base_trail: tuple[str, ...]
    ) -> None:
        """Recursively explain enablers on the actual fired path."""
        if deep is None or pdg is None:
            return
        for ec in supports:
            name = ec.tag_name
            # ``held_since_scan`` may be None because it was never computed
            # (caller-gate enablers) — recompute; a second None is truth.
            held = ec.held_since_scan if ec.held_since_scan is not None else _held_since(name)
            if held is not None:
                t = _find_transition_at_scan(
                    history,
                    name,
                    held,
                    timelines=timelines,
                    pdg=pdg,
                    scan_log=scan_log,
                    initial_tags=initial_tags,
                )
                if t is not None:
                    _walk_backward(
                        logic=logic,
                        history=history,
                        rung_firings_fn=rung_firings_fn,
                        transition=t,
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
                        node_previous_firing_fn=node_previous_firing_fn,
                        node_rung_fn=node_rung_fn,
                        node_views_fn=node_views_fn,
                        node_views_cache=node_views_cache,
                        node_runs_fn=node_runs_fn,
                        node_runs_cache=node_runs_cache,
                        node_reads_fn=node_reads_fn,
                        node_reads_cache=node_reads_cache,
                        deep=deep,
                        trail=base_trail,
                    )
                    continue
            _absence_hop(name, ec.value, base_trail)

    def _absence_hop(name: str, value: Any, base_trail: tuple[str, ...]) -> None:
        """Expand a held enabler through its observed value writer.

        A steady contact only enters here because an actual fired rung made it
        newly relevant.  Continue through a writer that really executed and
        supplied the observed value at this frame; never sweep every static
        writer merely because it *could* write the tag.  If no matching writer
        executed, the held/default fact is the honest terminal.
        """
        if deep is None or pdg is None:
            return
        hop_trail = (*base_trail, f"{name}(held)")
        kind = _classify_terminal(name)
        if kind is not None:
            if value is None:
                value = history.at(scan_id).tags.get(name)
            _add_root(name, value, kind, None, hop_trail)
            return
        state = history.at(scan_id)
        if value is None:
            value = state.tags.get(name)
        absence_key = (name, scan_id, repr(value))
        if absence_key in deep.absence_visited:
            return
        deep.absence_visited.add(absence_key)

        writer_scan = scan_id
        observed_writers: list[tuple[int, Rung, str | None]] = []
        # A held support is explained by the occurrence that established its
        # value, not by every later rung execution that reasserted it. Stable,
        # alternating, and arithmetic firing ranges name compressed transition
        # candidates; validate each against committed state before replaying
        # the exact writer occurrence.
        candidate_rungs = frozenset(
            RungId(pdg.rung_nodes[node_idx].subroutine, pdg.rung_nodes[node_idx].rung_index)
            for node_idx in pdg.writers_of.get(name, frozenset())
        )
        if node_previous_firing_fn is not None and candidate_rungs:
            prior_scans: Any = _indexed_value_transition_scans(
                node_previous_firing_fn,
                candidate_rungs,
                name,
                value,
                scan_id,
                history.oldest_scan_id,
            )
        else:
            prior_scans = range(scan_id, history.oldest_scan_id - 1, -1)
        for prior_scan in prior_scans:
            transition = _find_transition_at_scan(
                history,
                name,
                prior_scan,
                timelines=timelines,
                pdg=pdg,
                scan_log=scan_log,
                initial_tags=initial_tags,
            )
            if transition is None or transition.to_value != value:
                continue
            prior_writers = _replayed_writers_from_pdg(
                pdg=pdg,
                program=program,
                logic=logic,
                tag_name=name,
                to_value=value,
                scan_id=prior_scan,
                node_views_fn=node_views_fn,
                node_views_cache=node_views_cache,
                node_runs_fn=node_runs_fn,
                node_runs_cache=node_runs_cache,
            )
            if prior_writers:
                writer_scan = prior_scan
                observed_writers = prior_writers
                break
        if not observed_writers:
            _add_root(name, value, "unattributed", None, hop_trail)
            return

        _walk_backward(
            logic=logic,
            history=history,
            rung_firings_fn=rung_firings_fn,
            transition=Transition(name, writer_scan, value, value),
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
            node_previous_firing_fn=node_previous_firing_fn,
            node_rung_fn=node_rung_fn,
            node_views_fn=node_views_fn,
            node_views_cache=node_views_cache,
            node_runs_fn=node_runs_fn,
            node_runs_cache=node_runs_cache,
            node_reads_fn=node_reads_fn,
            node_reads_cache=node_reads_cache,
            deep=deep,
            trail=hop_trail,
        )

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
        node_rung_fn=node_rung_fn,
        tag_name=tag_name,
        scan_id=scan_id,
        to_value=transition.to_value,
    )

    if not resolved_writers and pdg is not None:
        # The firing timeline can omit a PDG-filtered tag value. Recover its writer
        # only from an exact rung observed during interpreted replay; the PDG
        # supplies writer identity, not evidence that a candidate executed.
        resolved_writers = _replayed_writers_from_pdg(
            pdg=pdg,
            program=program,
            logic=logic,
            tag_name=tag_name,
            to_value=transition.to_value,
            scan_id=scan_id,
            node_views_fn=node_views_fn,
            node_views_cache=node_views_cache,
            node_runs_fn=node_runs_fn,
            node_runs_cache=node_runs_cache,
        )

    indirect_crossings: dict[
        tuple[int, str | None],
        tuple[tuple[Transition, ...], tuple[EnablingCondition, ...]],
    ] = {}
    if pdg is not None and pdg.indirect_writes:
        indirect_writers, indirect_crossings = _resolve_indirect_writers(
            pdg=pdg,
            program=program,
            history=history,
            tag_name=tag_name,
            scan_id=scan_id,
            timelines=timelines,
            scan_log=scan_log,
            initial_tags=initial_tags,
        )
        if not resolved_writers:
            resolved_writers = indirect_writers

    if not resolved_writers:
        # No rung wrote this value — root cause (external input / patch)
        conjunctive_roots.append(transition)
        _mirror_root(transition)
        return

    for rung_idx, rung, sub_name in resolved_writers:
        sp_tree = rung.sp_tree()

        # At-fire-time view: for a subroutine writer, the rung's contacts
        # may have flipped later the same scan (a command gate consumed
        # downstream).  Reconstruct the writer rung's entry-time
        # ConditionView via on-demand replay so triggers/enablers reflect
        # what the rung *actually read* — not end-of-scan state.
        fire_run = latest_writer_run(
            rung,
            tag_name,
            transition.to_value,
            _node_runs_at(scan_id, node_runs_fn, node_runs_cache),
        )
        fire_view = (
            fire_run.view
            if fire_run is not None
            else _writer_fire_view(
                sub_name,
                rung_idx,
                scan_id,
                node_views_fn=node_views_fn,
                node_views_cache=node_views_cache,
            )
        )
        caller_rung_index = (
            fire_run.caller_rung if fire_run is not None and fire_run.kind == "subroutine" else None
        )

        if indirect_crossings and (rung_idx, sub_name) in indirect_crossings:
            triggers, enablers = indirect_crossings[(rung_idx, sub_name)]
            step = ChainStep(
                transition=transition,
                rung_index=rung_idx,
                triggers=triggers,
                enablers=enablers,
                subroutine=sub_name,
                fidelity="full",
            )
            wrapped = _with_caller_gate(
                step,
                sub_name,
                fire_view,
                pdg,
                program,
                caller_rung_index=caller_rung_index,
            )
            steps.append(wrapped)
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
                    node_previous_firing_fn=node_previous_firing_fn,
                    node_rung_fn=node_rung_fn,
                    node_views_fn=node_views_fn,
                    node_views_cache=node_views_cache,
                    node_runs_fn=node_runs_fn,
                    node_runs_cache=node_runs_cache,
                    node_reads_fn=node_reads_fn,
                    node_reads_cache=node_reads_cache,
                    deep=deep,
                    trail=trail,
                )
            _chase_supports(wrapped.enablers, trail)
            continue

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
                rung=rung,
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
                wrapped = _with_caller_gate(
                    step,
                    sub_name,
                    fire_view,
                    pdg,
                    program,
                    caller_rung_index=caller_rung_index,
                )
                steps.append(wrapped)
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
                        node_previous_firing_fn=node_previous_firing_fn,
                        node_rung_fn=node_rung_fn,
                        node_views_fn=node_views_fn,
                        node_views_cache=node_views_cache,
                        node_runs_fn=node_runs_fn,
                        node_runs_cache=node_runs_cache,
                        node_reads_fn=node_reads_fn,
                        node_reads_cache=node_reads_cache,
                        deep=deep,
                        trail=trail,
                    )
                _chase_supports(wrapped.enablers, trail)
                continue
            step = ChainStep(
                transition=transition,
                rung_index=rung_idx,
                triggers=(),
                enablers=(),
                subroutine=sub_name,
            )
            wrapped = _with_caller_gate(
                step,
                sub_name,
                fire_view,
                pdg,
                program,
                caller_rung_index=caller_rung_index,
            )
            steps.append(wrapped)
            conjunctive_roots.append(transition)
            _mirror_root(transition)
            _chase_supports(wrapped.enablers, trail)
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
            attributed_tags: dict[str, Any] = {}

            for attr in attributions:
                cond_tag = _condition_tag_name(attr.condition)
                if cond_tag is None:
                    continue
                attributed_tags.setdefault(cond_tag, view.get_tag(cond_tag))

            # Classify contacts against the value the writer actually read,
            # not the end-of-scan value.  Commands are commonly written,
            # consumed, and cleared within one scan; the committed history then
            # shows no transition even though the command is precisely what
            # made this rung newly conductive. A value established in the prior
            # scan stays an enabler here; deep recursion follows that separate
            # establishing transition.
            fire_diff = recorded_read_changes(
                history,
                frozenset(attributed_tags),
                scan_id,
                read_values=attributed_tags,
            )
            changed_at_fire = {
                name: Transition(name, scan_id, before, after)
                for name, before, after in fire_diff.changed
            }

            for cond_tag, at_fire_value in attributed_tags.items():
                cond_transition = changed_at_fire.get(cond_tag)
                if cond_transition is not None:
                    proximate.append(cond_transition)
                    continue

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
                        value=at_fire_value,
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
            steps.append(
                _with_caller_gate(
                    step,
                    sub_name,
                    fire_view,
                    pdg,
                    program,
                    caller_rung_index=caller_rung_index,
                )
            )
            step_idx = len(steps) - 1
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
            step_idx = len(steps) - 1
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
            step_idx = len(steps) - 1
            proximate = proximate_st

        if not proximate or deep is not None:
            # Conditioned writer with no proximate cause — explained only by
            # held gate conditions (or nothing).  Phase 1: cross the writer's
            # data reads so a gated calc/sum/copy continues from its changed/
            # non-zero operands, folding them into the step alongside the held
            # gate enablers instead of dead-ending at the written tag.
            #
            # Deep walk: the value channel is orthogonal to the guard, so
            # cross it even when the guard has a proximate trigger — a calc
            # whose gate transitioned the same scan must not hide its
            # operands (the guard trigger explains the *firing*, the data
            # reads explain the *value*).
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
                rung=rung,
                fire_view=fire_view,
                node_reads_fn=node_reads_fn,
                node_reads_cache=node_reads_cache,
            )
            if crossed is not None:
                dr_triggers, dr_enablers = crossed
                steps[step_idx] = replace(
                    steps[step_idx],
                    triggers=steps[step_idx].triggers + dr_triggers,
                    enablers=steps[step_idx].enablers + dr_enablers,
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
                        node_previous_firing_fn=node_previous_firing_fn,
                        node_rung_fn=node_rung_fn,
                        node_views_fn=node_views_fn,
                        node_views_cache=node_views_cache,
                        node_runs_fn=node_runs_fn,
                        node_runs_cache=node_runs_cache,
                        node_reads_fn=node_reads_fn,
                        node_reads_cache=node_reads_cache,
                        deep=deep,
                        trail=trail,
                    )
            # If a rung was explained only by held enablers, do not
            # invent the written tag as its own root; callers can fall
            # through to the enabler set as the remaining choices.
            elif not proximate and not steps[step_idx].enablers:
                conjunctive_roots.append(transition)
                _mirror_root(transition)
        if proximate:
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
                    node_previous_firing_fn=node_previous_firing_fn,
                    node_rung_fn=node_rung_fn,
                    node_views_fn=node_views_fn,
                    node_views_cache=node_views_cache,
                    node_runs_fn=node_runs_fn,
                    node_runs_cache=node_runs_cache,
                    node_reads_fn=node_reads_fn,
                    node_reads_cache=node_reads_cache,
                    deep=deep,
                    trail=trail,
                )
        # Deep walk: explain this fired rung's enablers through their
        # establishing transitions or observed value writers.
        _chase_supports(steps[step_idx].enablers, trail)


def _replayed_writers_from_pdg(
    *,
    pdg: ProgramGraph,
    program: Program | None,
    logic: list[Rung],
    tag_name: str,
    to_value: Any,
    scan_id: int,
    node_views_fn: Any = None,
    node_views_cache: dict[int, dict[RungId, Any]] | None = None,
    node_runs_fn: Any = None,
    node_runs_cache: dict[int, tuple[Any, ...]] | None = None,
) -> list[tuple[int, Rung, str | None]]:
    """Resolve writers not identified precisely by the compact timeline.

    ``writers_of`` says which rungs *can* write the tag.  It does not prove a
    subroutine was called, or even that a main rung was reached.  The runner's
    interpreted replay supplies that missing fact: a ``ConditionView`` exists
    only for a rung that actually executed in the historical scan.  A candidate
    qualifies only when that exact view exists and its guard is conductive.

    Returns resolved ``(rung_index, rung, subroutine)`` tuples so the
    caller gets correct rung objects for subroutine and branch writers.
    """
    candidates = pdg.writers_of.get(tag_name, frozenset())
    if not candidates:
        return []
    return _executed_writers_from_pdg(
        pdg=pdg,
        program=program,
        logic=logic,
        tag_name=tag_name,
        to_value=to_value,
        scan_id=scan_id,
        candidates=candidates,
        node_views_fn=node_views_fn,
        node_views_cache=node_views_cache,
        node_runs_fn=node_runs_fn,
        node_runs_cache=node_runs_cache,
    )


def _tag_in_block(block: Any, tag_name: str) -> bool:
    """Check whether *tag_name* is a tag inside *block*."""
    mapped = getattr(block, "_mapped_tags", None)
    if mapped is not None:
        for tag in mapped.values():
            if getattr(tag, "name", None) == tag_name:
                return True
    cache = getattr(block, "_tag_cache", None)
    if cache is not None:
        for tag in cache.values():
            if getattr(tag, "name", None) == tag_name:
                return True
    return False


def _resolve_indirect_writers(
    *,
    pdg: ProgramGraph,
    program: Program | None,
    history: History,
    tag_name: str,
    scan_id: int,
    timelines: RungFiringTimelines | None,
    scan_log: Any,
    initial_tags: Any,
) -> tuple[
    list[tuple[int, Rung, str | None]],
    dict[tuple[int, str | None], tuple[tuple[Transition, ...], tuple[EnablingCondition, ...]]],
]:
    """Resolve indirect writers whose block contains *tag_name*.

    When a block exceeds ``_INDIRECT_BLOCK_CAP``, the PDG drops per-tag
    writes and records an ``IndirectWriteRef`` descriptor instead.  This
    function checks whether *tag_name* lives inside the descriptor's block
    and, if so, treats the indirect copy as the writer — synthesising a
    crossing with the instruction's source tags as triggers and the pointer
    as an enabler.

    The pointer at end-of-scan may NOT match *tag_name* when the
    subroutine is called multiple times per scan (each call overwrites
    the pointer).  Block membership is sufficient: the tag *did*
    transition, and the indirect write is the only instruction that
    writes to this block region.
    """
    writers: list[tuple[int, Rung, str | None]] = []
    crossings: dict[
        tuple[int, str | None],
        tuple[tuple[Transition, ...], tuple[EnablingCondition, ...]],
    ] = {}

    state = history.at(scan_id)

    tag_obj = pdg.tags.get(tag_name)
    tag_addr = getattr(tag_obj, "_pyrung_block_addr", None) if tag_obj is not None else None

    for ref in pdg.indirect_writes:
        if tag_addr is not None:
            if not (ref.block.start <= tag_addr <= ref.block.end):
                continue
        elif not _tag_in_block(ref.block, tag_name):
            continue

        node = pdg.rung_nodes[ref.node_index]
        rung = resolve_rung(program, node) if program is not None else None
        if rung is None:
            continue

        triggers: list[Transition] = []
        for src_tag in sorted(ref.source_tags):
            t = _find_transition_at_scan(
                history,
                src_tag,
                scan_id,
                timelines=timelines,
                pdg=pdg,
                scan_log=scan_log,
                initial_tags=initial_tags,
            )
            if t is not None:
                triggers.append(t)

        enablers: list[EnablingCondition] = [
            EnablingCondition(
                tag_name=ref.pointer_tag,
                value=state.tags.get(ref.pointer_tag),
                held_since_scan=_find_last_transition_scan(
                    history,
                    ref.pointer_tag,
                    scan_id,
                    timelines=timelines,
                    pdg=pdg,
                    scan_log=scan_log,
                    initial_tags=initial_tags,
                ),
            )
        ]

        if not triggers:
            continue
        key = (node.rung_index, node.subroutine)
        writers.append((node.rung_index, rung, node.subroutine))
        crossings[key] = (tuple(triggers), tuple(enablers))

    return writers, crossings


def _recorded_writers_from_firings(
    *,
    pdg: ProgramGraph | None,
    program: Program | None,
    logic: list[Rung],
    history: History,
    rung_firings_fn: Any,
    node_firings_fn: Any,
    node_rung_fn: Any,
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

    firings = rung_firings_fn(scan_id)
    main_matching = {
        rung_idx
        for rung_idx, writes in firings.items()
        if tag_name in writes and writes[tag_name] == to_value
    }

    # 1. Node writers — precise subroutine nodes plus synthesis namespaces.
    if node_firings_fn is not None:
        from pyrung.core.synthesis import (
            PILOT_RUNG_NAMESPACE,
            PLANT_RUNG_NAMESPACE,
        )

        node_firings = node_firings_fn(scan_id)
        synthetic_matches: list[RungId] = []
        for rung_id in sorted(node_firings, key=lambda r: (r.subroutine or "", r.rung_index)):
            if rung_id.subroutine is None:
                continue
            writes = node_firings[rung_id]
            if tag_name not in writes or writes[tag_name] != to_value:
                continue
            if rung_id.subroutine in (
                PLANT_RUNG_NAMESPACE,
                PILOT_RUNG_NAMESPACE,
            ):
                synthetic_matches.append(rung_id)
                continue
            rung = (
                node_rung_fn(rung_id)
                if node_rung_fn is not None
                else _resolve_subroutine_rung(program, rung_id.subroutine, rung_id.rung_index)
            )
            if rung is None:
                continue
            key = (rung_id.subroutine, rung_id.rung_index)
            if key in seen:
                continue
            seen.add(key)
            resolved.append((rung_id.rung_index, rung, rung_id.subroutine))

        # User logic executes after both synthesis brackets.  Only when no main
        # rung wrote the final value can a synthesis node be the final writer.
        # Holds follow plant, and higher indices execute later within a bracket.
        if not main_matching and synthetic_matches:
            holds = [r for r in synthetic_matches if r.subroutine == PILOT_RUNG_NAMESPACE]
            winner = max(holds or synthetic_matches, key=lambda r: r.rung_index)
            rung = node_rung_fn(winner) if node_rung_fn is not None else None
            if rung is not None:
                key = (winner.subroutine, winner.rung_index)
                seen.add(key)
                resolved.append((winner.rung_index, rung, winner.subroutine))

    # 2. Main-scope (and branch) writers — from the main-rung firing log.
    main_rungs = program.rungs if program is not None else logic
    candidates = pdg.writers_of.get(tag_name, frozenset()) if pdg is not None else frozenset()

    for rung_idx in sorted(main_matching):
        writes = firings[rung_idx]
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
    rung: Rung | None = None,
    tag_name: str | None = None,
    to_value: Any = None,
    node_views_fn: Any,
    node_views_cache: dict[int, dict[RungId, Any]] | None,
    node_runs_fn: Any = None,
    node_runs_cache: dict[int, tuple[Any, ...]] | None = None,
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
    if rung is not None and tag_name is not None:
        run = latest_writer_run(
            rung,
            tag_name,
            to_value,
            _node_runs_at(scan_id, node_runs_fn, node_runs_cache),
        )
        if run is not None:
            return run.view
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
    *,
    caller_rung_index: int | None = None,
) -> ChainStep:
    """Surface the call-site caller gate as a lever on a subroutine writer.

    A subroutine only runs when its call-site rung is enabled, so the
    caller gate is a first-class enabler of every write the subroutine
    makes: reversing it disables the whole subtree.  Adds the caller
    rung's condition contacts (held True at fire time) as enablers and
    records ``caller_rung_index`` for traceability.  Per-occurrence replay
    names the actual caller; older evidence falls back only when one call site
    is structurally unambiguous.
    """
    if sub_name is None or pdg is None or program is None:
        return step
    call_sites = pdg.call_site_rung_indices().get(sub_name, frozenset())
    if caller_rung_index is None and len(call_sites) != 1:
        # Zero (can't happen for a fired sub) or several (ambiguous without
        # per-call attribution) — leave the proximate writer alone.
        return step
    caller_idx = caller_rung_index if caller_rung_index is not None else next(iter(call_sites))
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


def _executed_writers_from_pdg(
    *,
    pdg: ProgramGraph,
    program: Program | None,
    logic: list[Rung],
    tag_name: str,
    to_value: Any,
    scan_id: int,
    candidates: frozenset[int],
    node_views_fn: Any = None,
    node_views_cache: dict[int, dict[RungId, Any]] | None = None,
    node_runs_fn: Any = None,
    node_runs_cache: dict[int, tuple[Any, ...]] | None = None,
) -> list[tuple[int, Rung, str | None]]:
    """Return PDG writers proven executed and conductive by replay."""

    writers: list[tuple[int, Rung, str | None]] = []
    observed_runs = _node_runs_at(scan_id, node_runs_fn, node_runs_cache)
    for node_idx in sorted(candidates):
        node = pdg.rung_nodes[node_idx]
        if program is not None:
            rung = resolve_rung(program, node)
        elif node.subroutine is None and node.rung_index < len(logic):
            rung = logic[node.rung_index]
        else:
            rung = None
        if rung is None:
            continue
        if program is not None and writer_runs_for_node(
            pdg,
            program,
            node_idx,
            tag_name,
            to_value,
            observed_runs,
        ):
            writers.append((node.rung_index, rung, node.subroutine))
            continue
        fire_view = _writer_fire_view(
            node.subroutine,
            node.rung_index,
            scan_id,
            rung=rung,
            tag_name=tag_name,
            to_value=to_value,
            node_views_fn=node_views_fn,
            node_views_cache=node_views_cache,
            node_runs_fn=node_runs_fn,
            node_runs_cache=node_runs_cache,
        )
        if fire_view is None:
            continue
        sp_tree = rung.sp_tree()
        if sp_tree is None:
            if _replayed_writer_proves_value(rung, tag_name, to_value, fire_view):
                writers.append((node.rung_index, rung, node.subroutine))
            continue

        def _eval_fire(cond: Condition, _v: Any = fire_view) -> bool:
            return cond.evaluate(_v)  # type: ignore[arg-type]

        if evaluate_sp(sp_tree, _eval_fire) and _replayed_writer_proves_value(
            rung, tag_name, to_value, fire_view
        ):
            writers.append((node.rung_index, rung, node.subroutine))
    return writers


def _replayed_writer_proves_value(
    rung: Rung,
    tag_name: str,
    to_value: Any,
    fire_view: Any,
) -> bool:
    """Whether a conductive replayed rung must write ``tag_name=to_value``."""
    from pyrung.core.analysis.sp_values import (
        _values_match,
        _writer_for_tag,
        _written_value_for_tag,
    )
    from pyrung.core.crossing import Affine, Literal
    from pyrung.core.instruction.coils import LatchInstruction, OutInstruction
    from pyrung.core.instruction.data_transfer import (
        CopyInstruction,
        _store_copy_value_to_tag_type,
    )
    from pyrung.core.tag import Tag

    written = _written_value_for_tag(rung, tag_name)
    if isinstance(written, Literal):
        return _values_match(written.value, to_value)
    if isinstance(written, Affine):
        try:
            source = fire_view.get_tag(written.source)
            return _values_match(source * written.scale + written.offset, to_value)
        except (AttributeError, KeyError, TypeError, ValueError):
            return False
    # A named slot inside a block can evade the generic forward crossing even
    # though Boolean coil semantics remain exact.
    instr = _writer_for_tag(rung, tag_name)
    if (
        isinstance(instr, CopyInstruction)
        and instr.convert is None
        and isinstance(instr.source, Tag)
        and isinstance(instr.dest, Tag)
        and instr.dest.name == tag_name
    ):
        try:
            source = fire_view.get_tag(instr.source.name)
            stored = _store_copy_value_to_tag_type(source, instr.dest)
            return _values_match(stored, to_value)
        except (AttributeError, KeyError, TypeError, ValueError):
            return False
    return isinstance(instr, (OutInstruction, LatchInstruction)) and _values_match(
        to_value,
        True,
    )


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
