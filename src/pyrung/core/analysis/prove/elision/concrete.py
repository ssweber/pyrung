"""Concrete kernel proofs for state-key elision."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from itertools import product
from typing import TYPE_CHECKING, Any, cast

from pyrung.core.analysis.pdg import ProgramGraph
from pyrung.core.kernel import CompiledKernel
from pyrung.core.tag import Tag, TagType

from ..inputs import _detect_exclusive_input_groups, _exclusive_input_group_membership
from ..kernel import _step_compiled_kernel
from ..results import PENDING

if TYPE_CHECKING:
    from pyrung.core.program import Program

    from . import _ElisionContext


_ELISION_ENUM_LIMIT = 200_000

_FORCED_TRUE_COMBO_LIMIT = 4_096

_DEFAULT_DT = 0.010

_MEMORY_EXCLUDED_PREFIXES = ("_dt", "_frac:", "_oneshot:")

# Batch removal proves candidates against one retained snapshot per round.
# Lower _ELISION_PROOF_BUDGET to skip medium-cost proofs when tuning startup time.
_ELISION_BATCH_REMOVE = True

_ELISION_PROOF_BUDGET = _ELISION_ENUM_LIMIT

_SCAN_CACHE_LIMIT = 500_000
_SCAN_CACHE_MISS: object = object()


# ---------------------------------------------------------------------------
# Phase 2: Concrete kernel elision
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ForcedTrueCoverage:
    written_tags: frozenset[str]
    varied_tags: tuple[str, ...]
    truncated: bool = False


def _domain_from_tag_metadata(tag: Tag) -> tuple[Any, ...] | None:
    if tag.type == TagType.BOOL:
        return (False, True)
    if tag.choices is not None:
        return tuple(sorted(tag.choices.keys()))
    if tag.min is None or tag.max is None:
        return None
    try:
        size = int(tag.max - tag.min + 1)
    except Exception:
        return None
    if 0 < size <= 256:
        return tuple(range(int(tag.min), int(tag.max) + 1))
    return None


def _product_size(domains: tuple[tuple[Any, ...], ...]) -> int:
    total = 1
    for domain in domains:
        total *= len(domain)
    return total


def _is_fault_tag(tag_name: str) -> bool:
    return tag_name.startswith("fault.")


def _alternate_seed_value(tag: Tag) -> Any:
    default = tag.default
    if tag.type == TagType.BOOL:
        return not bool(default)
    if tag.choices is not None:
        for value in sorted(tag.choices.keys()):
            if value != default:
                return value
    if tag.min is not None and tag.max is not None:
        try:
            min_value = int(tag.min)
            max_value = int(tag.max)
            if default != min_value:
                return min_value
            if default != max_value:
                return max_value
        except Exception:
            pass
    if tag.type in {TagType.INT, TagType.DINT, TagType.WORD}:
        return 1 if default != 1 else 0
    if tag.type == TagType.REAL:
        return 1.0 if default != 1.0 else 0.0
    if tag.type == TagType.CHAR:
        return "__alt__" if default != "__alt__" else ""
    return default


def _seed_profile(compiled: CompiledKernel, *, alternate: bool) -> dict[str, Any]:
    seeded: dict[str, Any] = {}
    for name, tag in compiled.referenced_tags.items():
        seeded[name] = _alternate_seed_value(tag) if alternate else tag.default
    return seeded


def _coverage_domain_items(
    graph: ProgramGraph,
    stateful_dims: Mapping[str, tuple[Any, ...]],
    nondeterministic_dims: Mapping[str, tuple[Any, ...]],
    *,
    combo_limit: int,
) -> tuple[tuple[str, tuple[Any, ...]], ...]:
    known_domains = dict(stateful_dims)
    known_domains.update(nondeterministic_dims)
    if not known_domains:
        return ()

    all_items = tuple(sorted(known_domains.items()))
    if _product_size(tuple(domain for _name, domain in all_items)) <= combo_limit:
        return all_items

    selected: set[str] = set(nondeterministic_dims)
    for pointer_name in graph.pointer_tags:
        if pointer_name in known_domains:
            selected.add(pointer_name)
        selected.update(graph.upstream_slice(pointer_name) & set(known_domains))

    if not selected:
        return ()

    items = tuple(sorted((name, known_domains[name]) for name in selected))
    if _product_size(tuple(domain for _name, domain in items)) <= combo_limit:
        return items

    reduced = tuple(sorted((name, known_domains[name]) for name in nondeterministic_dims))
    return reduced


def _collect_forced_true_coverage(
    program: Program,
    graph: ProgramGraph,
    stateful_dims: Mapping[str, tuple[Any, ...]],
    nondeterministic_dims: Mapping[str, tuple[Any, ...]],
    *,
    compiled: CompiledKernel | None = None,
    combo_limit: int = _FORCED_TRUE_COMBO_LIMIT,
) -> _ForcedTrueCoverage:
    from pyrung.circuitpy.codegen import compile_kernel

    forced_compiled = compiled or compile_kernel(
        program,
        force_rung_enable=True,
        blockless=True,
    )
    domain_items = _coverage_domain_items(
        graph,
        stateful_dims,
        nondeterministic_dims,
        combo_limit=combo_limit,
    )
    varied_tags = tuple(name for name, _domain in domain_items)
    combo_space = (
        _product_size(tuple(domain for _name, domain in domain_items)) if domain_items else 1
    )
    truncated = (combo_space * 2) > combo_limit

    written_tags: set[str] = set()
    domain_values = [domain for _name, domain in domain_items]
    seed_profiles = (False, True)
    remaining_budget = combo_limit
    for seed_index, alternate in enumerate(seed_profiles):
        if remaining_budget <= 0:
            break
        seeds_left = len(seed_profiles) - seed_index
        combo_budget = (
            remaining_budget
            if combo_space <= remaining_budget
            else max(1, (remaining_budget + seeds_left - 1) // seeds_left)
        )
        seed = _seed_profile(forced_compiled, alternate=alternate)
        processed = 0
        combo_iter = product(*domain_values) if domain_values else [()]
        for combo in combo_iter:
            if processed >= combo_budget or remaining_budget <= 0:
                break
            kernel = forced_compiled.create_kernel()
            kernel.tags.update(seed)
            entry_values = dict(zip(varied_tags, combo, strict=True))
            kernel.tags.update(entry_values)
            before = {name: kernel.tags.get(name) for name in forced_compiled.referenced_tags}
            _step_compiled_kernel(forced_compiled, kernel, dt=_DEFAULT_DT)
            for name in forced_compiled.referenced_tags:
                if kernel.tags.get(name) != before.get(name):
                    written_tags.add(name)
            processed += 1
            remaining_budget -= 1

    return _ForcedTrueCoverage(
        written_tags=frozenset(written_tags),
        varied_tags=varied_tags,
        truncated=truncated,
    )


class _ConcreteStateElider:
    def __init__(
        self,
        program: Program,
        graph: ProgramGraph,
        stateful_dims: Mapping[str, tuple[Any, ...]],
        nondeterministic_dims: Mapping[str, tuple[Any, ...]],
        *,
        state_basis: frozenset[str] | None = None,
        compiled: CompiledKernel | None = None,
        progress: Callable[[str], None] | None = None,
        progress_prefix: Callable[[], str] | None = None,
    ) -> None:
        from pyrung.circuitpy.codegen import compile_kernel

        self._program = program
        self._graph = graph
        self._stateful_dims = dict(stateful_dims)
        self._state_basis = (
            frozenset(self._stateful_dims)
            if state_basis is None
            else frozenset(state_basis) & frozenset(self._stateful_dims)
        )
        self._nondeterministic_dims = dict(nondeterministic_dims)
        self._progress = progress
        self._progress_prefix = progress_prefix
        self._compiled = compiled or compile_kernel(program, blockless=True)
        self._entry_sensitive_cache: dict[tuple[str, frozenset[str]], bool] = {}
        self._scan_cache: dict[
            tuple[tuple[tuple[str, Any], ...], tuple[str, ...]], tuple[Any, ...] | None
        ] = {}
        self._baseline_registry: dict[
            tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]],
            dict[tuple[tuple[str, Any], ...], tuple[Any, ...]],
        ] = {}
        self._coverage = _collect_forced_true_coverage(
            program,
            graph,
            stateful_dims,
            nondeterministic_dims,
        )
        self._exclusive_input_groups = _detect_exclusive_input_groups(
            program,
            graph,
            self._nondeterministic_dims,
            project=tuple(self._stateful_dims),
            extra_exprs=None,
        )
        self._exclusive_input_group_by_member = _exclusive_input_group_membership(
            self._exclusive_input_groups
        )
        static_writers = set(graph.writers_of) & set(self._stateful_dims)
        dynamic_writers = set(self._coverage.written_tags) & set(self._stateful_dims)
        self._written_tags = frozenset(static_writers | dynamic_writers)
        self._continued_source_tags = self._find_continued_source_tags()

        # Discover kernel memory keys via pilot scans.  Instructions like
        # oneshot OTE store hidden state in kernel.memory that fresh kernels
        # do not reproduce.  We sweep ND input combinations (using both
        # default and alternate seeds) to find a memory snapshot where at
        # least one key differs from the fresh-kernel default.
        self._warm_memory: dict[str, Any] | None = None
        mem_keys: list[str] = []
        nd_names = sorted(self._nondeterministic_dims)
        nd_domains = [self._nondeterministic_dims[n] for n in nd_names]
        combo_iter = product(*nd_domains) if nd_domains else [()]
        fresh_memory: dict[str, Any] = {}
        warm_found = False
        pilot_budget = _FORCED_TRUE_COMBO_LIMIT
        pilots_run = 0
        for combo in combo_iter:
            if pilots_run >= pilot_budget:
                break
            for alt in (False, True):
                pilot = self._compiled.create_kernel()
                if alt:
                    pilot.tags.update(_seed_profile(self._compiled, alternate=True))
                for name, val in zip(nd_names, combo, strict=True):
                    pilot.tags[name] = val
                _step_compiled_kernel(self._compiled, pilot, dt=_DEFAULT_DT)
                pilots_run += 1
                found = [
                    k
                    for k in pilot.memory
                    if not any(k.startswith(p) for p in _MEMORY_EXCLUDED_PREFIXES)
                ]
                if found and not mem_keys:
                    mem_keys = found
                    fresh_memory = {k: pilot.memory.get(k) for k in found}
                if found and not warm_found:
                    for k in found:
                        if pilot.memory.get(k) != fresh_memory.get(k):
                            self._warm_memory = dict(pilot.memory)
                            warm_found = True
                            break
            if warm_found:
                break

        self._emit(
            "elision | setup complete"
            f" | stateful={len(self._stateful_dims):,}"
            f" | candidates={len(self._candidate_names()):,}"
            f" | forced-true={'truncated' if self._coverage.truncated else 'complete'}"
            f" | input-groups={len(self._exclusive_input_groups)}"
            f" | memory_keys={len(mem_keys)}"
        )

    def elide(self) -> dict[str, tuple[Any, ...]]:
        import sys

        retained = set(self._state_basis)
        never_written = self._never_written_elidable(retained)
        if never_written:
            retained.difference_update(never_written)
            self._emit(f"elision | fast-path: {len(never_written)} never-written tag(s) elided")

        use_dots = self._progress_prefix is not None
        dots: list[str] = []
        n_candidates = len(self._ordered_candidates(retained))

        if use_dots:
            assert self._progress_prefix is not None
            header = f"{self._progress_prefix()}elision | concrete {n_candidates} tags "
            print(header, end="", file=sys.stderr, flush=True)

        changed = True
        while changed:
            changed = False
            snapshot = set(retained)
            candidates = self._ordered_candidates(snapshot)
            removable: list[str] = []
            for tag_name in candidates:
                compare_retained = frozenset(snapshot - {tag_name})
                if not self._can_elide(tag_name, compare_retained):
                    if use_dots:
                        print(".", end="", file=sys.stderr, flush=True)
                        dots.append(".")
                    continue
                if _ELISION_BATCH_REMOVE:
                    removable.append(tag_name)
                    if use_dots:
                        print("x", end="", file=sys.stderr, flush=True)
                        dots.append("x")
                    continue
                retained.remove(tag_name)
                changed = True
                if use_dots:
                    print("x", end="", file=sys.stderr, flush=True)
                    dots.append("x")
            if _ELISION_BATCH_REMOVE and removable:
                retained.difference_update(removable)
                changed = True

        removed = len(self._state_basis) - len(retained)
        if use_dots:
            print(f"  removed={removed}", file=sys.stderr)
        else:
            self._emit(
                "elision | concrete phase complete"
                f" | removed={removed:,}"
                f" | retained={len(retained):,}"
                f" | scan_cache={len(self._scan_cache):,}"
                f" | baseline_groups={len(self._baseline_registry):,}"
            )
        return {name: domain for name, domain in self._stateful_dims.items() if name in retained}

    def _find_continued_source_tags(self) -> frozenset[str]:
        """Tags read via continued() rungs — their cross-scan value is observable."""
        sources: set[str] = set()
        for node in self._graph.rung_nodes:
            if node.scope == "main":
                rung = self._program.rungs[node.rung_index]
            else:
                assert node.subroutine is not None
                rung = self._program.subroutines[node.subroutine][node.rung_index]
            if not getattr(rung, "_use_prior_snapshot", False):
                continue
            for read_tag in node.condition_reads | node.data_reads:
                if read_tag in self._stateful_dims:
                    sources.add(read_tag)
        return frozenset(sources)

    def _is_concrete_candidate(self, name: str) -> bool:
        """True when the tag is eligible for concrete elision proofs."""
        if name not in self._state_basis:
            return False
        if name not in self._written_tags:
            return False
        # Tags with PENDING in their domain use abstract three-valued logic
        # (timer/counter Done bits).  The concrete kernel cannot execute with
        # the PENDING sentinel, so these tags must be retained unconditionally.
        if PENDING in self._stateful_dims.get(name, ()):
            return False
        # Tags read via continued() have observable cross-scan state — their
        # previous-scan value flows into the current scan's outputs.  The
        # concrete frontier traversal misses combinational observers, so
        # retain unconditionally.
        if name in self._continued_source_tags:
            return False
        return True

    def _never_written_elidable(self, retained: set[str]) -> list[str]:
        """Tags that are never written — their value is always default, trivially elidable."""
        result: list[str] = []
        for name in sorted(retained):
            if name not in self._state_basis:
                continue
            if name in self._written_tags:
                continue
            if PENDING in self._stateful_dims.get(name, ()):
                continue
            if name in self._continued_source_tags:
                continue
            result.append(name)
        return result

    def _candidate_names(self) -> tuple[str, ...]:
        return tuple(
            sorted(name for name in self._stateful_dims if self._is_concrete_candidate(name))
        )

    def _ordered_candidates(self, retained: set[str]) -> tuple[str, ...]:
        return tuple(
            sorted(
                (name for name in retained if self._is_concrete_candidate(name)),
                key=lambda name: (
                    len(self._graph.downstream_slice(name) & retained),
                    len(self._stateful_dims[name]),
                    name,
                ),
            )
        )

    def _emit(self, message: str) -> None:
        if self._progress is not None:
            self._progress(message)

    def _input_assignment_dimensions(
        self,
        input_names: tuple[str, ...],
    ) -> tuple[tuple[tuple[tuple[str, Any], ...], ...], ...]:
        dimensions: list[tuple[tuple[tuple[str, Any], ...], ...]] = []
        live_inputs = set(input_names)
        seen_groups: set[int] = set()

        for name in sorted(input_names):
            group_index = self._exclusive_input_group_by_member.get(name)
            if group_index is not None:
                if group_index in seen_groups:
                    continue
                seen_groups.add(group_index)
                group = self._exclusive_input_groups[group_index]
                options: list[tuple[tuple[str, Any], ...]] = []
                seen_options: set[tuple[tuple[str, Any], ...]] = set()
                for canonical in group.canonical_assignments:
                    filtered = tuple(
                        (member, value) for member, value in canonical if member in live_inputs
                    )
                    if filtered in seen_options:
                        continue
                    seen_options.add(filtered)
                    options.append(filtered)
                if options:
                    dimensions.append(tuple(options))
                continue

            dimensions.append(
                tuple(((name, value),) for value in self._nondeterministic_dims[name])
            )

        return tuple(dimensions)

    def _has_self_referencing_write(self, name: str) -> bool:
        """True when any rung that writes the tag also reads it."""
        for rung_idx in self._graph.writers_of.get(name, frozenset()):
            node = self._graph.rung_nodes[rung_idx]
            if name in node.data_reads or name in node.condition_reads:
                return True
        return False

    def _can_elide(self, candidate: str, retained: frozenset[str]) -> bool:
        observed, fallback_hidden = self._reachable_stateful_frontier(candidate, retained)
        if not observed:
            sticky_hidden = tuple(
                name for name in fallback_hidden if self._hidden_entry_matters(name, retained)
            )
            if not sticky_hidden:
                if (
                    self._has_self_referencing_write(candidate)
                    or candidate in self._graph.readers_of
                ):
                    observed = (candidate,)
                else:
                    return True
            else:
                observed = sticky_hidden

        retained_names, input_names, hidden_stateful = self._scoped_dependencies(
            candidate,
            observed,
            retained,
        )
        hidden_stateful = tuple(n for n in hidden_stateful if n != candidate)

        retained_domains = tuple(self._stateful_dims[name] for name in retained_names)
        input_assignment_dimensions = self._input_assignment_dimensions(input_names)
        input_combo_count = 1
        for dimension in input_assignment_dimensions:
            input_combo_count *= len(dimension)
        group_product = _product_size(retained_domains) * input_combo_count
        vary_names = hidden_stateful + (candidate,)
        vary_domains = tuple(self._stateful_dims[name] for name in hidden_stateful) + (
            self._stateful_dims[candidate],
        )
        proof_limit = min(_ELISION_ENUM_LIMIT, _ELISION_PROOF_BUDGET)
        if group_product * _product_size(vary_domains) > proof_limit:
            return False

        scope_key = (retained_names, input_names, observed, hidden_stateful)
        registered_baselines = self._baseline_registry.get(scope_key)
        new_baselines: dict[tuple[tuple[str, Any], ...], tuple[Any, ...]] | None = (
            None if registered_baselines is not None else {}
        )

        retained_iter = product(*retained_domains) if retained_domains else [()]
        for retained_values in retained_iter:
            retained_entry = dict(zip(retained_names, retained_values, strict=True))
            input_iter = (
                product(*input_assignment_dimensions) if input_assignment_dimensions else [()]
            )
            for input_assignments in input_iter:
                entry_values = dict(retained_entry)
                for partial_assignment in input_assignments:
                    entry_values.update(partial_assignment)

                baseline_key = tuple(sorted(entry_values.items()))
                expected: tuple[Any, ...] | None = None
                if registered_baselines is not None:
                    expected = registered_baselines.get(baseline_key)

                vary_iter_values = product(*vary_domains) if vary_domains else [()]
                for vary_values in vary_iter_values:
                    full_entry = dict(entry_values)
                    full_entry.update(dict(zip(vary_names, vary_values, strict=True)))
                    outcome = self._scan(full_entry, observed)
                    if outcome is None:
                        return False
                    if expected is None:
                        expected = outcome
                        if new_baselines is not None:
                            new_baselines[baseline_key] = outcome
                        continue
                    if outcome != expected:
                        return False

        if new_baselines is not None and new_baselines:
            self._baseline_registry[scope_key] = new_baselines
        return True

    def _reachable_stateful_frontier(
        self,
        candidate: str,
        retained: frozenset[str],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        reachable_retained: set[str] = set()
        reachable_hidden: set[str] = set()
        retained_set = set(retained)
        stateful_names = set(self._stateful_dims)
        visited: set[str] = {candidate}
        queue: deque[str] = deque([candidate])

        while queue:
            current = queue.popleft()
            if _is_fault_tag(current):
                continue
            for rung_idx in self._graph.readers_of.get(current, frozenset()):
                node = self._graph.rung_nodes[rung_idx]
                for written_tag in node.writes:
                    if _is_fault_tag(written_tag):
                        continue
                    if written_tag in retained_set:
                        reachable_retained.add(written_tag)
                        continue
                    if written_tag in stateful_names and written_tag != candidate:
                        reachable_hidden.add(written_tag)
                    if written_tag in visited:
                        continue
                    visited.add(written_tag)
                    queue.append(written_tag)

        return tuple(sorted(reachable_retained)), tuple(sorted(reachable_hidden))

    def _hidden_entry_matters(self, tag_name: str, retained: frozenset[str]) -> bool:
        cache_key = (tag_name, retained)
        cached = self._entry_sensitive_cache.get(cache_key)
        if cached is not None:
            return cached

        upstream = set(self._graph.upstream_slice(tag_name))
        cone = upstream | {tag_name}
        retained_names = tuple(sorted(set(retained) & upstream))
        input_names = tuple(sorted(upstream & set(self._nondeterministic_dims)))
        hidden_names = tuple(
            sorted((cone & set(self._stateful_dims)) - set(retained_names) - {tag_name})
        )

        fixed_domains = (
            tuple(self._stateful_dims[name] for name in retained_names)
            + tuple(self._nondeterministic_dims[name] for name in input_names)
            + tuple(self._stateful_dims[name] for name in hidden_names)
        )
        tag_domain = self._stateful_dims[tag_name]
        if _product_size(fixed_domains + (tag_domain,)) > _ELISION_ENUM_LIMIT:
            self._entry_sensitive_cache[cache_key] = True
            return True

        fixed_names = retained_names + input_names + hidden_names
        fixed_iter = product(*fixed_domains) if fixed_domains else [()]
        for fixed_values in fixed_iter:
            base_entry = dict(zip(fixed_names, fixed_values, strict=True))
            expected: tuple[Any, ...] | None = None
            for tag_value in tag_domain:
                full_entry = dict(base_entry)
                full_entry[tag_name] = tag_value
                outcome = self._scan(full_entry, (tag_name,))
                if outcome is None:
                    self._entry_sensitive_cache[cache_key] = True
                    return True
                if expected is None:
                    expected = outcome
                    continue
                if outcome != expected:
                    self._entry_sensitive_cache[cache_key] = True
                    return True

        self._entry_sensitive_cache[cache_key] = False
        return False

    def _is_retained_anchor(self, tag_name: str) -> bool:
        tag = self._graph.tags.get(tag_name)
        if tag is None:
            return False
        if tag_name not in self._written_tags:
            return True
        return bool(tag.external or tag.final)

    def _scoped_dependencies(
        self,
        candidate: str,
        observed: tuple[str, ...],
        retained: frozenset[str],
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        retained_set = set(retained)
        observed_set = set(observed)
        retained_names = set(observed_set & retained_set)
        input_names: set[str] = set()
        hidden_stateful = set(observed_set - retained_set)
        queue: deque[str] = deque([candidate, *observed])
        visited: set[str] = set(queue)

        while queue:
            current = queue.popleft()
            if _is_fault_tag(current):
                continue
            for rung_idx in self._graph.writers_of.get(current, frozenset()):
                node = self._graph.rung_nodes[rung_idx]
                for src in node.condition_reads | node.data_reads:
                    if _is_fault_tag(src):
                        continue
                    if src in self._nondeterministic_dims:
                        input_names.add(src)
                        continue
                    if src in retained_set and self._is_retained_anchor(src):
                        retained_names.add(src)
                        continue
                    if src in self._state_basis and src not in retained_set and src != candidate:
                        hidden_stateful.add(src)
                    if src in visited:
                        continue
                    visited.add(src)
                    queue.append(src)

        return (
            tuple(sorted(retained_names)),
            tuple(sorted(input_names)),
            tuple(sorted(hidden_stateful)),
        )

    def _scan(
        self,
        entry_values: Mapping[str, Any],
        observed: tuple[str, ...],
    ) -> tuple[Any, ...] | None:
        cache_key = (tuple(sorted(entry_values.items())), observed)
        cached = self._scan_cache.get(cache_key, _SCAN_CACHE_MISS)
        if cached is not _SCAN_CACHE_MISS:
            return cast("tuple[Any, ...] | None", cached)

        kernel = self._compiled.create_kernel()
        kernel.tags.update(entry_values)
        _step_compiled_kernel(self._compiled, kernel, dt=_DEFAULT_DT)
        result = tuple(kernel.tags.get(name) for name in observed)

        if self._warm_memory is not None:
            warm_kernel = self._compiled.create_kernel()
            warm_kernel.tags.update(entry_values)
            warm_kernel.memory.update(self._warm_memory)
            _step_compiled_kernel(self._compiled, warm_kernel, dt=_DEFAULT_DT)
            warm_result = tuple(warm_kernel.tags.get(name) for name in observed)
            if warm_result != result:
                if len(self._scan_cache) < _SCAN_CACHE_LIMIT:
                    self._scan_cache[cache_key] = None
                return None

        if len(self._scan_cache) < _SCAN_CACHE_LIMIT:
            self._scan_cache[cache_key] = result
        return result


# ---------------------------------------------------------------------------
# Elision pass pipeline component
# ---------------------------------------------------------------------------


def _pass_concrete_batch(ctx: _ElisionContext) -> None:
    if not ctx.stateful_dims:
        return
    # Use the original full stateful_dims for observer coverage —
    # abstract-removed tags still act as same-scan observers.
    concrete_elider = _ConcreteStateElider(
        ctx.program,
        ctx.graph,
        ctx._original_stateful_dims,
        ctx.nondeterministic_dims,
        state_basis=frozenset(ctx.stateful_dims),
        compiled=ctx.compiled,
        progress=ctx.progress,
        progress_prefix=ctx.progress_prefix,
    )
    result = concrete_elider.elide()
    for tag_name in list(ctx.stateful_dims):
        if tag_name not in result:
            del ctx.stateful_dims[tag_name]
            ctx.elided[tag_name] = "concrete"
