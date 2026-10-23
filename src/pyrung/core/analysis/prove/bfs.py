"""BFS exploration loop and helpers for the prove subsystem."""

from __future__ import annotations

import itertools
import time
from collections import deque
from collections.abc import Callable, Generator
from dataclasses import replace
from typing import Any

from pyrung.core.kernel import ReplayKernel
from pyrung.core.system_points import SYSTEM_TAGS_BY_NAME

from . import _ExploreContext
from .classify import _build_dimension_hints
from .cycles import _CycleTracker
from .events import (
    _has_pending_done,
    _has_pending_hidden_event,
    _HiddenEventCache,
    _HiddenEventOutcome,
    _maybe_jump_hidden_event,
    _settle_pending,
)
from .independence import _find_bridge_tags
from .inputs import _FREE_COMBO_CAP, _FreeComboOverflow, _iter_input_assignments
from .kernel import (
    _EdgeCompressor,
    _extract_state_key,
    _KernelSnapshot,
    _LiveInputCache,
    _restore_kernel,
    _seed_synthetic_presets,
    _snapshot_kernel,
    _step_kernel,
    _threshold_vector_key,
)
from .passes import _DEFAULT_BFS_CONFIG, _BFSConfig
from .results import Counterexample, Intractable, Proven, TraceStep, _ParentLink
from .system_points import FIRST_SCAN, FREE_SYSTEM_CAVEAT, UnmodeledSystemRead


def _projected_tuple(kernel: ReplayKernel, project_names: tuple[str, ...]) -> tuple[Any, ...]:
    """Project kernel state onto a fixed ordered list of tag names."""
    return tuple(map(kernel.tags.get, project_names))


def _projected_states(
    project_names: tuple[str, ...],
    projected_rows: set[tuple[Any, ...]],
) -> frozenset[frozenset[tuple[str, Any]]]:
    """Convert ordered projection rows to the public frozenset shape."""
    return frozenset(frozenset(zip(project_names, row, strict=True)) for row in projected_rows)


def _build_intractable_hints(context: _ExploreContext) -> list[str]:
    hints = _build_dimension_hints(context)
    if context.free_input_factoring is None and len(context.free_input_names) >= 2:
        bridges = _find_bridge_tags(
            context.graph,
            context.stateful_dims,
            context.nondeterministic_dims,
            context.exclusive_input_groups,
            context.free_input_names,
            context.nondeterministic_names,
        )
        for tag_name, group_count in bridges[:3]:
            hints.append(
                f"Consider split_at=['{tag_name}'] to decompose "
                f"the state space ({group_count} independent groups)"
            )
    return hints


def _free_system_caveat(context: _ExploreContext, trace: list[TraceStep]) -> tuple[str, ...]:
    """Caveat for a trace that drives system tags explored as free inputs."""
    free = context.free_system_inputs
    if not free:
        return ()
    used = sorted({name for step in trace for name in step.inputs if name in free})
    if not used:
        return ()
    return (FREE_SYSTEM_CAVEAT.format(names=", ".join(used)),)


def _merge_caveats(*groups: tuple[str, ...]) -> tuple[str, ...]:
    """Merge caveat tuples while preserving first-seen order."""
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for caveat in group:
            if caveat in seen:
                continue
            seen.add(caveat)
            merged.append(caveat)
    return tuple(merged)


def _build_trace(
    parent_map: dict[tuple[Any, ...], _ParentLink],
    key: tuple[Any, ...],
) -> tuple[list[TraceStep], tuple[str, ...]]:
    """Reconstruct the input trace and per-edge caveats to failure."""
    links: list[_ParentLink] = []
    current = key
    while current in parent_map:
        link = parent_map[current]
        links.append(link)
        if link.parent_key is None:
            break
        current = link.parent_key
    links.reverse()
    trace: list[TraceStep] = []
    for link in links:
        trace.append(TraceStep(inputs=link.inputs, scans=link.scans, prev=link.prev))
        trace.extend(link.repeat)
    caveats = _merge_caveats(*(link.caveats for link in links))
    return trace, caveats


# Marks a refined trace id (see _refine_revisit in _bfs_explore_gen).
_REFINED = object()

# Longest pumped cycle repetition written out step by step in a trace.
_MAX_PUMPED_TRACE_STEPS = 100_000

_BFSResult = (
    list[Proven | Counterexample | Intractable]
    | frozenset[frozenset[tuple[str, Any]]]
    | Intractable
)


def _free_combo_intractable(
    context: _ExploreContext,
    overflow: _FreeComboOverflow,
    visited: int,
) -> Intractable:
    names = sorted(overflow.domain_sizes)
    return Intractable(
        reason=(
            f"free inputs [{', '.join(names)}] take {overflow.combos:,} joint values on one"
            f" scan, more than the {_FREE_COMBO_CAP:,} the verifier enumerates"
        ),
        dimensions=len(context.stateful_dims) + len(context.nondeterministic_dims),
        estimated_space=visited,
        tags=names,
        hints=[
            *(
                f"  {name}: {size:,} values (system tag, set by the runtime)"
                if name in SYSTEM_TAGS_BY_NAME
                else f"  {name}: {size:,} values; narrow it with choices= or min=/max="
                for name, size in sorted(overflow.domain_sizes.items(), key=lambda kv: -kv[1])
            ),
            "  every joint value of these inputs is a successor of one state; a property"
            " that reads fewer of them together stays under the cap",
        ],
        journal=context.journal,
    )


def _bfs_explore(
    context: _ExploreContext,
    *,
    predicates: list[Callable[[dict[str, Any]], bool]] | None = None,
    project: tuple[str, ...] | None = None,
    depth_budget: int = 50,
    max_states: int = 100_000,
    max_evals: int | None = None,
    bfs_config: _BFSConfig = _DEFAULT_BFS_CONFIG,
    progress: Callable[[int, int, float], None] | None = None,
    settled: bool = False,
    paced: bool = False,
    initial_state: dict[str, Any] | None = None,
    edge_collector: (
        Callable[
            [
                tuple[Any, ...],
                tuple[Any, ...],
                dict[str, Any],
                int,
                tuple[str, ...],
                dict[str, Any],
            ],
            None,
        ]
        | None
    ) = None,
    state_filter: Callable[[dict[str, Any]], bool] | None = None,
    frontier_collector: list[dict[str, Any]] | None = None,
) -> _BFSResult:
    """BFS over the reachable state space (consumes first result from generator)."""
    for predicate in predicates or ():
        bind = getattr(predicate, "_pyrung_bind_system", None)
        if bind is not None:
            bind(context)
    try:
        return _first_bfs_result(
            context,
            predicates=predicates,
            project=project,
            depth_budget=depth_budget,
            max_states=max_states,
            max_evals=max_evals,
            bfs_config=bfs_config,
            progress=progress,
            settled=settled,
            paced=paced,
            initial_state=initial_state,
            edge_collector=edge_collector,
            state_filter=state_filter,
            frontier_collector=frontier_collector,
        )
    except UnmodeledSystemRead as exc:
        assert predicates is not None
        refusal = Intractable(
            reason=(f"callable property reads {exc.name}, which this search did not explore"),
            dimensions=len(context.stateful_dims) + len(context.nondeterministic_dims),
            estimated_space=0,
            tags=[exc.name],
            hints=[
                f"  {exc.name}: system tag the verifier explores only where a rung or a"
                f" condition property names it; write the property as a condition"
            ],
            journal=context.journal,
        )
        return [refusal for _ in predicates]


def _first_bfs_result(context: _ExploreContext, **kwargs: Any) -> _BFSResult:
    return next(_bfs_explore_gen(context, **kwargs))


def _bfs_explore_gen(
    context: _ExploreContext,
    *,
    predicates: list[Callable[[dict[str, Any]], bool]] | None = None,
    project: tuple[str, ...] | None = None,
    depth_budget: int = 50,
    max_states: int = 100_000,
    max_evals: int | None = None,
    bfs_config: _BFSConfig = _DEFAULT_BFS_CONFIG,
    progress: Callable[[int, int, float], None] | None = None,
    settled: bool = False,
    paced: bool = False,
    initial_state: dict[str, Any] | None = None,
    edge_collector: (
        Callable[
            [
                tuple[Any, ...],
                tuple[Any, ...],
                dict[str, Any],
                int,
                tuple[str, ...],
                dict[str, Any],
            ],
            None,
        ]
        | None
    ) = None,
    state_filter: Callable[[dict[str, Any]], bool] | None = None,
    frontier_collector: list[dict[str, Any]] | None = None,
) -> Generator[_BFSResult, None, None]:
    """BFS generator — yields each time all predicates are resolved."""
    kernel = context.compiled.create_kernel()
    _mutable = context.mutable_tag_names
    _base_keys = context.base_tag_keys
    _seed_synthetic_presets(context, kernel)
    if context.system_points is not None:
        context.system_points.seed(kernel.tags)
    if initial_state is not None:
        # A supplied state is a running PLC, not power-on.
        if FIRST_SCAN in kernel.tags and FIRST_SCAN not in initial_state:
            kernel.tags[FIRST_SCAN] = False
        for tag_name, value in initial_state.items():
            if tag_name in kernel.tags:
                kernel.tags[tag_name] = value
            if tag_name in kernel.prev:
                kernel.prev[tag_name] = value
    edge_comp = _EdgeCompressor(context)
    hidden_event_cache = _HiddenEventCache(context)
    live_cache = _LiveInputCache(context)

    def _state_key(
        k: ReplayKernel,
        live: frozenset[str] | None = None,
        threshold_vector: tuple[Any, ...] | None = None,
    ) -> tuple[Any, ...]:
        if bfs_config.edge_compression:
            return edge_comp.state_key(k, live_inputs=live, threshold_vector=threshold_vector)
        return _extract_state_key(
            k,
            context.stateful_names,
            context.edge_tag_names,
            context.memory_key_names,
            context.state_key_done_specs,
            context.threshold_vector_specs,
            nondeterministic_names=context.nondeterministic_names,
            live_inputs=live,
            threshold_vector=threshold_vector,
        )

    _demoted = context.demoted_edge_names
    _has_demoted = bool(_demoted)

    initial_base_key = _state_key(kernel)
    initial_key = (*initial_base_key, False) if paced else initial_base_key
    initial_bprev = tuple(kernel.prev.get(n) for n in _demoted)

    def _trace_id(key: tuple[Any, ...], bprev: tuple[Any, ...]) -> tuple[Any, ...]:
        return (key, bprev) if _has_demoted else key

    if _has_demoted:
        visited_bprev: dict[tuple[Any, ...], set[tuple[Any, ...]]] = {initial_key: {initial_bprev}}
        visited: dict[tuple[Any, ...], set[tuple[Any, ...]]] | set[tuple[Any, ...]] = visited_bprev
    else:
        visited_flat: set[tuple[Any, ...]] = {initial_key}
        visited = visited_flat
    initial_tid = _trace_id(initial_key, initial_bprev)
    parent_map: dict[tuple[Any, ...], _ParentLink] | None = (
        {
            initial_tid: _ParentLink(
                None,
                {},
                0,
                prev=dict(zip(_demoted, initial_bprev, strict=True)) if _has_demoted else {},
            )
        }
        if predicates is not None
        else None
    )

    results: list[Counterexample | Proven | Intractable | None] | None = (
        [None] * len(predicates) if predicates is not None else None
    )
    projected_rows: set[tuple[Any, ...]] = set()
    if project is not None:
        projected_rows.add(_projected_tuple(kernel, project))

    def _record_failures(
        *,
        state: dict[str, Any],
        p_key: tuple[Any, ...],
        input_dict: dict[str, Any],
        edge_scans: int,
        edge_caveats: tuple[str, ...] = (),
        initial: bool = False,
        bprev_dict: dict[str, Any] | None = None,
        extra_steps: tuple[TraceStep, ...] = (),
    ) -> None:
        assert predicates is not None and results is not None and parent_map is not None
        for i, predicate in enumerate(predicates):
            if results[i] is not None:
                continue
            if predicate(state):
                continue
            if initial:
                results[i] = Counterexample(
                    trace=[TraceStep(inputs={}, scans=0)],
                    journal=context.journal,
                )
                continue
            trace, trace_caveats = _build_trace(parent_map, p_key)
            trace.append(TraceStep(inputs=input_dict, scans=edge_scans, prev=bprev_dict or {}))
            trace.extend(extra_steps)
            results[i] = Counterexample(
                trace=trace,
                caveats=_merge_caveats(
                    trace_caveats, edge_caveats, _free_system_caveat(context, trace)
                ),
                journal=context.journal,
            )

    if predicates is not None:
        _record_failures(
            state=kernel.tags,
            p_key=initial_tid,
            input_dict={},
            edge_scans=0,
            initial=True,
        )
        assert results is not None
        if all(r is not None for r in results):
            yield [r for r in results if r is not None]
            return

    def _should_enqueue(key: tuple[Any, ...], bprev: tuple[Any, ...]) -> bool:
        """Check whether (key, bprev) needs exploration; update visited."""
        if _has_demoted:
            assert isinstance(visited, dict)
            bprev_set = visited.get(key)
            if bprev_set is None:
                visited[key] = {bprev}
                return True
            if bprev not in bprev_set:
                bprev_set.add(bprev)
                return True
            return False
        assert isinstance(visited, set)
        if key not in visited:
            visited.add(key)
            return True
        return False

    def _extract_bprev(k: ReplayKernel) -> tuple[Any, ...]:
        return tuple(k.tags.get(n) for n in _demoted)

    # Revisits that bring new hidden accumulator progress (cycles.py).  A
    # refined node shares its key with the first node there and gets the
    # trace id ``(_REFINED, base_tid, projection)``.
    cycles = _CycleTracker(context)
    cycles_active = cycles.active
    cycles_hidden = cycles.hidden_by_node
    if cycles_active:
        cycles.record_node(initial_tid, None, 0, cycles.hidden_vector(kernel, initial_key))

    def _base_tid(tid: Any) -> Any:
        if type(tid) is tuple and len(tid) == 3 and tid[0] is _REFINED:
            return tid[1]
        return tid

    def _visible_key(tid: Any) -> tuple[Any, ...]:
        base = _base_tid(tid)
        return base[0] if _has_demoted else base

    def _probe_stutter(key: tuple[Any, ...], hidden: tuple[Any, ...]) -> set[int]:
        """Hidden accumulators a held-input scan advances while keeping *key*."""
        probe_snap = _snapshot_kernel(kernel, _mutable, _base_keys)
        if _has_demoted:
            for name, value in zip(_demoted, _extract_bprev(kernel), strict=True):
                kernel.prev[name] = value
        _step_kernel(context, kernel)
        tv = _threshold_vector_key(kernel, context.threshold_vector_specs)
        live_after = (
            live_cache.live_inputs(kernel, threshold_vector=tv)
            if bfs_config.live_input_pruning
            else None
        )
        probe_key = _state_key(kernel, live=live_after, threshold_vector=tv)
        visible = key[:-1] if paced else key
        advanced = (
            cycles.advanced(hidden, cycles.hidden_vector(kernel, key))
            if probe_key == visible
            else set()
        )
        _restore_kernel(kernel, probe_snap)
        return advanced

    def _traverses_again(
        key: tuple[Any, ...],
        hidden: tuple[Any, ...],
        chain: list[Any],
        input_dict: dict[str, Any],
        edge_scans: int,
        deltas: dict[int, float],
        timer_steps: dict[int, int],
    ) -> bool:
        """Replay the cycle once from the revisit: same key, same progress again."""
        visible = key[:-1] if paced else key
        untracked = cycles.untracked_values(kernel)
        timers_before = cycles.timer_readings(kernel)
        for inputs, scans in (*cycles.cycle_steps(chain), (input_dict, edge_scans)):
            for name, value in inputs.items():
                kernel.tags[name] = value
            for _ in range(scans):
                if _has_demoted:
                    for name, value in zip(_demoted, _extract_bprev(kernel), strict=True):
                        kernel.prev[name] = value
                _step_kernel(context, kernel)
        tv = _threshold_vector_key(kernel, context.threshold_vector_specs)
        live_after = (
            live_cache.live_inputs(kernel, threshold_vector=tv)
            if bfs_config.live_input_pruning
            else None
        )
        return (
            _state_key(kernel, live=live_after, threshold_vector=tv) == visible
            and cycles.repeats_linearly(hidden, cycles.hidden_vector(kernel, key), deltas)
            and cycles.timer_lap_matches(timers_before, kernel, timer_steps)
            and cycles.untracked_values(kernel) == untracked
        )

    def _refine_revisit(
        *,
        key: tuple[Any, ...],
        tid: Any,
        input_dict: dict[str, Any],
        edge_scans: int,
        edge_caveats: tuple[str, ...],
        child_flipped: bool,
        parent_key: Any,
        bprev_dict: dict[str, Any],
        bprev: tuple[Any, ...],
        depth: int,
    ) -> Intractable | None:
        """Enqueue a revisit of *key* whose hidden progress is new there (cycles.py)."""
        # Pending accumulators follow from the key, so the first node's
        # vector says which ones to read.
        base_hidden = cycles.node_hidden(tid)
        if base_hidden is None:
            return None
        hidden = cycles.changed_from(kernel, base_hidden)
        if hidden is None:
            return None
        differing = cycles.differing(base_hidden, hidden)
        if not differing or cycles.covered(tid, base_hidden, hidden):
            return None
        chain = cycles.cycle_start(parent_key, tid, _base_tid, edge_scans)
        inherited = cycles.is_pumped(parent_key)
        pulsed = cycles.counters(cycles.advanced(cycles.node_hidden(parent_key), hidden, differing))
        if (
            chain is None
            and not inherited
            and not pulsed
            and not cycles.counters(differing)
            and cycles.timer_seeded(tid)
            and cycles.ahead(base_hidden, hidden, differing)
        ):
            return None
        probe_key = (tid, tuple(sorted(input_dict.items(), key=lambda kv: kv[0])))
        if not cycles.probed(probe_key):
            cycles.note_timelike(tid, probe_key, _probe_stutter(key, hidden))
            if cycles.covered(tid, base_hidden, hidden):
                return None
        pulse = differing - cycles.timelike(tid)
        pulsed &= pulse
        x_hidden = hidden
        x_snap: _KernelSnapshot | None = None
        repeat: tuple[TraceStep, ...] = ()
        pumped = cycles.pump(kernel, key, hidden, chain, pulse) if chain is not None else None
        if pumped is not None and pumped.count:
            assert chain is not None
            x_snap = _snapshot_kernel(kernel, _mutable, _base_keys)
            if not _traverses_again(
                key, hidden, chain, input_dict, edge_scans, pumped.deltas, pumped.timer_steps
            ):
                pumped = None
            _restore_kernel(kernel, x_snap)
        if pumped is not None and pumped.count:
            assert x_snap is not None
            cycles.apply(kernel, pumped)
            tv = _threshold_vector_key(kernel, context.threshold_vector_specs)
            live_after = (
                live_cache.live_inputs(kernel, threshold_vector=tv)
                if bfs_config.live_input_pruning
                else None
            )
            pumped_key = _state_key(kernel, live=live_after, threshold_vector=tv)
            if paced:
                pumped_key = (*pumped_key, child_flipped)
            if pumped_key != key:
                _restore_kernel(kernel, x_snap)
                pumped = None
            else:
                assert chain is not None
                hidden = pumped.values
                if parent_map is not None:
                    steps = [
                        TraceStep(
                            inputs=parent_map[node].inputs,
                            scans=parent_map[node].scans,
                            prev=parent_map[node].prev,
                        )
                        for node in reversed(chain[:-1])
                    ]
                    steps.append(TraceStep(inputs=input_dict, scans=edge_scans, prev=bprev_dict))
                    if len(steps) * pumped.count <= _MAX_PUMPED_TRACE_STEPS:
                        repeat = tuple(steps) * pumped.count
                    else:
                        # Spelled out, the trace would not fit in memory.
                        repeat = tuple(steps)
                        edge_caveats = (
                            *edge_caveats,
                            f"Counterexample trace lists a {len(steps)}-step input cycle once"
                            f" where the PLC runs it {pumped.count} times; repeat those steps"
                            f" {pumped.count - 1} more times to replay it.",
                        )
        if pumped is None or not pumped.count:
            x_snap = None
        claimed_x = cycles.claim(tid, base_hidden, x_hidden)
        # Kept: a counter that differs from the first node's (its value was
        # made on another branch, ahead or behind), a time-like accumulator
        # behind it (not covered: it has more time left), a pumped or
        # inherited revisit, or a fresh pulse; a timer that only advanced
        # around a cycle the tree does not show seeds the key once.
        if (
            pumped is None
            and not inherited
            and not pulsed
            and pulse
            and not cycles.counters(pulse)
            and not (claimed_x and cycles.seed_timer(tid, kernel, key, pulse))
        ):
            return None
        fresh = claimed_x if x_snap is None else cycles.claim(tid, base_hidden, hidden)
        if not fresh:
            if x_snap is not None:
                _restore_kernel(kernel, x_snap)
            return None
        refined_tid = (_REFINED, tid, hidden)
        cycles.refined_nodes += 1
        if len(visited) + cycles.refined_nodes > max_states:
            return Intractable(
                reason="max_states exceeded",
                dimensions=len(context.stateful_dims) + len(context.nondeterministic_dims),
                estimated_space=len(visited) + cycles.refined_nodes,
                hints=_build_intractable_hints(context),
                journal=context.journal,
            )
        if x_snap is not None:
            # A pumped state still has uncrossed thresholds pending: under
            # ``settled`` only its settled descendants are judged.
            if predicates is not None and not settled:
                _record_failures(
                    state=kernel.tags,
                    p_key=parent_key,
                    input_dict=input_dict,
                    edge_scans=edge_scans,
                    edge_caveats=edge_caveats,
                    bprev_dict=bprev_dict,
                    extra_steps=repeat,
                )
            if project is not None:
                projected_rows.add(_projected_tuple(kernel, project))
        if parent_map is not None:
            parent_map[refined_tid] = _ParentLink(
                parent_key,
                input_dict,
                edge_scans,
                edge_caveats,
                prev=bprev_dict,
                repeat=repeat,
            )
        # A pumped node's link spans many scans: no later cycle runs through
        # it, and its successors inherit its progress instead.  Nor through a
        # link with a caveat: holding its inputs does not replay it.
        cycles.record_node(
            refined_tid,
            parent_key,
            -1 if x_snap is not None or edge_caveats else edge_scans,
            hidden,
            (input_dict, bprev_dict),
        )
        if x_snap is not None or inherited:
            cycles.mark_pumped(refined_tid)
        queue.append(
            (
                _snapshot_kernel(kernel, _mutable, _base_keys),
                depth + 1,
                refined_tid,
                child_flipped,
                bprev,
            )
        )
        if x_snap is not None:
            _restore_kernel(kernel, x_snap)
        return None

    queue: deque[tuple[_KernelSnapshot, int, tuple[Any, ...], bool, tuple[Any, ...]]] = deque()
    queue.append(
        (_snapshot_kernel(kernel, _mutable, _base_keys), 0, initial_tid, False, initial_bprev)
    )

    _progress_last_time = time.monotonic()
    _progress_next_time = _progress_last_time + 5.0
    _progress_step: Callable[[], None] | None = (
        getattr(progress, "step", None) if progress is not None else None
    )
    _progress_set_depth: Callable[[int], None] | None = (
        getattr(progress, "set_depth", None) if progress is not None else None
    )
    depth_truncated = False
    # Set when free inputs past the joint-value cap were sampled (see
    # inputs._FreeComboOverflow): no property may then be Proven.
    sampled_overflow: _FreeComboOverflow | None = None
    # Deterministic work budget: count kernel evaluations (machine-independent,
    # unlike wall-clock) so a bounded how() search yields the same Intractable
    # verdict on every machine.  None => unbounded, so always/never/reachable_states
    # keep their exact prior behaviour; only how() opts in.  max_states caps
    # *enqueued* states, but the dominant cost is _step_kernel calls burned between
    # enqueues (one per input assignment per popped state) — only this counter
    # bounds them, which is what turns a runaway scoped-BFS into a fast Intractable.
    _eval_count = 0

    while queue:
        if max_evals is not None and _eval_count > max_evals:
            intractable = Intractable(
                reason=f"how() eval budget exhausted ({_eval_count} kernel evaluations)",
                dimensions=len(context.stateful_dims) + len(context.nondeterministic_dims),
                estimated_space=len(visited),
                hints=_build_intractable_hints(context),
                journal=context.journal,
            )
            if results is not None:
                yield [r if r is not None else intractable for r in results]
            else:
                yield intractable
            return
        if progress is not None:
            now = time.monotonic()
            if now >= _progress_next_time:
                dt = now - _progress_last_time
                progress(len(visited), len(queue), dt)
                _progress_last_time = now
                _progress_next_time = now + 5.0

        snap, depth, parent_key, just_flipped, cur_bprev = queue.popleft()
        _parent_visible = _visible_key(parent_key)
        _bprev_dict: dict[str, Any] = (
            dict(zip(_demoted, cur_bprev, strict=True)) if _has_demoted else {}
        )
        if _progress_set_depth is not None:
            _progress_set_depth(depth)
        if depth >= depth_budget:
            depth_truncated = True
            if frontier_collector is not None and len(frontier_collector) < 1000:
                _restore_kernel(kernel, snap)
                frontier_collector.append(dict(kernel.tags))
            continue

        _restore_kernel(kernel, snap)
        live = (
            live_cache.live_inputs(kernel)
            if bfs_config.live_input_pruning
            else frozenset(context.nondeterministic_dims)
        )
        current_values = {
            name: kernel.tags.get(name, context.nondeterministic_dims[name][0]) for name in live
        }
        has_hidden_events = bool(context.done_event_specs or context.threshold_event_specs)
        _factoring = context.free_input_factoring
        _factoring_active = (
            bfs_config.free_input_factoring
            and _factoring is not None
            and not (paced and just_flipped)
            # Factored successors are currently generated only by the
            # single-outcome fast path below.  If the base assignment settles
            # or jumps a hidden event, that branch bypasses composition and
            # silently omits the factored input combinations.  Fall back to
            # exact joint enumeration until both paths share one successor
            # pipeline.
            and not has_hidden_events
        )
        _factored_names: frozenset[str] = frozenset()
        _group_combos: list[list[tuple[tuple[str, Any], ...]]] | None = None
        _shared_combos: list[tuple[tuple[str, Any], ...]] | None = None

        if _factoring_active:
            assert _factoring is not None
            _factored_names = frozenset().union(*_factoring.groups) | _factoring.shared_inputs

        if paced and just_flipped:
            assignments = [tuple(sorted(current_values.items()))]
        else:
            try:
                assignments = _iter_input_assignments(
                    live,
                    context.nondeterministic_dims,
                    context.exclusive_input_groups if bfs_config.exclusive_input_grouping else (),
                    context.exclusive_input_group_by_member
                    if bfs_config.exclusive_input_grouping
                    else {},
                    current_values=current_values,
                    joint_inputs=context.joint_inputs,
                    free_inputs=context.free_input_names,
                    factored_free=_factored_names,
                    sample=sampled_overflow is not None,
                )
            except _FreeComboOverflow as overflow:
                if results is None:
                    yield _free_combo_intractable(context, overflow, len(visited))
                    return
                # Keep searching a thinned sample: a counterexample it finds
                # is real, but what it does not find is not Proven.
                sampled_overflow = overflow
                assignments = _iter_input_assignments(
                    live,
                    context.nondeterministic_dims,
                    context.exclusive_input_groups if bfs_config.exclusive_input_grouping else (),
                    context.exclusive_input_group_by_member
                    if bfs_config.exclusive_input_grouping
                    else {},
                    current_values=current_values,
                    joint_inputs=context.joint_inputs,
                    free_inputs=context.free_input_names,
                    factored_free=_factored_names,
                    sample=True,
                )

        if _factoring_active:
            assert _factoring is not None
            _group_combos = []
            for _g_members in _factoring.groups:
                _live_g = sorted(n for n in _g_members if n in live)
                if not _live_g:
                    _stutter_only: tuple[tuple[str, Any], ...] = ()
                    _group_combos.append([_stutter_only])
                    continue
                _domains = [[(n, v) for v in context.nondeterministic_dims[n]] for n in _live_g]
                _group_combos.append([tuple(c) for c in itertools.product(*_domains)])
            _live_shared = sorted(n for n in _factoring.shared_inputs if n in live)
            if _live_shared:
                _sh_domains = [
                    [(n, v) for v in context.nondeterministic_dims[n]] for n in _live_shared
                ]
                _shared_combos = [tuple(c) for c in itertools.product(*_sh_domains)]
            else:
                _shared_combos = [()]

        seen_outcomes: set[tuple[tuple[Any, ...], tuple[Any, ...]]] | None = (
            set() if project is not None else None
        )

        _any_enqueued_ref = [False]

        def _max_states_intractable() -> Intractable:
            return Intractable(
                reason="max_states exceeded",
                dimensions=len(context.stateful_dims) + len(context.nondeterministic_dims),
                estimated_space=len(visited),
                hints=_build_intractable_hints(context),
                journal=context.journal,
            )

        def _collect_hidden_outcomes(
            key: tuple[Any, ...],
            before_snap: _KernelSnapshot,
            *,
            has_hidden_events: bool,
            jump_self_loop: bool,
        ) -> list[_HiddenEventOutcome] | None:
            if predicates is not None:
                assert results is not None
                any_unsettled = any(
                    results[i] is None and not predicates[i](kernel.tags)
                    for i in range(len(predicates))
                )
                if (
                    bfs_config.pending_settlement
                    and any_unsettled
                    and _has_pending_done(context, key)
                ):
                    settle_outcomes = _settle_pending(
                        context,
                        kernel,
                        before_snap,
                        edge_comp,
                        hidden_event_cache,
                    )
                    return settle_outcomes or None
                if (
                    bfs_config.hidden_event_jumping
                    and not any_unsettled
                    and has_hidden_events
                    and key in visited
                    and jump_self_loop
                    and _has_pending_hidden_event(context, key)
                ):
                    jumped = _maybe_jump_hidden_event(
                        context,
                        kernel,
                        before_snap,
                        visited,
                        key,
                        edge_comp,
                        hidden_event_cache,
                    )
                    return jumped or None
                return None

            if not (
                has_hidden_events and key in visited and _has_pending_hidden_event(context, key)
            ):
                return None

            event_outcomes: list[_HiddenEventOutcome] = []
            if bfs_config.pending_settlement and _has_pending_done(context, key):
                event_outcomes.extend(
                    _settle_pending(
                        context,
                        kernel,
                        before_snap,
                        edge_comp,
                        hidden_event_cache,
                    )
                )
            if bfs_config.hidden_event_jumping and jump_self_loop:
                event_outcomes.extend(
                    _maybe_jump_hidden_event(
                        context,
                        kernel,
                        before_snap,
                        visited,
                        key,
                        edge_comp,
                        hidden_event_cache,
                    )
                )
            return event_outcomes or None

        def _enqueue_current_successor(
            *,
            key: tuple[Any, ...],
            input_dict: dict[str, Any],
            edge_scans: int,
            edge_caveats: tuple[str, ...] = (),
            child_flipped: bool,
            parent_key: tuple[Any, ...],
            bprev_dict: dict[str, Any],
            depth: int,
            any_enqueued_ref: list[bool],
        ) -> Intractable | None:
            bprev = _extract_bprev(kernel)
            tid = _trace_id(key, bprev)
            if edge_collector is not None:
                edge_collector(
                    parent_key,
                    tid,
                    input_dict,
                    edge_scans,
                    edge_caveats,
                    dict(kernel.tags),
                )
            if not _should_enqueue(key, bprev):
                if cycles_active:
                    base_hidden = cycles_hidden.get(tid)
                    if base_hidden is None or cycles.changed_from(kernel, base_hidden) is None:
                        return None
                    return _refine_revisit(
                        key=key,
                        tid=tid,
                        input_dict=input_dict,
                        edge_scans=edge_scans,
                        edge_caveats=edge_caveats,
                        child_flipped=child_flipped,
                        parent_key=parent_key,
                        bprev_dict=bprev_dict,
                        bprev=bprev,
                        depth=depth,
                    )
                return None

            any_enqueued_ref[0] = True
            if len(visited) + cycles.refined_nodes > max_states:
                return _max_states_intractable()
            if cycles_active:
                cycles.record_node(
                    tid,
                    parent_key,
                    -1 if edge_caveats else edge_scans,
                    cycles.hidden_vector(kernel, key),
                    (input_dict, bprev_dict),
                )
            if parent_map is not None:
                parent_map[tid] = _ParentLink(
                    parent_key,
                    input_dict,
                    edge_scans,
                    edge_caveats,
                    prev=bprev_dict,
                )
            queue.append(
                (
                    _snapshot_kernel(kernel, _mutable, _base_keys),
                    depth + 1,
                    tid,
                    child_flipped,
                    bprev,
                )
            )
            return None

        def _record_projection_for_current(
            key: tuple[Any, ...],
            seen_outcomes: set[tuple[tuple[Any, ...], tuple[Any, ...]]] | None,
        ) -> bool:
            if project is None:
                return True
            projected_row = _projected_tuple(kernel, project)
            outcome = (key, projected_row)
            assert seen_outcomes is not None
            if outcome in seen_outcomes:
                return False
            seen_outcomes.add(outcome)
            projected_rows.add(projected_row)
            return True

        def _process_current_successor(
            *,
            key: tuple[Any, ...],
            input_dict: dict[str, Any],
            child_flipped: bool,
            record_failures: bool,
            skip_project_duplicate: bool,
            parent_key: tuple[Any, ...],
            bprev_dict: dict[str, Any],
            depth: int,
            any_enqueued_ref: list[bool],
            seen_outcomes: set[tuple[tuple[Any, ...], tuple[Any, ...]]] | None,
        ) -> Intractable | None:
            if state_filter is not None and state_filter(kernel.tags):
                return None
            if predicates is not None and record_failures:
                _record_failures(
                    state=kernel.tags,
                    p_key=parent_key,
                    input_dict=input_dict,
                    edge_scans=1,
                    bprev_dict=bprev_dict,
                )

            is_new_projection = _record_projection_for_current(key, seen_outcomes)
            if skip_project_duplicate and not is_new_projection:
                # An outcome this expansion already produced, unless it carries
                # new hidden progress (cycles.py).
                if not cycles_active:
                    return None
                base_hidden = cycles_hidden.get(
                    _trace_id(key, _extract_bprev(kernel)) if _has_demoted else key
                )
                if base_hidden is None or cycles.changed_from(kernel, base_hidden) is None:
                    return None

            return _enqueue_current_successor(
                key=key,
                input_dict=input_dict,
                edge_scans=1,
                child_flipped=child_flipped,
                parent_key=parent_key,
                bprev_dict=bprev_dict,
                depth=depth,
                any_enqueued_ref=any_enqueued_ref,
            )

        def _process_hidden_successors(
            outcomes: list[_HiddenEventOutcome],
            *,
            input_dict: dict[str, Any],
            child_flipped: bool,
            parent_key: tuple[Any, ...],
            bprev_dict: dict[str, Any],
            depth: int,
            any_enqueued_ref: list[bool],
            seen_outcomes: set[tuple[tuple[Any, ...], tuple[Any, ...]]] | None,
        ) -> Intractable | None:
            seen_branch_keys: set[tuple[Any, ...]] = set()
            for outcome in outcomes:
                branch_key = (*outcome.key, child_flipped) if paced else outcome.key
                is_new_branch = branch_key not in seen_branch_keys
                if is_new_branch:
                    seen_branch_keys.add(branch_key)
                _restore_kernel(kernel, outcome.snapshot)
                if state_filter is not None and state_filter(kernel.tags):
                    continue
                branch_edge_scans = 1 + outcome.additional_scans
                branch_input_dict = (
                    {**input_dict, **outcome.event_inputs}
                    if outcome.event_inputs is not None
                    else input_dict
                )

                if is_new_branch and predicates is not None:
                    _record_failures(
                        state=kernel.tags,
                        p_key=parent_key,
                        input_dict=branch_input_dict,
                        edge_scans=branch_edge_scans,
                        edge_caveats=outcome.caveats,
                        bprev_dict=bprev_dict,
                    )

                _record_projection_for_current(branch_key, seen_outcomes)

                if not is_new_branch:
                    continue

                intractable = _enqueue_current_successor(
                    key=branch_key,
                    input_dict=branch_input_dict,
                    edge_scans=branch_edge_scans,
                    edge_caveats=outcome.caveats,
                    child_flipped=child_flipped,
                    parent_key=parent_key,
                    bprev_dict=bprev_dict,
                    depth=depth,
                    any_enqueued_ref=any_enqueued_ref,
                )
                if intractable is not None:
                    return intractable
            return None

        def _ready_results() -> list[Counterexample | Proven | Intractable] | None:
            if results is not None and all(r is not None for r in results):
                ready = [r for r in results if r is not None]
                for ri in range(len(results)):
                    results[ri] = None
                return ready
            return None

        for input_assignment in assignments:
            if _progress_step is not None:
                _progress_step()
            if progress is not None:
                now = time.monotonic()
                if now >= _progress_next_time:
                    dt = now - _progress_last_time
                    progress(len(visited), len(queue), dt)
                    _progress_last_time = now
                    _progress_next_time = now + 5.0
            _restore_kernel(kernel, snap)
            if _has_demoted:
                for name, value in zip(_demoted, cur_bprev, strict=True):
                    kernel.prev[name] = value
            for name, value in input_assignment:
                kernel.tags[name] = value

            _step_kernel(context, kernel)
            _eval_count += 1
            tv = _threshold_vector_key(kernel, context.threshold_vector_specs)
            post_step_live = (
                live_cache.live_inputs(kernel, threshold_vector=tv)
                if bfs_config.live_input_pruning
                else None
            )
            child_flipped = (
                any(value != current_values.get(name) for name, value in input_assignment)
                if paced
                else False
            )
            new_key = _state_key(kernel, live=post_step_live, threshold_vector=tv)
            new_key = (*new_key, child_flipped) if paced else new_key

            _base_state_filtered = state_filter is not None and state_filter(kernel.tags)

            # A hidden-event jump models time elapsing while the program stays
            # on ONE plateau, so it is only valid as a self-loop: this input
            # step must not have transitioned to a *different* (already-visited)
            # state.  If it did, the accelerated successors belong to that other
            # state, not the one we are expanding.  Attributing them here drops
            # the intermediate transition — and the edge inputs that drove it —
            # from the trace, so the counterexample fails to replay (and the
            # jump's own delta math, keyed off the pre-step ``snap``, is bogus
            # across a transition).  The other state is enqueued in its own
            # right, so its successors are still reached when it is expanded;
            # suppressing the cross-transition jump only removes a spurious,
            # over-approximating edge.
            _jump_self_loop = (
                new_key[:-1] == _parent_visible[:-1] if paced else new_key == _parent_visible
            )

            input_dict: dict[str, Any] = dict(input_assignment)
            alt_outcomes = _collect_hidden_outcomes(
                new_key,
                snap,
                has_hidden_events=has_hidden_events,
                jump_self_loop=_jump_self_loop,
            )

            if alt_outcomes is not None:
                # The base post-step state is reachable regardless of where
                # settlement/jumping lands.  Always check predicates here:
                # settlement may diverge and mask a base-state violation.
                intractable = _process_current_successor(
                    key=new_key,
                    input_dict=input_dict,
                    child_flipped=child_flipped,
                    record_failures=not settled,
                    skip_project_duplicate=False,
                    parent_key=parent_key,
                    bprev_dict=_bprev_dict,
                    depth=depth,
                    any_enqueued_ref=_any_enqueued_ref,
                    seen_outcomes=seen_outcomes,
                )
                if intractable is not None:
                    if results is not None:
                        yield [r if r is not None else intractable for r in results]
                    else:
                        yield intractable
                    return

                intractable = _process_hidden_successors(
                    alt_outcomes,
                    input_dict=input_dict,
                    child_flipped=child_flipped,
                    parent_key=parent_key,
                    bprev_dict=_bprev_dict,
                    depth=depth,
                    any_enqueued_ref=_any_enqueued_ref,
                    seen_outcomes=seen_outcomes,
                )
                if intractable is not None:
                    if results is not None:
                        yield [r if r is not None else intractable for r in results]
                    else:
                        yield intractable
                    return

                ready = _ready_results()
                if ready is not None:
                    yield ready
            else:
                # Fast path: single base outcome — no snapshot/restore overhead.
                # The kernel is already in the post-step state.
                intractable = _process_current_successor(
                    key=new_key,
                    input_dict=input_dict,
                    child_flipped=child_flipped,
                    record_failures=True,
                    skip_project_duplicate=True,
                    parent_key=parent_key,
                    bprev_dict=_bprev_dict,
                    depth=depth,
                    any_enqueued_ref=_any_enqueued_ref,
                    seen_outcomes=seen_outcomes,
                )
                if intractable is not None:
                    if results is not None:
                        yield [r if r is not None else intractable for r in results]
                    else:
                        yield intractable
                    return

                ready = _ready_results()
                if ready is not None:
                    yield ready

                if _base_state_filtered:
                    continue

                # ---- Factored free-input composition ----
                if _factoring_active and _group_combos is not None and _shared_combos is not None:
                    assert _factoring is not None
                    _f_base_tags = dict(kernel.tags)
                    _f_base_memory = dict(kernel.memory)
                    _f_base_prev = dict(kernel.prev)
                    _f_base_scan_id = kernel.scan_id
                    _f_base_timestamp = kernel.timestamp

                    for _f_shared_combo in _shared_combos:
                        _f_all_deltas: list[
                            list[
                                tuple[
                                    tuple[tuple[str, Any], ...],
                                    dict[str, Any],
                                    dict[str, Any],
                                    dict[str, Any],
                                ]
                            ]
                        ] = []

                        for _f_gi, _f_g_combos in enumerate(_group_combos):
                            _f_g_write = _factoring.write_tags[_f_gi]
                            _f_g_deltas: list[
                                tuple[
                                    tuple[tuple[str, Any], ...],
                                    dict[str, Any],
                                    dict[str, Any],
                                    dict[str, Any],
                                ]
                            ] = []
                            for _f_combo in _f_g_combos:
                                _f_full_combo = (*_f_shared_combo, *_f_combo)
                                _f_is_stutter = all(
                                    v == current_values.get(n) for n, v in _f_full_combo
                                )
                                if _f_is_stutter:
                                    _f_g_deltas.append((_f_combo, {}, {}, {}))
                                    continue

                                _restore_kernel(kernel, snap)
                                if _has_demoted:
                                    for name, value in zip(_demoted, cur_bprev, strict=True):
                                        kernel.prev[name] = value
                                for name, value in input_assignment:
                                    kernel.tags[name] = value
                                for name, value in _f_shared_combo:
                                    kernel.tags[name] = value
                                for name, value in _f_combo:
                                    kernel.tags[name] = value
                                _step_kernel(context, kernel)
                                _eval_count += 1

                                _f_dt: dict[str, Any] = {}
                                for _f_t in _f_g_write:
                                    _f_tv = kernel.tags.get(_f_t)
                                    if _f_tv != _f_base_tags.get(_f_t):
                                        _f_dt[_f_t] = _f_tv
                                _f_dm: dict[str, Any] = {}
                                for _f_k in kernel.memory:
                                    if kernel.memory[_f_k] != _f_base_memory.get(_f_k):
                                        _f_dm[_f_k] = kernel.memory[_f_k]
                                _f_dp: dict[str, Any] = {}
                                for _f_k in kernel.prev:
                                    if kernel.prev[_f_k] != _f_base_prev.get(_f_k):
                                        _f_dp[_f_k] = kernel.prev[_f_k]
                                _f_g_deltas.append((_f_combo, _f_dt, _f_dm, _f_dp))
                            _f_all_deltas.append(_f_g_deltas)

                        for _f_composed in itertools.product(*_f_all_deltas):
                            if all(not d[1] and not d[2] and not d[3] for d in _f_composed):
                                continue

                            _f_merged_tags = dict(_f_base_tags)
                            _f_merged_mem = dict(_f_base_memory)
                            _f_merged_prev = dict(_f_base_prev)
                            _f_full_input: dict[str, Any] = dict(input_assignment)
                            _f_full_input.update(dict(_f_shared_combo))
                            for _f_combo, _f_dt, _f_dm, _f_dp in _f_composed:
                                _f_full_input.update(dict(_f_combo))
                            _f_merged_tags.update(_f_full_input)
                            for _f_combo, _f_dt, _f_dm, _f_dp in _f_composed:
                                _f_merged_tags.update(_f_dt)
                                _f_merged_mem.update(_f_dm)
                                _f_merged_prev.update(_f_dp)

                            kernel.tags.clear()
                            kernel.tags.update(_f_merged_tags)
                            kernel.memory.clear()
                            kernel.memory.update(_f_merged_mem)
                            kernel.prev.clear()
                            kernel.prev.update(_f_merged_prev)
                            kernel.scan_id = _f_base_scan_id
                            kernel.timestamp = _f_base_timestamp

                            if state_filter is not None and state_filter(kernel.tags):
                                continue

                            _f_tv = _threshold_vector_key(kernel, context.threshold_vector_specs)
                            _f_post_live = (
                                live_cache.live_inputs(kernel, threshold_vector=_f_tv)
                                if bfs_config.live_input_pruning
                                else None
                            )
                            _f_child_flipped = (
                                any(
                                    _f_full_input.get(n) != current_values.get(n)
                                    for n in _f_full_input
                                )
                                if paced
                                else False
                            )
                            _f_key = _state_key(kernel, live=_f_post_live, threshold_vector=_f_tv)
                            _f_key = (*_f_key, _f_child_flipped) if paced else _f_key

                            _f_parent_visible = _parent_visible
                            _f_jump_self_loop = (
                                _f_key[:-1] == _f_parent_visible[:-1]
                                if paced
                                else _f_key == _f_parent_visible
                            )
                            _f_alt_outcomes = _collect_hidden_outcomes(
                                _f_key,
                                snap,
                                has_hidden_events=has_hidden_events,
                                jump_self_loop=_f_jump_self_loop,
                            )

                            if _f_alt_outcomes is not None:
                                intractable = _process_current_successor(
                                    key=_f_key,
                                    input_dict=_f_full_input,
                                    child_flipped=_f_child_flipped,
                                    record_failures=not settled,
                                    skip_project_duplicate=False,
                                    parent_key=parent_key,
                                    bprev_dict=_bprev_dict,
                                    depth=depth,
                                    any_enqueued_ref=_any_enqueued_ref,
                                    seen_outcomes=seen_outcomes,
                                )
                                if intractable is not None:
                                    if results is not None:
                                        yield [r if r is not None else intractable for r in results]
                                    else:
                                        yield intractable
                                    return

                                intractable = _process_hidden_successors(
                                    _f_alt_outcomes,
                                    input_dict=_f_full_input,
                                    child_flipped=_f_child_flipped,
                                    parent_key=parent_key,
                                    bprev_dict=_bprev_dict,
                                    depth=depth,
                                    any_enqueued_ref=_any_enqueued_ref,
                                    seen_outcomes=seen_outcomes,
                                )
                                if intractable is not None:
                                    if results is not None:
                                        yield [r if r is not None else intractable for r in results]
                                    else:
                                        yield intractable
                                    return
                            else:
                                intractable = _process_current_successor(
                                    key=_f_key,
                                    input_dict=_f_full_input,
                                    child_flipped=_f_child_flipped,
                                    record_failures=True,
                                    skip_project_duplicate=True,
                                    parent_key=parent_key,
                                    bprev_dict=_bprev_dict,
                                    depth=depth,
                                    any_enqueued_ref=_any_enqueued_ref,
                                    seen_outcomes=seen_outcomes,
                                )
                                if intractable is not None:
                                    if results is not None:
                                        yield [r if r is not None else intractable for r in results]
                                    else:
                                        yield intractable
                                    return

                            ready = _ready_results()
                            if ready is not None:
                                yield ready

                ready = _ready_results()
                if ready is not None:
                    yield ready

    if project is not None:
        yield _projected_states(project, projected_rows)
        return

    caveats = context.caveats
    if depth_truncated:
        caveats = (
            *caveats,
            (
                f"BFS exhausted depth_budget={depth_budget}; deeper abstract states were not explored. "
                f"The property held for all {len(visited)} explored states but may fail "
                f"beyond depth_budget={depth_budget}."
            ),
        )

    journal = context.journal
    if journal is not None and depth_truncated:
        journal = replace(
            journal,
            notes=(
                *journal.notes,
                f"BFS exhausted depth_budget={depth_budget}; deeper abstract states were not explored.",
            ),
        )

    if sampled_overflow is not None:
        sampled = _free_combo_intractable(context, sampled_overflow, len(visited))
        yield [r if r is not None else sampled for r in results or [None]]
        return

    if results is not None:
        yield [
            r
            if r is not None
            else Proven(states_explored=len(visited), caveats=caveats, journal=journal)
            for r in results
        ]
        return

    yield [Proven(states_explored=len(visited), caveats=caveats, journal=journal)]
