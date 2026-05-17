"""Traced influence graph for state-key elision.

Builds a dependency graph from actual runner execution by instrumenting
ScanContext to record all tag/memory reads and writes per rung.  Two
traced scans (forced-on, forced-off) produce data, control, and entry
preservation edges.  Backward cone analysis from observers determines
which tags are elidable.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pdg import ProgramGraph, _extract_tag_names
from pyrung.core.analysis.simplified import Expr
from pyrung.core.condition import (
    AllCondition,
    AnyCondition,
    FallingEdgeCondition,
    RisingEdgeCondition,
)
from pyrung.core.context import ConditionView, ScanContext
from pyrung.core.executor import NOOP_OBSERVER, ExecutionMode, execute_program
from pyrung.core.rung import Rung
from pyrung.core.state import SystemState
from pyrung.core.tag import ImmediateRef

from ..expr import _referenced_tags
from ..results import PENDING

if TYPE_CHECKING:
    from pyrung.core.program import Program


# ---------------------------------------------------------------------------
# Step 1: TracingScanContext
# ---------------------------------------------------------------------------

_ACCESS_READ = True
_ACCESS_WRITE = False


class _Access:
    __slots__ = ("name", "kind", "rung_index", "step_index", "is_read", "from_entry")

    def __init__(
        self,
        name: str,
        kind: str,
        rung_index: int,
        step_index: int,
        is_read: bool,
        from_entry: bool,
    ) -> None:
        self.name = name
        self.kind = kind
        self.rung_index = rung_index
        self.step_index = step_index
        self.is_read = is_read
        self.from_entry = from_entry


class TracingScanContext(ScanContext):
    """ScanContext that records every tag and memory access with step attribution."""

    __slots__ = ("_trace", "_current_rung_index", "_current_step_index", "_next_step_index")

    def __init__(self, state: SystemState) -> None:
        super().__init__(state)
        self._trace: list[_Access] = []
        self._current_rung_index: int = -1
        self._current_step_index: int = -1
        self._next_step_index: int = 0

    def _begin_step(self) -> None:
        self._current_step_index = self._next_step_index
        self._next_step_index += 1

    def _new_condition_view(self) -> TracingConditionView:
        return TracingConditionView(self)

    def _record_access(self, name: str, kind: str, is_read: bool, from_entry: bool) -> None:
        if self._current_rung_index >= 0:
            self._trace.append(
                _Access(
                    name,
                    kind,
                    self._current_rung_index,
                    self._current_step_index,
                    is_read,
                    from_entry,
                )
            )

    def get_tag(self, name: str, default: Any = None) -> Any:
        from_entry = name not in self._tags_pending
        result = super().get_tag(name, default)
        self._record_access(name, "tag", _ACCESS_READ, from_entry)
        return result

    def set_tag(self, name: str, value: Any) -> None:
        super().set_tag(name, value)
        self._record_access(name, "tag", _ACCESS_WRITE, False)

    def set_tags(self, updates: dict[str, Any]) -> None:
        super().set_tags(updates)
        for name in updates:
            self._record_access(name, "tag", _ACCESS_WRITE, False)

    def get_memory(self, key: str, default: Any = None) -> Any:
        from_entry = key not in self._memory_pending
        result = super().get_memory(key, default)
        self._record_access(key, "memory", _ACCESS_READ, from_entry)
        return result

    def set_memory(self, key: str, value: Any) -> None:
        super().set_memory(key, value)
        self._record_access(key, "memory", _ACCESS_WRITE, False)

    def set_memory_bulk(self, updates: dict[str, Any]) -> None:
        super().set_memory_bulk(updates)
        for key in updates:
            self._record_access(key, "memory", _ACCESS_WRITE, False)


class TracingConditionView(ConditionView):
    """Condition snapshot view that reports reads back to the tracing context."""

    __slots__ = ("_trace_ctx",)

    def __init__(self, ctx: TracingScanContext) -> None:
        super().__init__(ctx)
        self._trace_ctx = ctx

    def get_tag(self, name: str, default: Any = None) -> Any:
        from_entry = name not in self._tags_snapshot
        result = super().get_tag(name, default)
        self._trace_ctx._record_access(name, "tag", _ACCESS_READ, from_entry)
        return result

    def get_memory(self, key: str, default: Any = None) -> Any:
        from_entry = key not in self._memory_snapshot
        result = super().get_memory(key, default)
        self._trace_ctx._record_access(key, "memory", _ACCESS_READ, from_entry)
        return result


class TracingObserver:
    """Execution observer that assigns trace step ids to execution boundaries."""

    __slots__ = ()

    def _begin(self, ctx: ScanContext, rung_index: int) -> None:
        if isinstance(ctx, TracingScanContext):
            ctx._current_rung_index = rung_index
            ctx._begin_step()

    begin_rung = NOOP_OBSERVER.begin_rung

    def begin_condition(
        self,
        ctx: ScanContext,
        rung_index: int,
        rung: Rung,
        kind: str,
        depth: int,
        subroutine_name: str | None,
        call_stack: tuple[str, ...],
    ) -> None:
        del rung, kind, depth, subroutine_name, call_stack
        self._begin(ctx, rung_index)

    def begin_branch(
        self,
        ctx: ScanContext,
        rung_index: int,
        branch: Rung,
        depth: int,
        enabled: bool,
        call_stack: tuple[str, ...],
    ) -> None:
        del branch, depth, enabled, call_stack
        self._begin(ctx, rung_index)

    def begin_instruction(
        self,
        ctx: ScanContext,
        rung_index: int,
        rung: Rung,
        instruction: Any,
        depth: int,
        enabled: bool,
        call_stack: tuple[str, ...],
    ) -> None:
        del rung, instruction, depth, enabled, call_stack
        self._begin(ctx, rung_index)

    def begin_subroutine_call(
        self,
        ctx: ScanContext,
        rung_index: int,
        instruction: Any,
        depth: int,
        call_stack: tuple[str, ...],
    ) -> None:
        del instruction, depth, call_stack
        self._begin(ctx, rung_index)

    def begin_loop_iteration(
        self,
        ctx: ScanContext,
        rung_index: int,
        instruction: Any,
        iteration: int,
        depth: int,
        call_stack: tuple[str, ...],
    ) -> None:
        del instruction, iteration, depth, call_stack
        self._begin(ctx, rung_index)


# ---------------------------------------------------------------------------
# Step 2: Traced scan execution
# ---------------------------------------------------------------------------


def _traced_scan(
    program: Program,
    state: SystemState,
    *,
    mode: ExecutionMode,
) -> list[_Access]:
    """Run one scan with tracing in the requested execution mode."""
    ctx = TracingScanContext(state)
    ctx.set_memory("_dt", 0.010)
    execute_program(program, ctx, mode=mode, observer=TracingObserver())
    ctx._current_rung_index = -1
    ctx._current_step_index = -1
    return ctx._trace


def _traced_scan_natural(
    program: Program,
    state: SystemState,
) -> list[_Access]:
    """Run one scan with natural condition evaluation, tracing all accesses."""
    return _traced_scan(program, state, mode="natural")


# ---------------------------------------------------------------------------
# Step 3: Influence graph + cone analysis
# ---------------------------------------------------------------------------


def _static_condition_reads(program: Program, graph: ProgramGraph) -> dict[int, frozenset[str]]:
    """Extract static condition reads per top-level rung index."""
    tag_refs = dict(graph.tags)
    result: dict[int, frozenset[str]] = {}
    for rung_index, rung in enumerate(program.rungs):
        names: set[str] = set()
        _collect_condition_reads(rung, names, tag_refs)
        result[rung_index] = frozenset(names)
    return result


def _collect_condition_reads(rung: Any, names: set[str], tag_refs: dict[str, Any]) -> None:
    for cond in rung._conditions:
        names.update(_extract_tag_names(cond, tag_refs))
    for branch in rung._branches:
        _collect_condition_reads(branch, names, tag_refs)


def _build_influence_graph(
    program: Program,
    graph: ProgramGraph,
    on_trace: list[_Access],
    *,
    inert_oneshot_only_tags: frozenset[str] = frozenset(),
) -> dict[str, set[str]]:
    """Build influence edges: for each node, which nodes does it depend on.

    Returns ``depends_on[node] = {upstream_nodes}``.  Nodes are tag or memory
    key names.  Special ``entry:<name>`` nodes represent scan-entry values.

    Uses forced-on trace for data/control edges.  Data dependencies are
    attributed at trace-step granularity; read-only steps are retained as
    control dependencies for later writes in the same top-level rung.
    """
    del program

    step_reads: dict[tuple[int, int], set[str]] = {}
    step_writes: dict[tuple[int, int], set[str]] = {}

    for acc in on_trace:
        key = (acc.rung_index, acc.step_index)
        if acc.is_read:
            step_reads.setdefault(key, set()).add(acc.name)
        else:
            step_writes.setdefault(key, set()).add(acc.name)

    depends_on: dict[str, set[str]] = {}

    rung_steps: dict[int, set[int]] = {}
    for rung_index, step_index in set(step_reads) | set(step_writes):
        rung_steps.setdefault(rung_index, set()).add(step_index)

    for rung_index, step_indices in rung_steps.items():
        control_reads: set[str] = set()
        for step_index in sorted(step_indices):
            key = (rung_index, step_index)
            reads = step_reads.get(key, set())
            writes = step_writes.get(key, set())
            if not writes:
                control_reads.update(reads)
                continue

            deps_for_step = control_reads | reads
            for w in writes:
                deps = depends_on.setdefault(w, set())
                deps.update(deps_for_step)

    all_written_on: set[str] = set()
    for writes in step_writes.values():
        for w in writes:
            all_written_on.add(w)

    all_names: set[str] = set()
    for acc in on_trace:
        all_names.add(acc.name)

    # Entry edges: tag never written → entry persists
    for name in all_names:
        if name not in all_written_on:
            if name in graph.writers_of:
                continue
            deps = depends_on.setdefault(name, set())
            deps.add(f"entry:{name}")

    # Inert-oneshot entry edges: the forced-on trace sees the write, but on
    # scan 2+ the guard_oneshot_execution decorator skips the instruction
    # entirely, so the tag retains its entry value.
    for name in inert_oneshot_only_tags:
        if name in all_written_on:
            deps = depends_on.setdefault(name, set())
            deps.add(f"entry:{name}")

    return depends_on


def _merge_natural_entry_edges(
    depends_on: dict[str, set[str]],
    natural_trace: list[_Access],
) -> None:
    """Merge step-precise entry-read dependencies from a natural trace.

    Entry reads are path-sensitive observations: a read of ``X`` from scan
    entry influences writes later in the same traced step/control chain, but it
    should not automatically make ``X`` itself depend on ``entry:X``.
    """
    step_reads: dict[tuple[int, int], set[str]] = {}
    step_writes: dict[tuple[int, int], set[str]] = {}

    for acc in natural_trace:
        key = (acc.rung_index, acc.step_index)
        if acc.is_read:
            if acc.from_entry:
                step_reads.setdefault(key, set()).add(f"entry:{acc.name}")
        else:
            step_writes.setdefault(key, set()).add(acc.name)

    rung_steps: dict[int, set[int]] = {}
    for rung_index, step_index in set(step_reads) | set(step_writes):
        rung_steps.setdefault(rung_index, set()).add(step_index)

    for rung_index, step_indices in rung_steps.items():
        control_reads: set[str] = set()
        for step_index in sorted(step_indices):
            key = (rung_index, step_index)
            reads = step_reads.get(key, set())
            writes = step_writes.get(key, set())
            if not writes:
                control_reads.update(reads)
                continue

            deps_for_step = control_reads | reads
            if not deps_for_step:
                continue
            for w in writes:
                deps = depends_on.setdefault(w, set())
                deps.update(deps_for_step)


def _backward_cone(
    depends_on: dict[str, set[str]],
    seeds: set[str],
) -> set[str]:
    """Compute the backward reachability cone from seed nodes."""
    visited: set[str] = set()
    queue = list(seeds)
    while queue:
        node = queue.pop()
        if node in visited:
            continue
        visited.add(node)
        for upstream in depends_on.get(node, set()):
            if upstream not in visited:
                queue.append(upstream)
    return visited


def _collect_entry_dependent_unwritten(
    traces: list[list[_Access]], all_written_on: set[str]
) -> set[str]:
    """Find tags read from entry on a natural trace where they are not written.

    A tag that is conditionally written but never *read from entry* on the
    unwritten paths cannot leak its entry value to any observable output.
    """
    result: set[str] = set()
    for trace in traces:
        written: set[str] = set()
        read_from_entry: set[str] = set()
        for acc in trace:
            if acc.is_read:
                if acc.from_entry:
                    read_from_entry.add(acc.name)
            else:
                written.add(acc.name)
        unwritten = all_written_on - written
        result.update(unwritten & read_from_entry)
    return result


def _edge_condition_tag_names(program: Program) -> frozenset[str]:
    """Collect tags used by explicit rise()/fall() conditions."""
    result: set[str] = set()

    def walk_condition(condition: Any) -> None:
        if isinstance(condition, RisingEdgeCondition | FallingEdgeCondition):
            tag = condition.tag
            wrapped = tag.value if isinstance(tag, ImmediateRef) else tag
            name = getattr(wrapped, "name", None)
            if isinstance(name, str):
                result.add(name)
            return
        if isinstance(condition, AllCondition | AnyCondition):
            for child in condition.conditions:
                walk_condition(child)

    def walk_rung(rung: Any) -> None:
        for condition in rung._conditions:
            walk_condition(condition)
        for branch in rung._branches:
            walk_rung(branch)

    for rung in program.rungs:
        walk_rung(rung)
    for rungs in program.subroutines.values():
        for rung in rungs:
            walk_rung(rung)

    return frozenset(result)


def _warm_prev_memory_states(
    program: Program,
    graph: ProgramGraph,
    nondeterministic_dims: Mapping[str, tuple[Any, ...]],
) -> tuple[dict[str, Any], ...]:
    """Return warm ``_prev:*`` memory assignments for edge-condition traces."""
    states: list[dict[str, Any]] = [{}]
    for name in sorted(_edge_condition_tag_names(program)):
        tag = graph.tags.get(name)
        if name in nondeterministic_dims:
            domain = nondeterministic_dims[name]
        elif tag is not None and tag.type.name == "BOOL":
            domain = (False, True)
        elif tag is not None and tag.choices is not None:
            domain = tuple(sorted(tag.choices.keys()))
        else:
            continue

        default = getattr(tag, "default", False) if tag is not None else False
        for value in domain:
            if value != default:
                states.append({f"_prev:{name}": value})

    return tuple(states)


def _collect_inert_oneshot_only_tags(program: Program, graph: ProgramGraph) -> frozenset[str]:
    """Tags written exclusively by inert-oneshot instructions.

    Instructions decorated with ``guard_oneshot_execution`` (copy, blockcopy,
    blockfill, calc, etc.) are completely skipped once their oneshot flag has
    fired.  OutInstruction is excluded because it always writes (False) even
    after the oneshot fires.  Tags whose *only* writers are inert-oneshot
    instructions retain their scan-entry value on subsequent scans.
    """
    from pyrung.core.analysis.pdg import _extract_write_targets
    from pyrung.core.instruction.coils import OutInstruction

    tag_refs = dict(graph.tags)
    inert_oneshot_written: set[str] = set()
    other_written: set[str] = set()

    def walk_rung(rung: Any) -> None:
        for item in rung._execution_items:
            if isinstance(item, Rung):
                walk_rung(item)
                continue
            is_inert_oneshot = getattr(item, "_oneshot", False) and not isinstance(
                item, OutInstruction
            )
            cls = type(item)
            for field_name in getattr(cls, "_writes", ()):
                target = getattr(item, field_name, None)
                if target is None:
                    continue
                names, _ = _extract_write_targets(target, tag_refs)
                if is_inert_oneshot:
                    inert_oneshot_written.update(names)
                else:
                    other_written.update(names)

    for rung in program.rungs:
        walk_rung(rung)
    for rungs in program.subroutines.values():
        for rung in rungs:
            walk_rung(rung)

    return frozenset(inert_oneshot_written - other_written)


def _collect_inert_oneshot_memory_keys(program: Program) -> frozenset[str]:
    """Collect memory keys for inert-oneshot instructions.

    When these keys are True the instruction is completely skipped on
    subsequent scans.  Natural traces must include the "already fired"
    state to reveal entry-read dependencies masked by the oneshot write.
    """
    from pyrung.core.instruction.coils import OutInstruction

    keys: set[str] = set()

    def walk_rung(rung: Any) -> None:
        for item in rung._execution_items:
            if isinstance(item, Rung):
                walk_rung(item)
                continue
            if getattr(item, "_oneshot", False) and not isinstance(item, OutInstruction):
                keys.add(item.memory_key("_oneshot"))

    for rung in program.rungs:
        walk_rung(rung)
    for rungs in program.subroutines.values():
        for rung in rungs:
            walk_rung(rung)

    return frozenset(keys)


def _collect_out_oneshot_memory_keys(program: Program) -> frozenset[str]:
    """Collect ``entry:_oneshot:*`` node names from ``out(..., oneshot=True)``.

    Unlike inert oneshots (copy/calc/fill) which skip entirely after
    firing, OutInstruction always writes — the memory key selects the
    *value* (True vs False), so it is a cross-scan state carrier that
    the influence graph's entry-edge mechanism does not cover.
    """
    from pyrung.core.instruction.coils import OutInstruction

    keys: set[str] = set()

    def walk_rung(rung: Any) -> None:
        for item in rung._execution_items:
            if isinstance(item, Rung):
                walk_rung(item)
                continue
            if isinstance(item, OutInstruction) and getattr(item, "_oneshot", False):
                keys.add(f"entry:{item.memory_key('_oneshot')}")

    for rung in program.rungs:
        walk_rung(rung)
    for rungs in program.subroutines.values():
        for rung in rungs:
            walk_rung(rung)

    return frozenset(keys)


def _has_latch_or_reset_writer(program: Program, graph: ProgramGraph, tag_name: str) -> bool:
    """Return True when *tag_name* is written by retentive latch/reset coils."""
    tag_refs = dict(graph.tags)

    def walk_rung(rung: Any) -> bool:
        for item in rung._execution_items:
            if isinstance(item, Rung):
                if walk_rung(item):
                    return True
                continue
            if type(item).__name__ not in {"LatchInstruction", "ResetInstruction"}:
                continue
            target = getattr(item, "target", None)
            if tag_name in _extract_tag_names(target, tag_refs):
                return True
        return False

    for rung in program.rungs:
        if walk_rung(rung):
            return True
    for rungs in program.subroutines.values():
        for rung in rungs:
            if walk_rung(rung):
                return True
    return False


def _must_seed_candidate_self(
    program: Program,
    graph: ProgramGraph,
    tag_name: str,
) -> bool:
    """Conservative self-observer cases where the tag's own exit value matters."""
    for rung_idx in graph.writers_of.get(tag_name, frozenset()):
        node = graph.rung_nodes[rung_idx]
        if tag_name in node.condition_reads:
            return True
    return _has_latch_or_reset_writer(program, graph, tag_name)


def _build_merged_influence_graph(
    program: Program,
    graph: ProgramGraph,
    nondeterministic_dims: Mapping[str, tuple[Any, ...]],
    stateful_dims: Mapping[str, tuple[Any, ...]] | None = None,
) -> dict[str, set[str]]:
    """Build influence graph from traced scans across ND input combos.

    Uses forced-on trace for data/control edges (sees all code paths)
    and natural-execution sweeps for step-precise entry-read edges.
    """
    merged, _conditionally_written = _build_merged_influence_graph_with_conditionals(
        program,
        graph,
        nondeterministic_dims,
        stateful_dims or {},
    )
    return merged


def _single_flip_combos(
    nondeterministic_dims: Mapping[str, tuple[Any, ...]],
) -> list[dict[str, Any]]:
    """Default + one flip per ND input — same strategy as BFS edge inputs."""
    nd_names = sorted(nondeterministic_dims)
    defaults = {n: nondeterministic_dims[n][0] for n in nd_names}
    combos: list[dict[str, Any]] = [dict(defaults)]
    for name in nd_names:
        for value in nondeterministic_dims[name][1:]:
            flipped = dict(defaults)
            flipped[name] = value
            combos.append(flipped)
    return combos


def _build_merged_influence_graph_with_conditionals(
    program: Program,
    graph: ProgramGraph,
    nondeterministic_dims: Mapping[str, tuple[Any, ...]],
    stateful_dims: Mapping[str, tuple[Any, ...]],
    progress_tick: Callable[[], None] | None = None,
) -> tuple[dict[str, set[str]], set[str]]:
    """Build the merged influence graph plus tags absent on some natural path."""
    on_trace = _traced_scan(program, SystemState(), mode="forced_on")
    if progress_tick is not None:
        progress_tick()
    all_written_on: set[str] = set()
    for acc in on_trace:
        if not acc.is_read:
            all_written_on.add(acc.name)
    inert_oneshot_only = _collect_inert_oneshot_only_tags(program, graph)
    merged = _build_influence_graph(
        program,
        graph,
        on_trace,
        inert_oneshot_only_tags=inert_oneshot_only,
    )

    memory_states = _warm_prev_memory_states(program, graph, nondeterministic_dims)
    inert_oneshot_keys = _collect_inert_oneshot_memory_keys(program)
    if inert_oneshot_keys:
        fired = {key: True for key in inert_oneshot_keys}
        memory_states = memory_states + tuple({**ms, **fired} for ms in memory_states)
    tag_states = _warm_entry_tag_states(program, graph, stateful_dims)
    nd_combos = _single_flip_combos(nondeterministic_dims)

    natural_traces: list[list[_Access]] = []
    for nd_values in nd_combos:
        state = SystemState().with_tags(nd_values) if nd_values else SystemState()
        for tag_state in tag_states:
            natural_state = state.with_tags(tag_state) if tag_state else state
            for memory_state in memory_states:
                natural_trace = _traced_scan_natural(
                    program, natural_state.with_memory(memory_state)
                )
                _merge_natural_entry_edges(merged, natural_trace)
                natural_traces.append(natural_trace)
        if progress_tick is not None:
            progress_tick()

    conditionally_written = _collect_entry_dependent_unwritten(natural_traces, all_written_on)
    return merged, conditionally_written


def _warm_entry_tag_states(
    program: Program,
    graph: ProgramGraph,
    stateful_dims: Mapping[str, tuple[Any, ...]],
) -> tuple[dict[str, Any], ...]:
    """Return small stateful-entry variants for natural entry-read traces."""
    condition_reads: set[str] = set()
    for rung in program.rungs:
        condition_reads.update(_static_condition_reads_for_rung(rung, graph))
    for rungs in program.subroutines.values():
        for rung in rungs:
            condition_reads.update(_static_condition_reads_for_rung(rung, graph))

    states: list[dict[str, Any]] = [{}]
    for name in sorted(condition_reads & set(stateful_dims)):
        tag = graph.tags.get(name)
        default = getattr(tag, "default", None)
        for value in stateful_dims[name]:
            if value != default:
                states.append({name: value})
                break
    return tuple(states)


def _static_condition_reads_for_rung(rung: Any, graph: ProgramGraph) -> set[str]:
    names: set[str] = set()
    _collect_condition_reads(rung, names, dict(graph.tags))
    return names


_CONSTANT_EXIT_COMBO_CAP = 1024
_CONSTANT_EXIT_SENTINEL = object()


def _find_constant_exit_tags(
    program: Program,
    graph: ProgramGraph,
    remaining_stateful: dict[str, tuple[Any, ...]],
    nondeterministic_dims: Mapping[str, tuple[Any, ...]],
    depends_on: dict[str, set[str]],
    inert_oneshot_only: frozenset[str],
    all_stateful_dims: Mapping[str, tuple[Any, ...]],
    all_stateful_names: frozenset[str],
    observer_seeds: frozenset[str],
) -> frozenset[str]:
    """Find stateful tags whose exit value is constant across all entry/input combos.

    A tag whose exit value is always the same constant regardless of its own
    entry value and all ND inputs can be removed from the BFS state key — its
    value never varies across scans.

    Even when the exit is constant, elision is only safe if either the constant
    equals the tag's default (so the entry never varies) or no other tag depends
    on ``entry:<candidate>`` (so the one-scan transition from default to the
    constant is invisible to the rest of the program).
    """
    if not remaining_stateful:
        return frozenset()

    memory_states = _warm_prev_memory_states(program, graph, nondeterministic_dims)
    constant_exit: set[str] = set()

    for candidate in sorted(remaining_stateful):
        if candidate in inert_oneshot_only:
            continue

        cone = _backward_cone(depends_on, {candidate})

        sweep: dict[str, tuple[Any, ...]] = {}
        oneshot_keys: list[str] = []
        for node in cone:
            if node.startswith("entry:"):
                name = node[6:]
                if name in all_stateful_dims:
                    sweep[name] = all_stateful_dims[name]
                elif name in remaining_stateful:
                    sweep[name] = remaining_stateful[name]
                elif name.startswith("_oneshot:"):
                    oneshot_keys.append(name)
            elif node in nondeterministic_dims:
                sweep[node] = nondeterministic_dims[node]

        if candidate not in sweep:
            sweep[candidate] = remaining_stateful[candidate]

        for swept_name in list(sweep):
            swept_tag = graph.tags.get(swept_name)
            swept_default = getattr(swept_tag, "default", None)
            if swept_default is not None and swept_default not in sweep[swept_name]:
                sweep[swept_name] = sweep[swept_name] + (swept_default,)

        candidate_memory_states = memory_states
        if oneshot_keys:
            expanded: list[dict[str, Any]] = []
            for base_mem in memory_states:
                for os_key in oneshot_keys:
                    for os_val in (False, True):
                        expanded.append({**base_mem, os_key: os_val})
            candidate_memory_states = tuple(expanded)

        total = len(candidate_memory_states)
        for domain in sweep.values():
            total *= len(domain)
            if total > _CONSTANT_EXIT_COMBO_CAP:
                break
        if total > _CONSTANT_EXIT_COMBO_CAP:
            continue

        names = sorted(sweep)
        domains = [sweep[n] for n in names]

        exit_value: Any = _CONSTANT_EXIT_SENTINEL
        found_varying = False
        for combo in itertools.product(*domains):
            if found_varying:
                break
            values = dict(zip(names, combo, strict=True))
            base_state = SystemState().with_tags(values)
            for mem_state in candidate_memory_states:
                state = base_state.with_memory(mem_state) if mem_state else base_state
                ctx = ScanContext(state)
                ctx.set_memory("_dt", 0.010)
                execute_program(program, ctx, mode="natural")
                val = ctx.get_tag(candidate)
                if exit_value is _CONSTANT_EXIT_SENTINEL:
                    exit_value = val
                elif val != exit_value:
                    found_varying = True
                    break

        if not found_varying and exit_value is not _CONSTANT_EXIT_SENTINEL:
            candidate_tag = graph.tags.get(candidate)
            candidate_default = getattr(candidate_tag, "default", None)
            if exit_value != candidate_default:
                dep_seeds = (set(all_stateful_names) - {candidate} - constant_exit) | set(
                    observer_seeds
                )
                if dep_seeds:
                    dep_cone = _backward_cone(depends_on, dep_seeds)
                    if f"entry:{candidate}" in dep_cone:
                        continue
            constant_exit.add(candidate)

    return frozenset(constant_exit)


_ExitSubstitution = tuple[str, Callable[[Any], Any]]


def _compute_exit_substitutions(
    program: Program,
    graph: ProgramGraph,
    candidates: set[str],
    surviving_names: frozenset[str],
) -> dict[str, _ExitSubstitution]:
    """Compute exit-expression substitutions for elidable observer tags.

    For each candidate, finds the single unconditional write instruction and
    extracts (source_tag_name, invert_fn).  Only succeeds for identity copies
    and invertible linear calcs where the source is a surviving dimension.
    """
    from pyrung.core.analysis.prove.classify import (
        _calc_reverse_edge,
        _tag_name_from_value,
    )
    from pyrung.core.instruction.calc import CalcInstruction
    from pyrung.core.instruction.data_transfer import CopyInstruction

    if not candidates:
        return {}

    unconditional_rung_indices: set[int] = set()
    for ni, node in enumerate(graph.rung_nodes):
        if not node.condition_reads and node.subroutine is None:
            unconditional_rung_indices.add(ni)

    candidate_writers: dict[str, list[tuple[str, Callable[[Any], Any]]]] = {
        name: [] for name in candidates
    }

    for ni in unconditional_rung_indices:
        rung = program.rungs[ni] if ni < len(program.rungs) else None
        if rung is None:
            continue
        for item in rung._execution_items:
            if isinstance(item, CopyInstruction):
                target_name = _tag_name_from_value(item.dest)
                if target_name not in candidates:
                    continue
                source_name = _tag_name_from_value(item.source)
                if source_name is None:
                    continue
                if source_name in surviving_names:
                    candidate_writers[target_name].append((source_name, lambda v: v))
            elif isinstance(item, CalcInstruction):
                target_name = _tag_name_from_value(item.dest)
                if target_name not in candidates:
                    continue
                edge = _calc_reverse_edge(item.expression)
                if edge is None:
                    continue
                source_name, invert = edge
                if source_name in surviving_names:
                    candidate_writers[target_name].append((source_name, invert))

    result: dict[str, _ExitSubstitution] = {}
    for name, writers in candidate_writers.items():
        if len(writers) == 1:
            result[name] = writers[0]
    return result


def find_elidable_traced(
    program: Program,
    graph: ProgramGraph,
    stateful_dims: Mapping[str, tuple[Any, ...]],
    nondeterministic_dims: Mapping[str, tuple[Any, ...]],
    *,
    observer_exprs: tuple[Expr, ...] = (),
    observer_tag_names: frozenset[str] = frozenset(),
    progress_tick: Callable[[], None] | None = None,
) -> tuple[dict[str, str], dict[str, _ExitSubstitution]]:
    """Determine which stateful tags are elidable via traced influence analysis.

    Returns ``(elidable_dict, substitutions)`` where *elidable_dict* maps
    elidable tag names to the sub-pass that justified elision and
    *substitutions* maps elided observer-referenced tags to their
    ``(source_tag, invert_fn)`` pair for property expression rewriting.

    Seeds the backward cone from written tag values and observer expressions.
    Stateful tag nodes are seeds too, because retained state can itself be an
    observer on the next scan.  We intentionally seed only the tag node, never
    its ``entry:<name>`` node; a stateful tag is elidable when its entry node is
    not reachable from the seeded output values.
    """
    depends_on, conditionally_written = _build_merged_influence_graph_with_conditionals(
        program,
        graph,
        nondeterministic_dims,
        stateful_dims,
        progress_tick=progress_tick,
    )

    out_oneshot_entries = _collect_out_oneshot_memory_keys(program)

    stateful_names = {name for name, domain in stateful_dims.items() if PENDING not in domain}
    observer_seeds: set[str] = set(observer_tag_names)
    for expr in observer_exprs:
        for name in _referenced_tags(expr):
            observer_seeds.add(name)

    elidable: dict[str, str] = {}
    for candidate in sorted(stateful_names):
        candidate_cone = _backward_cone(depends_on, {candidate})
        if not candidate_cone.isdisjoint(out_oneshot_entries):
            continue
        seeds = (stateful_names - {candidate}) | observer_seeds
        if _must_seed_candidate_self(program, graph, candidate):
            seeds.add(candidate)
        cone = _backward_cone(depends_on, seeds)
        if candidate in conditionally_written and candidate in cone:
            continue
        if f"entry:{candidate}" not in cone:
            elidable[candidate] = "influence_cone"

    # For observer-referenced tags marked elidable, attempt exit-expression
    # substitution.  Tags that can't be substituted are guarded (kept stateful).
    observer_elidable = {n for n in elidable if n in observer_seeds}
    substitutions: dict[str, _ExitSubstitution] = {}
    if observer_elidable:
        surviving = frozenset(nondeterministic_dims) | (
            frozenset(stateful_names) - frozenset(elidable)
        )
        substitutions = _compute_exit_substitutions(program, graph, observer_elidable, surviving)
        for name in observer_elidable - set(substitutions):
            del elidable[name]

    remaining = {n: stateful_dims[n] for n in stateful_names if n not in elidable}
    inert_oneshot_only = _collect_inert_oneshot_only_tags(program, graph)
    constant_exit = _find_constant_exit_tags(
        program,
        graph,
        remaining,
        nondeterministic_dims,
        depends_on,
        inert_oneshot_only,
        all_stateful_dims=stateful_dims,
        all_stateful_names=frozenset(stateful_names),
        observer_seeds=frozenset(observer_seeds),
    )
    for name in constant_exit:
        elidable[name] = "constant_exit"

    return elidable, substitutions


# ---------------------------------------------------------------------------
# Pipeline-compatible entry point
# ---------------------------------------------------------------------------


def _elide_traced(
    program: Program,
    graph: ProgramGraph,
    stateful_dims: Mapping[str, tuple[Any, ...]],
    nondeterministic_dims: Mapping[str, tuple[Any, ...]],
    *,
    observer_exprs: tuple[Expr, ...] = (),
    observer_tag_names: frozenset[str] = frozenset(),
    progress: Callable[[str], None] | None = None,
    progress_prefix: Callable[[], str] | None = None,
) -> tuple[
    dict[str, tuple[Any, ...]],
    dict[str, str],
    dict[str, tuple[tuple[str, str], ...]],
    dict[str, _ExitSubstitution],
]:
    """Traced influence graph elision with pipeline-compatible return signature.

    Returns ``(reduced_stateful_dims, elided_dict, proof_details, substitutions)``
    matching the contract of ``_elide_scan_local_stateful_dims``.
    """
    import sys

    if not stateful_dims:
        return {}, {}, {}, {}

    use_dots = progress_prefix is not None
    if use_dots:
        assert progress_prefix is not None
        header = f"{progress_prefix()}elision | traced {len(stateful_dims)} tags "
        print(header, end="", file=sys.stderr, flush=True)

    def _tick() -> None:
        print(".", end="", file=sys.stderr, flush=True)

    elidable, substitutions = find_elidable_traced(
        program,
        graph,
        stateful_dims,
        nondeterministic_dims,
        observer_exprs=observer_exprs,
        observer_tag_names=observer_tag_names,
        progress_tick=_tick if use_dots else None,
    )

    reduced: dict[str, tuple[Any, ...]] = {}
    elided: dict[str, str] = {}
    proof_details: dict[str, tuple[tuple[str, str], ...]] = {}

    if use_dots:
        print(" ", end="", file=sys.stderr, flush=True)
    for name, domain in stateful_dims.items():
        if name in elidable:
            elided[name] = "traced"
            proof_details[name] = (("traced_path", elidable[name]),)
            if use_dots:
                print("x", end="", file=sys.stderr, flush=True)
        else:
            reduced[name] = domain
            if use_dots:
                print(".", end="", file=sys.stderr, flush=True)

    removed = len(stateful_dims) - len(reduced)
    if use_dots:
        print(f"  removed={removed}", file=sys.stderr)
    elif progress is not None:
        progress(
            f"elision | traced phase complete | removed={removed:,} | retained={len(reduced):,}"
        )

    return reduced, elided, proof_details, substitutions
