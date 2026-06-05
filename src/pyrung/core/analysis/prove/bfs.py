"""BFS exploration loop and helpers for the prove subsystem."""

from __future__ import annotations

import itertools
import time
from collections import deque
from collections.abc import Callable, Generator
from dataclasses import replace
from typing import Any

from pyrung.core.kernel import ReplayKernel

from . import _ExploreContext
from .classify import _build_dimension_hints
from .events import (
    _has_pending_done,
    _has_pending_hidden_event,
    _HiddenEventCache,
    _maybe_jump_hidden_event,
    _settle_pending,
)
from .independence import _find_bridge_tags
from .inputs import _iter_input_assignments
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
    trace = [TraceStep(inputs=link.inputs, scans=link.scans, prev=link.prev) for link in links]
    caveats = _merge_caveats(*(link.caveats for link in links))
    return trace, caveats


_BFSResult = (
    list[Proven | Counterexample | Intractable]
    | frozenset[frozenset[tuple[str, Any]]]
    | Intractable
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
    return next(
        _bfs_explore_gen(
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
    )


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
    if initial_state is not None:
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
            results[i] = Counterexample(
                trace=trace,
                caveats=_merge_caveats(trace_caveats, edge_caveats),
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
        _factoring = context.free_input_factoring
        _factoring_active = (
            bfs_config.free_input_factoring
            and _factoring is not None
            and not (paced and just_flipped)
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

        has_hidden_events = bool(context.done_event_specs or context.threshold_event_specs)
        seen_outcomes: set[tuple[tuple[Any, ...], tuple[Any, ...]]] | None = (
            set() if project is not None else None
        )

        _any_enqueued_ref = [False]

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
            _parent_visible = parent_key[0] if _has_demoted else parent_key
            _jump_self_loop = (
                new_key[:-1] == _parent_visible[:-1] if paced else new_key == _parent_visible
            )

            # Determine if hidden-event branching produces alternate outcomes.
            # Settlement/jumping functions do their own internal save/restore,
            # so we never need a speculative snapshot of the base state.
            alt_outcomes: (
                list[
                    tuple[
                        _KernelSnapshot,
                        tuple[Any, ...],
                        int,
                        tuple[str, ...],
                        dict[str, Any] | None,
                    ]
                ]
                | None
            ) = None

            if predicates is not None:
                assert results is not None
                any_unsettled = any(
                    results[i] is None and not predicates[i](kernel.tags)
                    for i in range(len(predicates))
                )
                if (
                    bfs_config.pending_settlement
                    and any_unsettled
                    and _has_pending_done(context, new_key)
                ):
                    settle_outcomes = _settle_pending(
                        context,
                        kernel,
                        snap,
                        edge_comp,
                        hidden_event_cache,
                    )
                    if settle_outcomes:
                        alt_outcomes = [
                            (
                                outcome.snapshot,
                                outcome.key,
                                outcome.additional_scans,
                                outcome.caveats,
                                outcome.event_inputs,
                            )
                            for outcome in settle_outcomes
                        ]
                elif (
                    bfs_config.hidden_event_jumping
                    and not any_unsettled
                    and has_hidden_events
                    and new_key in visited
                    and _jump_self_loop
                    and _has_pending_hidden_event(context, new_key)
                ):
                    jumped = _maybe_jump_hidden_event(
                        context,
                        kernel,
                        snap,
                        visited,
                        new_key,
                        edge_comp,
                        hidden_event_cache,
                    )
                    if jumped:
                        alt_outcomes = [
                            (
                                outcome.snapshot,
                                outcome.key,
                                outcome.additional_scans,
                                outcome.caveats,
                                outcome.event_inputs,
                            )
                            for outcome in jumped
                        ]
            elif (
                has_hidden_events
                and new_key in visited
                and _has_pending_hidden_event(context, new_key)
            ):
                _ev_outcomes: list[
                    tuple[
                        _KernelSnapshot,
                        tuple[Any, ...],
                        int,
                        tuple[str, ...],
                        dict[str, Any] | None,
                    ]
                ] = []
                if bfs_config.pending_settlement and _has_pending_done(context, new_key):
                    for o in _settle_pending(
                        context,
                        kernel,
                        snap,
                        edge_comp,
                        hidden_event_cache,
                    ):
                        _ev_outcomes.append(
                            (o.snapshot, o.key, o.additional_scans, o.caveats, o.event_inputs)
                        )
                if bfs_config.hidden_event_jumping and _jump_self_loop:
                    for o in _maybe_jump_hidden_event(
                        context,
                        kernel,
                        snap,
                        visited,
                        new_key,
                        edge_comp,
                        hidden_event_cache,
                    ):
                        _ev_outcomes.append(
                            (o.snapshot, o.key, o.additional_scans, o.caveats, o.event_inputs)
                        )
                if _ev_outcomes:
                    alt_outcomes = _ev_outcomes

            if alt_outcomes is not None:
                # Slow path: process alternate outcomes from hidden events.
                # Build input_dict only here (needed for traces / parent_map).
                input_dict: dict[str, Any] = dict(input_assignment)

                if not _base_state_filtered:
                    # The base post-step state is reachable regardless of where
                    # settlement/jumping lands.  Always check predicates here —
                    # settlement may diverge (e.g. a counter reset undoes the
                    # fast-forward, masking a violation that exists in the base).
                    if predicates is not None and not settled:
                        _record_failures(
                            state=kernel.tags,
                            p_key=parent_key,
                            input_dict=input_dict,
                            edge_scans=1,
                            bprev_dict=_bprev_dict,
                        )

                    if project is not None:
                        base_projected = _projected_tuple(kernel, project)
                        base_outcome = (new_key, base_projected)
                        assert seen_outcomes is not None
                        if base_outcome not in seen_outcomes:
                            seen_outcomes.add(base_outcome)
                            projected_rows.add(base_projected)

                    base_bprev = _extract_bprev(kernel)
                    base_tid = _trace_id(new_key, base_bprev)
                    if edge_collector is not None:
                        edge_collector(parent_key, base_tid, input_dict, 1, (), dict(kernel.tags))
                    if _should_enqueue(new_key, base_bprev):
                        _any_enqueued_ref[0] = True
                        if len(visited) > max_states:
                            intractable = Intractable(
                                reason="max_states exceeded",
                                dimensions=len(context.stateful_dims)
                                + len(context.nondeterministic_dims),
                                estimated_space=len(visited),
                                hints=_build_intractable_hints(context),
                                journal=context.journal,
                            )
                            if results is not None:
                                yield [r if r is not None else intractable for r in results]
                            else:
                                yield intractable
                            return
                        if parent_map is not None:
                            parent_map[base_tid] = _ParentLink(
                                parent_key, input_dict, 1, prev=_bprev_dict
                            )
                        queue.append(
                            (
                                _snapshot_kernel(kernel, _mutable, _base_keys),
                                depth + 1,
                                base_tid,
                                child_flipped,
                                base_bprev,
                            )
                        )

                seen_branch_keys: set[tuple[Any, ...]] = set()
                for (
                    branch_snapshot,
                    branch_base_key,
                    branch_additional_scans,
                    branch_caveats,
                    branch_event_inputs,
                ) in alt_outcomes:
                    branch_key = (*branch_base_key, child_flipped) if paced else branch_base_key
                    is_new_branch = branch_key not in seen_branch_keys
                    if is_new_branch:
                        seen_branch_keys.add(branch_key)
                    _restore_kernel(kernel, branch_snapshot)
                    if state_filter is not None and state_filter(kernel.tags):
                        continue
                    branch_edge_scans = 1 + branch_additional_scans
                    branch_input_dict = (
                        {**input_dict, **branch_event_inputs}
                        if branch_event_inputs is not None
                        else input_dict
                    )

                    if is_new_branch and predicates is not None:
                        _record_failures(
                            state=kernel.tags,
                            p_key=parent_key,
                            input_dict=branch_input_dict,
                            edge_scans=branch_edge_scans,
                            edge_caveats=branch_caveats,
                            bprev_dict=_bprev_dict,
                        )

                    if project is not None:
                        projected_row = _projected_tuple(kernel, project)
                        outcome = (branch_key, projected_row)
                        assert seen_outcomes is not None
                        if outcome not in seen_outcomes:
                            seen_outcomes.add(outcome)
                            projected_rows.add(projected_row)

                    if not is_new_branch:
                        continue

                    branch_bprev = _extract_bprev(kernel)
                    branch_tid = _trace_id(branch_key, branch_bprev)
                    if edge_collector is not None:
                        edge_collector(
                            parent_key,
                            branch_tid,
                            branch_input_dict,
                            branch_edge_scans,
                            branch_caveats,
                            dict(kernel.tags),
                        )
                    if _should_enqueue(branch_key, branch_bprev):
                        _any_enqueued_ref[0] = True
                        if len(visited) > max_states:
                            intractable = Intractable(
                                reason="max_states exceeded",
                                dimensions=len(context.stateful_dims)
                                + len(context.nondeterministic_dims),
                                estimated_space=len(visited),
                                hints=_build_intractable_hints(context),
                                journal=context.journal,
                            )
                            if results is not None:
                                yield [r if r is not None else intractable for r in results]
                            else:
                                yield intractable
                            return
                        if parent_map is not None:
                            parent_map[branch_tid] = _ParentLink(
                                parent_key,
                                branch_input_dict,
                                branch_edge_scans,
                                branch_caveats,
                                prev=_bprev_dict,
                            )
                        queue.append(
                            (
                                _snapshot_kernel(kernel, _mutable, _base_keys),
                                depth + 1,
                                branch_tid,
                                child_flipped,
                                branch_bprev,
                            )
                        )

                    if results is not None and all(r is not None for r in results):
                        yield [r for r in results if r is not None]
                        for _ri in range(len(results)):
                            results[_ri] = None
            else:
                # Fast path: single base outcome — no snapshot/restore overhead.
                # The kernel is already in the post-step state.
                if _base_state_filtered:
                    continue
                if predicates is not None:
                    input_dict = dict(input_assignment)
                    _record_failures(
                        state=kernel.tags,
                        p_key=parent_key,
                        input_dict=input_dict,
                        edge_scans=1,
                        bprev_dict=_bprev_dict,
                    )

                if project is not None:
                    projected_row = _projected_tuple(kernel, project)
                    outcome_pair = (new_key, projected_row)
                    assert seen_outcomes is not None
                    if outcome_pair in seen_outcomes:
                        continue
                    seen_outcomes.add(outcome_pair)
                    projected_rows.add(projected_row)

                new_bprev = _extract_bprev(kernel)
                new_tid = _trace_id(new_key, new_bprev)
                if edge_collector is not None:
                    edge_collector(
                        parent_key,
                        new_tid,
                        dict(input_assignment),
                        1,
                        (),
                        dict(kernel.tags),
                    )
                if _should_enqueue(new_key, new_bprev):
                    _any_enqueued_ref[0] = True
                    if len(visited) > max_states:
                        intractable = Intractable(
                            reason="max_states exceeded",
                            dimensions=len(context.stateful_dims)
                            + len(context.nondeterministic_dims),
                            estimated_space=len(visited),
                            hints=_build_intractable_hints(context),
                            journal=context.journal,
                        )
                        if results is not None:
                            yield [r if r is not None else intractable for r in results]
                        else:
                            yield intractable
                        return
                    if parent_map is not None:
                        input_dict = dict(input_assignment)
                        parent_map[new_tid] = _ParentLink(
                            parent_key, input_dict, 1, prev=_bprev_dict
                        )
                    queue.append(
                        (
                            _snapshot_kernel(kernel, _mutable, _base_keys),
                            depth + 1,
                            new_tid,
                            child_flipped,
                            new_bprev,
                        )
                    )

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
                                _f_merged_tags.update(_f_dt)
                                _f_merged_mem.update(_f_dm)
                                _f_merged_prev.update(_f_dp)
                                _f_full_input.update(dict(_f_combo))

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

                            if predicates is not None:
                                _record_failures(
                                    state=kernel.tags,
                                    p_key=parent_key,
                                    input_dict=_f_full_input,
                                    edge_scans=1,
                                    bprev_dict=_bprev_dict,
                                )

                            if project is not None:
                                _f_projected = _projected_tuple(kernel, project)
                                _f_outcome = (_f_key, _f_projected)
                                assert seen_outcomes is not None
                                if _f_outcome in seen_outcomes:
                                    continue
                                seen_outcomes.add(_f_outcome)
                                projected_rows.add(_f_projected)

                            _f_bprev = _extract_bprev(kernel)
                            _f_tid = _trace_id(_f_key, _f_bprev)
                            if edge_collector is not None:
                                edge_collector(
                                    parent_key, _f_tid, _f_full_input, 1, (), dict(kernel.tags)
                                )
                            if _should_enqueue(_f_key, _f_bprev):
                                _any_enqueued_ref[0] = True
                                if len(visited) > max_states:
                                    intractable = Intractable(
                                        reason="max_states exceeded",
                                        dimensions=len(context.stateful_dims)
                                        + len(context.nondeterministic_dims),
                                        estimated_space=len(visited),
                                        hints=_build_intractable_hints(context),
                                        journal=context.journal,
                                    )
                                    if results is not None:
                                        yield [r if r is not None else intractable for r in results]
                                    else:
                                        yield intractable
                                    return
                                if parent_map is not None:
                                    parent_map[_f_tid] = _ParentLink(
                                        parent_key, _f_full_input, 1, prev=_bprev_dict
                                    )
                                # kernel currently holds the merged state (set
                                # above), so snapshot it directly — this scopes
                                # the factored snapshot the same as every other.
                                queue.append(
                                    (
                                        _snapshot_kernel(kernel, _mutable, _base_keys),
                                        depth + 1,
                                        _f_tid,
                                        _f_child_flipped,
                                        _f_bprev,
                                    )
                                )

                            if results is not None and all(r is not None for r in results):
                                yield [r for r in results if r is not None]
                                for _ri in range(len(results)):
                                    results[_ri] = None

                if results is not None and all(r is not None for r in results):
                    yield [r for r in results if r is not None]
                    for _ri in range(len(results)):
                        results[_ri] = None

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

    if results is not None:
        yield [
            r
            if r is not None
            else Proven(states_explored=len(visited), caveats=caveats, journal=journal)
            for r in results
        ]
        return

    yield [Proven(states_explored=len(visited), caveats=caveats, journal=journal)]
