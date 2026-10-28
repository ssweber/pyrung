"""Click portability validation for pyrung programs.

Consumes Stage 1 walker facts for R1-R5, and instruction context plus hardware
profile data for Stage 3 (R6-R8).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pyrung.core.validation.walker import ProgramLocation, walk_program

from .findings import (
    CLK_ADDRESS_IDENTITY_CONFLICT,
    CLK_BANK_NOT_WRITABLE,
    CLK_BANK_UNRESOLVED,
    CLK_BANK_WRONG_ROLE,
    CLK_CALC_BIT_RANGE_AGGREGATE,
    CLK_CALC_FLOOR_DIV,
    CLK_CALC_FUNC_MODE_MISMATCH,
    CLK_CALC_MEDIAN_RANGE_TOO_LARGE,
    CLK_CALC_MODE_MIXED,
    CLK_CALC_NESTING_DEPTH,
    CLK_CALC_RANGE_MIXED_BANKS,
    CLK_CALC_RANGE_NOT_CONTIGUOUS,
    CLK_COPY_BANK_INCOMPATIBLE,
    CLK_COPY_CONVERTER_INCOMPATIBLE,
    CLK_DRUM_JUMP_STEP_TAG_REQUIRED,
    CLK_EXPR_ONLY_IN_CALC,
    CLK_FORLOOP_INNER_ONESHOT,
    CLK_FORLOOP_NESTING_DEPTH,
    CLK_FORLOOP_RETURN_INSIDE,
    CLK_FUNCTION_CALL_NOT_PORTABLE,
    CLK_IMMEDIATE_COIL_TARGET_MUST_BE_Y,
    CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED,
    CLK_IMMEDIATE_EDGE_CONTACT_NOT_ALLOWED,
    CLK_IMMEDIATE_RANGE_MUST_BE_CONTIGUOUS,
    CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED,
    CLK_INT_TRUTHINESS_EXPLICIT_COMPARE_REQUIRED,
    CLK_NICKNAME_TOO_LONG,
    CLK_OUT_UNIT_UNSUPPORTED,
    CLK_PACK_TEXT_BANK_INCOMPATIBLE,
    CLK_PROFILE_UNAVAILABLE,
    CLK_PTR_CONTEXT_ONLY_COPY,
    CLK_PTR_DS_UNVERIFIED,
    CLK_PTR_EXPR_NOT_ALLOWED,
    CLK_PTR_POINTER_MUST_BE_DS,
    CLK_STATUS_BIT_NOT_PORTABLE,
    CLK_TILDE_BOOL_CONTACT_ONLY,
    CLK_TIMER_PAIR_MISMATCH,
    CLK_TIMER_PRESET_OVERFLOW,
    ClickFinding,
    ClickValidationReport,
    FindingSeverity,
    ValidationMode,
    _route_severity,
)
from .hardware import (
    _evaluate_coil_targets,
    _evaluate_copy_compatibility,
    _evaluate_drums,
    _evaluate_out_variant_unit,
    _evaluate_pack_text,
    _evaluate_role_assignments,
    _evaluate_timer_pairs,
    _evaluate_timer_preset_overflow,
    _evaluate_write_targets,
)
from .hardware import (
    _load_default_profile as _hardware_load_default_profile,
)
from .identity import _evaluate_address_identity_conflicts
from .nicknames import _evaluate_nickname_lengths
from .portability import (
    _evaluate_fact,
    _evaluate_forloop_structure,
    _evaluate_immediate_usage,
    _evaluate_instruction_portability,
    _evaluate_range_aggregates,
    _evaluate_status_bit_usage,
)
from .resolve import _ResolutionCache

if TYPE_CHECKING:
    from pyrung.click.profile import HardwareProfile
    from pyrung.click.tag_map import TagMap
    from pyrung.core.program import Program

# Compatibility hook for tests that monkeypatch pyrung.click.validation._load_default_profile.
_load_default_profile = _hardware_load_default_profile


def _iter_instruction_sites(program: Program) -> list[tuple[Any, ProgramLocation]]:
    sites: list[tuple[Any, ProgramLocation]] = []

    def walk_rung(
        rung: Any,
        *,
        scope: Literal["main", "subroutine"],
        subroutine: str | None,
        rung_index: int,
        branch_path: tuple[int, ...],
    ) -> None:
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

            # ForLoopInstruction captures nested instructions in-order.
            if hasattr(instruction, "instructions"):
                for child_instruction in instruction.instructions:
                    walk_instruction(child_instruction, instruction_index)

        for instruction_index, instruction in enumerate(rung._instructions):
            walk_instruction(instruction, instruction_index)

        for branch_index, branch in enumerate(rung._branches):
            walk_rung(
                branch,
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


def validate_click_program(
    program: Program,
    tag_map: TagMap,
    mode: ValidationMode = "warn",
    profile: HardwareProfile | None = None,
) -> ClickValidationReport:
    """Validate a Program against Click portability rules."""
    facts = walk_program(program)
    instruction_sites = _iter_instruction_sites(program)
    resolver = _ResolutionCache(tag_map)

    findings: list[ClickFinding] = []
    findings.extend(_evaluate_address_identity_conflicts(program, facts, tag_map, mode))
    findings.extend(_evaluate_nickname_lengths(program, facts, tag_map, mode))
    for fact in facts.operands:
        findings.extend(_evaluate_fact(fact, tag_map, mode, resolver))

    findings.extend(
        _evaluate_immediate_usage(facts.operands, instruction_sites, tag_map, mode, resolver)
    )
    findings.extend(_evaluate_status_bit_usage(program, mode))
    findings.extend(_evaluate_forloop_structure(program, mode))

    for instruction, base_location in instruction_sites:
        findings.extend(_evaluate_instruction_portability(instruction, base_location, mode))
        findings.extend(_evaluate_range_aggregates(instruction, base_location, mode, resolver))
        findings.extend(_evaluate_timer_preset_overflow(instruction, base_location, tag_map, mode))
        findings.extend(_evaluate_out_variant_unit(instruction, base_location, mode))

    active_profile = profile if profile is not None else _load_default_profile()

    if active_profile is None:
        findings.append(
            ClickFinding(
                code=CLK_PROFILE_UNAVAILABLE,
                severity=_route_severity(CLK_PROFILE_UNAVAILABLE, mode),
                message="Click hardware profile is unavailable; skipped Stage 3 checks (R6-R8).",
                location="program",
            )
        )
    else:
        for instruction, base_location in instruction_sites:
            findings.extend(
                _evaluate_write_targets(
                    instruction, base_location, tag_map, active_profile, mode, resolver
                )
            )
            findings.extend(
                _evaluate_role_assignments(
                    instruction, base_location, tag_map, active_profile, mode, resolver
                )
            )
            findings.extend(
                _evaluate_coil_targets(instruction, base_location, active_profile, mode, resolver)
            )
            findings.extend(
                _evaluate_copy_compatibility(
                    instruction, base_location, tag_map, active_profile, mode, resolver
                )
            )
            findings.extend(
                _evaluate_pack_text(instruction, base_location, tag_map, mode, resolver)
            )
            findings.extend(
                _evaluate_drums(instruction, base_location, tag_map, active_profile, mode, resolver)
            )
            findings.extend(_evaluate_timer_pairs(instruction, base_location, mode, resolver))

    errors: list[ClickFinding] = []
    warnings: list[ClickFinding] = []
    hints: list[ClickFinding] = []

    for finding in findings:
        if finding.severity == "error":
            errors.append(finding)
        elif finding.severity == "warning":
            warnings.append(finding)
        else:
            hints.append(finding)

    return ClickValidationReport(
        errors=tuple(errors),
        warnings=tuple(warnings),
        hints=tuple(hints),
    )


__all__ = [
    "ValidationMode",
    "FindingSeverity",
    "CLK_PTR_CONTEXT_ONLY_COPY",
    "CLK_PTR_POINTER_MUST_BE_DS",
    "CLK_PTR_EXPR_NOT_ALLOWED",
    "CLK_EXPR_ONLY_IN_CALC",
    "CLK_TILDE_BOOL_CONTACT_ONLY",
    "CLK_INT_TRUTHINESS_EXPLICIT_COMPARE_REQUIRED",
    "CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED",
    "CLK_PTR_DS_UNVERIFIED",
    "CLK_FUNCTION_CALL_NOT_PORTABLE",
    "CLK_CALC_MODE_MIXED",
    "CLK_CALC_FLOOR_DIV",
    "CLK_CALC_FUNC_MODE_MISMATCH",
    "CLK_CALC_NESTING_DEPTH",
    "CLK_CALC_RANGE_MIXED_BANKS",
    "CLK_CALC_RANGE_NOT_CONTIGUOUS",
    "CLK_CALC_MEDIAN_RANGE_TOO_LARGE",
    "CLK_CALC_BIT_RANGE_AGGREGATE",
    "CLK_PROFILE_UNAVAILABLE",
    "CLK_ADDRESS_IDENTITY_CONFLICT",
    "CLK_NICKNAME_TOO_LONG",
    "CLK_BANK_UNRESOLVED",
    "CLK_BANK_NOT_WRITABLE",
    "CLK_BANK_WRONG_ROLE",
    "CLK_COPY_BANK_INCOMPATIBLE",
    "CLK_COPY_CONVERTER_INCOMPATIBLE",
    "CLK_PACK_TEXT_BANK_INCOMPATIBLE",
    "CLK_DRUM_JUMP_STEP_TAG_REQUIRED",
    "CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED",
    "CLK_IMMEDIATE_EDGE_CONTACT_NOT_ALLOWED",
    "CLK_IMMEDIATE_COIL_TARGET_MUST_BE_Y",
    "CLK_IMMEDIATE_RANGE_MUST_BE_CONTIGUOUS",
    "CLK_TIMER_PRESET_OVERFLOW",
    "CLK_OUT_UNIT_UNSUPPORTED",
    "CLK_TIMER_PAIR_MISMATCH",
    "CLK_FORLOOP_INNER_ONESHOT",
    "CLK_FORLOOP_NESTING_DEPTH",
    "CLK_FORLOOP_RETURN_INSIDE",
    "CLK_STATUS_BIT_NOT_PORTABLE",
    "ClickFinding",
    "ClickValidationReport",
]
