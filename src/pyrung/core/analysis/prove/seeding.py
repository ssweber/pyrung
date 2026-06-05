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
    initial_state: dict[str, Any] | None = None,
) -> tuple[Any, ...]:
    """Run scans with a probe value and fingerprint downstream stateful state.

    When *nd_combos* is given, the fingerprint is the concatenation of
    per-combo sub-fingerprints — two probes are in the same behavioral
    partition only if they produce identical downstream state across
    every ND combo.

    Real-typed tags are excluded from the fingerprint because continuous
    values (e.g. ``calc(100 - level, pv)``) produce a unique fingerprint
    for every distinct probe, creating spurious partition boundaries.
    The behaviorally meaningful boundaries come from discrete state
    changes (Bool, Int, counter/timer Done bits).
    """
    from pyrung.core.tag import TagType

    real_tags = frozenset(
        name for name, tag in compiled.referenced_tags.items() if tag.type is TagType.REAL
    )

    combos: list[dict[str, Any]] = nd_combos if nd_combos else [{}]
    all_parts: list[Any] = []

    for nd_values in combos:
        kernel = compiled.create_kernel()
        if initial_state is not None:
            for n, v in initial_state.items():
                if n in kernel.tags:
                    kernel.tags[n] = v
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
            if k == tag_name or k in real_tags:
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
    initial_state: dict[str, Any] | None = None,
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

        fp_lo = _behavior_fingerprint(compiled, tag_name, lo, dt, nd_combos, initial_state)
        fp_mid = _behavior_fingerprint(compiled, tag_name, mid, dt, nd_combos, initial_state)

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


def _snapshot_probes(tag: Tag, snapshot_value: int | float) -> list[int | float]:
    """Neighbor probes around a non-default snapshot value."""
    type_key = tag.type.name
    vals: set[int | float] = {snapshot_value}
    if type_key in _INT_TYPE_RANGES:
        lo, hi = _INT_TYPE_RANGES[type_key]
        for delta in (1, 2, 5, 10):
            v = snapshot_value + delta
            if v <= hi:
                vals.add(v)
            v = snapshot_value - delta
            if v >= lo:
                vals.add(v)
    else:
        for delta in (0.001, 0.01, 0.1, 0.5, 1.0, 5.0, 10.0):
            vals.add(snapshot_value + delta)
            vals.add(snapshot_value - delta)
    return sorted(vals)


def _seed_nd_via_bisection(
    compiled: CompiledKernel,
    tags: dict[str, Tag],
    nondeterministic_dims: dict[str, tuple[Any, ...]],
    dt: float,
    candidates: list[str],
    discovered: dict[str, tuple[Any, ...]],
    initial_state: dict[str, Any] | None = None,
) -> None:
    """Discover behavioral partition boundaries for ND inputs via bisection."""
    for tag_name in candidates:
        tag = tags[tag_name]
        type_key = tag.type.name
        is_int = type_key in _INT_TYPE_RANGES
        probes = _initial_probes(tag)

        if initial_state is not None and tag_name in initial_state:
            snap_val = initial_state[tag_name]
            if snap_val != tag.default:
                probes = sorted(set(probes) | set(_snapshot_probes(tag, snap_val)))

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
                initial_state,
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
                        initial_state,
                    )
                    boundary_values.update(boundary)

        domain_values: set[int | float] = set(boundary_values)
        domain_values.add(tag.default)
        if initial_state is not None and tag_name in initial_state:
            domain_values.add(initial_state[tag_name])

        discovered[tag_name] = tuple(sorted(domain_values))


def _seed_stateful_via_trace(
    compiled: CompiledKernel,
    tags: dict[str, Tag],
    nondeterministic_dims: dict[str, tuple[Any, ...]],
    dt: float,
    candidates: list[str],
    discovered: dict[str, tuple[Any, ...]],
    initial_state: dict[str, Any] | None = None,
) -> None:
    """Discover domains for stateful tags by running scans and observing values."""
    nd_combos = _single_flip_nd_combos(nondeterministic_dims)

    for tag_name in candidates:
        tag = tags[tag_name]
        type_key = tag.type.name
        observed: set[Any] = {tag.default}
        if initial_state is not None and tag_name in initial_state:
            observed.add(initial_state[tag_name])

        for nd_values in nd_combos:
            kernel = compiled.create_kernel()
            if initial_state is not None:
                for n, v in initial_state.items():
                    if n in kernel.tags:
                        kernel.tags[n] = v
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


def _expand_comparison_domains(
    tags: dict[str, Tag],
    all_exprs: list[Any],
    discovered: dict[str, tuple[Any, ...]],
) -> None:
    """Expand domains so each side of a tag-vs-tag comparison spans the partner's values.

    After bisection, if A and B appear in ``A >= B`` and A's domain is {0.5}
    while B's domain is {0.0}, A can never cross the comparison boundary.
    This adds values on both sides of each partner value (+/- neighbors) so
    BFS can explore both sides of every comparison threshold.
    """
    from .expr import _build_atom_index

    comparison_forms = {"eq", "ne", "lt", "le", "gt", "ge"}
    discovered_set = frozenset(discovered)
    if not discovered_set:
        return

    atom_idx = _build_atom_index(all_exprs)
    pairs: set[tuple[str, str]] = set()
    for tag_name in list(discovered):
        for atom in atom_idx.get(tag_name, []):
            if atom.form not in comparison_forms:
                continue
            if not isinstance(atom.operand, str):
                continue
            other = atom.operand if atom.tag == tag_name else atom.tag
            if other not in discovered_set:
                continue
            pairs.add((min(tag_name, other), max(tag_name, other)))

    for name_a, name_b in pairs:
        tag_a = tags.get(name_a)
        tag_b = tags.get(name_b)
        if tag_a is None or tag_b is None:
            continue
        domain_a = set(discovered[name_a])
        domain_b = set(discovered[name_b])
        type_key_a = tag_a.type.name
        type_key_b = tag_b.type.name

        for val in discovered[name_b]:
            _add_neighbors(domain_a, val, type_key_a)
        for val in discovered[name_a]:
            _add_neighbors(domain_b, val, type_key_b)

        if type_key_a in _INT_TYPE_RANGES:
            lo, hi = _INT_TYPE_RANGES[type_key_a]
            domain_a = {v for v in domain_a if lo <= v <= hi}
        if type_key_b in _INT_TYPE_RANGES:
            lo, hi = _INT_TYPE_RANGES[type_key_b]
            domain_b = {v for v in domain_b if lo <= v <= hi}

        discovered[name_a] = tuple(sorted(domain_a))
        discovered[name_b] = tuple(sorted(domain_b))


def _add_neighbors(
    domain: set[int | float],
    value: int | float,
    type_key: str,
) -> None:
    """Add *value* and its immediate neighbors to *domain*."""
    domain.add(value)
    if type_key in _INT_TYPE_RANGES:
        domain.add(int(value) - 1)
        domain.add(int(value) + 1)
    else:
        domain.add(value - _REAL_EPSILON)
        domain.add(value + _REAL_EPSILON)
        domain.add(value - 1.0)
        domain.add(value + 1.0)


def _range_fill_arithmetic_writers(
    discovered: dict[str, tuple[Any, ...]],
    program: Any,
    graph: Any,
) -> None:
    """Fill domain gaps for tags with arithmetic writers (increment/decrement).

    For each seeded tag, if any writer returns ("increment", step) or
    ("decrement", step), fill the range between min and max observed
    domain values at that stride.  Tags whose writers are all literal
    are left as-is — the literals are the complete domain.
    """
    from pyrung.core.analysis.prove.waypoints import _resolve_rung, _written_value_for_tag

    for tag_name in list(discovered):
        domain = discovered[tag_name]
        if not domain:
            continue

        numeric_vals = [v for v in domain if isinstance(v, (int, float))]
        if len(numeric_vals) < 2:
            continue

        writers = graph.writers_of.get(tag_name, frozenset())
        if not writers:
            continue

        best_step: int | float | None = None
        for ri in writers:
            node = graph.rung_nodes[ri]
            rung_obj = _resolve_rung(program, node)
            if rung_obj is None:
                continue
            wv = _written_value_for_tag(rung_obj, tag_name)
            if wv is None:
                continue
            if wv[0] in ("increment", "decrement"):
                if best_step is None or wv[1] < best_step:
                    best_step = wv[1]

        if best_step is None:
            continue

        step = best_step
        if not isinstance(step, (int, float)) or step <= 0:
            continue

        lo = min(numeric_vals)
        hi = max(numeric_vals)
        filled: set[Any] = set(domain)
        v = lo
        while v <= hi:
            filled.add(int(v) if isinstance(step, int) and isinstance(lo, int) else v)
            v += step
        discovered[tag_name] = tuple(sorted(filled))


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
    initial_state: dict[str, Any] | None = None,
    program: Any = None,
    graph: Any = None,
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
        _seed_stateful_via_trace(
            compiled, tags, nd_dims, dt, stateful_candidates, discovered, initial_state
        )

    if program is not None and graph is not None and discovered:
        _range_fill_arithmetic_writers(discovered, program, graph)

    if nd_candidates and compiled is not None:
        _seed_nd_via_bisection(
            compiled, tags, nd_dims, dt, nd_candidates, discovered, initial_state
        )
    elif nd_candidates:
        _seed_type_boundaries(tags, nd_candidates, discovered)

    if all_exprs is not None:
        _expand_comparison_domains(tags, all_exprs, discovered)

    return discovered
