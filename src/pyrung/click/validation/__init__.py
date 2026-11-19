"""Click portability validation for pyrung programs.

Consumes Stage 1 walker facts for R1-R5, and instruction context plus hardware
profile data for Stage 3 (R6-R8).
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal

from pyrung.core.validation.walker import ProgramLocation, walk_program

from .fields import evaluate_copy_family, tag_names
from .findings import (
    CLK_ADDRESS_IDENTITY_CONFLICT,
    CLK_BANK_NOT_WRITABLE,
    CLK_BANK_UNRESOLVED,
    CLK_BANK_WRONG_ROLE,
    CLK_COMPARE_HEX_MIX,
    CLK_COPY_RANGE_NOT_CONSECUTIVE,
    CLK_COPY_WIDE_CONSTANT,
    CLK_DRUM_JUMP_STEP_TAG_REQUIRED,
    CLK_EXPR_ONLY_IN_MATH,
    CLK_FORLOOP_COUNT_BANK,
    CLK_FORLOOP_INNER_ONESHOT,
    CLK_FORLOOP_NESTING_DEPTH,
    CLK_FORLOOP_RETURN_INSIDE,
    CLK_FUNCTION_CALL_NOT_PORTABLE,
    CLK_IMMEDIATE_COIL_TARGET_MUST_BE_Y,
    CLK_IMMEDIATE_CONTACT_MUST_BE_X,
    CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED,
    CLK_IMMEDIATE_EDGE_CONTACT_NOT_ALLOWED,
    CLK_IMMEDIATE_RANGE_MUST_BE_CONTIGUOUS,
    CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED,
    CLK_INT_TRUTHINESS_EXPLICIT_COMPARE_REQUIRED,
    CLK_MATH_ARG_NOT_PLAIN,
    CLK_MATH_BIT_OPERAND,
    CLK_MATH_BIT_RANGE_AGGREGATE,
    CLK_MATH_DEST_BANK,
    CLK_MATH_FLOOR_DIV,
    CLK_MATH_FUNC_MODE_MISMATCH,
    CLK_MATH_MEDIAN_RANGE_TOO_LARGE,
    CLK_MATH_MODE_MIXED,
    CLK_MATH_NESTING_DEPTH,
    CLK_MATH_RANGE_MIXED_BANKS,
    CLK_MATH_RANGE_NOT_CONTIGUOUS,
    CLK_MATH_SUM_BANK_NOT_AVAILABLE,
    CLK_MATH_WIDE_CONSTANT,
    CLK_NESTED_CALL,
    CLK_NICKNAME_TOO_LONG,
    CLK_NOP_NO_COIL,
    CLK_OUT_UNIT_UNSUPPORTED,
    CLK_PROFILE_UNAVAILABLE,
    CLK_PTR_BLOCK_OFFSET,
    CLK_PTR_CONTEXT_ONLY_COPY,
    CLK_PTR_DS_UNVERIFIED,
    CLK_PTR_EXPR_NOT_ALLOWED,
    CLK_PTR_PAST_BANK,
    CLK_PTR_POINTER_MUST_BE_DS,
    CLK_RAND_BOUND_TYPE,
    CLK_RAND_EQUAL_BOUNDS,
    CLK_SEARCH_RANGE_BANK,
    CLK_SEARCH_RESULT_NOT_DS,
    CLK_STATUS_BIT_NOT_PORTABLE,
    CLK_STEP_LIMIT,
    CLK_TEXT_ORDER_COMPARE,
    CLK_TEXT_PAST_BANK,
    CLK_TEXT_PAST_BLOCK,
    CLK_TEXT_WINDOW_NOT_CONTIGUOUS,
    CLK_TILDE_BOOL_CONTACT_ONLY,
    CLK_TIMER_PAIR_MISMATCH,
    CLK_TIMER_PRESET_OVERFLOW,
    CLK_TIMER_PRESET_TOO_LOW,
    ClickFinding,
    ClickValidationReport,
    FindingSeverity,
    ValidationMode,
    _route_severity,
)
from .hardware import (
    _evaluate_coil_targets,
    _evaluate_drums,
    _evaluate_math_dest_bank,
    _evaluate_modbus_banks,
    _evaluate_out_variant_unit,
    _evaluate_role_assignments,
    _evaluate_search_banks,
    _evaluate_text_past_bank,
    _evaluate_timer_pairs,
    _evaluate_timer_preset_floor,
    _evaluate_timer_preset_overflow,
    _evaluate_wide_copy_constant,
    _evaluate_write_targets,
)
from .hardware import (
    _load_default_profile as _hardware_load_default_profile,
)
from .identity import _evaluate_address_identity_conflicts
from .nicknames import _evaluate_nickname_lengths
from .pointer_range import (
    _evaluate_condition_pointer_past_bank,
    _evaluate_pointer_past_bank,
    _PointerBounds,
)
from .portability import (
    _evaluate_compare_hex_mix,
    _evaluate_fact,
    _evaluate_forloop_count,
    _evaluate_forloop_structure,
    _evaluate_immediate_contacts,
    _evaluate_immediate_usage,
    _evaluate_instruction_portability,
    _evaluate_math_bit_operands,
    _evaluate_nested_calls,
    _evaluate_nop_no_coil,
    _evaluate_rand_bounds,
    _evaluate_range_aggregates,
    _evaluate_status_bit_usage,
    _evaluate_text_order_compares,
    _evaluate_text_windows,
    _evaluate_wide_constants,
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


#: Above this share of CLICK's limit, an estimate within its error may not fit.
_STEP_LIMIT_NEAR = 0.95


def _export_bundle(program: Program, tag_map: TagMap) -> Any:
    """Return the export of *program* for the findings that read its rows, or None."""
    import warnings

    from pyrung.click.ladder import LadderExportError, pyrung_to_ladder

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return pyrung_to_ladder(program, tag_map, validate=False)
    except LadderExportError:
        return None  # Not exportable: the export says why.


def _field_renderer(program: Program, tag_map: TagMap) -> Any:
    """Return the export's renderer, which spells each operand as the export writes it."""
    from pyrung.click.ladder._exporter import _LadderExporter

    return _LadderExporter(tag_map=tag_map, program=program)


def _site_key(location: ProgramLocation) -> ProgramLocation:
    """Return the location of the instruction that *location* is in."""
    return replace(location, arg_path="")


# pyrung's pointer codes, and the laddercodec code for the same fault.
_POINTER_FACT_CODES = {
    CLK_PTR_POINTER_MUST_BE_DS: "pointer-not-ds",
    CLK_PTR_CONTEXT_ONLY_COPY: "pointer-outside-copy",
}


def _evaluate_step_limit(bundle: Any, mode: ValidationMode) -> list[ClickFinding]:
    """Return a CLK_STEP_LIMIT finding if the estimated steps exceed or near CLICK's 8000."""
    from pyrung.click.ladder.steps import CLICK_STEP_LIMIT, estimate_steps

    estimate = estimate_steps(bundle)
    if estimate.total <= CLICK_STEP_LIMIT * _STEP_LIMIT_NEAR:
        return []
    over = estimate.total > CLICK_STEP_LIMIT
    largest = sorted(estimate.subroutines.items(), key=lambda kv: -kv[1])[:3]
    parts = [f"main {estimate.main}", *(f"{name} {steps}" for name, steps in largest)]
    verb = "exceeds" if over else "is near"
    severity: FindingSeverity = "error" if mode == "strict" else ("warning" if over else "hint")
    return [
        ClickFinding(
            code=CLK_STEP_LIMIT,
            severity=severity,
            message=(
                f"The program needs about {estimate.total} steps, and that {verb} CLICK's "
                f"limit of {CLICK_STEP_LIMIT}. Steps by part: {', '.join(parts)}."
            ),
            location="program",
            suggestion=(
                "Split the program into separate CLICK projects, or remove rungs. pyrung's "
                "estimate is within about 1% of CLICK's count."
            ),
        )
    ]


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
    # laddercodec judges the Copy family. Where it raised a pointer code, pyrung's own
    # pointer finding for that operand would report the same fault twice. The refusal's
    # finding takes the advice of each pyrung finding that it replaces. That advice names
    # each pointer that the tag map maps.
    exporter = _field_renderer(program, tag_map)
    names = tag_names(tag_map)
    copy_family = {
        _site_key(base_location): evaluate_copy_family(
            instruction, base_location, exporter, mode, resolver, names
        )
        for instruction, base_location in instruction_sites
    }
    pointer_advice: dict[ProgramLocation, dict[tuple[str, str], list[str]]] = {}
    for fact in facts.operands:
        key = _site_key(fact.location)
        judged = copy_family.get(key)
        for finding in _evaluate_fact(fact, tag_map, mode, resolver):
            code = _POINTER_FACT_CODES.get(finding.code)
            if judged is not None and code is not None and code in judged.codes:
                path = fact.location.arg_path.removeprefix("instruction.")
                operand = path.partition(".")[0]
                advice = pointer_advice.setdefault(key, {}).setdefault((code, operand), [])
                if finding.suggestion and finding.suggestion not in advice:
                    advice.append(finding.suggestion)
                continue
            findings.append(finding)

    findings.extend(
        _evaluate_immediate_usage(facts.operands, instruction_sites, tag_map, mode, resolver)
    )
    findings.extend(_evaluate_immediate_contacts(program, tag_map, mode, resolver))
    findings.extend(_evaluate_status_bit_usage(program, mode))
    findings.extend(_evaluate_text_order_compares(program, mode))
    findings.extend(_evaluate_compare_hex_mix(program, mode, resolver))
    findings.extend(_evaluate_text_windows(program, mode, resolver))
    findings.extend(_evaluate_forloop_structure(program, mode))
    findings.extend(_evaluate_nested_calls(program, mode))
    bundle = _export_bundle(program, tag_map)
    if bundle is not None:
        findings.extend(_evaluate_nop_no_coil(bundle, mode))
        findings.extend(_evaluate_step_limit(bundle, mode))

    bounds = _PointerBounds(program)
    findings.extend(_evaluate_condition_pointer_past_bank(program, tag_map, bounds, mode, resolver))
    for instruction, base_location in instruction_sites:
        findings.extend(
            _evaluate_pointer_past_bank(instruction, base_location, tag_map, bounds, mode, resolver)
        )
        findings.extend(_evaluate_instruction_portability(instruction, base_location, mode))
        findings.extend(_evaluate_range_aggregates(instruction, base_location, mode, resolver))
        findings.extend(_evaluate_timer_preset_overflow(instruction, base_location, tag_map, mode))
        findings.extend(_evaluate_timer_preset_floor(instruction, base_location, mode))
        findings.extend(_evaluate_out_variant_unit(instruction, base_location, mode))
        findings.extend(_evaluate_forloop_count(instruction, base_location, mode, resolver))
        findings.extend(_evaluate_rand_bounds(instruction, base_location, mode, resolver))
        findings.extend(_evaluate_math_bit_operands(instruction, base_location, mode))
        findings.extend(_evaluate_wide_constants(instruction, base_location, mode))
        key = _site_key(base_location)
        findings.extend(_evaluate_text_past_bank(instruction, base_location, mode, resolver))
        findings.extend(_evaluate_wide_copy_constant(instruction, base_location, mode, resolver))
        findings.extend(copy_family[key].findings(pointer_advice.get(key)))

    active_profile = profile if profile is not None else _load_default_profile()

    if active_profile is None:
        findings.append(
            ClickFinding(
                code=CLK_PROFILE_UNAVAILABLE,
                severity=_route_severity(CLK_PROFILE_UNAVAILABLE, mode),
                message=(
                    "pyrung could not load the CLICK hardware profile, so it skipped the checks "
                    "of which memory banks each instruction can use."
                ),
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
                _evaluate_drums(instruction, base_location, tag_map, active_profile, mode, resolver)
            )
            findings.extend(_evaluate_timer_pairs(instruction, base_location, mode, resolver))
            findings.extend(_evaluate_modbus_banks(instruction, base_location, mode, resolver))
            findings.extend(_evaluate_search_banks(instruction, base_location, mode, resolver))
            findings.extend(_evaluate_math_dest_bank(instruction, base_location, mode, resolver))

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
    "CLK_PTR_BLOCK_OFFSET",
    "CLK_PTR_CONTEXT_ONLY_COPY",
    "CLK_PTR_POINTER_MUST_BE_DS",
    "CLK_PTR_EXPR_NOT_ALLOWED",
    "CLK_EXPR_ONLY_IN_MATH",
    "CLK_TILDE_BOOL_CONTACT_ONLY",
    "CLK_INT_TRUTHINESS_EXPLICIT_COMPARE_REQUIRED",
    "CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED",
    "CLK_PTR_DS_UNVERIFIED",
    "CLK_FUNCTION_CALL_NOT_PORTABLE",
    "CLK_MATH_MODE_MIXED",
    "CLK_MATH_FLOOR_DIV",
    "CLK_MATH_FUNC_MODE_MISMATCH",
    "CLK_MATH_NESTING_DEPTH",
    "CLK_MATH_RANGE_MIXED_BANKS",
    "CLK_MATH_RANGE_NOT_CONTIGUOUS",
    "CLK_MATH_MEDIAN_RANGE_TOO_LARGE",
    "CLK_MATH_BIT_RANGE_AGGREGATE",
    "CLK_MATH_SUM_BANK_NOT_AVAILABLE",
    "CLK_PROFILE_UNAVAILABLE",
    "CLK_ADDRESS_IDENTITY_CONFLICT",
    "CLK_NICKNAME_TOO_LONG",
    "CLK_BANK_UNRESOLVED",
    "CLK_BANK_NOT_WRITABLE",
    "CLK_COMPARE_HEX_MIX",
    "CLK_NOP_NO_COIL",
    "CLK_BANK_WRONG_ROLE",
    "CLK_DRUM_JUMP_STEP_TAG_REQUIRED",
    "CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED",
    "CLK_IMMEDIATE_EDGE_CONTACT_NOT_ALLOWED",
    "CLK_IMMEDIATE_COIL_TARGET_MUST_BE_Y",
    "CLK_IMMEDIATE_CONTACT_MUST_BE_X",
    "CLK_COPY_RANGE_NOT_CONSECUTIVE",
    "CLK_IMMEDIATE_RANGE_MUST_BE_CONTIGUOUS",
    "CLK_TIMER_PRESET_OVERFLOW",
    "CLK_OUT_UNIT_UNSUPPORTED",
    "CLK_TIMER_PAIR_MISMATCH",
    "CLK_FORLOOP_INNER_ONESHOT",
    "CLK_FORLOOP_NESTING_DEPTH",
    "CLK_STEP_LIMIT",
    "CLK_FORLOOP_RETURN_INSIDE",
    "CLK_NESTED_CALL",
    "CLK_STATUS_BIT_NOT_PORTABLE",
    "CLK_FORLOOP_COUNT_BANK",
    "CLK_RAND_BOUND_TYPE",
    "CLK_RAND_EQUAL_BOUNDS",
    "CLK_MATH_ARG_NOT_PLAIN",
    "CLK_MATH_WIDE_CONSTANT",
    "CLK_TEXT_ORDER_COMPARE",
    "CLK_TEXT_PAST_BANK",
    "CLK_TEXT_PAST_BLOCK",
    "CLK_TEXT_WINDOW_NOT_CONTIGUOUS",
    "CLK_COPY_WIDE_CONSTANT",
    "CLK_MATH_BIT_OPERAND",
    "CLK_MATH_DEST_BANK",
    "CLK_SEARCH_RANGE_BANK",
    "CLK_SEARCH_RESULT_NOT_DS",
    "CLK_TIMER_PRESET_TOO_LOW",
    "CLK_PTR_PAST_BANK",
    "ClickFinding",
    "ClickValidationReport",
]
