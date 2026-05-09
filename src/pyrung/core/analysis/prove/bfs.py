"""BFS exploration loop and helpers for the prove subsystem."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
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
)
from .passes import _DEFAULT_BFS_CONFIG, _BFSConfig
from .results import Counterexample, Intractable, Proven, TraceStep, _ParentLink


def _projected_tuple(kernel: ReplayKernel, project_names: tuple[str, ...]) -> tuple[Any, ...]:
    """Project kernel state onto a fixed ordered list of tag names."""
    return tuple(kernel.tags.get(name) for name in project_names)


def _projected_states(
    project_names: tuple[str, ...],
    projected_rows: set[tuple[Any, ...]],
) -> frozenset[frozenset[tuple[str, Any]]]:
    """Convert ordered projection rows to the public frozenset shape."""
    return frozenset(frozenset(zip(project_names, row, strict=True)) for row in projected_rows)


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
    trace = [TraceStep(inputs=link.inputs, scans=link.scans) for link in links]
    caveats = _merge_caveats(*(link.caveats for link in links))
    return trace, caveats


def _bfs_explore(
    context: _ExploreContext,
    *,
    predicates: list[Callable[[dict[str, Any]], bool]] | None = None,
    project: tuple[str, ...] | None = None,
    depth_budget: int = 50,
    max_states: int = 100_000,
    bfs_config: _BFSConfig = _DEFAULT_BFS_CONFIG,
    progress: Callable[[int, int, float], None] | None = None,
) -> (
    list[Proven | Counterexample | Intractable]
    | frozenset[frozenset[tuple[str, Any]]]
    | Intractable
):
    """BFS over the reachable state space."""
    kernel = context.compiled.create_kernel()
    _seed_synthetic_presets(context, kernel)
    edge_comp = _EdgeCompressor(context)
    hidden_event_cache = _HiddenEventCache(context)
    live_cache = _LiveInputCache(context)

    def _state_key(
        k: ReplayKernel,
        live: frozenset[str] | None = None,
    ) -> tuple[Any, ...]:
        if bfs_config.edge_compression:
            return edge_comp.state_key(k, live_inputs=live)
        return _extract_state_key(
            k,
            context.stateful_names,
            context.edge_tag_names,
            context.memory_key_names,
            context.state_key_done_specs,
            context.threshold_vector_specs,
            nondeterministic_names=context.nondeterministic_names,
            live_inputs=live,
        )

    initial_key = _state_key(kernel)

    visited: set[tuple[Any, ...]] = {initial_key}
    parent_map: dict[tuple[Any, ...], _ParentLink] | None = (
        {initial_key: _ParentLink(None, {}, 0)} if predicates is not None else None
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
    ) -> None:
        assert predicates is not None and results is not None and parent_map is not None
        for i, predicate in enumerate(predicates):
            if results[i] is not None:
                continue
            if predicate(state):
                continue
            if initial:
                results[i] = Counterexample(trace=[TraceStep(inputs={}, scans=0)])
                continue
            trace, trace_caveats = _build_trace(parent_map, p_key)
            trace.append(TraceStep(inputs=input_dict, scans=edge_scans))
            results[i] = Counterexample(
                trace=trace,
                caveats=_merge_caveats(trace_caveats, edge_caveats),
            )

    if predicates is not None:
        _record_failures(
            state=kernel.tags,
            p_key=initial_key,
            input_dict={},
            edge_scans=0,
            initial=True,
        )
        assert results is not None
        if all(r is not None for r in results):
            return [r for r in results if r is not None]

    queue: deque[tuple[_KernelSnapshot, int, tuple[Any, ...]]] = deque()
    queue.append((_snapshot_kernel(kernel), 0, initial_key))

    _progress_last_time = time.monotonic()
    _progress_next_time = _progress_last_time + 5.0
    _progress_step: Callable[[], None] | None = (
        getattr(progress, "step", None) if progress is not None else None
    )
    _progress_set_depth: Callable[[int], None] | None = (
        getattr(progress, "set_depth", None) if progress is not None else None
    )
    depth_truncated = False

    while queue:
        if progress is not None:
            now = time.monotonic()
            if now >= _progress_next_time:
                dt = now - _progress_last_time
                progress(len(visited), len(queue), dt)
                _progress_last_time = now
                _progress_next_time = now + 5.0

        snap, depth, parent_key = queue.popleft()
        if _progress_set_depth is not None:
            _progress_set_depth(depth)
        if depth >= depth_budget:
            depth_truncated = True
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
        assignments = _iter_input_assignments(
            live,
            context.nondeterministic_dims,
            context.exclusive_input_groups if bfs_config.exclusive_input_grouping else (),
            context.exclusive_input_group_by_member if bfs_config.exclusive_input_grouping else {},
            current_values=current_values,
            joint_inputs=context.joint_inputs,
            free_inputs=context.free_input_names,
        )

        has_hidden_events = bool(context.done_event_specs or context.threshold_event_specs)
        seen_outcomes: set[tuple[tuple[Any, ...], tuple[Any, ...]]] | None = (
            set() if project is not None else None
        )
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
            for name, value in input_assignment:
                kernel.tags[name] = value

            _step_kernel(context, kernel)
            post_step_live = (
                live_cache.live_inputs(kernel) if bfs_config.live_input_pruning else None
            )
            new_key = _state_key(kernel, live=post_step_live)

            # Determine if hidden-event branching produces alternate outcomes.
            # Settlement/jumping functions do their own internal save/restore,
            # so we never need a speculative snapshot of the base state.
            alt_outcomes: (
                list[tuple[_KernelSnapshot, tuple[Any, ...], int, tuple[str, ...]]] | None
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
                    settled = _settle_pending(
                        context,
                        kernel,
                        snap,
                        edge_comp,
                        hidden_event_cache,
                    )
                    if settled:
                        alt_outcomes = [
                            (
                                outcome.snapshot,
                                outcome.key,
                                outcome.additional_scans,
                                outcome.caveats,
                            )
                            for outcome in settled
                        ]
                elif (
                    bfs_config.hidden_event_jumping
                    and not any_unsettled
                    and has_hidden_events
                    and new_key in visited
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
                            )
                            for outcome in jumped
                        ]
            elif (
                bfs_config.hidden_event_jumping
                and has_hidden_events
                and new_key in visited
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
                        )
                        for outcome in jumped
                    ]

            if alt_outcomes is not None:
                # Slow path: process alternate outcomes from hidden events.
                # Build input_dict only here (needed for traces / parent_map).
                input_dict: dict[str, Any] = dict(input_assignment)

                # Capture base post-step projection before processing jumps.
                # The base state is a valid reachable snapshot even when jump
                # destinations diverge (e.g. a latch fires mid-step, killing
                # the timer that the jump tried to settle).
                if project is not None:
                    base_projected = _projected_tuple(kernel, project)
                    base_outcome = (new_key, base_projected)
                    assert seen_outcomes is not None
                    if base_outcome not in seen_outcomes:
                        seen_outcomes.add(base_outcome)
                        projected_rows.add(base_projected)

                seen_branch_keys: set[tuple[Any, ...]] = set()
                for (
                    branch_snapshot,
                    branch_key,
                    branch_additional_scans,
                    branch_caveats,
                ) in alt_outcomes:
                    if branch_key in seen_branch_keys:
                        continue
                    seen_branch_keys.add(branch_key)
                    _restore_kernel(kernel, branch_snapshot)
                    branch_edge_scans = 1 + branch_additional_scans

                    if predicates is not None:
                        _record_failures(
                            state=kernel.tags,
                            p_key=parent_key,
                            input_dict=input_dict,
                            edge_scans=branch_edge_scans,
                            edge_caveats=branch_caveats,
                        )

                    if project is not None:
                        projected_row = _projected_tuple(kernel, project)
                        outcome = (branch_key, projected_row)
                        assert seen_outcomes is not None
                        if outcome in seen_outcomes:
                            continue
                        seen_outcomes.add(outcome)
                        projected_rows.add(projected_row)

                    if branch_key not in visited:
                        visited.add(branch_key)
                        if len(visited) > max_states:
                            intractable = Intractable(
                                reason="max_states exceeded",
                                dimensions=len(context.stateful_dims)
                                + len(context.nondeterministic_dims),
                                estimated_space=len(visited),
                                hints=_build_dimension_hints(context),
                            )
                            if results is not None:
                                return [r if r is not None else intractable for r in results]
                            return intractable
                        if parent_map is not None:
                            parent_map[branch_key] = _ParentLink(
                                parent_key,
                                input_dict,
                                branch_edge_scans,
                                branch_caveats,
                            )
                        queue.append((_snapshot_kernel(kernel), depth + 1, branch_key))

                    if results is not None and all(r is not None for r in results):
                        return [r for r in results if r is not None]
            else:
                # Fast path: single base outcome — no snapshot/restore overhead.
                # The kernel is already in the post-step state.
                if predicates is not None:
                    input_dict = dict(input_assignment)
                    _record_failures(
                        state=kernel.tags,
                        p_key=parent_key,
                        input_dict=input_dict,
                        edge_scans=1,
                    )

                if project is not None:
                    projected_row = _projected_tuple(kernel, project)
                    outcome_pair = (new_key, projected_row)
                    assert seen_outcomes is not None
                    if outcome_pair in seen_outcomes:
                        continue
                    seen_outcomes.add(outcome_pair)
                    projected_rows.add(projected_row)

                if new_key not in visited:
                    visited.add(new_key)
                    if len(visited) > max_states:
                        intractable = Intractable(
                            reason="max_states exceeded",
                            dimensions=len(context.stateful_dims)
                            + len(context.nondeterministic_dims),
                            estimated_space=len(visited),
                            hints=_build_dimension_hints(context),
                        )
                        if results is not None:
                            return [r if r is not None else intractable for r in results]
                        return intractable
                    if parent_map is not None:
                        input_dict = dict(input_assignment)
                        parent_map[new_key] = _ParentLink(parent_key, input_dict, 1)
                    queue.append((_snapshot_kernel(kernel), depth + 1, new_key))

                if results is not None and all(r is not None for r in results):
                    return [r for r in results if r is not None]

    if project is not None:
        return _projected_states(project, projected_rows)

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
    if results is not None:
        return [
            r if r is not None else Proven(states_explored=len(visited), caveats=caveats)
            for r in results
        ]

    return [Proven(states_explored=len(visited), caveats=caveats)]
