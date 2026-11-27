"""Conservative value and guard evidence at ordered scalar indirect accesses.

Direct copies propagate finite values. Unknown writes and calls invalidate them;
we never mistake merely writing a pointer for establishing a valid address.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, assert_never

from pyrung.core.analysis.affine import extract_forward_affine
from pyrung.core.analysis.pdg import _extract_reads_from_condition
from pyrung.core.analysis.write_sites import instruction_write_targets, static_write_target_names
from pyrung.core.condition import AllCondition, Condition
from pyrung.core.instruction.calc import CalcInstruction
from pyrung.core.instruction.control import ReturnInstruction
from pyrung.core.instruction.conversions import (
    _store_copy_value_to_tag_type,
    _truncate_to_tag_type,
    copy_source_kind,
)
from pyrung.core.instruction.data_transfer import CopyInstruction
from pyrung.core.instruction.operands import ConstArg, ExprArg, PointerArg, TagArg
from pyrung.core.rung import Rung

if TYPE_CHECKING:
    from pyrung.core.validation.context import ValidationContext
    from pyrung.core.validation.walker import OperandFact

Values = tuple[Any, ...] | None


def _union(left: Values, right: Values) -> Values:
    if left is None or right is None:
        return None
    values = set(left) | set(right)
    return tuple(values) if len(values) <= 4096 else None


def _preceding_conditions(conditions: list[Condition], path: str) -> list[Condition]:
    """Only AND terms evaluated before a condition operand may guard its read."""
    match = re.match(r"condition(?:\[(\d+)\])?", path)
    if match is None:
        return []
    index = int(match.group(1) or 0)
    result = list(conditions[:index])
    current = conditions[index]
    suffix = path[match.end() :]
    for child_index in re.findall(r"\.conditions\[(\d+)\]", suffix):
        if not isinstance(current, AllCondition):
            break
        index = int(child_index)
        result.extend(current.conditions[:index])
        current = current.conditions[index]
    return result


def site_evidence(
    context: ValidationContext, fact: OperandFact, pointer_name: str
) -> tuple[Values, tuple[tuple[Condition, ...], ...], bool]:
    """Return possible values, alternative guards, and whether the default remains possible."""
    program, graph = context.program, context.graph
    env: dict[str, Values] = dict(context.closed_domains)
    default_possible = True
    unreachable = False
    guards: list[Condition] = []
    loc = fact.location
    rungs = program.rungs if loc.subroutine is None else program.subroutines[loc.subroutine]

    def writes(instr: Any) -> set[str]:
        targets = instruction_write_targets(instr)
        if type(instr).__name__ in {
            "CallInstruction",
            "ForLoopInstruction",
            "FunctionCallInstruction",
            "EnabledFunctionCallInstruction",
        } or any(not static_write_target_names(target) for target in targets):
            return set(graph.tags)
        return {name for target in targets for name in static_write_target_names(target)}

    def apply(instr: Any, guaranteed: bool) -> None:
        nonlocal default_possible, unreachable
        if isinstance(instr, ReturnInstruction) and guaranteed:
            unreachable = True
        changed = writes(instr)
        guards[:] = [g for g in guards if not (_extract_reads_from_condition(g, {}) & changed)]
        new: Values = None
        target = None
        match instr:
            case CopyInstruction(dest=TagArg(tag=tag)) | CalcInstruction(dest=TagArg(tag=tag)):
                target = tag
        if isinstance(instr, CopyInstruction) and target is not None and instr.convert is None:
            match instr.source:
                case TagArg(tag=source):
                    values = env.get(source.name)
                case ConstArg(value=value):
                    values = (value,)
                case PointerArg() | ExprArg():
                    values = None
                case _:
                    assert_never(instr.source)
            if values is not None and all(isinstance(v, (int, float, bool)) for v in values):
                kind = copy_source_kind(instr.source)
                new = tuple(_store_copy_value_to_tag_type(v, target, kind) for v in values)
        elif isinstance(instr, CalcInstruction) and target is not None:
            affine = extract_forward_affine(instr)
            if affine is not None:
                source_name, scale, offset = affine
                values = env.get(source_name)
                if values is not None and all(isinstance(v, (int, float, bool)) for v in values):
                    new = tuple(
                        _truncate_to_tag_type(scale * v + offset, target, instr.mode)
                        for v in values
                    )
        guaranteed = guaranteed and not getattr(instr, "_oneshot", False)
        for name in changed:
            assigned = new if target is not None and target.name == name else None
            env[name] = assigned if guaranteed else _union(env.get(name), assigned)
        if pointer_name in changed and guaranteed:
            default_possible = False

    def visit(rung: Rung, guaranteed: bool = True) -> None:
        guaranteed = guaranteed and not rung._conditions
        for item in rung._execution_items:
            if isinstance(item, Rung):
                visit(item, guaranteed)
            else:
                apply(item, guaranteed)

    # Rungs solve one after another, so the access sees every rung above it.  A
    # ``continued()`` rung is no exception, because it reads live.
    for previous in rungs[: loc.rung_index]:
        visit(previous)

    # Entry conditions are usable only when their operands cannot change in the
    # program. Mutable caller guards need interprocedural instruction-local proof.
    entries: list[tuple[Condition, ...]] = [()]
    if loc.subroutine is not None:
        reach = context.scope_reach_chains.get(loc.subroutine, ())
        entries = [
            tuple(
                g
                for g in chain.conditions
                if not any(
                    graph.may_be_written(name) for name in _extract_reads_from_condition(g, {})
                )
            )
            for chain in reach
        ] or [()]

    # These are the rows from the rail down to the access.  A row reads its own contacts
    # when the solve reaches it, after the rows above it ran.  The trunk reads them as the
    # rung starts.  A branch's ``_conditions`` begin with its ancestors' conditions.
    path = [rungs[loc.rung_index]]
    for branch_index in loc.branch_path:
        path.append(path[-1]._branches[branch_index])
    depth = len(path) - 1
    if loc.instruction_index is None:
        match = re.match(r"condition(?:\[(\d+)\])?", loc.arg_path)
        if match is not None:
            condition_index = int(match.group(1) or 0)
            # An inherited contact is read on the ancestor row that owns it.
            while depth and condition_index < path[depth]._branch_condition_start:
                depth -= 1
    for level in range(depth):
        row, branch = path[level], path[level + 1]
        guards.extend(row._conditions[row._branch_condition_start :])
        for item in row._execution_items:
            if item is branch:
                break
            if isinstance(item, Rung):
                visit(item)
            else:
                apply(item, True)
    rung = path[depth]
    own_start = rung._branch_condition_start
    if loc.instruction_index is None:
        # A contact reads after the AND terms before it on its own row.
        guards.extend(_preceding_conditions(rung._conditions, loc.arg_path)[own_start:])
    else:
        guards.extend(rung._conditions[own_start:])
        # Install the row's guards first, then invalidate them as earlier
        # instructions change their operands on the way to this exact access.
        target_instr = rung._instructions[loc.instruction_index]
        for item in rung._execution_items:
            if item is target_instr:
                break
            if isinstance(item, Rung):
                visit(item)
            else:
                apply(item, True)
        if type(target_instr).__name__ == "ForLoopInstruction":
            env[pointer_name] = None
            guards.clear()
            default_possible = False
    chains = () if unreachable else tuple((*entry, *guards) for entry in entries)
    return env.get(pointer_name), chains, default_possible
