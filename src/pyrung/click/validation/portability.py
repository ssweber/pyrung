"""Structural portability checks for Click validation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyrung.core.instruction.calc import infer_calc_mode
from pyrung.core.memory_block import BlockRange
from pyrung.core.tag import ImmediateRef, Tag
from pyrung.core.validation.walker import ProgramLocation

from .findings import (
    CLK_CALC_MODE_MIXED,
    CLK_EXPR_ONLY_IN_CALC,
    CLK_FUNCTION_CALL_NOT_PORTABLE,
    CLK_IMMEDIATE_COIL_TARGET_MUST_BE_Y,
    CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED,
    CLK_IMMEDIATE_EDGE_CONTACT_NOT_ALLOWED,
    CLK_IMMEDIATE_RANGE_MUST_BE_CONTIGUOUS,
    CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED,
    CLK_INT_TRUTHINESS_EXPLICIT_COMPARE_REQUIRED,
    CLK_PTR_CONTEXT_ONLY_COPY,
    CLK_PTR_DS_UNVERIFIED,
    CLK_PTR_EXPR_NOT_ALLOWED,
    CLK_PTR_POINTER_MUST_BE_DS,
    CLK_TILDE_BOOL_CONTACT_ONLY,
    ClickFinding,
    ValidationMode,
    _build_suggestion,
    _route_severity,
)
from .resolve import (
    _bank_label,
    _format_location,
    _instruction_location,
    _resolve_direct_tag,
    _resolve_pointer_memory_type,
    _ResolvedSlot,
    _unique_slots,
    _unresolved_finding,
)

if TYPE_CHECKING:
    from pyrung.click.tag_map import TagMap
    from pyrung.core.validation.walker import OperandFact


def _evaluate_fact(
    fact: OperandFact,
    tag_map: TagMap,
    mode: ValidationMode,
) -> list[ClickFinding]:
    """Apply Stage 2 rules to a single OperandFact."""
    findings: list[ClickFinding] = []
    loc = fact.location
    location_str = _format_location(loc)

    if fact.value_kind == "indirect_ref":
        allowed = loc.instruction_type == "CopyInstruction" and loc.arg_path in {
            "instruction.source",
            "instruction.source.source",
            "instruction.target",
        }
        if not allowed:
            findings.append(
                ClickFinding(
                    code=CLK_PTR_CONTEXT_ONLY_COPY,
                    severity=_route_severity(CLK_PTR_CONTEXT_ONLY_COPY, mode),
                    message=f"Pointer (IndirectRef) used outside copy instruction at {location_str}.",
                    location=location_str,
                    suggestion=_build_suggestion(CLK_PTR_CONTEXT_ONLY_COPY, fact, tag_map),
                )
            )
        else:
            pointer_name = str(fact.metadata.get("pointer_name", ""))
            memory_type = _resolve_pointer_memory_type(pointer_name, tag_map)
            if memory_type is None:
                code = CLK_PTR_DS_UNVERIFIED
                findings.append(
                    ClickFinding(
                        code=code,
                        severity=_route_severity(code, mode),
                        message=(
                            f"Pointer '{pointer_name}' memory type could not be verified "
                            f"as DS at {location_str}."
                        ),
                        location=location_str,
                        suggestion=_build_suggestion(code, fact, tag_map),
                    )
                )
            elif memory_type != "DS":
                code = CLK_PTR_POINTER_MUST_BE_DS
                findings.append(
                    ClickFinding(
                        code=code,
                        severity=_route_severity(code, mode),
                        message=(
                            f"Pointer '{pointer_name}' is mapped to {memory_type}, "
                            f"not DS at {location_str}."
                        ),
                        location=location_str,
                        suggestion=_build_suggestion(code, fact, tag_map),
                    )
                )

    if fact.value_kind == "indirect_expr_ref":
        findings.append(
            ClickFinding(
                code=CLK_PTR_EXPR_NOT_ALLOWED,
                severity=_route_severity(CLK_PTR_EXPR_NOT_ALLOWED, mode),
                message=f"Computed pointer expression (IndirectExprRef) not allowed at {location_str}.",
                location=location_str,
                suggestion=_build_suggestion(CLK_PTR_EXPR_NOT_ALLOWED, fact, tag_map),
            )
        )

    if fact.value_kind == "expression":
        allowed = (
            loc.instruction_type == "CalcInstruction" and loc.arg_path == "instruction.expression"
        )
        if not allowed:
            findings.append(
                ClickFinding(
                    code=CLK_EXPR_ONLY_IN_CALC,
                    severity=_route_severity(CLK_EXPR_ONLY_IN_CALC, mode),
                    message=f"Expression used outside calc instruction at {location_str}.",
                    location=location_str,
                    suggestion=_build_suggestion(CLK_EXPR_ONLY_IN_CALC, fact, tag_map),
                )
            )
        expr_dsl = str(fact.metadata.get("expr_dsl", ""))
        if "~" in expr_dsl:
            findings.append(
                ClickFinding(
                    code=CLK_TILDE_BOOL_CONTACT_ONLY,
                    severity=_route_severity(CLK_TILDE_BOOL_CONTACT_ONLY, mode),
                    message=(
                        f"Expression uses `~` (bitwise invert) at {location_str}. "
                        "Click portability reserves `~` for BOOL contact inversion."
                    ),
                    location=location_str,
                    suggestion=_build_suggestion(CLK_TILDE_BOOL_CONTACT_ONLY, fact, tag_map),
                )
            )

    if (
        fact.value_kind == "condition"
        and fact.metadata.get("condition_type") == "IntTruthyCondition"
    ):
        findings.append(
            ClickFinding(
                code=CLK_INT_TRUTHINESS_EXPLICIT_COMPARE_REQUIRED,
                severity=_route_severity(CLK_INT_TRUTHINESS_EXPLICIT_COMPARE_REQUIRED, mode),
                message=f"Implicit INT truthiness used in condition at {location_str}.",
                location=location_str,
                suggestion=_build_suggestion(
                    CLK_INT_TRUTHINESS_EXPLICIT_COMPARE_REQUIRED, fact, tag_map
                ),
            )
        )

    if fact.value_kind == "indirect_block_range":
        findings.append(
            ClickFinding(
                code=CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED,
                severity=_route_severity(CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED, mode),
                message=(
                    f"IndirectBlockRange not allowed at {location_str}. "
                    "Click hardware does not support computed block ranges."
                ),
                location=location_str,
                suggestion=_build_suggestion(CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED, fact, tag_map),
            )
        )

    return findings


def _location_key(
    location: ProgramLocation,
) -> tuple[str, str | None, int, tuple[int, ...], int | None, str | None]:
    return (
        location.scope,
        location.subroutine,
        location.rung_index,
        location.branch_path,
        location.instruction_index,
        location.instruction_type,
    )


def _evaluate_immediate_coil_target(
    immediate_ref: ImmediateRef,
    location: ProgramLocation,
    tag_map: TagMap,
    mode: ValidationMode,
) -> list[ClickFinding]:
    wrapped = immediate_ref.value
    findings: list[ClickFinding] = []
    location_text = _format_location(location)
    slots: list[_ResolvedSlot] = []

    if isinstance(wrapped, Tag):
        resolved = _resolve_direct_tag(wrapped, tag_map)
        if resolved is None:
            return [
                _unresolved_finding(
                    location,
                    mode,
                    "immediate coil target mapping missing or ambiguous",
                )
            ]
        slots = [resolved]
    elif isinstance(wrapped, BlockRange):
        for tag in wrapped.tags():
            resolved = _resolve_direct_tag(tag, tag_map)
            if resolved is None:
                return [
                    _unresolved_finding(
                        location,
                        mode,
                        "immediate coil range mapping missing or ambiguous",
                    )
                ]
            slots.append(resolved)
    else:
        findings.append(
            ClickFinding(
                code=CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED,
                severity=_route_severity(CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED, mode),
                message=(
                    f"Immediate wrapper must wrap Tag or BlockRange at {location_text}, "
                    f"got {type(wrapped).__name__}."
                ),
                location=location_text,
                suggestion=_build_suggestion(CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED, None, tag_map),
            )
        )
        return findings

    non_y_slots = [slot for slot in slots if slot.memory_type != "Y"]
    if non_y_slots:
        findings.append(
            ClickFinding(
                code=CLK_IMMEDIATE_COIL_TARGET_MUST_BE_Y,
                severity=_route_severity(CLK_IMMEDIATE_COIL_TARGET_MUST_BE_Y, mode),
                message=(
                    f"Immediate coil target must resolve to Y bank at {location_text}, "
                    f"found {', '.join(_bank_label(slot) for slot in _unique_slots(non_y_slots))}."
                ),
                location=location_text,
                suggestion=_build_suggestion(CLK_IMMEDIATE_COIL_TARGET_MUST_BE_Y, None, tag_map),
            )
        )

    if isinstance(wrapped, BlockRange) and len(slots) > 1:
        addresses = [slot.address for slot in slots]
        if any(address is None for address in addresses):
            findings.append(
                _unresolved_finding(location, mode, "immediate range address unresolved")
            )
        else:
            numeric_addresses = [int(address) for address in addresses if address is not None]
            contiguous = all(
                numeric_addresses[idx] + 1 == numeric_addresses[idx + 1]
                for idx in range(len(numeric_addresses) - 1)
            )
            if not contiguous:
                findings.append(
                    ClickFinding(
                        code=CLK_IMMEDIATE_RANGE_MUST_BE_CONTIGUOUS,
                        severity=_route_severity(CLK_IMMEDIATE_RANGE_MUST_BE_CONTIGUOUS, mode),
                        message=(
                            "Immediate coil range must map to contiguous addresses "
                            f"at {location_text}."
                        ),
                        location=location_text,
                        suggestion=_build_suggestion(
                            CLK_IMMEDIATE_RANGE_MUST_BE_CONTIGUOUS, None, tag_map
                        ),
                    )
                )

    return findings


def _evaluate_immediate_usage(
    facts: tuple[OperandFact, ...],
    instruction_sites: list[tuple[Any, ProgramLocation]],
    tag_map: TagMap,
    mode: ValidationMode,
) -> list[ClickFinding]:
    findings: list[ClickFinding] = []
    condition_types: dict[
        tuple[tuple[str, str | None, int, tuple[int, ...], int | None, str | None], str], str
    ] = {}
    instructions_by_site: dict[
        tuple[str, str | None, int, tuple[int, ...], int | None, str | None], Any
    ] = {}

    for fact in facts:
        if fact.value_kind != "condition":
            continue
        condition_type = fact.metadata.get("condition_type")
        if not isinstance(condition_type, str):
            continue
        condition_types[(_location_key(fact.location), fact.location.arg_path)] = condition_type

    for instruction, location in instruction_sites:
        instructions_by_site[_location_key(location)] = instruction

    for fact in facts:
        if fact.value_kind != "immediate_ref":
            continue

        loc = fact.location
        location_text = _format_location(loc)
        site_key = _location_key(loc)

        if loc.instruction_index is None:
            parent_path = loc.arg_path.rsplit(".", 1)[0] if "." in loc.arg_path else ""
            condition_type = condition_types.get((site_key, parent_path))

            if condition_type in {"BitCondition", "NormallyClosedCondition"}:
                continue
            if condition_type in {"RisingEdgeCondition", "FallingEdgeCondition"}:
                findings.append(
                    ClickFinding(
                        code=CLK_IMMEDIATE_EDGE_CONTACT_NOT_ALLOWED,
                        severity=_route_severity(CLK_IMMEDIATE_EDGE_CONTACT_NOT_ALLOWED, mode),
                        message=f"Immediate edge contact is not allowed at {location_text}.",
                        location=location_text,
                        suggestion=_build_suggestion(
                            CLK_IMMEDIATE_EDGE_CONTACT_NOT_ALLOWED, fact, tag_map
                        ),
                    )
                )
                continue

            findings.append(
                ClickFinding(
                    code=CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED,
                    severity=_route_severity(CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED, mode),
                    message=f"Immediate wrapper is not allowed at {location_text}.",
                    location=location_text,
                    suggestion=_build_suggestion(CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED, fact, tag_map),
                )
            )
            continue

        if (
            loc.instruction_type in {"OutInstruction", "LatchInstruction", "ResetInstruction"}
            and loc.arg_path == "instruction.target"
        ):
            instruction = instructions_by_site.get(site_key)
            target = getattr(instruction, "target", None)
            if isinstance(target, ImmediateRef):
                findings.extend(_evaluate_immediate_coil_target(target, loc, tag_map, mode))
            continue

        findings.append(
            ClickFinding(
                code=CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED,
                severity=_route_severity(CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED, mode),
                message=f"Immediate wrapper is not allowed at {location_text}.",
                location=location_text,
                suggestion=_build_suggestion(CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED, fact, tag_map),
            )
        )

    return findings


def _evaluate_instruction_portability(
    instruction: Any, base_location: ProgramLocation, mode: ValidationMode
) -> list[ClickFinding]:
    findings: list[ClickFinding] = []
    instruction_type = type(instruction).__name__
    if instruction_type == "CalcInstruction":
        mode_info = infer_calc_mode(instruction.expression, instruction.dest)
        if mode_info.mixed_families:
            location = _instruction_location(base_location, "instruction.expression")
            location_text = _format_location(location)
            findings.append(
                ClickFinding(
                    code=CLK_CALC_MODE_MIXED,
                    severity=_route_severity(CLK_CALC_MODE_MIXED, mode),
                    message=(
                        "calc() mixes WORD (hex-family) and non-WORD (decimal-family) operands "
                        f"at {location_text}."
                    ),
                    location=location_text,
                    suggestion=(
                        "Split mixed calc math into separate decimal and WORD-only calc() steps, "
                        "or convert through an intermediate tag so each calc() stays one family."
                    ),
                )
            )

    if instruction_type in {"FunctionCallInstruction", "EnabledFunctionCallInstruction"}:
        location_text = _format_location(base_location)
        findings.append(
            ClickFinding(
                code=CLK_FUNCTION_CALL_NOT_PORTABLE,
                severity=_route_severity(CLK_FUNCTION_CALL_NOT_PORTABLE, mode),
                message=(
                    f"{instruction_type} is not Click-portable at {location_text}. "
                    "Click execution cannot run arbitrary Python callables."
                ),
                location=location_text,
                suggestion=(
                    "Replace run_function/run_enabled_function with Click-portable instructions "
                    "(copy/calc/timer/counter/send/receive)."
                ),
            )
        )
    return findings
