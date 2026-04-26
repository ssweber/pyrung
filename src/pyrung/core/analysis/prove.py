"""Exhaustive state-space verification for pyrung programs.

BFS over the reachable state space using the compiled replay kernel
as the execution oracle and the expression tree for search-space
reduction (dimension classification, value domain extraction,
don't-care pruning).
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pdg import TagRole, build_program_graph
from pyrung.core.analysis.simplified import (
    And,
    Atom,
    Const,
    Expr,
    Or,
    _condition_to_expr,
    simplified_forms,
)
from pyrung.core.kernel import BlockSpec, CompiledKernel, ReplayKernel
from pyrung.core.tag import TagType

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.program import Program
    from pyrung.core.tag import Tag


# ---------------------------------------------------------------------------
# Public result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Proven:
    """Invariant holds across all reachable states."""

    states_explored: int


@dataclass(frozen=True)
class Counterexample:
    """Invariant violated — trace reproduces the failure."""

    trace: list[dict[str, Any]]


@dataclass(frozen=True)
class Intractable:
    """Verification cannot complete within resource bounds."""

    reason: str
    dimensions: int
    estimated_space: int
    tags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class StateDiff:
    """Difference between two reachable state sets."""

    added: frozenset[frozenset[tuple[str, Any]]]
    removed: frozenset[frozenset[tuple[str, Any]]]


# ---------------------------------------------------------------------------
# Dimension classification
# ---------------------------------------------------------------------------

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

PENDING = "Pending"

_DONE_KIND_ON_DELAY = "on_delay"
_DONE_KIND_OFF_DELAY = "off_delay"
_DONE_KIND_COUNT_UP = "count_up"
_DONE_KIND_COUNT_DOWN = "count_down"


@dataclass(frozen=True)
class _DoneAccInfo:
    pairs: dict[str, str]
    presets: dict[str, int]
    kinds: dict[str, str]


def _collect_done_acc_pairs(program: Program) -> _DoneAccInfo:
    """Map Done tag names to their Acc tag names for timer/counter instructions.

    Also captures constant presets and instruction kinds for event jumps.
    """
    from pyrung.core.instruction.counters import CountDownInstruction, CountUpInstruction
    from pyrung.core.instruction.timers import OffDelayInstruction, OnDelayInstruction
    from pyrung.core.tag import Tag
    from pyrung.core.validation._common import walk_instructions

    pairs: dict[str, str] = {}
    presets: dict[str, int] = {}
    kinds: dict[str, str] = {}

    for instr in walk_instructions(program):
        if isinstance(instr, OnDelayInstruction):
            kind = _DONE_KIND_ON_DELAY
        elif isinstance(instr, OffDelayInstruction):
            kind = _DONE_KIND_OFF_DELAY
        elif isinstance(instr, CountUpInstruction):
            kind = _DONE_KIND_COUNT_UP
        elif isinstance(instr, CountDownInstruction):
            kind = _DONE_KIND_COUNT_DOWN
        else:
            continue

        pairs[instr.done_bit.name] = instr.accumulator.name
        kinds[instr.done_bit.name] = kind
        if not isinstance(instr.preset, Tag) and isinstance(instr.preset, (int, float)):
            presets[instr.done_bit.name] = int(instr.preset)

    return _DoneAccInfo(pairs=pairs, presets=presets, kinds=kinds)


def _done_acc_state(kind: str, done_val: Any, acc_val: Any) -> bool | str:
    """Derive the three-valued timer/counter state from Done and Acc."""
    acc_nonzero = bool(acc_val and acc_val != 0)
    if kind == _DONE_KIND_OFF_DELAY:
        if done_val and acc_nonzero:
            return PENDING
        return bool(done_val)
    if done_val:
        return True
    if acc_nonzero:
        return PENDING
    return False


def _all_write_targets(instr: Any) -> list[tuple[str, str]]:
    """Extract (tag_name, instruction_type) for every write target."""
    from pyrung.core.instruction.advanced import SearchInstruction, ShiftInstruction
    from pyrung.core.instruction.calc import CalcInstruction
    from pyrung.core.instruction.coils import (
        LatchInstruction,
        OutInstruction,
        ResetInstruction,
    )
    from pyrung.core.instruction.control import (
        EnabledFunctionCallInstruction,
        ForLoopInstruction,
        FunctionCallInstruction,
    )
    from pyrung.core.instruction.counters import (
        CountDownInstruction,
        CountUpInstruction,
    )
    from pyrung.core.instruction.data_transfer import (
        BlockCopyInstruction,
        CopyInstruction,
        FillInstruction,
    )
    from pyrung.core.instruction.drums import (
        EventDrumInstruction,
        TimeDrumInstruction,
    )
    from pyrung.core.instruction.packing import (
        PackBitsInstruction,
        PackTextInstruction,
        PackWordsInstruction,
        UnpackToBitsInstruction,
        UnpackToWordsInstruction,
    )
    from pyrung.core.instruction.send_receive import (
        ModbusReceiveInstruction,
        ModbusSendInstruction,
    )
    from pyrung.core.instruction.timers import OffDelayInstruction, OnDelayInstruction
    from pyrung.core.tag import Tag
    from pyrung.core.validation._common import _resolve_tag_names

    itype = type(instr).__name__
    targets: list[str] = []

    if isinstance(instr, OutInstruction):
        targets = _resolve_tag_names(instr.target)
    elif isinstance(instr, (LatchInstruction, ResetInstruction)):
        targets = _resolve_tag_names(instr.target)
    elif isinstance(instr, (OnDelayInstruction, OffDelayInstruction)):
        targets = [instr.done_bit.name, instr.accumulator.name]
    elif isinstance(instr, (CountUpInstruction, CountDownInstruction)):
        targets = [instr.done_bit.name, instr.accumulator.name]
    elif isinstance(instr, CopyInstruction):
        dest = instr.target
        if isinstance(dest, Tag):
            targets = [dest.name]
        else:
            targets = _resolve_tag_names(dest)
    elif isinstance(instr, CalcInstruction):
        dest = instr.dest
        if isinstance(dest, Tag):
            targets = [dest.name]
        else:
            targets = _resolve_tag_names(dest)
    elif isinstance(instr, ShiftInstruction):
        targets = _resolve_tag_names(instr.bit_range)
    elif isinstance(instr, TimeDrumInstruction):
        targets = [t.name for t in instr.outputs]
        targets.append(instr.current_step.name)
        targets.append(instr.completion_flag.name)
        targets.append(instr.accumulator.name)
    elif isinstance(instr, EventDrumInstruction):
        targets = [t.name for t in instr.outputs]
        targets.append(instr.current_step.name)
        targets.append(instr.completion_flag.name)
    elif isinstance(instr, (FunctionCallInstruction, EnabledFunctionCallInstruction)):
        for target in instr._outs.values():
            if isinstance(target, Tag):
                targets.append(target.name)
    elif isinstance(instr, BlockCopyInstruction):
        targets = _resolve_tag_names(instr.dest)
    elif isinstance(instr, FillInstruction):
        targets = _resolve_tag_names(instr.dest)
    elif isinstance(
        instr,
        (
            PackBitsInstruction,
            PackWordsInstruction,
            PackTextInstruction,
            UnpackToBitsInstruction,
            UnpackToWordsInstruction,
        ),
    ):
        targets = _resolve_tag_names(instr.dest)
    elif isinstance(instr, ModbusSendInstruction):
        targets = [
            instr.sending.name,
            instr.success.name,
            instr.error.name,
            instr.exception_response.name,
        ]
    elif isinstance(instr, ModbusReceiveInstruction):
        targets = [
            instr.receiving.name,
            instr.success.name,
            instr.error.name,
            instr.exception_response.name,
        ]
    elif isinstance(instr, SearchInstruction):
        targets = [instr.result.name, instr.found.name]
    elif isinstance(instr, ForLoopInstruction):
        targets = [instr.idx_tag.name]

    return [(name, itype) for name in targets]


def _collect_all_exprs(
    program: Program,
    graph: ProgramGraph,
    scope: list[str] | None = None,
) -> list[Expr]:
    """Collect all expression trees from simplified forms and write-site conditions.

    When *scope* is given, restricts to expressions in the upstream cone
    of the scoped tags.  This improves don't-care pruning without
    affecting soundness (filtering only discards irrelevant expressions).
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


def _collect_atoms_for_tag(exprs: list[Expr], tag_name: str) -> list[Atom]:
    """Collect all Atom nodes referencing a specific tag from a list of expressions."""
    atoms: list[Atom] = []
    for expr in exprs:
        _walk_atoms(expr, tag_name, atoms)
    return atoms


def _walk_atoms(expr: Expr, tag_name: str, out: list[Atom]) -> None:
    if isinstance(expr, Atom):
        if expr.tag == tag_name:
            out.append(expr)
    elif isinstance(expr, (And, Or)):
        for t in expr.terms:
            _walk_atoms(t, tag_name, out)


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


def _extract_value_domain(
    tag_name: str,
    tag: Tag,
    all_exprs: list[Expr],
    all_tags: dict[str, Tag] | None = None,
) -> tuple[Any, ...] | None:
    """Determine the finite value domain for a tag, or None if unbounded."""
    if tag.type == TagType.BOOL:
        return (False, True)

    atoms = _collect_atoms_for_tag(all_exprs, tag_name)
    if not atoms:
        return ()  # not consumed in any condition — excluded

    comparison_forms = {"eq", "ne", "lt", "le", "gt", "ge"}
    literals: set[Any] = set()
    unresolved_tag_comparison = False

    for atom in atoms:
        if atom.form in comparison_forms and atom.operand is not None:
            if isinstance(atom.operand, str):
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
        if domain_size > 1000:
            if not literals:
                return None
            # Large range but we have comparison boundaries — use those
        else:
            return tuple(range(int(tag.min), int(tag.max) + 1))

    if unresolved_tag_comparison and not literals:
        return None

    if not literals:
        return ()

    sorted_literals = sorted(literals)
    unmatched = sorted_literals[-1] + 1 if sorted_literals else 0
    return tuple(sorted_literals) + (unmatched,)


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


@dataclass(frozen=True)
class _StateKeyDoneSpec:
    index: int
    acc_name: str
    kind: str


@dataclass(frozen=True)
class _DoneEventSpec:
    state_index: int
    acc_name: str
    kind: str
    preset: int


@dataclass(frozen=True)
class _ExploreContext:
    compiled: CompiledKernel
    graph: ProgramGraph
    all_exprs: list[Expr]
    stateful_dims: dict[str, tuple[Any, ...]]
    nondeterministic_dims: dict[str, tuple[Any, ...]]
    stateful_names: tuple[str, ...]
    edge_tag_names: tuple[str, ...]
    state_key_done_specs: tuple[_StateKeyDoneSpec, ...]
    done_event_specs: tuple[_DoneEventSpec, ...]
    block_specs: tuple[BlockSpec, ...]
    dt: float
    edge_tag_exprs: dict[str, list[Expr]] = field(default_factory=dict)


def _classify_dimensions_from_graph(
    program: Program,
    graph: ProgramGraph,
    all_exprs: list[Expr],
    *,
    scope: list[str] | None = None,
) -> _ClassifyResult | Intractable:
    """Classify dimensions using prebuilt graph/expression context."""
    done_acc_info = _collect_done_acc_pairs(program)

    consumed_accs: set[str] = set()
    for acc_name in done_acc_info.pairs.values():
        if _collect_atoms_for_tag(all_exprs, acc_name):
            consumed_accs.add(acc_name)

    done_acc = {d: a for d, a in done_acc_info.pairs.items() if a not in consumed_accs}
    unconsumed_accs = frozenset(done_acc.values())

    scope_input_tags: frozenset[str] | None = None
    if scope is not None:
        dv = program.dataview()
        upstream_tags: set[str] = set()
        for tag_name in scope:
            upstream_tags.update(dv.upstream(tag_name).inputs().tags)
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

        role = graph.tag_roles.get(tag_name)
        is_written = tag_name in graph.writers_of

        if role == TagRole.INPUT or (tag.external and not is_written):
            if scope_input_tags is not None and tag_name not in scope_input_tags:
                continue
            domain = _extract_value_domain(tag_name, tag, all_exprs, graph.tags)
            if domain is None:
                infeasible_tags.append(tag_name)
                continue
            if domain:
                nondeterministic[tag_name] = domain
            continue

        if not is_written:
            continue

        if _is_ote_only(tag_name, graph):
            combinational.add(tag_name)
            continue

        if tag_name in done_acc:
            stateful[tag_name] = (False, PENDING, True)
            continue

        domain = _extract_value_domain(tag_name, tag, all_exprs, graph.tags)
        if domain is None:
            infeasible_tags.append(tag_name)
            continue
        if domain:
            stateful[tag_name] = domain

    if infeasible_tags:
        total_dims = len(stateful) + len(nondeterministic) + len(infeasible_tags)
        return Intractable(
            reason=f"unbounded domain on {', '.join(sorted(infeasible_tags))}",
            dimensions=total_dims,
            estimated_space=0,
            tags=sorted(infeasible_tags),
        )

    fn_escape = _detect_function_escape_hatches(program, graph)
    if fn_escape:
        total_dims = len(stateful) + len(nondeterministic) + len(fn_escape)
        return Intractable(
            reason=f"unannotated function output: {', '.join(sorted(fn_escape))}",
            dimensions=total_dims,
            estimated_space=0,
            tags=sorted(fn_escape),
        )

    done_presets = {d: p for d, p in done_acc_info.presets.items() if d in done_acc}
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


# ---------------------------------------------------------------------------
# Don't-care pruning
# ---------------------------------------------------------------------------


def _eval_atom(atom: Atom, value: Any) -> bool | None:
    """Evaluate a single atom against a concrete value.

    Returns None for rise/fall (needs prev — cannot evaluate statically).
    """
    form = atom.form
    if form == "xic":
        return bool(value)
    if form == "xio":
        return not bool(value)
    if form == "truthy":
        return bool(value)
    if form == "rise" or form == "fall":
        return None
    op = atom.operand
    if form == "eq":
        return value == op
    if form == "ne":
        return value != op
    if form == "lt":
        return value < op
    if form == "le":
        return value <= op
    if form == "gt":
        return value > op
    if form == "ge":
        return value >= op
    return None


def _partial_eval(expr: Expr, known: dict[str, Any]) -> Expr:
    """Substitute known tag values and simplify."""
    if isinstance(expr, Const):
        return expr

    if isinstance(expr, Atom):
        if expr.tag in known:
            result = _eval_atom(expr, known[expr.tag])
            if result is not None:
                return Const(result)
        return expr

    if isinstance(expr, And):
        terms: list[Expr] = []
        for t in expr.terms:
            evaled = _partial_eval(t, known)
            if isinstance(evaled, Const):
                if not evaled.value:
                    return Const(False)
                continue
            terms.append(evaled)
        if not terms:
            return Const(True)
        return And(tuple(terms)) if len(terms) > 1 else terms[0]

    if isinstance(expr, Or):
        terms = []
        for t in expr.terms:
            evaled = _partial_eval(t, known)
            if isinstance(evaled, Const):
                if evaled.value:
                    return Const(True)
                continue
            terms.append(evaled)
        if not terms:
            return Const(False)
        return Or(tuple(terms)) if len(terms) > 1 else terms[0]

    return expr


def _referenced_tags(expr: Expr) -> frozenset[str]:
    """Collect all tag names still referenced in an expression."""
    tags: set[str] = set()
    _walk_tags(expr, tags)
    return frozenset(tags)


def _walk_tags(expr: Expr, out: set[str]) -> None:
    if isinstance(expr, Atom):
        out.add(expr.tag)
    elif isinstance(expr, (And, Or)):
        for t in expr.terms:
            _walk_tags(t, out)


def _live_inputs(
    state: dict[str, Any],
    nd_dims: dict[str, tuple[Any, ...]],
    all_exprs: list[Expr],
) -> frozenset[str]:
    """Determine which nondeterministic inputs are live at a given state."""
    nd_names = frozenset(nd_dims)
    known = {k: v for k, v in state.items() if k not in nd_names}

    live: set[str] = set()
    for expr in all_exprs:
        residual = _partial_eval(expr, known)
        if isinstance(residual, Const):
            continue
        live.update(_referenced_tags(residual) & nd_names)

    return frozenset(live)


# ---------------------------------------------------------------------------
# Edge tag compression
# ---------------------------------------------------------------------------

_EDGE_DEAD: Any = object()


def _has_edge_atom(expr: Expr, tag_name: str) -> bool:
    """True if *expr* contains a rise/fall atom for *tag_name*."""
    if isinstance(expr, Atom):
        return expr.tag == tag_name and expr.form in ("rise", "fall")
    if isinstance(expr, (And, Or)):
        return any(_has_edge_atom(t, tag_name) for t in expr.terms)
    return False


def _collect_edge_tag_exprs(
    program: Program,
    edge_tag_names: tuple[str, ...],
) -> dict[str, list[Expr]]:
    """For each edge tag, collect full rung conditions containing its rise/fall.

    Uses the complete AND of all rung conditions so that partial evaluation
    can resolve masked branches (e.g. ``And(State == IDLE, rise(Sensor))``
    resolves to False when State != IDLE).
    """
    result: dict[str, list[Expr]] = {name: [] for name in edge_tag_names}
    if not edge_tag_names:
        return result
    edge_set = frozenset(edge_tag_names)
    seen: dict[str, set[int]] = {name: set() for name in edge_tag_names}
    for rung_idx, rung in enumerate(program.rungs):
        conds = rung._conditions
        if not conds:
            continue
        if len(conds) == 1:
            expr = _condition_to_expr(conds[0])
        else:
            expr = And(tuple(_condition_to_expr(c) for c in conds))
        for name in edge_set:
            if _has_edge_atom(expr, name) and rung_idx not in seen[name]:
                seen[name].add(rung_idx)
                result[name].append(expr)
    return result


def _live_edge_prevs(
    state: dict[str, Any],
    nd_dims: dict[str, tuple[Any, ...]],
    edge_tag_exprs: dict[str, list[Expr]],
) -> frozenset[str]:
    """Determine which edge tag prev values are live at a given state.

    An edge prev is live if any expression containing its rise/fall atom
    does not resolve to a constant under partial evaluation of known
    (non-nondeterministic) state.
    """
    nd_names = frozenset(nd_dims)
    known = {k: v for k, v in state.items() if k not in nd_names}

    live: set[str] = set()
    for name, exprs in edge_tag_exprs.items():
        for expr in exprs:
            residual = _partial_eval(expr, known)
            if not isinstance(residual, Const):
                live.add(name)
                break
    return frozenset(live)


def _precompute_always_live_edges(
    edge_tag_exprs: dict[str, list[Expr]],
) -> frozenset[str]:
    """Find edge tags whose expressions can never be resolved.

    Bare rise/fall atoms (no surrounding AND/OR with stateful guards)
    are always live regardless of state.
    """
    always_live: set[str] = set()
    for name, exprs in edge_tag_exprs.items():
        for expr in exprs:
            if isinstance(expr, Atom):
                always_live.add(name)
                break
    return frozenset(always_live)


# ---------------------------------------------------------------------------
# Kernel integration
# ---------------------------------------------------------------------------


def _step_kernel(
    context: _ExploreContext,
    kernel: ReplayKernel,
) -> None:
    """Execute one scan cycle on the kernel."""
    kernel.memory["_dt"] = context.dt
    for spec in context.block_specs:
        kernel.load_block_from_tags(spec)
    context.compiled.step_fn(kernel.tags, kernel.blocks, kernel.memory, kernel.prev, context.dt)
    for spec in context.block_specs:
        kernel.flush_block_to_tags(spec)
    for name in context.edge_tag_names:
        if name in kernel.tags:
            kernel.prev[name] = kernel.tags[name]
    kernel.advance(context.dt)


_Snapshot = tuple[
    dict[str, Any],  # tags
    dict[str, list[Any]],  # blocks
    dict[str, Any],  # memory
    dict[str, Any],  # prev
    int,  # scan_id
    float,  # timestamp
]


def _snapshot_kernel(kernel: ReplayKernel) -> _Snapshot:
    """Deep-copy kernel state."""
    return (
        dict(kernel.tags),
        {k: list(v) for k, v in kernel.blocks.items()},
        dict(kernel.memory),
        dict(kernel.prev),
        kernel.scan_id,
        kernel.timestamp,
    )


def _restore_kernel(kernel: ReplayKernel, snap: _Snapshot) -> None:
    """Restore kernel state from a snapshot."""
    tags, blocks, memory, prev, scan_id, timestamp = snap
    kernel.tags.clear()
    kernel.tags.update(tags)
    for k in list(kernel.blocks):
        if k in blocks:
            kernel.blocks[k] = list(blocks[k])
    kernel.memory.clear()
    kernel.memory.update(memory)
    kernel.prev.clear()
    kernel.prev.update(prev)
    kernel.scan_id = scan_id
    kernel.timestamp = timestamp


class _EdgeCompressor:
    """Cached edge-prev liveness for state key compression.

    Edge liveness depends only on stateful dims (non-ND known state).
    This caches the result per stateful-key prefix so the (relatively
    expensive) partial evaluation runs at most once per unique stateful
    configuration, not per combo.
    """

    __slots__ = ("_context", "_compressible", "_cache")

    def __init__(self, context: _ExploreContext) -> None:
        self._context = context
        always_live = _precompute_always_live_edges(context.edge_tag_exprs)
        self._compressible = {
            name: exprs for name, exprs in context.edge_tag_exprs.items() if name not in always_live
        }
        self._cache: dict[tuple[Any, ...], frozenset[str]] = {}

    def live_edges(self, kernel: ReplayKernel) -> frozenset[str] | None:
        """Return the set of live edge tags, or None if no compression."""
        if not self._compressible:
            return None
        ctx = self._context
        stateful_prefix = tuple(kernel.tags.get(n) for n in ctx.stateful_names)
        cached = self._cache.get(stateful_prefix)
        if cached is not None:
            return cached
        result = _live_edge_prevs(
            kernel.tags,
            ctx.nondeterministic_dims,
            self._compressible,
        )
        self._cache[stateful_prefix] = result
        return result

    def state_key(self, kernel: ReplayKernel) -> tuple[Any, ...]:
        ctx = self._context
        return _extract_state_key(
            kernel,
            ctx.stateful_names,
            ctx.edge_tag_names,
            ctx.state_key_done_specs,
            self.live_edges(kernel),
        )


def _extract_state_key(
    kernel: ReplayKernel,
    stateful_names: tuple[str, ...],
    edge_tag_names: tuple[str, ...],
    done_specs: tuple[_StateKeyDoneSpec, ...] = (),
    live_edges: frozenset[str] | None = None,
) -> tuple[Any, ...]:
    """Hash key for the visited set — stateful dims + edge prev values.

    Timer/counter Done bits use three-valued abstraction
    ``(False, PENDING, True)`` derived from Done + Acc.

    When *live_edges* is provided, edge tags not in the set use a sentinel
    value, collapsing states that differ only in irrelevant prev values.
    """
    parts = [kernel.tags.get(name) for name in stateful_names]
    for spec in done_specs:
        parts[spec.index] = _done_acc_state(
            spec.kind,
            parts[spec.index],
            kernel.tags.get(spec.acc_name),
        )
    for n in edge_tag_names:
        if live_edges is not None and n not in live_edges:
            parts.append(_EDGE_DEAD)
        else:
            parts.append(kernel.prev.get(n))
    return tuple(parts)


def _build_explore_context(
    program: Program,
    *,
    scope: list[str] | None = None,
    dt: float = 0.010,
) -> _ExploreContext | Intractable:
    """Build shared verifier context once for prove()/reachable_states()."""
    from pyrung.circuitpy.codegen import compile_kernel

    graph = build_program_graph(program)
    all_exprs = _collect_all_exprs(program, graph, scope=scope)
    result = _classify_dimensions_from_graph(program, graph, all_exprs, scope=scope)
    if isinstance(result, Intractable):
        return result
    stateful_dims, nondeterministic_dims, _comb, done_acc, done_presets, done_kinds = result

    compiled = compile_kernel(program)
    stateful_names = tuple(sorted(stateful_dims))
    edge_tag_names = tuple(sorted(compiled.edge_tags))

    state_key_done_specs: list[_StateKeyDoneSpec] = []
    done_event_specs: list[_DoneEventSpec] = []
    for index, done_name in enumerate(stateful_names):
        acc_name = done_acc.get(done_name)
        if acc_name is None:
            continue
        kind = done_kinds[done_name]
        state_key_done_specs.append(_StateKeyDoneSpec(index=index, acc_name=acc_name, kind=kind))
        preset = done_presets.get(done_name)
        if preset is not None:
            done_event_specs.append(
                _DoneEventSpec(
                    state_index=index,
                    acc_name=acc_name,
                    kind=kind,
                    preset=preset,
                )
            )

    edge_tag_exprs = _collect_edge_tag_exprs(program, edge_tag_names)

    return _ExploreContext(
        compiled=compiled,
        graph=graph,
        all_exprs=all_exprs,
        stateful_dims=stateful_dims,
        nondeterministic_dims=nondeterministic_dims,
        stateful_names=stateful_names,
        edge_tag_names=edge_tag_names,
        state_key_done_specs=tuple(state_key_done_specs),
        done_event_specs=tuple(done_event_specs),
        block_specs=tuple(compiled.block_specs.values()),
        dt=dt,
        edge_tag_exprs=edge_tag_exprs,
    )


def _projected_tuple(kernel: ReplayKernel, project_names: tuple[str, ...]) -> tuple[Any, ...]:
    """Project kernel state onto a fixed ordered list of tag names."""
    return tuple(kernel.tags.get(name) for name in project_names)


def _projected_states(
    project_names: tuple[str, ...],
    projected_rows: set[tuple[Any, ...]],
) -> frozenset[frozenset[tuple[str, Any]]]:
    """Convert ordered projection rows to the public frozenset shape."""
    return frozenset(frozenset(zip(project_names, row, strict=True)) for row in projected_rows)


# ---------------------------------------------------------------------------
# BFS core
# ---------------------------------------------------------------------------


def _build_trace(
    parent_map: dict[tuple[Any, ...], tuple[tuple[Any, ...] | None, dict[str, Any]]],
    failing_key: tuple[Any, ...],
    failing_inputs: dict[str, Any],
) -> list[dict[str, Any]]:
    """Reconstruct the input trace from initial state to failure."""
    trace: list[dict[str, Any]] = [failing_inputs]
    current = failing_key
    while current in parent_map:
        parent_key, inputs = parent_map[current]
        if parent_key is None:
            break
        trace.append(inputs)
        current = parent_key
    trace.reverse()
    return trace


def _compile_expr_evaluator(expr: Expr) -> Callable[[dict[str, Any]], bool | None]:
    """Compile an Expr into a tri-state evaluator.

    Returns ``True``/``False`` when the expression is decidable from the
    concrete state dict, otherwise ``None`` for residual edge-sensitive terms
    like ``rise()``/``fall()``.
    """
    if isinstance(expr, Const):
        value = bool(expr.value)
        return lambda _state: value

    if isinstance(expr, Atom):
        tag = expr.tag
        form = expr.form
        operand = expr.operand

        def _eval_atom_from_state(state: dict[str, Any]) -> bool | None:
            if form in {"rise", "fall"}:
                return None
            if tag not in state:
                return None

            value = state[tag]
            resolved_operand = (
                state[operand] if isinstance(operand, str) and operand in state else operand
            )

            if form == "xic":
                return bool(value)
            if form == "xio":
                return not bool(value)
            if form == "truthy":
                return bool(value)
            if form == "eq":
                return value == resolved_operand
            if form == "ne":
                return value != resolved_operand
            if form == "lt":
                return value < resolved_operand
            if form == "le":
                return value <= resolved_operand
            if form == "gt":
                return value > resolved_operand
            if form == "ge":
                return value >= resolved_operand
            return None

        return _eval_atom_from_state

    if isinstance(expr, And):
        term_fns = tuple(_compile_expr_evaluator(term) for term in expr.terms)

        def _eval_and(state: dict[str, Any]) -> bool | None:
            saw_unknown = False
            for fn in term_fns:
                result = fn(state)
                if result is False:
                    return False
                if result is None:
                    saw_unknown = True
            return None if saw_unknown else True

        return _eval_and

    term_fns = tuple(_compile_expr_evaluator(term) for term in expr.terms)

    def _eval_or(state: dict[str, Any]) -> bool | None:
        saw_unknown = False
        for fn in term_fns:
            result = fn(state)
            if result is True:
                return True
            if result is None:
                saw_unknown = True
        return None if saw_unknown else False

    return _eval_or


def _compile_property_spec(
    spec: Any,
) -> tuple[Callable[[dict[str, Any]], bool], list[str] | None]:
    """Compile one property spec into a predicate and optional auto-scope.

    ``spec`` may be a single condition/callable or a tuple of conditions with
    implicit AND semantics.
    """
    if isinstance(spec, tuple):
        return _compile_property(*spec)
    return _compile_property(spec)


def _normalize_property_specs(*conditions: Any) -> tuple[bool, list[Any]]:
    """Split prove() inputs into single-property or batch-property form.

    A sole list argument means "batch prove these properties". Tuple items
    inside that list represent grouped AND terms for one property.
    """
    if len(conditions) == 1 and isinstance(conditions[0], list):
        property_specs = list(conditions[0])
        if not property_specs:
            raise ValueError("prove() property list cannot be empty")
        return True, property_specs

    if not conditions:
        raise ValueError("prove() requires at least one condition")
    if len(conditions) == 1:
        return False, [conditions[0]]
    return False, [tuple(conditions)]


def _timer_total(kernel: ReplayKernel, acc_name: str) -> float:
    """Return timer progress as accumulator plus fractional remainder."""
    frac_key = f"_frac:{acc_name}"
    acc = int(kernel.tags.get(acc_name, 0) or 0)
    frac = float(kernel.memory.get(frac_key, 0.0) or 0.0)
    return acc + frac


def _scans_until_done_event(
    kind: str,
    preset: int,
    acc_name: str,
    before: _Snapshot,
    kernel: ReplayKernel,
) -> int | None:
    """Estimate scans until this pending timer/counter reaches its next Done event."""
    before_tags, _blocks, before_memory, _prev, _scan_id, _timestamp = before
    acc_before = int(before_tags.get(acc_name, 0) or 0)
    acc_after = int(kernel.tags.get(acc_name, 0) or 0)

    if kind in {_DONE_KIND_ON_DELAY, _DONE_KIND_OFF_DELAY}:
        before_total = acc_before + float(before_memory.get(f"_frac:{acc_name}", 0.0) or 0.0)
        after_total = _timer_total(kernel, acc_name)
        delta = after_total - before_total
        remaining = preset - after_total
    elif kind == _DONE_KIND_COUNT_UP:
        delta = acc_after - acc_before
        remaining = preset - acc_after
    else:
        delta = acc_before - acc_after
        remaining = preset + acc_after

    if delta <= 0:
        return None
    if remaining <= 0:
        return 1
    return max(1, int(math.ceil(remaining / delta)))


def _advance_hidden_progress(
    kind: str,
    acc_name: str,
    skipped_scans: int,
    before: _Snapshot,
    kernel: ReplayKernel,
) -> None:
    """Advance a hidden timer/counter through skipped scans before the event scan."""
    if skipped_scans <= 0:
        return

    before_tags, _blocks, before_memory, _prev, _scan_id, _timestamp = before
    acc_before = int(before_tags.get(acc_name, 0) or 0)
    acc_after = int(kernel.tags.get(acc_name, 0) or 0)

    if kind in {_DONE_KIND_ON_DELAY, _DONE_KIND_OFF_DELAY}:
        before_total = acc_before + float(before_memory.get(f"_frac:{acc_name}", 0.0) or 0.0)
        after_total = _timer_total(kernel, acc_name)
        delta = after_total - before_total
        target_total = after_total + (skipped_scans * delta)
        target_acc = int(target_total)
        kernel.tags[acc_name] = target_acc
        kernel.memory[f"_frac:{acc_name}"] = target_total - target_acc
        return

    if kind == _DONE_KIND_COUNT_UP:
        delta = acc_after - acc_before
        kernel.tags[acc_name] = acc_after + (skipped_scans * delta)
        return

    delta = acc_before - acc_after
    kernel.tags[acc_name] = acc_after - (skipped_scans * delta)


def _has_pending_done(context: _ExploreContext, key: tuple[Any, ...]) -> bool:
    """True if any timer/counter Done bit in *key* is PENDING."""
    return any(key[spec.state_index] == PENDING for spec in context.done_event_specs)


def _resolve_nearest_pending(
    context: _ExploreContext,
    kernel: ReplayKernel,
    before_snap: _Snapshot,
    key: tuple[Any, ...],
    edge_comp: _EdgeCompressor,
) -> tuple[Any, ...] | None:
    """Advance to the nearest pending timer/counter completion and step once.

    Returns the new state key, or ``None`` if no pending events can be resolved.
    *before_snap* must precede the current kernel state by one step.
    """
    pending_events: list[tuple[_DoneEventSpec, int]] = []
    for spec in context.done_event_specs:
        if key[spec.state_index] != PENDING:
            continue
        scans = _scans_until_done_event(spec.kind, spec.preset, spec.acc_name, before_snap, kernel)
        if scans is not None:
            pending_events.append((spec, scans))

    if not pending_events:
        return None

    next_event_scans = min(scans for _spec, scans in pending_events)
    skipped_scans = max(next_event_scans - 1, 0)
    for spec, _scans in pending_events:
        _advance_hidden_progress(spec.kind, spec.acc_name, skipped_scans, before_snap, kernel)

    _step_kernel(context, kernel)
    return edge_comp.state_key(kernel)


def _settle_pending(
    context: _ExploreContext,
    kernel: ReplayKernel,
    before_snap: _Snapshot,
    edge_comp: _EdgeCompressor,
) -> tuple[Any, ...]:
    """Resolve all pending timers/counters so the system reaches a stable state.

    *before_snap* must be from before the most recent ``_step_kernel`` call
    so that the per-scan delta can be computed (acc_after − acc_before).
    """
    key = edge_comp.state_key(kernel)
    for _ in range(len(context.done_event_specs) + 1):
        resolved = _resolve_nearest_pending(context, kernel, before_snap, key, edge_comp)
        if resolved is None:
            break
        before_snap = _snapshot_kernel(kernel)
        key = resolved
    return key


def _maybe_jump_hidden_event(
    context: _ExploreContext,
    kernel: ReplayKernel,
    snap: _Snapshot,
    visited: set[tuple[Any, ...]],
    new_key: tuple[Any, ...],
    edge_comp: _EdgeCompressor,
) -> tuple[tuple[Any, ...], bool]:
    """Jump from a revisited hidden pending plateau to the next completion event."""
    if not context.done_event_specs or new_key not in visited:
        return new_key, False

    resolved = _resolve_nearest_pending(context, kernel, snap, new_key, edge_comp)
    if resolved is None:
        return new_key, False
    return resolved, True


def _bfs_explore(
    context: _ExploreContext,
    *,
    predicate: Callable[[dict[str, Any]], bool] | None = None,
    project: tuple[str, ...] | None = None,
    max_depth: int = 50,
    max_states: int = 100_000,
) -> Proven | Counterexample | Intractable | frozenset[frozenset[tuple[str, Any]]]:
    """BFS over the reachable state space."""
    kernel = context.compiled.create_kernel()
    edge_comp = _EdgeCompressor(context)
    initial_key = edge_comp.state_key(kernel)

    visited: set[tuple[Any, ...]] = {initial_key}
    parent_map: dict[tuple[Any, ...], tuple[tuple[Any, ...] | None, dict[str, Any]]] | None = (
        {} if predicate is not None else None
    )
    projected_rows: set[tuple[Any, ...]] = set()

    if project is not None:
        projected_rows.add(_projected_tuple(kernel, project))

    if predicate is not None and not predicate(kernel.tags):
        return Counterexample(trace=[{}])

    queue: deque[tuple[_Snapshot, int, tuple[Any, ...]]] = deque()
    queue.append((_snapshot_kernel(kernel), 0, initial_key))

    while queue:
        snap, depth, parent_key = queue.popleft()

        if depth >= max_depth:
            continue

        _restore_kernel(kernel, snap)
        live = _live_inputs(kernel.tags, context.nondeterministic_dims, context.all_exprs)

        if live:
            live_sorted = sorted(live)
            domains = [context.nondeterministic_dims[n] for n in live_sorted]
            combos: Any = itertools.product(*domains)
        else:
            live_sorted = []
            combos = [()]

        seen_outcomes: set[tuple[tuple[Any, ...], tuple[Any, ...]]] | None = (
            set() if project is not None else None
        )
        for combo in combos:
            _restore_kernel(kernel, snap)

            input_dict: dict[str, Any] = {}
            for i, name in enumerate(live_sorted):
                kernel.tags[name] = combo[i]
                input_dict[name] = combo[i]

            _step_kernel(context, kernel)

            if predicate is not None and not predicate(kernel.tags):
                new_key = edge_comp.state_key(kernel)
                if context.done_event_specs and _has_pending_done(context, new_key):
                    new_key = _settle_pending(context, kernel, snap, edge_comp)
                if not predicate(kernel.tags):
                    assert parent_map is not None
                    trace = _build_trace(parent_map, parent_key, input_dict)
                    trace.append(input_dict)
                    return Counterexample(trace=trace)

            new_key = edge_comp.state_key(kernel)

            new_key, jumped = _maybe_jump_hidden_event(
                context, kernel, snap, visited, new_key, edge_comp
            )
            if jumped and predicate is not None and not predicate(kernel.tags):
                assert parent_map is not None
                trace = _build_trace(parent_map, parent_key, input_dict)
                trace.append(input_dict)
                return Counterexample(trace=trace)

            if project is not None:
                projected_row = _projected_tuple(kernel, project)
                outcome = (new_key, projected_row)
                assert seen_outcomes is not None
                if outcome in seen_outcomes:
                    continue
                seen_outcomes.add(outcome)
                projected_rows.add(projected_row)

            if new_key not in visited:
                visited.add(new_key)
                if len(visited) > max_states:
                    return Intractable(
                        reason="max_states exceeded",
                        dimensions=len(context.stateful_dims) + len(context.nondeterministic_dims),
                        estimated_space=len(visited),
                    )
                if parent_map is not None:
                    parent_map[new_key] = (parent_key, input_dict)
                queue.append((_snapshot_kernel(kernel), depth + 1, new_key))

    if project is not None:
        return _projected_states(project, projected_rows)

    return Proven(states_explored=len(visited))


def _bfs_explore_many(
    context: _ExploreContext,
    *,
    predicates: list[Callable[[dict[str, Any]], bool]],
    max_depth: int = 50,
    max_states: int = 100_000,
) -> list[Proven | Counterexample | Intractable]:
    """BFS over the reachable state space for multiple properties at once."""
    kernel = context.compiled.create_kernel()
    edge_comp = _EdgeCompressor(context)
    initial_key = edge_comp.state_key(kernel)

    visited: set[tuple[Any, ...]] = {initial_key}
    parent_map: dict[tuple[Any, ...], tuple[tuple[Any, ...] | None, dict[str, Any]]] = {}
    results: list[Counterexample | Proven | Intractable | None] = [None] * len(predicates)

    def _record_failures(
        *,
        state: dict[str, Any],
        parent_key: tuple[Any, ...],
        input_dict: dict[str, Any],
        initial: bool = False,
    ) -> None:
        for i, predicate in enumerate(predicates):
            if results[i] is not None:
                continue
            if predicate(state):
                continue
            if initial:
                results[i] = Counterexample(trace=[{}])
                continue
            trace = _build_trace(parent_map, parent_key, input_dict)
            trace.append(input_dict)
            results[i] = Counterexample(trace=trace)

    _record_failures(state=kernel.tags, parent_key=initial_key, input_dict={}, initial=True)
    if all(result is not None for result in results):
        return [result for result in results if result is not None]

    queue: deque[tuple[_Snapshot, int, tuple[Any, ...]]] = deque()
    queue.append((_snapshot_kernel(kernel), 0, initial_key))

    while queue:
        snap, depth, parent_key = queue.popleft()

        if depth >= max_depth:
            continue

        _restore_kernel(kernel, snap)
        live = _live_inputs(kernel.tags, context.nondeterministic_dims, context.all_exprs)

        if live:
            live_sorted = sorted(live)
            domains = [context.nondeterministic_dims[n] for n in live_sorted]
            combos: Any = itertools.product(*domains)
        else:
            live_sorted = []
            combos = [()]

        for combo in combos:
            _restore_kernel(kernel, snap)

            input_dict: dict[str, Any] = {}
            for i, name in enumerate(live_sorted):
                kernel.tags[name] = combo[i]
                input_dict[name] = combo[i]

            _step_kernel(context, kernel)

            any_unsettled = any(
                results[i] is None and not predicates[i](kernel.tags)
                for i in range(len(predicates))
            )
            new_key = edge_comp.state_key(kernel)
            if any_unsettled and context.done_event_specs and _has_pending_done(context, new_key):
                new_key = _settle_pending(context, kernel, snap, edge_comp)

            _record_failures(state=kernel.tags, parent_key=parent_key, input_dict=input_dict)

            new_key, jumped = _maybe_jump_hidden_event(
                context, kernel, snap, visited, new_key, edge_comp
            )
            if jumped:
                _record_failures(state=kernel.tags, parent_key=parent_key, input_dict=input_dict)

            if new_key not in visited:
                visited.add(new_key)
                if len(visited) > max_states:
                    intractable = Intractable(
                        reason="max_states exceeded",
                        dimensions=len(context.stateful_dims) + len(context.nondeterministic_dims),
                        estimated_space=len(visited),
                    )
                    return [result if result is not None else intractable for result in results]
                parent_map[new_key] = (parent_key, input_dict)
                queue.append((_snapshot_kernel(kernel), depth + 1, new_key))

            if all(result is not None for result in results):
                return [result for result in results if result is not None]

    return [
        result if result is not None else Proven(states_explored=len(visited)) for result in results
    ]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _compile_property(
    *conditions: Any,
) -> tuple[Callable[[dict[str, Any]], bool], list[str] | None]:
    """Normalize a condition expression or callable into a dict predicate.

    Returns ``(predicate_fn, auto_scope)`` where *auto_scope* is a list of
    referenced tag names (for automatic upstream-cone restriction) or ``None``
    when the caller passed an opaque callable.
    """
    if len(conditions) == 1 and callable(conditions[0]) and not _is_condition_like(conditions[0]):
        user_predicate = conditions[0]

        def _predicate(state: dict[str, Any]) -> bool:
            return bool(user_predicate(dict(state)))

        return _predicate, None

    from pyrung.core.condition import _as_condition, _normalize_and_condition

    normalized = _normalize_and_condition(
        *conditions,
        coerce=_as_condition,
        empty_error="prove() requires at least one condition",
        group_empty_error="prove() condition group cannot be empty",
    )
    expr = _condition_to_expr(normalized)
    tags_in_expr = sorted(_referenced_tags(expr))
    evaluator = _compile_expr_evaluator(expr)

    def _predicate(state: dict[str, Any]) -> bool:
        return evaluator(state) is not False

    return _predicate, tags_in_expr


def _is_condition_like(obj: Any) -> bool:
    """True if *obj* is a Tag or Condition (not a plain callable)."""
    from pyrung.core.condition import Condition
    from pyrung.core.tag import Tag

    return isinstance(obj, (Tag, Condition))


def prove(
    program: Program,
    *conditions: Any,
    scope: list[str] | None = None,
    max_depth: int = 50,
    max_states: int = 100_000,
) -> Proven | Counterexample | Intractable | list[Proven | Counterexample | Intractable]:
    """Exhaustively prove a property over all reachable states.

    Accepts the same condition syntax as ``Rung()`` and ``when()``::

        prove(logic, Or(~Running, EstopOK))
        prove(logic, ~Running, EstopOK)        # implicit AND
        prove(logic, (Ready, AutoMode))        # grouped AND as one property
        prove(logic, [prop_a, prop_b, prop_c]) # batch prove in one pass
        prove(logic, lambda s: s["Running"] <= s["Limit"])

    When given condition expressions, the upstream cone is derived
    automatically — no ``scope=`` needed.

    Parameters
    ----------
    program : Program
        The compiled ladder logic program.
    *conditions : Tag, Condition, callable, tuple, or list
        One property, or a sole list of properties for batch proving.
        Tuple terms represent grouped AND conditions for one property.
        Tag/Condition expressions are preferred; a callable
        ``(state_dict) -> bool`` is accepted as a fallback.
    scope : list of tag names, optional
        Override automatic scope derivation.
    max_depth : int
        BFS depth limit (scan cycles).
    max_states : int
        Visited-set cap — bail with ``Intractable`` if exceeded.
    """
    is_batch, property_specs = _normalize_property_specs(*conditions)
    compiled_properties = [_compile_property_spec(spec) for spec in property_specs]

    if scope is not None:
        effective_scope = scope
    else:
        auto_scopes = [auto_scope for _predicate, auto_scope in compiled_properties]
        if any(auto_scope is None for auto_scope in auto_scopes):
            effective_scope = None
        else:
            merged_scope: set[str] = set()
            for auto_scope in auto_scopes:
                assert auto_scope is not None
                merged_scope.update(auto_scope)
            effective_scope = sorted(merged_scope)

    context = _build_explore_context(program, scope=effective_scope)
    if isinstance(context, Intractable):
        if is_batch:
            return [context for _ in property_specs]
        return context

    if is_batch:
        predicates = [predicate for predicate, _auto_scope in compiled_properties]
        return _bfs_explore_many(
            context,
            predicates=predicates,
            max_depth=max_depth,
            max_states=max_states,
        )

    predicate, _auto_scope = compiled_properties[0]
    return _bfs_explore(  # ty: ignore[invalid-return-type]
        context,
        predicate=predicate,
        max_depth=max_depth,
        max_states=max_states,
    )


def reachable_states(
    program: Program,
    scope: list[str] | None = None,
    project: list[str] | None = None,
    max_depth: int = 50,
    max_states: int = 100_000,
) -> frozenset[frozenset[tuple[str, Any]]] | Intractable:
    """Compute the full reachable state space.

    Parameters
    ----------
    program : Program
        The compiled ladder logic program.
    scope : list of tag names, optional
        If given, restrict input enumeration to the upstream cone.
    project : list of tag names, optional
        Tags to project onto. Defaults to terminal tags.
    max_depth : int
        BFS depth limit (scan cycles).
    max_states : int
        Visited-set cap.
    """
    context = _build_explore_context(program, scope=scope)
    if isinstance(context, Intractable):
        return context

    project_names = tuple(project) if project is not None else tuple(_default_projection(program))
    return _bfs_explore(  # ty: ignore[invalid-return-type]
        context,
        project=project_names,
        max_depth=max_depth,
        max_states=max_states,
    )


def diff_states(
    before: frozenset[frozenset[tuple[str, Any]]],
    after: frozenset[frozenset[tuple[str, Any]]],
) -> StateDiff:
    """Compare two reachable state sets."""
    return StateDiff(added=after - before, removed=before - after)


def _default_projection(program: Program) -> list[str]:
    """Choose default projection tags: terminal outputs only."""
    dv = program.dataview()
    return sorted(dv.terminals().tags)


# ---------------------------------------------------------------------------
# Lock file
# ---------------------------------------------------------------------------


def _states_to_json(
    states: frozenset[frozenset[tuple[str, Any]]],
) -> list[dict[str, Any]]:
    """Convert state frozensets to sorted list of dicts."""
    rows = [dict(sorted(s)) for s in states]
    rows.sort(key=lambda d: tuple(sorted(d.items())))
    return rows


def _json_to_states(
    rows: list[dict[str, Any]],
) -> frozenset[frozenset[tuple[str, Any]]]:
    """Convert list of dicts back to state frozensets."""
    return frozenset(frozenset(d.items()) for d in rows)


def write_lock(
    path: Path,
    states: frozenset[frozenset[tuple[str, Any]]],
    projection: list[str],
    program_hash: str,
    unreachable_examples: list[dict[str, Any]] | None = None,
) -> None:
    """Write a state-space lock file."""
    data = {
        "version": 1,
        "program_hash": program_hash,
        "projection": sorted(projection),
        "reachable": _states_to_json(states),
        "unreachable_examples": unreachable_examples or [],
    }
    path.write_text(json.dumps(data, indent=2, default=_json_default) + "\n")


def _json_default(obj: Any) -> Any:
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, (int, float, str)):
        return obj
    msg = f"Object of type {type(obj).__name__} is not JSON serializable"
    raise TypeError(msg)


def read_lock(path: Path) -> dict[str, Any]:
    """Read a state-space lock file."""
    return json.loads(path.read_text())


def program_hash(program: Program) -> str:
    """Compute a hash of the program's compiled kernel source."""
    from pyrung.circuitpy.codegen import compile_kernel

    compiled = compile_kernel(program)
    return hashlib.sha256(compiled.source.encode()).hexdigest()[:16]


def check_lock(
    program: Program,
    lock_path: Path = Path("pyrung.lock"),
    max_depth: int = 50,
    max_states: int = 100_000,
) -> StateDiff | None:
    """Recompute reachable states and diff against a lock file.

    Returns None if the lock matches, or a ``StateDiff`` if changed.
    """
    lock_data = read_lock(lock_path)
    projection = lock_data["projection"]
    old_states = _json_to_states(lock_data["reachable"])

    new_states = reachable_states(
        program,
        project=projection,
        max_depth=max_depth,
        max_states=max_states,
    )
    if isinstance(new_states, Intractable):
        msg = f"Verification intractable: {new_states.reason}"
        raise RuntimeError(msg)

    d = diff_states(old_states, new_states)
    if not d.added and not d.removed:
        return None
    return d
