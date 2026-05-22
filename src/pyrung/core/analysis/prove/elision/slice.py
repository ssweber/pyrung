"""Slice elision: sound-by-construction state-key elision.

Answers a single structural question per candidate stateful tag ``X``:

    "Is X always written before it is read, in every structurally distinct
    single-scan scenario?"

If yes, ``X``'s scan-entry value never reaches any computation, so ``X`` is
scan-local and can be dropped from the BFS state key.

This is a single-scan hypothetical over *all* entry states.  Because the
reachable entry states are a subset of all entry states, an answer that holds
for every entry state holds for the reachable ones too — which decouples
elision from BFS reachability entirely.

Pipeline per candidate:

0. Fast path — def-use chains: if ``X``'s entry version has no readers and the
   first write is unconditional, ``X`` is trivially write-before-read.
1. Slice — the rungs that touch ``X`` (writers + readers, incl. subroutines).
2. Closure — the transitive set of entry-state tags that can affect the
   conditions gating those rungs (``_upstream_closure``).
3. Free variables — the closure tags that classify already tracks
   (``stateful_dims`` ∪ ``nondeterministic_dims``); every other closure tag is
   provably scan-local already, so it is fixed at its default.  Plus oneshot
   and rising/falling-edge memory keys.
4. Enumerate — the cross product of the free variables' domains (capped).
5. Check — for each combination, run one natural-mode scan of the full program
   and detect whether any read of ``X`` saw its scan-entry value.
6. Decide — all combinations write-before-read ⇒ elidable.

Soundness notes:

* Free variables are restricted to ``stateful_dims`` ∪ ``nondeterministic_dims``.
  classify has already proven every other written tag scan-local, so fixing it
  at its default in the hypothetical scan is sound.
* Timer/counter accumulators enter the closure via ``exclusive_reads`` and are
  enumerated across their threshold-partitioned domains.  Their sub-tick
  fractional memory is *not* modelled — it is unnecessary, because within one
  scan the integer accumulator advances by at most one unit, and a threshold
  partition that contains a representative ``p`` also covers ``p+1``'s
  condition behaviour.
* Drum and shift instructions carry cross-scan memory that is not modelled; a
  candidate whose slice depends on one bails conservatively (kept stateful).
* An unclassified tag (no inferrable domain) in the closure also bails.

Anything that cannot be partitioned, or whose cross product exceeds the cap,
bails conservatively — the candidate is kept stateful.  Bailing is always
sound; it only costs aggressiveness.
"""

from __future__ import annotations

import itertools
import sys
from collections import defaultdict
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pdg import ProgramGraph, _extract_write_targets
from pyrung.core.condition import (
    AllCondition,
    AnyCondition,
    Condition,
    FallingEdgeCondition,
    RisingEdgeCondition,
)
from pyrung.core.context import ConditionView, ScanContext
from pyrung.core.executor import execute_program
from pyrung.core.instruction.advanced import ShiftInstruction
from pyrung.core.instruction.control import ForLoopInstruction
from pyrung.core.instruction.drums import EventDrumInstruction, TimeDrumInstruction
from pyrung.core.rung import Rung
from pyrung.core.state import SystemState
from pyrung.core.tag import ImmediateRef

from ..expr import _referenced_tags
from ..results import PENDING

if TYPE_CHECKING:
    from pyrung.core.analysis.simplified import Expr
    from pyrung.core.program import Program
    from pyrung.core.tag import Tag


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


# Maximum size of a candidate's free-variable cross product.  Larger ⇒ bail.
_SLICE_COMBO_CAP = 1024

# Fixed scan timestep, mirroring the traced elision scans.
_SLICE_DT = 0.010


# ---------------------------------------------------------------------------
# Write-before-read instrumentation
# ---------------------------------------------------------------------------


class _WriteBeforeReadContext(ScanContext):
    """ScanContext that flags whether one watched tag is read from entry.

    ``entry_read_seen`` becomes True the first time the watched tag is read
    before any write to it occurred during the scan — i.e. a read that
    observed the tag's scan-entry value.
    """

    __slots__ = ("_watched", "entry_read_seen")

    def __init__(self, state: SystemState, watched: str) -> None:
        super().__init__(state)
        self._watched = watched
        self.entry_read_seen = False

    def _new_condition_view(self) -> _WBRConditionView:
        return _WBRConditionView(self)

    def get_tag(self, name: str, default: Any = None) -> Any:
        if name == self._watched and name not in self._tags_pending:
            self.entry_read_seen = True
        return super().get_tag(name, default)


class _WBRConditionView(ConditionView):
    """Condition snapshot view that reports watched-tag entry reads."""

    __slots__ = ("_wbr_ctx",)

    def __init__(self, ctx: _WriteBeforeReadContext) -> None:
        super().__init__(ctx)
        self._wbr_ctx = ctx

    def get_tag(self, name: str, default: Any = None) -> Any:
        if name == self._wbr_ctx._watched and name not in self._tags_snapshot:
            self._wbr_ctx.entry_read_seen = True
        return super().get_tag(name, default)


# ---------------------------------------------------------------------------
# Static helpers
# ---------------------------------------------------------------------------


def _upstream_closure(graph: ProgramGraph, seed_tags: frozenset[str]) -> frozenset[str]:
    """All tags transitively upstream of *seed_tags* through the PDG.

    Multi-seeded variant of :meth:`ProgramGraph.upstream_slice_with_calls`.
    Follows ``condition_reads | data_reads | exclusive_reads`` of every writer
    (``exclusive_reads`` carries timer/counter accumulators) and, for writers
    inside a subroutine, the call-site conditions.  The seed tags are included
    in the result.
    """
    visited_tags: set[str] = set()
    visited_rungs: set[int] = set()
    visited_subs: set[str] = set()
    queue: list[str] = list(seed_tags)

    while queue:
        current = queue.pop()
        if current in visited_tags:
            continue
        visited_tags.add(current)
        for rung_idx in graph.writers_of.get(current, frozenset()):
            if rung_idx in visited_rungs:
                continue
            visited_rungs.add(rung_idx)
            node = graph.rung_nodes[rung_idx]
            for read_tag in node.condition_reads | node.data_reads | node.exclusive_reads:
                if read_tag not in visited_tags:
                    queue.append(read_tag)
            if node.subroutine is not None and node.subroutine not in visited_subs:
                visited_subs.add(node.subroutine)
                for caller in graph.rung_nodes:
                    if node.subroutine in caller.calls:
                        for read_tag in caller.condition_reads:
                            if read_tag not in visited_tags:
                                queue.append(read_tag)

    return frozenset(visited_tags)


def _iter_program_instructions(program: Program) -> Any:
    """Yield every instruction in the program (main + subroutines + loops)."""

    def walk_forloop(loop: ForLoopInstruction) -> Any:
        for child in loop.instructions:
            yield child
            if isinstance(child, ForLoopInstruction):
                yield from walk_forloop(child)

    def walk_rung(rung: Rung) -> Any:
        for item in rung._execution_items:
            if isinstance(item, Rung):
                yield from walk_rung(item)
            else:
                yield item
                if isinstance(item, ForLoopInstruction):
                    yield from walk_forloop(item)

    for rung in program.rungs:
        yield from walk_rung(rung)
    for rungs in program.subroutines.values():
        for rung in rungs:
            yield from walk_rung(rung)


def _instruction_write_targets(instr: Any, tag_refs: dict[str, Tag]) -> set[str]:
    """Return the set of tag names written by *instr*."""
    targets: set[str] = set()
    for field_name in getattr(type(instr), "_writes", ()):
        value = getattr(instr, field_name, None)
        if value is None:
            continue
        writes, _ = _extract_write_targets(value, tag_refs)
        targets.update(writes)
    return targets


def _collect_hidden_memory_info(
    program: Program,
    tag_refs: dict[str, Tag],
) -> tuple[frozenset[str], tuple[tuple[str, frozenset[str]], ...]]:
    """Survey instructions for unmodelled cross-scan memory and oneshot keys.

    Returns ``(drum_shift_writes, oneshot_key_writes)`` where *drum_shift_writes*
    is the set of tags written by drum/shift instructions (whose hidden memory
    slice elision does not model) and *oneshot_key_writes* pairs each
    oneshot instruction's ``_oneshot`` memory key with the tags it writes.
    """
    drum_shift_writes: set[str] = set()
    oneshot_key_writes: list[tuple[str, frozenset[str]]] = []

    for instr in _iter_program_instructions(program):
        targets = _instruction_write_targets(instr, tag_refs)
        if isinstance(instr, (EventDrumInstruction, TimeDrumInstruction, ShiftInstruction)):
            drum_shift_writes.update(targets)
        if getattr(instr, "_oneshot", False):
            oneshot_key_writes.append((instr.memory_key("_oneshot"), frozenset(targets)))

    return frozenset(drum_shift_writes), tuple(oneshot_key_writes)


def _collect_edge_tags(program: Program) -> frozenset[str]:
    """Collect tags referenced by explicit rise()/fall() conditions anywhere.

    Walks rung conditions, branch conditions, and instruction condition fields
    so that an edge condition nested inside an instruction guard is not missed.
    """
    result: set[str] = set()

    def walk_condition(cond: Any) -> None:
        if isinstance(cond, (RisingEdgeCondition, FallingEdgeCondition)):
            tag = cond.tag
            wrapped = tag.value if isinstance(tag, ImmediateRef) else tag
            name = getattr(wrapped, "name", None)
            if isinstance(name, str):
                result.add(name)
            return
        if isinstance(cond, (AllCondition, AnyCondition)):
            for child in cond.conditions:
                walk_condition(child)

    def walk_instruction(instr: Any) -> None:
        for field_name in getattr(type(instr), "_conditions", ()):
            value = getattr(instr, field_name, None)
            if isinstance(value, Condition):
                walk_condition(value)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    if isinstance(item, Condition):
                        walk_condition(item)
        if isinstance(instr, ForLoopInstruction):
            for child in instr.instructions:
                walk_instruction(child)

    def walk_rung(rung: Rung) -> None:
        for cond in rung._conditions:
            walk_condition(cond)
        for branch in rung._branches:
            walk_rung(branch)
        for item in rung._execution_items:
            if not isinstance(item, Rung):
                walk_instruction(item)

    for rung in program.rungs:
        walk_rung(rung)
    for rungs in program.subroutines.values():
        for rung in rungs:
            walk_rung(rung)

    return frozenset(result)


def _collect_caller_condition_reads(graph: ProgramGraph) -> dict[str, frozenset[str]]:
    """Map each subroutine name to the union of its call-site condition reads."""
    caller: dict[str, set[str]] = defaultdict(set)
    for node in graph.rung_nodes:
        for sub_name in node.calls:
            caller[sub_name].update(node.condition_reads)
    return {name: frozenset(reads) for name, reads in caller.items()}


# ---------------------------------------------------------------------------
# Per-candidate elision analysis
# ---------------------------------------------------------------------------


class _SliceElision:
    """Per-program slice-elision analyzer.

    Holds the program-wide precomputed indices and exposes
    :meth:`candidate_elidable` for one candidate stateful tag at a time.
    """

    def __init__(
        self,
        program: Program,
        graph: ProgramGraph,
        stateful_dims: Mapping[str, tuple[Any, ...]],
        nondeterministic_dims: Mapping[str, tuple[Any, ...]],
    ) -> None:
        self.program = program
        self.graph = graph
        self.stateful_dims = stateful_dims
        self.nondeterministic_dims = nondeterministic_dims

        tag_refs = dict(graph.tags)
        self.drum_shift_writes, self.oneshot_key_writes = _collect_hidden_memory_info(
            program, tag_refs
        )
        # Tags written by a oneshot instruction.  A oneshot write does not fire
        # every scan, so def-use chains (sequential SSA, which assumes every
        # write fires) cannot be trusted for the fast path.
        self.oneshot_written_tags: frozenset[str] = frozenset(
            tag for _key, targets in self.oneshot_key_writes for tag in targets
        )
        self.edge_tags = _collect_edge_tags(program)
        self.caller_condition_reads = _collect_caller_condition_reads(graph)

        # Memoized scan-locality results, plus a recursion guard.  Determining
        # whether one tag is scan-local can require recursively checking
        # unclassified closure tags; a cycle means cross-scan dependence.
        self._scan_local_memo: dict[str, bool] = {}
        self._in_progress: set[str] = set()

    # -- fast path ---------------------------------------------------------

    def _fast_path_elidable(self, candidate: str) -> bool:
        """True when def-use chains prove unconditional write-before-read.

        Delegates to ``ProgramGraph.unconditional_write_before_read`` with an
        additional oneshot guard: a tag written by a oneshot instruction is
        never eligible because the oneshot write fires only on first
        activation, but def-use chains optimistically record it as a normal
        write, so a later read could observe the entry value on subsequent
        scans.  Such tags fall through to the full slice check.
        """
        if candidate in self.oneshot_written_tags:
            return False
        return self.graph.unconditional_write_before_read(candidate)

    # -- scan-locality determination --------------------------------------

    def _is_scan_local(self, name: str) -> bool:
        """Memoized, cycle-safe test of whether *name* is always written before read.

        A scan-local tag's entry value is never observed, so it can be dropped
        from the BFS state key.  Used both for top-level candidates and,
        recursively, for unclassified tags found inside a candidate's closure.
        A dependency cycle means the tag's value depends on its own scan-entry
        value — cross-scan state — so it is conservatively not scan-local.
        """
        cached = self._scan_local_memo.get(name)
        if cached is not None:
            return cached
        if name in self._in_progress:
            return False

        tag = self.graph.tags.get(name)
        if tag is None:
            self._scan_local_memo[name] = False
            return False

        if not self.graph.all_readers_of.get(name):
            self._scan_local_memo[name] = True
            return True

        if self._fast_path_elidable(name):
            self._scan_local_memo[name] = True
            return True

        self._in_progress.add(name)
        try:
            result = self._slice_check(name, tag)
        finally:
            self._in_progress.discard(name)
        self._scan_local_memo[name] = result
        return result

    def candidate_elidable(self, candidate: str) -> tuple[bool, str]:
        """Return ``(elidable, reason)`` for one candidate stateful tag."""
        if self.graph.tags.get(candidate) is None:
            return False, ""
        if not self._is_scan_local(candidate):
            return False, ""
        if not self.graph.all_readers_of.get(candidate):
            return True, "no_readers"
        if self._fast_path_elidable(candidate):
            return True, "fast_path"
        return True, "enumerated"

    # -- full slice check --------------------------------------------------

    def _slice_check(self, candidate: str, tag: Tag) -> bool:
        """Run the full enumerate-and-scan write-before-read check."""
        graph = self.graph
        writer_rungs = graph.writers_of.get(candidate, frozenset())
        reader_rungs = graph.all_readers_of.get(candidate, frozenset())
        x_touching = writer_rungs | reader_rungs

        # Step 2: condition seed + transitive closure.
        seed: set[str] = set()
        for rung_idx in x_touching:
            node = graph.rung_nodes[rung_idx]
            seed.update(node.condition_reads)
            if node.subroutine is not None:
                seed.update(self.caller_condition_reads.get(node.subroutine, frozenset()))
        seed.discard(candidate)
        closure = set(_upstream_closure(graph, frozenset(seed)))
        closure.discard(candidate)

        relevant_tags = closure | {candidate}

        # Bail: drum/shift hidden memory feeds the slice.
        if relevant_tags & self.drum_shift_writes:
            return False

        # Step 3: free variables and their domains.
        var_kinds: list[str] = []  # "tag" | "mem"
        var_names: list[str] = []
        domains: list[tuple[Any, ...]] = []

        for name in sorted(closure):
            if name in self.stateful_dims:
                # A stateful closure tag that is itself scan-local has an
                # irrelevant entry value — fix it at its default and let the
                # scan recompute it, shrinking the cross product.
                if self._is_scan_local(name):
                    continue
                domain = self.stateful_dims[name]
                if PENDING in domain:
                    return False
            elif name in self.nondeterministic_dims:
                domain = self.nondeterministic_dims[name]
            else:
                # Not a tracked dimension.  A readonly tag is a constant — its
                # default is its value, so fixing it there is exact.  Any other
                # tag (combinational, comparison-only absorbed, unclassified)
                # may only be fixed at its default when it is itself
                # scan-local; otherwise its entry value matters but has no
                # enumerable domain, and the candidate must bail.
                closure_tag = self.graph.tags.get(name)
                if closure_tag is not None and closure_tag.readonly:
                    continue
                if self._is_scan_local(name):
                    continue
                return False
            if not domain:
                return False
            var_kinds.append("tag")
            var_names.append(name)
            domains.append(tuple(domain))

        # Oneshot memory keys whose instruction writes a relevant tag.
        for key, targets in self.oneshot_key_writes:
            if targets & relevant_tags:
                var_kinds.append("mem")
                var_names.append(key)
                domains.append((False, True))

        # Rising/falling-edge _prev memory keys for relevant edge tags.
        for name in sorted(closure & self.edge_tags):
            prev_domain = self._prev_domain(name)
            if prev_domain is None:
                return False
            var_kinds.append("mem")
            var_names.append(f"_prev:{name}")
            domains.append(prev_domain)

        # Step 4: cap the cross product.
        total = 1
        for domain in domains:
            total *= len(domain)
            if total > _SLICE_COMBO_CAP:
                return False

        # Step 5: one natural scan per combination.
        x_default = tag.default
        for combo in itertools.product(*domains):
            tag_values: dict[str, Any] = {}
            memory_values: dict[str, Any] = {}
            if x_default is not None:
                tag_values[candidate] = x_default
            for kind, name, value in zip(var_kinds, var_names, combo, strict=True):
                if kind == "tag":
                    tag_values[name] = value
                else:
                    memory_values[name] = value

            state = SystemState().with_tags(tag_values) if tag_values else SystemState()
            if memory_values:
                state = state.with_memory(memory_values)
            ctx = _WriteBeforeReadContext(state, candidate)
            ctx.set_memory("_dt", _SLICE_DT)
            execute_program(self.program, ctx, mode="natural")
            if ctx.entry_read_seen:
                return False

        return True

    def _prev_domain(self, name: str) -> tuple[Any, ...] | None:
        """Domain for an edge tag's ``_prev:`` memory key, or None to bail."""
        domain = self.stateful_dims.get(name) or self.nondeterministic_dims.get(name)
        if domain and PENDING not in domain:
            return tuple(domain)
        tag = self.graph.tags.get(name)
        if tag is not None and tag.type.name == "BOOL":
            return (False, True)
        return None


# ---------------------------------------------------------------------------
# Pipeline-compatible entry point
# ---------------------------------------------------------------------------


def _elide_sliced(
    program: Program,
    graph: ProgramGraph,
    stateful_dims: Mapping[str, tuple[Any, ...]],
    nondeterministic_dims: Mapping[str, tuple[Any, ...]],
    *,
    observer_exprs: tuple[Expr, ...] = (),
    observer_tag_names: frozenset[str] = frozenset(),
    projected_observers: frozenset[str] = frozenset(),
    progress: Callable[[str], None] | None = None,
    progress_prefix: Callable[[], str] | None = None,
    unclassified_tags: frozenset[str] = frozenset(),
    infeasible_out: set[str] | None = None,
) -> tuple[
    dict[str, tuple[Any, ...]],
    dict[str, str],
    dict[str, tuple[tuple[str, str], ...]],
    dict[str, _ExitSubstitution],
]:
    """Slice elision with the pipeline-compatible return signature.

    Returns ``(reduced_stateful_dims, elided_dict, proof_details, substitutions)``
    matching the contract of ``_elide_scan_local_stateful_dims``.
    """
    del projected_observers  # slice elision does not need scratch projection

    if not stateful_dims:
        return {}, {}, {}, {}

    use_dots = progress_prefix is not None
    if use_dots:
        assert progress_prefix is not None
        header = f"{progress_prefix()}elision | sliced {len(stateful_dims)} tags "
        print(header, end="", file=sys.stderr, flush=True)

    analyzer = _SliceElision(program, graph, stateful_dims, nondeterministic_dims)

    stateful_names = sorted(name for name, domain in stateful_dims.items() if PENDING not in domain)

    observer_seeds: set[str] = set(observer_tag_names)
    for expr in observer_exprs:
        observer_seeds.update(_referenced_tags(expr))

    elidable: dict[str, str] = {}
    proof_details: dict[str, tuple[tuple[str, str], ...]] = {}
    for name in stateful_names:
        ok, reason = analyzer.candidate_elidable(name)
        if ok:
            elidable[name] = "slice"
            proof_details[name] = (("slice", reason),)
        if use_dots:
            print("x" if ok else ".", end="", file=sys.stderr, flush=True)

    # Observer-referenced elidable tags need an exit-expression substitution so
    # the property can still be evaluated; those that cannot be substituted are
    # kept stateful.
    observer_elidable = {name for name in elidable if name in observer_seeds}
    substitutions: dict[str, _ExitSubstitution] = {}
    if observer_elidable:
        surviving = frozenset(nondeterministic_dims) | (
            frozenset(stateful_names) - frozenset(elidable)
        )
        substitutions = _compute_exit_substitutions(program, graph, observer_elidable, surviving)
        for name in observer_elidable - set(substitutions):
            del elidable[name]
            proof_details.pop(name, None)

    # An unclassified tag (no inferrable domain) only forces an Intractable
    # result when its *entry* value matters — i.e. it is not itself scan-local.
    # A scan-local unclassified tag is recomputed every scan and never needs a
    # domain, even when it feeds a surviving stateful tag or observer.
    if unclassified_tags and infeasible_out is not None:
        survivor_seeds = (frozenset(stateful_names) - frozenset(elidable)) | frozenset(
            observer_seeds
        )
        survivor_closure = _upstream_closure(graph, survivor_seeds)
        for name in unclassified_tags:
            if name not in survivor_closure:
                continue
            scan_local, _reason = analyzer.candidate_elidable(name)
            if not scan_local:
                infeasible_out.add(name)

    reduced: dict[str, tuple[Any, ...]] = {}
    elided: dict[str, str] = {}
    if use_dots:
        print(" ", end="", file=sys.stderr, flush=True)
    for name, domain in stateful_dims.items():
        if name in elidable:
            elided[name] = "slice"
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
            f"elision | sliced phase complete | removed={removed:,} | retained={len(reduced):,}"
        )

    return reduced, elided, proof_details, substitutions
