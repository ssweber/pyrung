"""Exhaustive state-space verification for pyrung programs.

BFS over the reachable state space using the compiled replay kernel
as the execution oracle and the expression tree for search-space
reduction (dimension classification, value domain extraction,
don't-care pruning).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, cast

from pyrung.core.analysis.simplified import Expr, _condition_to_expr
from pyrung.core.kernel import CompiledKernel

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.program import Program

    from .inputs import _ExclusiveInputGroup

from .expr import _eval_atom as _eval_atom
from .expr import _eval_expr_from_state, _referenced_tags
from .expr import _live_inputs as _live_inputs
from .expr import _partial_eval as _partial_eval
from .results import PENDING as PENDING
from .results import Counterexample, Intractable, Proven
from .results import Decision as Decision
from .results import Journal as Journal
from .results import StateDiff as StateDiff
from .results import TagEntry as TagEntry
from .results import TraceStep as TraceStep
from .results import _ParentLink as _ParentLink


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
    dt: float
    edge_tag_exprs: dict[str, list[Expr]] = field(default_factory=dict)
    demoted_edge_names: tuple[str, ...] = ()
    synthetic_preset_tags: tuple[str, ...] = ()
    nondeterministic_names: tuple[str, ...] = ()
    free_input_names: frozenset[str] = field(default_factory=frozenset)
    always_live_input_names: tuple[str, ...] = ()
    exclusive_input_groups: tuple[_ExclusiveInputGroup, ...] = ()
    exclusive_input_group_by_member: dict[str, int] = field(default_factory=dict)
    joint_inputs: tuple[tuple[str, ...], ...] = ()
    caveats: tuple[str, ...] = ()
    journal: Journal | None = None
    drum_event_meta: dict[str, _DrumEventMeta] = field(default_factory=dict)
    independence_relation: IndependenceRelation | None = None
    free_input_factoring: FreeInputFactoring | None = None
    # Tags that BFS or step_fn can mutate. When set, kernel snapshots capture
    # only these keys (the rest are write-once constants); None = snapshot all.
    mutable_tag_names: frozenset[str] | None = None


from .absorb import _DrumEventMeta, _ThresholdVectorSpec
from .bfs import _bfs_explore
from .bfs import _build_trace as _build_trace
from .bfs import _merge_caveats as _merge_caveats
from .bfs import _projected_states as _projected_states
from .bfs import _projected_tuple as _projected_tuple
from .classify import (
    _classify_dimensions as _classify_dimensions,
)
from .classify import (
    _has_data_feedback as _has_data_feedback,
)
from .classify import (
    _pilot_sweep_domains as _pilot_sweep_domains,
)
from .elision import _elide_scan_local_stateful_dims as _elide_scan_local_stateful_dims
from .events import (
    _DoneEventSpec,
    _StateKeyDoneSpec,
    _ThresholdEventSpec,
)
from .independence import FreeInputFactoring, IndependenceRelation
from .lockfile import _apply_band as _apply_band
from .lockfile import (
    _build_band_maps,
    _build_choice_labels,
    _default_projection,
    _resolve_band_labels,
    _resolve_choice_labels,
)
from .lockfile import _json_default as _json_default
from .lockfile import _json_to_states as _json_to_states
from .lockfile import _match_band_predicate as _match_band_predicate
from .lockfile import _parse_band_number as _parse_band_number
from .lockfile import _states_to_json as _states_to_json
from .lockfile import check_lock as check_lock
from .lockfile import diff_states as diff_states
from .lockfile import program_hash as program_hash
from .lockfile import read_lock as read_lock
from .lockfile import write_lock as write_lock
from .passes import _DEFAULT_BFS_CONFIG as _DEFAULT_BFS_CONFIG
from .passes import _DEFAULT_OPT_CONFIG as _DEFAULT_OPT_CONFIG
from .passes import _BFSConfig as _BFSConfig
from .passes import (
    _JournalBuilder,
    _PassContext,
    _passes_for_opt_config,
    _run_pre_bfs_pipeline,
)
from .passes import _OptConfig as _OptConfig


def _resolve_opt_config(opt_config: _OptConfig | None, skip_optimizations: bool) -> _OptConfig:
    """Resolve the effective optimization config for a prove/reachable call.

    An explicit ``_opt_config`` wins. Otherwise ``_skip_optimizations=True``
    selects the maximally-reduced *sound* baseline — every soundness-optional
    reduction off, the reach-extending optimizations left on (disabling those
    under a finite depth_budget would under-approximate the reachable set).
    The absence of both means the production default.
    """
    if opt_config is not None:
        return opt_config
    return _OptConfig.sound_baseline() if skip_optimizations else _DEFAULT_OPT_CONFIG


def _validate_split_at(
    program: Program,
    split_at: list[str],
) -> dict[str, tuple[Any, ...]]:
    from pyrung.core.analysis.pdg import TagRole, build_program_graph
    from pyrung.core.tag import TagType

    from .absorb import _collect_done_acc_pairs
    from .expr import _edge_source_tags

    graph = build_program_graph(program)
    edge_sources = _edge_source_tags(program)
    done_info = _collect_done_acc_pairs(program)

    result: dict[str, tuple[Any, ...]] = {}
    for tag_name in split_at:
        tag = graph.tags.get(tag_name)
        if tag is None:
            msg = f"split_at tag {tag_name!r} does not exist in the program"
            raise ValueError(msg)

        role = graph.tag_roles.get(tag_name)
        is_written = tag_name in graph.writers_of
        is_nd = role == TagRole.INPUT or (tag.external and not is_written)
        if is_nd:
            msg = f"split_at tag {tag_name!r} is an external input — it is already nondeterministic"
            raise ValueError(msg)

        if tag_name in edge_sources:
            msg = (
                f"split_at tag {tag_name!r} appears in rise()/fall() — "
                f"edge-bearing tags cannot be split"
            )
            raise ValueError(msg)

        if tag.type is TagType.BOOL:
            domain: tuple[Any, ...] = (False, True)
        elif tag_name in done_info.pairs:
            domain = (False, PENDING, True)
        elif tag.choices is not None:
            domain = tuple(sorted(tag.choices.keys()))
        else:
            msg = (
                f"split_at tag {tag_name!r} has no small enumerable domain — "
                f"only Bool, Done-paired, and choices= tags can be split"
            )
            raise ValueError(msg)

        result[tag_name] = domain
    return result


def _build_explore_context(
    program: Program,
    *,
    scope: list[str] | None = None,
    project: tuple[str, ...] | None = None,
    extra_exprs: list[Expr] | None = None,
    dt: float = 0.010,
    compiled: CompiledKernel | None = None,
    joint_inputs: tuple[tuple[str, ...], ...] = (),
    exclusive_inputs: tuple[tuple[str, ...], ...] = (),
    split_at: list[str] | None = None,
    progress_info: Callable[[str], None] | None = None,
    progress_prefix: Callable[[], str] | None = None,
    _opt_config: _OptConfig = _DEFAULT_OPT_CONFIG,
    journal: bool = False,
) -> _ExploreContext | Intractable:
    """Build shared verifier context once for always()/reachable_states()."""
    split_at_tags = _validate_split_at(program, split_at) if split_at else None
    ctx = _PassContext(
        program=program,
        scope=scope,
        project=project,
        extra_exprs=extra_exprs,
        dt=dt,
        compiled=compiled,
        joint_inputs=joint_inputs,
        exclusive_inputs=exclusive_inputs,
        progress_info=progress_info,
        progress_prefix=progress_prefix,
        journal_builder=_JournalBuilder() if journal else None,
        split_at_tags=split_at_tags,
        scope_snapshot=_opt_config.scope_snapshot,
    )
    return _run_pre_bfs_pipeline(ctx, _passes_for_opt_config(_opt_config))


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
    """Split always() inputs into single-property or batch-property form.

    A sole list argument means "batch prove these properties". Tuple items
    inside that list represent grouped AND terms for one property.
    """
    if len(conditions) == 1 and isinstance(conditions[0], list):
        property_specs = list(conditions[0])
        if not property_specs:
            raise ValueError("always() property list cannot be empty")
        return True, property_specs

    if not conditions:
        raise ValueError("always() requires at least one condition")
    if len(conditions) == 1:
        return False, [conditions[0]]
    return False, [tuple(conditions)]


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
        empty_error="always() requires at least one condition",
        group_empty_error="always() condition group cannot be empty",
    )
    expr = _condition_to_expr(normalized)
    tags_in_expr = sorted(_referenced_tags(expr))

    def _predicate(state: dict[str, Any]) -> bool:
        return _eval_expr_from_state(expr, state) is not False

    return _predicate, tags_in_expr, expr


def _is_condition_like(obj: Any) -> bool:
    """True if *obj* is a Tag or Condition (not a plain callable)."""
    from pyrung.core.condition import Condition
    from pyrung.core.tag import Tag

    return isinstance(obj, (Tag, Condition))


def _upstream_cone(program: Program, tags: list[str]) -> frozenset[str]:
    """Compute the full upstream dependency cone for a set of tags."""
    from pyrung.core.analysis.pdg import build_program_graph

    graph = build_program_graph(program)
    cone: set[str] = set(tags)
    for tag_name in tags:
        cone.update(graph.upstream_slice_with_calls(tag_name))
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


def always(
    program: Program,
    *conditions: Any,
    scope: list[str] | None = None,
    depth_budget: int = 50,
    max_states: int = 100_000,
    joint_inputs: tuple[tuple[str, ...], ...] = (),
    exclusive_inputs: tuple[tuple[str, ...], ...] = (),
    split_at: list[str] | None = None,
    settled: bool = False,
    paced: bool = False,
    _skip_optimizations: bool = False,
    _opt_config: _OptConfig | None = None,
    journal: bool = False,
    _debug: bool = False,
) -> Proven | Counterexample | Intractable | list[Proven | Counterexample | Intractable]:
    """Prove a property holds in every reachable state.

    Accepts the same condition syntax as ``Rung()`` and ``when()``::

        always(logic, Or(~Running, EstopOK))
        always(logic, ~Running, EstopOK)        # implicit AND
        always(logic, (Ready, AutoMode))         # grouped AND as one property
        always(logic, [prop_a, prop_b, prop_c])  # batch prove in one pass
        always(logic, lambda s: s["Running"] <= s["Limit"])

    When given condition expressions, the upstream cone is derived
    automatically — no ``scope=`` needed.

    See :func:`never` for the dual: proving a bad state is unreachable.

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
    depth_budget : int
        Abstract BFS depth budget. Hidden-event acceleration may cover more
        concrete PLC scans than this budget.
    max_states : int
        Visited-set cap — bail with ``Intractable`` if exceeded.
    joint_inputs : tuple of tag-name tuples
        Input groups explored jointly (multi-flip combinations).
    exclusive_inputs : tuple of tag-name tuples
        Mutually exclusive input groups (at most one True at a time).
    settled : bool
        When True, evaluate predicates only on settled states (after
        pending timers/counters have fired), not on transient base
        states.  Use this for timer-gated alarm properties where the
        transient period before the timer fires is expected.
    paced : bool
        When True, enforce that after any input flip the next BFS step
        must hold all inputs (stutter).  This separates violations
        reachable only under aggressive back-to-back input changes from
        those reachable under realistic paced timing.  When paced
        exploration proves a property but aggressive exploration finds a
        counterexample, the result is ``Proven`` with a populated
        ``aggressive_counterexample`` field.
    """
    from pyrung.circuitpy.codegen import compile_kernel

    is_batch, property_specs = _normalize_property_specs(*conditions)
    compiled_properties = [_compile_property_spec(spec) for spec in property_specs]
    opt = _resolve_opt_config(_opt_config, _skip_optimizations)

    if not is_batch:
        predicate, auto_scope, expr = compiled_properties[0]
        effective_scope = scope if scope is not None else auto_scope
        extra = [expr] if expr is not None else []
        context = _build_explore_context(
            program,
            scope=effective_scope,
            extra_exprs=extra,
            joint_inputs=joint_inputs,
            exclusive_inputs=exclusive_inputs,
            split_at=split_at,
            _opt_config=opt,
            journal=journal,
        )
        if isinstance(context, Intractable):
            if _debug:
                return replace(context, _debug_context=context)
            return context
        if not paced:
            result = _bfs_explore(
                context,
                predicates=[predicate],
                depth_budget=depth_budget,
                max_states=max_states,
                bfs_config=opt.bfs_config,
                settled=settled,
            )[0]
        else:
            result = _prove_paced_single(
                context,
                predicate,
                depth_budget=depth_budget,
                max_states=max_states,
                bfs_config=opt.bfs_config,
                settled=settled,
            )
        if split_at and isinstance(result, Counterexample):
            names = ", ".join(sorted(split_at))
            result = replace(
                result,
                caveats=(
                    *result.caveats,
                    f"split_at=[{names}]: counterexample may exercise "
                    f"unreachable values of split tags",
                ),
            )
        if _debug:
            return replace(result, _debug_context=context)
        return result

    if scope is not None:
        partitions = [(list(range(len(compiled_properties))), scope)]
    else:
        partitions = _partition_batch(program, compiled_properties)

    compiled_kernel = compile_kernel(program, blockless=True, proof_metadata=True)
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
            joint_inputs=joint_inputs,
            exclusive_inputs=exclusive_inputs,
            split_at=split_at,
            _opt_config=opt,
            journal=journal,
        )
        if isinstance(context, Intractable):
            for i in indices:
                results[i] = replace(context, _debug_context=context) if _debug else context
            continue

        group_predicates = [compiled_properties[i][0] for i in indices]
        if not paced:
            group_results = cast(
                "list[Proven | Counterexample | Intractable]",
                _bfs_explore(
                    context,
                    predicates=group_predicates,
                    depth_budget=depth_budget,
                    max_states=max_states,
                    bfs_config=opt.bfs_config,
                    settled=settled,
                ),
            )
        else:
            group_results = _prove_paced_batch(
                context,
                group_predicates,
                depth_budget=depth_budget,
                max_states=max_states,
                bfs_config=opt.bfs_config,
                settled=settled,
            )
        for i, r in zip(indices, group_results, strict=True):
            if split_at and isinstance(r, Counterexample):
                names = ", ".join(sorted(split_at))
                r = replace(
                    r,
                    caveats=(
                        *r.caveats,
                        f"split_at=[{names}]: counterexample may exercise "
                        f"unreachable values of split tags",
                    ),
                )
            results[i] = replace(r, _debug_context=context) if _debug else r

    return [r if r is not None else Proven(states_explored=0) for r in results]


def never(
    program: Program,
    *conditions: Any,
    scope: list[str] | None = None,
    depth_budget: int = 50,
    max_states: int = 100_000,
    joint_inputs: tuple[tuple[str, ...], ...] = (),
    exclusive_inputs: tuple[tuple[str, ...], ...] = (),
    split_at: list[str] | None = None,
    settled: bool = False,
    paced: bool = False,
    _skip_optimizations: bool = False,
    _opt_config: _OptConfig | None = None,
    journal: bool = False,
    _debug: bool = False,
) -> Proven | Counterexample | Intractable | list[Proven | Counterexample | Intractable]:
    """Prove a bad state is never reachable.

    ``never(logic, A, B)`` proves that ``A and B`` is never simultaneously
    true — equivalent to ``always(logic, Or(~A, ~B))``.

    Accepts the same condition syntax and parameters as :func:`always`.
    """
    _, specs = _normalize_property_specs(*conditions)
    spec = specs[0] if len(specs) == 1 else tuple(specs)
    pred, auto_scope, _expr = _compile_property_spec(spec)
    neg = (lambda p: lambda s: not p(s))(pred)
    return always(
        program,
        neg,
        scope=scope if scope is not None else auto_scope,
        depth_budget=depth_budget,
        max_states=max_states,
        joint_inputs=joint_inputs,
        exclusive_inputs=exclusive_inputs,
        split_at=split_at,
        settled=settled,
        paced=paced,
        _skip_optimizations=_skip_optimizations,
        _opt_config=_opt_config,
        journal=journal,
        _debug=_debug,
    )


def _prove_paced_single(
    context: _ExploreContext,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    depth_budget: int,
    max_states: int,
    bfs_config: _BFSConfig,
    settled: bool,
) -> Proven | Counterexample | Intractable:
    """Two-pass paced prove: paced BFS first, aggressive only if paced proves."""
    paced_result = _bfs_explore(
        context,
        predicates=[predicate],
        depth_budget=depth_budget,
        max_states=max_states,
        bfs_config=bfs_config,
        settled=settled,
        paced=True,
    )[0]
    if not isinstance(paced_result, Proven):
        return paced_result
    aggressive_result = _bfs_explore(
        context,
        predicates=[predicate],
        depth_budget=depth_budget,
        max_states=max_states,
        bfs_config=bfs_config,
        settled=settled,
    )[0]
    if isinstance(aggressive_result, Counterexample):
        return replace(paced_result, aggressive_counterexample=aggressive_result)
    return paced_result


def _prove_paced_batch(
    context: _ExploreContext,
    predicates: list[Callable[[dict[str, Any]], bool]],
    *,
    depth_budget: int,
    max_states: int,
    bfs_config: _BFSConfig,
    settled: bool,
) -> list[Proven | Counterexample | Intractable]:
    """Two-pass paced prove for batch: paced first, aggressive for paced-proven properties."""
    _ResultList = list[Proven | Counterexample | Intractable]
    paced_results = cast(
        _ResultList,
        _bfs_explore(
            context,
            predicates=predicates,
            depth_budget=depth_budget,
            max_states=max_states,
            bfs_config=bfs_config,
            settled=settled,
            paced=True,
        ),
    )
    proven_indices = [i for i, r in enumerate(paced_results) if isinstance(r, Proven)]
    if not proven_indices:
        return paced_results
    aggressive_predicates = [predicates[i] for i in proven_indices]
    aggressive_results = cast(
        _ResultList,
        _bfs_explore(
            context,
            predicates=aggressive_predicates,
            depth_budget=depth_budget,
            max_states=max_states,
            bfs_config=bfs_config,
            settled=settled,
        ),
    )
    for idx, aggressive_result in zip(proven_indices, aggressive_results, strict=True):
        if isinstance(aggressive_result, Counterexample):
            paced_proven = paced_results[idx]
            assert isinstance(paced_proven, Proven)
            paced_results[idx] = replace(paced_proven, aggressive_counterexample=aggressive_result)
    return paced_results


def _format_elapsed(elapsed: float) -> str:
    h, rem = divmod(int(elapsed), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


@dataclass
class _StderrProgressReporter:
    start: float = field(default_factory=time.monotonic)

    def emit(self, message: str) -> None:
        import sys

        elapsed = time.monotonic() - self.start
        print(f"[{_format_elapsed(elapsed)}] {message}", file=sys.stderr)

    def info(self, message: str, *, label: str = "") -> None:
        prefix = f"{label} | " if label else ""
        self.emit(f"{prefix}{message}")

    def report_dimensions(
        self,
        context: _ExploreContext,
        *,
        label: str = "",
    ) -> None:
        from .absorb import _THRESHOLD_KIND_COMPARISON_ONLY

        if not isinstance(context, _ExploreContext):
            return
        stateful = context.stateful_dims
        nd = context.nondeterministic_dims
        n_stateful = len(stateful)
        n_input = len(nd)
        product = 1
        for domain in stateful.values():
            product *= len(domain)
        for domain in nd.values():
            product *= len(domain)

        self.info(
            f"{n_stateful} stateful + {n_input} input dimensions | state space: {product:,}",
            label=label,
        )

        dims: list[tuple[str, int]] = []
        for name, domain in stateful.items():
            dims.append((name, len(domain)))
        for name, domain in nd.items():
            dims.append((name, len(domain)))
        dims.sort(key=lambda x: x[1], reverse=True)

        top = dims[:8]
        if top:
            parts = [f"{name}: {size}" for name, size in top]
            suffix = f" ... +{len(dims) - 8} more" if len(dims) > 8 else ""
            self.info(f"  {', '.join(parts)}{suffix}", label=label)

        absorbed: dict[str, int] = {}
        for vec in context.threshold_vector_specs:
            kind = "comparison" if vec.kind == _THRESHOLD_KIND_COMPARISON_ONLY else "threshold"
            absorbed[kind] = absorbed.get(kind, 0) + 1
        if absorbed:
            parts = [f"{kind}: {count}" for kind, count in sorted(absorbed.items())]
            self.info(f"  absorbed: {', '.join(parts)}", label=label)

    def prefix_builder(self, label: str = "") -> Callable[[], str]:
        def _build() -> str:
            elapsed = time.monotonic() - self.start
            prefix = f"{label} | " if label else ""
            return f"[{_format_elapsed(elapsed)}] {prefix}"

        return _build

    def bfs_callback(self, label: str = "") -> _BFSProgress:
        self.info("BFS started ...", label=label)
        return _BFSProgress(self, label)


class _BFSProgress:
    __slots__ = (
        "_reporter",
        "_label",
        "_prev_queue",
        "_depth",
        "_prev_visited",
        "_prev_steps",
        "_steps",
    )

    def __init__(self, reporter: _StderrProgressReporter, label: str) -> None:
        self._reporter = reporter
        self._label = label
        self._prev_queue = 0
        self._prev_visited = 0
        self._prev_steps = 0
        self._steps = 0
        self._depth = 0

    def set_depth(self, depth: int) -> None:
        self._depth = depth

    def step(self) -> None:
        self._steps += 1

    def __call__(self, visited: int, queue_size: int, dt: float) -> None:
        if queue_size > self._prev_queue:
            arrow = "↑"
        elif queue_size < self._prev_queue:
            arrow = "↓"
        else:
            arrow = "="
        self._prev_queue = queue_size
        new_visited = visited - self._prev_visited
        interval_steps = self._steps - self._prev_steps
        self._prev_visited = visited
        self._prev_steps = self._steps
        disc_rate = new_visited / dt if dt > 0 else 0
        step_rate = interval_steps / dt if dt > 0 else 0
        self._reporter.info(
            f"depth={self._depth} | visited={visited:,} | queue={queue_size:,} ({arrow})"
            f" | {disc_rate:,.0f} new/s | {step_rate:,.0f} steps/s",
            label=self._label,
        )


def _stderr_progress(
    label: str = "",
) -> Callable[[int, int, float], None]:
    return _StderrProgressReporter().bfs_callback(label)


def _build_reachable_context(
    program: Program,
    *,
    scope: list[str],
    project: tuple[str, ...],
    joint_inputs: tuple[tuple[str, ...], ...] = (),
    exclusive_inputs: tuple[tuple[str, ...], ...] = (),
    split_at: list[str] | None = None,
    progress_info: Callable[[str], None] | None = None,
    progress_prefix: Callable[[], str] | None = None,
    _opt_config: _OptConfig = _DEFAULT_OPT_CONFIG,
    journal: bool = False,
) -> _ExploreContext | Intractable:
    """Build a reachable-states context on the original program."""
    from pyrung.circuitpy.codegen import compile_kernel

    compiled_kernel = compile_kernel(program, blockless=True, proof_metadata=True)
    return _build_explore_context(
        program,
        scope=scope,
        project=project,
        compiled=compiled_kernel,
        joint_inputs=joint_inputs,
        exclusive_inputs=exclusive_inputs,
        split_at=split_at,
        progress_info=progress_info,
        progress_prefix=progress_prefix,
        _opt_config=_opt_config,
        journal=journal,
    )


class _DebugFrozenSet(frozenset):
    """frozenset subclass that can carry a _debug_context attribute."""

    _debug_context: _ExploreContext | None = None


def reachable_states(
    program: Program,
    scope: list[str] | None = None,
    project: list[str] | None = None,
    depth_budget: int = 50,
    max_states: int = 100_000,
    progress: bool | Callable[[int, int, float], None] = False,
    joint_inputs: tuple[tuple[str, ...], ...] = (),
    exclusive_inputs: tuple[tuple[str, ...], ...] = (),
    split_at: list[str] | None = None,
    _skip_optimizations: bool = False,
    _opt_config: _OptConfig | None = None,
    _journal: bool = False,
    _debug: bool = False,
) -> frozenset[frozenset[tuple[str, Any]]] | Intractable:
    """Compute the full reachable state space.

    Parameters
    ----------
    program : Program
        The compiled ladder logic program.
    scope : list of tag names, optional
        If given, restrict input enumeration to the upstream cone.
        Defaults to the projection tags.
    project : list of tag names, optional
        Tags to project onto. Defaults to terminal tags.
    depth_budget : int
        Abstract BFS depth budget. Hidden-event acceleration may cover more
        concrete PLC scans than this budget.
    max_states : int
        Visited-set cap.
    joint_inputs : tuple of tag-name tuples
        Input groups explored jointly (multi-flip combinations).
    exclusive_inputs : tuple of tag-name tuples
        Mutually exclusive input groups (at most one True at a time).
    """
    project_list = list(project) if project is not None else _default_projection(program)
    project_names = tuple(project_list)
    opt = _resolve_opt_config(_opt_config, _skip_optimizations)

    progress_cb: Callable[[int, int, float], None] | None = None
    stderr_reporter: _StderrProgressReporter | None = None
    if progress is True:
        stderr_reporter = _StderrProgressReporter()
    elif callable(progress):
        progress_cb = progress

    effective_scope = sorted(set(scope or project_list) | set(project_names))
    if stderr_reporter is not None:
        stderr_reporter.info(
            f"preparing reachability slice for {len(project_names):,} projected tag(s)"
        )
    context = _build_reachable_context(
        program,
        scope=effective_scope,
        project=project_names,
        joint_inputs=joint_inputs,
        exclusive_inputs=exclusive_inputs,
        split_at=split_at,
        progress_info=stderr_reporter.info if stderr_reporter is not None else None,
        progress_prefix=stderr_reporter.prefix_builder() if stderr_reporter is not None else None,
        _opt_config=opt,
        journal=_journal,
    )
    if isinstance(context, Intractable):
        if _debug:
            return replace(context, _debug_context=context)
        return context
    if stderr_reporter is not None:
        stderr_reporter.report_dimensions(context)
    bfs_progress = stderr_reporter.bfs_callback() if stderr_reporter is not None else progress_cb
    result = _bfs_explore(
        context,
        project=project_names,
        depth_budget=depth_budget,
        max_states=max_states,
        bfs_config=opt.bfs_config,
        progress=bfs_progress,
    )
    if isinstance(result, Intractable):
        if _debug:
            return replace(result, _debug_context=context)
        return result
    assert isinstance(result, frozenset)
    choice_labels = _build_choice_labels(project_list, context.graph.tags)
    result = _resolve_choice_labels(result, choice_labels)
    band_maps = _build_band_maps(project_list, context.graph.tags)
    result = _resolve_band_labels(result, band_maps)
    if stderr_reporter is not None:
        stderr_reporter.info(f"reachable states complete | total={len(result):,}")
    if _debug:
        debug_result = _DebugFrozenSet(result)
        debug_result._debug_context = context
        return debug_result
    return result


# ---------------------------------------------------------------------------
# explore() — build a complete transition graph
# ---------------------------------------------------------------------------


class _GraphBuilder:
    """Accumulates edges and state snapshots from the BFS edge_collector."""

    def __init__(
        self,
        tag_names: frozenset[str],
        initial_key: tuple[Any, ...],
        initial_tags: dict[str, Any],
        stateful_names: tuple[str, ...] = (),
        done_specs: tuple[tuple[int, str, str], ...] = (),
    ) -> None:
        from pyrung.core.analysis.graph import TransitionEdge

        self._TransitionEdge = TransitionEdge
        self._tag_names = tag_names
        self._adjacency: dict[tuple[Any, ...], list[Any]] = {}
        self._state_tags: dict[tuple[Any, ...], dict[str, Any]] = {
            initial_key: {n: initial_tags.get(n) for n in tag_names}
        }
        self._initial_key = initial_key
        self._stateful_names = stateful_names
        self._done_specs = done_specs

    def collect(
        self,
        src_key: tuple[Any, ...],
        dst_key: tuple[Any, ...],
        input_dict: dict[str, Any],
        scans: int,
        caveats: tuple[str, ...],
        dest_tags: dict[str, Any],
    ) -> None:
        snapshot = {n: dest_tags.get(n) for n in self._tag_names}
        edge = self._TransitionEdge(src_key, dst_key, input_dict, scans, caveats, snapshot)
        self._adjacency.setdefault(src_key, []).append(edge)
        if dst_key not in self._state_tags:
            self._state_tags[dst_key] = snapshot

    def build(self) -> Any:
        from pyrung.core.analysis.graph import TransitionGraph

        return TransitionGraph(
            adjacency=self._adjacency,
            state_tags=self._state_tags,
            initial_key=self._initial_key,
            tag_names=self._tag_names,
            stateful_names=self._stateful_names,
            done_specs=self._done_specs,
        )


def explore(
    program: Program,
    *,
    scope: list[str] | None = None,
    project: list[str] | None = None,
    depth_budget: int = 50,
    max_states: int = 100_000,
    progress: bool | Callable[[int, int, float], None] = False,
    joint_inputs: tuple[tuple[str, ...], ...] = (),
    exclusive_inputs: tuple[tuple[str, ...], ...] = (),
    split_at: list[str] | None = None,
    _skip_optimizations: bool = False,
    _opt_config: _OptConfig | None = None,
) -> Any:
    """Build a complete transition graph via BFS exploration.

    Uses the same projection/scope logic as :func:`reachable_states`
    but additionally captures edges into a ``TransitionGraph``.

    Returns a ``TransitionGraph`` on success or ``Intractable`` if the
    state space exceeds *max_states*.
    """
    from pyrung.core.analysis.pdg import build_program_graph

    pdg = build_program_graph(program)
    all_tag_names = sorted(pdg.tags)
    project_list = list(project) if project is not None else _default_projection(program)
    project_names = tuple(project_list)
    opt = _resolve_opt_config(_opt_config, _skip_optimizations)

    progress_cb: Callable[[int, int, float], None] | None = None
    stderr_reporter: _StderrProgressReporter | None = None
    if progress is True:
        stderr_reporter = _StderrProgressReporter()
    elif callable(progress):
        progress_cb = progress

    effective_scope = sorted(set(scope or all_tag_names) | set(project_names))
    if stderr_reporter is not None:
        stderr_reporter.info(f"preparing exploration for {len(project_names):,} projected tag(s)")
    context = _build_reachable_context(
        program,
        scope=effective_scope,
        project=project_names,
        joint_inputs=joint_inputs,
        exclusive_inputs=exclusive_inputs,
        split_at=split_at,
        progress_info=stderr_reporter.info if stderr_reporter is not None else None,
        progress_prefix=stderr_reporter.prefix_builder() if stderr_reporter is not None else None,
        _opt_config=opt,
    )
    if isinstance(context, Intractable):
        return context

    from .kernel import _EdgeCompressor, _seed_synthetic_presets

    tag_names = frozenset(all_tag_names)

    init_kernel = context.compiled.create_kernel()
    _seed_synthetic_presets(context, init_kernel)
    edge_comp = _EdgeCompressor(context)
    if opt.bfs_config.edge_compression:
        initial_key = edge_comp.state_key(init_kernel)
    else:
        from .kernel import _extract_state_key

        initial_key = _extract_state_key(
            init_kernel,
            context.stateful_names,
            context.edge_tag_names,
            context.memory_key_names,
            context.state_key_done_specs,
            context.threshold_vector_specs,
            nondeterministic_names=context.nondeterministic_names,
        )
    demoted = context.demoted_edge_names
    if demoted:
        initial_bprev = tuple(init_kernel.prev.get(n) for n in demoted)
        initial_tid: tuple[Any, ...] = (initial_key, initial_bprev)
    else:
        initial_tid = initial_key
    initial_tags = dict(init_kernel.tags)

    done_specs = tuple((s.index, s.acc_name, s.kind) for s in context.state_key_done_specs)
    builder = _GraphBuilder(
        tag_names, initial_tid, initial_tags, context.stateful_names, done_specs
    )

    if stderr_reporter is not None:
        stderr_reporter.report_dimensions(context)
    bfs_progress = stderr_reporter.bfs_callback() if stderr_reporter is not None else progress_cb
    result = _bfs_explore(
        context,
        depth_budget=depth_budget,
        max_states=max_states,
        bfs_config=opt.bfs_config,
        progress=bfs_progress,
        edge_collector=builder.collect,
    )
    if isinstance(result, Intractable):
        return result

    return builder.build()
