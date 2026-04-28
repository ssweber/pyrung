"""Accumulator absorption and threshold abstraction helpers.

Threshold absorption principle
------------------------------
The concrete value of a threshold is irrelevant to reachability.  A timer
fires or it doesn't — prove explores both outcomes.  Whether the threshold
is 100 or 4000 changes WHEN the crossing occurs, not WHETHER it's
reachable.  The gate for absorption is **exclusivity** (the threshold tag
is only used in threshold comparisons and as a timer/counter preset, never
in copy/calc data-flow), not **stability** (the threshold doesn't change
between scans).  If a tag passes the exclusivity check it can be absorbed
regardless of how, when, or by whom it is written.

Implementation status: this module has two absorption paths with different
readiness for the exclusivity framing.

*Redundant Acc absorption* (``_find_redundant_acc_absorptions``): the
threshold is already discarded (synthetic preset=1).  The stability check
(``_is_stable_dynamic_preset``) is purely unnecessary — the exclusivity
check (``_has_non_timer_data_read``) is the only gate that matters.

*Threshold vector absorption* (``_find_threshold_absorptions``): accepts
both exact and abstract threshold atoms.  Exact atoms keep the current
scan-distance scheduling.  Abstract atoms represent exclusive threshold-only
tags whose concrete value is hidden from the state key; the BFS materializes
representative crossed successors when needed instead of requiring a stable
value up front.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.simplified import Atom, Expr
from pyrung.core.tag import TagType

from . import PENDING
from .expr import _collect_atoms_for_tag

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.program import Program

_DONE_KIND_ON_DELAY = "on_delay"

_DONE_KIND_OFF_DELAY = "off_delay"

_DONE_KIND_COUNT_UP = "count_up"

_DONE_KIND_COUNT_DOWN = "count_down"

_PROGRESS_KIND_INT_UP = "int_up"

_THRESHOLD_FORM_GT = "gt"

_THRESHOLD_FORM_GE = "ge"

_THRESHOLD_MODE_EXACT = "exact"

_THRESHOLD_MODE_ABSTRACT = "abstract"


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
    from pyrung.core.instruction.counters import CountDownInstruction, CountUpInstruction
    from pyrung.core.instruction.data_transfer import (
        BlockCopyInstruction,
        CopyInstruction,
        FillInstruction,
    )
    from pyrung.core.instruction.drums import EventDrumInstruction, TimeDrumInstruction
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


def _is_stable_dynamic_preset(preset_tag_name: str, graph: ProgramGraph) -> bool:
    """True when a dynamic preset is frozen or owned by the ladder.

    Redundant — the synthetic preset=1 path discards the concrete value,
    so stability doesn't matter.  The real gate is exclusivity
    (_has_non_timer_data_read).  Safe to relax or remove.
    """
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
    """True when a threshold value is fixed for precise event scheduling."""
    if _is_numeric_literal(value):
        return True
    if not isinstance(value, str):
        return False
    tag = graph.tags.get(value)
    if tag is None:
        return False
    if value not in graph.writers_of and not tag.external:
        return True
    if tag.external or tag.public:
        return False
    if tag.readonly:
        return True
    return bool(tag.final and value in graph.writers_of)


def _threshold_mode(
    threshold: Any,
    graph: ProgramGraph,
) -> str | None:
    """Classify a threshold operand as exact, abstract, or unsupported."""
    if _is_numeric_literal(threshold):
        return _THRESHOLD_MODE_EXACT
    if not isinstance(threshold, str):
        return None
    if graph.tags.get(threshold) is None:
        return None
    if _is_stable_threshold(threshold, graph):
        return _THRESHOLD_MODE_EXACT
    return _THRESHOLD_MODE_ABSTRACT


def _threshold_atom_for_progress(
    atom: Atom,
    acc_name: str,
    graph: ProgramGraph,
) -> _ThresholdAtomSpec | None:
    """Normalize supported Progress/Threshold comparison atoms."""
    if atom.tag == acc_name and atom.form in {_THRESHOLD_FORM_GT, _THRESHOLD_FORM_GE}:
        mode = _threshold_mode(atom.operand, graph)
        if mode is not None:
            return _ThresholdAtomSpec(acc_name, atom.operand, atom.form, mode)
        return None

    if atom.operand == acc_name and atom.form in {"lt", "le"}:
        mode = _threshold_mode(atom.tag, graph)
        if mode is None:
            return None
        form = _THRESHOLD_FORM_GT if atom.form == "lt" else _THRESHOLD_FORM_GE
        return _ThresholdAtomSpec(acc_name, atom.tag, form, mode)

    return None


def _diagnose_unstable_atom(
    atom: Atom,
    acc_name: str,
    graph: ProgramGraph,
) -> str | None:
    """Return a human-readable reason when an atom blocks threshold abstraction.

    This diagnoses unsupported comparison structure or unknown threshold
    operands.  Stability is no longer an admission gate.
    """
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
    return None


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


@dataclass(frozen=True)
class _ThresholdAtomSpec:
    acc_name: str
    threshold: int | float | str
    form: str
    mode: str


@dataclass(frozen=True)
class _ThresholdVectorSpec:
    acc_name: str
    kind: str
    atoms: tuple[_ThresholdAtomSpec, ...]


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
