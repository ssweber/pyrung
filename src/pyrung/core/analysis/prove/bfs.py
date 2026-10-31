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
from .blackbox import ORACLE_KEY, ChoiceOverflow, _TimerOracle, choice_runs
from .classify import _build_dimension_hints
from .independence import _find_bridge_tags
from .inputs import _FREE_COMBO_CAP, _FreeComboOverflow, _iter_input_assignments
from .kernel import (
    _EdgeCompressor,
    _extract_state_key,
    _KernelSnapshot,
    _LiveInputCache,
    _restore_kernel,
    _snapshot_kernel,
    _step_kernel,
)
from .passes import _DEFAULT_BFS_CONFIG, _BFSConfig
from .results import Counterexample, Intractable, Proven, TraceStep, _ParentLink
from .system_points import FIRST_SCAN, FREE_SYSTEM_CAVEAT, UnmodeledRead, UnmodeledSystemRead


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
        trace.append(
            TraceStep(
                inputs=link.inputs,
                scans=link.scans,
                prev=link.prev,
                timers=link.timers,
                _choices=link.choices,
            )
        )
    caveats = _merge_caveats(*(link.caveats for link in links))
    return trace, caveats


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
    except ChoiceOverflow as exc:
        names = ", ".join(exc.keys) or "several timers"
        overflow = Intractable(
            reason=(
                f"timers [{names}] can finish on one scan in more combinations than the"
                f" verifier enumerates"
            ),
            dimensions=len(context.stateful_dims) + len(context.nondeterministic_dims),
            estimated_space=0,
            tags=list(exc.keys),
            hints=[
                "  every running timer may finish on any scan, so timers running at once"
                " multiply each scan's successors; narrowing choices=/min=/max= does not"
                " help",
                "  prove a property whose scope reaches fewer of them at once, or check"
                " the timing with a run_for() test",
            ],
            journal=context.journal,
            kind="timers",
        )
        if predicates is None:
            return overflow
        return [overflow for _ in predicates]
    except UnmodeledRead as exc:
        assert predicates is not None
        if isinstance(exc, UnmodeledSystemRead):
            hint = (
                f"  {exc.name}: system tag the verifier explores only where a rung or a"
                f" condition property names it; write the property as a condition"
            )
        else:
            hint = (
                f"  {exc.name}: no rung reads or writes it, so only a condition property"
                f" brings it into the search; write the property as a condition"
            )
        refusal = Intractable(
            reason=(f"callable property reads {exc.name}, which this search did not explore"),
            dimensions=len(context.stateful_dims) + len(context.nondeterministic_dims),
            estimated_space=0,
            tags=[exc.name],
            hints=[hint],
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
    for name, value in context.outside_tag_defaults:
        kernel.tags[name] = value
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
    # Black-box timers: the kernel's ticks ask this oracle (blackbox.py).
    # ``_choice`` is (choices, moved ticks, a Done choice still open) of the
    # successor being processed.
    oracle = _TimerOracle(context.timer_model) if context.timer_model is not None else None
    if oracle is not None:
        kernel.memory[ORACLE_KEY] = oracle
    branching = oracle is not None and bool(context.timer_model and context.timer_model.branch)
    _no_choice: tuple[tuple[int, ...], tuple[tuple[str, int], ...], bool] = ((), (), False)
    _choice: list[tuple[tuple[int, ...], tuple[tuple[str, int], ...], bool]] = [_no_choice]
    edge_comp = _EdgeCompressor(context)
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
            nondeterministic_names=context.nondeterministic_names,
            live_inputs=live,
            memory_key_defaults=context.memory_key_defaults,
            count_clamps=context.count_clamps,
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
            choices, moved, _open = _choice[0]
            trace.append(
                TraceStep(
                    inputs=input_dict,
                    scans=edge_scans,
                    prev=bprev_dict or {},
                    timers=moved,
                    _choices=choices,
                )
            )
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
            # Composition merges one run per group; tick choices would need
            # every group's runs crossed with them.
            and not branching
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

        def _max_states_intractable() -> Intractable:
            return Intractable(
                reason="max_states exceeded",
                dimensions=len(context.stateful_dims) + len(context.nondeterministic_dims),
                estimated_space=len(visited),
                hints=_build_intractable_hints(context),
                journal=context.journal,
            )

        def _enqueue_current_successor(
            *,
            key: tuple[Any, ...],
            input_dict: dict[str, Any],
            child_flipped: bool,
            parent_key: tuple[Any, ...],
            bprev_dict: dict[str, Any],
            depth: int,
        ) -> Intractable | None:
            bprev = _extract_bprev(kernel)
            tid = _trace_id(key, bprev)
            if edge_collector is not None:
                edge_collector(
                    parent_key,
                    tid,
                    input_dict,
                    1,
                    (),
                    dict(kernel.tags),
                )
            if not _should_enqueue(key, bprev):
                return None
            if len(visited) > max_states:
                return _max_states_intractable()
            if parent_map is not None:
                parent_map[tid] = _ParentLink(
                    parent_key,
                    input_dict,
                    1,
                    prev=bprev_dict,
                    timers=_choice[0][1],
                    choices=_choice[0][0],
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
            parent_key: tuple[Any, ...],
            bprev_dict: dict[str, Any],
            depth: int,
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
            # An outcome this expansion already produced.
            if not _record_projection_for_current(key, seen_outcomes):
                return None
            return _enqueue_current_successor(
                key=key,
                input_dict=input_dict,
                child_flipped=child_flipped,
                parent_key=parent_key,
                bprev_dict=bprev_dict,
                depth=depth,
            )

        def _ready_results() -> list[Counterexample | Proven | Intractable] | None:
            if results is not None and all(r is not None for r in results):
                ready = [r for r in results if r is not None]
                for ri in range(len(results)):
                    results[ri] = None
                return ready
            return None

        def _stepped(
            assignments: Any,
            base: _KernelSnapshot = snap,
            base_bprev: tuple[Any, ...] = cur_bprev,
        ) -> Generator[tuple[tuple[str, Any], ...], None, None]:
            # One scan per input assignment, and under branching black-box
            # timers one per sequence of tick choices; each yield leaves the
            # kernel in the successor, with ``_choice`` describing it.
            def _prepare(input_assignment: tuple[tuple[str, Any], ...]) -> None:
                _restore_kernel(kernel, base)
                if _has_demoted:
                    for name, value in zip(_demoted, base_bprev, strict=True):
                        kernel.prev[name] = value
                for name, value in input_assignment:
                    kernel.tags[name] = value

            for input_assignment in assignments:
                if oracle is None:
                    _prepare(input_assignment)
                    _step_kernel(context, kernel)
                    yield input_assignment
                    continue

                def _run(assignment: tuple[tuple[str, Any], ...] = input_assignment) -> None:
                    _prepare(assignment)
                    _step_kernel(context, kernel)

                for _ in choice_runs(oracle, _run):
                    _choice[0] = oracle.outcome()
                    yield input_assignment
                _choice[0] = _no_choice

        for input_assignment in _stepped(assignments):
            if _progress_step is not None:
                _progress_step()
            if progress is not None:
                now = time.monotonic()
                if now >= _progress_next_time:
                    dt = now - _progress_last_time
                    progress(len(visited), len(queue), dt)
                    _progress_last_time = now
                    _progress_next_time = now + 5.0
            _eval_count += 1
            post_step_live = (
                live_cache.live_inputs(kernel) if bfs_config.live_input_pruning else None
            )
            child_flipped = (
                any(value != current_values.get(name) for name, value in input_assignment)
                if paced
                else False
            )
            new_key = _state_key(kernel, live=post_step_live)
            new_key = (*new_key, child_flipped) if paced else new_key

            _base_state_filtered = state_filter is not None and state_filter(kernel.tags)

            # Under ``settled`` a state where a timer could still finish (a
            # Done choice is open) is not judged; its settled successors are.
            intractable = _process_current_successor(
                key=new_key,
                input_dict=dict(input_assignment),
                child_flipped=child_flipped,
                record_failures=not (settled and _choice[0][2]),
                parent_key=parent_key,
                bprev_dict=_bprev_dict,
                depth=depth,
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

                        _f_post_live = (
                            live_cache.live_inputs(kernel)
                            if bfs_config.live_input_pruning
                            else None
                        )
                        _f_child_flipped = (
                            any(
                                _f_full_input.get(n) != current_values.get(n) for n in _f_full_input
                            )
                            if paced
                            else False
                        )
                        _f_key = _state_key(kernel, live=_f_post_live)
                        _f_key = (*_f_key, _f_child_flipped) if paced else _f_key

                        intractable = _process_current_successor(
                            key=_f_key,
                            input_dict=_f_full_input,
                            child_flipped=_f_child_flipped,
                            record_failures=True,
                            parent_key=parent_key,
                            bprev_dict=_bprev_dict,
                            depth=depth,
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
                f"BFS exhausted depth_budget={depth_budget}; deeper states were not explored. "
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
                f"BFS exhausted depth_budget={depth_budget}; deeper states were not explored.",
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
