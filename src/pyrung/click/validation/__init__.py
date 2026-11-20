"""Click portability validation for pyrung programs.

Consumes Stage 1 walker facts for R1-R5, and instruction context plus hardware
profile data for Stage 3 (R6-R8).
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal

from laddercodec import Code

from pyrung.core.validation.walker import ProgramLocation, walk_program

from . import findings as _findings
from .checks import Check, Context
from .conditions import ConditionSites, condition_sites
from .fields import FamilyResult, evaluate_family, tag_names
from .findings import *  # noqa: F403
from .findings import (
    CLK_BANK_UNRESOLVED,
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
    _evaluate_text_past_bank,
    _evaluate_timer_pairs,
    _evaluate_wide_copy_constant,
)
from .hardware import _load_default_profile as _hardware_load_default_profile
from .identity import _evaluate_address_identity_conflicts
from .math_ranges import _evaluate_range_aggregates
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
    _is_status_bit_tag,
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


def _judge_leaves(
    leaves: ConditionSites,
    exporter: Any,
    mode: ValidationMode,
    resolver: _ResolutionCache,
    names: dict[str, str],
) -> list[FamilyResult]:
    """Return laddercodec's verdict on each contact in *leaves*, in the same order.

    Some findings are left out, because another finding reports the same fault:

    - The instruction's family reports a pin contact's CLK_BANK_UNRESOLVED.
    - CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED reports any immediate contact in a pin, so the
      contact gets no immediate-contact-not-x.
    - A timer's or counter's status bit gets CLK_STATUS_BIT_NOT_PORTABLE and no
      CLK_BANK_UNRESOLVED.
    """
    from .fields import _value_tags

    results: list[FamilyResult] = []
    for site in leaves.sites:
        result = evaluate_family(
            site.leaf, site.location, exporter, mode, resolver, names, line=site.line
        )
        if site.pin:
            refusals = tuple(r for r in result.refusals if r.code != Code.IMMEDIATE_CONTACT_NOT_X)
            result = replace(result, refusals=refusals)
        if site.pin or any(_is_status_bit_tag(tag) for tag, _name in _value_tags(site.leaf)):
            checks = [f for f in result.checks if f.code != CLK_BANK_UNRESOLVED]
            result = replace(result, checks=checks)
        results.append(result)
    return results


def _evaluate_text_pairs(leaves: ConditionSites, mode: ValidationMode) -> list[ClickFinding]:
    """Report a compare of two text constants that the CSV reads as two different compares.

    Only an inline expression has a text on both sides, such as
    ``LiteralExpr("A") == LiteralExpr("B")``. laddercodec cannot read such a cell, so pyrung
    checks it. The export refuses the compare even when the finding is allowed, so warn
    mode reports it as a warning.
    """

    from pyrung.click.ladder.translator import (
        TEXT_PAIR_ADVICE,
        text_constant_token,
        text_pair_reads_two_ways,
    )
    from pyrung.core.expression import ExprCompare, LiteralExpr
    from pyrung.core.validation.render import render_condition

    from .resolve import _format_location

    findings: list[ClickFinding] = []
    for site in leaves.sites:
        leaf = site.leaf
        if not isinstance(leaf, ExprCompare):
            continue
        left, right = leaf.left, leaf.right
        if not (isinstance(left, LiteralExpr) and isinstance(right, LiteralExpr)):
            continue
        if not (isinstance(left.value, str) and isinstance(right.value, str)):
            continue
        token = f"{text_constant_token(left.value)}{leaf.symbol}{text_constant_token(right.value)}"
        if not text_pair_reads_two_ways(token):
            continue
        location = _format_location(site.location)
        findings.append(
            ClickFinding(
                code=Code.INVALID_TEXT_CONSTANT.value,
                severity="error" if mode == "strict" else "warning",
                message=(
                    f"{render_condition(leaf)} at {location} compares two texts, and the CSV "
                    "reads it as two different compares."
                ),
                location=location,
                suggestion=TEXT_PAIR_ADVICE,
            )
        )
    return findings


# laddercodec's verdicts, by place. A place is a location without its arg_path. Each
# verdict comes with the path that it covers, such as "instruction" or "condition[1]".
_Verdicts = dict[ProgramLocation, list[tuple[str, FamilyResult]]]


def _verdicts(
    instruction_sites: list[tuple[Any, ProgramLocation]],
    judged: list[FamilyResult],
    leaves: ConditionSites,
    leaf_results: list[FamilyResult],
) -> _Verdicts:
    """Return laddercodec's verdicts on the instructions and the contacts, by place.

    Two instructions of one class in one forloop() share a place, so a place can hold
    several verdicts.
    """
    found: _Verdicts = {}
    for (_instruction, location), result in zip(instruction_sites, judged, strict=True):
        found.setdefault(_site_key(location), []).append((location.arg_path, result))
    by_location: dict[ProgramLocation, FamilyResult] = {}
    for site, result in zip(leaves.sites, leaf_results, strict=True):
        by_location[site.location] = result
        found.setdefault(_site_key(site.location), []).append((site.location.arg_path, result))
    for alias, owner in leaves.aliases.items():
        result = by_location.get(owner)
        if result is not None:
            found.setdefault(_site_key(alias), []).append((alias.arg_path, result))
    return found


def _owners(location: ProgramLocation, verdicts: _Verdicts) -> list[tuple[FamilyResult, str]]:
    """Return each verdict whose path holds the path of *location*.

    Each comes with the attribute that holds the operand, such as "source". Two
    instructions of one class in one forloop() share a place and a path, so a fact there
    could belong to either. Such a fact gets no verdict. pyrung reports it.
    """
    path = location.arg_path
    found: list[tuple[FamilyResult, str]] = []
    paths: list[str] = []
    for owner_path, result in verdicts.get(_site_key(location), ()):
        if path == owner_path:
            found.append((result, ""))
        elif path.startswith(f"{owner_path}."):
            found.append((result, path[len(owner_path) + 1 :].partition(".")[0]))
        else:
            continue
        paths.append(owner_path)
    if len(paths) != len(set(paths)):
        return []
    return found


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


# The checks that stay in pyrung, and the checks that several families share.
_CHECKS = (
    Check("program", 10, lambda c: _evaluate_immediate_usage(c.facts.operands, c.tag_map, c.mode)),
    Check("program", 30, lambda c: _evaluate_status_bit_usage(c.program, c.mode)),
    Check("program", 60, lambda c: _evaluate_text_windows(c.program, c.mode, c.resolver)),
    Check("program", 70, lambda c: _evaluate_forloop_structure(c.program, c.mode)),
    Check("program", 80, lambda c: _evaluate_nested_calls(c.program, c.mode)),
    Check("instruction", 20, lambda i, loc, c: _evaluate_instruction_portability(i, loc, c.mode)),
    Check(
        "instruction",
        30,
        lambda i, loc, c: _evaluate_range_aggregates(i, loc, c.mode, c.resolver),
    ),
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
    Check(
        "profile",
        50,
        lambda i, loc, c: _evaluate_timer_pairs(i, loc, c.profile, c.mode, c.resolver),
    ),
)


def _phase(phase: str) -> list[Check]:
    """Return the checks of *phase* in their order."""
    checks = [check for check in _CHECKS if check.phase == phase]
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
    judged = [
        evaluate_family(instruction, base_location, exporter, mode, resolver, names)
        for instruction, base_location in instruction_sites
    ]
    leaves = condition_sites(program, exporter)
    leaf_results = _judge_leaves(leaves, exporter, mode, resolver, names)
    verdicts = _verdicts(instruction_sites, judged, leaves, leaf_results)
    pointer_advice: dict[int, dict[tuple[str, str], list[str]]] = {}
    for fact in facts.operands:
        owners = _owners(fact.location, verdicts)
        for finding in _evaluate_fact(fact, tag_map, mode, resolver):
            code = _POINTER_FACT_CODES.get(finding.code)
            taken = [(r, a) for r, a in owners if code is not None and code in r.codes]
            for result, attribute in taken:
                key = (str(code), attribute)
                advice = pointer_advice.setdefault(id(result), {}).setdefault(key, [])
                if finding.suggestion and finding.suggestion not in advice:
                    advice.append(finding.suggestion)
            if not taken:
                findings.append(finding)

    context = Context(program, facts, instruction_sites, tag_map, mode, resolver)
    for check in _phase("program"):
        findings.extend(check.run(context))
    bundle = _export_bundle(program, tag_map)
    if bundle is not None:
        findings.extend(_evaluate_nop_no_coil(bundle, mode))
        findings.extend(_evaluate_step_limit(bundle, mode))

    findings.extend(_evaluate_text_pairs(leaves, mode))
    for result in leaf_results:
        findings.extend(result.findings(pointer_advice.get(id(result))))

    bounds = _PointerBounds(program)
    findings.extend(_evaluate_condition_pointer_past_bank(program, tag_map, bounds, mode, resolver))
    instruction_checks = _phase("instruction")
    for (instruction, base_location), result in zip(instruction_sites, judged, strict=True):
        findings.extend(
            _evaluate_pointer_past_bank(instruction, base_location, tag_map, bounds, mode, resolver)
        )
        for check in instruction_checks:
            findings.extend(check.run(instruction, base_location, context))
        findings.extend(result.findings(pointer_advice.get(id(result))))

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
