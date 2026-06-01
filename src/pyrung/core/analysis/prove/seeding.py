"""Heuristic domain seeding for the prove subsystem.

Unsound — seeds representative values for tags the static domain stack
cannot close.  Two strategies based on tag role:

**Stateful tags** (written internally): trace-observation — run scans from
the snapshot across ND input combos, collect all values the kernel produces,
expand +/- 1.

**Nondeterministic tags** (external inputs): behavioral bisection — spread
probe values across the type range, fingerprint the downstream behavior at
each probe, bisect between probes with differing fingerprints to discover
the partition boundaries.  Domain = one representative per behavioral
partition + boundary values +/- 1.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pdg import TagRole

from .kernel import _restore_kernel, _snapshot_kernel, _step_compiled_kernel

if TYPE_CHECKING:
    from pyrung.core.kernel import CompiledKernel
    from pyrung.core.tag import Tag

_INT_TYPE_RANGES: dict[str, tuple[int, int]] = {
    "INT": (-32768, 32767),
    "DINT": (-2147483648, 2147483647),
    "WORD": (0, 65535),
}

_BISECTION_MAX_DEPTH = 20
_BISECTION_SCANS = 10
_TRACE_SCANS = 20
_REAL_EPSILON = 0.001
_MAX_CROSS_DIM_VALUES = 7


def _thin_domain(domain: tuple[Any, ...], max_values: int) -> tuple[Any, ...]:
    """Subsample a domain to at most *max_values* evenly-spaced representatives."""
    if len(domain) <= max_values:
        return domain
    step = (len(domain) - 1) / (max_values - 1)
    indices = {round(i * step) for i in range(max_values)}
    indices.add(0)
    indices.add(len(domain) - 1)
    return tuple(domain[i] for i in sorted(indices))


def _initial_probes(tag: Tag) -> list[int | float]:
    """Generate spread probes across a tag's type range."""
    type_key = tag.type.name
    default = tag.default

    if type_key in _INT_TYPE_RANGES:
        lo, hi = _INT_TYPE_RANGES[type_key]
        probes: set[int | float] = {lo, hi, 0}
        if type_key != "WORD":
            probes.add(-1)
        probes.add(1)
        probes.add(default)
        for exp in (10, 100, 1000, 10000):
            probes.add(exp)
            if type_key != "WORD":
                probes.add(-exp)
        probes = {v for v in probes if lo <= v <= hi}
    elif type_key == "REAL":
        probes = {0.0, 1.0, -1.0, default}
        for exp in (10.0, 100.0, 1000.0, 10000.0):
            probes.add(exp)
            probes.add(-exp)
        for frac in (0.1, 0.5, 2.5):
            probes.add(frac)
            probes.add(-frac)
    else:
        probes = {0, default}

    return sorted(probes)


def _behavior_fingerprint(
    compiled: CompiledKernel,
    tag_name: str,
    value: int | float,
    dt: float,
    nd_combos: list[dict[str, Any]] | None = None,
) -> tuple[Any, ...]:
    """Run scans with a probe value and fingerprint downstream stateful state.

    When *nd_combos* is given, the fingerprint is the concatenation of
    per-combo sub-fingerprints — two probes are in the same behavioral
    partition only if they produce identical downstream state across
    every ND combo.
    """
    combos: list[dict[str, Any]] = nd_combos if nd_combos else [{}]
    all_parts: list[Any] = []

    for nd_values in combos:
        kernel = compiled.create_kernel()
        kernel.tags[tag_name] = value
        for n, v in nd_values.items():
            kernel.tags[n] = v
        snap = _snapshot_kernel(kernel)

        _step_compiled_kernel(compiled, kernel, dt=dt)
        after_one = dict(kernel.tags)
        _restore_kernel(kernel, snap)

        kernel.tags[tag_name] = value
        for n, v in nd_values.items():
            kernel.tags[n] = v
        for _s in range(_BISECTION_SCANS):
            kernel.tags[tag_name] = value
            _step_compiled_kernel(compiled, kernel, dt=dt)
        after_n = dict(kernel.tags)

        for k in sorted(after_one):
            if k == tag_name:
                continue
            all_parts.append((k, after_one[k], after_n[k]))

    return tuple(all_parts)


def _bisect_boundary(
    compiled: CompiledKernel,
    tag_name: str,
    lo: int | float,
    hi: int | float,
    dt: float,
    is_int: bool,
    nd_combos: list[dict[str, Any]] | None = None,
) -> list[int | float]:
    """Bisect between lo and hi to find the behavioral boundary value(s)."""
    results: list[int | float] = []
    for _ in range(_BISECTION_MAX_DEPTH):
        if is_int:
            if hi - lo <= 1:
                results.extend([int(lo), int(hi)])
                return results
            mid = int((lo + hi) // 2)
        else:
            if abs(hi - lo) < _REAL_EPSILON * 2:
                results.extend([lo, hi])
                return results
            mid = (lo + hi) / 2.0

        fp_lo = _behavior_fingerprint(compiled, tag_name, lo, dt, nd_combos)
        fp_mid = _behavior_fingerprint(compiled, tag_name, mid, dt, nd_combos)

        if fp_lo == fp_mid:
            lo = mid
        else:
            hi = mid

    results.extend([lo, hi])
    return results


def _single_flip_nd_combos(
    nd_dims: dict[str, tuple[Any, ...]],
) -> list[dict[str, Any]]:
    """Default + one flip per ND input."""
    nd_names = sorted(nd_dims)
    defaults = {n: nd_dims[n][0] for n in nd_names}
    combos: list[dict[str, Any]] = [dict(defaults)]
    for name in nd_names:
        for value in nd_dims[name][1:]:
            flipped = dict(defaults)
            flipped[name] = value
            combos.append(flipped)
    return combos


def _seed_nd_via_bisection(
    compiled: CompiledKernel,
    tags: dict[str, Tag],
    nondeterministic_dims: dict[str, tuple[Any, ...]],
    dt: float,
    candidates: list[str],
    discovered: dict[str, tuple[Any, ...]],
) -> None:
    """Discover behavioral partition boundaries for ND inputs via bisection."""
    for tag_name in candidates:
        tag = tags[tag_name]
        type_key = tag.type.name
        is_int = type_key in _INT_TYPE_RANGES
        probes = _initial_probes(tag)

        cross_dims: dict[str, tuple[Any, ...]] = dict(nondeterministic_dims)
        for prev_name, prev_domain in discovered.items():
            if prev_name != tag_name:
                cross_dims[prev_name] = _thin_domain(prev_domain, _MAX_CROSS_DIM_VALUES)
        nd_combos = _single_flip_nd_combos(cross_dims) if cross_dims else None

        fps: dict[int | float, tuple[Any, ...]] = {}
        for probe in probes:
            fps[probe] = _behavior_fingerprint(
                compiled,
                tag_name,
                probe,
                dt,
                nd_combos,
            )

        sorted_probes = sorted(fps)
        boundary_values: set[int | float] = set()

        for i, probe in enumerate(sorted_probes):
            if i > 0:
                prev = sorted_probes[i - 1]
                if fps[prev] != fps[probe]:
                    boundary = _bisect_boundary(
                        compiled,
                        tag_name,
                        prev,
                        probe,
                        dt,
                        is_int,
                        nd_combos,
                    )
                    boundary_values.update(boundary)

        domain_values: set[int | float] = set(boundary_values)
        domain_values.add(tag.default)

        discovered[tag_name] = tuple(sorted(domain_values))


def _seed_stateful_via_trace(
    compiled: CompiledKernel,
    tags: dict[str, Tag],
    nondeterministic_dims: dict[str, tuple[Any, ...]],
    dt: float,
    candidates: list[str],
    discovered: dict[str, tuple[Any, ...]],
) -> None:
    """Discover domains for stateful tags by running scans and observing values."""
    nd_combos = _single_flip_nd_combos(nondeterministic_dims)

    for tag_name in candidates:
        tag = tags[tag_name]
        type_key = tag.type.name
        observed: set[Any] = {tag.default}

        for nd_values in nd_combos:
            kernel = compiled.create_kernel()
            for n, v in nd_values.items():
                kernel.tags[n] = v

            for _scan in range(_TRACE_SCANS):
                _step_compiled_kernel(compiled, kernel, dt=dt)
                observed.add(kernel.tags.get(tag_name, tag.default))

        expanded: set[Any] = set(observed)
        for val in observed:
            if isinstance(val, int):
                expanded.add(val - 1)
                expanded.add(val + 1)
            elif isinstance(val, float):
                expanded.add(val - 1.0)
                expanded.add(val + 1.0)

        if type_key in _INT_TYPE_RANGES:
            lo, hi = _INT_TYPE_RANGES[type_key]
            expanded = {v for v in expanded if lo <= v <= hi}

        discovered[tag_name] = tuple(sorted(expanded))


def _seed_type_boundaries(
    tags: dict[str, Tag],
    candidates: list[str],
    discovered: dict[str, tuple[Any, ...]],
) -> None:
    """Fallback: seed with type boundaries when no compiled kernel is available."""
    for tag_name in candidates:
        tag = tags[tag_name]
        discovered[tag_name] = tuple(sorted(_initial_probes(tag)))


def _cross_seed_primer(tag: Tag) -> tuple[Any, ...]:
    """Small representative set for cross-dim priming.

    Just ``{default - 1, default, default + 1}`` — enough for bisection to
    detect behavioral differences without inflating the state space.
    """
    d = tag.default
    type_key = tag.type.name
    if type_key in _INT_TYPE_RANGES:
        lo, hi = _INT_TYPE_RANGES[type_key]
        vals = {d, d - 1, d + 1}
        return tuple(sorted(v for v in vals if lo <= v <= hi))
    return (d - 1.0, float(d), d + 1.0)


def _seed_comparison_partners(
    tags: dict[str, Tag],
    all_exprs: list[Any],
    candidates: list[str],
    discovered: dict[str, tuple[Any, ...]],
) -> None:
    """Cross-seed tag-vs-tag comparison partners.

    When two infeasible tags appear in the same comparison (``A > B``),
    bisection of A needs B's domain in the cross-dims (and vice versa).
    This primer breaks the chicken-and-egg dependency by pre-populating
    *discovered* with a small representative set before bisection runs.
    """
    from .expr import _build_atom_index

    candidate_set = frozenset(candidates)
    if not candidate_set:
        return

    atom_idx = _build_atom_index(all_exprs)
    comparison_forms = {"eq", "ne", "lt", "le", "gt", "ge"}
    seen_pairs: set[tuple[str, str]] = set()

    for tag_name in candidates:
        atoms = atom_idx.get(tag_name, [])
        for atom in atoms:
            if atom.form not in comparison_forms:
                continue
            if not isinstance(atom.operand, str):
                continue
            other = atom.operand if atom.tag == tag_name else atom.tag
            if other not in candidate_set:
                continue
            pair = (min(tag_name, other), max(tag_name, other))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)

            tag_a = tags.get(pair[0])
            tag_b = tags.get(pair[1])
            if tag_a is None or tag_b is None:
                continue

            for name, tag in ((pair[0], tag_a), (pair[1], tag_b)):
                if name not in discovered:
                    discovered[name] = _cross_seed_primer(tag)


def _discover_domains(
    infeasible_tags: list[str],
    tags: dict[str, Tag],
    tag_roles: dict[str, Any],
    writers_of: dict[str, Any],
    all_exprs: list[Any] | None,
    compiled: CompiledKernel | None,
    nondeterministic_dims: dict[str, tuple[Any, ...]] | None,
    dt: float,
    receive_dest_names: frozenset[str] = frozenset(),
) -> dict[str, tuple[Any, ...]]:
    """Run heuristic seeding on infeasible tags and return discovered domains.

    Shared by the main seeding pass and the post-elision seeding pass.
    Splits candidates into ND vs stateful, cross-seeds comparison partners,
    then runs bisection/trace/fallback as appropriate.
    """
    from pyrung.core.tag import TagType

    nd_candidates: list[str] = []
    stateful_candidates: list[str] = []
    for tag_name in infeasible_tags:
        tag = tags.get(tag_name)
        if tag is None or tag.type is TagType.BOOL:
            continue
        role = tag_roles.get(tag_name)
        is_written = tag_name in writers_of
        is_nd = role == TagRole.INPUT or not is_written or tag_name in receive_dest_names
        if is_nd:
            nd_candidates.append(tag_name)
        else:
            stateful_candidates.append(tag_name)

    if not nd_candidates and not stateful_candidates:
        return {}

    discovered: dict[str, tuple[Any, ...]] = {}

    if all_exprs is not None:
        all_candidates = nd_candidates + stateful_candidates
        _seed_comparison_partners(tags, all_exprs, all_candidates, discovered)

    nd_dims = nondeterministic_dims or {}

    if stateful_candidates and compiled is not None:
        _seed_stateful_via_trace(compiled, tags, nd_dims, dt, stateful_candidates, discovered)

    if nd_candidates and compiled is not None:
        _seed_nd_via_bisection(compiled, tags, nd_dims, dt, nd_candidates, discovered)
    elif nd_candidates:
        _seed_type_boundaries(tags, nd_candidates, discovered)

    return discovered
