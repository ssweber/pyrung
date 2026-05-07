"""Configurable pre-BFS pass pipeline for prove."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pdg import TagRole, build_program_graph
from pyrung.core.analysis.simplified import Expr
from pyrung.core.kernel import BlockSpec, CompiledKernel

from . import _ExploreContext
from .absorb import (
    _THRESHOLD_KIND_COMPARISON_ONLY,
    _collect_done_acc_pairs,
    _DoneAccInfo,
    _find_comparison_absorptions,
    _find_redundant_acc_absorptions,
    _find_threshold_absorptions,
    _has_forbidden_data_read,
    _merge_threshold_absorptions,
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
from .elision import _elide_scan_local_stateful_dims
from .events import _DoneEventSpec, _StateKeyDoneSpec, _ThresholdEventSpec
from .expr import _collect_atoms_for_tag, _collect_edge_input_tags, _partition_edge_bearing_inputs
from .inputs import (
    _detect_auto_joint_inputs,
    _detect_exclusive_input_groups,
    _exclusive_input_group_membership,
    _ExclusiveInputGroup,
)
from .kernel import _collect_edge_tag_exprs, _step_compiled_kernel
from .results import Intractable

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.program import Program


def _detect_edge_caveats(
    all_exprs: list[Expr],
    nondeterministic_dims: dict[str, tuple[Any, ...]],
    joint_inputs: tuple[tuple[str, ...], ...],
    program: Any = None,
) -> tuple[str, ...]:
    """Detect external inputs used in edge detection not covered by a joint group."""
    if program is not None:
        edge_inputs = _partition_edge_bearing_inputs(all_exprs, nondeterministic_dims, program)
    else:
        edge_inputs = _collect_edge_input_tags(all_exprs, nondeterministic_dims)
    if not edge_inputs:
        return ()
    grouped: set[str] = set()
    for g in joint_inputs:
        grouped.update(g)
    uncovered = sorted(edge_inputs - grouped)
    if not uncovered:
        return ()
    names = ", ".join(uncovered)
    return (
        f"Simultaneous edge combinations on external inputs [{names}] "
        f"were not explored. These inputs use rise()/fall() but are not "
        f"covered by a joint input declaration.",
    )


def _narrow_indirect_block_specs(
    specs: dict[str, BlockSpec],
    compiled: CompiledKernel,
    graph: ProgramGraph,
    stateful_dims: dict[str, tuple[Any, ...]],
    nondeterministic_dims: dict[str, tuple[Any, ...]],
) -> dict[str, BlockSpec]:
    """Narrow block specs for indirect blocks using known pointer domains.

    For each indirect block, only sync the tags reachable through the pointer
    domain plus any statically accessed addresses.  The array layout is
    unchanged — ``tag_indices`` maps each narrowed tag to its original
    ``addr - start`` position.
    """
    if not compiled.indirect_block_info:
        return specs

    all_domains: dict[str, tuple[Any, ...]] = dict(stateful_dims)
    all_domains.update(nondeterministic_dims)

    block_domains: dict[tuple[str, int, int], set[int]] = {}
    for ptr_name, (block_name, start, end) in graph.pointer_tags.items():
        domain = all_domains.get(ptr_name)
        if domain is None:
            continue
        key = (block_name, start, end)
        block_domains.setdefault(key, set()).update(
            int(v) for v in domain if isinstance(v, (int, float)) and start <= int(v) <= end
        )

    result = dict(specs)

    for symbol, (block_name, start, end, static_addrs) in compiled.indirect_block_info.items():
        spec = specs.get(symbol)
        if spec is None:
            continue
        domain = block_domains.get((block_name, start, end))
        if domain is None:
            continue

        needed_addrs = sorted(domain | set(static_addrs))
        if len(needed_addrs) >= spec.size:
            continue

        narrowed_tag_names = tuple(spec.tag_names[addr - start] for addr in needed_addrs)
        narrowed_tag_indices = tuple(addr - start for addr in needed_addrs)

        result[symbol] = BlockSpec(
            symbol=symbol,
            size=spec.size,
            default=spec.default,
            tag_type=spec.tag_type,
            tag_names=narrowed_tag_names,
            tag_indices=narrowed_tag_indices,
        )

    return result


@dataclass
class _PassContext:
    """Mutable accumulator built up by pre-BFS passes."""

    program: Program
    scope: list[str] | None
    project: tuple[str, ...] | None
    extra_exprs: list[Expr] | None
    dt: float
    compiled: CompiledKernel | None
    joint_inputs: tuple[tuple[str, ...], ...] = ()
    exclusive_inputs: tuple[tuple[str, ...], ...] = ()
    progress_info: Callable[[str], None] | None = None
    progress_prefix: Callable[[], str] | None = None

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
    receive_dest_names: frozenset[str] = frozenset()

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
        exclusive_input_groups = _detect_exclusive_input_groups(
            self.program,
            self.graph,
            self.nondeterministic_dims,
            project=self.project,
            extra_exprs=self.extra_exprs,
        )
        auto_joint_inputs = _detect_auto_joint_inputs(self.program, self.nondeterministic_dims)
        if self.exclusive_inputs:
            from .inputs import _canonical_assignments_for_members

            auto_members: set[str] = set()
            for g in exclusive_input_groups:
                auto_members.update(g.members)
            user_groups: list[_ExclusiveInputGroup] = []
            for members_tuple in self.exclusive_inputs:
                if any(m in auto_members for m in members_tuple):
                    continue
                user_groups.append(
                    _ExclusiveInputGroup(
                        target_name="",
                        members=tuple(sorted(members_tuple)),
                        canonical_assignments=_canonical_assignments_for_members(
                            tuple(sorted(members_tuple))
                        ),
                    )
                )
            if user_groups:
                exclusive_input_groups = exclusive_input_groups + tuple(user_groups)
        edge_bearing = _partition_edge_bearing_inputs(
            self.all_exprs, self.nondeterministic_dims, self.program
        )
        projected_nd = frozenset(self.project or ()) & frozenset(self.nondeterministic_dims)
        nd_in_key = edge_bearing | projected_nd
        free = frozenset(self.nondeterministic_dims) - nd_in_key
        combined_joint_inputs = tuple(
            sorted({tuple(sorted(g)) for g in auto_joint_inputs + self.joint_inputs})
        )
        caveats = _detect_edge_caveats(
            self.all_exprs,
            self.nondeterministic_dims,
            combined_joint_inputs,
            program=self.program,
        )
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
            dt=self.dt,
            edge_tag_exprs=self.edge_tag_exprs or {},
            synthetic_preset_tags=self.synthetic_preset_tags or (),
            nondeterministic_names=tuple(sorted(nd_in_key)),
            free_input_names=free,
            always_live_input_names=tuple(
                sorted(set(self.project or ()) & set(self.nondeterministic_dims))
            ),
            exclusive_input_groups=exclusive_input_groups,
            exclusive_input_group_by_member=_exclusive_input_group_membership(
                exclusive_input_groups
            ),
            joint_inputs=combined_joint_inputs,
            caveats=caveats,
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
    exclusive_input_grouping: bool = True
    edge_compression: bool = True
    hidden_event_jumping: bool = True
    pending_settlement: bool = True

    @property
    def active_optimizations(self) -> tuple[str, ...]:
        names: list[str] = []
        if self.live_input_pruning:
            names.append("live_input_pruning")
        if self.exclusive_input_grouping:
            names.append("exclusive_input_grouping")
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
    ctx.receive_dest_names = frozenset(_collect_receive_dest_names(ctx.program))


def _pass_classify_dimensions(ctx: _PassContext) -> None:
    assert ctx.graph is not None and ctx.all_exprs is not None
    result = _classify_dimensions_from_graph(
        ctx.program,
        ctx.graph,
        ctx.all_exprs,
        scope=ctx.scope,
        project=ctx.project,
        receive_dest_names=ctx.receive_dest_names,
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
        ctx.compiled = _compile_kernel(ctx.program, blockless=True)
    first_pass_nd: dict[str, tuple[Any, ...]] = {}
    for tag_name, tag in ctx.graph.tags.items():
        role = ctx.graph.tag_roles.get(tag_name)
        is_written = tag_name in ctx.graph.writers_of
        is_nd = (
            role == TagRole.INPUT
            or (tag.external and not is_written)
            or tag_name in ctx.receive_dest_names
        )
        if not is_nd:
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
            receive_dest_names=ctx.receive_dest_names,
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


def _collect_receive_dest_names(program: Program) -> set[str]:
    from pyrung.core.instruction.send_receive._core import ModbusReceiveInstruction
    from pyrung.core.validation._common import walk_instructions

    names: set[str] = set()
    for instr in walk_instructions(program):
        if not isinstance(instr, ModbusReceiveInstruction):
            continue
        dest = instr.dest
        if hasattr(dest, "name"):
            names.add(dest.name)
        elif hasattr(dest, "tags"):
            for tag in dest.tags():
                names.add(tag.name)
    return names


def _pass_diagnose_unwritten_tags(ctx: _PassContext) -> None:
    assert ctx.graph is not None
    if ctx.stateful_dims is None or ctx.nondeterministic_dims is None:
        return

    never_written: list[str] = []
    for tag_name, tag in sorted(ctx.graph.tags.items()):
        if tag_name in ctx.graph.writers_of:
            continue
        if tag.external or tag.readonly:
            continue
        if tag_name.startswith("fault."):
            continue
        never_written.append(tag_name)

    if never_written and ctx.progress_info is not None:
        names = ", ".join(never_written)
        ctx.progress_info(
            f"diagnostic | {len(never_written)} tag(s) are never written: [{names}]. "
            f"Each is either: (1) an external input — add external=True, "
            f"(2) a configuration constant — add readonly=True, "
            f"or (3) a bug — the tag is declared but never wired to any instruction."
        )

    missing_external = sorted(
        name
        for name in ctx.receive_dest_names
        if name in ctx.graph.tags and not ctx.graph.tags[name].external
    )

    if missing_external and ctx.progress_info is not None:
        names = ", ".join(missing_external)
        ctx.progress_info(
            f"diagnostic | {len(missing_external)} receive() destination tag(s) "
            f"missing external=True: [{names}]. "
            f"Receive destinations hold data from outside the program; "
            f"consider adding external=True to their declarations."
        )


def _pass_elide_scan_local_state(ctx: _PassContext) -> None:
    from pyrung.circuitpy.codegen import compile_kernel as _compile_kernel

    assert ctx.graph is not None
    assert ctx.stateful_dims is not None and ctx.nondeterministic_dims is not None
    if ctx.compiled is None:
        ctx.compiled = _compile_kernel(ctx.program, blockless=True)
    ctx.stateful_dims = _elide_scan_local_stateful_dims(
        ctx.program,
        ctx.graph,
        ctx.stateful_dims,
        ctx.nondeterministic_dims,
        compiled=ctx.compiled,
        progress=ctx.progress_info,
        progress_prefix=ctx.progress_prefix,
    )


def _pass_compile_kernel(ctx: _PassContext) -> None:
    from pyrung.circuitpy.codegen import compile_kernel as _compile_kernel

    if ctx.compiled is None:
        ctx.compiled = _compile_kernel(ctx.program, blockless=True)
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
    literal_write_domains = _collect_literal_write_domains(ctx.program, ctx.graph.tags)
    structural_domains = _collect_structural_domains(
        ctx.program,
        ctx.graph,
        ctx.all_exprs,
        literal_write_domains,
    )

    threshold_absorptions = _find_threshold_absorptions(
        ctx.program,
        ctx.graph,
        ctx.all_exprs,
        project=ctx.project,
    )
    comparison_absorptions = _find_comparison_absorptions(
        ctx.program,
        ctx.graph,
        ctx.all_exprs,
        structural_domains,
        project=ctx.project,
    )
    ctx.threshold_absorptions = _merge_threshold_absorptions(
        threshold_absorptions,
        comparison_absorptions,
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
        if vector.kind == _THRESHOLD_KIND_COMPARISON_ONLY:
            continue
        for ai, atom in enumerate(vector.atoms):
            t_events.append(
                _ThresholdEventSpec(
                    vector_index=vi,
                    atom_index=ai,
                    acc_name=vector.acc_name,
                    kind=vector.kind,
                    threshold=atom.threshold,
                    form=atom.form,
                    mode=atom.mode,
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
    _step_compiled_kernel(ctx.compiled, pilot, dt=ctx.dt)
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
        "diagnose_unwritten_tags",
        "Surface never-written tags as user diagnostics",
        _pass_diagnose_unwritten_tags,
    ),
    _PreBFSPass(
        "elide_scan_local_state",
        "Elide scan-local state that is provably irrelevant across scans",
        _pass_elide_scan_local_state,
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
