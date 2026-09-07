"""Conservative value and guard evidence at ordered scalar indirect accesses.

Direct copies propagate finite values. Unknown writes and calls invalidate them;
we never mistake merely writing a pointer for establishing a valid address.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.affine import extract_forward_affine
from pyrung.core.analysis.pdg import _extract_reads_from_condition
from pyrung.core.analysis.write_sites import instruction_write_targets, static_write_target_names
from pyrung.core.condition import AllCondition, Condition
from pyrung.core.instruction.calc import CalcInstruction
from pyrung.core.instruction.control import ReturnInstruction
from pyrung.core.instruction.conversions import _store_copy_value_to_tag_type, _truncate_to_tag_type
from pyrung.core.instruction.data_transfer import CopyInstruction
from pyrung.core.rung import Rung
from pyrung.core.tag import ImmediateRef, Tag

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
        target = instr.dest if isinstance(instr, (CopyInstruction, CalcInstruction)) else None
        if isinstance(target, ImmediateRef):
            target = target.value
        if isinstance(instr, CopyInstruction) and isinstance(target, Tag) and instr.convert is None:
            source = instr.source
            if isinstance(source, ImmediateRef):
                source = source.value
            values = env.get(source.name) if isinstance(source, Tag) else (source,)
            if values is not None and all(isinstance(v, (int, float, bool)) for v in values):
                new = tuple(_store_copy_value_to_tag_type(v, target) for v in values)
        elif isinstance(instr, CalcInstruction) and isinstance(target, Tag):
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
            assigned = new if isinstance(target, Tag) and target.name == name else None
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

    snapshot_index = loc.rung_index
    while snapshot_index and rungs[snapshot_index]._use_prior_snapshot:
        snapshot_index -= 1
    for rung in rungs[:snapshot_index]:
        visit(rung)

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
                    graph.writers_of.get(name) for name in _extract_reads_from_condition(g, {})
                )
            )
            for chain in reach
        ] or [()]

    rung = rungs[loc.rung_index]
    ancestors: list[tuple[Rung, Rung]] = []
    for branch_index in loc.branch_path:
        guards.extend(rung._conditions)
        branch = rung._branches[branch_index]
        ancestors.append((rung, branch))
        rung = branch
    if loc.instruction_index is None:
        # Every branch condition is evaluated before any instruction in the
        # top-level rung. Instruction prefixes must not affect these reads.
        guards.extend(_preceding_conditions(rung._conditions, loc.arg_path))
    else:
        guards.extend(rung._conditions)
        # Install all pre-evaluated guards first, then invalidate them as earlier
        # instructions change their operands on the way to this exact access.
        for previous in rungs[snapshot_index : loc.rung_index]:
            visit(previous)
        for parent, branch in ancestors:
            for item in parent._execution_items:
                if item is branch:
                    break
                if isinstance(item, Rung):
                    visit(item)
                else:
                    apply(item, True)
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
