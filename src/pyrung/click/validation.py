"""Click portability validation — consumes Stage 1 walker facts and applies policy rules.

Produces a ClickValidationReport with findings categorized by severity.
Does not modify runtime execution semantics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from pyrung.core.validation.walker import walk_program

if TYPE_CHECKING:
    from pyrung.click.tag_map import TagMap
    from pyrung.core.program import Program
    from pyrung.core.validation.walker import OperandFact, ProgramLocation

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

ValidationMode = Literal["warn", "strict"]
FindingSeverity = Literal["error", "warning", "hint"]

# ---------------------------------------------------------------------------
# Finding codes
# ---------------------------------------------------------------------------

CLK_PTR_CONTEXT_ONLY_COPY = "CLK_PTR_CONTEXT_ONLY_COPY"
CLK_PTR_POINTER_MUST_BE_DS = "CLK_PTR_POINTER_MUST_BE_DS"
CLK_PTR_EXPR_NOT_ALLOWED = "CLK_PTR_EXPR_NOT_ALLOWED"
CLK_EXPR_ONLY_IN_MATH = "CLK_EXPR_ONLY_IN_MATH"
CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED = "CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED"
CLK_PTR_DS_UNVERIFIED = "CLK_PTR_DS_UNVERIFIED"


@dataclass(frozen=True)
class ClickFinding:
    code: str
    severity: FindingSeverity
    message: str
    location: str
    suggestion: str | None = None


@dataclass(frozen=True)
class ClickValidationReport:
    errors: tuple[ClickFinding, ...] = field(default_factory=tuple)
    warnings: tuple[ClickFinding, ...] = field(default_factory=tuple)
    hints: tuple[ClickFinding, ...] = field(default_factory=tuple)

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


# ---------------------------------------------------------------------------
# Location formatting
# ---------------------------------------------------------------------------


def _format_location(loc: ProgramLocation) -> str:
    """Convert a ProgramLocation into a deterministic human-readable string."""
    if loc.scope == "subroutine":
        prefix = f"subroutine[{loc.subroutine}].rung[{loc.rung_index}]"
    else:
        prefix = f"main.rung[{loc.rung_index}]"

    for branch_idx in loc.branch_path:
        prefix += f".branch[{branch_idx}]"

    if loc.instruction_index is not None:
        prefix += f".instruction[{loc.instruction_index}]({loc.instruction_type})"

    return f"{prefix}.{loc.arg_path}"


# ---------------------------------------------------------------------------
# Severity routing
# ---------------------------------------------------------------------------


def _route_severity(code: str, mode: ValidationMode) -> FindingSeverity:
    if mode == "strict":
        return "error"
    return "hint"


# ---------------------------------------------------------------------------
# Pointer memory-type resolution
# ---------------------------------------------------------------------------


def _resolve_pointer_memory_type(pointer_name: str, tag_map: TagMap) -> str | None:
    """Resolve a pointer tag name to its memory_type via mapped_slots().

    Returns the memory_type string if unambiguously resolved, else None.
    """
    found_types: set[str] = set()
    for slot in tag_map.mapped_slots():
        if slot.logical_name == pointer_name:
            found_types.add(slot.memory_type)

    if len(found_types) == 1:
        return next(iter(found_types))
    return None


# ---------------------------------------------------------------------------
# Suggestion text
# ---------------------------------------------------------------------------


def _build_suggestion(code: str, fact: OperandFact, tag_map: TagMap) -> str:
    """Build a context-aware suggestion string for a finding code.

    Uses fact.metadata and tag_map to produce specific, actionable hints.
    Falls back to generic messages when metadata is unavailable.
    """
    meta = fact.metadata

    if code == CLK_PTR_CONTEXT_ONLY_COPY:
        block_name = str(meta.get("block_name", ""))
        pointer_name = str(meta.get("pointer_name", ""))
        if block_name and pointer_name:
            return (
                f"Pointer {block_name}[{pointer_name}] can only be used inside copy(). "
                f"Use a direct tag reference here instead."
            )
        return "Use direct tag addressing in this context; keep pointer usage in copy() only."

    if code == CLK_PTR_POINTER_MUST_BE_DS:
        pointer_name = str(meta.get("pointer_name", ""))
        resolved_type = (
            _resolve_pointer_memory_type(pointer_name, tag_map) if pointer_name else None
        )
        if pointer_name and resolved_type:
            return (
                f"Pointer '{pointer_name}' is mapped to {resolved_type} memory. "
                f"Remap it to a DS address so Click hardware can use it as a pointer."
            )
        return "Use a DS tag as the pointer source for copy() addressing."

    if code == CLK_PTR_DS_UNVERIFIED:
        pointer_name = str(meta.get("pointer_name", ""))
        if pointer_name:
            return (
                f"Pointer '{pointer_name}' is not in the tag map — cannot verify it is DS. "
                f"Map it to a DS address: {pointer_name}.map_to(ds[N])"
            )
        return "Use a DS tag as the pointer source for copy() addressing."

    if code == CLK_PTR_EXPR_NOT_ALLOWED:
        block_name = str(meta.get("block_name", ""))
        expr_dsl = str(meta.get("expr_dsl", ""))
        if block_name and expr_dsl:
            # Try to get hardware offset for concrete fix
            block_entry = tag_map.block_entry_by_name(block_name)
            if block_entry is not None:
                try:
                    offset = tag_map.offset_for(block_entry.logical)
                    return (
                        f"Click cannot compute {block_name}[{expr_dsl}] at runtime. "
                        f"Store the index in a DS tag and use copy(): "
                        f"math({expr_dsl} + {offset}, Ptr); copy({block_name}[Ptr], dest)"
                    )
                except (KeyError, ValueError):
                    pass
            return (
                f"Click cannot compute {block_name}[{expr_dsl}] at runtime. "
                f"Pre-compute the index with math() into a DS pointer tag, "
                f"then use {block_name}[Ptr]."
            )
        return "Replace computed pointer arithmetic with DS pointer tag updated separately."

    if code == CLK_EXPR_ONLY_IN_MATH:
        expr_dsl = str(meta.get("expr_dsl", ""))
        if expr_dsl:
            return (
                f"Expression '{expr_dsl}' cannot be used directly here. "
                f"Move it into math({expr_dsl}, temp) and use temp in this context."
            )
        return "Move expression into math(expr, temp) and use temp in this context."

    if code == CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED:
        block_name = str(meta.get("block_name", ""))
        if block_name:
            return (
                f"Block '{block_name}' uses computed range bounds. "
                f"Use a fixed BlockRange with literal start/end addresses instead."
            )
        return "Use a fixed BlockRange with literal start/end addresses for block copy operations."

    return ""


# ---------------------------------------------------------------------------
# Rule evaluation
# ---------------------------------------------------------------------------


def _evaluate_fact(
    fact: OperandFact,
    tag_map: TagMap,
    mode: ValidationMode,
) -> list[ClickFinding]:
    """Apply all rules to a single OperandFact, returning any findings."""
    findings: list[ClickFinding] = []
    loc = fact.location
    location_str = _format_location(loc)

    # R1: IndirectRef context — allowed only in CopyInstruction source/target
    if fact.value_kind == "indirect_ref":
        allowed = loc.instruction_type == "CopyInstruction" and loc.arg_path in {
            "instruction.source",
            "instruction.target",
        }
        if not allowed:
            findings.append(
                ClickFinding(
                    code=CLK_PTR_CONTEXT_ONLY_COPY,
                    severity=_route_severity(CLK_PTR_CONTEXT_ONLY_COPY, mode),
                    message=(
                        f"Pointer (IndirectRef) used outside copy instruction at {location_str}."
                    ),
                    location=location_str,
                    suggestion=_build_suggestion(CLK_PTR_CONTEXT_ONLY_COPY, fact, tag_map),
                )
            )
        else:
            # R2: DS pointer enforcement — only for IndirectRef that passed R1
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

    # R3: IndirectExprRef — always disallowed
    if fact.value_kind == "indirect_expr_ref":
        findings.append(
            ClickFinding(
                code=CLK_PTR_EXPR_NOT_ALLOWED,
                severity=_route_severity(CLK_PTR_EXPR_NOT_ALLOWED, mode),
                message=(
                    f"Computed pointer expression (IndirectExprRef) not allowed at {location_str}."
                ),
                location=location_str,
                suggestion=_build_suggestion(CLK_PTR_EXPR_NOT_ALLOWED, fact, tag_map),
            )
        )

    # R4: Expression context — allowed only in MathInstruction.expression
    if fact.value_kind == "expression":
        allowed = (
            loc.instruction_type == "MathInstruction" and loc.arg_path == "instruction.expression"
        )
        if not allowed:
            findings.append(
                ClickFinding(
                    code=CLK_EXPR_ONLY_IN_MATH,
                    severity=_route_severity(CLK_EXPR_ONLY_IN_MATH, mode),
                    message=(f"Expression used outside math instruction at {location_str}."),
                    location=location_str,
                    suggestion=_build_suggestion(CLK_EXPR_ONLY_IN_MATH, fact, tag_map),
                )
            )

    # R5: IndirectBlockRange — always disallowed
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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_click_program(
    program: Program,
    tag_map: TagMap,
    mode: ValidationMode = "warn",
) -> ClickValidationReport:
    """Validate a Program against Click portability rules.

    Walks the program to extract facts, then evaluates each fact against
    the Click policy rules. Returns a report with categorized findings.
    """
    facts = walk_program(program)

    errors: list[ClickFinding] = []
    warnings: list[ClickFinding] = []
    hints: list[ClickFinding] = []

    for fact in facts.operands:
        for finding in _evaluate_fact(fact, tag_map, mode):
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
