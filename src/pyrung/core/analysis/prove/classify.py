"""Dimension classification and domain discovery for prove."""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pdg import TagRole, build_program_graph
from pyrung.core.analysis.simplified import Expr, _condition_to_expr, simplified_forms
from pyrung.core.kernel import CompiledKernel
from pyrung.core.tag import TagType

from . import PENDING, Intractable
from .absorb import (
    _DONE_KIND_COUNT_DOWN,
    _DONE_KIND_COUNT_UP,
    _DONE_KIND_OFF_DELAY,
    _DONE_KIND_ON_DELAY,
    _PROGRESS_KIND_INT_UP,
    _all_write_targets,
    _collect_done_acc_pairs,
    _find_redundant_acc_absorptions,
    _find_threshold_absorptions,
    _has_forbidden_data_read,
    _ThresholdBlocker,
)
from .expr import _collect_atoms_for_tag
from .kernel import _restore_kernel, _snapshot_kernel

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.program import Program
    from pyrung.core.tag import Tag

    from . import _ExploreContext

_OTE_INSTRUCTION = "OutInstruction"

_STATEFUL_INSTRUCTIONS = frozenset(
    {
        "LatchInstruction",
        "ResetInstruction",
        "OnDelayInstruction",
        "OffDelayInstruction",
        "CountUpInstruction",
        "CountDownInstruction",
        "CopyInstruction",
        "CalcInstruction",
        "ShiftInstruction",
        "EventDrumInstruction",
        "TimeDrumInstruction",
        "FunctionCallInstruction",
        "EnabledFunctionCallInstruction",
        "BlockCopyInstruction",
        "FillInstruction",
        "PackBitsInstruction",
        "PackWordsInstruction",
        "PackTextInstruction",
        "UnpackToBitsInstruction",
        "UnpackToWordsInstruction",
        "ModbusSendInstruction",
        "ModbusReceiveInstruction",
        "SearchInstruction",
        "ForLoopInstruction",
    }
)

_FUNCTION_INSTRUCTIONS = frozenset(
    {
        "FunctionCallInstruction",
        "EnabledFunctionCallInstruction",
    }
)

_TIMER_COUNTER_INSTRUCTIONS = frozenset(
    {
        "OnDelayInstruction",
        "OffDelayInstruction",
        "CountUpInstruction",
        "CountDownInstruction",
    }
)


def _collect_all_exprs(
    program: Program,
    graph: ProgramGraph,
    scope: list[str] | None = None,
) -> list[Expr]:
    """Collect all expression trees from simplified forms and write-site conditions.

    When *scope* is given, restricts to expressions in the upstream cone
    of the scoped tags.  This improves don't-care pruning without
    affecting soundness — cone-of-influence reduction and abstraction
    commute for safety properties, so absorption decisions made on the
    scoped set are sound even though out-of-cone reads are not visible.
    """
    forms = simplified_forms(program)

    upstream: frozenset[str] | None = None
    if scope is not None:
        upstream_tags: set[str] = set(scope)
        for tag_name in scope:
            upstream_tags.update(graph.upstream_slice(tag_name))
        upstream = frozenset(upstream_tags)
        forms = {k: v for k, v in forms.items() if k in upstream}

    exprs: list[Expr] = [tf.expr for tf in forms.values()]

    from pyrung.core.validation._common import _collect_write_sites

    sites = _collect_write_sites(program, target_extractor=_all_write_targets)
    for site in sites:
        if upstream is not None and site.target_name not in upstream:
            continue
        if site.conditions:
            for cond in site.conditions:
                exprs.append(_condition_to_expr(cond))
    return exprs


def _boundary_values_for_tag(other_tag: Tag) -> list[Any]:
    """Extract boundary-representative values from a tag's metadata.

    For tag-vs-tag comparisons like ``A > B``, we only need values that
    distinguish both comparison outcomes — not the full numeric range.
    """
    if other_tag.choices is not None:
        return sorted(other_tag.choices.keys())
    if other_tag.min is not None and other_tag.max is not None:
        vals: list[Any] = [other_tag.min]
        if other_tag.max != other_tag.min:
            vals.append(other_tag.max)
        return vals
    return []


_NO_LITERAL_WRITE = object()
_EQ_NE_OTHER = object()


def _has_non_condition_data_read(tag_name: str, graph: ProgramGraph | None) -> bool:
    """True when a tag participates in non-condition data flow."""
    if graph is None:
        return False
    return any(tag_name in node.data_reads for node in graph.rung_nodes)


def _normalize_literal_write_value(raw_value: Any, target: Tag) -> Any | object:
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


def _literal_write_values(
    instr: Any,
    tags: dict[str, Tag],
) -> dict[str, Any] | None:
    """Return normalized literal values for copy/fill targets, if any."""
    from pyrung.core.instruction.data_transfer import CopyInstruction, FillInstruction

    if isinstance(instr, CopyInstruction):
        if instr.convert is not None:
            return None
        raw_value = instr.source
    elif isinstance(instr, FillInstruction):
        raw_value = instr.value
    else:
        return None

    values: dict[str, Any] = {}
    for target_name, _itype in _all_write_targets(instr):
        target = tags.get(target_name)
        if target is None:
            return None
        stored = _normalize_literal_write_value(raw_value, target)
        if stored is _NO_LITERAL_WRITE:
            return None
        values[target_name] = stored
    return values


def _collect_literal_write_domains(
    program: Program,
    tags: dict[str, Tag],
) -> dict[str, tuple[Any, ...]]:
    """Infer exact finite domains for tags written only by literal copy/fill."""
    from pyrung.core.validation._common import walk_instructions

    literal_values_by_target: dict[str, set[Any]] = {}
    disqualified: set[str] = set()

    for instr in walk_instructions(program):
        targets = [name for name, _itype in _all_write_targets(instr)]
        if not targets:
            continue

        literal_values = _literal_write_values(instr, tags)
        for target_name in targets:
            if target_name in disqualified:
                continue
            if literal_values is None or target_name not in literal_values:
                disqualified.add(target_name)
                literal_values_by_target.pop(target_name, None)
                continue
            literal_values_by_target.setdefault(target_name, set()).add(literal_values[target_name])

    domains: dict[str, tuple[Any, ...]] = {}
    for target_name, values in literal_values_by_target.items():
        if target_name in disqualified:
            continue
        tag = tags.get(target_name)
        if tag is None:
            continue
        domains[target_name] = tuple(sorted({tag.default, *values}))
    return domains


def _declared_domain(tag: Tag) -> tuple[Any, ...] | None:
    """Return a direct metadata domain when one is finite and explicit."""
    if tag.type == TagType.BOOL:
        return (False, True)
    if tag.choices is not None:
        return tuple(sorted(tag.choices.keys()))
    if tag.min is None or tag.max is None:
        return None
    if not isinstance(tag.min, int | float) or not isinstance(tag.max, int | float):
        return None
    domain_size = tag.max - tag.min + 1
    if domain_size > 1000:
        return None
    return tuple(range(int(tag.min), int(tag.max) + 1))


def _tag_name_from_value(value: Any) -> str | None:
    """Extract a source tag name from a raw instruction operand/expression node."""
    from pyrung.core.expression import TagExpr
    from pyrung.core.tag import ImmediateRef, Tag

    raw = value.value if isinstance(value, ImmediateRef) else value
    if isinstance(raw, Tag):
        return raw.name
    if isinstance(raw, TagExpr):
        return raw.tag.name
    return None


def _literal_value_from_value(value: Any) -> Any | None:
    """Extract a plain literal value from a raw instruction operand/expression node."""
    from pyrung.core.expression import LiteralExpr
    from pyrung.core.tag import ImmediateRef

    raw = value.value if isinstance(value, ImmediateRef) else value
    if isinstance(raw, LiteralExpr):
        return raw.value
    if isinstance(raw, (bool, int, float)):
        return raw
    return None


def _domain_for_source_tag(
    tag_name: str,
    graph: ProgramGraph,
    all_exprs: list[Expr],
    known_domains: dict[str, tuple[Any, ...]],
) -> tuple[Any, ...] | None:
    """Resolve the best current finite domain for a source tag."""
    if tag_name in known_domains:
        return known_domains[tag_name]

    tag = graph.tags.get(tag_name)
    if tag is None:
        return None
    if not tag.external and tag_name not in graph.writers_of:
        return (tag.default,)

    domain = _extract_value_domain(
        tag_name,
        tag,
        all_exprs,
        graph.tags,
        known_domains=known_domains,
        graph=graph,
    )
    if domain:
        return domain
    return _declared_domain(tag)


def _domain_from_copy_like_value(
    raw_value: Any,
    target: Tag,
    graph: ProgramGraph,
    all_exprs: list[Expr],
    known_domains: dict[str, tuple[Any, ...]],
) -> tuple[Any, ...] | None:
    """Infer a target domain from a copy/fill-style source operand."""
    source_tag_name = _tag_name_from_value(raw_value)
    if source_tag_name is not None:
        return _domain_for_source_tag(source_tag_name, graph, all_exprs, known_domains)

    literal = _literal_value_from_value(raw_value)
    if literal is None:
        return None
    stored = _normalize_literal_write_value(literal, target)
    if stored is _NO_LITERAL_WRITE:
        return None
    return (stored,)


def _domain_from_calc_expression(
    expression: Any,
    target: Tag,
    graph: ProgramGraph,
    all_exprs: list[Expr],
    known_domains: dict[str, tuple[Any, ...]],
) -> tuple[Any, ...] | None:
    """Infer a target domain from a supported calc expression shape."""
    from pyrung.core.expression import BinaryExpr

    direct = _domain_from_copy_like_value(expression, target, graph, all_exprs, known_domains)
    if direct is not None:
        return direct

    if not isinstance(expression, BinaryExpr) or expression.symbol != "%":
        return None
    modulus = _literal_value_from_value(expression.right)
    if not isinstance(modulus, int) or isinstance(modulus, bool) or modulus <= 0 or modulus > 1000:
        return None
    return tuple(range(modulus))


def _domain_from_write_instruction(
    instr: Any,
    target_name: str,
    target: Tag,
    graph: ProgramGraph,
    all_exprs: list[Expr],
    known_domains: dict[str, tuple[Any, ...]],
) -> tuple[Any, ...] | None:
    """Infer a target domain from one supported writer instruction."""
    from pyrung.core.instruction.calc import CalcInstruction
    from pyrung.core.instruction.data_transfer import CopyInstruction, FillInstruction

    if isinstance(instr, CopyInstruction):
        if instr.convert is not None:
            return None
        return _domain_from_copy_like_value(instr.source, target, graph, all_exprs, known_domains)

    if isinstance(instr, FillInstruction):
        return _domain_from_copy_like_value(instr.value, target, graph, all_exprs, known_domains)

    if isinstance(instr, CalcInstruction):
        if instr.dest.name != target_name:
            return None
        return _domain_from_calc_expression(
            instr.expression,
            target,
            graph,
            all_exprs,
            known_domains,
        )

    return None


def _collect_structural_domains(
    program: Program,
    graph: ProgramGraph,
    all_exprs: list[Expr],
    literal_write_domains: dict[str, tuple[Any, ...]] | None = None,
) -> dict[str, tuple[Any, ...]]:
    """Discover finite domains from structural writes via fixed-point propagation."""
    from pyrung.core.validation._common import walk_instructions

    known_domains = dict(
        literal_write_domains or _collect_literal_write_domains(program, graph.tags)
    )
    for tag_name, tag in graph.tags.items():
        if not tag.external and tag_name not in graph.writers_of:
            known_domains.setdefault(tag_name, (tag.default,))

    by_target: dict[str, list[Any]] = {}
    for instr in walk_instructions(program):
        for target_name, _itype in _all_write_targets(instr):
            by_target.setdefault(target_name, []).append(instr)

    changed = True
    while changed:
        changed = False
        for target_name, writers in by_target.items():
            target = graph.tags.get(target_name)
            if target is None or not writers:
                continue

            candidate_values: set[Any] = set()
            for instr in writers:
                domain = _domain_from_write_instruction(
                    instr,
                    target_name,
                    target,
                    graph,
                    all_exprs,
                    known_domains,
                )
                if domain is None:
                    candidate_values = set()
                    break
                candidate_values.update(domain)

            if not candidate_values:
                continue

            merged_values = set(known_domains.get(target_name, ()))
            merged_values.update(candidate_values)
            if len(merged_values) > 1000:
                continue

            merged = tuple(sorted(merged_values))
            if known_domains.get(target_name) != merged:
                known_domains[target_name] = merged
                changed = True

    return known_domains


def _extract_value_domain(
    tag_name: str,
    tag: Tag,
    all_exprs: list[Expr],
    all_tags: dict[str, Tag] | None = None,
    literal_write_domains: dict[str, tuple[Any, ...]] | None = None,
    known_domains: dict[str, tuple[Any, ...]] | None = None,
    graph: ProgramGraph | None = None,
) -> tuple[Any, ...] | None:
    """Determine the finite value domain for a tag, or None if unbounded."""
    if literal_write_domains is not None and tag_name in literal_write_domains:
        return literal_write_domains[tag_name]
    if known_domains is not None and tag_name in known_domains:
        return known_domains[tag_name]

    atoms = _collect_atoms_for_tag(all_exprs, tag_name)
    if not atoms:
        return ()

    if tag.type == TagType.BOOL:
        return (False, True)

    if tag.choices is None and not (tag.min is not None and tag.max is not None):
        eq_ne_literals = {
            atom.operand
            for atom in atoms
            if atom.form in {"eq", "ne"}
            and atom.operand is not None
            and not isinstance(atom.operand, str)
        }
        if (
            eq_ne_literals
            and all(
                atom.form in {"eq", "ne"}
                and atom.operand is not None
                and not isinstance(atom.operand, str)
                for atom in atoms
            )
            and not _has_non_condition_data_read(tag_name, graph)
        ):
            return tuple(sorted(eq_ne_literals)) + (_EQ_NE_OTHER,)

    comparison_forms = {"eq", "ne", "lt", "le", "gt", "ge"}
    literals: set[Any] = set()
    unresolved_tag_comparison = False

    for atom in atoms:
        if atom.form in comparison_forms and atom.operand is not None:
            if isinstance(atom.operand, str):
                if known_domains is not None and atom.operand in known_domains:
                    boundary = list(known_domains[atom.operand])
                else:
                    other = all_tags.get(atom.operand) if all_tags is not None else None
                    boundary = _boundary_values_for_tag(other) if other is not None else []
                if boundary:
                    literals.update(boundary)
                else:
                    unresolved_tag_comparison = True
            else:
                literals.add(atom.operand)

    if tag.choices is not None:
        return tuple(sorted(tag.choices.keys()))

    if tag.min is not None and tag.max is not None:
        domain_size = tag.max - tag.min + 1
        if literals:
            literals.add(tag.min)
            literals.add(tag.max)
        elif domain_size > 1000:
            return None
        else:
            return tuple(range(int(tag.min), int(tag.max) + 1))

    if unresolved_tag_comparison and not literals:
        return None

    if not literals:
        return ()

    partitioned: set[Any] = set()
    for lit in literals:
        partitioned.add(lit)
        if isinstance(lit, (int, float)):
            partitioned.add(lit - 1)
            partitioned.add(lit + 1)
    if tag.min is not None:
        partitioned = {v for v in partitioned if v >= tag.min}
    if tag.max is not None:
        partitioned = {v for v in partitioned if v <= tag.max}
    return tuple(sorted(partitioned))


def _is_ote_only(tag_name: str, graph: ProgramGraph) -> bool:
    """True if every writer of *tag_name* uses OutInstruction."""
    writer_indices = graph.writers_of.get(tag_name, frozenset())
    if not writer_indices:
        return False
    return all(tag_name in graph.rung_nodes[ni].ote_writes for ni in writer_indices)


_ClassifyResult = tuple[
    dict[str, tuple[Any, ...]],  # stateful_dims
    dict[str, tuple[Any, ...]],  # nondeterministic_dims
    frozenset[str],  # combinational_tags
    dict[str, str],  # done_acc_pairs: done_tag → acc_tag
    dict[str, int],  # done_presets: done_tag → constant preset value
    dict[str, str],  # done_kinds: done_tag → timer/counter instruction kind
]

_KIND_LABELS: dict[str, str] = {
    _DONE_KIND_ON_DELAY: "on-delay timer",
    _DONE_KIND_OFF_DELAY: "off-delay timer",
    _DONE_KIND_COUNT_UP: "count-up counter",
    _DONE_KIND_COUNT_DOWN: "count-down counter",
    _PROGRESS_KIND_INT_UP: "integer progress",
}


def _build_infeasible_hints(
    infeasible_tags: list[str],
    graph: ProgramGraph,
    threshold_blockers: dict[str, _ThresholdBlocker] | None = None,
) -> list[str]:
    """Generate actionable hints for each infeasible tag."""
    blockers = threshold_blockers or {}
    hints: list[str] = []
    for name in infeasible_tags:
        blocker = blockers.get(name)
        if blocker is not None:
            label = _KIND_LABELS.get(blocker.kind, "accumulator")
            hints.append(f"  {name}: {label} — threshold abstraction blocked:")
            for reason in blocker.reasons:
                hints.append(f"    {reason}")
            continue
        tag = graph.tags.get(name)
        ptr_info = graph.pointer_tags.get(name)
        if ptr_info is not None:
            block_name, start, end = ptr_info
            hints.append(
                f"  {name}: pointer into {block_name}[{start}..{end}]"
                f" — add choices=, min={start}/max={end}, or readonly=True"
            )
        elif tag is not None and tag.min is not None and tag.max is not None:
            hints.append(
                f"  {name}: range {tag.min}..{tag.max} ({int(tag.max - tag.min + 1)} values)"
                f" — too wide; add choices=, narrow min=/max=, or readonly=True"
            )
        else:
            hints.append(
                f"  {name}: no domain constraint — add choices=, min=/max=, or readonly=True"
            )
    return hints


def _build_dimension_hints(context: _ExploreContext) -> list[str]:
    """Summarise the largest dimensions when max_states is exceeded."""
    dims: list[tuple[str, int]] = []
    for name, domain in context.stateful_dims.items():
        dims.append((name, len(domain)))
    for name, domain in context.nondeterministic_dims.items():
        dims.append((name, len(domain)))
    dims.sort(key=lambda x: x[1], reverse=True)
    product = 1
    for _, size in dims:
        product *= size
    hints = [f"  state space: {product:,} combinations across {len(dims)} dimensions"]
    for name, size in dims[:10]:
        ptr_info = context.graph.pointer_tags.get(name)
        suffix = ""
        if ptr_info is not None:
            block_name, start, end = ptr_info
            suffix = f" (pointer into {block_name}[{start}..{end}])"
        hints.append(f"  {name}: {size} values{suffix}")
    if len(dims) > 10:
        hints.append(f"  ... and {len(dims) - 10} more")
    hints.append("Constrain the largest dimensions with choices=, min=/max=, or readonly=True")
    return hints


def _classify_dimensions_from_graph(
    program: Program,
    graph: ProgramGraph,
    all_exprs: list[Expr],
    *,
    scope: list[str] | None = None,
    project: tuple[str, ...] | None = None,
    discovered_domains: dict[str, tuple[Any, ...]] | None = None,
) -> _ClassifyResult | Intractable:
    """Classify dimensions using prebuilt graph/expression context."""
    done_acc_info = _collect_done_acc_pairs(program)
    literal_write_domains = _collect_literal_write_domains(program, graph.tags)
    structural_domains = _collect_structural_domains(
        program,
        graph,
        all_exprs,
        literal_write_domains,
    )
    known_domains = dict(structural_domains)
    if discovered_domains is not None:
        known_domains.update(discovered_domains)

    consumed_accs: set[str] = set()
    for acc_name in done_acc_info.pairs.values():
        if _collect_atoms_for_tag(all_exprs, acc_name) or _has_forbidden_data_read(
            program,
            acc_name,
        ):
            consumed_accs.add(acc_name)

    absorptions = _find_redundant_acc_absorptions(
        program,
        graph,
        all_exprs,
        done_acc_info,
        consumed_accs,
    )
    consumed_accs.difference_update(absorptions.acc_names)

    threshold_absorptions = _find_threshold_absorptions(
        program,
        graph,
        all_exprs,
        project=project,
    )
    consumed_accs.difference_update(threshold_absorptions.progress_names)

    done_acc = {d: a for d, a in done_acc_info.pairs.items() if a not in consumed_accs}
    unconsumed_accs = frozenset(done_acc.values())

    scope_input_tags: frozenset[str] | None = None
    if scope is not None:
        dv = program.dataview()
        upstream_tags: set[str] = set()
        for tag_name in scope:
            upstream_tags.update(dv.upstream(tag_name).inputs().tags)
        scope_input_tags = frozenset(upstream_tags | set(scope))

    stateful: dict[str, tuple[Any, ...]] = {}
    nondeterministic: dict[str, tuple[Any, ...]] = {}
    combinational: set[str] = set()
    infeasible_tags: list[str] = []

    for tag_name, tag in graph.tags.items():
        if tag.readonly:
            continue

        if tag_name in unconsumed_accs:
            continue
        if tag_name in absorptions.preset_tags:
            continue
        if tag_name in threshold_absorptions.progress_names:
            continue
        if tag_name in threshold_absorptions.threshold_tags:
            continue

        role = graph.tag_roles.get(tag_name)
        is_written = tag_name in graph.writers_of

        if not tag.external and not is_written:
            continue

        if role == TagRole.INPUT or (tag.external and not is_written):
            if scope_input_tags is not None and tag_name not in scope_input_tags:
                continue
            domain = _extract_value_domain(
                tag_name,
                tag,
                all_exprs,
                graph.tags,
                literal_write_domains,
                known_domains,
                graph,
            )
            if domain is None:
                infeasible_tags.append(tag_name)
                continue
            if domain:
                nondeterministic[tag_name] = domain
            continue

        if not is_written:
            continue

        if tag_name not in graph.readers_of:
            combinational.add(tag_name)
            continue

        if _is_ote_only(tag_name, graph):
            combinational.add(tag_name)
            continue

        if tag_name in done_acc:
            stateful[tag_name] = (False, PENDING, True)
            continue

        if tag_name in done_acc_info.pairs.values() and tag_name in consumed_accs:
            if not _collect_atoms_for_tag(all_exprs, tag_name):
                if tag_name in known_domains:
                    stateful[tag_name] = known_domains[tag_name]
                else:
                    infeasible_tags.append(tag_name)
                continue

        domain = _extract_value_domain(
            tag_name,
            tag,
            all_exprs,
            graph.tags,
            literal_write_domains,
            known_domains,
            graph,
        )
        if domain is None:
            if tag_name in known_domains:
                stateful[tag_name] = known_domains[tag_name]
            else:
                infeasible_tags.append(tag_name)
            continue
        if domain:
            stateful[tag_name] = domain

    if infeasible_tags:
        total_dims = len(stateful) + len(nondeterministic) + len(infeasible_tags)
        blocker_map = {b.acc_name: b for b in threshold_absorptions.blockers}
        hints = _build_infeasible_hints(sorted(infeasible_tags), graph, blocker_map)
        return Intractable(
            reason=f"unbounded domain on {', '.join(sorted(infeasible_tags))}",
            dimensions=total_dims,
            estimated_space=0,
            tags=sorted(infeasible_tags),
            hints=hints,
        )

    fn_escape = _detect_function_escape_hatches(program, graph)
    if fn_escape:
        total_dims = len(stateful) + len(nondeterministic) + len(fn_escape)
        hints = [
            f"  {name}: function output — add choices=, min=/max=, or readonly=True"
            for name in sorted(fn_escape)
        ]
        return Intractable(
            reason=f"unannotated function output: {', '.join(sorted(fn_escape))}",
            dimensions=total_dims,
            estimated_space=0,
            tags=sorted(fn_escape),
            hints=hints,
        )

    done_presets = {d: p for d, p in done_acc_info.presets.items() if d in done_acc}
    done_presets.update({d: p for d, p in absorptions.synthetic_presets.items() if d in done_acc})
    done_kinds = {d: done_acc_info.kinds[d] for d in done_acc}
    return (
        stateful,
        nondeterministic,
        frozenset(combinational),
        done_acc,
        done_presets,
        done_kinds,
    )


def _classify_dimensions(
    program: Program,
    scope: list[str] | None = None,
) -> _ClassifyResult | Intractable:
    """Partition tags into stateful, nondeterministic, and combinational.

    Returns ``(stateful_dims, nondeterministic_dims, combinational_tags,
    done_acc_pairs, done_presets, done_kinds)`` where each dim dict maps
    tag name to its value domain.
    Timer/counter Done bits get a three-valued domain ``(False, PENDING, True)``
    and their Acc tags are excluded from the state space.
    Returns ``Intractable`` when a domain cannot be bounded.
    """
    graph = build_program_graph(program)
    all_exprs = _collect_all_exprs(program, graph, scope=scope)
    return _classify_dimensions_from_graph(program, graph, all_exprs, scope=scope)


def _has_data_feedback(tag_name: str, graph: ProgramGraph) -> bool:
    """Detect data-flow cycles through *tag_name*.

    Follows ``data_reads`` through writer rungs — condition-only reads do
    not count as data feedback.  Returns True for direct self-feed
    (e.g. ``calc(Count + 1, Count)``) and transitive cycles
    (e.g. ``calc(A + 1, B)`` plus ``copy(B, A)``).
    """
    writer_indices = graph.writers_of.get(tag_name, frozenset())
    if not writer_indices:
        return False
    for wi in writer_indices:
        node = graph.rung_nodes[wi]
        if tag_name in node.data_reads:
            return True
    visited: set[str] = set()
    queue: list[str] = []
    for wi in writer_indices:
        node = graph.rung_nodes[wi]
        for src in node.data_reads:
            if src != tag_name and src not in visited:
                queue.append(src)
    while queue:
        current = queue.pop()
        if current in visited:
            continue
        visited.add(current)
        for wi in graph.writers_of.get(current, frozenset()):
            node = graph.rung_nodes[wi]
            if tag_name in node.writes:
                return True
            for src in node.data_reads:
                if src not in visited:
                    queue.append(src)
    return False


def _pilot_sweep_domains(
    compiled: CompiledKernel,
    infeasible_tags: list[str],
    nondeterministic_dims: dict[str, tuple[Any, ...]],
    graph: ProgramGraph,
    *,
    dt: float = 0.010,
    max_combos: int = 100_000,
    max_domain: int = 1000,
    max_scans: int = 30,
) -> dict[str, tuple[Any, ...]]:
    """Discover finite domains for infeasible tags via kernel pilot sweep."""
    candidates: list[str] = []
    for tag_name in infeasible_tags:
        tag = graph.tags.get(tag_name)
        if tag is None:
            continue
        if tag.readonly:
            continue
        if tag_name not in graph.writers_of:
            if not (tag.external and tag.final):
                continue
        if tag.external and not tag.final:
            continue
        if _has_data_feedback(tag_name, graph):
            continue
        candidates.append(tag_name)

    if not candidates:
        return {}

    candidate_upstream: dict[str, dict[str, tuple[Any, ...]]] = {}
    for cname in candidates:
        upstream: dict[str, tuple[Any, ...]] = {}
        visited_rungs: set[int] = set()
        queue: list[str] = [cname]
        visited_tags: set[str] = set()
        while queue:
            cur = queue.pop()
            if cur in visited_tags:
                continue
            visited_tags.add(cur)
            for wi in graph.writers_of.get(cur, frozenset()):
                if wi in visited_rungs:
                    continue
                visited_rungs.add(wi)
                node = graph.rung_nodes[wi]
                for src in node.condition_reads | node.data_reads:
                    if src not in visited_tags:
                        queue.append(src)
        for t in visited_tags:
            if t in nondeterministic_dims:
                upstream[t] = nondeterministic_dims[t]
        candidate_upstream[cname] = upstream

    relevant_nd: dict[str, tuple[Any, ...]] = {}
    for up in candidate_upstream.values():
        for t, domain in up.items():
            if t not in relevant_nd:
                relevant_nd[t] = domain

    combo_count = 1
    for domain in relevant_nd.values():
        combo_count *= len(domain)
        if combo_count > max_combos:
            break

    if combo_count > max_combos:
        return {}

    observed: dict[str, set[Any]] = {c: set() for c in candidates}
    for c in candidates:
        tag = graph.tags[c]
        observed[c].add(tag.default)

    edge_tag_names = tuple(compiled.edge_tags)
    nd_names = sorted(relevant_nd)
    nd_domains = [relevant_nd[n] for n in nd_names]

    initial_kernel = compiled.create_kernel()
    initial_kernel.memory["_dt"] = dt
    for spec in compiled.block_specs.values():
        initial_kernel.load_block_from_tags(spec)
    initial_snap = _snapshot_kernel(initial_kernel)

    kernel = initial_kernel
    for combo in itertools.product(*nd_domains) if nd_domains else [()]:
        _restore_kernel(kernel, initial_snap)
        for name, val in zip(nd_names, combo, strict=True):
            kernel.tags[name] = val
        kernel.memory["_dt"] = dt
        for spec in compiled.block_specs.values():
            kernel.load_block_from_tags(spec)

        for _scan in range(max_scans):
            prev_sizes = tuple(len(observed[c]) for c in candidates)
            compiled.step_fn(kernel.tags, kernel.blocks, kernel.memory, kernel.prev, dt)
            for spec in compiled.block_specs.values():
                kernel.flush_block_to_tags(spec)
            for c in candidates:
                observed[c].add(kernel.tags.get(c, graph.tags[c].default))
            for name in edge_tag_names:
                if name in kernel.tags:
                    kernel.prev[name] = kernel.tags[name]
            kernel.advance(dt)
            new_sizes = tuple(len(observed[c]) for c in candidates)
            if new_sizes == prev_sizes:
                break

    result: dict[str, tuple[Any, ...]] = {}
    for c in candidates:
        vals = observed[c]
        if len(vals) > max_domain:
            continue
        result[c] = tuple(sorted(vals))
    return result


def _detect_function_escape_hatches(
    program: Program,
    graph: ProgramGraph,
) -> list[str]:
    """Find function output tags that lack domain annotations."""
    from pyrung.core.instruction.control import (
        EnabledFunctionCallInstruction,
        FunctionCallInstruction,
    )
    from pyrung.core.tag import Tag
    from pyrung.core.validation._common import _collect_write_sites

    fn_targets: set[str] = set()

    def _fn_write_targets(instr: Any) -> list[tuple[str, str]]:
        if not isinstance(instr, (FunctionCallInstruction, EnabledFunctionCallInstruction)):
            return []
        targets: list[str] = []
        for target in instr._outs.values():
            if isinstance(target, Tag):
                targets.append(target.name)
        return [(name, type(instr).__name__) for name in targets]

    sites = _collect_write_sites(program, target_extractor=_fn_write_targets)
    for site in sites:
        fn_targets.add(site.target_name)

    infeasible: list[str] = []
    for name in fn_targets:
        tag = graph.tags.get(name)
        if tag is None:
            continue
        if tag.type == TagType.BOOL:
            continue
        if tag.choices is not None or (tag.min is not None and tag.max is not None):
            continue
        infeasible.append(name)
    return infeasible
