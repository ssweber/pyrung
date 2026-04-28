"""Configurable pre-BFS pass pipeline for prove."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pdg import TagRole, build_program_graph
from pyrung.core.analysis.simplified import Expr
from pyrung.core.kernel import CompiledKernel

from . import Intractable, _ExploreContext
from .absorb import (
    _collect_done_acc_pairs,
    _DoneAccInfo,
    _find_redundant_acc_absorptions,
    _find_threshold_absorptions,
    _has_forbidden_data_read,
    _RedundantAccAbsorptions,
    _ThresholdAbsorptions,
)
from .classify import (
    _classify_dimensions_from_graph,
    _collect_all_exprs,
    _collect_literal_write_domains,
    _collect_structural_domains,
    _extract_value_domain,
    _pilot_sweep_domains,
)
from .events import _DoneEventSpec, _StateKeyDoneSpec, _ThresholdEventSpec
from .expr import _collect_atoms_for_tag
from .kernel import _collect_edge_tag_exprs

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.program import Program


@dataclass
class _PassContext:
    """Mutable accumulator built up by pre-BFS passes."""

    program: Program
    scope: list[str] | None
    project: tuple[str, ...] | None
    extra_exprs: list[Expr] | None
    dt: float
    compiled: CompiledKernel | None

    graph: ProgramGraph | None = None
    all_exprs: list[Expr] | None = None
    intractable: Intractable | None = None

    stateful_dims: dict[str, tuple[Any, ...]] | None = None
    nondeterministic_dims: dict[str, tuple[Any, ...]] | None = None
    done_acc: dict[str, str] | None = None
    done_presets: dict[str, int] | None = None
    done_kinds: dict[str, str] | None = None

    done_acc_info: _DoneAccInfo | None = None
    absorptions: _RedundantAccAbsorptions | None = None
    threshold_absorptions: _ThresholdAbsorptions | None = None

    stateful_names: tuple[str, ...] | None = None
    edge_tag_names: tuple[str, ...] | None = None
    state_key_done_specs: tuple[_StateKeyDoneSpec, ...] | None = None
    done_event_specs: tuple[_DoneEventSpec, ...] | None = None
    threshold_event_specs: tuple[_ThresholdEventSpec, ...] | None = None
    edge_tag_exprs: dict[str, list[Expr]] | None = None
    memory_key_names: tuple[str, ...] | None = None
    synthetic_preset_tags: tuple[str, ...] | None = None

    def freeze(self) -> _ExploreContext:
        assert self.compiled is not None
        assert self.graph is not None
        assert self.all_exprs is not None
        assert self.stateful_dims is not None
        assert self.nondeterministic_dims is not None
        assert self.stateful_names is not None
        assert self.edge_tag_names is not None
        assert self.memory_key_names is not None
        assert self.state_key_done_specs is not None
        assert self.done_event_specs is not None
        assert self.threshold_absorptions is not None
        assert self.threshold_event_specs is not None
        return _ExploreContext(
            compiled=self.compiled,
            graph=self.graph,
            all_exprs=self.all_exprs,
            stateful_dims=self.stateful_dims,
            nondeterministic_dims=self.nondeterministic_dims,
            stateful_names=self.stateful_names,
            edge_tag_names=self.edge_tag_names,
            memory_key_names=self.memory_key_names,
            state_key_done_specs=self.state_key_done_specs,
            done_event_specs=self.done_event_specs,
            threshold_vector_specs=self.threshold_absorptions.vector_specs,
            threshold_event_specs=self.threshold_event_specs,
            block_specs=tuple(self.compiled.block_specs.values()),
            dt=self.dt,
            edge_tag_exprs=self.edge_tag_exprs or {},
            synthetic_preset_tags=self.synthetic_preset_tags or (),
        )


@dataclass(frozen=True)
class _PreBFSPass:
    name: str
    description: str
    run: Callable[[_PassContext], None]
    enabled: bool = True


@dataclass(frozen=True)
class _BFSConfig:
    """Enable/disable flags for BFS-interleaved optimizations."""

    live_input_pruning: bool = True
    edge_compression: bool = True
    hidden_event_jumping: bool = True
    pending_settlement: bool = True

    @property
    def active_optimizations(self) -> tuple[str, ...]:
        names: list[str] = []
        if self.live_input_pruning:
            names.append("live_input_pruning")
        if self.edge_compression:
            names.append("edge_compression")
        if self.hidden_event_jumping:
            names.append("hidden_event_jumping")
        if self.pending_settlement:
            names.append("pending_settlement")
        return tuple(names)


_DEFAULT_BFS_CONFIG = _BFSConfig()


def _pass_build_graph(ctx: _PassContext) -> None:
    ctx.graph = build_program_graph(ctx.program)
    ctx.all_exprs = _collect_all_exprs(ctx.program, ctx.graph, scope=ctx.scope)
    if ctx.extra_exprs:
        ctx.all_exprs = ctx.all_exprs + ctx.extra_exprs


def _pass_classify_dimensions(ctx: _PassContext) -> None:
    assert ctx.graph is not None and ctx.all_exprs is not None
    result = _classify_dimensions_from_graph(
        ctx.program,
        ctx.graph,
        ctx.all_exprs,
        scope=ctx.scope,
        project=ctx.project,
    )
    if isinstance(result, Intractable):
        ctx.intractable = result
        return
    sd, nd, _comb, da, dp, dk = result
    ctx.stateful_dims = sd
    ctx.nondeterministic_dims = nd
    ctx.done_acc = da
    ctx.done_presets = dp
    ctx.done_kinds = dk


def _pass_pilot_sweep(ctx: _PassContext) -> None:
    from pyrung.circuitpy.codegen import compile_kernel as _compile_kernel

    if ctx.intractable is None or not ctx.intractable.tags:
        return
    assert ctx.graph is not None and ctx.all_exprs is not None
    literal_write_domains = _collect_literal_write_domains(ctx.program, ctx.graph.tags)
    structural_domains = _collect_structural_domains(
        ctx.program,
        ctx.graph,
        ctx.all_exprs,
        literal_write_domains,
    )
    if ctx.compiled is None:
        ctx.compiled = _compile_kernel(ctx.program)
    first_pass_nd: dict[str, tuple[Any, ...]] = {}
    for tag_name, tag in ctx.graph.tags.items():
        role = ctx.graph.tag_roles.get(tag_name)
        is_written = tag_name in ctx.graph.writers_of
        if not (role == TagRole.INPUT or (tag.external and not is_written)):
            continue
        domain = _extract_value_domain(
            tag_name,
            tag,
            ctx.all_exprs,
            ctx.graph.tags,
            literal_write_domains,
            structural_domains,
            ctx.graph,
        )
        if not domain:
            if tag.choices is not None:
                domain = tuple(sorted(tag.choices.keys()))
            elif tag.min is not None and tag.max is not None:
                range_size = int(tag.max - tag.min + 1)
                if range_size <= 1000:
                    domain = tuple(range(int(tag.min), int(tag.max) + 1))
        if domain:
            first_pass_nd[tag_name] = domain
    discovered = _pilot_sweep_domains(
        ctx.compiled,
        ctx.intractable.tags,
        first_pass_nd,
        ctx.graph,
        dt=ctx.dt,
    )
    if discovered:
        result = _classify_dimensions_from_graph(
            ctx.program,
            ctx.graph,
            ctx.all_exprs,
            scope=ctx.scope,
            project=ctx.project,
            discovered_domains=discovered,
        )
        if isinstance(result, Intractable):
            ctx.intractable = result
        else:
            sd, nd, _comb, da, dp, dk = result
            ctx.stateful_dims = sd
            ctx.nondeterministic_dims = nd
            ctx.done_acc = da
            ctx.done_presets = dp
            ctx.done_kinds = dk
            ctx.intractable = None


def _pass_compile_kernel(ctx: _PassContext) -> None:
    from pyrung.circuitpy.codegen import compile_kernel as _compile_kernel

    if ctx.compiled is None:
        ctx.compiled = _compile_kernel(ctx.program)
    assert ctx.stateful_dims is not None
    ctx.stateful_names = tuple(sorted(ctx.stateful_dims))
    ctx.edge_tag_names = tuple(sorted(ctx.compiled.edge_tags))


def _pass_collect_done_acc_pairs(ctx: _PassContext) -> None:
    ctx.done_acc_info = _collect_done_acc_pairs(ctx.program)


def _pass_find_redundant_absorptions(ctx: _PassContext) -> None:
    assert ctx.graph is not None and ctx.all_exprs is not None
    assert ctx.done_acc_info is not None
    consumed_accs = {
        acc_name
        for acc_name in ctx.done_acc_info.pairs.values()
        if _collect_atoms_for_tag(ctx.all_exprs, acc_name)
        or _has_forbidden_data_read(ctx.program, acc_name)
    }
    ctx.absorptions = _find_redundant_acc_absorptions(
        ctx.program,
        ctx.graph,
        ctx.all_exprs,
        ctx.done_acc_info,
        consumed_accs,
    )
    ctx.synthetic_preset_tags = tuple(sorted(ctx.absorptions.preset_tags))


def _pass_find_threshold_absorptions(ctx: _PassContext) -> None:
    assert ctx.graph is not None and ctx.all_exprs is not None
    ctx.threshold_absorptions = _find_threshold_absorptions(
        ctx.program,
        ctx.graph,
        ctx.all_exprs,
        project=ctx.project,
    )


def _pass_build_event_specs(ctx: _PassContext) -> None:
    assert ctx.stateful_names is not None and ctx.done_acc is not None
    assert ctx.done_kinds is not None and ctx.done_presets is not None
    assert ctx.threshold_absorptions is not None
    sk_done: list[_StateKeyDoneSpec] = []
    d_events: list[_DoneEventSpec] = []
    for index, done_name in enumerate(ctx.stateful_names):
        acc_name = ctx.done_acc.get(done_name)
        if acc_name is None:
            continue
        kind = ctx.done_kinds[done_name]
        sk_done.append(_StateKeyDoneSpec(index=index, acc_name=acc_name, kind=kind))
        preset = ctx.done_presets.get(done_name)
        if preset is not None:
            d_events.append(
                _DoneEventSpec(state_index=index, acc_name=acc_name, kind=kind, preset=preset)
            )
    ctx.state_key_done_specs = tuple(sk_done)
    ctx.done_event_specs = tuple(d_events)

    t_events: list[_ThresholdEventSpec] = []
    for vi, vector in enumerate(ctx.threshold_absorptions.vector_specs):
        for ai, atom in enumerate(vector.atoms):
            t_events.append(
                _ThresholdEventSpec(
                    vector_index=vi,
                    atom_index=ai,
                    acc_name=vector.acc_name,
                    kind=vector.kind,
                    threshold=atom.threshold,
                    form=atom.form,
                )
            )
    ctx.threshold_event_specs = tuple(t_events)


def _pass_collect_edge_exprs(ctx: _PassContext) -> None:
    assert ctx.edge_tag_names is not None
    ctx.edge_tag_exprs = _collect_edge_tag_exprs(ctx.program, ctx.edge_tag_names)


def _pass_discover_memory_keys(ctx: _PassContext) -> None:
    assert ctx.compiled is not None and ctx.absorptions is not None
    pilot = ctx.compiled.create_kernel()
    for name in ctx.absorptions.preset_tags:
        pilot.tags[name] = 1
    pilot.memory["_dt"] = ctx.dt
    for spec in ctx.compiled.block_specs.values():
        pilot.load_block_from_tags(spec)
    ctx.compiled.step_fn(pilot.tags, pilot.blocks, pilot.memory, pilot.prev, ctx.dt)
    excluded_prefixes = ("_dt", "_frac:")
    ctx.memory_key_names = tuple(
        sorted(k for k in pilot.memory if not any(k.startswith(p) for p in excluded_prefixes))
    )


_DEFAULT_PRE_BFS_PASSES: tuple[_PreBFSPass, ...] = (
    _PreBFSPass(
        "build_graph", "Build program dependency graph and collect expressions", _pass_build_graph
    ),
    _PreBFSPass(
        "classify_dimensions",
        "Partition tags into stateful/nondeterministic/combinational",
        _pass_classify_dimensions,
    ),
    _PreBFSPass(
        "pilot_sweep",
        "Discover finite domains for unbounded tags via kernel execution",
        _pass_pilot_sweep,
    ),
    _PreBFSPass(
        "compile_kernel",
        "Compile the replay kernel and derive stateful/edge tag names",
        _pass_compile_kernel,
    ),
    _PreBFSPass(
        "collect_done_acc_pairs",
        "Map Done tags to their accumulator partners",
        _pass_collect_done_acc_pairs,
    ),
    _PreBFSPass(
        "find_redundant_absorptions",
        "Identify accumulators absorbed into Done bit state",
        _pass_find_redundant_absorptions,
    ),
    _PreBFSPass(
        "find_threshold_absorptions",
        "Identify threshold jumping patterns for hidden accumulators",
        _pass_find_threshold_absorptions,
    ),
    _PreBFSPass(
        "build_event_specs",
        "Construct Done and threshold event specifications",
        _pass_build_event_specs,
    ),
    _PreBFSPass(
        "collect_edge_exprs",
        "Build expression map for edge tag compression",
        _pass_collect_edge_exprs,
    ),
    _PreBFSPass(
        "discover_memory_keys",
        "Discover kernel memory keys via pilot scan",
        _pass_discover_memory_keys,
    ),
)


def _run_pre_bfs_pipeline(
    ctx: _PassContext,
    passes: tuple[_PreBFSPass, ...] = _DEFAULT_PRE_BFS_PASSES,
) -> _ExploreContext | Intractable:
    for i, p in enumerate(passes):
        if not p.enabled:
            continue
        p.run(ctx)
        if ctx.intractable is None:
            continue
        if p.name != "classify_dimensions":
            return ctx.intractable
        pilot_sweep_ahead = any(
            later.enabled and later.name == "pilot_sweep" for later in passes[i + 1 :]
        )
        if not pilot_sweep_ahead:
            return ctx.intractable
    return ctx.freeze()
