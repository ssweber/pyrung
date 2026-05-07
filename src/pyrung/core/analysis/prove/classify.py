"""Dimension classification and domain discovery for prove."""

from __future__ import annotations

import itertools
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pdg import TagRole, build_program_graph
from pyrung.core.analysis.simplified import Expr, _condition_to_expr, simplified_forms
from pyrung.core.kernel import CompiledKernel
from pyrung.core.tag import TagType

from .absorb import (
    _DONE_KIND_COUNT_DOWN,
    _DONE_KIND_COUNT_UP,
    _DONE_KIND_OFF_DELAY,
    _DONE_KIND_ON_DELAY,
    _PROGRESS_KIND_INT_DOWN,
    _PROGRESS_KIND_INT_UP,
    _all_write_targets,
    _collect_done_acc_pairs,
    _find_comparison_absorptions,
    _find_redundant_acc_absorptions,
    _find_threshold_absorptions,
    _has_forbidden_data_read,
    _merge_threshold_absorptions,
    _ThresholdBlocker,
)
from .expr import _build_atom_index, _collect_atoms_for_tag, _referenced_tags
from .kernel import _restore_kernel, _snapshot_kernel, _step_compiled_kernel
from .results import PENDING, Intractable

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph, RungNode
    from pyrung.core.analysis.simplified import Atom
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

    from pyrung.core.analysis.pdg import _implicit_fault_writes
    from pyrung.core.validation._common import (
        _build_caller_map,
        _caller_conditions,
        _collect_write_sites,
    )

    tag_refs = dict(graph.tags)

    def _write_targets_with_faults(instr: Any) -> list[tuple[str, str]]:
        seen: set[tuple[str, str]] = set(_all_write_targets(instr))
        instr_type = type(instr).__name__
        for fault_name in sorted(_implicit_fault_writes(instr, tag_refs)):
            seen.add((fault_name, instr_type))
        return sorted(seen)

    sites = _collect_write_sites(program, target_extractor=_write_targets_with_faults)
    caller_map = _build_caller_map(program)
    for site in sites:
        if upstream is not None and site.target_name not in upstream:
            continue
        if site.conditions:
            for cond in site.conditions:
                exprs.append(_condition_to_expr(cond))
        if site.scope == "subroutine":
            for caller_chain in _caller_conditions(site, caller_map):
                for cond in caller_chain:
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
    atom_index: dict[str, list[Atom]] | None = None,
) -> tuple[Any, ...] | None:
    """Resolve the best current finite domain for a source tag."""
    if tag_name in known_domains:
        return known_domains[tag_name]

    tag = graph.tags.get(tag_name)
    if tag is None:
        return None
    if (
        not tag.external
        and tag_name not in graph.writers_of
        and not graph.is_physical_input(tag_name)
    ):
        return (tag.default,)

    domain = _extract_value_domain(
        tag_name,
        tag,
        all_exprs,
        graph.tags,
        known_domains=known_domains,
        graph=graph,
        atom_index=atom_index,
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
    atom_index: dict[str, list[Atom]] | None = None,
) -> tuple[Any, ...] | None:
    """Infer a target domain from a copy/fill-style source operand."""
    source_tag_name = _tag_name_from_value(raw_value)
    if source_tag_name is not None:
        return _domain_for_source_tag(source_tag_name, graph, all_exprs, known_domains, atom_index)

    literal = _literal_value_from_value(raw_value)
    if literal is None:
        return None
    stored = _normalize_literal_write_value(literal, target)
    if stored is _NO_LITERAL_WRITE:
        return None
    return (stored,)


def _interval_bounds(
    node: Any,
    graph: ProgramGraph,
    all_exprs: list[Expr],
    known_domains: dict[str, tuple[Any, ...]],
    atom_index: dict[str, list[Atom]] | None = None,
) -> tuple[float, float] | None:
    """Resolve min/max interval for an expression node, or None if unbounded."""
    from pyrung.core.expression import BinaryExpr

    literal = _literal_value_from_value(node)
    if literal is not None and isinstance(literal, (int, float)):
        return (literal, literal)

    tag_name = _tag_name_from_value(node)
    if tag_name is not None:
        if tag_name in known_domains:
            vals = known_domains[tag_name]
            if vals:
                return (min(vals), max(vals))
            return None
        tag = graph.tags.get(tag_name)
        if tag is not None and tag.min is not None and tag.max is not None:
            return (tag.min, tag.max)
        return None

    if not isinstance(node, BinaryExpr):
        return None

    left = _interval_bounds(node.left, graph, all_exprs, known_domains, atom_index)
    right = _interval_bounds(node.right, graph, all_exprs, known_domains, atom_index)
    if left is None or right is None:
        return None

    a_lo, a_hi = left
    b_lo, b_hi = right

    if node.symbol == "/" and b_lo <= 0 <= b_hi:
        return None

    try:
        corners = [node.op(a, b) for a in (a_lo, a_hi) for b in (b_lo, b_hi)]
    except (ZeroDivisionError, OverflowError):
        return None
    return (min(corners), max(corners))


def _domain_from_calc_expression(
    expression: Any,
    target: Tag,
    graph: ProgramGraph,
    all_exprs: list[Expr],
    known_domains: dict[str, tuple[Any, ...]],
    atom_index: dict[str, list[Atom]] | None = None,
) -> tuple[Any, ...] | None:
    """Infer a target domain from a supported calc expression shape."""
    from pyrung.core.expression import BinaryExpr

    direct = _domain_from_copy_like_value(
        expression, target, graph, all_exprs, known_domains, atom_index
    )
    if direct is not None:
        return direct

    if isinstance(expression, BinaryExpr) and expression.symbol == "%":
        modulus = _literal_value_from_value(expression.right)
        if isinstance(modulus, int) and not isinstance(modulus, bool) and 0 < modulus <= 1000:
            return tuple(range(modulus))

    bounds = _interval_bounds(expression, graph, all_exprs, known_domains, atom_index)
    if bounds is None:
        return None
    lo, hi = bounds
    from pyrung.core.instruction.conversions import _truncate_to_tag_type

    lo_t = _truncate_to_tag_type(lo, target)
    hi_t = _truncate_to_tag_type(hi, target)
    if not isinstance(lo_t, (int, float)) or not isinstance(hi_t, (int, float)):
        return None
    if isinstance(lo_t, int) and isinstance(hi_t, int):
        return tuple(range(lo_t, hi_t + 1))
    return None


def _domain_from_write_instruction(
    instr: Any,
    target_name: str,
    target: Tag,
    graph: ProgramGraph,
    all_exprs: list[Expr],
    known_domains: dict[str, tuple[Any, ...]],
    atom_index: dict[str, list[Atom]] | None = None,
) -> tuple[Any, ...] | None:
    """Infer a target domain from one supported writer instruction."""
    from pyrung.core.instruction.calc import CalcInstruction
    from pyrung.core.instruction.data_transfer import CopyInstruction, FillInstruction

    if isinstance(instr, CopyInstruction):
        if instr.convert is not None:
            return None
        return _domain_from_copy_like_value(
            instr.source, target, graph, all_exprs, known_domains, atom_index
        )

    if isinstance(instr, FillInstruction):
        return _domain_from_copy_like_value(
            instr.value, target, graph, all_exprs, known_domains, atom_index
        )

    if isinstance(instr, CalcInstruction):
        if instr.dest.name != target_name:
            return None
        return _domain_from_calc_expression(
            instr.expression,
            target,
            graph,
            all_exprs,
            known_domains,
            atom_index,
        )

    return None


def _collect_structural_domain_info(
    program: Program,
    graph: ProgramGraph,
    all_exprs: list[Expr],
    literal_write_domains: dict[str, tuple[Any, ...]] | None = None,
) -> tuple[dict[str, tuple[Any, ...]], frozenset[str]]:
    """Discover finite domains and reverse soundness blockers.

    Returns ``(domains, blockers)`` where *domains* maps tag names to their
    inferred finite value tuples and *blockers* lists source tags with
    unsupported reverse dependencies that could not be safely bounded.
    """
    from pyrung.core.validation._common import walk_instructions

    known_domains = dict(
        literal_write_domains or _collect_literal_write_domains(program, graph.tags)
    )
    for tag_name, tag in graph.tags.items():
        if (
            not tag.external
            and tag_name not in graph.writers_of
            and not graph.is_physical_input(tag_name)
        ):
            known_domains.setdefault(tag_name, (tag.default,))

    by_target: dict[str, list[Any]] = {}
    for instr in walk_instructions(program):
        for target_name, _itype in _all_write_targets(instr):
            by_target.setdefault(target_name, []).append(instr)

    atom_idx = _build_atom_index(all_exprs)

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
                    atom_idx,
                )
                if domain is None:
                    candidate_values = set()
                    break
                candidate_values.update(domain)

            if not candidate_values:
                continue

            merged_values = set(known_domains.get(target_name, ()))
            merged_values.update(candidate_values)
            if target.choices is not None:
                merged_values = merged_values & set(target.choices.keys())
            if target.min is not None:
                merged_values = {v for v in merged_values if v >= target.min}
            if target.max is not None:
                merged_values = {v for v in merged_values if v <= target.max}
            if len(merged_values) > 1000:
                continue

            merged = tuple(sorted(merged_values))
            if known_domains.get(target_name) != merged:
                known_domains[target_name] = merged
                changed = True

    blockers = _backward_propagate_comparison_boundaries(
        program, graph, all_exprs, known_domains, atom_idx
    )

    return known_domains, blockers


def _collect_structural_domains(
    program: Program,
    graph: ProgramGraph,
    all_exprs: list[Expr],
    literal_write_domains: dict[str, tuple[Any, ...]] | None = None,
) -> dict[str, tuple[Any, ...]]:
    """Discover finite domains from structural writes via fixed-point propagation."""
    domains, _blockers = _collect_structural_domain_info(
        program, graph, all_exprs, literal_write_domains
    )
    return domains


_InvertFn = Callable[[Any], Any]
_IDENTITY: _InvertFn = lambda v: v


def _calc_reverse_edge(
    expression: Any,
) -> tuple[str, _InvertFn] | None:
    """Extract (source_tag_name, invert_fn) from a calc expression.

    Returns the inverse transform for single-source-tag expressions of the
    form ``source ± literal``, ``literal ± source``, ``source * literal``,
    ``literal * source``, ``+source``, or ``-source``.  The invert function
    maps a target comparison value back to the source value that produces it,
    returning ``None`` when the preimage is not exact (e.g. non-integer
    division for ``*``).
    """
    from pyrung.core.expression import BinaryExpr, UnaryExpr

    if isinstance(expression, UnaryExpr):
        tag_name = _tag_name_from_value(expression.operand)
        if tag_name is None:
            return None
        if expression.symbol == "+":
            return tag_name, _IDENTITY
        if expression.symbol == "-":
            return tag_name, lambda v: -v
        return None

    if not isinstance(expression, BinaryExpr):
        return None
    if expression.symbol not in ("+", "-", "*"):
        return None

    left_tag = _tag_name_from_value(expression.left)
    left_lit = _literal_value_from_value(expression.left)
    right_tag = _tag_name_from_value(expression.right)
    right_lit = _literal_value_from_value(expression.right)

    if left_tag is not None and right_lit is not None and isinstance(right_lit, (int, float)):
        if expression.symbol == "+":
            return left_tag, lambda v, k=right_lit: v - k
        if expression.symbol == "-":
            return left_tag, lambda v, k=right_lit: v + k
        if expression.symbol == "*":
            if right_lit == 0:
                return None
            return (
                left_tag,
                lambda v, k=right_lit: (
                    v // k if isinstance(v, int) and isinstance(k, int) and v % k == 0 else None
                ),
            )

    if right_tag is not None and left_lit is not None and isinstance(left_lit, (int, float)):
        if expression.symbol == "+":
            return right_tag, lambda v, k=left_lit: v - k
        if expression.symbol == "-":
            return right_tag, lambda v, k=left_lit: k - v
        if expression.symbol == "*":
            if left_lit == 0:
                return None
            return (
                right_tag,
                lambda v, k=left_lit: (
                    v // k if isinstance(v, int) and isinstance(k, int) and v % k == 0 else None
                ),
            )

    return None


def _expand_indirect_tag_names(dest: Any) -> list[str]:
    """Expand indirect refs to possible concrete target tag names.

    For IndirectRef, uses pointer min/max/choices to tighten the range.
    For IndirectBlockRange/IndirectExprRef, falls back to full block bounds.
    Returns [] if expansion exceeds 1000 tags.
    """
    from pyrung.core.memory_block import IndirectBlockRange, IndirectExprRef, IndirectRef

    if isinstance(dest, IndirectRef):
        block = dest.block
        lo = block.start
        hi = block.end
        ptr = dest.pointer
        if ptr.min is not None:
            lo = max(lo, ptr.min)
        if ptr.max is not None:
            hi = min(hi, ptr.max)
        if lo > hi:
            return []
        if ptr.choices is not None:
            addrs = sorted(a for a in ptr.choices if lo <= a <= hi)
        else:
            addrs = list(range(lo, hi + 1))
        if len(addrs) > 1000:
            return []
        return [block._get_tag(addr).name for addr in addrs]

    if isinstance(dest, (IndirectBlockRange, IndirectExprRef)):
        block = dest.block
        size = block.end - block.start + 1
        if size > 1000:
            return []
        return [block._get_tag(addr).name for addr in range(block.start, block.end + 1)]

    return []


def _unsupported_reverse_sources(
    instr: Any,
    covered_pairs: set[tuple[str, str]],
) -> list[tuple[str, list[str]]]:
    """Identify (source_tag, [target_tags]) for writes not covered by reverse edges.

    Returns pairs where a tag-valued source feeds an instruction whose
    reverse shape is unsupported, so backward propagation cannot invert it.
    """
    from pyrung.core.instruction.calc import CalcInstruction
    from pyrung.core.instruction.data_transfer import (
        BlockCopyInstruction,
        CopyInstruction,
        FillInstruction,
    )
    from pyrung.core.validation._common import _resolve_tag_names

    if isinstance(instr, CalcInstruction):
        source_name = _tag_name_from_value(instr.expression)
        if source_name is None:
            from pyrung.core.expression import BinaryExpr, UnaryExpr

            if isinstance(instr.expression, UnaryExpr):
                source_name = _tag_name_from_value(instr.expression.operand)
            elif isinstance(instr.expression, BinaryExpr):
                source_name = _tag_name_from_value(instr.expression.left)
                if source_name is None:
                    source_name = _tag_name_from_value(instr.expression.right)
        if source_name is None:
            return []
        target_names = _resolve_tag_names(instr.dest)
        if not target_names:
            target_names = _expand_indirect_tag_names(instr.dest)
        uncovered = [
            tn
            for tn in target_names
            if tn != source_name and (source_name, tn) not in covered_pairs
        ]
        if uncovered:
            return [(source_name, uncovered)]

    elif isinstance(instr, (CopyInstruction, FillInstruction)):
        raw = instr.source if isinstance(instr, CopyInstruction) else instr.value
        source_name = _tag_name_from_value(raw)
        if source_name is None:
            return []
        dest = instr.dest
        target_names = _resolve_tag_names(dest)
        if not target_names:
            target_names = _expand_indirect_tag_names(dest)
        uncovered = [
            tn
            for tn in target_names
            if tn != source_name and (source_name, tn) not in covered_pairs
        ]
        if uncovered:
            return [(source_name, uncovered)]

    elif isinstance(instr, BlockCopyInstruction):
        source_names = _resolve_tag_names(instr.source)
        dest_names = _resolve_tag_names(instr.dest)
        if not source_names:
            source_names = _expand_indirect_tag_names(instr.source)
        if not dest_names:
            dest_names = _expand_indirect_tag_names(instr.dest)
        result: list[tuple[str, list[str]]] = []
        if source_names and dest_names:
            if len(source_names) == len(dest_names):
                for src, dst in zip(source_names, dest_names, strict=True):
                    if src != dst and (src, dst) not in covered_pairs:
                        result.append((src, [dst]))
            else:
                for src in source_names:
                    uncovered = [
                        dst for dst in dest_names if src != dst and (src, dst) not in covered_pairs
                    ]
                    if uncovered:
                        result.append((src, uncovered))
        return result

    return []


def _backward_propagate_comparison_boundaries(
    program: Program,
    graph: ProgramGraph,
    all_exprs: list[Expr],
    known_domains: dict[str, tuple[Any, ...]],
    atom_idx: dict[str, list[Atom]],
) -> frozenset[str]:
    """Propagate comparison boundary values from write targets back to sources.

    Covers ``copy``, ``fill``, ``blockcopy`` (identity), and invertible
    ``calc(expr, target)`` expressions.  When a target has downstream
    comparisons (e.g. ``target == 75``), the boundary values are transformed
    through the inverse and added to the source's domain so the BFS
    enumerates them.

    Returns the set of *reverse soundness blockers* — source tags with
    unsupported reverse dependencies whose domains could not be safely
    bounded by structural or declared fallbacks.
    """
    from pyrung.core.instruction.calc import CalcInstruction
    from pyrung.core.instruction.data_transfer import (
        BlockCopyInstruction,
        CopyInstruction,
        FillInstruction,
    )
    from pyrung.core.validation._common import _resolve_tag_names, walk_instructions

    reverse_edges: dict[str, list[tuple[str, _InvertFn]]] = {}
    covered_pairs: set[tuple[str, str]] = set()

    for instr in walk_instructions(program):
        if isinstance(instr, CopyInstruction):
            if instr.convert is not None:
                continue
            source_name = _tag_name_from_value(instr.source)
            if source_name is None:
                continue
            target_names = _resolve_tag_names(instr.dest)
            if not target_names:
                target_names = _expand_indirect_tag_names(instr.dest)
            for target_name in target_names:
                reverse_edges.setdefault(source_name, []).append((target_name, _IDENTITY))
                covered_pairs.add((source_name, target_name))

        elif isinstance(instr, FillInstruction):
            source_name = _tag_name_from_value(instr.value)
            if source_name is None:
                continue
            target_names = _resolve_tag_names(instr.dest)
            if not target_names:
                target_names = _expand_indirect_tag_names(instr.dest)
            for target_name in target_names:
                reverse_edges.setdefault(source_name, []).append((target_name, _IDENTITY))
                covered_pairs.add((source_name, target_name))

        elif isinstance(instr, BlockCopyInstruction):
            if instr.convert is not None:
                continue
            source_names = _resolve_tag_names(instr.source)
            dest_names = _resolve_tag_names(instr.dest)
            if not dest_names:
                dest_names = _expand_indirect_tag_names(instr.dest)
            if source_names and dest_names:
                if len(source_names) == len(dest_names):
                    for src, dst in zip(source_names, dest_names, strict=True):
                        reverse_edges.setdefault(src, []).append((dst, _IDENTITY))
                        covered_pairs.add((src, dst))
                else:
                    for src in source_names:
                        for dst in dest_names:
                            reverse_edges.setdefault(src, []).append((dst, _IDENTITY))
                            covered_pairs.add((src, dst))

        elif isinstance(instr, CalcInstruction):
            target_name = _tag_name_from_value(instr.dest)
            if target_name is None:
                continue
            edge = _calc_reverse_edge(instr.expression)
            if edge is not None:
                source_name, invert = edge
                reverse_edges.setdefault(source_name, []).append((target_name, invert))
                covered_pairs.add((source_name, target_name))

    comparison_forms = {"eq", "ne", "lt", "le", "gt", "ge"}

    if reverse_edges:
        changed = True
        while changed:
            changed = False
            for source_name, edges in reverse_edges.items():
                source_tag = graph.tags.get(source_name)
                if source_tag is None:
                    continue

                back_values: set[Any] = set()
                for target_name, invert in edges:
                    for atom in atom_idx.get(target_name, []):
                        if atom.form not in comparison_forms or atom.operand is None:
                            continue
                        if isinstance(atom.operand, str):
                            continue
                        raw = invert(atom.operand)
                        if not isinstance(raw, (int, float)):
                            continue
                        back_values.add(raw)
                        back_values.add(raw - 1)
                        back_values.add(raw + 1)

                if not back_values:
                    continue

                existing = set(known_domains.get(source_name, ()))
                merged = existing | back_values
                if source_tag.choices is not None:
                    merged = merged & set(source_tag.choices.keys())
                if source_tag.min is not None:
                    merged = {v for v in merged if v >= source_tag.min}
                if source_tag.max is not None:
                    merged = {v for v in merged if v <= source_tag.max}
                if len(merged) > 1000:
                    continue

                new_domain = tuple(sorted(merged))
                if known_domains.get(source_name) != new_domain:
                    known_domains[source_name] = new_domain
                    changed = True

    reverse_soundness_blockers: set[str] = set()
    for instr in walk_instructions(program):
        source_targets = _unsupported_reverse_sources(instr, covered_pairs)
        for source_name, target_names in source_targets:
            has_downstream_comparison = any(atom_idx.get(tn) for tn in target_names)
            if not has_downstream_comparison:
                continue
            if source_name in known_domains:
                continue
            source_tag = graph.tags.get(source_name)
            if source_tag is None:
                continue
            declared = _declared_domain(source_tag)
            if declared is not None:
                known_domains[source_name] = declared
                continue
            reverse_soundness_blockers.add(source_name)

    return frozenset(reverse_soundness_blockers)


def _extract_value_domain(
    tag_name: str,
    tag: Tag,
    all_exprs: list[Expr],
    all_tags: dict[str, Tag] | None = None,
    literal_write_domains: dict[str, tuple[Any, ...]] | None = None,
    known_domains: dict[str, tuple[Any, ...]] | None = None,
    graph: ProgramGraph | None = None,
    atom_index: dict[str, list[Atom]] | None = None,
) -> tuple[Any, ...] | None:
    """Determine the finite value domain for a tag, or None if unbounded."""
    if literal_write_domains is not None and tag_name in literal_write_domains:
        return literal_write_domains[tag_name]
    if known_domains is not None and tag_name in known_domains:
        return known_domains[tag_name]

    atoms = (
        atom_index.get(tag_name, [])
        if atom_index is not None
        else _collect_atoms_for_tag(all_exprs, tag_name)
    )
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
        if atom.form not in comparison_forms or atom.operand is None:
            continue
        other_ref = atom.operand if atom.tag == tag_name else atom.tag
        if isinstance(other_ref, str):
            if other_ref == tag_name:
                continue
            if known_domains is not None and other_ref in known_domains:
                boundary = list(known_domains[other_ref])
            else:
                other = all_tags.get(other_ref) if all_tags is not None else None
                boundary = _boundary_values_for_tag(other) if other is not None else []
            if boundary:
                literals.update(boundary)
            else:
                unresolved_tag_comparison = True
        else:
            literals.add(other_ref)

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
    if isinstance(tag.default, (int, float)) and not isinstance(tag.default, bool):
        partitioned.add(tag.default)
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


def _is_ote_unconditionally_reachable(
    tag_name: str,
    graph: ProgramGraph,
) -> bool:
    """True if every OTE writer rung for *tag_name* executes every scan.

    An OTE in the main program always executes.  An OTE inside a subroutine
    only executes when every call site for that subroutine is itself in an
    unconditionally-reachable rung with no condition guard.
    """
    writer_indices = graph.writers_of.get(tag_name, frozenset())
    for ni in writer_indices:
        node = graph.rung_nodes[ni]
        if node.subroutine is not None:
            has_unconditional_call = False
            for _caller_ni, caller_node in enumerate(graph.rung_nodes):
                if node.subroutine not in caller_node.calls:
                    continue
                if caller_node.subroutine is not None:
                    return False
                if caller_node.condition_reads:
                    return False
                has_unconditional_call = True
            if not has_unconditional_call:
                return False
    return True


def _is_self_referencing_ote(
    tag_name: str,
    graph: ProgramGraph,
) -> bool:
    """True if any OTE writer rung for *tag_name* reads the tag in its condition."""
    writer_indices = graph.writers_of.get(tag_name, frozenset())
    for ni in writer_indices:
        if tag_name in graph.rung_nodes[ni].ote_writes:
            if tag_name in graph.rung_nodes[ni].condition_reads:
                return True
    return False


def _is_nested_in_dynamic_for_loop(
    tag_name: str,
    graph: ProgramGraph,
    program: Program,
) -> bool:
    """True if any OTE writer for *tag_name* is inside a ForLoop with dynamic count.

    A dynamic count (Tag-based) can be 0, meaning the ForLoop body is skipped
    and the OTE doesn't execute — the tag retains its previous-scan value.
    """
    from pyrung.core.instruction.coils import OutInstruction
    from pyrung.core.instruction.control import ForLoopInstruction

    def _has_ote_for_tag(instructions: list) -> bool:
        for instr in instructions:
            if isinstance(instr, OutInstruction) and hasattr(instr, "target"):
                target = instr.target
                if hasattr(target, "name") and target.name == tag_name:
                    return True
        return False

    def _count_is_dynamic(count: object) -> bool:
        from pyrung.core.tag import Tag

        if isinstance(count, Tag):
            return True
        if isinstance(count, (int, float)) and count > 0:
            return False
        return True

    def _walk_instructions(instructions: list) -> bool:
        for instr in instructions:
            if isinstance(instr, ForLoopInstruction):
                if _has_ote_for_tag(instr.instructions) and _count_is_dynamic(instr.count):
                    return True
        return False

    writer_indices = graph.writers_of.get(tag_name, frozenset())
    for ni in writer_indices:
        node = graph.rung_nodes[ni]
        if tag_name not in node.ote_writes:
            continue
        if node.scope == "main":
            rung = program.rungs[node.rung_index]
        else:
            assert node.subroutine is not None
            rung = program.subroutines[node.subroutine][node.rung_index]
        if _walk_instructions(rung._instructions):
            return True
        for branch in rung._branches:
            if _walk_instructions(branch._instructions):
                return True
    return False


def _uses_prior_snapshot(program: Program, node: RungNode) -> bool:
    """Return whether a graph node belongs to a continued() top-level rung."""
    if node.scope == "main":
        return program.rungs[node.rung_index]._use_prior_snapshot
    assert node.subroutine is not None
    return program.subroutines[node.subroutine][node.rung_index]._use_prior_snapshot


def _has_continued_reader(
    program: Program,
    tag_name: str,
    graph: ProgramGraph,
) -> bool:
    """True if any read of *tag_name* comes from a continued() rung."""
    return any(
        _uses_prior_snapshot(program, graph.rung_nodes[reader_idx])
        for reader_idx in graph.readers_of.get(tag_name, frozenset())
    )


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
    _PROGRESS_KIND_INT_DOWN: "descending integer progress",
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
                f"  {name}: no domain constraint"
                f" — add choices= for discrete values, or cover with dt= testing"
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
    receive_dest_names: frozenset[str] = frozenset(),
) -> _ClassifyResult | Intractable:
    """Classify dimensions using prebuilt graph/expression context."""
    done_acc_info = _collect_done_acc_pairs(program)
    literal_write_domains = _collect_literal_write_domains(program, graph.tags)
    structural_domains, reverse_blockers = _collect_structural_domain_info(
        program,
        graph,
        all_exprs,
        literal_write_domains,
    )
    known_domains = dict(structural_domains)
    if discovered_domains is not None:
        known_domains.update(discovered_domains)

    for ptr_name, (_block_name, start, end) in graph.pointer_tags.items():
        if ptr_name in known_domains:
            continue
        tag = graph.tags.get(ptr_name)
        if tag is None or tag.readonly:
            continue
        declared = _declared_domain(tag)
        if declared is not None:
            known_domains[ptr_name] = declared
            continue
        bound_size = end - start + 1
        if bound_size > 1000:
            continue
        values = set(range(start, end + 1))
        values.add(tag.default)
        known_domains[ptr_name] = tuple(sorted(values))

    atom_idx = _build_atom_index(all_exprs)

    consumed_accs: set[str] = set()
    for acc_name in done_acc_info.pairs.values():
        if atom_idx.get(acc_name) or _has_forbidden_data_read(
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
    comparison_absorptions = _find_comparison_absorptions(
        program,
        graph,
        all_exprs,
        structural_domains,
        project=project,
    )
    threshold_absorptions = _merge_threshold_absorptions(
        threshold_absorptions,
        comparison_absorptions,
    )
    consumed_accs.difference_update(threshold_absorptions.progress_names)

    done_acc = {d: a for d, a in done_acc_info.pairs.items() if a not in consumed_accs}
    unconsumed_accs = frozenset(done_acc.values())

    scope_input_tags: frozenset[str] | None = None
    if scope is not None:
        _is_nd_input = lambda tn: (
            graph.tag_roles.get(tn) is TagRole.INPUT or tn in receive_dest_names
        )
        upstream_tags: set[str] = set()
        for tag_name in scope:
            if _is_nd_input(tag_name):
                upstream_tags.add(tag_name)
            upstream_tags.update(tag for tag in graph.upstream_slice(tag_name) if _is_nd_input(tag))
        for expr in all_exprs:
            upstream_tags.update(
                tag_name for tag_name in _referenced_tags(expr) if _is_nd_input(tag_name)
            )
        scope_input_tags = frozenset(upstream_tags)

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
        if tag_name in threshold_absorptions.comparison_tags and tag_name not in receive_dest_names:
            continue

        role = graph.tag_roles.get(tag_name)
        is_written = tag_name in graph.writers_of

        if not tag.external and not is_written and not graph.is_physical_input(tag_name):
            continue

        if (
            role == TagRole.INPUT
            or (tag.external and not is_written)
            or tag_name in receive_dest_names
        ):
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
                atom_idx,
            )
            if not domain:
                declared = _declared_domain(tag)
                if declared is not None:
                    domain = declared
            if not domain and _has_non_condition_data_read(tag_name, graph):
                declared = _declared_domain(tag)
                if declared is not None:
                    domain = declared
            if domain is None:
                infeasible_tags.append(tag_name)
                continue
            if domain:
                nondeterministic[tag_name] = domain
            continue

        if not is_written:
            continue

        if tag_name in done_acc:
            stateful[tag_name] = (False, PENDING, True)
            continue

        if tag_name not in graph.readers_of:
            combinational.add(tag_name)
            continue

        if (
            _is_ote_only(tag_name, graph)
            and not _has_continued_reader(program, tag_name, graph)
            and _is_ote_unconditionally_reachable(tag_name, graph)
            and not _is_self_referencing_ote(tag_name, graph)
            and not _is_nested_in_dynamic_for_loop(tag_name, graph, program)
        ):
            combinational.add(tag_name)
            continue

        if tag_name in done_acc_info.pairs.values() and tag_name in consumed_accs:
            if not atom_idx.get(tag_name):
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
            atom_idx,
        )
        if domain is None:
            if tag_name in known_domains:
                stateful[tag_name] = known_domains[tag_name]
            else:
                declared = _declared_domain(tag)
                if declared is not None:
                    stateful[tag_name] = declared
                else:
                    infeasible_tags.append(tag_name)
            continue
        if domain:
            stateful[tag_name] = domain

    for ptr_name in graph.pointer_tags:
        if (
            ptr_name in stateful
            or ptr_name in nondeterministic
            or ptr_name in combinational
            or ptr_name in infeasible_tags
        ):
            continue
        tag = graph.tags.get(ptr_name)
        if tag is None or tag.readonly:
            continue
        is_written = ptr_name in graph.writers_of
        if not tag.external and not is_written and not graph.is_physical_input(ptr_name):
            continue
        infeasible_tags.append(ptr_name)

    for blocker_name in sorted(reverse_blockers):
        if blocker_name not in stateful and blocker_name not in nondeterministic:
            continue
        if blocker_name in infeasible_tags:
            continue
        infeasible_tags.append(blocker_name)

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
    from pyrung.core.analysis.prove.passes import _collect_receive_dest_names

    graph = build_program_graph(program)
    all_exprs = _collect_all_exprs(program, graph, scope=scope)
    return _classify_dimensions_from_graph(
        program,
        graph,
        all_exprs,
        scope=scope,
        receive_dest_names=frozenset(_collect_receive_dest_names(program)),
    )


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

    nd_names = sorted(relevant_nd)
    nd_domains = [relevant_nd[n] for n in nd_names]

    initial_kernel = compiled.create_kernel()
    initial_snap = _snapshot_kernel(initial_kernel)

    kernel = initial_kernel
    for combo in itertools.product(*nd_domains) if nd_domains else [()]:
        _restore_kernel(kernel, initial_snap)
        for name, val in zip(nd_names, combo, strict=True):
            kernel.tags[name] = val

        for _scan in range(max_scans):
            prev_sizes = tuple(len(observed[c]) for c in candidates)
            _step_compiled_kernel(compiled, kernel, dt=dt)
            for c in candidates:
                observed[c].add(kernel.tags.get(c, graph.tags[c].default))
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
