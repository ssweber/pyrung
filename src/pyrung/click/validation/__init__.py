"""Click portability validation for pyrung programs.

Consumes Stage 1 walker facts for R1-R5, and instruction context plus hardware
profile data for Stage 3 (R6-R8).
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal

from pyrung.core.validation.walker import ProgramLocation, walk_program

from . import findings as _findings
from .checks import Check, Context
from .fields import evaluate_family, tag_names
from .findings import *  # noqa: F403
from .findings import (
    CLK_PROFILE_UNAVAILABLE,
    CLK_PTR_CONTEXT_ONLY_COPY,
    CLK_PTR_POINTER_MUST_BE_DS,
    CLK_STEP_LIMIT,
    ClickFinding,
    ClickValidationReport,
    FindingSeverity,
    ValidationMode,
    _route_severity,
)
from .hardware import (
    _evaluate_role_assignments,
    _evaluate_text_past_bank,
    _evaluate_timer_pairs,
    _evaluate_timer_preset_floor,
    _evaluate_timer_preset_overflow,
    _evaluate_unreadable_copy_text,
    _evaluate_wide_copy_constant,
    _evaluate_write_targets,
)
from .hardware import _load_default_profile as _hardware_load_default_profile
from .identity import _evaluate_address_identity_conflicts
from .legacy import CHECKS as _FAMILY_CHECKS
from .nicknames import _evaluate_nickname_lengths
from .pointer_range import (
    _evaluate_condition_pointer_past_bank,
    _evaluate_pointer_past_bank,
    _PointerBounds,
)
from .portability import (
    _evaluate_fact,
    _evaluate_forloop_structure,
    _evaluate_immediate_usage,
    _evaluate_instruction_portability,
    _evaluate_nested_calls,
    _evaluate_nop_no_coil,
    _evaluate_status_bit_usage,
    _evaluate_text_windows,
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


# The checks that stay in pyrung, and the checks that several families share. Each family's
# own checks are in legacy/, and their order numbers fit between these.
_CHECKS = (
    Check(
        "program",
        10,
        lambda c: _evaluate_immediate_usage(
            c.facts.operands, c.sites, c.tag_map, c.mode, c.resolver
        ),
    ),
    Check("program", 30, lambda c: _evaluate_status_bit_usage(c.program, c.mode)),
    Check("program", 60, lambda c: _evaluate_text_windows(c.program, c.mode, c.resolver)),
    Check("program", 70, lambda c: _evaluate_forloop_structure(c.program, c.mode)),
    Check("program", 80, lambda c: _evaluate_nested_calls(c.program, c.mode)),
    Check("instruction", 20, lambda i, loc, c: _evaluate_instruction_portability(i, loc, c.mode)),
    Check(
        "instruction",
        40,
        lambda i, loc, c: _evaluate_timer_preset_overflow(i, loc, c.tag_map, c.mode),
    ),
    Check("instruction", 50, lambda i, loc, c: _evaluate_timer_preset_floor(i, loc, c.mode)),
    Check(
        "instruction",
        100,
        lambda i, loc, c: _evaluate_text_past_bank(i, loc, c.mode, c.resolver),
    ),
    Check(
        "instruction",
        110,
        lambda i, loc, c: _evaluate_wide_copy_constant(i, loc, c.mode, c.resolver),
    ),
    Check("instruction", 120, lambda i, loc, c: _evaluate_unreadable_copy_text(i, loc, c.mode)),
    Check(
        "profile",
        10,
        lambda i, loc, c: _evaluate_write_targets(i, loc, c.tag_map, c.profile, c.mode, c.resolver),
    ),
    Check(
        "profile",
        20,
        lambda i, loc, c: _evaluate_role_assignments(
            i, loc, c.tag_map, c.profile, c.mode, c.resolver
        ),
    ),
    Check("profile", 50, lambda i, loc, c: _evaluate_timer_pairs(i, loc, c.mode, c.resolver)),
)


def _phase(phase: str) -> list[Check]:
    """Return the checks of *phase*, the shared ones and the families' ones, in their order."""
    checks = [check for check in (*_CHECKS, *_FAMILY_CHECKS) if check.phase == phase]
    return sorted(checks, key=lambda check: check.order)


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
    # laddercodec judges each instruction of a family in families/. Where it raised a pointer
    # code, pyrung's own pointer finding for that operand would report the same fault twice.
    # The refusal's finding takes the advice of each pyrung finding that it replaces. That
    # advice names each pointer that the tag map maps.
    exporter = _field_renderer(program, tag_map)
    names = tag_names(tag_map)
    by_laddercodec = {
        _site_key(base_location): evaluate_family(
            instruction, base_location, exporter, mode, resolver, names
        )
        for instruction, base_location in instruction_sites
    }
    pointer_advice: dict[ProgramLocation, dict[tuple[str, str], list[str]]] = {}
    for fact in facts.operands:
        key = _site_key(fact.location)
        judged = by_laddercodec.get(key)
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

    context = Context(program, facts, instruction_sites, tag_map, mode, resolver)
    for check in _phase("program"):
        findings.extend(check.run(context))
    bundle = _export_bundle(program, tag_map)
    if bundle is not None:
        findings.extend(_evaluate_nop_no_coil(bundle, mode))
        findings.extend(_evaluate_step_limit(bundle, mode))

    bounds = _PointerBounds(program)
    findings.extend(_evaluate_condition_pointer_past_bank(program, tag_map, bounds, mode, resolver))
    instruction_checks = _phase("instruction")
    for instruction, base_location in instruction_sites:
        findings.extend(
            _evaluate_pointer_past_bank(instruction, base_location, tag_map, bounds, mode, resolver)
        )
        for check in instruction_checks:
            findings.extend(check.run(instruction, base_location, context))

        key = _site_key(base_location)
        findings.extend(by_laddercodec[key].findings(pointer_advice.get(key)))

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
        context = replace(context, profile=active_profile)
        profile_checks = _phase("profile")
        for instruction, base_location in instruction_sites:
            for check in profile_checks:
                findings.extend(check.run(instruction, base_location, context))

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


__all__: list[str] = []
__all__ += _findings.__all__
