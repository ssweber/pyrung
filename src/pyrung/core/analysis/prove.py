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

    trace: list[TraceStep]


@dataclass(frozen=True)
class TraceStep:
    inputs: dict[str, Any]
    scans: int = 1


@dataclass(frozen=True)
class Intractable:
    """Verification cannot complete within resource bounds."""

    reason: str
    dimensions: int
    estimated_space: int
    tags: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)


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
_PROGRESS_KIND_INT_UP = "int_up"

_THRESHOLD_FORM_GT = "gt"
_THRESHOLD_FORM_GE = "ge"


@dataclass(frozen=True)
class _DoneAccInfo:
    pairs: dict[str, str]
    presets: dict[str, int]
    preset_tags: dict[str, str]
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
    preset_tags: dict[str, str] = {}
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
        if isinstance(instr.preset, Tag):
            preset_tags[instr.done_bit.name] = instr.preset.name
        elif isinstance(instr.preset, (int, float)):
            presets[instr.done_bit.name] = int(instr.preset)

    return _DoneAccInfo(pairs=pairs, presets=presets, preset_tags=preset_tags, kinds=kinds)


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
        if expr.tag == tag_name or expr.operand == tag_name:
            out.append(expr)
    elif isinstance(expr, (And, Or)):
        for t in expr.terms:
            _walk_atoms(t, tag_name, out)


def _is_stable_dynamic_preset(preset_tag_name: str, graph: ProgramGraph) -> bool:
    """True when a dynamic preset is frozen or owned by the ladder."""
    tag = graph.tags.get(preset_tag_name)
    if tag is None:
        return False
    if tag.readonly:
        return True
    return bool(tag.final and preset_tag_name in graph.writers_of)


def _atom_matches_acc_preset_boundary(
    atom: Atom,
    acc_name: str,
    preset_match_values: frozenset[Any],
) -> bool:
    """True if *atom* is one side of the Acc/Preset done threshold."""
    if atom.tag == acc_name:
        return atom.operand in preset_match_values and atom.form in {"ge", "lt"}
    if atom.operand == acc_name:
        return atom.tag in preset_match_values and atom.form in {"le", "gt"}
    return False


def _is_acc_done_redundant(
    acc_name: str,
    preset_match_values: frozenset[Any],
    kind: str,
    atoms: list[Atom],
) -> bool:
    """True when every accumulator atom is representable by Done/Pending/True."""
    if kind == _DONE_KIND_COUNT_DOWN or not atoms:
        return False
    return all(
        _atom_matches_acc_preset_boundary(atom, acc_name, preset_match_values) for atom in atoms
    )


def _all_atoms_absorbed(
    preset_atoms: list[Atom],
    acc_name: str,
    preset_match_values: frozenset[Any],
) -> bool:
    """True when every preset atom is the same absorbed Acc/Preset threshold."""
    return all(
        _atom_matches_acc_preset_boundary(atom, acc_name, preset_match_values)
        for atom in preset_atoms
    )


def _is_matching_timer_preset_read(instr: Any, done_name: str, acc_name: str) -> bool:
    return (
        getattr(getattr(instr, "done_bit", None), "name", None) == done_name
        and getattr(getattr(instr, "accumulator", None), "name", None) == acc_name
    )


def _has_non_timer_data_read(
    program: Program,
    preset_tag_name: str,
    done_name: str,
    acc_name: str,
) -> bool:
    """Detect value-flow uses of a preset outside its matching timer/counter."""
    from pyrung.core.analysis.pdg import _extract_tag_names
    from pyrung.core.validation._common import walk_instructions

    for instr in walk_instructions(program):
        if _is_matching_timer_preset_read(instr, done_name, acc_name):
            continue
        for field_name in getattr(type(instr), "_reads", ()):
            refs = _extract_tag_names(getattr(instr, field_name), {})
            if preset_tag_name in refs:
                return True
    return False


def _preset_match_values(preset_tag_name: str, graph: ProgramGraph) -> frozenset[Any]:
    """Values that may represent a stable preset in simplified atoms."""
    values: set[Any] = {preset_tag_name}
    tag = graph.tags.get(preset_tag_name)
    if tag is not None and tag.readonly:
        values.add(tag.default)
    return frozenset(values)


@dataclass(frozen=True)
class _RedundantAccAbsorptions:
    acc_names: frozenset[str]
    preset_tags: frozenset[str]
    synthetic_presets: dict[str, int]


def _find_redundant_acc_absorptions(
    program: Program,
    graph: ProgramGraph,
    all_exprs: list[Expr],
    done_acc_info: _DoneAccInfo,
    consumed_accs: set[str],
) -> _RedundantAccAbsorptions:
    """Find dynamic timer presets whose Acc/Preset comparisons are redundant."""
    absorbed_accs: set[str] = set()
    absorbed_preset_tags: set[str] = set()
    synthetic_presets: dict[str, int] = {}

    for done_name, acc_name in done_acc_info.pairs.items():
        if acc_name not in consumed_accs:
            continue
        preset_tag_name = done_acc_info.preset_tags.get(done_name)
        if preset_tag_name is None:
            continue
        if not _is_stable_dynamic_preset(preset_tag_name, graph):
            continue

        kind = done_acc_info.kinds[done_name]
        match_values = _preset_match_values(preset_tag_name, graph)
        acc_atoms = _collect_atoms_for_tag(all_exprs, acc_name)
        if not _is_acc_done_redundant(acc_name, match_values, kind, acc_atoms):
            continue

        preset_atoms = _collect_atoms_for_tag(all_exprs, preset_tag_name)
        if not _all_atoms_absorbed(preset_atoms, acc_name, match_values):
            continue
        if _has_non_timer_data_read(program, preset_tag_name, done_name, acc_name):
            continue

        absorbed_accs.add(acc_name)
        absorbed_preset_tags.add(preset_tag_name)
        synthetic_presets[done_name] = 1

    return _RedundantAccAbsorptions(
        acc_names=frozenset(absorbed_accs),
        preset_tags=frozenset(absorbed_preset_tags),
        synthetic_presets=synthetic_presets,
    )


def _is_numeric_literal(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_stable_threshold(value: Any, graph: ProgramGraph) -> bool:
    """True when a threshold value is fixed for verifier event scheduling."""
    if _is_numeric_literal(value):
        return True
    if not isinstance(value, str):
        return False
    tag = graph.tags.get(value)
    if tag is None or tag.external or tag.public:
        return False
    if tag.readonly:
        return True
    return bool(tag.final and value in graph.writers_of)


def _threshold_atom_for_progress(
    atom: Atom,
    acc_name: str,
    graph: ProgramGraph,
) -> _ThresholdAtomSpec | None:
    """Normalize supported Progress/Threshold comparison atoms."""
    if atom.tag == acc_name and atom.form in {_THRESHOLD_FORM_GT, _THRESHOLD_FORM_GE}:
        if _is_stable_threshold(atom.operand, graph):
            return _ThresholdAtomSpec(acc_name, atom.operand, atom.form)
        return None

    if atom.operand == acc_name and atom.form in {"lt", "le"}:
        if not _is_stable_threshold(atom.tag, graph):
            return None
        form = _THRESHOLD_FORM_GT if atom.form == "lt" else _THRESHOLD_FORM_GE
        return _ThresholdAtomSpec(acc_name, atom.tag, form)

    return None


def _diagnose_unstable_atom(
    atom: Atom,
    acc_name: str,
    graph: ProgramGraph,
) -> str | None:
    """Return a human-readable reason when an atom blocks threshold abstraction."""
    if atom.tag == acc_name and atom.form in {_THRESHOLD_FORM_GT, _THRESHOLD_FORM_GE}:
        threshold = atom.operand
    elif atom.operand == acc_name and atom.form in {"lt", "le"}:
        threshold = atom.tag
    else:
        if atom.form in {"eq", "ne"}:
            return (
                f"compared with {atom.form}"
                " — only monotonic threshold comparisons (>, >=) can be abstracted"
            )
        return (
            "compared as below-threshold"
            " — only upward-crossing (Acc > T, Acc >= T) can be abstracted"
        )

    if _is_numeric_literal(threshold):
        return None
    if not isinstance(threshold, str):
        return "non-tag threshold operand"
    tag = graph.tags.get(threshold)
    if tag is None:
        return f"{threshold}: unknown tag"
    if tag.external:
        return f"{threshold}: external — add readonly=True or bounded metadata (min=/max=)"
    if tag.public:
        return f"{threshold}: public — add readonly=True or bounded metadata (min=/max=)"
    return f"{threshold}: not stable — add readonly=True or final=True (with a write)"


def _threshold_tag_name(spec: _ThresholdAtomSpec) -> str | None:
    return spec.threshold if isinstance(spec.threshold, str) else None


def _is_matching_owner_preset_read(instr: Any, threshold_name: str, acc_name: str) -> bool:
    """Allow a threshold tag to also be the owning timer/counter preset."""
    from pyrung.core.tag import Tag

    if getattr(getattr(instr, "accumulator", None), "name", None) != acc_name:
        return False
    preset = getattr(instr, "preset", None)
    return isinstance(preset, Tag) and preset.name == threshold_name


def _direct_write_target(instr: Any) -> Any:
    """Return a direct Tag write target for copy/calc-like instructions."""
    from pyrung.core.instruction.calc import CalcInstruction
    from pyrung.core.instruction.data_transfer import CopyInstruction
    from pyrung.core.tag import Tag

    if isinstance(instr, CopyInstruction):
        target = instr.target
    elif isinstance(instr, CalcInstruction):
        target = instr.dest
    else:
        return None
    return target if isinstance(target, Tag) else None


def _is_zero_literal(value: Any) -> bool:
    from pyrung.core.expression import Expression, LiteralExpr

    if isinstance(value, LiteralExpr):
        return value.value == 0
    if isinstance(value, Expression):
        return False
    return value == 0 and not isinstance(value, bool)


def _tag_expr_name(value: Any) -> str | None:
    from pyrung.core.expression import TagExpr

    return value.tag.name if isinstance(value, TagExpr) else None


def _literal_expr_value(value: Any) -> Any:
    from pyrung.core.expression import LiteralExpr

    return value.value if isinstance(value, LiteralExpr) else None


def _is_unit_self_increment_expr(value: Any, tag_name: str) -> bool:
    from pyrung.core.expression import BinaryExpr

    if not isinstance(value, BinaryExpr) or value.symbol != "+":
        return False
    left_tag = _tag_expr_name(value.left)
    right_tag = _tag_expr_name(value.right)
    left_lit = _literal_expr_value(value.left)
    right_lit = _literal_expr_value(value.right)
    return (left_tag == tag_name and right_lit == 1) or (right_tag == tag_name and left_lit == 1)


def _is_int_progress_write(instr: Any, tag_name: str) -> bool:
    """True for the exact reset/self-increment writes accepted by v1."""
    from pyrung.core.instruction.calc import CalcInstruction
    from pyrung.core.instruction.data_transfer import CopyInstruction

    target = _direct_write_target(instr)
    if target is None or target.name != tag_name:
        return False
    if isinstance(instr, CopyInstruction):
        source = instr.source
    elif isinstance(instr, CalcInstruction):
        source = instr.expression
    else:
        return False
    return _is_zero_literal(source) or _is_unit_self_increment_expr(source, tag_name)


def _is_zero_copy_to_tag(instr: Any, tag_name: str) -> bool:
    """True for a plain direct ``copy(0, tag)`` reset write."""
    from pyrung.core.instruction.data_transfer import CopyInstruction

    if not isinstance(instr, CopyInstruction):
        return False
    target = _direct_write_target(instr)
    return (
        target is not None
        and target.name == tag_name
        and instr.convert is None
        and _is_zero_literal(instr.source)
    )


def _collect_progress_source_kinds(program: Program) -> dict[str, str]:
    """Find instruction-owned progress accumulators and recognized int counters."""
    from pyrung.core.instruction.counters import CountUpInstruction
    from pyrung.core.instruction.timers import OffDelayInstruction, OnDelayInstruction
    from pyrung.core.validation._common import walk_instructions

    kinds: dict[str, str] = {}
    invalid: set[str] = set()

    for instr in walk_instructions(program):
        if isinstance(instr, OnDelayInstruction):
            acc_name = instr.accumulator.name
            kind = _DONE_KIND_ON_DELAY
        elif isinstance(instr, OffDelayInstruction):
            acc_name = instr.accumulator.name
            kind = _DONE_KIND_OFF_DELAY
        elif isinstance(instr, CountUpInstruction) and instr.down_condition is None:
            acc_name = instr.accumulator.name
            kind = _DONE_KIND_COUNT_UP
        else:
            continue

        if acc_name in kinds and kinds[acc_name] != kind:
            invalid.add(acc_name)
        else:
            kinds[acc_name] = kind

    for name in invalid:
        kinds.pop(name, None)

    return kinds


def _has_forbidden_data_read(
    program: Program,
    tag_name: str,
    *,
    allowed: Callable[[Any], bool] | None = None,
) -> bool:
    """Detect non-condition data-flow reads of a candidate absorbed tag."""
    from pyrung.core.analysis.pdg import _extract_tag_names
    from pyrung.core.validation._common import walk_instructions

    for instr in walk_instructions(program):
        for field_name in getattr(type(instr), "_reads", ()):
            refs = _extract_tag_names(getattr(instr, field_name), {})
            if tag_name not in refs:
                continue
            if allowed is not None and allowed(instr):
                continue
            return True
    return False


def _has_only_owner_writes(program: Program, acc_name: str, kind: str) -> bool:
    """True when a progress accumulator has only owner/reset-safe writes."""
    from pyrung.core.instruction.counters import CountUpInstruction
    from pyrung.core.instruction.timers import OffDelayInstruction, OnDelayInstruction
    from pyrung.core.validation._common import walk_instructions

    saw_owner = False
    for instr in walk_instructions(program):
        if acc_name not in {name for name, _itype in _all_write_targets(instr)}:
            continue

        is_owner = False
        if kind == _DONE_KIND_ON_DELAY:
            is_owner = isinstance(instr, OnDelayInstruction) and instr.accumulator.name == acc_name
        elif kind == _DONE_KIND_OFF_DELAY:
            is_owner = isinstance(instr, OffDelayInstruction) and instr.accumulator.name == acc_name
        elif kind == _DONE_KIND_COUNT_UP:
            is_owner = (
                isinstance(instr, CountUpInstruction)
                and instr.accumulator.name == acc_name
                and instr.down_condition is None
            )
        if not is_owner:
            if kind in {
                _DONE_KIND_ON_DELAY,
                _DONE_KIND_OFF_DELAY,
                _DONE_KIND_COUNT_UP,
            } and _is_zero_copy_to_tag(instr, acc_name):
                continue
            return False
        saw_owner = True
    return saw_owner


def _collect_int_progress_source_kinds(
    program: Program,
    graph: ProgramGraph,
    all_exprs: list[Expr],
) -> dict[str, str]:
    """Find internal integer progress counters implemented as reset/+1 writes."""
    from pyrung.core.validation._common import walk_instructions

    result: dict[str, str] = {}
    by_target: dict[str, list[Any]] = {}
    for instr in walk_instructions(program):
        for target_name, _itype in _all_write_targets(instr):
            by_target.setdefault(target_name, []).append(instr)

    for tag_name, tag in graph.tags.items():
        if tag.type not in {TagType.INT, TagType.DINT}:
            continue
        if tag.external or tag.public or tag.readonly:
            continue
        atoms = _collect_atoms_for_tag(all_exprs, tag_name)
        if not atoms:
            continue
        writes = by_target.get(tag_name, [])
        if not writes or not all(_is_int_progress_write(instr, tag_name) for instr in writes):
            continue
        if _has_forbidden_data_read(
            program,
            tag_name,
            allowed=lambda instr, name=tag_name: _is_int_progress_write(instr, name),
        ):
            continue
        result[tag_name] = _PROGRESS_KIND_INT_UP

    return result


def _find_threshold_absorptions(
    program: Program,
    graph: ProgramGraph,
    all_exprs: list[Expr],
    *,
    project: tuple[str, ...] | None = None,
) -> _ThresholdAbsorptions:
    """Find progress accumulator threshold comparisons that can be event-abstracted."""
    projected = frozenset(project or ())
    source_kinds = _collect_progress_source_kinds(program)
    source_kinds.update(_collect_int_progress_source_kinds(program, graph, all_exprs))

    candidate_vectors: dict[str, _ThresholdVectorSpec] = {}
    threshold_progress: dict[str, set[str]] = {}
    blockers: list[_ThresholdBlocker] = []

    for acc_name, kind in sorted(source_kinds.items()):
        if acc_name in projected:
            continue
        tag = graph.tags.get(acc_name)
        if tag is not None and (tag.external or tag.public):
            continue
        atoms = _collect_atoms_for_tag(all_exprs, acc_name)
        if not atoms:
            continue

        normalized: list[_ThresholdAtomSpec] = []
        atom_reasons: list[str] = []
        blocked = False
        for atom in atoms:
            spec = _threshold_atom_for_progress(atom, acc_name, graph)
            if spec is None:
                reason = _diagnose_unstable_atom(atom, acc_name, graph)
                if reason:
                    atom_reasons.append(reason)
                blocked = True
            else:
                normalized.append(spec)
        if blocked:
            if atom_reasons:
                seen: set[str] = set()
                unique = []
                for r in atom_reasons:
                    if r not in seen:
                        seen.add(r)
                        unique.append(r)
                blockers.append(_ThresholdBlocker(acc_name, kind, tuple(unique)))
            normalized = []
        if not normalized:
            continue

        if kind != _PROGRESS_KIND_INT_UP:
            if not _has_only_owner_writes(program, acc_name, kind):
                blockers.append(
                    _ThresholdBlocker(
                        acc_name,
                        kind,
                        (f"{acc_name}: has non-owner writes — remove direct assignments",),
                    )
                )
                continue
            if _has_forbidden_data_read(program, acc_name):
                blockers.append(
                    _ThresholdBlocker(
                        acc_name,
                        kind,
                        (f"{acc_name}: read in data-flow — remove copy/calc reads of accumulator",),
                    )
                )
                continue

        unique_atoms = tuple(dict.fromkeys(normalized))
        projected_thresholds = [
            _threshold_tag_name(spec)
            for spec in unique_atoms
            if _threshold_tag_name(spec) in projected
        ]
        if projected_thresholds:
            blockers.append(
                _ThresholdBlocker(
                    acc_name,
                    kind,
                    tuple(
                        f"{name}: in projection — remove from project= to allow abstraction"
                        for name in projected_thresholds
                    ),
                )
            )
            continue

        candidate_vectors[acc_name] = _ThresholdVectorSpec(
            acc_name=acc_name,
            kind=kind,
            atoms=unique_atoms,
        )
        for spec in unique_atoms:
            threshold_name = _threshold_tag_name(spec)
            if threshold_name is not None:
                threshold_progress.setdefault(threshold_name, set()).add(acc_name)

    shared_thresholds = {name for name, accs in threshold_progress.items() if len(accs) > 1}
    absorbed_progress: set[str] = set()
    absorbed_thresholds: set[str] = set()
    vector_specs: list[_ThresholdVectorSpec] = []

    for acc_name, vector in candidate_vectors.items():
        threshold_names = {
            name for spec in vector.atoms if (name := _threshold_tag_name(spec)) is not None
        }
        if threshold_names & shared_thresholds:
            shared = threshold_names & shared_thresholds
            blockers.append(
                _ThresholdBlocker(
                    acc_name,
                    vector.kind,
                    tuple(f"{name}: shared with other accumulators" for name in sorted(shared)),
                )
            )
            continue
        forbidden_reasons: list[str] = []
        for threshold_name in threshold_names:
            threshold_atoms = _collect_atoms_for_tag(all_exprs, threshold_name)
            if not all(
                _threshold_atom_for_progress(atom, acc_name, graph) is not None
                for atom in threshold_atoms
            ):
                forbidden_reasons.append(
                    f"{threshold_name}: also used in non-threshold comparisons"
                )
                break
            if _has_forbidden_data_read(
                program,
                threshold_name,
                allowed=lambda instr, t=threshold_name, a=acc_name: _is_matching_owner_preset_read(
                    instr, t, a
                ),
            ):
                forbidden_reasons.append(
                    f"{threshold_name}: read in data-flow outside accumulator comparisons"
                )
                break
        if forbidden_reasons:
            blockers.append(_ThresholdBlocker(acc_name, vector.kind, tuple(forbidden_reasons)))
            continue

        absorbed_progress.add(acc_name)
        absorbed_thresholds.update(threshold_names)
        vector_specs.append(vector)

    return _ThresholdAbsorptions(
        progress_names=frozenset(absorbed_progress),
        threshold_tags=frozenset(absorbed_thresholds),
        vector_specs=tuple(vector_specs),
        blockers=tuple(blockers),
    )


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
    atoms = _collect_atoms_for_tag(all_exprs, tag_name)
    if not atoms:
        return ()

    if tag.type == TagType.BOOL:
        return (False, True)

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
class _ThresholdAtomSpec:
    acc_name: str
    threshold: int | float | str
    form: str


@dataclass(frozen=True)
class _ThresholdVectorSpec:
    acc_name: str
    kind: str
    atoms: tuple[_ThresholdAtomSpec, ...]


@dataclass(frozen=True)
class _ThresholdEventSpec:
    vector_index: int
    atom_index: int
    acc_name: str
    kind: str
    threshold: int | float | str
    form: str


@dataclass(frozen=True)
class _ThresholdBlocker:
    acc_name: str
    kind: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class _ThresholdAbsorptions:
    progress_names: frozenset[str]
    threshold_tags: frozenset[str]
    vector_specs: tuple[_ThresholdVectorSpec, ...]
    blockers: tuple[_ThresholdBlocker, ...] = ()


@dataclass(frozen=True)
class _ExploreContext:
    compiled: CompiledKernel
    graph: ProgramGraph
    all_exprs: list[Expr]
    stateful_dims: dict[str, tuple[Any, ...]]
    nondeterministic_dims: dict[str, tuple[Any, ...]]
    stateful_names: tuple[str, ...]
    edge_tag_names: tuple[str, ...]
    memory_key_names: tuple[str, ...]
    state_key_done_specs: tuple[_StateKeyDoneSpec, ...]
    done_event_specs: tuple[_DoneEventSpec, ...]
    threshold_vector_specs: tuple[_ThresholdVectorSpec, ...]
    threshold_event_specs: tuple[_ThresholdEventSpec, ...]
    block_specs: tuple[BlockSpec, ...]
    dt: float
    edge_tag_exprs: dict[str, list[Expr]] = field(default_factory=dict)
    synthetic_preset_tags: tuple[str, ...] = ()


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
                if discovered_domains is not None and tag_name in discovered_domains:
                    stateful[tag_name] = discovered_domains[tag_name]
                else:
                    infeasible_tags.append(tag_name)
                continue

        domain = _extract_value_domain(tag_name, tag, all_exprs, graph.tags)
        if domain is None:
            if discovered_domains is not None and tag_name in discovered_domains:
                stateful[tag_name] = discovered_domains[tag_name]
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


# ---------------------------------------------------------------------------
# Kernel domain discovery
# ---------------------------------------------------------------------------


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
            eval_expr = expr
            if isinstance(expr.operand, str):
                if expr.operand not in known:
                    return expr
                eval_expr = Atom(expr.tag, expr.form, known[expr.operand])
            result = _eval_atom(eval_expr, known[expr.tag])
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
        if isinstance(expr.operand, str):
            out.add(expr.operand)
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


def _seed_synthetic_presets(context: _ExploreContext, kernel: ReplayKernel) -> None:
    """Seed absorbed dynamic presets away from their default zero value."""
    for name in context.synthetic_preset_tags:
        kernel.tags[name] = 1


@dataclass(frozen=True, slots=True)
class _KernelSnapshot:
    tags: dict[str, Any]
    blocks: dict[str, list[Any]]
    memory: dict[str, Any]
    prev: dict[str, Any]
    scan_id: int
    timestamp: float


def _snapshot_kernel(kernel: ReplayKernel) -> _KernelSnapshot:
    """Deep-copy kernel state."""
    return _KernelSnapshot(
        tags=dict(kernel.tags),
        blocks={k: list(v) for k, v in kernel.blocks.items()},
        memory=dict(kernel.memory),
        prev=dict(kernel.prev),
        scan_id=kernel.scan_id,
        timestamp=kernel.timestamp,
    )


def _restore_kernel(kernel: ReplayKernel, snap: _KernelSnapshot) -> None:
    """Restore kernel state from a snapshot."""
    kernel.tags.clear()
    kernel.tags.update(snap.tags)
    for k in list(kernel.blocks):
        if k in snap.blocks:
            kernel.blocks[k] = list(snap.blocks[k])
    kernel.memory.clear()
    kernel.memory.update(snap.memory)
    kernel.prev.clear()
    kernel.prev.update(snap.prev)
    kernel.scan_id = snap.scan_id
    kernel.timestamp = snap.timestamp


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
        threshold_prefix = _threshold_vector_key(kernel, ctx.threshold_vector_specs)
        stateful_prefix = stateful_prefix + threshold_prefix
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
            ctx.memory_key_names,
            ctx.state_key_done_specs,
            ctx.threshold_vector_specs,
            self.live_edges(kernel),
        )


def _threshold_value(kernel: ReplayKernel, threshold: int | float | str) -> Any:
    if isinstance(threshold, str):
        return kernel.tags.get(threshold)
    return threshold


def _threshold_crossed(
    kernel: ReplayKernel,
    acc_name: str,
    threshold: int | float | str,
    form: str,
) -> bool:
    acc_value = kernel.tags.get(acc_name)
    threshold_value = _threshold_value(kernel, threshold)
    if acc_value is None or threshold_value is None:
        return False
    if form == _THRESHOLD_FORM_GT:
        return acc_value > threshold_value
    return acc_value >= threshold_value


def _threshold_vector_key(
    kernel: ReplayKernel,
    specs: tuple[_ThresholdVectorSpec, ...],
) -> tuple[Any, ...]:
    result: list[Any] = []
    for spec in specs:
        result.append(
            tuple(
                _threshold_crossed(kernel, spec.acc_name, atom.threshold, atom.form)
                for atom in spec.atoms
            )
        )
    return tuple(result)


def _extract_state_key(
    kernel: ReplayKernel,
    stateful_names: tuple[str, ...],
    edge_tag_names: tuple[str, ...],
    memory_key_names: tuple[str, ...] = (),
    done_specs: tuple[_StateKeyDoneSpec, ...] = (),
    threshold_vector_specs: tuple[_ThresholdVectorSpec, ...] = (),
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
    parts.extend(_threshold_vector_key(kernel, threshold_vector_specs))
    for n in edge_tag_names:
        if live_edges is not None and n not in live_edges:
            parts.append(_EDGE_DEAD)
        else:
            parts.append(kernel.prev.get(n))
    for mk in memory_key_names:
        parts.append(kernel.memory.get(mk))
    return tuple(parts)


@dataclass
class _PassContext:
    """Mutable accumulator built up by pre-BFS passes."""

    program: Program
    scope: list[str] | None
    project: tuple[str, ...] | None
    extra_exprs: list[Expr] | None
    dt: float
    compiled: CompiledKernel | None

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
            block_specs=tuple(self.compiled.block_specs.values()),
            dt=self.dt,
            edge_tag_exprs=self.edge_tag_exprs or {},
            synthetic_preset_tags=self.synthetic_preset_tags or (),
        )


@dataclass(frozen=True)
class _PreBFSPass:
    name: str
    description: str
    run: Callable[[_PassContext], None]
    enabled: bool = True


@dataclass(frozen=True)
class _BFSConfig:
    """Enable/disable flags for BFS-interleaved optimizations."""

    live_input_pruning: bool = True
    edge_compression: bool = True
    hidden_event_jumping: bool = True
    pending_settlement: bool = True

    @property
    def active_optimizations(self) -> tuple[str, ...]:
        names: list[str] = []
        if self.live_input_pruning:
            names.append("live_input_pruning")
        if self.edge_compression:
            names.append("edge_compression")
        if self.hidden_event_jumping:
            names.append("hidden_event_jumping")
        if self.pending_settlement:
            names.append("pending_settlement")
        return tuple(names)


_DEFAULT_BFS_CONFIG = _BFSConfig()

# ---------------------------------------------------------------------------
# Individual pre-BFS passes
# ---------------------------------------------------------------------------


def _pass_build_graph(ctx: _PassContext) -> None:
    ctx.graph = build_program_graph(ctx.program)
    ctx.all_exprs = _collect_all_exprs(ctx.program, ctx.graph, scope=ctx.scope)
    if ctx.extra_exprs:
        ctx.all_exprs = ctx.all_exprs + ctx.extra_exprs


def _pass_classify_dimensions(ctx: _PassContext) -> None:
    assert ctx.graph is not None and ctx.all_exprs is not None
    result = _classify_dimensions_from_graph(
        ctx.program,
        ctx.graph,
        ctx.all_exprs,
        scope=ctx.scope,
        project=ctx.project,
    )
    if isinstance(result, Intractable):
        ctx.intractable = result
        return
    sd, nd, _comb, da, dp, dk = result
    ctx.stateful_dims = sd
    ctx.nondeterministic_dims = nd
    ctx.done_acc = da
    ctx.done_presets = dp
    ctx.done_kinds = dk


def _pass_pilot_sweep(ctx: _PassContext) -> None:
    from pyrung.circuitpy.codegen import compile_kernel as _compile_kernel

    if ctx.intractable is None or not ctx.intractable.tags:
        return
    assert ctx.graph is not None and ctx.all_exprs is not None
    if ctx.compiled is None:
        ctx.compiled = _compile_kernel(ctx.program)
    first_pass_nd: dict[str, tuple[Any, ...]] = {}
    for tag_name, tag in ctx.graph.tags.items():
        role = ctx.graph.tag_roles.get(tag_name)
        is_written = tag_name in ctx.graph.writers_of
        if not (role == TagRole.INPUT or (tag.external and not is_written)):
            continue
        domain = _extract_value_domain(tag_name, tag, ctx.all_exprs, ctx.graph.tags)
        if not domain:
            if tag.choices is not None:
                domain = tuple(sorted(tag.choices.keys()))
            elif tag.min is not None and tag.max is not None:
                range_size = int(tag.max - tag.min + 1)
                if range_size <= 1000:
                    domain = tuple(range(int(tag.min), int(tag.max) + 1))
        if domain:
            first_pass_nd[tag_name] = domain
    discovered = _pilot_sweep_domains(
        ctx.compiled,
        ctx.intractable.tags,
        first_pass_nd,
        ctx.graph,
        dt=ctx.dt,
    )
    if discovered:
        result = _classify_dimensions_from_graph(
            ctx.program,
            ctx.graph,
            ctx.all_exprs,
            scope=ctx.scope,
            project=ctx.project,
            discovered_domains=discovered,
        )
        if isinstance(result, Intractable):
            ctx.intractable = result
        else:
            sd, nd, _comb, da, dp, dk = result
            ctx.stateful_dims = sd
            ctx.nondeterministic_dims = nd
            ctx.done_acc = da
            ctx.done_presets = dp
            ctx.done_kinds = dk
            ctx.intractable = None


def _pass_compile_kernel(ctx: _PassContext) -> None:
    from pyrung.circuitpy.codegen import compile_kernel as _compile_kernel

    if ctx.compiled is None:
        ctx.compiled = _compile_kernel(ctx.program)
    assert ctx.stateful_dims is not None
    ctx.stateful_names = tuple(sorted(ctx.stateful_dims))
    ctx.edge_tag_names = tuple(sorted(ctx.compiled.edge_tags))


def _pass_collect_done_acc_pairs(ctx: _PassContext) -> None:
    ctx.done_acc_info = _collect_done_acc_pairs(ctx.program)


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


def _pass_find_threshold_absorptions(ctx: _PassContext) -> None:
    assert ctx.graph is not None and ctx.all_exprs is not None
    ctx.threshold_absorptions = _find_threshold_absorptions(
        ctx.program,
        ctx.graph,
        ctx.all_exprs,
        project=ctx.project,
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
        preset = ctx.done_presets.get(done_name)
        if preset is not None:
            d_events.append(
                _DoneEventSpec(state_index=index, acc_name=acc_name, kind=kind, preset=preset)
            )
    ctx.state_key_done_specs = tuple(sk_done)
    ctx.done_event_specs = tuple(d_events)

    t_events: list[_ThresholdEventSpec] = []
    for vi, vector in enumerate(ctx.threshold_absorptions.vector_specs):
        for ai, atom in enumerate(vector.atoms):
            t_events.append(
                _ThresholdEventSpec(
                    vector_index=vi,
                    atom_index=ai,
                    acc_name=vector.acc_name,
                    kind=vector.kind,
                    threshold=atom.threshold,
                    form=atom.form,
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
    pilot.memory["_dt"] = ctx.dt
    for spec in ctx.compiled.block_specs.values():
        pilot.load_block_from_tags(spec)
    ctx.compiled.step_fn(pilot.tags, pilot.blocks, pilot.memory, pilot.prev, ctx.dt)
    excluded_prefixes = ("_dt", "_frac:")
    ctx.memory_key_names = tuple(
        sorted(k for k in pilot.memory if not any(k.startswith(p) for p in excluded_prefixes))
    )


_DEFAULT_PRE_BFS_PASSES: tuple[_PreBFSPass, ...] = (
    _PreBFSPass(
        "build_graph", "Build program dependency graph and collect expressions", _pass_build_graph
    ),
    _PreBFSPass(
        "classify_dimensions",
        "Partition tags into stateful/nondeterministic/combinational",
        _pass_classify_dimensions,
    ),
    _PreBFSPass(
        "pilot_sweep",
        "Discover finite domains for unbounded tags via kernel execution",
        _pass_pilot_sweep,
    ),
    _PreBFSPass(
        "compile_kernel",
        "Compile the replay kernel and derive stateful/edge tag names",
        _pass_compile_kernel,
    ),
    _PreBFSPass(
        "collect_done_acc_pairs",
        "Map Done tags to their accumulator partners",
        _pass_collect_done_acc_pairs,
    ),
    _PreBFSPass(
        "find_redundant_absorptions",
        "Identify accumulators absorbed into Done bit state",
        _pass_find_redundant_absorptions,
    ),
    _PreBFSPass(
        "find_threshold_absorptions",
        "Identify threshold jumping patterns for hidden accumulators",
        _pass_find_threshold_absorptions,
    ),
    _PreBFSPass(
        "build_event_specs",
        "Construct Done and threshold event specifications",
        _pass_build_event_specs,
    ),
    _PreBFSPass(
        "collect_edge_exprs",
        "Build expression map for edge tag compression",
        _pass_collect_edge_exprs,
    ),
    _PreBFSPass(
        "discover_memory_keys",
        "Discover kernel memory keys via pilot scan",
        _pass_discover_memory_keys,
    ),
)


def _run_pre_bfs_pipeline(
    ctx: _PassContext,
    passes: tuple[_PreBFSPass, ...] = _DEFAULT_PRE_BFS_PASSES,
) -> _ExploreContext | Intractable:
    for p in passes:
        if not p.enabled:
            continue
        p.run(ctx)
        if ctx.intractable is not None and p.name not in {"classify_dimensions"}:
            return ctx.intractable
    return ctx.freeze()


def _build_explore_context(
    program: Program,
    *,
    scope: list[str] | None = None,
    project: tuple[str, ...] | None = None,
    extra_exprs: list[Expr] | None = None,
    dt: float = 0.010,
    compiled: CompiledKernel | None = None,
) -> _ExploreContext | Intractable:
    """Build shared verifier context once for prove()/reachable_states()."""
    ctx = _PassContext(
        program=program,
        scope=scope,
        project=project,
        extra_exprs=extra_exprs,
        dt=dt,
        compiled=compiled,
    )
    return _run_pre_bfs_pipeline(ctx)


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
    parent_map: dict[tuple[Any, ...], tuple[tuple[Any, ...] | None, dict[str, Any], int]],
    key: tuple[Any, ...],
) -> list[TraceStep]:
    """Reconstruct the input trace from initial state to failure."""
    trace: list[TraceStep] = []
    current = key
    while current in parent_map:
        parent_key, inputs, scans = parent_map[current]
        trace.append(TraceStep(inputs=inputs, scans=scans))
        if parent_key is None:
            break
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
) -> tuple[Callable[[dict[str, Any]], bool], list[str] | None, Expr | None]:
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
    before: _KernelSnapshot,
    kernel: ReplayKernel,
) -> int | None:
    """Estimate scans until this pending timer/counter reaches its next Done event."""
    acc_before = int(before.tags.get(acc_name, 0) or 0)
    acc_after = int(kernel.tags.get(acc_name, 0) or 0)

    if kind in {_DONE_KIND_ON_DELAY, _DONE_KIND_OFF_DELAY}:
        before_total = acc_before + float(before.memory.get(f"_frac:{acc_name}", 0.0) or 0.0)
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


def _progress_delta_and_current(
    kind: str,
    acc_name: str,
    before: _KernelSnapshot,
    kernel: ReplayKernel,
) -> tuple[float, float] | None:
    acc_before = int(before.tags.get(acc_name, 0) or 0)
    acc_after = int(kernel.tags.get(acc_name, 0) or 0)

    if kind in {_DONE_KIND_ON_DELAY, _DONE_KIND_OFF_DELAY}:
        before_total = acc_before + float(before.memory.get(f"_frac:{acc_name}", 0.0) or 0.0)
        after_total = _timer_total(kernel, acc_name)
        return after_total - before_total, after_total

    if kind in {_DONE_KIND_COUNT_UP, _PROGRESS_KIND_INT_UP}:
        return float(acc_after - acc_before), float(acc_after)

    return None


def _scans_until_threshold_event(
    spec: _ThresholdEventSpec,
    before: _KernelSnapshot,
    kernel: ReplayKernel,
) -> int | None:
    """Estimate scans until an uncrossed threshold atom crosses."""
    threshold_value = _threshold_value(kernel, spec.threshold)
    if not _is_numeric_literal(threshold_value):
        return None

    delta_current = _progress_delta_and_current(spec.kind, spec.acc_name, before, kernel)
    if delta_current is None:
        return None
    delta, current = delta_current
    if delta <= 0:
        return None

    threshold = float(threshold_value)
    if spec.form == _THRESHOLD_FORM_GE:
        if current >= threshold:
            return 1
        return max(1, int(math.ceil((threshold - current) / delta)))

    if current > threshold:
        return 1
    return max(1, int(math.floor((threshold - current) / delta)) + 1)


def _advance_hidden_progress(
    kind: str,
    acc_name: str,
    skipped_scans: int,
    before: _KernelSnapshot,
    kernel: ReplayKernel,
) -> None:
    """Advance a hidden timer/counter through skipped scans before the event scan."""
    if skipped_scans <= 0:
        return

    acc_before = int(before.tags.get(acc_name, 0) or 0)
    acc_after = int(kernel.tags.get(acc_name, 0) or 0)

    if kind in {_DONE_KIND_ON_DELAY, _DONE_KIND_OFF_DELAY}:
        before_total = acc_before + float(before.memory.get(f"_frac:{acc_name}", 0.0) or 0.0)
        after_total = _timer_total(kernel, acc_name)
        delta = after_total - before_total
        target_total = after_total + (skipped_scans * delta)
        target_acc = int(target_total)
        kernel.tags[acc_name] = target_acc
        kernel.memory[f"_frac:{acc_name}"] = target_total - target_acc
        return

    if kind in {_DONE_KIND_COUNT_UP, _PROGRESS_KIND_INT_UP}:
        delta = acc_after - acc_before
        kernel.tags[acc_name] = acc_after + (skipped_scans * delta)
        return

    delta = acc_before - acc_after
    kernel.tags[acc_name] = acc_after - (skipped_scans * delta)


def _has_pending_done(context: _ExploreContext, key: tuple[Any, ...]) -> bool:
    """True if any timer/counter Done bit in *key* is PENDING."""
    return any(key[spec.state_index] == PENDING for spec in context.done_event_specs)


def _has_uncrossed_threshold_event(context: _ExploreContext, key: tuple[Any, ...]) -> bool:
    """True if any threshold vector bit is currently false."""
    offset = len(context.stateful_names)
    for spec in context.threshold_event_specs:
        vector = key[offset + spec.vector_index]
        if not vector[spec.atom_index]:
            return True
    return False


def _has_pending_hidden_event(context: _ExploreContext, key: tuple[Any, ...]) -> bool:
    return _has_pending_done(context, key) or _has_uncrossed_threshold_event(context, key)


def _resolve_nearest_hidden_event(
    context: _ExploreContext,
    kernel: ReplayKernel,
    before_snap: _KernelSnapshot,
    key: tuple[Any, ...],
    edge_comp: _EdgeCompressor,
) -> tuple[tuple[Any, ...], int] | None:
    """Advance to the nearest hidden Done/threshold event and step once.

    Returns ``(new_key, additional_scans)``, or ``None`` if no pending events
    can be resolved. ``additional_scans`` is the skipped scan count beyond the
    caller's already-executed step. *before_snap* must precede the current
    kernel state by one step.
    """
    pending_sources: dict[tuple[str, str], int] = {}
    pending_scans: list[int] = []

    for spec in context.done_event_specs:
        if key[spec.state_index] != PENDING:
            continue
        scans = _scans_until_done_event(spec.kind, spec.preset, spec.acc_name, before_snap, kernel)
        if scans is not None:
            pending_scans.append(scans)
            pending_sources[(spec.kind, spec.acc_name)] = scans

    vector_offset = len(context.stateful_names)
    for spec in context.threshold_event_specs:
        vector = key[vector_offset + spec.vector_index]
        if vector[spec.atom_index]:
            continue
        scans = _scans_until_threshold_event(spec, before_snap, kernel)
        if scans is not None:
            pending_scans.append(scans)
            pending_sources[(spec.kind, spec.acc_name)] = scans

    if not pending_scans:
        return None

    next_event_scans = min(pending_scans)
    skipped_scans = max(next_event_scans - 1, 0)
    for kind, acc_name in pending_sources:
        _advance_hidden_progress(kind, acc_name, skipped_scans, before_snap, kernel)

    _step_kernel(context, kernel)
    return edge_comp.state_key(kernel), skipped_scans


def _settle_pending(
    context: _ExploreContext,
    kernel: ReplayKernel,
    before_snap: _KernelSnapshot,
    edge_comp: _EdgeCompressor,
) -> tuple[tuple[Any, ...], int]:
    """Resolve all pending timers/counters so the system reaches a stable state.

    *before_snap* must be from before the most recent ``_step_kernel`` call
    so that the per-scan delta can be computed (acc_after − acc_before).
    """
    key = edge_comp.state_key(kernel)
    total_additional_scans = 0
    event_count = len(context.done_event_specs) + len(context.threshold_event_specs)
    for _ in range(event_count + 1):
        resolved = _resolve_nearest_hidden_event(context, kernel, before_snap, key, edge_comp)
        if resolved is None:
            break
        key, additional_scans = resolved
        total_additional_scans += additional_scans
        before_snap = _snapshot_kernel(kernel)
    return key, total_additional_scans


def _maybe_jump_hidden_event(
    context: _ExploreContext,
    kernel: ReplayKernel,
    snap: _KernelSnapshot,
    visited: set[tuple[Any, ...]],
    new_key: tuple[Any, ...],
    edge_comp: _EdgeCompressor,
) -> tuple[tuple[Any, ...], int]:
    """Jump from a revisited hidden pending plateau to the next completion event."""
    if not (context.done_event_specs or context.threshold_event_specs) or new_key not in visited:
        return new_key, 0

    resolved = _resolve_nearest_hidden_event(context, kernel, snap, new_key, edge_comp)
    if resolved is None:
        return new_key, 0
    resolved_key, additional_scans = resolved
    return resolved_key, additional_scans


def _bfs_explore(
    context: _ExploreContext,
    *,
    predicates: list[Callable[[dict[str, Any]], bool]] | None = None,
    project: tuple[str, ...] | None = None,
    max_depth: int = 50,
    max_states: int = 100_000,
    bfs_config: _BFSConfig = _DEFAULT_BFS_CONFIG,
) -> (
    list[Proven | Counterexample | Intractable]
    | frozenset[frozenset[tuple[str, Any]]]
    | Intractable
):
    """BFS over the reachable state space."""
    kernel = context.compiled.create_kernel()
    _seed_synthetic_presets(context, kernel)
    edge_comp = _EdgeCompressor(context)

    def _state_key(k: ReplayKernel) -> tuple[Any, ...]:
        if bfs_config.edge_compression:
            return edge_comp.state_key(k)
        return _extract_state_key(
            k, context.stateful_names, context.edge_tag_names,
            context.memory_key_names, context.state_key_done_specs,
            context.threshold_vector_specs,
        )

    initial_key = _state_key(kernel)

    visited: set[tuple[Any, ...]] = {initial_key}
    parent_map: dict[tuple[Any, ...], tuple[tuple[Any, ...] | None, dict[str, Any], int]] | None = (
        {initial_key: (None, {}, 0)} if predicates is not None else None
    )

    results: list[Counterexample | Proven | Intractable | None] | None = (
        [None] * len(predicates) if predicates is not None else None
    )
    projected_rows: set[tuple[Any, ...]] = set()
    if project is not None:
        projected_rows.add(_projected_tuple(kernel, project))

    def _record_failures(
        *,
        state: dict[str, Any],
        p_key: tuple[Any, ...],
        input_dict: dict[str, Any],
        edge_scans: int,
        initial: bool = False,
    ) -> None:
        assert predicates is not None and results is not None and parent_map is not None
        for i, predicate in enumerate(predicates):
            if results[i] is not None:
                continue
            if predicate(state):
                continue
            if initial:
                results[i] = Counterexample(trace=[TraceStep(inputs={}, scans=0)])
                continue
            trace = _build_trace(parent_map, p_key)
            trace.append(TraceStep(inputs=input_dict, scans=edge_scans))
            results[i] = Counterexample(trace=trace)

    if predicates is not None:
        _record_failures(
            state=kernel.tags,
            p_key=initial_key,
            input_dict={},
            edge_scans=0,
            initial=True,
        )
        assert results is not None
        if all(r is not None for r in results):
            return [r for r in results if r is not None]

    queue: deque[tuple[_KernelSnapshot, int, tuple[Any, ...]]] = deque()
    queue.append((_snapshot_kernel(kernel), 0, initial_key))

    while queue:
        snap, depth, parent_key = queue.popleft()
        if depth >= max_depth:
            continue

        _restore_kernel(kernel, snap)
        live = (
            _live_inputs(kernel.tags, context.nondeterministic_dims, context.all_exprs)
            if bfs_config.live_input_pruning
            else frozenset(context.nondeterministic_dims)
        )
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
            edge_scans = 1

            if predicates is not None:
                assert results is not None
                any_unsettled = any(
                    results[i] is None and not predicates[i](kernel.tags)
                    for i in range(len(predicates))
                )
                new_key = _state_key(kernel)
                if (
                    bfs_config.pending_settlement
                    and any_unsettled
                    and _has_pending_hidden_event(context, new_key)
                ):
                    new_key, additional_scans = _settle_pending(
                        context,
                        kernel,
                        snap,
                        edge_comp,
                    )
                    edge_scans += additional_scans
                    new_key = _state_key(kernel)
                _record_failures(
                    state=kernel.tags,
                    p_key=parent_key,
                    input_dict=input_dict,
                    edge_scans=edge_scans,
                )
            else:
                new_key = _state_key(kernel)

            before_jump_key = new_key
            if bfs_config.hidden_event_jumping:
                _, additional_scans = _maybe_jump_hidden_event(
                    context,
                    kernel,
                    snap,
                    visited,
                    new_key,
                    edge_comp,
                )
                new_key = _state_key(kernel)
            else:
                additional_scans = 0
            jumped = new_key != before_jump_key or additional_scans > 0
            if additional_scans:
                edge_scans += additional_scans
            if jumped and predicates is not None:
                _record_failures(
                    state=kernel.tags,
                    p_key=parent_key,
                    input_dict=input_dict,
                    edge_scans=edge_scans,
                )

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
                    intractable = Intractable(
                        reason="max_states exceeded",
                        dimensions=len(context.stateful_dims) + len(context.nondeterministic_dims),
                        estimated_space=len(visited),
                        hints=_build_dimension_hints(context),
                    )
                    if results is not None:
                        return [r if r is not None else intractable for r in results]
                    return intractable
                if parent_map is not None:
                    parent_map[new_key] = (parent_key, input_dict, edge_scans)
                queue.append((_snapshot_kernel(kernel), depth + 1, new_key))

            if results is not None and all(r is not None for r in results):
                return [r for r in results if r is not None]

    if project is not None:
        return _projected_states(project, projected_rows)

    if results is not None:
        return [r if r is not None else Proven(states_explored=len(visited)) for r in results]

    return [Proven(states_explored=len(visited))]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _compile_property(
    *conditions: Any,
) -> tuple[Callable[[dict[str, Any]], bool], list[str] | None, Expr | None]:
    """Normalize a condition expression or callable into a dict predicate.

    Returns ``(predicate_fn, auto_scope, expr_or_none)`` where *auto_scope* is
    a list of referenced tag names (for automatic upstream-cone restriction)
    or ``None`` when the caller passed an opaque callable.
    """
    if len(conditions) == 1 and callable(conditions[0]) and not _is_condition_like(conditions[0]):
        user_predicate = conditions[0]

        def _predicate(state: dict[str, Any]) -> bool:
            return bool(user_predicate(dict(state)))

        return _predicate, None, None

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

    return _predicate, tags_in_expr, expr


def _is_condition_like(obj: Any) -> bool:
    """True if *obj* is a Tag or Condition (not a plain callable)."""
    from pyrung.core.condition import Condition
    from pyrung.core.tag import Tag

    return isinstance(obj, (Tag, Condition))


def _upstream_cone(program: Program, tags: list[str]) -> frozenset[str]:
    """Compute the full upstream dependency cone for a set of tags."""
    dv = program.dataview()
    cone: set[str] = set()
    for tag_name in tags:
        cone.update(dv.upstream(tag_name).tags)
    cone.update(tags)
    return frozenset(cone)


def _partition_batch(
    program: Program,
    compiled_properties: list[
        tuple[Callable[[dict[str, Any]], bool], list[str] | None, Expr | None]
    ],
) -> list[tuple[list[int], list[str] | None]]:
    """Group batch properties into independent partitions by upstream cone overlap.

    Returns a list of ``(original_indices, merged_scope)`` pairs.
    Properties with ``auto_scope=None`` (lambdas) get full scope.
    """
    n = len(compiled_properties)
    if n <= 1:
        scope = compiled_properties[0][1] if n == 1 else None
        return [(list(range(n)), scope)]

    cones: list[frozenset[str] | None] = []
    for _predicate, auto_scope, _expr in compiled_properties:
        if auto_scope is None:
            cones.append(None)
        else:
            cones.append(_upstream_cone(program, auto_scope))

    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    null_indices = [i for i, c in enumerate(cones) if c is None]
    if null_indices:
        for i in null_indices[1:]:
            union(null_indices[0], i)

    for i in range(n):
        cone_i = cones[i]
        if cone_i is None:
            continue
        for j in range(i + 1, n):
            cone_j = cones[j]
            if cone_j is None:
                union(i, j)
            elif cone_i & cone_j:
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    result: list[tuple[list[int], list[str] | None]] = []
    for indices in groups.values():
        group_scopes = [compiled_properties[i][1] for i in indices]
        if any(s is None for s in group_scopes):
            result.append((indices, None))
        else:
            merged: set[str] = set()
            for s in group_scopes:
                assert s is not None
                merged.update(s)
            result.append((indices, sorted(merged)))
    return result


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
    from pyrung.circuitpy.codegen import compile_kernel

    is_batch, property_specs = _normalize_property_specs(*conditions)
    compiled_properties = [_compile_property_spec(spec) for spec in property_specs]

    if not is_batch:
        predicate, auto_scope, expr = compiled_properties[0]
        effective_scope = scope if scope is not None else auto_scope
        extra = [expr] if expr is not None else []
        context = _build_explore_context(program, scope=effective_scope, extra_exprs=extra)
        if isinstance(context, Intractable):
            return context
        return _bfs_explore(
            context,
            predicates=[predicate],
            max_depth=max_depth,
            max_states=max_states,
        )[0]

    if scope is not None:
        partitions = [(list(range(len(compiled_properties))), scope)]
    else:
        partitions = _partition_batch(program, compiled_properties)

    compiled_kernel = compile_kernel(program)
    results: list[Proven | Counterexample | Intractable | None] = [None] * len(compiled_properties)
    for indices, group_scope in partitions:
        group_exprs: list[Expr] = [
            e for i in indices if (e := compiled_properties[i][2]) is not None
        ]
        context = _build_explore_context(
            program,
            scope=group_scope,
            extra_exprs=group_exprs,
            compiled=compiled_kernel,
        )
        if isinstance(context, Intractable):
            for i in indices:
                results[i] = context
            continue

        group_predicates = [compiled_properties[i][0] for i in indices]
        group_results = _bfs_explore(
            context,
            predicates=group_predicates,
            max_depth=max_depth,
            max_states=max_states,
        )
        for i, r in zip(indices, group_results, strict=True):  # ty: ignore[invalid-argument-type]
            results[i] = r

    return [r if r is not None else Proven(states_explored=0) for r in results]


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
    project_names = tuple(project) if project is not None else tuple(_default_projection(program))
    context = _build_explore_context(program, scope=scope, project=project_names)
    if isinstance(context, Intractable):
        return context

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
