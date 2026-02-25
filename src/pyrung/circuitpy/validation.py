"""CircuitPython deployment validation for pyrung programs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pyrung.circuitpy.hardware import P1AM
from pyrung.core.condition import Condition
from pyrung.core.copy_modifiers import CopyModifier
from pyrung.core.expression import Expression
from pyrung.core.memory_block import (
    BlockRange,
    IndirectBlockRange,
    IndirectExprRef,
    IndirectRef,
    InputBlock,
    OutputBlock,
)
from pyrung.core.tag import InputTag, OutputTag, Tag
from pyrung.core.time_mode import TimeUnit
from pyrung.core.validation.walker import (
    _INSTRUCTION_FIELDS,
    ProgramLocation,
    _condition_children,
)

if TYPE_CHECKING:
    from pyrung.core.program import Program
    from pyrung.core.rung import Rung as LogicRung

ValidationMode = Literal["warn", "strict"]
FindingSeverity = Literal["error", "warning", "hint"]

CPY_FUNCTION_CALL_VERIFY = "CPY_FUNCTION_CALL_VERIFY"
CPY_IO_BLOCK_UNTRACKED = "CPY_IO_BLOCK_UNTRACKED"
CPY_TIMER_RESOLUTION = "CPY_TIMER_RESOLUTION"
_NON_BLOCKING_ADVISORY_CODES = frozenset(
    {
        CPY_FUNCTION_CALL_VERIFY,
        CPY_TIMER_RESOLUTION,
    }
)


@dataclass(frozen=True)
class CircuitPyFinding:
    code: str
    severity: FindingSeverity
    message: str
    location: str
    suggestion: str | None = None


@dataclass(frozen=True)
class CircuitPyValidationReport:
    errors: tuple[CircuitPyFinding, ...] = ()
    warnings: tuple[CircuitPyFinding, ...] = ()
    hints: tuple[CircuitPyFinding, ...] = ()

    def summary(self) -> str:
        parts: list[str] = []
        if self.errors:
            parts.append(f"{len(self.errors)} error(s)")
        if self.warnings:
            parts.append(f"{len(self.warnings)} warning(s)")
        if self.hints:
            parts.append(f"{len(self.hints)} hint(s)")
        if not parts:
            return "No findings."
        return ", ".join(parts) + "."


_BLOCK_REGISTRY: dict[int, InputBlock | OutputBlock] = {}


def _format_location(loc: ProgramLocation) -> str:
    if loc.scope == "subroutine":
        prefix = f"subroutine[{loc.subroutine}].rung[{loc.rung_index}]"
    else:
        prefix = f"main.rung[{loc.rung_index}]"

    for branch_idx in loc.branch_path:
        prefix += f".branch[{branch_idx}]"

    if loc.instruction_index is not None:
        prefix += f".instruction[{loc.instruction_index}]({loc.instruction_type})"

    return f"{prefix}.{loc.arg_path}"


def _route_severity(code: str, mode: ValidationMode) -> FindingSeverity:
    if mode == "strict" and code not in _NON_BLOCKING_ADVISORY_CODES:
        return "error"
    return "hint"


def _collect_hw_blocks(hw: P1AM) -> set[int]:
    _BLOCK_REGISTRY.clear()
    block_ids: set[int] = set()

    for _, (_, configured) in sorted(hw._slots.items()):
        blocks: tuple[InputBlock | OutputBlock, ...]
        if isinstance(configured, tuple):
            blocks = configured
        else:
            blocks = (configured,)

        for block in blocks:
            block_id = id(block)
            block_ids.add(block_id)
            _BLOCK_REGISTRY[block_id] = block

    return block_ids


def _extract_io_tags(instruction: Any) -> list[Tag]:
    found: list[Tag] = []
    seen_values: set[int] = set()
    seen_tags: set[int] = set()

    def walk(value: Any) -> None:
        value_id = id(value)
        if value_id in seen_values:
            return
        seen_values.add(value_id)

        if isinstance(value, (InputTag, OutputTag)):
            tag_id = id(value)
            if tag_id not in seen_tags:
                seen_tags.add(tag_id)
                found.append(value)
            return

        if isinstance(value, CopyModifier):
            walk(value.source)
            return

        if isinstance(value, BlockRange):
            for tag in value.tags():
                walk(tag)
            return

        if isinstance(value, IndirectBlockRange):
            walk(value.start_expr)
            walk(value.end_expr)
            return

        if isinstance(value, IndirectRef):
            walk(value.pointer)
            return

        if isinstance(value, IndirectExprRef):
            walk(value.expr)
            return

        if isinstance(value, Condition):
            for _, child in _condition_children(value):
                walk(child)
            return

        if isinstance(value, Expression):
            try:
                attrs = vars(value)
            except TypeError:
                return
            for key in sorted(attrs):
                if key.startswith("_"):
                    continue
                walk(attrs[key])
            return

        if isinstance(value, dict):
            for key in sorted(value):
                walk(value[key])
            return

        if isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                walk(item)

    instruction_type = type(instruction).__name__
    fields = _INSTRUCTION_FIELDS.get(instruction_type)

    if fields is None:
        walk(instruction)
        return found

    for field_name in fields:
        walk(getattr(instruction, field_name))

    if instruction_type in {"FunctionCallInstruction", "EnabledFunctionCallInstruction"}:
        ins = getattr(instruction, "_ins", {})
        if isinstance(ins, dict):
            for key in sorted(ins):
                walk(ins[key])

        outs = getattr(instruction, "_outs", {})
        if isinstance(outs, dict):
            for key in sorted(outs):
                walk(outs[key])

    if hasattr(instruction, "oneshot"):
        walk(instruction.oneshot)

    return found


def _evaluate_function_call(
    instruction: Any,
    location: ProgramLocation,
    mode: ValidationMode,
) -> list[CircuitPyFinding]:
    instruction_type = type(instruction).__name__
    if instruction_type not in {"FunctionCallInstruction", "EnabledFunctionCallInstruction"}:
        return []

    location_text = _format_location(location)
    code = CPY_FUNCTION_CALL_VERIFY
    return [
        CircuitPyFinding(
            code=code,
            severity=_route_severity(code, mode),
            message=(
                f"{instruction_type} will be embedded via inspect.getsource() at {location_text}. "
                "Verify the callable is CircuitPython-compatible."
            ),
            location=location_text,
            suggestion=(
                "Run the function on-device (or in a CircuitPython-compatible environment) and "
                "avoid CPython-only modules/APIs."
            ),
        )
    ]


def _tag_is_tracked_by_hw(tag: Tag, hw_blocks: set[int]) -> bool:
    for block_id in hw_blocks:
        block = _BLOCK_REGISTRY.get(block_id)
        if block is None:
            continue
        for cached_tag in block._tag_cache.values():
            if cached_tag is tag:
                return True
    return False


def _evaluate_io_provenance(
    instruction: Any,
    location: ProgramLocation,
    hw_blocks: set[int],
    mode: ValidationMode,
) -> list[CircuitPyFinding]:
    findings: list[CircuitPyFinding] = []
    location_text = _format_location(location)
    code = CPY_IO_BLOCK_UNTRACKED

    for tag in _extract_io_tags(instruction):
        if _tag_is_tracked_by_hw(tag, hw_blocks):
            continue
        findings.append(
            CircuitPyFinding(
                code=code,
                severity=_route_severity(code, mode),
                message=(
                    f"I/O tag '{tag.name}' is not traceable to a configured P1AM slot "
                    f"at {location_text}."
                ),
                location=location_text,
                suggestion=(
                    "Use tags created from the same hw=P1AM() instance passed to validation "
                    "(for example: block = hw.slot(...); tag = block[...])."
                ),
            )
        )

    return findings


def _evaluate_timer_resolution(
    instruction: Any,
    location: ProgramLocation,
    mode: ValidationMode,
) -> list[CircuitPyFinding]:
    instruction_type = type(instruction).__name__
    if instruction_type not in {"OnDelayInstruction", "OffDelayInstruction"}:
        return []

    if getattr(instruction, "unit", None) != TimeUnit.Tms:
        return []

    location_text = _format_location(location)
    code = CPY_TIMER_RESOLUTION
    return [
        CircuitPyFinding(
            code=code,
            severity=_route_severity(code, mode),
            message=(
                f"{instruction_type} uses Tms timing at {location_text}. "
                "Effective timing resolution depends on scan time in CircuitPython."
            ),
            location=location_text,
            suggestion=(
                "Millisecond timers are supported, but accuracy depends on maintaining "
                "sub-millisecond scan times on your target."
            ),
        )
    ]


def _iter_instruction_sites(program: Program) -> list[tuple[Any, ProgramLocation]]:
    sites: list[tuple[Any, ProgramLocation]] = []

    def walk_rung(
        rung: LogicRung,
        *,
        scope: Literal["main", "subroutine"],
        subroutine: str | None,
        rung_index: int,
        branch_path: tuple[int, ...],
    ) -> None:
        conditions = rung._conditions
        for cond_idx, cond in enumerate(conditions):
            arg_path = "condition" if len(conditions) == 1 else f"condition[{cond_idx}]"
            sites.append(
                (
                    cond,
                    ProgramLocation(
                        scope=scope,
                        subroutine=subroutine,
                        rung_index=rung_index,
                        branch_path=branch_path,
                        instruction_index=None,
                        instruction_type=None,
                        arg_path=arg_path,
                    ),
                )
            )

        def walk_instruction(instruction: Any, instruction_index: int) -> None:
            sites.append(
                (
                    instruction,
                    ProgramLocation(
                        scope=scope,
                        subroutine=subroutine,
                        rung_index=rung_index,
                        branch_path=branch_path,
                        instruction_index=instruction_index,
                        instruction_type=type(instruction).__name__,
                        arg_path="instruction",
                    ),
                )
            )

            if hasattr(instruction, "instructions"):
                for child_instruction in instruction.instructions:
                    walk_instruction(child_instruction, instruction_index)

        for instruction_index, instruction in enumerate(rung._instructions):
            walk_instruction(instruction, instruction_index)

        for branch_index, branch_rung in enumerate(rung._branches):
            walk_rung(
                branch_rung,
                scope=scope,
                subroutine=subroutine,
                rung_index=rung_index,
                branch_path=branch_path + (branch_index,),
            )

    for rung_index, rung in enumerate(program.rungs):
        walk_rung(rung, scope="main", subroutine=None, rung_index=rung_index, branch_path=())

    for subroutine_name in sorted(program.subroutines):
        for rung_index, rung in enumerate(program.subroutines[subroutine_name]):
            walk_rung(
                rung,
                scope="subroutine",
                subroutine=subroutine_name,
                rung_index=rung_index,
                branch_path=(),
            )

    return sites


def validate_circuitpy_program(
    program: Program,
    hw: P1AM | None = None,
    mode: ValidationMode = "warn",
) -> CircuitPyValidationReport:
    if mode not in {"warn", "strict"}:
        raise ValueError("mode must be 'warn' or 'strict'")
    if hw is not None and not isinstance(hw, P1AM):
        raise TypeError("hw must be a P1AM instance when provided")

    findings: list[CircuitPyFinding] = []

    instruction_sites = _iter_instruction_sites(program)
    for instruction, location in instruction_sites:
        findings.extend(_evaluate_function_call(instruction, location, mode))

    if hw is not None:
        hw_blocks = _collect_hw_blocks(hw)
        for instruction, location in instruction_sites:
            findings.extend(_evaluate_io_provenance(instruction, location, hw_blocks, mode))
            findings.extend(_evaluate_timer_resolution(instruction, location, mode))

    errors: list[CircuitPyFinding] = []
    warnings: list[CircuitPyFinding] = []
    hints: list[CircuitPyFinding] = []

    for finding in findings:
        if finding.severity == "error":
            errors.append(finding)
        elif finding.severity == "warning":
            warnings.append(finding)
        else:
            hints.append(finding)

    return CircuitPyValidationReport(
        errors=tuple(errors),
        warnings=tuple(warnings),
        hints=tuple(hints),
    )


__all__ = [
    "CPY_FUNCTION_CALL_VERIFY",
    "CPY_IO_BLOCK_UNTRACKED",
    "CPY_TIMER_RESOLUTION",
    "CircuitPyFinding",
    "CircuitPyValidationReport",
    "FindingSeverity",
    "ValidationMode",
    "validate_circuitpy_program",
]
