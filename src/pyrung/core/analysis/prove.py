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
from pyrung.core.kernel import CompiledKernel, ReplayKernel
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

_TIMER_FORWARD_FLOOR = 10_000


@dataclass(frozen=True)
class _DoneAccInfo:
    pairs: dict[str, str]
    max_preset: int


def _collect_done_acc_pairs(program: Program) -> _DoneAccInfo:
    """Map Done tag names to their Acc tag names for timer/counter instructions.

    Also tracks the largest constant preset to size the fast-forward budget.
    """
    from pyrung.core.instruction.counters import CountDownInstruction, CountUpInstruction
    from pyrung.core.instruction.timers import OffDelayInstruction, OnDelayInstruction
    from pyrung.core.tag import Tag
    from pyrung.core.validation._common import walk_instructions

    pairs: dict[str, str] = {}
    max_preset = 0

    for instr in walk_instructions(program):
        if isinstance(
            instr,
            (
                OnDelayInstruction,
                OffDelayInstruction,
                CountUpInstruction,
                CountDownInstruction,
            ),
        ):
            pairs[instr.done_bit.name] = instr.accumulator.name
            if not isinstance(instr.preset, Tag) and isinstance(instr.preset, (int, float)):
                max_preset = max(max_preset, int(instr.preset))

    return _DoneAccInfo(pairs=pairs, max_preset=max_preset)


def _done_acc_state(done_val: Any, acc_val: Any) -> bool | str:
    """Derive the three-valued timer/counter state from Done and Acc."""
    if done_val:
        return True
    if acc_val and acc_val != 0:
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
        dest = instr.destination
        if isinstance(dest, Tag):
            targets = [dest.name]
        else:
            targets = _resolve_tag_names(dest)
    elif isinstance(instr, CalcInstruction):
        dest = instr.destination
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
        targets = _resolve_tag_names(instr.destination)
    elif isinstance(instr, FillInstruction):
        targets = _resolve_tag_names(instr.destination)
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
        targets = _resolve_tag_names(instr.destination)
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


def _extract_value_domain(
    tag_name: str,
    tag: Tag,
    all_exprs: list[Expr],
) -> tuple[Any, ...] | None:
    """Determine the finite value domain for a tag, or None if unbounded."""
    if tag.type == TagType.BOOL:
        return (False, True)

    atoms = _collect_atoms_for_tag(all_exprs, tag_name)
    if not atoms:
        return ()  # not consumed in any condition — excluded

    comparison_forms = {"eq", "ne", "lt", "le", "gt", "ge"}
    literals: set[Any] = set()
    has_tag_comparison = False

    for atom in atoms:
        if atom.form in comparison_forms and atom.operand is not None:
            if isinstance(atom.operand, str):
                has_tag_comparison = True
            else:
                literals.add(atom.operand)

    if tag.choices is not None:
        return tuple(sorted(tag.choices.keys()))

    if tag.min is not None and tag.max is not None:
        domain_size = tag.max - tag.min + 1
        if domain_size > 1000:
            return None
        return tuple(range(int(tag.min), int(tag.max) + 1))

    if has_tag_comparison and not literals:
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
    int,  # max_preset: largest constant timer/counter preset
]


def _classify_dimensions(
    program: Program,
    scope: list[str] | None = None,
) -> _ClassifyResult | Intractable:
    """Partition tags into stateful, nondeterministic, and combinational.

    Returns ``(stateful_dims, nondeterministic_dims, combinational_tags,
    done_acc_pairs)`` where each dim dict maps tag name to its value domain.
    Timer/counter Done bits get a three-valued domain ``(False, PENDING, True)``
    and their Acc tags are excluded from the state space.
    Returns ``Intractable`` when a domain cannot be bounded.
    """
    graph = build_program_graph(program)
    all_exprs = _collect_all_exprs(program, graph, scope=scope)

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
            domain = _extract_value_domain(tag_name, tag, all_exprs)
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

        domain = _extract_value_domain(tag_name, tag, all_exprs)
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

    return stateful, nondeterministic, frozenset(combinational), done_acc, done_acc_info.max_preset


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
# Kernel integration
# ---------------------------------------------------------------------------


def _step_kernel(
    compiled: CompiledKernel,
    kernel: ReplayKernel,
    dt: float,
) -> None:
    """Execute one scan cycle on the kernel."""
    kernel.memory["_dt"] = dt
    for spec in compiled.block_specs.values():
        kernel.load_block_from_tags(spec)
    compiled.step_fn(kernel.tags, kernel.blocks, kernel.memory, kernel.prev, dt)
    for spec in compiled.block_specs.values():
        kernel.flush_block_to_tags(spec)
    kernel.capture_prev(compiled.edge_tags)
    kernel.advance(dt)


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


def _extract_state_key(
    kernel: ReplayKernel,
    stateful_names: tuple[str, ...],
    edge_tag_names: tuple[str, ...],
    done_acc_pairs: dict[str, str],
) -> tuple[Any, ...]:
    """Hash key for the visited set — stateful dims + edge prev values.

    Timer/counter Done bits use three-valued abstraction
    ``(False, PENDING, True)`` derived from Done + Acc.
    """
    parts: list[Any] = []
    for n in stateful_names:
        if n in done_acc_pairs:
            acc_name = done_acc_pairs[n]
            parts.append(_done_acc_state(kernel.tags.get(n), kernel.tags.get(acc_name)))
        else:
            parts.append(kernel.tags.get(n))
    for n in edge_tag_names:
        parts.append(kernel.prev.get(n))
    return tuple(parts)


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


def _bfs_explore(
    compiled: CompiledKernel,
    stateful_dims: dict[str, tuple[Any, ...]],
    nondeterministic_dims: dict[str, tuple[Any, ...]],
    done_acc_pairs: dict[str, str],
    all_exprs: list[Expr],
    *,
    predicate: Callable[[dict[str, Any]], bool] | None = None,
    project: list[str] | None = None,
    max_depth: int = 50,
    max_states: int = 100_000,
    max_preset: int = 0,
    dt: float = 0.010,
) -> Proven | Counterexample | Intractable | frozenset[frozenset[tuple[str, Any]]]:
    """BFS over the reachable state space."""
    forward_budget = max(int(max_preset / dt) if dt > 0 else 0, _TIMER_FORWARD_FLOOR)
    stateful_names = tuple(sorted(stateful_dims))
    edge_tag_names = tuple(sorted(compiled.edge_tags))

    kernel = compiled.create_kernel()
    initial_key = _extract_state_key(kernel, stateful_names, edge_tag_names, done_acc_pairs)

    visited: set[tuple[Any, ...]] = {initial_key}
    parent_map: dict[tuple[Any, ...], tuple[tuple[Any, ...] | None, dict[str, Any]]] = {}
    projected: set[frozenset[tuple[str, Any]]] = set()

    if project is not None:
        projected.add(frozenset((n, kernel.tags.get(n)) for n in project))

    if predicate is not None and not predicate(dict(kernel.tags)):
        return Counterexample(trace=[{}])

    queue: deque[tuple[_Snapshot, int, tuple[Any, ...]]] = deque()
    queue.append((_snapshot_kernel(kernel), 0, initial_key))

    while queue:
        snap, depth, parent_key = queue.popleft()

        if depth >= max_depth:
            continue

        _restore_kernel(kernel, snap)
        current_state = dict(kernel.tags)

        live = _live_inputs(current_state, nondeterministic_dims, all_exprs)

        if live:
            live_sorted = sorted(live)
            domains = [nondeterministic_dims[n] for n in live_sorted]
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

            _step_kernel(compiled, kernel, dt)

            if predicate is not None and not predicate(dict(kernel.tags)):
                trace = _build_trace(parent_map, parent_key, input_dict)
                trace.append(input_dict)
                return Counterexample(trace=trace)

            new_key = _extract_state_key(kernel, stateful_names, edge_tag_names, done_acc_pairs)

            # Fast-forward through Pending timer states: keep stepping
            # (same inputs) until the state key changes or a budget is hit.
            #
            # Soundness: the BFS has already explored all input combos
            # from this PENDING state when it was first enqueued (since
            # new_key ∈ visited).  Fast-forward only fires for re-visited
            # PENDING keys, so it discovers the Pending→Done transition
            # that the BFS can't reach within its depth budget.  Inputs
            # are frozen, so this may explore states not reachable in the
            # real system (the enable condition might go false mid-
            # accumulation).  This makes the verifier sound (no missed
            # violations) but conservative (possible false counterexamples
            # if a violation only occurs with frozen inputs).
            if done_acc_pairs and new_key in visited:
                has_pending = any(v == PENDING for v in new_key[: len(stateful_names)])
                if has_pending:
                    for _ in range(forward_budget):
                        _step_kernel(compiled, kernel, dt)
                        if predicate is not None and not predicate(dict(kernel.tags)):
                            # The trace records the parent-state inputs but not
                            # how many fast-forward steps were taken.  A consumer
                            # replaying this trace won't land at the exact scan
                            # where the violation occurred — the intermediate
                            # accumulation steps are elided.
                            trace = _build_trace(parent_map, parent_key, input_dict)
                            trace.append(input_dict)
                            return Counterexample(trace=trace)
                        candidate = _extract_state_key(
                            kernel, stateful_names, edge_tag_names, done_acc_pairs
                        )
                        if candidate != new_key:
                            new_key = candidate
                            break

            if project is not None:
                projected.add(frozenset((n, kernel.tags.get(n)) for n in project))

            if new_key not in visited:
                visited.add(new_key)
                if len(visited) > max_states:
                    return Intractable(
                        reason="max_states exceeded",
                        dimensions=len(stateful_dims) + len(nondeterministic_dims),
                        estimated_space=len(visited),
                    )
                parent_map[new_key] = (parent_key, input_dict)
                queue.append((_snapshot_kernel(kernel), depth + 1, new_key))

    if project is not None:
        return frozenset(projected)

    return Proven(states_explored=len(visited))


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
        return conditions[0], None

    from pyrung.core.condition import _as_condition, _normalize_and_condition

    normalized = _normalize_and_condition(
        *conditions,
        coerce=_as_condition,
        empty_error="prove() requires at least one condition",
        group_empty_error="prove() condition group cannot be empty",
    )
    expr = _condition_to_expr(normalized)
    tags_in_expr = sorted(_referenced_tags(expr))

    def _predicate(state: dict[str, Any]) -> bool:
        result = _partial_eval(expr, state)
        if isinstance(result, Const):
            return bool(result.value)
        return True

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
) -> Proven | Counterexample | Intractable:
    """Exhaustively prove a property over all reachable states.

    Accepts the same condition syntax as ``Rung()`` and ``when()``::

        prove(logic, Or(~Running, EstopOK))
        prove(logic, ~Running, EstopOK)        # implicit AND
        prove(logic, lambda s: s["Running"] <= s["Limit"])

    When given condition expressions, the upstream cone is derived
    automatically — no ``scope=`` needed.

    Parameters
    ----------
    program : Program
        The compiled ladder logic program.
    *conditions : Tag, Condition, or callable
        The property to prove.  Tag/Condition expressions are preferred;
        a callable ``(state_dict) -> bool`` is accepted as a fallback.
    scope : list of tag names, optional
        Override automatic scope derivation.
    max_depth : int
        BFS depth limit (scan cycles).
    max_states : int
        Visited-set cap — bail with ``Intractable`` if exceeded.
    """
    from pyrung.circuitpy.codegen import compile_kernel

    predicate, auto_scope = _compile_property(*conditions)
    effective_scope = scope if scope is not None else auto_scope

    result = _classify_dimensions(program, effective_scope)
    if isinstance(result, Intractable):
        return result
    stateful_dims, nd_dims, _combinational, done_acc, max_preset = result

    graph = build_program_graph(program)
    all_exprs = _collect_all_exprs(program, graph, scope=effective_scope)

    compiled = compile_kernel(program)
    return _bfs_explore(  # ty: ignore[invalid-return-type]
        compiled,
        stateful_dims,
        nd_dims,
        done_acc,
        all_exprs,
        predicate=predicate,
        max_depth=max_depth,
        max_states=max_states,
        max_preset=max_preset,
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
        Tags to project onto. Defaults to ``public`` tags, then terminals.
    max_depth : int
        BFS depth limit (scan cycles).
    max_states : int
        Visited-set cap.
    """
    from pyrung.circuitpy.codegen import compile_kernel

    result = _classify_dimensions(program, scope)
    if isinstance(result, Intractable):
        return result
    stateful_dims, nd_dims, _combinational, done_acc, max_preset = result

    if project is None:
        project = _default_projection(program)

    graph = build_program_graph(program)
    all_exprs = _collect_all_exprs(program, graph, scope=scope)

    compiled = compile_kernel(program)
    return _bfs_explore(  # ty: ignore[invalid-return-type]
        compiled,
        stateful_dims,
        nd_dims,
        done_acc,
        all_exprs,
        project=project,
        max_depth=max_depth,
        max_states=max_states,
        max_preset=max_preset,
    )


def diff_states(
    before: frozenset[frozenset[tuple[str, Any]]],
    after: frozenset[frozenset[tuple[str, Any]]],
) -> StateDiff:
    """Compare two reachable state sets."""
    return StateDiff(added=after - before, removed=before - after)


def _default_projection(program: Program) -> list[str]:
    """Choose default projection tags: public first, then terminals."""
    graph = build_program_graph(program)
    public = [name for name, tag in graph.tags.items() if tag.public]
    if public:
        return sorted(public)
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
