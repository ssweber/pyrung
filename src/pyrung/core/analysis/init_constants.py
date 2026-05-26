"""Init-constant detection: tags with fixed values set at program start.

Detects tags written only under first-scan or monotonic latch guards with
literal values.  These tags have deterministic values after scan 0 and never
change again — they are evidence anchors in diagnosis and can be projected
out of the BFS state key in the prover.

Three detection patterns:

A. **Self-latching Bool guard** — a Bool tag written only via latch/literal-True,
   guarding other tags that are written with literal values.
B. **Co-latching nondeterministic guard** — a nondeterministic Bool input gating
   groups of literal writes.  Two or more tags guarded by the same input are
   projected to a representative.
C. **System first_scan guard** — tags written only under ``system.sys.first_scan``
   with literal values.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.program import Program


def find_instruction_at_site(program: Program, site: Any) -> Any:
    """Retrieve the instruction object at a WriteSite's location."""
    if site.subroutine is not None:
        rungs = program.subroutines.get(site.subroutine, [])
    else:
        rungs = program.rungs
    if site.rung_index >= len(rungs):
        return None
    rung = rungs[site.rung_index]
    for bi in site.branch_path:
        if bi >= len(rung._branches):
            return None
        rung = rung._branches[bi]
    items = rung._execution_items
    if site.instruction_index >= len(items):
        return None
    return items[site.instruction_index]


_NO_LITERAL_WRITE = object()


def _normalize_literal_write_value(raw_value: Any, target: Any) -> Any | object:
    """Return the concrete value stored by a literal copy/fill write."""
    from pyrung.core.expression import Expression
    from pyrung.core.instruction.conversions import _store_copy_value_to_tag_type
    from pyrung.core.tag import ImmediateRef, Tag

    value = raw_value.value if isinstance(raw_value, ImmediateRef) else raw_value
    if isinstance(value, (Tag, Expression)):
        return _NO_LITERAL_WRITE
    if not isinstance(value, (bool, int, float)):
        return _NO_LITERAL_WRITE
    return _store_copy_value_to_tag_type(value, target)


def _check_literal_write(
    program: Program,
    site: Any,
    tags: dict[str, Any],
    tag_name: str,
) -> Any | None:
    """Check if the write at this site is a literal value for *tag_name*.

    Returns the literal value, or ``None`` if the write is not literal.
    """
    from pyrung.core.instruction.data_transfer import CopyInstruction, FillInstruction

    instr = find_instruction_at_site(program, site)
    if instr is None:
        return None

    if isinstance(instr, CopyInstruction):
        if instr.convert is not None:
            return None
        raw_value = instr.source
    elif isinstance(instr, FillInstruction):
        raw_value = instr.value
    else:
        return None

    target = tags.get(tag_name)
    if target is None:
        return None
    stored = _normalize_literal_write_value(raw_value, target)
    if stored is _NO_LITERAL_WRITE:
        return None
    return stored


def detect_init_constants(
    program: Program,
    graph: ProgramGraph,
    sites_by_target: dict[str, list[Any]],
    candidate_tags: set[str],
    nondeterministic_inputs: set[str] | None = None,
    edge_source_tags: frozenset[str] | None = None,
) -> dict[str, tuple[str, str]]:
    """Detect tags with fixed init-time values.

    Args:
        program: The program.
        graph: The program dependency graph.
        sites_by_target: Pre-computed write sites grouped by target tag name.
        candidate_tags: Tags to consider for projection (e.g. stateful dims).
        nondeterministic_inputs: Boolean input tags (for Pattern B).
        edge_source_tags: Tags in ``rise()``/``fall()`` conditions (excluded
            from projection).  Defaults to empty.

    Returns:
        Mapping of ``tag_name -> (representative_tag, method)`` where method
        is one of ``"init_constant"``, ``"init_constant_colatch"``, or
        ``"init_constant_first_scan"``.
    """
    from pyrung.core.condition import BitCondition, NormallyClosedCondition
    from pyrung.core.tag import TagType

    edge_sources = edge_source_tags or frozenset()
    nd_inputs = nondeterministic_inputs or set()
    projected: dict[str, tuple[str, str]] = {}

    # --- Pattern A: self-latching Bool guard ---

    latch_tags: set[str] = set()
    for l_name in candidate_tags:
        tag = graph.tags.get(l_name)
        if tag is None or tag.type != TagType.BOOL:
            continue
        sites = sites_by_target.get(l_name, [])
        if not sites:
            continue
        is_monotonic = True
        for site in sites:
            if site.instruction_type == "LatchInstruction":
                continue
            val = _check_literal_write(program, site, graph.tags, l_name)
            if val is not True:
                is_monotonic = False
                break
        if is_monotonic:
            latch_tags.add(l_name)

    init_rung_indices: dict[str, set[int]] = {}
    for l_name in latch_tags:
        indices: set[int] = set()
        for ri, rung in enumerate(program.rungs):
            for cond in rung._conditions:
                if isinstance(cond, NormallyClosedCondition):
                    cond_tag = getattr(cond, "_resolved_tag", getattr(cond, "tag", None))
                    if cond_tag is not None and getattr(cond_tag, "name", None) == l_name:
                        indices.add(ri)
                        break
        if indices:
            init_rung_indices[l_name] = indices

    for l_name, rung_indices in init_rung_indices.items():
        for x_name in list(candidate_tags):
            if x_name == l_name or x_name in projected or x_name in edge_sources:
                continue
            x_sites = sites_by_target.get(x_name, [])
            if not x_sites:
                continue
            if any(site.subroutine is not None for site in x_sites):
                continue
            if not all(site.rung_index in rung_indices for site in x_sites):
                continue
            all_literal = True
            for site in x_sites:
                if _check_literal_write(program, site, graph.tags, x_name) is None:
                    all_literal = False
                    break
            if all_literal:
                projected[x_name] = (l_name, "init_constant")

    # --- Pattern B: co-latching nondeterministic guard ---

    nd_bool_guards: set[str] = set()
    for f_name in nd_inputs:
        tag = graph.tags.get(f_name)
        if tag is not None and tag.type == TagType.BOOL:
            nd_bool_guards.add(f_name)

    if nd_bool_guards:
        nd_guarded: dict[str, tuple[str, frozenset[int]]] = {}

        for x_name in list(candidate_tags):
            if x_name in projected or x_name in edge_sources:
                continue
            x_sites = sites_by_target.get(x_name, [])
            if not x_sites:
                continue
            if any(site.subroutine is not None for site in x_sites):
                continue

            guard_name: str | None = None
            rung_set: set[int] = set()
            valid = True

            for site in x_sites:
                if _check_literal_write(program, site, graph.tags, x_name) is None:
                    valid = False
                    break
                rung = program.rungs[site.rung_index]
                site_guard: str | None = None
                for cond in rung._conditions:
                    cond_tag = getattr(cond, "_resolved_tag", getattr(cond, "tag", None))
                    if cond_tag is None:
                        continue
                    cname = getattr(cond_tag, "name", None)
                    if cname in nd_bool_guards and isinstance(
                        cond, (BitCondition, NormallyClosedCondition)
                    ):
                        site_guard = cname
                        break
                if site_guard is None:
                    valid = False
                    break
                if guard_name is None:
                    guard_name = site_guard
                elif guard_name != site_guard:
                    valid = False
                    break
                rung_set.add(site.rung_index)

            if valid and guard_name is not None:
                nd_guarded[x_name] = (guard_name, frozenset(rung_set))

        groups: dict[tuple[str, frozenset[int]], list[str]] = {}
        for x_name, (guard, rungs) in nd_guarded.items():
            groups.setdefault((guard, rungs), []).append(x_name)

        for members in groups.values():
            if len(members) < 2:
                continue
            sorted_members = sorted(members)
            representative: str | None = None
            for m in sorted_members:
                tag = graph.tags.get(m)
                if tag is None:
                    continue
                m_sites = sites_by_target.get(m, [])
                lit_val = None
                for site in m_sites:
                    val = _check_literal_write(program, site, graph.tags, m)
                    if val is not None:
                        lit_val = val
                        break
                if lit_val is not None and lit_val != tag.default:
                    representative = m
                    break
            if representative is None:
                continue
            for m in sorted_members:
                if m != representative:
                    projected[m] = (representative, "init_constant_colatch")

    # --- Pattern C: system first_scan guard ---

    from pyrung.core.system_points import system

    first_scan_name = system.sys.first_scan.name
    fs_candidates: list[str] = []

    for x_name in list(candidate_tags):
        if x_name in projected or x_name in edge_sources:
            continue
        x_sites = sites_by_target.get(x_name, [])
        if not x_sites:
            continue
        all_fs_literal = True
        for site in x_sites:
            rung = (
                program.subroutines[site.subroutine][site.rung_index]
                if site.subroutine is not None
                else program.rungs[site.rung_index]
            )
            guard_ok = False
            for cond in rung._conditions:
                cond_tag = getattr(cond, "_resolved_tag", getattr(cond, "tag", None))
                if cond_tag is not None and getattr(cond_tag, "name", None) == first_scan_name:
                    guard_ok = True
                    break
            if not guard_ok:
                all_fs_literal = False
                break
            if _check_literal_write(program, site, graph.tags, x_name) is None:
                all_fs_literal = False
                break
        if all_fs_literal:
            fs_candidates.append(x_name)

    if len(fs_candidates) >= 2:
        fs_candidates.sort()
        representative = None
        for m in fs_candidates:
            tag = graph.tags.get(m)
            if tag is None:
                continue
            m_sites = sites_by_target.get(m, [])
            lit_val = None
            for site in m_sites:
                val = _check_literal_write(program, site, graph.tags, m)
                if val is not None:
                    lit_val = val
                    break
            if lit_val is not None and lit_val != tag.default:
                representative = m
                break
        if representative is not None:
            for m in fs_candidates:
                if m != representative:
                    projected[m] = (representative, "init_constant_first_scan")

    return projected
