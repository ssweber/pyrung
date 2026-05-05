"""Kernel integration helpers for prove BFS."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.simplified import And, Atom, Const, Expr, _condition_to_expr
from pyrung.core.kernel import CompiledKernel, ReplayKernel

from .absorb import _DONE_KIND_COUNT_DOWN, _THRESHOLD_FORM_GT, _done_acc_state
from .expr import _has_edge_atom, _live_inputs, _partial_eval

if TYPE_CHECKING:
    from pyrung.core.program import Program

    from . import _ExploreContext
    from .absorb import _ThresholdVectorSpec
    from .events import _StateKeyDoneSpec

_EDGE_DEAD: Any = object()
_INPUT_DEAD: Any = object()


def _step_compiled_kernel(
    compiled: CompiledKernel,
    kernel: ReplayKernel,
    *,
    dt: float,
) -> None:
    """Execute one compiled scan, syncing legacy block arrays only when needed."""
    if not compiled.blockless:
        for spec in compiled.block_specs.values():
            kernel.load_block_from_tags(spec)
    compiled.step_fn(kernel.tags, kernel.blocks, kernel.memory, kernel.prev, dt)
    if not compiled.blockless:
        for spec in compiled.block_specs.values():
            kernel.flush_block_to_tags(spec)
    for name in compiled.edge_tags:
        if name in kernel.tags:
            kernel.prev[name] = kernel.tags[name]
    kernel.advance(dt)


def _collect_edge_tag_exprs(
    program: Program,
    edge_tag_names: tuple[str, ...],
) -> dict[str, list[Expr]]:
    """For each edge tag, collect full rung conditions containing its rise/fall.

    Uses the complete AND of all rung conditions so that partial evaluation
    can resolve masked branches (e.g. ``And(State == IDLE, rise(Sensor))``
    resolves to False when State != IDLE).
    """
    result: dict[str, list[Expr]] = {name: [] for name in edge_tag_names}
    if not edge_tag_names:
        return result
    edge_set = frozenset(edge_tag_names)
    seen: dict[str, set[int]] = {name: set() for name in edge_tag_names}
    for rung_idx, rung in enumerate(program.rungs):
        conds = rung._conditions
        if not conds:
            continue
        if len(conds) == 1:
            expr = _condition_to_expr(conds[0])
        else:
            expr = And(tuple(_condition_to_expr(c) for c in conds))
        for name in edge_set:
            if _has_edge_atom(expr, name) and rung_idx not in seen[name]:
                seen[name].add(rung_idx)
                result[name].append(expr)
    return result


def _live_edge_prevs(
    state: dict[str, Any],
    nd_dims: dict[str, tuple[Any, ...]],
    edge_tag_exprs: dict[str, list[Expr]],
) -> frozenset[str]:
    """Determine which edge tag prev values are live at a given state.

    An edge prev is live if any expression containing its rise/fall atom
    does not resolve to a constant under partial evaluation of known
    (non-nondeterministic) state.
    """
    nd_names = frozenset(nd_dims)
    known = {k: v for k, v in state.items() if k not in nd_names}

    live: set[str] = set()
    for name, exprs in edge_tag_exprs.items():
        for expr in exprs:
            residual = _partial_eval(expr, known)
            if not isinstance(residual, Const):
                live.add(name)
                break
    return frozenset(live)


def _abstracted_hidden_tags(context: _ExploreContext) -> frozenset[str]:
    """Tags whose concrete values are intentionally hidden from the BFS state."""
    exact_stateful = set(context.stateful_names)
    hidden = {name for name in context.synthetic_preset_tags if name not in exact_stateful}
    hidden.update(
        spec.acc_name
        for spec in context.state_key_done_specs
        if spec.acc_name not in exact_stateful
    )
    for vector in context.threshold_vector_specs:
        if vector.acc_name not in exact_stateful:
            hidden.add(vector.acc_name)
        for atom in vector.atoms:
            if isinstance(atom.threshold, str) and atom.threshold not in exact_stateful:
                hidden.add(atom.threshold)
    return frozenset(hidden)


def _visible_partial_eval_state(
    state: dict[str, Any],
    hidden_tags: frozenset[str],
) -> dict[str, Any]:
    """Drop abstracted tags before partial evaluation and liveness analysis."""
    if not hidden_tags:
        return state
    return {name: value for name, value in state.items() if name not in hidden_tags}


def _precompute_always_live_edges(
    edge_tag_exprs: dict[str, list[Expr]],
) -> frozenset[str]:
    """Find edge tags whose expressions can never be resolved.

    Bare rise/fall atoms (no surrounding AND/OR with stateful guards)
    are always live regardless of state.
    """
    always_live: set[str] = set()
    for name, exprs in edge_tag_exprs.items():
        for expr in exprs:
            if isinstance(expr, Atom):
                always_live.add(name)
                break
    return frozenset(always_live)


def _step_kernel(
    context: _ExploreContext,
    kernel: ReplayKernel,
) -> None:
    """Execute one scan cycle on the kernel."""
    _step_compiled_kernel(context.compiled, kernel, dt=context.dt)


def _seed_synthetic_presets(context: _ExploreContext, kernel: ReplayKernel) -> None:
    """Seed absorbed dynamic presets away from their default zero value."""
    for name in context.synthetic_preset_tags:
        kernel.tags[name] = 1


@dataclass(frozen=True, slots=True)
class _KernelSnapshot:
    tags: dict[str, Any]
    memory: dict[str, Any]
    prev: dict[str, Any]
    scan_id: int
    timestamp: float


def _snapshot_kernel(kernel: ReplayKernel) -> _KernelSnapshot:
    """Deep-copy kernel state (blocks excluded — reloaded from tags each step)."""
    return _KernelSnapshot(
        tags=dict(kernel.tags),
        memory=dict(kernel.memory),
        prev=dict(kernel.prev),
        scan_id=kernel.scan_id,
        timestamp=kernel.timestamp,
    )


def _restore_kernel(kernel: ReplayKernel, snap: _KernelSnapshot) -> None:
    """Restore kernel state from a snapshot."""
    kernel.tags.clear()
    kernel.tags.update(snap.tags)
    kernel.memory.clear()
    kernel.memory.update(snap.memory)
    kernel.prev.clear()
    kernel.prev.update(snap.prev)
    kernel.scan_id = snap.scan_id
    kernel.timestamp = snap.timestamp


class _EdgeCompressor:
    """Cached edge-prev liveness for state key compression.

    Edge liveness depends only on stateful dims (non-ND known state).
    This caches the result per stateful-key prefix so the (relatively
    expensive) partial evaluation runs at most once per unique stateful
    configuration, not per combo.
    """

    __slots__ = ("_context", "_compressible", "_cache", "_hidden_tags")

    def __init__(self, context: _ExploreContext) -> None:
        self._context = context
        always_live = _precompute_always_live_edges(context.edge_tag_exprs)
        self._compressible = {
            name: exprs for name, exprs in context.edge_tag_exprs.items() if name not in always_live
        }
        self._cache: dict[tuple[Any, ...], frozenset[str]] = {}
        self._hidden_tags = _abstracted_hidden_tags(context)

    def live_edges(self, kernel: ReplayKernel) -> frozenset[str] | None:
        """Return the set of live edge tags, or None if no compression."""
        if not self._compressible:
            return None
        ctx = self._context
        stateful_prefix = tuple(kernel.tags.get(n) for n in ctx.stateful_names)
        threshold_prefix = _threshold_vector_key(kernel, ctx.threshold_vector_specs)
        stateful_prefix = stateful_prefix + threshold_prefix
        cached = self._cache.get(stateful_prefix)
        if cached is not None:
            return cached
        result = _live_edge_prevs(
            _visible_partial_eval_state(kernel.tags, self._hidden_tags),
            ctx.nondeterministic_dims,
            self._compressible,
        )
        self._cache[stateful_prefix] = result
        return result

    def state_key(
        self,
        kernel: ReplayKernel,
        live_inputs: frozenset[str] | None = None,
    ) -> tuple[Any, ...]:
        ctx = self._context
        return _extract_state_key(
            kernel,
            ctx.stateful_names,
            ctx.edge_tag_names,
            ctx.memory_key_names,
            ctx.state_key_done_specs,
            ctx.threshold_vector_specs,
            self.live_edges(kernel),
            nondeterministic_names=ctx.nondeterministic_names,
            live_inputs=live_inputs,
        )


class _LiveInputCache:
    """Cached live-input results per stateful-key prefix.

    Same cache-key strategy as _EdgeCompressor: states sharing a stateful
    prefix + threshold vector produce identical _partial_eval results for
    non-ND tags, yielding the same live-input set.
    """

    __slots__ = ("_context", "_cache", "_hidden_tags", "_hidden_input_deps")

    def __init__(self, context: _ExploreContext) -> None:
        self._context = context
        self._cache: dict[tuple[Any, ...], frozenset[str]] = {}
        self._hidden_tags = _abstracted_hidden_tags(context)
        nd_names = frozenset(context.nondeterministic_dims)
        self._hidden_input_deps = {
            tag_name: frozenset(context.graph.upstream_slice(tag_name) & nd_names)
            for tag_name in self._hidden_tags
        }

    def live_inputs(self, kernel: ReplayKernel) -> frozenset[str]:
        ctx = self._context
        stateful_prefix = tuple(kernel.tags.get(n) for n in ctx.stateful_names)
        threshold_prefix = _threshold_vector_key(kernel, ctx.threshold_vector_specs)
        cache_key = stateful_prefix + threshold_prefix
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        result = _live_inputs(
            _visible_partial_eval_state(kernel.tags, self._hidden_tags),
            ctx.nondeterministic_dims,
            ctx.all_exprs,
            self._hidden_input_deps,
        )
        if ctx.always_live_input_names:
            result = frozenset(set(result) | set(ctx.always_live_input_names))
        self._cache[cache_key] = result
        return result


def _threshold_value(kernel: ReplayKernel, threshold: int | float | str) -> Any:
    if isinstance(threshold, str):
        return kernel.tags.get(threshold)
    return threshold


def _threshold_crossed(
    kernel: ReplayKernel,
    kind: str,
    acc_name: str,
    threshold: int | float | str,
    form: str,
) -> bool:
    acc_value = kernel.tags.get(acc_name)
    threshold_value = _threshold_value(kernel, threshold)
    if acc_value is None or threshold_value is None:
        return False
    if kind == _DONE_KIND_COUNT_DOWN:
        acc_value = -acc_value
        threshold_value = -threshold_value
    if form == _THRESHOLD_FORM_GT:
        return acc_value > threshold_value
    return acc_value >= threshold_value


def _threshold_vector_key(
    kernel: ReplayKernel,
    specs: tuple[_ThresholdVectorSpec, ...],
) -> tuple[Any, ...]:
    result: list[Any] = []
    for spec in specs:
        result.append(
            tuple(
                _threshold_crossed(kernel, spec.kind, spec.acc_name, atom.threshold, atom.form)
                for atom in spec.atoms
            )
        )
    return tuple(result)


def _extract_state_key(
    kernel: ReplayKernel,
    stateful_names: tuple[str, ...],
    edge_tag_names: tuple[str, ...],
    memory_key_names: tuple[str, ...] = (),
    done_specs: tuple[_StateKeyDoneSpec, ...] = (),
    threshold_vector_specs: tuple[_ThresholdVectorSpec, ...] = (),
    live_edges: frozenset[str] | None = None,
    nondeterministic_names: tuple[str, ...] = (),
    live_inputs: frozenset[str] | None = None,
) -> tuple[Any, ...]:
    """Hash key for the visited set — stateful + input + edge prev values.

    Inputs are included so the BFS can interleave single-dimension flips
    from each distinct input baseline.  Dead inputs (not live in the
    current stateful configuration) are masked to a sentinel, collapsing
    states that differ only in irrelevant input values.

    Timer/counter Done bits use three-valued abstraction
    ``(False, PENDING, True)`` derived from Done + Acc.

    When *live_edges* is provided, edge tags not in the set use a sentinel
    value, collapsing states that differ only in irrelevant prev values.
    """
    parts = [kernel.tags.get(name) for name in stateful_names]
    for spec in done_specs:
        parts[spec.index] = _done_acc_state(
            spec.kind,
            parts[spec.index],
            kernel.tags.get(spec.acc_name),
        )
    parts.extend(_threshold_vector_key(kernel, threshold_vector_specs))
    for n in nondeterministic_names:
        if live_inputs is not None and n not in live_inputs:
            parts.append(_INPUT_DEAD)
        else:
            parts.append(kernel.tags.get(n))
    for n in edge_tag_names:
        if live_edges is not None and n not in live_edges:
            parts.append(_EDGE_DEAD)
        else:
            parts.append(kernel.prev.get(n))
    for mk in memory_key_names:
        parts.append(kernel.memory.get(mk))
    return tuple(parts)
