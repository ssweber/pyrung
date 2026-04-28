"""Exhaustive state-space verification for pyrung programs.

BFS over the reachable state space using the compiled replay kernel
as the execution oracle and the expression tree for search-space
reduction (dimension classification, value domain extraction,
don't-care pruning).
"""

from __future__ import annotations

import hashlib
import itertools
import json
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.simplified import And, Atom, Const, Expr, _condition_to_expr
from pyrung.core.kernel import BlockSpec, CompiledKernel, ReplayKernel

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.program import Program

from .expr import _eval_atom as _eval_atom
from .expr import _live_inputs, _referenced_tags
from .expr import _partial_eval as _partial_eval


@dataclass(frozen=True)
class Proven:
    """Invariant holds across all reachable states."""

    states_explored: int


@dataclass(frozen=True)
class Counterexample:
    """Invariant violated — trace reproduces the failure."""

    trace: list[TraceStep]


@dataclass(frozen=True)
class TraceStep:
    inputs: dict[str, Any]
    scans: int = 1


@dataclass(frozen=True)
class Intractable:
    """Verification cannot complete within resource bounds."""

    reason: str
    dimensions: int
    estimated_space: int
    tags: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class StateDiff:
    """Difference between two reachable state sets."""

    added: frozenset[frozenset[tuple[str, Any]]]
    removed: frozenset[frozenset[tuple[str, Any]]]


PENDING = "Pending"


@dataclass(frozen=True)
class _ExploreContext:
    compiled: CompiledKernel
    graph: ProgramGraph
    all_exprs: list[Expr]
    stateful_dims: dict[str, tuple[Any, ...]]
    nondeterministic_dims: dict[str, tuple[Any, ...]]
    stateful_names: tuple[str, ...]
    edge_tag_names: tuple[str, ...]
    memory_key_names: tuple[str, ...]
    state_key_done_specs: tuple[_StateKeyDoneSpec, ...]
    done_event_specs: tuple[_DoneEventSpec, ...]
    threshold_vector_specs: tuple[_ThresholdVectorSpec, ...]
    threshold_event_specs: tuple[_ThresholdEventSpec, ...]
    block_specs: tuple[BlockSpec, ...]
    dt: float
    edge_tag_exprs: dict[str, list[Expr]] = field(default_factory=dict)
    synthetic_preset_tags: tuple[str, ...] = ()


from .absorb import _ThresholdVectorSpec
from .classify import (
    _build_dimension_hints,
)
from .classify import (
    _classify_dimensions as _classify_dimensions,
)
from .classify import (
    _has_data_feedback as _has_data_feedback,
)
from .classify import (
    _pilot_sweep_domains as _pilot_sweep_domains,
)
from .events import (
    _DoneEventSpec,
    _has_pending_hidden_event,
    _maybe_jump_hidden_event,
    _settle_pending,
    _StateKeyDoneSpec,
    _ThresholdEventSpec,
)
from .kernel import (
    _EdgeCompressor,
    _extract_state_key,
    _KernelSnapshot,
    _restore_kernel,
    _seed_synthetic_presets,
    _snapshot_kernel,
    _step_kernel,
)
from .passes import (
    _DEFAULT_BFS_CONFIG,
    _BFSConfig,
    _PassContext,
    _run_pre_bfs_pipeline,
)


def _build_explore_context(
    program: Program,
    *,
    scope: list[str] | None = None,
    project: tuple[str, ...] | None = None,
    extra_exprs: list[Expr] | None = None,
    dt: float = 0.010,
    compiled: CompiledKernel | None = None,
) -> _ExploreContext | Intractable:
    """Build shared verifier context once for prove()/reachable_states()."""
    ctx = _PassContext(
        program=program,
        scope=scope,
        project=project,
        extra_exprs=extra_exprs,
        dt=dt,
        compiled=compiled,
    )
    return _run_pre_bfs_pipeline(ctx)


def _projected_tuple(kernel: ReplayKernel, project_names: tuple[str, ...]) -> tuple[Any, ...]:
    """Project kernel state onto a fixed ordered list of tag names."""
    return tuple(kernel.tags.get(name) for name in project_names)


def _projected_states(
    project_names: tuple[str, ...],
    projected_rows: set[tuple[Any, ...]],
) -> frozenset[frozenset[tuple[str, Any]]]:
    """Convert ordered projection rows to the public frozenset shape."""
    return frozenset(frozenset(zip(project_names, row, strict=True)) for row in projected_rows)


def _build_trace(
    parent_map: dict[tuple[Any, ...], tuple[tuple[Any, ...] | None, dict[str, Any], int]],
    key: tuple[Any, ...],
) -> list[TraceStep]:
    """Reconstruct the input trace from initial state to failure."""
    trace: list[TraceStep] = []
    current = key
    while current in parent_map:
        parent_key, inputs, scans = parent_map[current]
        trace.append(TraceStep(inputs=inputs, scans=scans))
        if parent_key is None:
            break
        current = parent_key
    trace.reverse()
    return trace


def _compile_expr_evaluator(expr: Expr) -> Callable[[dict[str, Any]], bool | None]:
    """Compile an Expr into a tri-state evaluator.

    Returns ``True``/``False`` when the expression is decidable from the
    concrete state dict, otherwise ``None`` for residual edge-sensitive terms
    like ``rise()``/``fall()``.
    """
    if isinstance(expr, Const):
        value = bool(expr.value)
        return lambda _state: value

    if isinstance(expr, Atom):
        tag = expr.tag
        form = expr.form
        operand = expr.operand

        def _eval_atom_from_state(state: dict[str, Any]) -> bool | None:
            if form in {"rise", "fall"}:
                return None
            if tag not in state:
                return None

            value = state[tag]
            resolved_operand = (
                state[operand] if isinstance(operand, str) and operand in state else operand
            )

            if form == "xic":
                return bool(value)
            if form == "xio":
                return not bool(value)
            if form == "truthy":
                return bool(value)
            if form == "eq":
                return value == resolved_operand
            if form == "ne":
                return value != resolved_operand
            if form == "lt":
                return value < resolved_operand
            if form == "le":
                return value <= resolved_operand
            if form == "gt":
                return value > resolved_operand
            if form == "ge":
                return value >= resolved_operand
            return None

        return _eval_atom_from_state

    if isinstance(expr, And):
        term_fns = tuple(_compile_expr_evaluator(term) for term in expr.terms)

        def _eval_and(state: dict[str, Any]) -> bool | None:
            saw_unknown = False
            for fn in term_fns:
                result = fn(state)
                if result is False:
                    return False
                if result is None:
                    saw_unknown = True
            return None if saw_unknown else True

        return _eval_and

    term_fns = tuple(_compile_expr_evaluator(term) for term in expr.terms)

    def _eval_or(state: dict[str, Any]) -> bool | None:
        saw_unknown = False
        for fn in term_fns:
            result = fn(state)
            if result is True:
                return True
            if result is None:
                saw_unknown = True
        return None if saw_unknown else False

    return _eval_or


def _compile_property_spec(
    spec: Any,
) -> tuple[Callable[[dict[str, Any]], bool], list[str] | None, Expr | None]:
    """Compile one property spec into a predicate and optional auto-scope.

    ``spec`` may be a single condition/callable or a tuple of conditions with
    implicit AND semantics.
    """
    if isinstance(spec, tuple):
        return _compile_property(*spec)
    return _compile_property(spec)


def _normalize_property_specs(*conditions: Any) -> tuple[bool, list[Any]]:
    """Split prove() inputs into single-property or batch-property form.

    A sole list argument means "batch prove these properties". Tuple items
    inside that list represent grouped AND terms for one property.
    """
    if len(conditions) == 1 and isinstance(conditions[0], list):
        property_specs = list(conditions[0])
        if not property_specs:
            raise ValueError("prove() property list cannot be empty")
        return True, property_specs

    if not conditions:
        raise ValueError("prove() requires at least one condition")
    if len(conditions) == 1:
        return False, [conditions[0]]
    return False, [tuple(conditions)]


def _bfs_explore(
    context: _ExploreContext,
    *,
    predicates: list[Callable[[dict[str, Any]], bool]] | None = None,
    project: tuple[str, ...] | None = None,
    max_depth: int = 50,
    max_states: int = 100_000,
    bfs_config: _BFSConfig = _DEFAULT_BFS_CONFIG,
) -> (
    list[Proven | Counterexample | Intractable]
    | frozenset[frozenset[tuple[str, Any]]]
    | Intractable
):
    """BFS over the reachable state space."""
    kernel = context.compiled.create_kernel()
    _seed_synthetic_presets(context, kernel)
    edge_comp = _EdgeCompressor(context)

    def _state_key(k: ReplayKernel) -> tuple[Any, ...]:
        if bfs_config.edge_compression:
            return edge_comp.state_key(k)
        return _extract_state_key(
            k,
            context.stateful_names,
            context.edge_tag_names,
            context.memory_key_names,
            context.state_key_done_specs,
            context.threshold_vector_specs,
        )

    initial_key = _state_key(kernel)

    visited: set[tuple[Any, ...]] = {initial_key}
    parent_map: dict[tuple[Any, ...], tuple[tuple[Any, ...] | None, dict[str, Any], int]] | None = (
        {initial_key: (None, {}, 0)} if predicates is not None else None
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
            trace = _build_trace(parent_map, p_key)
            trace.append(TraceStep(inputs=input_dict, scans=edge_scans))
            results[i] = Counterexample(trace=trace)

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

    while queue:
        snap, depth, parent_key = queue.popleft()
        if depth >= max_depth:
            continue

        _restore_kernel(kernel, snap)
        live = (
            _live_inputs(kernel.tags, context.nondeterministic_dims, context.all_exprs)
            if bfs_config.live_input_pruning
            else frozenset(context.nondeterministic_dims)
        )
        if live:
            live_sorted = sorted(live)
            domains = [context.nondeterministic_dims[n] for n in live_sorted]
            combos: Any = itertools.product(*domains)
        else:
            live_sorted = []
            combos = [()]

        seen_outcomes: set[tuple[tuple[Any, ...], tuple[Any, ...]]] | None = (
            set() if project is not None else None
        )
        for combo in combos:
            _restore_kernel(kernel, snap)
            input_dict: dict[str, Any] = {}
            for i, name in enumerate(live_sorted):
                kernel.tags[name] = combo[i]
                input_dict[name] = combo[i]

            _step_kernel(context, kernel)
            edge_scans = 1

            if predicates is not None:
                assert results is not None
                any_unsettled = any(
                    results[i] is None and not predicates[i](kernel.tags)
                    for i in range(len(predicates))
                )
                new_key = _state_key(kernel)
                if (
                    bfs_config.pending_settlement
                    and any_unsettled
                    and _has_pending_hidden_event(context, new_key)
                ):
                    new_key, additional_scans = _settle_pending(
                        context,
                        kernel,
                        snap,
                        edge_comp,
                    )
                    edge_scans += additional_scans
                    new_key = _state_key(kernel)
                _record_failures(
                    state=kernel.tags,
                    p_key=parent_key,
                    input_dict=input_dict,
                    edge_scans=edge_scans,
                )
            else:
                new_key = _state_key(kernel)

            before_jump_key = new_key
            if bfs_config.hidden_event_jumping:
                _, additional_scans = _maybe_jump_hidden_event(
                    context,
                    kernel,
                    snap,
                    visited,
                    new_key,
                    edge_comp,
                )
                new_key = _state_key(kernel)
            else:
                additional_scans = 0
            jumped = new_key != before_jump_key or additional_scans > 0
            if additional_scans:
                edge_scans += additional_scans
            if jumped and predicates is not None:
                _record_failures(
                    state=kernel.tags,
                    p_key=parent_key,
                    input_dict=input_dict,
                    edge_scans=edge_scans,
                )

            if project is not None:
                projected_row = _projected_tuple(kernel, project)
                outcome = (new_key, projected_row)
                assert seen_outcomes is not None
                if outcome in seen_outcomes:
                    continue
                seen_outcomes.add(outcome)
                projected_rows.add(projected_row)

            if new_key not in visited:
                visited.add(new_key)
                if len(visited) > max_states:
                    intractable = Intractable(
                        reason="max_states exceeded",
                        dimensions=len(context.stateful_dims) + len(context.nondeterministic_dims),
                        estimated_space=len(visited),
                        hints=_build_dimension_hints(context),
                    )
                    if results is not None:
                        return [r if r is not None else intractable for r in results]
                    return intractable
                if parent_map is not None:
                    parent_map[new_key] = (parent_key, input_dict, edge_scans)
                queue.append((_snapshot_kernel(kernel), depth + 1, new_key))

            if results is not None and all(r is not None for r in results):
                return [r for r in results if r is not None]

    if project is not None:
        return _projected_states(project, projected_rows)

    if results is not None:
        return [r if r is not None else Proven(states_explored=len(visited)) for r in results]

    return [Proven(states_explored=len(visited))]


def _compile_property(
    *conditions: Any,
) -> tuple[Callable[[dict[str, Any]], bool], list[str] | None, Expr | None]:
    """Normalize a condition expression or callable into a dict predicate.

    Returns ``(predicate_fn, auto_scope, expr_or_none)`` where *auto_scope* is
    a list of referenced tag names (for automatic upstream-cone restriction)
    or ``None`` when the caller passed an opaque callable.
    """
    if len(conditions) == 1 and callable(conditions[0]) and not _is_condition_like(conditions[0]):
        user_predicate = conditions[0]

        def _predicate(state: dict[str, Any]) -> bool:
            return bool(user_predicate(dict(state)))

        return _predicate, None, None

    from pyrung.core.condition import _as_condition, _normalize_and_condition

    normalized = _normalize_and_condition(
        *conditions,
        coerce=_as_condition,
        empty_error="prove() requires at least one condition",
        group_empty_error="prove() condition group cannot be empty",
    )
    expr = _condition_to_expr(normalized)
    tags_in_expr = sorted(_referenced_tags(expr))
    evaluator = _compile_expr_evaluator(expr)

    def _predicate(state: dict[str, Any]) -> bool:
        return evaluator(state) is not False

    return _predicate, tags_in_expr, expr


def _is_condition_like(obj: Any) -> bool:
    """True if *obj* is a Tag or Condition (not a plain callable)."""
    from pyrung.core.condition import Condition
    from pyrung.core.tag import Tag

    return isinstance(obj, (Tag, Condition))


def _upstream_cone(program: Program, tags: list[str]) -> frozenset[str]:
    """Compute the full upstream dependency cone for a set of tags."""
    dv = program.dataview()
    cone: set[str] = set()
    for tag_name in tags:
        cone.update(dv.upstream(tag_name).tags)
    cone.update(tags)
    return frozenset(cone)


def _partition_batch(
    program: Program,
    compiled_properties: list[
        tuple[Callable[[dict[str, Any]], bool], list[str] | None, Expr | None]
    ],
) -> list[tuple[list[int], list[str] | None]]:
    """Group batch properties into independent partitions by upstream cone overlap.

    Returns a list of ``(original_indices, merged_scope)`` pairs.
    Properties with ``auto_scope=None`` (lambdas) get full scope.
    """
    n = len(compiled_properties)
    if n <= 1:
        scope = compiled_properties[0][1] if n == 1 else None
        return [(list(range(n)), scope)]

    cones: list[frozenset[str] | None] = []
    for _predicate, auto_scope, _expr in compiled_properties:
        if auto_scope is None:
            cones.append(None)
        else:
            cones.append(_upstream_cone(program, auto_scope))

    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    null_indices = [i for i, c in enumerate(cones) if c is None]
    if null_indices:
        for i in null_indices[1:]:
            union(null_indices[0], i)

    for i in range(n):
        cone_i = cones[i]
        if cone_i is None:
            continue
        for j in range(i + 1, n):
            cone_j = cones[j]
            if cone_j is None:
                union(i, j)
            elif cone_i & cone_j:
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    result: list[tuple[list[int], list[str] | None]] = []
    for indices in groups.values():
        group_scopes = [compiled_properties[i][1] for i in indices]
        if any(s is None for s in group_scopes):
            result.append((indices, None))
        else:
            merged: set[str] = set()
            for s in group_scopes:
                assert s is not None
                merged.update(s)
            result.append((indices, sorted(merged)))
    return result


def prove(
    program: Program,
    *conditions: Any,
    scope: list[str] | None = None,
    max_depth: int = 50,
    max_states: int = 100_000,
) -> Proven | Counterexample | Intractable | list[Proven | Counterexample | Intractable]:
    """Exhaustively prove a property over all reachable states.

    Accepts the same condition syntax as ``Rung()`` and ``when()``::

        prove(logic, Or(~Running, EstopOK))
        prove(logic, ~Running, EstopOK)        # implicit AND
        prove(logic, (Ready, AutoMode))        # grouped AND as one property
        prove(logic, [prop_a, prop_b, prop_c]) # batch prove in one pass
        prove(logic, lambda s: s["Running"] <= s["Limit"])

    When given condition expressions, the upstream cone is derived
    automatically — no ``scope=`` needed.

    Parameters
    ----------
    program : Program
        The compiled ladder logic program.
    *conditions : Tag, Condition, callable, tuple, or list
        One property, or a sole list of properties for batch proving.
        Tuple terms represent grouped AND conditions for one property.
        Tag/Condition expressions are preferred; a callable
        ``(state_dict) -> bool`` is accepted as a fallback.
    scope : list of tag names, optional
        Override automatic scope derivation.
    max_depth : int
        BFS depth limit (scan cycles).
    max_states : int
        Visited-set cap — bail with ``Intractable`` if exceeded.
    """
    from pyrung.circuitpy.codegen import compile_kernel

    is_batch, property_specs = _normalize_property_specs(*conditions)
    compiled_properties = [_compile_property_spec(spec) for spec in property_specs]

    if not is_batch:
        predicate, auto_scope, expr = compiled_properties[0]
        effective_scope = scope if scope is not None else auto_scope
        extra = [expr] if expr is not None else []
        context = _build_explore_context(program, scope=effective_scope, extra_exprs=extra)
        if isinstance(context, Intractable):
            return context
        return _bfs_explore(
            context,
            predicates=[predicate],
            max_depth=max_depth,
            max_states=max_states,
        )[0]

    if scope is not None:
        partitions = [(list(range(len(compiled_properties))), scope)]
    else:
        partitions = _partition_batch(program, compiled_properties)

    compiled_kernel = compile_kernel(program)
    results: list[Proven | Counterexample | Intractable | None] = [None] * len(compiled_properties)
    for indices, group_scope in partitions:
        group_exprs: list[Expr] = [
            e for i in indices if (e := compiled_properties[i][2]) is not None
        ]
        context = _build_explore_context(
            program,
            scope=group_scope,
            extra_exprs=group_exprs,
            compiled=compiled_kernel,
        )
        if isinstance(context, Intractable):
            for i in indices:
                results[i] = context
            continue

        group_predicates = [compiled_properties[i][0] for i in indices]
        group_results = _bfs_explore(
            context,
            predicates=group_predicates,
            max_depth=max_depth,
            max_states=max_states,
        )
        for i, r in zip(indices, group_results, strict=True):  # ty: ignore[invalid-argument-type]
            results[i] = r

    return [r if r is not None else Proven(states_explored=0) for r in results]


def reachable_states(
    program: Program,
    scope: list[str] | None = None,
    project: list[str] | None = None,
    max_depth: int = 50,
    max_states: int = 100_000,
) -> frozenset[frozenset[tuple[str, Any]]] | Intractable:
    """Compute the full reachable state space.

    Parameters
    ----------
    program : Program
        The compiled ladder logic program.
    scope : list of tag names, optional
        If given, restrict input enumeration to the upstream cone.
    project : list of tag names, optional
        Tags to project onto. Defaults to terminal tags.
    max_depth : int
        BFS depth limit (scan cycles).
    max_states : int
        Visited-set cap.
    """
    project_names = tuple(project) if project is not None else tuple(_default_projection(program))
    context = _build_explore_context(program, scope=scope, project=project_names)
    if isinstance(context, Intractable):
        return context

    return _bfs_explore(  # ty: ignore[invalid-return-type]
        context,
        project=project_names,
        max_depth=max_depth,
        max_states=max_states,
    )


def diff_states(
    before: frozenset[frozenset[tuple[str, Any]]],
    after: frozenset[frozenset[tuple[str, Any]]],
) -> StateDiff:
    """Compare two reachable state sets."""
    return StateDiff(added=after - before, removed=before - after)


def _default_projection(program: Program) -> list[str]:
    """Choose default projection tags: terminal outputs only."""
    dv = program.dataview()
    return sorted(dv.terminals().tags)


def _states_to_json(
    states: frozenset[frozenset[tuple[str, Any]]],
) -> list[dict[str, Any]]:
    """Convert state frozensets to sorted list of dicts."""
    rows = [dict(sorted(s)) for s in states]
    rows.sort(key=lambda d: tuple(sorted(d.items())))
    return rows


def _json_to_states(
    rows: list[dict[str, Any]],
) -> frozenset[frozenset[tuple[str, Any]]]:
    """Convert list of dicts back to state frozensets."""
    return frozenset(frozenset(d.items()) for d in rows)


def write_lock(
    path: Path,
    states: frozenset[frozenset[tuple[str, Any]]],
    projection: list[str],
    program_hash: str,
    unreachable_examples: list[dict[str, Any]] | None = None,
) -> None:
    """Write a state-space lock file."""
    data = {
        "version": 1,
        "program_hash": program_hash,
        "projection": sorted(projection),
        "reachable": _states_to_json(states),
        "unreachable_examples": unreachable_examples or [],
    }
    path.write_text(json.dumps(data, indent=2, default=_json_default) + "\n")


def _json_default(obj: Any) -> Any:
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, (int, float, str)):
        return obj
    msg = f"Object of type {type(obj).__name__} is not JSON serializable"
    raise TypeError(msg)


def read_lock(path: Path) -> dict[str, Any]:
    """Read a state-space lock file."""
    return json.loads(path.read_text())


def program_hash(program: Program) -> str:
    """Compute a hash of the program's compiled kernel source."""
    from pyrung.circuitpy.codegen import compile_kernel

    compiled = compile_kernel(program)
    return hashlib.sha256(compiled.source.encode()).hexdigest()[:16]


def check_lock(
    program: Program,
    lock_path: Path = Path("pyrung.lock"),
    max_depth: int = 50,
    max_states: int = 100_000,
) -> StateDiff | None:
    """Recompute reachable states and diff against a lock file.

    Returns None if the lock matches, or a ``StateDiff`` if changed.
    """
    lock_data = read_lock(lock_path)
    projection = lock_data["projection"]
    old_states = _json_to_states(lock_data["reachable"])

    new_states = reachable_states(
        program,
        project=projection,
        max_depth=max_depth,
        max_states=max_states,
    )
    if isinstance(new_states, Intractable):
        msg = f"Verification intractable: {new_states.reason}"
        raise RuntimeError(msg)

    d = diff_states(old_states, new_states)
    if not d.added and not d.removed:
        return None
    return d
