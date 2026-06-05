"""Configurable pre-BFS pass pipeline for prove."""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pdg import TagRole, build_program_graph
from pyrung.core.analysis.simplified import Expr
from pyrung.core.kernel import (
    PROVE_EFFECTIVE_PRESET_PREFIX,
    BlockSpec,
    CompiledKernel,
    prove_effective_preset_key,
)
from pyrung.core.system_points import SYSTEM_TAGS_BY_NAME
from pyrung.core.tag import TagType

from . import _ExploreContext
from .absorb import (
    _DONE_KIND_COUNT_DOWN,
    _DONE_KIND_COUNT_UP,
    _DONE_KIND_OFF_DELAY,
    _DONE_KIND_ON_DELAY,
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
    _pilot_sweep_domains,
)
from .elision import _elide_scan_local_stateful_dims
from .events import _DoneEventSpec, _StateKeyDoneSpec, _ThresholdEventSpec
from .expr import _collect_atoms_for_tag, _partition_edge_bearing_inputs
from .inputs import (
    _detect_auto_joint_inputs,
    _detect_exclusive_input_groups,
    _exclusive_input_group_membership,
    _ExclusiveInputGroup,
    _observed_tags,
)
from .kernel import _collect_edge_tag_exprs, _step_compiled_kernel
from .results import Decision, Intractable, Journal, TagEntry

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.program import Program


def _infer_domain_source(
    tag_name: str,
    domain: tuple[Any, ...],
    graph: ProgramGraph,
) -> str:
    tag = graph.tags.get(tag_name)
    if tag is None:
        return "unknown"
    from pyrung.core.tag import TagType

    if tag.type is TagType.BOOL and domain == (False, True):
        return "bool"
    if tag.choices is not None and set(domain) <= set(tag.choices.keys()):
        return "choices"
    if tag.min is not None and tag.max is not None:
        expected = tuple(range(int(tag.min), int(tag.max) + 1))
        if domain == expected:
            return "min_max"
    from .results import PENDING

    if domain == (False, PENDING, True):
        return "done_acc_tri_state"
    if len(domain) == 1 and domain[0] == tag.default:
        return "default_only"
    return "expression_partition"


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


class _JournalBuilder:
    """Accumulates per-tag decisions during the pass pipeline."""

    def __init__(self) -> None:
        self._decisions: dict[str, list[Decision]] = {}
        self._notes: list[str] = []

    def record(self, tag_name: str, decision: Decision) -> None:
        self._decisions.setdefault(tag_name, []).append(decision)

    def add_note(self, text: str) -> None:
        self._notes.append(text)

    def freeze(
        self,
        graph_tags: dict[str, Any],
        exclusions: dict[str, str],
        stateful_dims: dict[str, tuple[Any, ...]],
        nondeterministic_dims: dict[str, tuple[Any, ...]],
        combinational_tags: frozenset[str],
        elided_tags: dict[str, str] | None,
        edge_bearing: frozenset[str],
        free: frozenset[str],
    ) -> Journal:
        from types import MappingProxyType

        entries: dict[str, TagEntry] = {}
        for tag_name in graph_tags:
            decisions = tuple(self._decisions.get(tag_name, ()))
            domain: tuple[Any, ...] | None = None
            domain_source: str | None = None

            if tag_name in stateful_dims:
                outcome = "stateful"
                domain = stateful_dims[tag_name]
            elif tag_name in nondeterministic_dims:
                if tag_name in edge_bearing:
                    outcome = "nondeterministic:edge_bearing"
                elif tag_name in free:
                    outcome = "nondeterministic:free"
                else:
                    outcome = "nondeterministic"
                domain = nondeterministic_dims[tag_name]
            elif tag_name in combinational_tags:
                outcome = "combinational"
            elif elided_tags is not None and tag_name in elided_tags:
                outcome = f"elided:{elided_tags[tag_name]}"
            elif tag_name in exclusions:
                outcome = f"excluded:{exclusions[tag_name]}"
            else:
                outcome = "unclassified"

            for d in decisions:
                if d.kind == "domain":
                    domain_source = d.reason

            entries[tag_name] = TagEntry(
                name=tag_name,
                outcome=outcome,
                domain=domain,
                domain_source=domain_source,
                decisions=decisions,
            )

        return Journal(
            tags=MappingProxyType(entries),
            notes=tuple(self._notes),
        )


@dataclass(frozen=True)
class _PipelineCache:
    """Reusable classification/absorption results from a prior pipeline run.

    The cached results come from a broader scope (the outer how() target).
    Per-waypoint rebuilds filter them to the waypoint's upstream cone,
    skipping the expensive kernel scans and fixed-point domain propagation.
    """

    stateful_dims: dict[str, tuple[Any, ...]]
    nondeterministic_dims: dict[str, tuple[Any, ...]]
    combinational_tags: frozenset[str]
    done_acc: dict[str, str]
    done_presets: dict[str, int]
    done_kinds: dict[str, str]
    consumed_accs: frozenset[str]
    unclassified_written: frozenset[str]
    heuristic_seeded_tags: frozenset[str]
    threshold_absorptions: _ThresholdAbsorptions


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
    journal_builder: _JournalBuilder | None = None
    split_at_tags: dict[str, tuple[Any, ...]] | None = None
    scope_snapshot: bool = True
    initial_state: dict[str, Any] | None = None
    pipeline_cache: _PipelineCache | None = None
    # how()-only: restrict the varied nondeterministic inputs to those in *scope*
    # (the narrowed per-step cone), holding all others at their initial value.
    # Avoids varying inputs that only reach the scope via a shared derived tag's
    # other writers (e.g. a sub-state's level inputs reached through fill_subStatus)
    # — don't-cares for this waypoint's transition that otherwise blow up branching.
    # Unsound for always()/never() (would skip reachable states); safe for how()
    # where each path is replay-verified.
    restrict_inputs_to_scope: bool = False

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
    drum_event_meta: dict[str, Any] | None = None
    demotable_edge_tag_names: tuple[str, ...] | None = None
    _combinational_tags: frozenset[str] | None = None
    _consumed_accs: frozenset[str] = frozenset()
    _elided_tags: dict[str, str] | None = None
    _functional_dep_projections: dict[str, tuple[str, int | float]] | None = None
    _init_constant_projections: dict[str, tuple[str, Any]] | None = None
    _exclusions: dict[str, str] | None = None
    _unclassified_written: frozenset[str] = frozenset()
    _pending_infeasible_tags: list[str] = field(default_factory=list)
    _pending_infeasible_hints: list[str] = field(default_factory=list)
    _heuristic_seeded_tags: frozenset[str] = frozenset()
    _elision_infeasible_delta: list[str] = field(default_factory=list)

    def extract_cache(self) -> _PipelineCache | None:
        if self.stateful_dims is None or self.nondeterministic_dims is None:
            return None
        if self.threshold_absorptions is None:
            return None
        return _PipelineCache(
            stateful_dims=dict(self.stateful_dims),
            nondeterministic_dims=dict(self.nondeterministic_dims),
            combinational_tags=self._combinational_tags or frozenset(),
            done_acc=dict(self.done_acc or {}),
            done_presets=dict(self.done_presets or {}),
            done_kinds=dict(self.done_kinds or {}),
            consumed_accs=self._consumed_accs,
            unclassified_written=self._unclassified_written,
            heuristic_seeded_tags=self._heuristic_seeded_tags,
            threshold_absorptions=self.threshold_absorptions,
        )

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
        grouped: set[str] = set()
        for g in combined_joint_inputs:
            grouped.update(g)
        uncovered = sorted(edge_bearing - grouped)
        if uncovered:
            names = ", ".join(uncovered)
            caveats: tuple[str, ...] = (
                f"Simultaneous edge combinations on external inputs [{names}] "
                f"were not explored. These inputs use rise()/fall() but are not "
                f"covered by a joint input declaration.",
            )
        else:
            caveats = ()

        if self._consumed_accs and self.done_acc_info is not None:
            for acc_name in sorted(self._consumed_accs):
                if acc_name not in self.stateful_dims:
                    continue
                domain = self.stateful_dims[acc_name]
                for done_name, paired_acc in self.done_acc_info.pairs.items():
                    if paired_acc != acc_name:
                        continue
                    preset = self.done_acc_info.presets.get(done_name)
                    if preset is None:
                        continue
                    kind = self.done_acc_info.kinds.get(done_name, "")
                    if kind in {_DONE_KIND_COUNT_UP, _DONE_KIND_ON_DELAY}:
                        if max(domain) < preset:
                            caveats = (
                                *caveats,
                                f"Consumed accumulator {acc_name} has domain max "
                                f"{max(domain)} < preset {preset} for {done_name}. "
                                f"BFS tracks concrete values so this is diagnostic only.",
                            )
                    elif kind in {_DONE_KIND_COUNT_DOWN, _DONE_KIND_OFF_DELAY}:
                        if min(domain) > -preset:
                            caveats = (
                                *caveats,
                                f"Consumed accumulator {acc_name} has domain min "
                                f"{min(domain)} > -{preset} for {done_name}. "
                                f"BFS tracks concrete values so this is diagnostic only.",
                            )

        if self._heuristic_seeded_tags:
            names = ", ".join(sorted(self._heuristic_seeded_tags))
            caveats = (
                *caveats,
                f"Heuristic domains used for [{names}] — "
                f"results may be incomplete (unsound domain seeding).",
            )

        journal: Journal | None = None
        if self.journal_builder is not None:
            for tag_name in nd_in_key:
                self.journal_builder.record(
                    tag_name,
                    Decision(
                        "freeze",
                        "input_partition",
                        "edge_bearing",
                        "previous-scan value affects behavior",
                    ),
                )
            for tag_name in free:
                self.journal_builder.record(
                    tag_name,
                    Decision(
                        "freeze",
                        "input_partition",
                        "free",
                        "current value doesn't constrain future behavior",
                    ),
                )
            for group in exclusive_input_groups:
                for member in group.members:
                    self.journal_builder.record(
                        member,
                        Decision(
                            "freeze",
                            "exclusive_group",
                            "grouped",
                            f"exclusive input group targeting {group.target_name}",
                            detail=(("members", group.members),),
                        ),
                    )
            journal = self.journal_builder.freeze(
                graph_tags=self.graph.tags,
                exclusions=self._exclusions or {},
                stateful_dims=self.stateful_dims,
                nondeterministic_dims=self.nondeterministic_dims,
                combinational_tags=self._combinational_tags or frozenset(),
                elided_tags=self._elided_tags,
                edge_bearing=edge_bearing,
                free=free,
            )

        from .independence import _build_independence_relation, _partition_free_inputs

        # Inputs the *property*/projection reads directly (not via the program's
        # data flow).  Free-input factoring composes only WRITE deltas, so a
        # factored input's own value never reaches the merged state — a property
        # like ``Or(~A, ~B)`` would only ever be evaluated at the base (default)
        # input values, silently missing the coupled (A, B) corner.  Excluding
        # observed ND inputs from the partition routes them through the normal
        # cross-product enumeration, which sets kernel.tags before stepping.
        observed_nd_inputs = _observed_tags(
            project=self.project, extra_exprs=self.extra_exprs
        ) & set(self.nondeterministic_dims)

        _split_names = frozenset(self.split_at_tags) if self.split_at_tags else frozenset()
        independence_relation = _build_independence_relation(
            self.graph,
            self.nondeterministic_dims,
            exclusive_input_groups,
            tuple(sorted(nd_in_key)),
            free,
            split_tags=_split_names,
        )

        free_input_factoring = _partition_free_inputs(
            independence_relation,
            free - observed_nd_inputs,
            split_tags=_split_names,
        )

        # Mutable write-set: every tag that can differ between two reachable
        # snapshots. Any tag outside it is a write-once constant identical in
        # every reachable state (e.g. read-only indirect lookup tables), so
        # kernel snapshots need not capture it. Sources:
        #   - graph.writers_of — step_fn writes (conservatively covers indirect/
        #     full-block and implicit fault writes)
        #   - nondeterministic_dims — inputs BFS assigns directly
        #   - stateful_dims, edge_tag_names, synthetic_preset_tags
        #   - accumulators / abstract thresholds / dynamic presets that the
        #     hidden-event scheduler materializes via kernel.tags[...] *outside*
        #     step_fn (so writers_of misses them — see events.py)
        #   - system tags (fault.*, rtc.*, ...) the kernel runtime sets each
        #     scan, also outside writers_of
        mutable_tag_names: frozenset[str] | None = None
        if self.scope_snapshot:
            mutable: set[str] = set(self.graph.writers_of)
            mutable.update(SYSTEM_TAGS_BY_NAME)
            mutable.update(self.nondeterministic_dims)
            mutable.update(self.stateful_dims)
            mutable.update(self.edge_tag_names)
            mutable.update(self.synthetic_preset_tags or ())
            for sk_spec in self.state_key_done_specs:
                mutable.add(sk_spec.acc_name)
            for done_spec in self.done_event_specs:
                mutable.add(done_spec.acc_name)
                if isinstance(done_spec.preset, str):
                    mutable.add(done_spec.preset)
            for thr_spec in self.threshold_event_specs:
                mutable.add(thr_spec.acc_name)
                if isinstance(thr_spec.threshold, str):
                    mutable.add(thr_spec.threshold)
            for vec_spec in self.threshold_absorptions.vector_specs:
                mutable.add(vec_spec.acc_name)
                for atom in vec_spec.atoms:
                    if isinstance(atom.threshold, str):
                        mutable.add(atom.threshold)
            # Text fan-out: copy/copy_convert to a Char tag can create new
            # tag keys at runtime (e.g. dest=Ch0 writes Ch1, Ch2, …) that
            # don't exist in any static tag set.  Snapshot scoping must
            # capture/restore these dynamic keys — see _snapshot_kernel.
            mutable_tag_names = frozenset(mutable)

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
            demoted_edge_names=tuple(sorted(self.demotable_edge_tag_names or ())),
            synthetic_preset_tags=self.synthetic_preset_tags or (),
            nondeterministic_names=tuple(sorted(nd_in_key)),
            free_input_names=free,
            always_live_input_names=tuple(
                sorted(
                    observed_nd_inputs
                    | _collect_stateful_upstream_nd_names(
                        self.graph, self.stateful_dims, self.nondeterministic_dims
                    )
                )
            ),
            exclusive_input_groups=exclusive_input_groups,
            exclusive_input_group_by_member=_exclusive_input_group_membership(
                exclusive_input_groups
            ),
            joint_inputs=combined_joint_inputs,
            caveats=caveats,
            journal=journal,
            drum_event_meta=self.drum_event_meta or {},
            independence_relation=independence_relation,
            free_input_factoring=free_input_factoring,
            mutable_tag_names=mutable_tag_names,
            base_tag_keys=frozenset(self.compiled._tag_template)
            if mutable_tag_names is not None
            else None,
        )


@dataclass(frozen=True)
class _PreBFSPass:
    name: str
    description: str
    run: Callable[[_PassContext], None]
    enabled: bool = True
    requires: frozenset[str] = frozenset()
    provides: frozenset[str] = frozenset()


def _validate_pass_dag(passes: tuple[_PreBFSPass, ...]) -> None:
    available: set[str] = set()
    for p in passes:
        if not p.enabled:
            continue
        missing = p.requires - available
        if missing:
            raise ValueError(
                f"Pass {p.name!r} requires {sorted(missing)} but only {sorted(available)} available"
            )
        available |= p.provides


@dataclass(frozen=True)
class _BFSConfig:
    """Enable/disable flags for BFS-interleaved optimizations."""

    live_input_pruning: bool = True
    exclusive_input_grouping: bool = True
    edge_compression: bool = True
    hidden_event_jumping: bool = True
    pending_settlement: bool = True
    free_input_factoring: bool = True

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
        if self.free_input_factoring:
            names.append("free_input_factoring")
        return tuple(names)


_DEFAULT_BFS_CONFIG = _BFSConfig()


# Optimizations whose effect is a sound search-space *reduction* — disabling
# any of them makes BFS explore an equal or larger set, so an all-off-of-these
# config is still a sound ground truth. The remaining two optimizations
# (hidden_event_jumping, pending_settlement) instead *extend reachability per
# unit of depth_budget*: disabling them under a finite budget under-approximates
# the reachable set (a timer never reaches its preset), so they must stay
# enabled in any config used as a ground-truth baseline.
#
# scope_snapshot is representation-level: disabling it explores the *same* set
# (just with full snapshots), so it satisfies "equal or larger". Listing it here
# means sound_baseline() — and thus the --prove-agreement re-run — uses full
# snapshots, keeping the agreement oracle a genuine scoped-vs-full check.
_REDUCTION_OPTIMIZATIONS: frozenset[str] = frozenset(
    {
        "traced_elision",
        "accumulator_absorption",
        "functional_dependency_projection",
        "init_constant_projection",
        "live_input_pruning",
        "exclusive_input_grouping",
        "edge_compression",
        "free_input_factoring",
        "scope_snapshot",
    }
)


@dataclass(frozen=True)
class _OptConfig:
    """Per-optimization enable flags spanning pre-BFS passes and BFS-interleaved opts.

    ``_OptConfig()`` is the all-on default and reproduces current production
    behavior exactly. ``_OptConfig.sound_baseline()`` disables every
    soundness-optional reduction while keeping the reach-extending
    optimizations on — that is the maximally-reduced config still usable as a
    ground truth. ``_OptConfig.all_off()`` disables everything; it is a valid
    config to *test* but is NOT a sound baseline (see _REDUCTION_OPTIMIZATIONS).

    Each field is one independently-toggleable optimization. This lets the
    soundness fuzzer test arbitrary *subsets* against the baseline —
    interaction bugs between two optimizations are missed by an all-on-only
    check but caught by a subset that isolates the interacting pair.
    """

    # pre-BFS passes
    traced_elision: bool = True
    accumulator_absorption: bool = True
    functional_dependency_projection: bool = True
    init_constant_projection: bool = True
    heuristic_domain_seeding: bool = False
    validate_declared_bounds: bool = True
    # BFS-interleaved (mirror _BFSConfig)
    live_input_pruning: bool = True
    exclusive_input_grouping: bool = True
    edge_compression: bool = True
    hidden_event_jumping: bool = True
    pending_settlement: bool = True
    free_input_factoring: bool = True
    # representation-level (does not change the explored state set)
    scope_snapshot: bool = True

    @classmethod
    def all_off(cls) -> _OptConfig:
        return cls(**dict.fromkeys(cls.__dataclass_fields__, False))

    @classmethod
    def sound_baseline(cls) -> _OptConfig:
        """Maximally-reduced config that is still a sound ground truth.

        Every soundness-optional reduction is disabled; the reach-extending
        optimizations stay enabled because disabling them under a finite
        depth_budget under-approximates reachability.
        """
        return cls(**{f: f not in _REDUCTION_OPTIMIZATIONS for f in cls.__dataclass_fields__})

    def subset(self, names: Iterable[str]) -> _OptConfig:
        """Return a config with exactly *names* enabled, all others off."""
        names = set(names)
        return _OptConfig(**{f: f in names for f in _OptConfig.__dataclass_fields__})

    @property
    def bfs_config(self) -> _BFSConfig:
        """Project the BFS-interleaved flags into a _BFSConfig for _bfs_explore."""
        return _BFSConfig(
            live_input_pruning=self.live_input_pruning,
            exclusive_input_grouping=self.exclusive_input_grouping,
            edge_compression=self.edge_compression,
            hidden_event_jumping=self.hidden_event_jumping,
            pending_settlement=self.pending_settlement,
            free_input_factoring=self.free_input_factoring,
        )

    @property
    def active_optimizations(self) -> tuple[str, ...]:
        return tuple(f for f in self.__dataclass_fields__ if getattr(self, f))


_DEFAULT_OPT_CONFIG = _OptConfig()


def _pass_build_graph(ctx: _PassContext) -> None:
    ctx.graph = build_program_graph(ctx.program)
    ctx.all_exprs = _collect_all_exprs(ctx.program, ctx.graph, scope=ctx.scope)
    if ctx.extra_exprs:
        ctx.all_exprs = ctx.all_exprs + ctx.extra_exprs
    ctx.receive_dest_names = frozenset(_collect_receive_dest_names(ctx.program))


def _scope_upstream(ctx: _PassContext) -> frozenset[str] | None:
    """Compute the upstream tag cone for the current scope."""
    if ctx.scope is None or ctx.graph is None:
        return None
    upstream: set[str] = set(ctx.scope)
    for tag_name in ctx.scope:
        upstream.update(ctx.graph.upstream_slice(tag_name))
    return frozenset(upstream)


def _apply_classification_cache(ctx: _PassContext) -> None:
    """Apply cached classification results, filtered to the current scope."""
    cache = ctx.pipeline_cache
    assert cache is not None
    upstream = _scope_upstream(ctx)
    if upstream is not None:
        ctx.stateful_dims = {k: v for k, v in cache.stateful_dims.items() if k in upstream}
        nd = {k: v for k, v in cache.nondeterministic_dims.items() if k in upstream}
        # how()-only: drop inputs that reach the scope only through a shared
        # derived tag's out-of-cone writers — keep only inputs in the cone itself.
        if ctx.restrict_inputs_to_scope and ctx.scope is not None:
            scope_set = set(ctx.scope)
            nd = {k: v for k, v in nd.items() if k in scope_set}
        ctx.nondeterministic_dims = nd
    else:
        ctx.stateful_dims = dict(cache.stateful_dims)
        ctx.nondeterministic_dims = dict(cache.nondeterministic_dims)
    ctx._combinational_tags = cache.combinational_tags
    ctx.done_acc = dict(cache.done_acc)
    ctx.done_presets = dict(cache.done_presets)
    ctx.done_kinds = dict(cache.done_kinds)
    ctx._consumed_accs = cache.consumed_accs
    ctx._unclassified_written = cache.unclassified_written
    ctx._heuristic_seeded_tags = cache.heuristic_seeded_tags
    logger.info(
        "classify_dimensions: using cached results (%d stateful, %d ND)",
        len(ctx.stateful_dims),
        len(ctx.nondeterministic_dims),
    )


def _pass_classify_dimensions(ctx: _PassContext) -> None:
    if ctx.pipeline_cache is not None:
        _apply_classification_cache(ctx)
        return
    assert ctx.graph is not None and ctx.all_exprs is not None
    exclusions: dict[str, str] | None = {} if ctx.journal_builder is not None else None
    unclassified: set[str] = set()
    result = _classify_dimensions_from_graph(
        ctx.program,
        ctx.graph,
        ctx.all_exprs,
        scope=ctx.scope,
        project=ctx.project,
        receive_dest_names=ctx.receive_dest_names,
        exclusions=exclusions,
        unclassified=unclassified,
    )
    if isinstance(result, Intractable):
        ctx._pending_infeasible_tags.extend(result.tags)
        ctx._pending_infeasible_hints.extend(result.hints)
        ctx._unclassified_written = frozenset(unclassified)
        if result._debug_context is not None:
            sd, nd, _comb, da, dp, dk = result._debug_context
            ctx.stateful_dims = sd
            ctx.nondeterministic_dims = nd
            ctx._combinational_tags = _comb
            ctx.done_acc = da
            ctx.done_presets = dp
            ctx.done_kinds = dk
            all_done_accs = set(_collect_done_acc_pairs(ctx.program).pairs.values())
            non_consumed = set(da.values())
            ctx._consumed_accs = frozenset((all_done_accs - non_consumed) & set(sd))
        if ctx.journal_builder is not None:
            for tag_name in result.tags:
                ctx.journal_builder.record(
                    tag_name,
                    Decision("classify_dimensions", "classification", "infeasible", result.reason),
                )
            if exclusions:
                ctx._exclusions = exclusions
        return
    sd, nd, _comb, da, dp, dk = result
    ctx.stateful_dims = sd
    ctx.nondeterministic_dims = nd
    ctx._combinational_tags = _comb
    ctx.done_acc = da
    ctx.done_presets = dp
    ctx.done_kinds = dk
    ctx._unclassified_written = frozenset(unclassified)
    all_done_accs = set(_collect_done_acc_pairs(ctx.program).pairs.values())
    non_consumed = set(da.values())
    ctx._consumed_accs = frozenset((all_done_accs - non_consumed) & set(sd))
    if ctx.journal_builder is not None:
        assert exclusions is not None
        ctx._exclusions = exclusions
        for tag_name, domain in sd.items():
            source = _infer_domain_source(tag_name, domain, ctx.graph)
            ctx.journal_builder.record(
                tag_name,
                Decision("classify_dimensions", "classification", "stateful", "cross-scan state"),
            )
            ctx.journal_builder.record(
                tag_name,
                Decision("classify_dimensions", "domain", source, source),
            )
        for tag_name, domain in nd.items():
            source = _infer_domain_source(tag_name, domain, ctx.graph)
            ctx.journal_builder.record(
                tag_name,
                Decision(
                    "classify_dimensions", "classification", "nondeterministic", "external input"
                ),
            )
            ctx.journal_builder.record(
                tag_name,
                Decision("classify_dimensions", "domain", source, source),
            )
        for tag_name in _comb:
            ctx.journal_builder.record(
                tag_name,
                Decision(
                    "classify_dimensions",
                    "classification",
                    "combinational",
                    "no cross-scan readers",
                ),
            )
        for tag_name, reason in exclusions.items():
            ctx.journal_builder.record(
                tag_name,
                Decision("classify_dimensions", "exclusion", "excluded", reason),
            )


def _pass_validate_declared_bounds(ctx: _PassContext) -> None:
    """Validate that the kernel respects user-declared min/max/choices.

    Does NOT contribute to domain inference.  Tags without static domains
    stay infeasible → Intractable.
    """
    from pyrung.circuitpy.codegen import compile_kernel as _compile_kernel
    from pyrung.core.bounds import build_constraint_index, check_bounds
    from pyrung.core.tag import TagType

    assert ctx.graph is not None and ctx.all_exprs is not None

    constraints = build_constraint_index(ctx.graph.tags)
    written_constrained = [name for name in constraints if name in ctx.graph.writers_of]
    if not written_constrained:
        return

    if ctx.compiled is None:
        ctx.compiled = _compile_kernel(ctx.program, blockless=True, proof_metadata=True)

    first_pass_nd: dict[str, tuple[Any, ...]] = {}
    for tag_name, tag in ctx.graph.tags.items():
        role = ctx.graph.tag_roles.get(tag_name)
        is_written = tag_name in ctx.graph.writers_of
        is_nd = role == TagRole.INPUT or not is_written or tag_name in ctx.receive_dest_names
        if not is_nd:
            continue
        # Use declared bounds (full range) rather than expression partition,
        # so the kernel is exercised across the entire declared input space.
        domain: tuple[Any, ...] | None = None
        if tag.choices is not None:
            domain = tuple(sorted(tag.choices.keys()))
        elif tag.min is not None and tag.max is not None:
            if int(tag.min) != tag.min or int(tag.max) != tag.max:
                domain = (tag.min, tag.max)
            else:
                range_size = int(tag.max - tag.min + 1)
                if range_size <= 1000:
                    domain = tuple(range(int(tag.min), int(tag.max) + 1))
        if not domain and tag.type is TagType.BOOL:
            domain = (False, True)
        if domain:
            first_pass_nd[tag_name] = domain

    observed = _pilot_sweep_domains(
        ctx.compiled,
        written_constrained,
        first_pass_nd,
        ctx.graph,
        dt=ctx.dt,
    )
    if not observed:
        return

    all_violations: list[str] = []
    for name, vals in observed.items():
        constraint = constraints.get(name)
        if constraint is None:
            continue
        for v in vals:
            violations = check_bounds({name: v}, constraints)
            if violations:
                viol = violations[name]
                all_violations.append(str(viol))

    if all_violations:
        msg = "Kernel produces values that violate declared bounds:\n" + "\n".join(
            f"  - {v}" for v in all_violations
        )
        raise ValueError(msg)


_WIDE_TRANSITIVE_ND_DOMAIN = 16
"""ND-domain size above which a transitively-compared numeric input is routed
through behavioral bisection (how-only).  Small enumerations (step numbers,
modes) stay as-is; clearly-wide ranges from declared bounds get collapsed."""

_BISECTABLE_NUMERIC_TYPES = frozenset({TagType.INT, TagType.DINT, TagType.WORD, TagType.REAL})


def _wide_transitive_nd_candidates(ctx: _PassContext) -> list[str]:
    """How-only: wide-domain numeric ND inputs compared only transitively.

    A numeric ND input that participates in no *direct* comparison atom gets a
    domain only from its declared ``min``/``max`` (``_declared_domain``), which
    enumerates the full integer range — up to 1000 values.  When that range is
    wide, the per-state ND enumeration blows the how() eval budget even though
    only a handful of band-crossing values are behaviorally distinct (e.g. an
    analog level fed through ``calc(100 - level, pv)`` and compared one hop
    downstream).  Route these through behavioral bisection to collapse the
    range to its partition boundaries.

    Inputs with a *direct* comparison atom already get a partitioned domain
    from ``_extract_value_domain``, so they are left alone — bisection could
    drop a tested value that the target depends on.
    """
    if ctx.nondeterministic_dims is None or ctx.graph is None or ctx.all_exprs is None:
        return []
    pending = set(ctx._pending_infeasible_tags)
    candidates: list[str] = []
    for name, domain in ctx.nondeterministic_dims.items():
        if name in pending or len(domain) <= _WIDE_TRANSITIVE_ND_DOMAIN:
            continue
        tag = ctx.graph.tags.get(name)
        if tag is None or tag.type not in _BISECTABLE_NUMERIC_TYPES:
            continue
        if _collect_atoms_for_tag(ctx.all_exprs, name):
            continue  # directly compared — its domain is already meaningful
        candidates.append(name)
    return candidates


def _pass_heuristic_seed_domains(ctx: _PassContext) -> None:
    """Seed heuristic domains for residual infeasible tags (how-only, unsound).

    Unsound — seeds representative values for tags the static domain stack
    cannot close, plus wide-domain numeric ND inputs that are only compared
    transitively (see ``_wide_transitive_nd_candidates``).  Skipped when a
    pipeline cache is present (the cache already includes seeded results).
    Two strategies based on tag role:

    **Stateful tags** (written internally): trace-observation — run scans from
    the snapshot across ND input combos, collect all values the kernel produces,
    expand ± 1.

    **Nondeterministic tags** (external inputs): behavioral bisection — spread
    probe values across the type range, fingerprint the downstream behavior at
    each probe, bisect between probes with differing fingerprints to discover
    the partition boundaries.  Domain = one representative per behavioral
    partition + boundary values ± 1.
    """
    if ctx.pipeline_cache is not None:
        return
    assert ctx.graph is not None and ctx.all_exprs is not None

    candidates = list(ctx._pending_infeasible_tags)
    seen = set(candidates)
    for name in _wide_transitive_nd_candidates(ctx):
        if name not in seen:
            candidates.append(name)
            seen.add(name)
    if not candidates:
        return

    from .seeding import _discover_domains

    discovered = _discover_domains(
        candidates,
        ctx.graph.tags,
        ctx.graph.tag_roles,
        ctx.graph.writers_of,
        ctx.all_exprs,
        ctx.compiled,
        ctx.nondeterministic_dims,
        ctx.dt,
        ctx.receive_dest_names,
        initial_state=ctx.initial_state,
        program=ctx.program,
        graph=ctx.graph,
    )

    if not discovered:
        return

    exclusions: dict[str, str] | None = {} if ctx.journal_builder is not None else None
    unclassified: set[str] = set()
    result = _classify_dimensions_from_graph(
        ctx.program,
        ctx.graph,
        ctx.all_exprs,
        scope=ctx.scope,
        project=ctx.project,
        discovered_domains=discovered,
        receive_dest_names=ctx.receive_dest_names,
        exclusions=exclusions,
        unclassified=unclassified,
    )
    if isinstance(result, Intractable):
        ctx._pending_infeasible_tags = list(result.tags)
        ctx._pending_infeasible_hints = list(result.hints)
        ctx._unclassified_written = frozenset(unclassified)
        if result._debug_context is not None:
            sd, nd, _comb, da, dp, dk = result._debug_context
            ctx.stateful_dims = sd
            ctx.nondeterministic_dims = nd
            ctx._combinational_tags = _comb
            ctx.done_acc = da
            ctx.done_presets = dp
            ctx.done_kinds = dk
    else:
        sd, nd, _comb, da, dp, dk = result
        ctx.stateful_dims = sd
        ctx.nondeterministic_dims = nd
        ctx._combinational_tags = _comb
        ctx.done_acc = da
        ctx.done_presets = dp
        ctx.done_kinds = dk
        ctx._pending_infeasible_tags.clear()
        ctx._pending_infeasible_hints.clear()
        ctx._unclassified_written = frozenset(unclassified)

    heuristic_tag_names = sorted(discovered.keys() & (set(sd) | set(nd)))
    if heuristic_tag_names and ctx.journal_builder is not None:
        for tag_name in heuristic_tag_names:
            ctx.journal_builder.record(
                tag_name,
                Decision(
                    "heuristic_seed_domains",
                    "domain",
                    "heuristic",
                    "unsound domain from type boundaries + trace observation",
                ),
            )

    if heuristic_tag_names:
        ctx._heuristic_seeded_tags = frozenset(heuristic_tag_names)


def _collect_stateful_upstream_nd_names(
    graph: ProgramGraph | None,
    stateful_dims: dict[str, tuple[Any, ...]] | None,
    nd_dims: dict[str, tuple[Any, ...]] | None,
) -> set[str]:
    if graph is None or not stateful_dims or not nd_dims:
        return set()
    nd_names = set(nd_dims)
    result: set[str] = set()
    for stateful_name in stateful_dims:
        result |= graph.upstream_slice(stateful_name) & nd_names
    return result


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


def _pass_apply_split_at(ctx: _PassContext) -> None:
    """Promote split_at tags from stateful to nondeterministic dimensions."""
    if ctx.split_at_tags is None:
        return
    if ctx.stateful_dims is None or ctx.nondeterministic_dims is None:
        return
    for tag_name, domain in ctx.split_at_tags.items():
        if tag_name in ctx.stateful_dims:
            del ctx.stateful_dims[tag_name]
            ctx.nondeterministic_dims[tag_name] = domain
            if ctx.journal_builder is not None:
                ctx.journal_builder.record(
                    tag_name,
                    Decision(
                        "apply_split_at",
                        "classification",
                        "nondeterministic",
                        "promoted by split_at directive",
                        detail=(("domain", domain),),
                    ),
                )


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
            f"info | diagnose_unwritten_tags | "
            f"{len(never_written)} tag(s) are never written and will be "
            f"treated as nondeterministic inputs: [{names}]. "
            f"Consider adding external=True (input) or readonly=True "
            f"(constant) to make intent explicit."
        )

    missing_external = sorted(
        name
        for name in ctx.receive_dest_names
        if name in ctx.graph.tags and not ctx.graph.tags[name].external
    )

    if missing_external and ctx.progress_info is not None:
        names = ", ".join(missing_external)
        ctx.progress_info(
            f"info | diagnose_unwritten_tags | "
            f"{len(missing_external)} receive() destination tag(s) "
            f"missing external=True: [{names}]. "
            f"Receive destinations hold data from outside the program; "
            f"consider adding external=True to their declarations."
        )


def _pass_elide_scan_local_state(ctx: _PassContext) -> None:
    from pyrung.circuitpy.codegen import compile_kernel as _compile_kernel

    assert ctx.graph is not None
    assert ctx.stateful_dims is not None and ctx.nondeterministic_dims is not None
    if ctx.compiled is None:
        ctx.compiled = _compile_kernel(ctx.program, blockless=True, proof_metadata=True)
    pre_elision_infeasible = set(ctx._pending_infeasible_tags)
    original_stateful_dims = dict(ctx.stateful_dims)
    projected_stateful = frozenset(ctx.project or ()) & frozenset(original_stateful_dims)
    observer_tag_names = (
        frozenset(ctx.graph.writers_of) - frozenset(original_stateful_dims)
    ) | projected_stateful
    infeasible_unclassified: set[str] = set()
    elidable_dims, elided_dict, proof_details, substitutions = _elide_scan_local_stateful_dims(
        ctx.program,
        ctx.graph,
        original_stateful_dims,
        ctx.nondeterministic_dims,
        observer_exprs=tuple(ctx.extra_exprs or ()),
        observer_tag_names=observer_tag_names,
        projected_observers=projected_stateful,
        progress=ctx.progress_info,
        progress_prefix=ctx.progress_prefix,
        unclassified_tags=ctx._unclassified_written,
        infeasible_out=infeasible_unclassified,
    )

    if infeasible_unclassified:
        tags = sorted(infeasible_unclassified)
        hints = [
            f"  {name}: unclassified tag with no inferrable domain — "
            f"add choices=, min=/max=, or readonly=True"
            for name in tags
        ]
        ctx._pending_infeasible_tags.extend(tags)
        ctx._pending_infeasible_hints.extend(hints)
        if ctx.journal_builder is not None:
            for tag_name in tags:
                ctx.journal_builder.record(
                    tag_name,
                    Decision(
                        "elide_scan_local_state",
                        "classification",
                        "infeasible",
                        "unclassified tag in observer influence cone — unbounded domain",
                    ),
                )

    if substitutions and ctx.extra_exprs:
        from .expr import _substitute_elided_atoms

        rewritten_exprs: list[Expr] = []
        for expr in ctx.extra_exprs:
            rewritten = _substitute_elided_atoms(expr, substitutions)
            rewritten_exprs.append(rewritten if rewritten is not None else expr)
        ctx.extra_exprs = rewritten_exprs
        if ctx.all_exprs:
            new_all: list[Expr] = []
            for expr in ctx.all_exprs:
                rewritten = _substitute_elided_atoms(expr, substitutions)
                new_all.append(rewritten if rewritten is not None else expr)
            ctx.all_exprs = new_all

    from .expr import _edge_source_tags

    edge_sources = _edge_source_tags(ctx.program)
    demoted = {name: method for name, method in elided_dict.items() if name in edge_sources}
    truly_elided = {
        name: method for name, method in elided_dict.items() if name not in edge_sources
    }
    ctx.demotable_edge_tag_names = tuple(sorted(demoted))
    ctx.stateful_dims = elidable_dims
    ctx._elided_tags = truly_elided
    if ctx.journal_builder is not None:
        for tag_name, method in truly_elided.items():
            ctx.journal_builder.record(
                tag_name,
                Decision(
                    "elide_scan_local_state",
                    "elision",
                    f"elided:{method}",
                    f"scan-local by {method} proof",
                    detail=proof_details.get(tag_name, ()),
                ),
            )
        for tag_name, method in demoted.items():
            ctx.journal_builder.record(
                tag_name,
                Decision(
                    "elide_scan_local_state",
                    "demotion",
                    f"demoted:{method}",
                    f"edge-source with scan-local exit by {method} proof -- B_prev forwarded by BFS",
                    detail=proof_details.get(tag_name, ()),
                ),
            )

    ctx._elision_infeasible_delta = sorted(
        set(ctx._pending_infeasible_tags) - pre_elision_infeasible
    )


def _pass_heuristic_seed_post_elision(ctx: _PassContext) -> None:
    """Seed heuristic domains for tags that became infeasible during elision.

    Lightweight variant of ``_pass_heuristic_seed_domains`` that directly
    slots discovered domains into the existing dims without full
    reclassification (which would undo elision decisions).
    """
    if ctx.pipeline_cache is not None:
        return
    delta = getattr(ctx, "_elision_infeasible_delta", None)
    if not delta:
        return
    assert ctx.graph is not None

    from .seeding import _discover_domains

    discovered = _discover_domains(
        list(delta),
        ctx.graph.tags,
        ctx.graph.tag_roles,
        ctx.graph.writers_of,
        ctx.all_exprs,
        ctx.compiled,
        ctx.nondeterministic_dims,
        ctx.dt,
        ctx.receive_dest_names,
        initial_state=ctx.initial_state,
        program=ctx.program,
        graph=ctx.graph,
    )

    if not discovered:
        return

    resolved: list[str] = []
    for tag_name, domain in discovered.items():
        if not domain:
            continue
        role = ctx.graph.tag_roles.get(tag_name)
        is_written = tag_name in ctx.graph.writers_of
        is_nd = role == TagRole.INPUT or not is_written or tag_name in ctx.receive_dest_names
        if is_nd:
            if ctx.nondeterministic_dims is not None:
                ctx.nondeterministic_dims[tag_name] = domain
        else:
            if ctx.stateful_dims is not None:
                ctx.stateful_dims[tag_name] = domain
        resolved.append(tag_name)
        if ctx.journal_builder is not None:
            ctx.journal_builder.record(
                tag_name,
                Decision(
                    "heuristic_seed_post_elision",
                    "domain",
                    "heuristic",
                    "unsound domain seeded after elision discovered infeasible tag",
                ),
            )

    resolved_set = set(resolved)
    ctx._pending_infeasible_tags = [
        t for t in ctx._pending_infeasible_tags if t not in resolved_set
    ]
    ctx._pending_infeasible_hints = [
        h
        for h in ctx._pending_infeasible_hints
        if not any(h.strip().startswith(f"{t}:") for t in resolved_set)
    ]
    ctx._heuristic_seeded_tags = ctx._heuristic_seeded_tags | resolved_set


def _is_sequential_unconditional_same_scope(
    x_name: str,
    y_name: str,
    graph: Any,
    program: Any,
) -> bool:
    """Check if Y is safely derived from X via sequential unconditional writes.

    Returns True when:
    - X and Y are written in the same subroutine (or both main-line)
    - Y's writer follows X's last writer in node order within that scope
    - Y's writer rung is unconditional (no conditions on the rung itself)
    - No ``return_early()`` between X's last writer and Y's writer
    """
    from pyrung.core.instruction.control import ReturnInstruction

    x_writers = graph.writers_of.get(x_name, frozenset())
    y_writers = graph.writers_of.get(y_name, frozenset())
    if not x_writers or not y_writers:
        return False

    y_scopes = {(graph.rung_nodes[i].subroutine, graph.rung_nodes[i].scope) for i in y_writers}
    if len(y_scopes) != 1:
        return False
    y_sub, y_scope = next(iter(y_scopes))

    if y_sub is not None:
        scope_rungs = program.subroutines.get(y_sub, [])
    else:
        scope_rungs = program.rungs

    for y_idx in y_writers:
        y_node = graph.rung_nodes[y_idx]
        if y_node.rung_index >= len(scope_rungs):
            return False
        if scope_rungs[y_node.rung_index]._conditions:
            return False

    x_in_scope = [
        i
        for i in x_writers
        if graph.rung_nodes[i].subroutine == y_sub and graph.rung_nodes[i].scope == y_scope
    ]
    if not x_in_scope:
        return False

    x_last = max(x_in_scope)
    y_first = min(y_writers)
    if x_last >= y_first:
        return False

    x_last_rung_index = graph.rung_nodes[x_last].rung_index
    y_first_rung_index = graph.rung_nodes[y_first].rung_index
    for ri in range(x_last_rung_index + 1, y_first_rung_index):
        if ri >= len(scope_rungs):
            break
        if any(isinstance(instr, ReturnInstruction) for instr in scope_rungs[ri]._instructions):
            return False

    return True


def _pass_detect_functional_dependencies(ctx: _PassContext) -> None:
    assert ctx.graph is not None and ctx.stateful_dims is not None

    from pyrung.core.validation._common import walk_instructions

    from .absorb import _all_write_targets
    from .classify import _extract_forward_offset
    from .expr import _edge_source_tags

    source_offsets: dict[str, set[tuple[str, int | float]]] = {}
    disqualified: set[str] = set()

    for instr in walk_instructions(ctx.program):
        targets = [name for name, _itype in _all_write_targets(instr)]
        if not targets:
            continue
        fwd = _extract_forward_offset(instr)
        for target_name in targets:
            if target_name not in ctx.stateful_dims or target_name in disqualified:
                continue
            if fwd is None:
                disqualified.add(target_name)
                source_offsets.pop(target_name, None)
            else:
                source_offsets.setdefault(target_name, set()).add(fwd)

    edge_sources = _edge_source_tags(ctx.program)
    candidates: dict[str, tuple[str, int | float]] = {}
    for y_name, offsets in source_offsets.items():
        if y_name in disqualified:
            continue
        if len(offsets) != 1:
            continue
        x_name, offset = next(iter(offsets))
        if x_name == y_name:
            continue
        if x_name not in ctx.stateful_dims:
            continue
        if y_name in edge_sources:
            continue
        x_writers = ctx.graph.writers_of.get(x_name, frozenset())
        y_writers = ctx.graph.writers_of.get(y_name, frozenset())
        if not x_writers <= y_writers:
            if not _is_sequential_unconditional_same_scope(x_name, y_name, ctx.graph, ctx.program):
                continue
        candidates[y_name] = (x_name, offset)

    projected = {y: v for y, v in candidates.items() if v[0] not in candidates}

    if not projected:
        return

    if ctx._elided_tags is None:
        ctx._elided_tags = {}
    ctx._functional_dep_projections = projected
    for y_name in projected:
        del ctx.stateful_dims[y_name]
        ctx._elided_tags[y_name] = "functional_dep"

    if ctx.journal_builder is not None:
        for y_name, (x_name, offset) in projected.items():
            ctx.journal_builder.record(
                y_name,
                Decision(
                    "detect_functional_dependencies",
                    "projection",
                    "projected",
                    f"constant-offset {y_name} = {x_name} + {offset}, representative: {x_name}",
                    detail=(("source", x_name), ("offset", offset)),
                ),
            )


def _pass_detect_init_constants(ctx: _PassContext) -> None:
    assert ctx.graph is not None and ctx.stateful_dims is not None
    assert ctx.nondeterministic_dims is not None

    from pyrung.core.analysis.init_constants import detect_init_constants
    from pyrung.core.validation._common import _collect_write_sites

    from .absorb import _all_write_targets
    from .expr import _edge_source_tags

    all_sites = _collect_write_sites(ctx.program, target_extractor=_all_write_targets)
    sites_by_target: dict[str, list[Any]] = {}
    for site in all_sites:
        sites_by_target.setdefault(site.target_name, []).append(site)

    projected = detect_init_constants(
        program=ctx.program,
        graph=ctx.graph,
        sites_by_target=sites_by_target,
        candidate_tags=set(ctx.stateful_dims),
        nondeterministic_inputs=set(ctx.nondeterministic_dims),
        edge_source_tags=_edge_source_tags(ctx.program),
    )

    if not projected:
        return

    if ctx._elided_tags is None:
        ctx._elided_tags = {}
    ctx._init_constant_projections = projected
    for x_name in projected:
        if x_name in ctx.stateful_dims:
            del ctx.stateful_dims[x_name]
            ctx._elided_tags[x_name] = projected[x_name][1]

    if ctx.journal_builder is not None:
        for x_name, (rep, method) in projected.items():
            ctx.journal_builder.record(
                x_name,
                Decision(
                    "detect_init_constants",
                    "projection",
                    "projected",
                    f"{method}: representative {rep}",
                    detail=(("representative", rep), ("method", method)),
                ),
            )


def _pass_compile_kernel(ctx: _PassContext) -> None:
    from pyrung.circuitpy.codegen import compile_kernel as _compile_kernel

    if ctx.compiled is None:
        ctx.compiled = _compile_kernel(ctx.program, blockless=True, proof_metadata=True)
    assert ctx.stateful_dims is not None
    ctx.stateful_names = tuple(sorted(ctx.stateful_dims))
    combinational_edge = set(ctx.compiled.edge_tags) & (ctx._combinational_tags or set())
    demoted = set(ctx.demotable_edge_tag_names or ()) | combinational_edge
    ctx.demotable_edge_tag_names = tuple(sorted(demoted))
    ctx.edge_tag_names = tuple(n for n in sorted(ctx.compiled.edge_tags) if n not in demoted)


def _pass_collect_done_acc_pairs(ctx: _PassContext) -> None:
    ctx.done_acc_info = _collect_done_acc_pairs(ctx.program)
    if ctx.journal_builder is not None:
        for done_name, acc_name in ctx.done_acc_info.pairs.items():
            ctx.journal_builder.record(
                done_name,
                Decision(
                    "collect_done_acc_pairs",
                    "pairing",
                    "paired",
                    f"Done/Acc pair: {done_name} -> {acc_name}",
                    detail=(("acc_name", acc_name),),
                ),
            )


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
    if ctx.journal_builder is not None:
        for acc_name in ctx.absorptions.acc_names:
            ctx.journal_builder.record(
                acc_name,
                Decision(
                    "find_redundant_absorptions",
                    "absorption",
                    "absorbed",
                    "three-valued Done bit absorption",
                ),
            )
        for preset_name in ctx.absorptions.preset_tags:
            ctx.journal_builder.record(
                preset_name,
                Decision(
                    "find_redundant_absorptions",
                    "absorption",
                    "absorbed",
                    "synthetic preset replacement",
                ),
            )
        for acc_name, reason in ctx.absorptions.rejected.items():
            ctx.journal_builder.record(
                acc_name,
                Decision("find_redundant_absorptions", "absorption_skipped", "skipped", reason),
            )


def _pass_find_threshold_absorptions(ctx: _PassContext) -> None:
    if ctx.pipeline_cache is not None:
        ctx.threshold_absorptions = ctx.pipeline_cache.threshold_absorptions
        return
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
        receive_dest_names=ctx.receive_dest_names,
    )
    ctx.threshold_absorptions = _merge_threshold_absorptions(
        threshold_absorptions,
        comparison_absorptions,
    )
    if ctx.journal_builder is not None:
        for name in ctx.threshold_absorptions.progress_names:
            ctx.journal_builder.record(
                name,
                Decision(
                    "find_threshold_absorptions",
                    "absorption",
                    "absorbed",
                    "threshold vector abstraction",
                ),
            )
        for name in ctx.threshold_absorptions.threshold_tags:
            ctx.journal_builder.record(
                name,
                Decision(
                    "find_threshold_absorptions", "absorption", "absorbed", "threshold tag absorbed"
                ),
            )
        for name in ctx.threshold_absorptions.comparison_tags:
            ctx.journal_builder.record(
                name,
                Decision(
                    "find_threshold_absorptions",
                    "absorption",
                    "absorbed",
                    "comparison-only tag absorbed",
                ),
            )
        for blocker in ctx.threshold_absorptions.blockers:
            for reason in blocker.reasons:
                ctx.journal_builder.record(
                    blocker.acc_name,
                    Decision("find_threshold_absorptions", "absorption_blocked", "blocked", reason),
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
        preset: int | str | None = ctx.done_presets.get(done_name)
        if preset is None and ctx.done_acc_info is not None:
            preset = ctx.done_acc_info.preset_tags.get(done_name)
        if preset is not None:
            preset_memory_key = (
                prove_effective_preset_key(done_name) if isinstance(preset, str) else None
            )
            d_events.append(
                _DoneEventSpec(
                    state_index=index,
                    acc_name=acc_name,
                    kind=kind,
                    preset=preset,
                    preset_memory_key=preset_memory_key,
                )
            )
    ctx.state_key_done_specs = tuple(sk_done)
    ctx.done_event_specs = tuple(d_events)

    if ctx.done_acc_info is not None:
        ctx.drum_event_meta = dict(ctx.done_acc_info.drum_meta)

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
    excluded_prefixes = ("_dt", "_frac:", PROVE_EFFECTIVE_PRESET_PREFIX)
    ctx.memory_key_names = tuple(
        sorted(k for k in pilot.memory if not any(k.startswith(p) for p in excluded_prefixes))
    )


def _pass_classify_dimensions_no_absorb(ctx: _PassContext) -> None:
    assert ctx.graph is not None and ctx.all_exprs is not None
    if ctx.journal_builder is not None:
        ctx.journal_builder.add_note(
            "Pass 'classify_dimensions' ran without absorption (_skip_optimizations=True)"
        )
    exclusions: dict[str, str] | None = {} if ctx.journal_builder is not None else None
    unclassified: set[str] = set()
    result = _classify_dimensions_from_graph(
        ctx.program,
        ctx.graph,
        ctx.all_exprs,
        scope=ctx.scope,
        project=ctx.project,
        receive_dest_names=ctx.receive_dest_names,
        _skip_absorptions=True,
        exclusions=exclusions,
        unclassified=unclassified,
    )
    if isinstance(result, Intractable):
        ctx._pending_infeasible_tags.extend(result.tags)
        ctx._pending_infeasible_hints.extend(result.hints)
        ctx._unclassified_written = frozenset(unclassified)
        if result._debug_context is not None:
            sd, nd, _comb, da, dp, dk = result._debug_context
            ctx.stateful_dims = sd
            ctx.nondeterministic_dims = nd
            ctx._combinational_tags = _comb
            ctx.done_acc = da
            ctx.done_presets = dp
            ctx.done_kinds = dk
        if exclusions:
            ctx._exclusions = exclusions
        return
    sd, nd, _comb, da, dp, dk = result
    ctx.stateful_dims = sd
    ctx.nondeterministic_dims = nd
    ctx._combinational_tags = _comb
    ctx.done_acc = da
    ctx.done_presets = dp
    ctx.done_kinds = dk
    ctx._unclassified_written = frozenset(unclassified)
    if exclusions:
        ctx._exclusions = exclusions


def _pass_stub_redundant_absorptions(ctx: _PassContext) -> None:
    ctx.absorptions = _RedundantAccAbsorptions(
        acc_names=frozenset(),
        preset_tags=frozenset(),
        synthetic_presets={},
    )
    ctx.synthetic_preset_tags = ()
    if ctx.journal_builder is not None:
        ctx.journal_builder.add_note(
            "Pass 'find_redundant_absorptions' disabled (_skip_optimizations=True)"
        )


def _pass_stub_threshold_absorptions(ctx: _PassContext) -> None:
    ctx.threshold_absorptions = _ThresholdAbsorptions(
        progress_names=frozenset(),
        threshold_tags=frozenset(),
        comparison_tags=frozenset(),
        vector_specs=(),
    )
    if ctx.journal_builder is not None:
        ctx.journal_builder.add_note(
            "Pass 'find_threshold_absorptions' disabled (_skip_optimizations=True)"
        )


def _pass_skip_elision(ctx: _PassContext) -> None:
    if ctx.journal_builder is not None:
        ctx.journal_builder.add_note(
            "Pass 'elide_scan_local_state' disabled (_skip_optimizations=True)"
        )


def _pass_skip_functional_dependencies(ctx: _PassContext) -> None:
    if ctx.journal_builder is not None:
        ctx.journal_builder.add_note("Pass 'detect_functional_dependencies' disabled")


def _pass_skip_init_constants(ctx: _PassContext) -> None:
    if ctx.journal_builder is not None:
        ctx.journal_builder.add_note("Pass 'detect_init_constants' disabled")


def _pass_skip_declared_bounds(ctx: _PassContext) -> None:
    pass


def _passes_for_opt_config(opt: _OptConfig) -> tuple[_PreBFSPass, ...]:
    """Select the pre-BFS pass tuple for *opt*, stubbing disabled optimizations.

    Each disabled pre-BFS optimization swaps its pass(es) for a stub. With all
    pre-BFS flags enabled this returns ``_DEFAULT_PRE_BFS_PASSES`` unchanged.
    The BFS-interleaved flags are not handled here — see ``_OptConfig.bfs_config``.
    """
    overrides: dict[str, Callable[[_PassContext], None]] = {}
    enable_overrides: dict[str, bool] = {}
    if not opt.accumulator_absorption:
        overrides["classify_dimensions"] = _pass_classify_dimensions_no_absorb
        overrides["find_redundant_absorptions"] = _pass_stub_redundant_absorptions
        overrides["find_threshold_absorptions"] = _pass_stub_threshold_absorptions
    if not opt.traced_elision:
        overrides["elide_scan_local_state"] = _pass_skip_elision
    if not opt.functional_dependency_projection:
        overrides["detect_functional_dependencies"] = _pass_skip_functional_dependencies
    if not opt.init_constant_projection:
        overrides["detect_init_constants"] = _pass_skip_init_constants
    if not opt.validate_declared_bounds:
        overrides["validate_declared_bounds"] = _pass_skip_declared_bounds
    if opt.heuristic_domain_seeding:
        enable_overrides["heuristic_seed_domains"] = True
        enable_overrides["heuristic_seed_post_elision"] = True
    if not overrides and not enable_overrides:
        return _DEFAULT_PRE_BFS_PASSES
    return tuple(
        _PreBFSPass(
            p.name,
            p.description,
            overrides.get(p.name, p.run),
            enabled=enable_overrides.get(p.name, p.enabled),
            requires=p.requires,
            provides=p.provides,
        )
        for p in _DEFAULT_PRE_BFS_PASSES
    )


def _unoptimized_passes() -> tuple[_PreBFSPass, ...]:
    """Return the pass tuple with elision and absorption replaced by stubs."""
    return _passes_for_opt_config(_OptConfig(traced_elision=False, accumulator_absorption=False))


_DEFAULT_PRE_BFS_PASSES: tuple[_PreBFSPass, ...] = (
    _PreBFSPass(
        "build_graph",
        "Build program dependency graph and collect expressions",
        _pass_build_graph,
        provides=frozenset({"graph", "all_exprs"}),
    ),
    _PreBFSPass(
        "classify_dimensions",
        "Partition tags into stateful/nondeterministic/combinational",
        _pass_classify_dimensions,
        requires=frozenset({"graph", "all_exprs"}),
        provides=frozenset({"classification"}),
    ),
    _PreBFSPass(
        "validate_declared_bounds",
        "Validate user-declared bounds against kernel behavior",
        _pass_validate_declared_bounds,
        requires=frozenset({"graph", "classification"}),
    ),
    _PreBFSPass(
        "heuristic_seed_domains",
        "Seed heuristic domains for residual infeasible tags (how-only, unsound)",
        _pass_heuristic_seed_domains,
        enabled=False,
        requires=frozenset({"graph", "classification"}),
    ),
    _PreBFSPass(
        "apply_split_at",
        "Promote split_at tags from stateful to nondeterministic (user directive)",
        _pass_apply_split_at,
        requires=frozenset({"classification"}),
    ),
    _PreBFSPass(
        "diagnose_unwritten_tags",
        "Surface never-written tags as user diagnostics",
        _pass_diagnose_unwritten_tags,
        requires=frozenset({"graph", "classification"}),
    ),
    _PreBFSPass(
        "elide_scan_local_state",
        "Elide scan-local state that is provably irrelevant across scans",
        _pass_elide_scan_local_state,
        requires=frozenset({"graph", "all_exprs", "classification"}),
    ),
    _PreBFSPass(
        "heuristic_seed_post_elision",
        "Seed heuristic domains for tags that became infeasible during elision (how-only, unsound)",
        _pass_heuristic_seed_post_elision,
        enabled=False,
        requires=frozenset({"graph", "classification"}),
    ),
    _PreBFSPass(
        "detect_functional_dependencies",
        "Project tags that are constant-offset functions of a surviving state dimension",
        _pass_detect_functional_dependencies,
        requires=frozenset({"graph", "all_exprs", "classification"}),
        provides=frozenset({"functional_deps"}),
    ),
    _PreBFSPass(
        "detect_init_constants",
        "Project tags whose values are fixed by a monotonic latch init guard",
        _pass_detect_init_constants,
        requires=frozenset({"graph", "all_exprs", "classification"}),
        provides=frozenset({"init_constants"}),
    ),
    _PreBFSPass(
        "compile_kernel",
        "Compile the replay kernel and derive stateful/edge tag names",
        _pass_compile_kernel,
        requires=frozenset({"classification"}),
        provides=frozenset({"compiled_names"}),
    ),
    _PreBFSPass(
        "collect_done_acc_pairs",
        "Map Done tags to their accumulator partners",
        _pass_collect_done_acc_pairs,
        provides=frozenset({"done_acc_info"}),
    ),
    _PreBFSPass(
        "find_redundant_absorptions",
        "Identify accumulators absorbed into Done bit state",
        _pass_find_redundant_absorptions,
        requires=frozenset({"graph", "all_exprs", "done_acc_info"}),
        provides=frozenset({"absorptions"}),
    ),
    _PreBFSPass(
        "find_threshold_absorptions",
        "Identify threshold jumping patterns for hidden accumulators",
        _pass_find_threshold_absorptions,
        requires=frozenset({"graph", "all_exprs"}),
        provides=frozenset({"threshold_absorptions"}),
    ),
    _PreBFSPass(
        "build_event_specs",
        "Construct Done and threshold event specifications",
        _pass_build_event_specs,
        requires=frozenset({"compiled_names", "classification", "threshold_absorptions"}),
        provides=frozenset({"event_specs"}),
    ),
    _PreBFSPass(
        "collect_edge_exprs",
        "Build expression map for edge tag compression",
        _pass_collect_edge_exprs,
        requires=frozenset({"compiled_names"}),
        provides=frozenset({"edge_tag_exprs"}),
    ),
    _PreBFSPass(
        "discover_memory_keys",
        "Discover kernel memory keys via pilot scan",
        _pass_discover_memory_keys,
        requires=frozenset({"compiled_names", "absorptions"}),
        provides=frozenset({"memory_key_names"}),
    ),
)


def _attach_partial_journal(ctx: _PassContext) -> Intractable:
    """Attach a partial journal to an Intractable from the pipeline."""
    from dataclasses import replace as _replace
    from types import MappingProxyType

    assert ctx.intractable is not None
    if ctx.journal_builder is None:
        return ctx.intractable
    partial = Journal(
        tags=MappingProxyType({}),
        notes=tuple(ctx.journal_builder._notes),
    )
    if ctx.graph is not None:
        partial = ctx.journal_builder.freeze(
            graph_tags=ctx.graph.tags,
            exclusions=ctx._exclusions or {},
            stateful_dims=ctx.stateful_dims or {},
            nondeterministic_dims=ctx.nondeterministic_dims or {},
            combinational_tags=ctx._combinational_tags or frozenset(),
            elided_tags=ctx._elided_tags,
            edge_bearing=frozenset(),
            free=frozenset(),
        )
    return _replace(ctx.intractable, journal=partial)


def _build_merged_intractable(ctx: _PassContext) -> Intractable:
    """Build a single Intractable from all accumulated infeasible tags/hints."""
    from dataclasses import replace as _replace

    tags = sorted(set(ctx._pending_infeasible_tags))
    hints = list(ctx._pending_infeasible_hints)

    sd = ctx.stateful_dims or {}
    nd = ctx.nondeterministic_dims or {}
    total_dims = len(sd) + len(nd) + len(tags)

    product = 1
    for domain in sd.values():
        product *= len(domain)
    for domain in nd.values():
        product *= len(domain)
    if product > 1:
        hints.append(
            f"  (surviving dimensions: {len(sd) + len(nd)}, "
            f"estimated {product:,} states before these blockers)"
        )

    intractable = Intractable(
        reason=f"unbounded domain on {', '.join(tags)}",
        dimensions=total_dims,
        estimated_space=product,
        tags=tags,
        hints=hints,
    )

    if ctx.journal_builder is not None and ctx.graph is not None:
        journal = ctx.journal_builder.freeze(
            graph_tags=ctx.graph.tags,
            exclusions=ctx._exclusions or {},
            stateful_dims=sd,
            nondeterministic_dims=nd,
            combinational_tags=ctx._combinational_tags or frozenset(),
            elided_tags=ctx._elided_tags,
            edge_bearing=frozenset(),
            free=frozenset(),
        )
        intractable = _replace(intractable, journal=journal)
    elif ctx.journal_builder is not None:
        from types import MappingProxyType

        partial = Journal(
            tags=MappingProxyType({}),
            notes=tuple(ctx.journal_builder._notes),
        )
        intractable = _replace(intractable, journal=partial)

    return intractable


def _run_pre_bfs_pipeline(
    ctx: _PassContext,
    passes: tuple[_PreBFSPass, ...] = _DEFAULT_PRE_BFS_PASSES,
) -> _ExploreContext | Intractable:
    _validate_pass_dag(passes)
    for p in passes:
        if not p.enabled:
            continue
        t0 = time.monotonic()
        p.run(ctx)
        logger.info("pass %s completed in %.2fs", p.name, time.monotonic() - t0)
        if ctx.intractable is not None:
            return _attach_partial_journal(ctx)
    if ctx._pending_infeasible_tags:
        return _build_merged_intractable(ctx)
    result = ctx.freeze()
    cache = ctx.extract_cache()
    if cache is not None:
        from dataclasses import replace as _dc_replace

        result = _dc_replace(result, pipeline_cache=cache)
    return result
